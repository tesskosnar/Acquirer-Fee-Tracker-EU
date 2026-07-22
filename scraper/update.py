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
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("update")

PCT_FIXED_RE = re.compile(
    r"(?P<pmin>\d{1,2}(?:[.,]\d{1,3})?)\s*(?:[–—-]\s*(?P<pmax>\d{1,2}(?:[.,]\d{1,3})?))?\s*%"
    r"(?:\s*(?:\+|plus)\s*(?P<fmin>\d+(?:[.,]\d+)?)\s*(?P<currency>CZK|Kč|PLN|EUR|HUF|RON|DKK|SEK))?",
    re.I,
)


def decimal(s: str | None) -> float | None:
    return None if s is None else float(s.replace(",", "."))


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


def fetch_source(url: str, fmt: str) -> tuple[str, str]:
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
    else:
        soup=BeautifulSoup(r.text,'html.parser')
        for el in soup(['script','style','noscript']): el.decompose()
        text=' '.join(soup.stripped_strings)
    return re.sub(r'\s+',' ',text),content_hash


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
        w=csv.DictWriter(f,fieldnames=cols);w.writeheader()
        for o in output['offers']:
            c=o.get('calculation_500_czk',{})
            row={k:o.get(k) for k in cols};row.update({'fee_500_min_czk':c.get('fee_min_czk'),'fee_500_max_czk':c.get('fee_max_czk'),'effective_500_min_pct':c.get('effective_min_pct'),'effective_500_max_pct':c.get('effective_max_pct')})
            w.writerow(row)


def main()->int:
    baseline=json.loads(BASELINE.read_text(encoding='utf-8'))
    previous=load_previous(baseline)
    offers=deepcopy(previous.get('offers') or baseline['offers'])
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
            text,h=fetch_source(s['url'],s.get('format','html'))
            fetched[sid]={'text':text,'hash':h,'status':'ok','checked_at':now}
            log.info('Fetched %s (%d chars)',sid,len(text))
        except Exception as exc:
            fetched[sid]={'text':'','hash':None,'status':f'error: {exc}','checked_at':now}
            log.warning('Source %s failed: %s',sid,exc)

    for o in offers:
        sid=o['source_id']; src=fetched.get(sid,{})
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

    output={'generated_at':now,'update_frequency':'weekly','default_transaction_czk':500,'methodology_version':'1.0',
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
