#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
import sys
import time
import urllib.robotparser
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import pdfplumber
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "docs" / "data"
HISTORY = DATA / "history"
BASELINE = Path(__file__).with_name("manual_offers.json")
CNB_URL = "https://www.cnb.cz/cs/financni-trhy/devizovy-trh/kurzy-devizoveho-trhu/kurzy-devizoveho-trhu/denni_kurz.txt"
USER_AGENT = "AcquirerFeeTrackerBot/1.0 (public-interest research; weekly read-only check of public pricing pages)"
TIMEOUT = 30
ADYEN_SOURCE_ID = "src_5"
ADYEN_CEE_COUNTRIES = {"CZ", "SK", "PL", "HU", "RO", "BG", "HR", "SI", "EE", "LV", "LT"}
ADYEN_A2A_TYPES = {"Online banking", "Real-time payments", "Direct debit", "Bank transfer"}

# Poslední ručně ověřený stav slouží jako bezpečná záloha, pokud je Adyen API
# dočasně nedostupné. Při každém běhu ho nahradí živá country-by-country data.
ADYEN_CEE_A2A_SEED = (
    ("CZ", "Online banking Czech Republic", "online-banking-czech-republic", 2.0, 0.0),
    ("SK", "Online banking Slovakia", "online-banking-slovakia", 2.0, 0.0),
    ("SK", "SEPA Direct Debit", "sepa-direct-debit", 0.0, 0.27),
    ("PL", "BLIK", "blik", 1.5, 0.0),
    ("PL", "Online banking Poland", "online-banking-poland", 2.3, 0.0),
    ("EE", "Trustly", "trustly", 0.0, 0.50),
    ("LV", "Trustly", "trustly", 0.0, 0.50),
    ("LT", "Trustly", "trustly", 0.0, 0.50),
)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("update")

PCT_FIXED_RE = re.compile(
    r"(?P<pmin>\d{1,2}(?:[.,]\d{1,3})?)\s*(?:[–—-]\s*(?P<pmax>\d{1,2}(?:[.,]\d{1,3})?))?\s*%"
    r"(?:\s*(?:\+|plus)\s*(?P<fmin>\d+(?:[.,]\d+)?)\s*(?P<currency>CZK|Kč|PLN|EUR|HUF|RON|DKK|SEK))?",
    re.I,
)

ADYEN_PERCENT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*%", re.I)
ADYEN_RANGE_RE = re.compile(
    r"(?:from\s*)?(\d+(?:[.,]\d+)?)\s*%\s*(?:to|[–—-])\s*(\d+(?:[.,]\d+)?)\s*%",
    re.I,
)
ADYEN_FIXED_RE = re.compile(
    r"(?:(?P<symbol>€|\$|£)\s*(?P<before>\d+(?:[.,]\d+)?)|"
    r"(?P<after>\d+(?:[.,]\d+)?)\s*(?P<code>EUR|USD|GBP))",
    re.I,
)
ADYEN_PROCESSING_PREFIX_RE = re.compile(
    r"^(?:€|\$|£)\s*\d+(?:[.,]\d+)?\s*\+\s*", re.I
)


def decimal(s: str | None) -> float | None:
    return None if s is None else float(s.replace(",", "."))


def resolve_nuxt_payload(flat: list) -> object:
    """Resolve Nuxt/devalue's flat array of numeric references."""
    cache: dict[int, object] = {}

    def resolve(ref: object) -> object:
        if isinstance(ref, bool) or not isinstance(ref, int) or ref < 0 or ref >= len(flat):
            return ref
        if ref in cache:
            return cache[ref]
        raw = flat[ref]
        if isinstance(raw, list):
            target: list = []
            cache[ref] = target
            target.extend(resolve(item) for item in raw)
            return target
        if isinstance(raw, dict):
            target_dict: dict = {}
            cache[ref] = target_dict
            target_dict.update({key: resolve(value) for key, value in raw.items()})
            return target_dict
        cache[ref] = raw
        return raw

    return resolve(0)


