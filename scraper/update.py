#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import re
import sys
import time
import unicodedata
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
CEE_REGISTRY = Path(__file__).with_name("cee_acquirer_registry.json")
CEE_VERIFIED = Path(__file__).with_name("cee_verified_offers.json")
EUROPE_WATCHLIST = Path(__file__).with_name("europe_acquirer_watchlist.json")
EUROPE_REGISTRY = Path(__file__).with_name("europe_acquirer_registry.json")
EUROPE_VERIFIED = Path(__file__).with_name("europe_verified_offers.json")
CNB_URL = "https://www.cnb.cz/cs/financni-trhy/devizovy-trh/kurzy-devizoveho-trhu/kurzy-devizoveho-trhu/denni_kurz.txt"
USER_AGENT = "AcquirerFeeTrackerBot/1.0 (public-interest research; weekly read-only check of public pricing pages)"
TIMEOUT = 30
ADYEN_SOURCE_ID = "src_5"
ADYEN_CEE_COUNTRIES = {"CZ", "SK", "PL", "HU", "RO", "BG", "HR", "SI", "EE", "LV", "LT"}
ADYEN_EUROPE_COUNTRIES = {
    "AT", "BE", "CH", "CY", "DE", "DK", "ES", "FI", "FR", "GB", "GR",
    "IE", "IS", "IT", "LI", "LU", "MT", "NL", "NO", "PT", "SE",
}
ADYEN_CARD_COUNTRIES = ADYEN_CEE_COUNTRIES | ADYEN_EUROPE_COUNTRIES
ADYEN_ICPP_REFERENCE_COUNTRIES = ADYEN_CARD_COUNTRIES - {"CH"}
ADYEN_A2A_TYPES = {"Online banking", "Real-time payments", "Direct debit", "Bank transfer"}
REFERENCE_TRANSACTION_EUR = 20.0
ICPP_EEA_DEBIT_INTERCHANGE_REFERENCE_PCT = 0.20
ICPP_EEA_SCHEME_FEE_REFERENCE_PCT = 0.15
ICPP_SCHEME_FEE_SOURCE_URL = "https://www.paybyrd.com/pricing/scheme-fees"

# The registry is deliberately provider-led, while the dataset sometimes uses
# a shorter commercial label.  These aliases make the reconciliation explicit
# instead of relying on fuzzy matching that could merge two different firms.
REGISTRY_DATASET_ALIASES = {
    ("CZ", "Global Payments s.r.o."): "Global Payments",
    ("SK", "ČSOB Slovensko GP WebPay"): "ČSOB Slovensko",
    ("HU", "CIB Bank eCommerce"): "CIB Bank",
    ("RO", "Banca Transilvania eCommerce"): "Banca Transilvania",
    ("RO", "Raiffeisen Romania eCommerce"): "Raiffeisen Romania",
    ("BG", "DSK Bank Virtual POS"): "DSK Bank",
    ("BG", "United Bulgarian Bank Virtual POS"): "United Bulgarian Bank",
    ("BG", "Postbank Virtual POS"): "Postbank",
    ("HR", "Zagrebačka banka eCommerce"): "Zagrebačka banka",
    ("SI", "NLB E-Commerce"): "NLB",
}

COUNTRY_FEE_CURRENCIES = {
    "GB": "GBP", "CH": "CHF", "LI": "CHF", "DK": "DKK", "SE": "SEK",
    "NO": "NOK", "IS": "ISK",
}
REGISTRY_PROVIDER_TYPES = {
    "acquiring_bank": "Acquirer (banka)",
    "direct_acquirer": "Acquirer",
    "acquirer_sales_channel": "Acquirer – distribuční kanál",
    "full_service_psp": "PSP s acquiringem",
    "gateway_or_processor": "Brána / procesor (bez acquiringu)",
    "a2a_provider": "A2A poskytovatel",
}

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


def provider_key(value: str | None) -> str:
    plain=unicodedata.normalize('NFKD',value or '').encode('ascii','ignore').decode('ascii')
    return re.sub(r'[^a-z0-9]+','',plain.lower())