def adyen_global_data(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    payload = soup.find("script", id="__NUXT_DATA__")
    if payload is None or not payload.string:
        raise ValueError("Adyen __NUXT_DATA__ payload not found")
    root = resolve_nuxt_payload(json.loads(payload.string))
    state = root["state"]
    if isinstance(state, list) and len(state) == 2 and state[0] in {"Reactive", "ShallowReactive"}:
        state = state[1]
    return state["$sglobalData"]["en"]


def adyen_country_context(global_data: dict) -> tuple[dict[str, str], dict[str, dict]]:
    currencies = {
        item.get("sys", {}).get("id"): item.get("isoCode")
        for item in global_data.get("globalDataCurrency", [])
    }
    regions = {
        item.get("sys", {}).get("id"): item
        for item in global_data.get("globalDataRegion", [])
    }
    country_id_to_code: dict[str, str] = {}
    processing: dict[str, dict] = {}
    for country in global_data.get("globalDataCountry", []):
        code = country.get("countryCode")
        country_id = country.get("sys", {}).get("id")
        if not code or not country_id:
            continue
        country_id_to_code[country_id] = code
        region = regions.get(country.get("region", {}).get("sys", {}).get("id"), {})
        amount = country.get("processingFeeAmount")
        currency_ref = country.get("processingFeeCurrency")
        if amount is None:
            amount = region.get("processingFeeAmount")
            currency_ref = region.get("processingFeeCurrency")
        currency_id = (currency_ref or {}).get("sys", {}).get("id")
        if amount is not None and currencies.get(currency_id):
            processing[code] = {"amount": float(amount), "currency": currencies[currency_id]}
    return country_id_to_code, processing


def parse_adyen_fee_text(text: str) -> dict | None:
    clean = re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()
    range_match = ADYEN_RANGE_RE.search(clean)
    pct_match = ADYEN_PERCENT_RE.search(clean)
    fixed_match = ADYEN_FIXED_RE.search(clean)
    # A fixed base fee can be followed by explanatory text mentioning a
    # surcharge (Trustly currently says that some sectors can be up to 3%).
    # Without an explicit '+' this is not the published base percentage.
    if fixed_match and fixed_match.start() == 0 and pct_match and "+" not in clean[:pct_match.start()]:
        pct_match = None
    if not pct_match and not fixed_match:
        return None
    pct_min = decimal(range_match.group(1)) if range_match else (decimal(pct_match.group(1)) if pct_match else 0.0)
    pct_max = decimal(range_match.group(2)) if range_match else pct_min
    fixed = 0.0
    currency = None
    if fixed_match:
        fixed = decimal(fixed_match.group("before") or fixed_match.group("after")) or 0.0
        symbol = fixed_match.group("symbol")
        currency = (fixed_match.group("code") or {"€": "EUR", "$": "USD", "£": "GBP"}.get(symbol, "")).upper()
    return {
        "pct_min": pct_min,
        "pct_max": pct_max,
        "fixed": fixed,
        "currency": currency,
        "icpp": "interchange" in clean.lower(),
        "raw": clean,
    }


def adyen_pricing_rows(html: str) -> dict[str, dict]:
    soup = BeautifulSoup(html, "html.parser")
    rows: dict[str, dict] = {}
    for row in soup.select('[role="row"]'):
        link = row.find("a", href=re.compile(r"^/payment-methods/"))
        cells = row.find_all(attrs={"role": "cell"}, recursive=False)
        if not link or len(cells) < 2:
            continue
        name = link.get_text(" ", strip=True)
        fee_text = cells[-1].get_text(" ", strip=True)
        # Server-side HTML combines the global processing fee and method fee in
        # one mobile cell; after hydration they are separate desktop cells.
        if len(cells) == 2:
            fee_text = ADYEN_PROCESSING_PREFIX_RE.sub("", fee_text)
        slug = link.get("href", "").rstrip("/").split("/")[-1]
        rows[slug] = {
            "name": name,
            "slug": slug,
            "fee": parse_adyen_fee_text(fee_text),
            "raw_fee": fee_text,
        }
    return rows


def parse_adyen_catalog(html: str, api_payload: dict) -> dict:
    country_ids, processing = adyen_country_context(adyen_global_data(html))
    price_rows = adyen_pricing_rows(html)
    api_methods = api_payload.get("en", {}).get("paymentsMethodsData", [])
    methods = []
    for item in api_methods:
        expected_slug = re.sub(r"[^a-z0-9]+", "-", (item.get("name") or "").lower()).strip("-")
        row = price_rows.get(expected_slug)
        types = {
            method_type.get("name")
            for method_type in item.get("paymentMethodTypeCollection", {}).get("items", [])
        }
        countries = {
            country_ids.get(entry.get("country"))
            for entry in item.get("countryData", [])
        }
        methods.append({
            "name": item.get("name"),
            "types": types,
            "countries": {code for code in countries if code},
            "slug": row.get("slug") if row else None,
            "fee": row.get("fee") if row else None,
            "raw_fee": row.get("raw_fee") if row else None,
        })
    return {"processing": processing, "methods": methods}


def adyen_pricing_components(processing: dict, method_fee: dict | None, card: bool = False) -> dict:
    components = {
        "processing_fee": {"amount": processing["amount"], "currency": processing["currency"]},
    }
    if card:
        components.update({
            "adyen_markup_pct": 0.6,
            "interchange": {
                "model": "pass-through",
                "eea_consumer_debit_reference_pct": 0.2,
                "eea_consumer_credit_reference_pct": 0.3,
            },
            "scheme_fees": "pass-through; variable",
        })
    elif method_fee:
        components["payment_method_fee"] = {
            "pct_min": method_fee["pct_min"],
            "pct_max": method_fee["pct_max"],
            "fixed": method_fee["fixed"],
            "currency": method_fee["currency"],
            "raw_public_fee": method_fee.get("raw"),
        }
    return components


def build_adyen_a2a_offer(country_code: str, country_name: str, name: str, slug: str,
                          processing: dict, method_fee: dict, checked_at: str,
                          source_hash: str | None, live: bool) -> dict:
    if method_fee.get("currency") not in (None, processing["currency"]):
        raise ValueError(f"Cannot aggregate Adyen {name}: mixed fee currencies")
    total_fixed = processing["amount"] + method_fee["fixed"]
    return {
        "id": f"{country_code}-adyen-{slug}-a2a",
        "country_iso2": country_code,
        "country": country_name,
        "provider": "Adyen",
        "provider_type": "Veřejný ceník",
        "product": f"{name} (A2A)",
        "channel": "online",
        "method": "a2a",
        "pricing_model": "Blended",
        "variable_pct_min": method_fee["pct_min"],
        "variable_pct_max": method_fee["pct_max"],
        "fixed_fee_min": round(total_fixed, 4),
        "fixed_fee_max": round(total_fixed, 4),
        "fee_currency": processing["currency"],
        "cap": None,
        "cap_currency": None,
        "monthly_fee": 0,
        "monthly_currency": processing["currency"],
        "condition": method_fee.get("raw", "") if re.search(r"\b(additional|minimum|depending|from)\b", method_fee.get("raw", ""), re.I) else "",
        "promo": False,
        "source_id": ADYEN_SOURCE_ID,
        "source_url": "https://www.adyen.com/pricing",
        "parser": {
            "type": "adyen_country_method",
            "country_code": country_code,
            "method_name": name,
            "auto_parse": True,
            "expected_currency": processing["currency"],
        },
        "pricing_components": adyen_pricing_components(processing, method_fee),
        "notes": f"Adyen processing fee plus the public payment-method fee ({method_fee.get('raw','')}). Classified as A2A from Adyen's official payment-method type, not from the method name.",
        "verification": "auto-checked by country and payment-method type" if live else "retained from last verified Adyen country audit",
        "source_status": "ok" if live else "seeded",
        "source_checked_at": checked_at,
        "source_hash": source_hash,
        "last_changed_at": None,
        "card_scheme": None,
    }


def sync_adyen_cee_offers(offers: list[dict], countries: dict, checked_at: str,
                          catalog: dict | None = None, source_hash: str | None = None) -> list[dict]:
    """Correct CEE cards and discover A2A from Adyen's official types and country IDs."""
    processing_by_country = (catalog or {}).get("processing", {})
    default_processing = {"amount": 0.11, "currency": "EUR"}
    for offer in offers:
        if offer.get("provider") != "Adyen" or offer.get("method") != "card":
            continue
        code = offer.get("country_iso2")
        if code not in ADYEN_CEE_COUNTRIES:
            continue
        processing = processing_by_country.get(code, default_processing)
        offer.update({
            "fixed_fee_min": processing["amount"],
            "fixed_fee_max": processing["amount"],
            "fee_currency": processing["currency"],
            "monthly_currency": processing["currency"],
            "pricing_components": adyen_pricing_components(processing, None, card=True),
            "notes": "Adyen processing fee + 0.60% acquiring markup. Interchange and scheme fees are passed through separately; EEA consumer-card reference caps are 0.20% debit and 0.30% credit.",
            "verification": "auto-checked by country (Adyen pricing + payment-method API)" if catalog else "retained from last verified Adyen country audit",
            "source_status": "ok" if catalog else "seeded",
            "source_checked_at": checked_at,
            "source_hash": source_hash,
            "parser": {
                "type": "adyen_country_method",
                "country_code": code,
                "method_name": "Visa / Mastercard",
                "auto_parse": True,
                "expected_currency": processing["currency"],
            },
        })

    # The generated Adyen A2A rows are rebuilt deterministically on every run.
    offers = [
        offer for offer in offers
        if not (offer.get("provider") == "Adyen" and offer.get("method") == "a2a"
                and offer.get("country_iso2") in ADYEN_CEE_COUNTRIES)
    ]
    discovered: dict[tuple[str, str], tuple] = {}
    if catalog:
        for method in catalog.get("methods", []):
            fee = method.get("fee")
            if not fee or not method.get("slug") or not (method.get("types", set()) & ADYEN_A2A_TYPES):
                continue
            for code in sorted(method.get("countries", set()) & ADYEN_CEE_COUNTRIES):
                processing = processing_by_country.get(code)
                if processing:
                    discovered[(code, method["slug"])] = (code, method["name"], method["slug"], processing, fee, True)
        # If the official API still confirms a seeded method/country pair but
        # its price row is temporarily unparsable, retain the last verified fee.
        by_name = {method.get("name"): method for method in catalog.get("methods", [])}
        for code, name, slug, pct, method_fixed in ADYEN_CEE_A2A_SEED:
            method = by_name.get(name, {})
            if code not in method.get("countries", set()) or (code, slug) in discovered:
                continue
            processing = processing_by_country.get(code, default_processing)
            discovered[(code, slug)] = (code, name, slug, processing, {
                "pct_min": pct, "pct_max": pct, "fixed": method_fixed,
                "currency": processing["currency"] if method_fixed else None,
                "icpp": False, "raw": "seeded",
            }, False)
    else:
        for code, name, slug, pct, method_fixed in ADYEN_CEE_A2A_SEED:
            processing = default_processing
            discovered[(code, slug)] = (code, name, slug, processing, {
                "pct_min": pct, "pct_max": pct, "fixed": method_fixed,
                "currency": processing["currency"] if method_fixed else None,
                "icpp": False, "raw": "seeded",
            }, False)
    for code, name, slug, processing, fee, live in discovered.values():
        offers.append(build_adyen_a2a_offer(
            code, countries[code]["name"], name, slug, processing, fee,
            checked_at, source_hash, live=live,
        ))
    return offers


def parse_fee_candidates(text: str) -> list[dict]:
    out=[]
    for m in PCT_FIXED_RE.finditer(text):
        pmin=decimal(m.group('pmin')); pmax=decimal(m.group('pmax')) or pmin
        fixed=decimal(m.group('fmin')) or 0.0
        curr=(m.group('currency') or '').upper().replace('KČ','CZK')
        out.append({'pct_min':pmin,'pct_max':pmax,'fixed_min':fixed,'fixed_max':fixed,'currency':curr or None,'start':m.start(),'raw':m.group(0)})
    return out


def parse_cnb(text: str) -> tuple[str, dict]:
    lines=[x.strip() for x in text.replace('\r','').split('\n') if x.strip()]
    # Some HTTP clients receive the entire response as one line; split the header before each country row when needed.
    if len(lines)==1 and 'země|měna|množství|kód|kurz' in lines[0]:
        raw=lines[0]
        header_end=raw.index('země|měna|množství|kód|kurz')+len('země|měna|množství|kód|kurz')
        head=raw[:header_end]
        tail=raw[header_end:].strip()
        rows=re.split(r'(?=[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ][^|]{1,35}\|[^|]+\|\d+\|[A-Z]{3}\|)',tail)
        lines=[head]+[r.strip() for r in rows if r.strip()]
    m=re.search(r'(\d{2}\.\d{2}\.\d{4})',lines[0])
    if not m: raise ValueError('CNB rate date not found')
    dt=datetime.strptime(m.group(1),'%d.%m.%Y').date().isoformat()
    rates={'CZK':{'amount':1,'rate_czk':1.0,'czk_per_unit':1.0}}
    for line in lines[1:]:
        parts=line.split('|')
        if len(parts)!=5 or parts[2]=='množství': continue
        amount=int(parts[2]); code=parts[3]; rate=float(parts[4].replace(',','.'))
        rates[code]={'amount':amount,'rate_czk':rate,'czk_per_unit':rate/amount}
    if len(rates)<5: raise ValueError(f'Only {len(rates)} CNB rates parsed')
    return dt,rates


def robots_allowed(url: str) -> bool:
    parsed=urlparse(url); origin=f'{parsed.scheme}://{parsed.netloc}'
    rp=urllib.robotparser.RobotFileParser()
    try:
        r=requests.get(origin+'/robots.txt',headers={'User-Agent':USER_AGENT},timeout=10)
        rp.parse((r.text if r.ok else '').splitlines())
        return rp.can_fetch(USER_AGENT,url)
    except requests.RequestException:
        return True


def fetch_source(url: str, fmt: str) -> tuple[str, str, str]:
    if not robots_allowed(url): raise PermissionError('robots.txt disallows this URL')
    r=requests.get(url,headers={'User-Agent':USER_AGENT,'Accept':'*/*'},timeout=TIMEOUT)
    if r.status_code in (401,403,429): raise PermissionError(f'HTTP {r.status_code}')
    r.raise_for_status(); time.sleep(0.7)
    content_hash=hashlib.sha256(r.content).hexdigest()
    if fmt=='pdf' or 'pdf' in r.headers.get('content-type','').lower():
        chunks=[]
        with pdfplumber.open(io.BytesIO(r.content)) as pdf:
            for page in pdf.pages: chunks.append(page.extract_text() or '')
        text='\n'.join(chunks)
        raw=text
    else:
        raw=r.text
        soup=BeautifulSoup(r.text,'html.parser')
        for el in soup(['script','style','noscript']): el.decompose()
        text=' '.join(soup.stripped_strings)
    return re.sub(r'\s+',' ',text),content_hash,raw


def fetch_json(url: str) -> dict:
    if not robots_allowed(url):
        raise PermissionError('robots.txt disallows this URL')
    r=requests.get(url,headers={'User-Agent':USER_AGENT,'Accept':'application/json'},timeout=TIMEOUT)
    if r.status_code in (401,403,429):
        raise PermissionError(f'HTTP {r.status_code}')
    r.raise_for_status()
    return r.json()


def select_candidate(offer: dict, text: str) -> tuple[dict|None,float,str]:
    if not offer.get('parser',{}).get('auto_parse',True): return None,0.0,'manual-only source'
    candidates=parse_fee_candidates(text)
    if not candidates: return None,0.0,'no fee expression found'
    anchor=(offer.get('parser',{}).get('anchor') or '').lower()
    expected=(offer.get('fee_currency') or '').upper()
    anchor_pos=text.lower().find(anchor) if anchor else -1
    best=None; best_score=-999
    for c in candidates:
        score=0.0
        if expected and c['currency'] in (None,expected): score+=0.25
        elif c['currency'] and c['currency']!=expected: score-=0.6
        old=offer.get('variable_pct_min')
        if old is not None: score+=max(0,0.35-abs(c['pct_min']-old)*0.12)
        if anchor_pos>=0:
            dist=abs(c['start']-anchor_pos)
            score+=max(0,0.45-dist/1400)
        if 0 <= c['pct_min'] <= 10 and 0 <= c['pct_max'] <= 10: score+=0.1
        if score>best_score: best,best_score=c,score
    confidence=min(0.99,max(0,best_score))
    return best,confidence,best['raw'] if best else 'none'


def calc(offer:dict,fx:dict,amount:float=500)->dict:
    if offer.get('variable_pct_min') is None:
        return {'fee_min_czk':None,'fee_max_czk':None,'effective_min_pct':None,'effective_max_pct':None}
    rate=fx.get(offer.get('fee_currency'),{'czk_per_unit':1})['czk_per_unit']
    mn=amount*offer['variable_pct_min']/100+(offer.get('fixed_fee_min') or 0)*rate
    mx=amount*offer['variable_pct_max']/100+(offer.get('fixed_fee_max') or 0)*rate
    return {'fee_min_czk':round(mn,4),'fee_max_czk':round(mx,4),'effective_min_pct':round(mn/amount*100,4),'effective_max_pct':round(mx/amount*100,4)}


def load_previous(baseline:dict)->dict:
    p=DATA/'latest.json'
    if p.exists():
        try:return json.loads(p.read_text(encoding='utf-8'))
        except Exception: log.exception('Previous latest.json unreadable; using baseline')
    return {'offers':deepcopy(baseline['offers']),'sources':baseline['sources'],'countries':baseline['countries'],'change_log':[]}


def write_csv(output:dict)->None:
    cols=['country_iso2','country','provider','provider_type','product','method','pricing_model','variable_pct_min','variable_pct_max','fixed_fee_min','fixed_fee_max','fee_currency','fee_500_min_czk','fee_500_max_czk','effective_500_min_pct','effective_500_max_pct','condition','source_url','verification','source_status','source_checked_at']
    with (DATA/'latest.csv').open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=cols,lineterminator='\n');w.writeheader()
        for o in output['offers']:
            c=o.get('calculation_500_czk',{})
            row={k:o.get(k) for k in cols};row.update({'fee_500_min_czk':c.get('fee_min_czk'),'fee_500_max_czk':c.get('fee_max_czk'),'effective_500_min_pct':c.get('effective_min_pct'),'effective_500_max_pct':c.get('effective_max_pct')})
            w.writerow(row)