def provider_role(offer: dict) -> str:
    """Return the provider's economic role, independently of payment method."""
    kind=(offer.get('provider_type') or '').lower()
    method=offer.get('method')
    if any(token in kind for token in ('bez acquiringu','gateway only','brána / procesor','brana / procesor')):
        return 'gateway_processor'
    if any(token in kind for token in ('distribuční kanál','distribucni kanal','sales channel')):
        return 'acquirer_sales_channel'
    if ('acquirer' in kind and 's acquiringem' not in kind) or offer.get('provider') in {'Adyen','Clearhaus'}:
        return 'acquirer'
    if any(token in kind for token in ('psp','wallet','veřejný ceník','verejny cenik')):
        return 'psp'
    if 'a2a poskytovatel' in kind or method=='a2a':
        return 'a2a_provider'
    return 'other'


def assign_provider_roles(offers: list[dict]) -> list[dict]:
    """Keep one organisation role across its card and A2A rows in a country."""
    priority={
        'other':0,
        'a2a_provider':1,
        'gateway_processor':2,
        'psp':3,
        'acquirer_sales_channel':4,
        'acquirer':5,
    }
    roles:dict[tuple[str,str],str]={}
    for offer in offers:
        key=(offer.get('country_iso2',''),provider_key(offer.get('provider')))
        candidate=provider_role(offer)
        current=roles.get(key,'other')
        if priority[candidate]>priority[current]:
            roles[key]=candidate
    for offer in offers:
        key=(offer.get('country_iso2',''),provider_key(offer.get('provider')))
        offer['provider_role']=roles.get(key,provider_role(offer))
    return offers


def build_registry_audit(registry: dict, offers: list[dict], aliases: dict | None = None) -> dict:
    """Compare independent provider discovery with the publishable dataset."""
    aliases = aliases or {}
    by_country: dict[str, list[dict]]={}
    for offer in offers:
        by_country.setdefault(offer.get('country_iso2',''),[]).append(offer)
    rows=[]
    providers=registry.get('providers',[])
    for code in sorted({item['country_iso2'] for item in providers}):
        discovered=[item for item in providers if item['country_iso2']==code]
        dataset=by_country.get(code,[])
        dataset_keys={provider_key(item.get('provider')) for item in dataset}
        matched=[]; missing=[]
        for item in discovered:
            dataset_name=aliases.get((code,item['provider']),item['provider'])
            (matched if provider_key(dataset_name) in dataset_keys else missing).append(item['provider'])
        acquirers=[item for item in discovered if item['role'] in {'acquiring_bank','direct_acquirer'}]
        matched_acquirers=[]
        for item in acquirers:
            dataset_name=aliases.get((code,item['provider']),item['provider'])
            if provider_key(dataset_name) in dataset_keys:
                matched_acquirers.append(item['provider'])
        rows.append({
            'country_iso2':code,
            'discovered_providers':len(discovered),
            'discovered_acquirers':len(acquirers),
            'matched_providers':len(matched),
            'matched_acquirers':len(matched_acquirers),
            'missing_from_dataset':missing,
        })
    return {
        'as_of':registry.get('as_of'),
        'methodology':registry.get('scope'),
        'discovered_provider_count':len(providers),
        'discovered_acquirer_count':sum(1 for item in providers if item['role'] in {'acquiring_bank','direct_acquirer'}),
        'countries':rows,
    }


def build_cee_audit(registry: dict, offers: list[dict]) -> dict:
    return build_registry_audit(registry, offers, REGISTRY_DATASET_ALIASES)


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
                "eea_consumer_debit_reference_pct": ICPP_EEA_DEBIT_INTERCHANGE_REFERENCE_PCT,
                "eea_consumer_credit_reference_pct": 0.3,
            },
            "scheme_fees": "pass-through; variable",
            "comparison_reference": {
                "profile": "EEA consumer debit card, authenticated Visa/Mastercard",
                "interchange_pct": ICPP_EEA_DEBIT_INTERCHANGE_REFERENCE_PCT,
                "scheme_fee_pct": ICPP_EEA_SCHEME_FEE_REFERENCE_PCT,
                "total_addon_pct": round(
                    ICPP_EEA_DEBIT_INTERCHANGE_REFERENCE_PCT
                    + ICPP_EEA_SCHEME_FEE_REFERENCE_PCT,
                    4,
                ),
                "scheme_fee_source_url": ICPP_SCHEME_FEE_SOURCE_URL,
            },
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
        "provider_type": "A2A poskytovatel",
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
    """Model Adyen cards across Europe and discover CEE A2A by official country IDs."""
    processing_by_country = (catalog or {}).get("processing", {})
    default_processing = {"amount": 0.11, "currency": "EUR"}
    existing_card_countries = {
        offer.get("country_iso2") for offer in offers
        if offer.get("provider") == "Adyen" and offer.get("method") == "card"
    }
    for code in sorted(ADYEN_CARD_COUNTRIES - existing_card_countries):
        if code not in countries:
            continue
        processing = processing_by_country.get(
            code,
            {"amount": 0.11, "currency": "GBP" if code == "GB" else "EUR"},
        )
        offers.append({
            "id": f"{code}-adyen-visa-mastercard-markup-card",
            "country_iso2": code,
            "country": countries[code]["name"],
            "provider": "Adyen",
            "provider_type": "Acquirer",
            "product": "Visa/Mastercard",
            "channel": "online",
            "method": "card",
            "pricing_model": "IC++",
            "variable_pct_min": 0.6,
            "variable_pct_max": 0.6,
            "fixed_fee_min": processing["amount"],
            "fixed_fee_max": processing["amount"],
            "fee_currency": processing["currency"],
            "monthly_fee": 0,
            "monthly_currency": processing["currency"],
            "card_scheme": "intl",
            "source_id": ADYEN_SOURCE_ID,
            "source_url": "https://www.adyen.com/pricing",
        })
    for offer in offers:
        if offer.get("provider") != "Adyen" or offer.get("method") != "card":
            continue
        code = offer.get("country_iso2")
        if code not in ADYEN_CARD_COUNTRIES:
            continue
        processing = processing_by_country.get(
            code,
            {"amount": 0.11, "currency": "GBP" if code == "GB" else "EUR"},
        )
        components = adyen_pricing_components(processing, None, card=True)
        has_reference = code in ADYEN_ICPP_REFERENCE_COUNTRIES
        if code == "GB":
            components["comparison_reference"]["profile"] = "UK domestic consumer debit card, authenticated Visa/Mastercard"
        if not has_reference:
            components.pop("comparison_reference", None)
        product = (
            "UK domácí spotřebitelská debetní karta (IC++ srovnávací odhad)"
            if code == "GB" else
            "Visa/Mastercard IC++ (srovnávací odhad zatím nedostupný)"
            if code == "CH" else
            "EEA spotřebitelská debetní karta (IC++ srovnávací odhad)"
        )
        offer.update({
            "provider_type": "Acquirer",
            "product": product,
            "fixed_fee_min": processing["amount"],
            "fixed_fee_max": processing["amount"],
            "fee_currency": processing["currency"],
            "monthly_currency": processing["currency"],
            "pricing_components": components,
            "all_in_complete": False,
            "comparison_estimate": has_reference,
            "notes": (
                "Adyen processing fee + 0.60% acquiring markup. The dashboard comparison adds a uniform domestic/EEA consumer-debit reference of 0.20% interchange + 0.15% scheme fees. This is a modelled comparison value, not a guaranteed transaction quote."
                if has_reference else
                "Adyen processing fee + 0.60% acquiring markup. No all-in comparison is shown for Switzerland until a suitable domestic interchange reference is verified."
            ),
            "verification": "auto-checked by country (Adyen pricing + payment-method API)" if catalog else "retained from last verified Adyen country audit",
            "source_status": "ok" if catalog else "seeded",
            "source_checked_at": checked_at,
            "source_hash": source_hash,
            "source_id": ADYEN_SOURCE_ID,
            "source_url": "https://www.adyen.com/pricing",
            "parser": {
                "type": "adyen_country_method",
                "country_code": code,
                "method_name": "Visa / Mastercard",
                "auto_parse": True,
                "expected_currency": processing["currency"],
            },
            "minimum_fee": None,
            "cap": None,
            "cap_currency": None,
            "condition": "",
            "promo": False,
            "last_changed_at": offer.get("last_changed_at"),
            "card_scheme": "intl",
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
            if code not in countries:
                continue
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
            if code not in countries:
                continue
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


def calc(offer:dict,fx:dict,amount:float|None=None)->dict:
    if amount is None:
        amount=REFERENCE_TRANSACTION_EUR*fx.get('EUR',{'czk_per_unit':1})['czk_per_unit']
    is_estimate=offer.get('comparison_estimate') is True
    if offer.get('variable_pct_min') is None or (offer.get('all_in_complete') is False and not is_estimate):
        return {'fee_min_czk':None,'fee_max_czk':None,'effective_min_pct':None,'effective_max_pct':None}
    rate=fx.get(offer.get('fee_currency'),{'czk_per_unit':1})['czk_per_unit']
    comparison=(offer.get('pricing_components') or {}).get('comparison_reference') or {}
    addon=comparison.get('total_addon_pct') or 0
    variable_min=offer['variable_pct_min']+addon
    variable_max=offer['variable_pct_max']+addon
    mn=amount*variable_min/100+(offer.get('fixed_fee_min') or 0)*rate
    mx=amount*variable_max/100+(offer.get('fixed_fee_max') or 0)*rate
    minimum=(offer.get('minimum_fee') or 0)*rate
    mn=max(mn,minimum)
    mx=max(mx,minimum)
    return {'fee_min_czk':round(mn,4),'fee_max_czk':round(mx,4),'effective_min_pct':round(mn/amount*100,4),'effective_max_pct':round(mx/amount*100,4)}


def load_previous(baseline:dict)->dict:
    p=DATA/'latest.json'
    if p.exists():
        try:return json.loads(p.read_text(encoding='utf-8'))
        except Exception: log.exception('Previous latest.json unreadable; using baseline')
    return {'offers':deepcopy(baseline['offers']),'sources':baseline['sources'],'countries':baseline['countries'],'change_log':[]}


def apply_cee_verified_overlay(offers:list[dict], countries:dict)->list[dict]:
    """Apply source-reviewed CEE corrections without rewriting the legacy seed file.

    The overlay is intentionally separate from the independently researched
    provider registry.  This keeps discovery and reconciliation as two distinct
    steps and makes every replacement/removal reviewable.
    """
    if not CEE_VERIFIED.exists():
        return offers
    overlay=json.loads(CEE_VERIFIED.read_text(encoding='utf-8'))
    remove_ids=set(overlay.get('remove_ids',[]))
    replacement_ids={item['id'] for item in overlay.get('offers',[])}
    merged=[o for o in offers if o['id'] not in remove_ids and o['id'] not in replacement_ids]
    for raw in overlay.get('offers',[]):
        item=deepcopy(raw)
        iso=item['country_iso2']
        currency=item['fee_currency']
        item.setdefault('country',countries[iso]['name'])
        item.setdefault('channel','online')
        item.setdefault('product','Standard')
        item.setdefault('pricing_model','Blended')
        item.setdefault('variable_pct_max',item.get('variable_pct_min'))
        item.setdefault('fixed_fee_min',0 if item.get('variable_pct_min') is not None else None)
        item.setdefault('fixed_fee_max',item.get('fixed_fee_min'))
        item.setdefault('minimum_fee',None)
        item.setdefault('cap',None)
        item.setdefault('cap_currency',None)
        item.setdefault('monthly_fee',0)
        item.setdefault('monthly_currency',currency)
        item.setdefault('condition','')
        item.setdefault('promo',False)
        item.setdefault('source_id',None)
        item.setdefault('parser',{'anchor':'','auto_parse':False,'expected_currency':currency})
        item.setdefault('notes','Country-by-country CEE acquirer review; official provider source.')
        item.setdefault('verification','ručně ověřeno na oficiálním zdroji 18. 8. 2026')
        item.setdefault('source_status','manual')
        item.setdefault('source_checked_at',None)
        item.setdefault('source_hash',None)
        item.setdefault('last_changed_at',None)
        item.setdefault('card_scheme',None)
        merged.append(item)
    ids=[o['id'] for o in merged]
    if len(ids)!=len(set(ids)):
        raise ValueError('Duplicate offer id after CEE verified overlay')
    return merged


def _normalise_verified_offer(raw: dict, countries: dict) -> dict:
    item=deepcopy(raw)
    iso=item['country_iso2']
    currency=item['fee_currency']
    item.setdefault('country',countries[iso]['name'])
    item.setdefault('channel','online')
    item.setdefault('product','Standard')
    item.setdefault('pricing_model','Blended')
    item.setdefault('variable_pct_max',item.get('variable_pct_min'))
    item.setdefault('fixed_fee_min',0 if item.get('variable_pct_min') is not None else None)
    item.setdefault('fixed_fee_max',item.get('fixed_fee_min'))
    item.setdefault('minimum_fee',None)
    item.setdefault('cap',None)
    item.setdefault('cap_currency',None)
    item.setdefault('monthly_fee',0)
    item.setdefault('monthly_currency',currency)
    item.setdefault('condition','')
    item.setdefault('promo',False)
    item.setdefault('source_id',None)
    item.setdefault('parser',{'anchor':'','auto_parse':False,'expected_currency':currency})
    item.setdefault('notes','Independent country review; official provider source.')
    item.setdefault('verification','ručně ověřeno na oficiálním zdroji 18. 8. 2026')
    item.setdefault('source_status','manual')
    item.setdefault('source_checked_at',None)
    item.setdefault('source_hash',None)
    item.setdefault('last_changed_at',None)
    item.setdefault('card_scheme',None)
    item.setdefault('all_in_complete',True)
    return item


def _registry_individual_offer(provider: dict, countries: dict) -> dict:
    iso=provider['country_iso2']
    currency=COUNTRY_FEE_CURRENCIES.get(iso,'EUR')
    method=provider['method']
    role=provider['role']
    return _normalise_verified_offer({
        'id':f"{iso}-{provider_key(provider['provider'])}-{method}-registry",
        'country_iso2':iso,
        'provider':provider['provider'],
        'provider_type':REGISTRY_PROVIDER_TYPES[role],
        'product':'Merchant acquiring' if method=='card' else 'A2A převod',
        'method':method,
        'pricing_model':'Individual',
        'variable_pct_min':None,
        'fixed_fee_min':None,
        'fee_currency':currency,
        'monthly_fee':0,
        'card_scheme':'intl' if method=='card' else None,
        'source_url':provider['official_url'],
        'verification':'ověřena role a lokální nabídka na oficiálním zdroji 18. 8. 2026; veřejná kompletní sazba nenalezena',
        'notes':'Role a lokální dostupnost ověřeny; číselná cena se nezobrazuje, protože nebyla zveřejněna jako kompletní merchant sazba.',
        'all_in_complete':False,
    },countries)


def apply_europe_verified_overlay(offers:list[dict], countries:dict)->list[dict]:
    """Replace the old non-CEE seed with registry-led, source-reviewed rows."""
    if not EUROPE_VERIFIED.exists() or not EUROPE_REGISTRY.exists():
        return offers
    overlay=json.loads(EUROPE_VERIFIED.read_text(encoding='utf-8'))
    registry=json.loads(EUROPE_REGISTRY.read_text(encoding='utf-8'))
    removed_countries=set(overlay.get('remove_country_iso2',[]))
    merged=[o for o in offers if o.get('country_iso2') not in removed_countries]
    explicit=[_normalise_verified_offer(raw,countries) for raw in overlay.get('offers',[])]
    explicit_keys={(o['country_iso2'],provider_key(o['provider']),o['method']) for o in explicit}
    defaults=[]
    for provider in registry.get('providers',[]):
        if provider['country_iso2'] not in removed_countries:
            raise ValueError(f"Europe registry country outside overlay scope: {provider['country_iso2']}")
        for method in provider.get('methods',[]):
            keyed={**provider,'method':method}
            key=(provider['country_iso2'],provider_key(provider['provider']),method)
            if key not in explicit_keys:
                defaults.append(_registry_individual_offer(keyed,countries))
    merged.extend(explicit)
    merged.extend(defaults)
    ids=[o['id'] for o in merged]
    if len(ids)!=len(set(ids)):
        raise ValueError('Duplicate offer id after Europe verified overlay')
    return merged


def normalize_revolut_cee_offers(offers:list[dict])->list[dict]:
    """Keep Revolut as a baseline, but use the current legal-table labels.

    Country fixed fees remain those already stored in the country rows.  The
    correction here is semantic: Revolut Pay retail A2A is not a generic wallet
    or Pay-by-Bank product, and the old EUR 5 cap is absent from the current
    legal price table.
    """
    for offer in offers:
        if offer.get('provider')!='Revolut' or offer.get('country_iso2') not in ADYEN_CEE_COUNTRIES:
            continue
        offer['source_url']='https://www.revolut.com/en-CZ/legal/business-basic-fees/'
        offer['source_id']=None
        offer['parser']={'anchor':'','auto_parse':False,'expected_currency':offer.get('fee_currency')}
        offer['source_status']='manual'
        offer['verification']='ručně ověřeno v oficiálním ceníku Revolut Business 18. 8. 2026'
        if offer.get('method')=='card':
            offer['provider_type']='Acquirer'
            offer['product']='Online EEA spotřebitelské karty'
            offer['condition']='American Express 1,7 % + lokální fixní poplatek; ostatní karty 2,8 % + lokální fixní poplatek.'
            offer['card_scheme']='intl'
        elif offer.get('method')=='a2a':
            offer['provider_type']='A2A poskytovatel'
            offer['product']='Revolut Pay – A2A od retail zákazníka'
            offer['condition']='Revolut Pay Account-to-account od retail zákazníka; Business/Pro zákazník 2,8 % + lokální fixní poplatek.'
            offer['cap']=None
            offer['cap_currency']=None
            offer['card_scheme']=None
    return offers


def normalize_card_schemes(offers:list[dict])->list[dict]:
    """Use card_scheme for the network, not for the cardholder's country.

    Clearhaus calls EEA-issued consumer cards "domestic", but they are still
    Visa/Mastercard.  True national schemes such as girocard remain domestic.
    """
    for offer in offers:
        if offer.get('provider')=='Clearhaus' and offer.get('method')=='card':
            offer['card_scheme']='intl'
    return offers


def write_csv(output:dict)->None:
    cols=['country_iso2','country','provider','provider_type','provider_role','product','method','pricing_model','variable_pct_min','variable_pct_max','fixed_fee_min','fixed_fee_max','minimum_fee','fee_currency','reference_transaction_eur','fee_reference_min_czk','fee_reference_max_czk','effective_reference_min_pct','effective_reference_max_pct','condition','source_url','verification','source_status','source_checked_at']
    with (DATA/'latest.csv').open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=cols,lineterminator='\n');w.writeheader()
        for o in output['offers']:
            c=o.get('calculation_reference',{})
            row={k:o.get(k) for k in cols};row.update({'reference_transaction_eur':REFERENCE_TRANSACTION_EUR,'fee_reference_min_czk':c.get('fee_min_czk'),'fee_reference_max_czk':c.get('fee_max_czk'),'effective_reference_min_pct':c.get('effective_min_pct'),'effective_reference_max_pct':c.get('effective_max_pct')})
            w.writerow(row)


def main()->int:
    baseline=json.loads(BASELINE.read_text(encoding='utf-8'))
    previous=load_previous(baseline)
    # DŮLEŽITÉ: vždy vycházet z čerstvého manual_offers.json, ne z minulého
    # vygenerovaného latest.json - jinak by každý další běh jen dokola
    # recykloval starý výstup a ignoroval jakékoliv ruční opravy v baseline.
    # 'previous' slouží níž jen ke sledování změn (prev_by_id), ne jako zdroj dat.
    offers=normalize_card_schemes(normalize_revolut_cee_offers(
        apply_europe_verified_overlay(
            apply_cee_verified_overlay(deepcopy(baseline['offers']),baseline['countries']),
            baseline['countries'],
        )
    ))
    prev_by_id={o['id']:o for o in previous.get('offers',[])}
    changes=list(previous.get('change_log',[]))[-250:]
    now=datetime.now(timezone.utc).isoformat(timespec='seconds')

    offline=os.environ.get('ACQ_TRACKER_OFFLINE')=='1'
    try:
        if offline:
            raise ConnectionError('offline regeneration requested')
        r=requests.get(CNB_URL,headers={'User-Agent':USER_AGENT},timeout=TIMEOUT);r.raise_for_status()
        fx_date,fx=parse_cnb(r.text)
    except Exception:
        if offline:
            log.info('Offline regeneration: preserving previous FX data')
        else:
            log.exception('CNB update failed; preserving previous FX data')
        fx_block=previous.get('fx')
        if not fx_block:return 1
        fx_date,fx=fx_block['date'],fx_block['rates']

    fetched={}
    for sid,s in baseline['sources'].items():
        if offline:
            fetched[sid]={'text':'','raw':'','hash':None,'status':'offline','checked_at':now}
            continue
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
                old=prev_by_id.get(o['id'])
                if old:
                    for field in ('source_status','source_checked_at','source_hash','verification'):
                        o[field]=old.get(field)
                else:
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
            o['calculation_reference']=calc(o,fx)
            continue
        # Ručně zadané nabídky (bez source_id, nebo výslovně auto_parse=False) se
        # vůbec nezkoušejí automaticky kontrolovat - jejich verification text píše
        # člověk, skript ho nemá přepisovat matoucím "source unavailable" hlášením.
        if not sid or not o.get('parser',{}).get('auto_parse',True):
            o['source_checked_at']=now
            o['calculation_reference']=calc(o,fx)
            continue
        src=fetched.get(sid,{})
        o['source_checked_at']=now;o['source_status']=src.get('status','not checked')
        if src.get('hash'):o['source_hash']=src['hash']
        if src.get('status')!='ok':
            old=prev_by_id.get(o['id'])
            if old:
                for field in ('source_status','source_checked_at','source_hash','verification'):
                    o[field]=old.get(field)
            else:
                o['verification']='retained – source unavailable'
            o['calculation_reference']=calc(o,fx);continue
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
        o['calculation_reference']=calc(o,fx)

    assign_provider_roles(offers)

    registry=json.loads(CEE_REGISTRY.read_text(encoding='utf-8')) if CEE_REGISTRY.exists() else {'providers':[]}
    europe_registry=json.loads(EUROPE_REGISTRY.read_text(encoding='utf-8')) if EUROPE_REGISTRY.exists() else {'providers':[]}
    watchlist=json.loads(EUROPE_WATCHLIST.read_text(encoding='utf-8')) if EUROPE_WATCHLIST.exists() else {'providers':[]}
    cee_audit=build_cee_audit(registry,offers)
    europe_audit=build_registry_audit(europe_registry,offers)
    output={'generated_at':now,'update_frequency':'weekly','default_transaction_eur':REFERENCE_TRANSACTION_EUR,'methodology_version':'1.4',
            'scope_note':'Publicly displayed merchant acceptance prices. Acquirers, PSPs, gateways and A2A wallets are separated by provider type; they are not automatically treated as economically identical.',
            'comparison_profile':{'transaction_amount':REFERENCE_TRANSACTION_EUR,'transaction_currency':'EUR','icpp_profile':'authenticated EEA consumer debit / domestic UK consumer debit','interchange_reference_pct':ICPP_EEA_DEBIT_INTERCHANGE_REFERENCE_PCT,'scheme_fee_reference_pct':ICPP_EEA_SCHEME_FEE_REFERENCE_PCT,'scheme_fee_source_url':ICPP_SCHEME_FEE_SOURCE_URL},
            'cee_acquirer_registry':{'as_of':registry.get('as_of'),'provider_count':len(registry.get('providers',[]))},
            'europe_acquirer_registry':{'as_of':europe_registry.get('as_of'),'provider_count':len(europe_registry.get('providers',[])),'country_count':len({item.get('country_iso2') for item in europe_registry.get('providers',[])})},
            'europe_acquirer_watchlist':{'as_of':watchlist.get('as_of'),'provider_count':len(watchlist.get('providers',[])),'country_count':len({item.get('country_iso2') for item in watchlist.get('providers',[])})},
            'cee_audit':cee_audit,
            'europe_audit':europe_audit,
            'fx':{'source':'Česká národní banka','source_url':CNB_URL,'date':fx_date,'rates':fx},'sources':baseline['sources'],'countries':baseline['countries'],'offers':offers,'change_log':changes[-250:]}
    DATA.mkdir(parents=True,exist_ok=True);HISTORY.mkdir(parents=True,exist_ok=True)
    (DATA/'latest.json').write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
    (DATA/'changes.json').write_text(json.dumps(changes[-250:],ensure_ascii=False,indent=2),encoding='utf-8')
    (DATA/'cee_audit.json').write_text(json.dumps(cee_audit,ensure_ascii=False,indent=2),encoding='utf-8')
    (DATA/'europe_audit.json').write_text(json.dumps(europe_audit,ensure_ascii=False,indent=2),encoding='utf-8')
    today=date.today().isoformat();(HISTORY/f'{today}.json').write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
    idx=[]
    for p in sorted(HISTORY.glob('*.json')):
        if p.name!='index.json':idx.append({'date':p.stem,'file':p.name})
    (HISTORY/'index.json').write_text(json.dumps(idx,indent=2),encoding='utf-8')
    write_csv(output)
    log.info('Wrote %d offers, CNB FX %s, %d history points',len(offers),fx_date,len(idx))
    return 0

if __name__=='__main__':sys.exit(main())