def main()->int:
    baseline=json.loads(BASELINE.read_text(encoding='utf-8'))
    previous=load_previous(baseline)
    # DŮLEŽITÉ: vždy vycházet z čerstvého manual_offers.json, ne z minulého
    # vygenerovaného latest.json - jinak by každý další běh jen dokola
    # recykloval starý výstup a ignoroval jakékoliv ruční opravy v baseline.
    # 'previous' slouží níž jen ke sledování změn (prev_by_id), ne jako zdroj dat.
    offers=deepcopy(baseline['offers'])
    prev_by_id={o['id']:o for o in previous.get('offers',[])}
    changes=list(previous.get('change_log',[]))[-250:]
    now=datetime.now(timezone.utc).isoformat(timespec='seconds')

    try:
        r=requests.get(CNB_URL,headers={'User-Agent':USER_AGENT},timeout=TIMEOUT);r.raise_for_status()
        fx_date,fx=parse_cnb(r.text)
    except Exception:
        log.exception('CNB update failed; preserving previous FX data')
        fx_block=previous.get('fx')
        if not fx_block:return 1
        fx_date,fx=fx_block['date'],fx_block['rates']

    fetched={}
    for sid,s in baseline['sources'].items():
        try:
            text,h,raw=fetch_source(s['url'],s.get('format','html'))
            fetched[sid]={'text':text,'raw':raw,'hash':h,'status':'ok','checked_at':now}
            if s.get('api_url'):
                fetched[sid]['api_payload']=fetch_json(s['api_url'])
            log.info('Fetched %s (%d chars)',sid,len(text))
        except Exception as exc:
            fetched[sid]={'text':'','raw':'','hash':None,'status':f'error: {exc}','checked_at':now}
            log.warning('Source %s failed: %s',sid,exc)

    adyen_source=fetched.get(ADYEN_SOURCE_ID,{})
    adyen_catalog=None
    if adyen_source.get('status')=='ok' and adyen_source.get('api_payload'):
        try:
            adyen_catalog=parse_adyen_catalog(adyen_source['raw'],adyen_source['api_payload'])
            log.info('Parsed Adyen country catalog: %d methods',len(adyen_catalog['methods']))
        except Exception as exc:
            adyen_source['status']=f'error: Adyen country parser: {exc}'
            log.exception('Adyen country parser failed; using verified fallback')
    offers=sync_adyen_cee_offers(
        offers,baseline['countries'],now,adyen_catalog,adyen_source.get('hash')
    )

    for o in offers:
        sid=o.get('source_id')
        if o.get('parser',{}).get('type')=='adyen_country_method':
            src=fetched.get(sid,{})
            o['source_checked_at']=now
            if src.get('hash'):o['source_hash']=src['hash']
            if not adyen_catalog:
                o['source_status']=src.get('status','not checked')
                o['verification']='retained – Adyen country/method source unavailable'
            old=prev_by_id.get(o['id'])
            if old:
                for field in ('variable_pct_min','variable_pct_max','fixed_fee_min','fixed_fee_max','fee_currency'):
                    if old.get(field)!=o.get(field):
                        changes.append({'detected_at':now,'offer_id':o['id'],'field':field,'old':old.get(field),'new':o.get(field),'source_url':o['source_url'],'confidence':0.99,'raw_match':'Adyen country + payment-method API'})
                        o['last_changed_at']=now
            else:
                changes.append({'detected_at':now,'offer_id':o['id'],'field':'offer','old':None,'new':'added','source_url':o['source_url'],'confidence':0.99,'raw_match':'Adyen country + payment-method API'})
                o['last_changed_at']=now
            o['calculation_500_czk']=calc(o,fx)
            continue
        # Ručně zadané nabídky (bez source_id, nebo výslovně auto_parse=False) se
        # vůbec nezkoušejí automaticky kontrolovat - jejich verification text píše
        # člověk, skript ho nemá přepisovat matoucím "source unavailable" hlášením.
        if not sid or not o.get('parser',{}).get('auto_parse',True):
            o['source_checked_at']=now
            o['calculation_500_czk']=calc(o,fx)
            continue
        src=fetched.get(sid,{})
        o['source_checked_at']=now;o['source_status']=src.get('status','not checked')
        if src.get('hash'):o['source_hash']=src['hash']
        if src.get('status')!='ok':
            o['verification']='retained – source unavailable';o['calculation_500_czk']=calc(o,fx);continue
        old=prev_by_id.get(o['id'],o)
        cand,conf,why=select_candidate(o,src['text'])
        if cand and conf>=0.88:
            newvals={'variable_pct_min':cand['pct_min'],'variable_pct_max':cand['pct_max'],'fixed_fee_min':cand['fixed_min'],'fixed_fee_max':cand['fixed_max']}
            changed=False
            for k,v in newvals.items():
                if old.get(k)!=v:
                    changes.append({'detected_at':now,'offer_id':o['id'],'field':k,'old':old.get(k),'new':v,'source_url':o['source_url'],'confidence':round(conf,3),'raw_match':why})
                    o[k]=v;changed=True
            o['verification']=f'auto-checked ({conf:.0%})'
            if changed:o['last_changed_at']=now
        else:
            o['verification']=f'retained – review suggested ({conf:.0%}; {why})'
        o['calculation_500_czk']=calc(o,fx)

    output={'generated_at':now,'update_frequency':'weekly','default_transaction_czk':500,'methodology_version':'1.1',
            'scope_note':'Publicly displayed merchant acceptance prices. Acquirers, PSPs, gateways and A2A wallets are separated by provider type; they are not automatically treated as economically identical.',
            'fx':{'source':'Česká národní banka','source_url':CNB_URL,'date':fx_date,'rates':fx},'sources':baseline['sources'],'countries':baseline['countries'],'offers':offers,'change_log':changes[-250:]}
    DATA.mkdir(parents=True,exist_ok=True);HISTORY.mkdir(parents=True,exist_ok=True)
    (DATA/'latest.json').write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
    (DATA/'changes.json').write_text(json.dumps(changes[-250:],ensure_ascii=False,indent=2),encoding='utf-8')
    today=date.today().isoformat();(HISTORY/f'{today}.json').write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
    idx=[]
    for p in sorted(HISTORY.glob('*.json')):
        if p.name!='index.json':idx.append({'date':p.stem,'file':p.name})
    (HISTORY/'index.json').write_text(json.dumps(idx,indent=2),encoding='utf-8')
    write_csv(output)
    log.info('Wrote %d offers, CNB FX %s, %d history points',len(offers),fx_date,len(idx))
    return 0

if __name__=='__main__':sys.exit(main())
