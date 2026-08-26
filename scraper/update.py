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
PROVIDER_GAPS = Path(__file__).with_name("provider_gap_offers.json")
AUDIT_CORRECTIONS = Path(__file__).with_name("audit_corrections.json")
PROVIDER_MASTER_CROSSCHECK = Path(__file__).with_name("provider_master_crosscheck.json")
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
ADYEN_ICPP_REFERENCE_COUNTRIES = ADYEN_CARD_COUNTRIES
ADYEN_A2A_TYPES = {"Online banking", "Real-time payments", "Direct debit", "Bank transfer"}
REFERENCE_TRANSACTION_EUR = 20.0
REFERENCE_MONTHLY_ACCEPTANCE_TURNOVER_EUR = 5000.0
ICPP_EEA_DEBIT_INTERCHANGE_REFERENCE_PCT = 0.20
ICPP_SCHEME_FEE_SOURCE_URL = "https://www.paybyrd.com/pricing/scheme-fees"
ICPP_EEA_SCHEME_FEE_SCENARIOS = (
    {"scheme": "Visa Debit", "pct": 0.116, "fixed": 0.035, "currency": "EUR"},
    {"scheme": "Mastercard Debit", "pct": 0.121, "fixed": 0.045, "currency": "EUR"},
)
ICPP_UK_SCHEME_FEE_SCENARIOS = (
    {"scheme": "Visa Debit", "pct": 0.100, "fixed": 0.039, "currency": "GBP"},
    {"scheme": "Mastercard Debit", "pct": 0.136, "fixed": 0.034, "currency": "GBP"},
)
ICPP_CH_CNP_DEBIT_INTERCHANGE_REFERENCE_PCT = 0.28
ICPP_CH_SCHEME_FEE_REFERENCE_PCT = 0.138
ICPP_CH_SCHEME_FEE_REFERENCE_FIXED_CHF = 0.052
ICPP_CH_INTERCHANGE_SOURCE_URL = (
    "https://www.weko.admin.ch/dam/en/sd-web/0Wuh3w3ftMx0/"
    "debitkarten_interchange_fees_anregungen_des_sekretariats_fuer_cnp_transaktionen.pdf"
)

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
ADYEN_METHOD_SOURCE_URLS = {
    "online-banking-czech-republic": "https://www.adyen.com/en_SG/payment-methods/online-banking-czech-republic",
    "online-banking-slovakia": "https://www.adyen.com/en_SG/payment-methods/online-banking-slovakia",
    "sepa-direct-debit": "https://www.adyen.com/payment-methods/sepa-direct-debit",
    "blik": "https://www.adyen.com/en_GB/payment-methods/blik",
    "online-banking-poland": "https://www.adyen.com/pl_PL/metody-platnosci/online-banking-poland",
    "trustly": "https://www.adyen.com/en_SG/payment-methods/trustly",
}
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
VERIFICATION_DATE_RE = re.compile(r"\b(?P<day>[0-3]?\d)\.\s*(?P<month>[01]?\d)\.\s*(?P<year>20\d{2})\b")
EFFECTIVE_DATE_PREFIX_RE = re.compile(r"(?:účinn\w*|platn\w*)\s+od\s*$", re.I)
PRICE_VERIFICATION_FIELDS = (
    'source_url', 'product', 'pricing_model', 'variable_pct_min', 'variable_pct_max',
    'fixed_fee_min', 'fixed_fee_max', 'minimum_fee', 'monthly_fee', 'package_effective_pct',
    'condition', 'pricing_components', 'all_in_complete', 'comparison_estimate',
)


def decimal(s: str | None) -> float | None:
    return None if s is None else float(s.replace(",", "."))


def provider_key(value: str | None) -> str:
    plain=unicodedata.normalize('NFKD',value or '').encode('ascii','ignore').decode('ascii')
    return re.sub(r'[^a-z0-9]+','',plain.lower())


def canonical_provider_key(value: str | None) -> str:
    """Collapse country-branch labels to a group name for registry audits only."""
    key=provider_key(value)
    brand_prefixes=(
        ('netsnexi','netsnexi'),('globalpayments','globalpayments'),
        ('societegenerale','societegenerale'),('unicreditbank','unicreditbank'),
        ('worldline','worldline'),('worldpay','worldpay'),('barclaycard','barclaycard'),
        ('checkoutcom','checkoutcom'),('elavon','elavon'),('nexi','nexi'),
        ('flatpay','flatpay'),('fiserv','fiserv'),('payone','payone'),('dojo','dojo'),
        ('getnet','getnet'),('teya','teya'),('europeanmerchantservices','europeanmerchantservices'),
        ('swedbank','swedbank'),('raiffeisen','raiffeisen'),('viva','viva'),
        ('payu','payu'),('unzer','unzer'),('shift4','shift4'),('planet','planet'),
        ('square','square'),('paypal','paypal'),('sibs','sibs'),
    )
    for prefix,canonical in brand_prefixes:
        if key.startswith(prefix):
            return canonical
    if 'bancasella' in key:
        return 'bancasella'
    return key


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


def is_source_reviewed_offer(offer: dict) -> bool:
    """Mirror the dashboard rule that decides whether a row is publishable."""
    state=offer.get('verification_state')
    if state:
        return state in {'verified_automated','verified_manual'}
    verification=(offer.get('verification') or '').lower()
    excluded=('částečně','namátkově','rozšířeného datasetu','obnoveno z původního')
    if any(token in verification for token in excluded):
        return False
    accepted=('ručně ověř','ověřen','last verified adyen','adyen country/method')
    return verification.startswith('auto-checked') or any(token in verification for token in accepted)


def verification_state(offer: dict) -> str:
    """Convert legacy prose into a stable, explicit review state once.

    The UI and exports consume this field and never have to guess from Czech
    sentences.  The text remains only as human-readable evidence.
    """
    verification=(offer.get('verification') or '').lower()
    if 'review suggested' in verification or verification.startswith('retained – review'):
        return 'review_needed'
    if is_source_reviewed_offer({**offer,'verification_state':None}):
        if not offer.get('price_verified_on'):
            return 'reviewed_undated'
        return 'verified_automated' if verification.startswith('auto-checked') else 'verified_manual'
    return 'legacy_unverified'


def pricing_model_code(offer: dict) -> str:
    """Normalise free-form pricing labels into a controlled vocabulary."""
    model=(offer.get('pricing_model') or '').lower()
    if offer.get('variable_pct_min') is None:
        return 'individual' if 'individual' in model else 'not_public'
    if 'interchange' in model or 'ic++' in model or 'icpp' in model:
        return 'icpp'
    if 'subscription' in model or 'monthly' in model:
        return 'subscription'
    if offer.get('promo') or 'promo' in model:
        return 'promo'
    if 'package' in model or offer.get('package_effective_pct') is not None:
        return 'package'
    if 'up to' in model or 'maximum' in model:
        return 'up_to'
    if 'from' in model or model.startswith('od '):
        return 'from'
    if 'free' in model:
        return 'free'
    if any(token in model for token in ('blended','fixed','flat','standard','published')):
        return 'blended'
    return 'other'


def card_scheme_fields(offer: dict) -> tuple[list[str], str]:
    """Return explicit networks and a conservative cardholder profile."""
    if offer.get('method')!='card':
        return [],'not_applicable'
    schemes=list(dict.fromkeys(offer.get('card_schemes') or []))
    if not schemes:
        if (offer.get('card_scheme') or '').startswith('domestic'):
            schemes=['national']
        else:
            schemes=['visa','mastercard']
    evidence=' '.join(str(offer.get(field) or '').lower() for field in ('product','condition'))
    if offer.get('card_profile'):
        profile=offer['card_profile']
    elif any(token in evidence for token in ('commercial card','business card','corporate card','firemní kart')):
        profile='commercial'
    elif any(token in evidence for token in ('consumer card','spotřebitelsk','debit spotřeb')):
        profile='consumer'
    else:
        profile='unspecified'
    return schemes,profile


def normalise_offer_schema(offers: list[dict]) -> list[dict]:
    """Make comparison-critical metadata explicit for every published row."""
    one_off_fees={
        'NL-worldlinenetherlands-card-registry':{'kind':'terminal_purchase','amount':199,'currency':'EUR','interval':'one_off'},
        'NL-worldline-domestic-debit-card':{'kind':'terminal_purchase','amount':199,'currency':'EUR','interval':'one_off'},
    }
    rental_ids={'NO-worldline-one-card-registry','SE-worldline-one-card-registry','DK-worldline-one-card'}
    for offer in offers:
        numeric=offer.get('variable_pct_min') is not None
        offer.setdefault('variable_pct_basis','provider_published' if numeric else 'not_applicable')
        if offer.get('all_in_complete') is None:
            components=offer.get('pricing_components') or {}
            missing_icpp=(components.get('model')=='interchange++' and not offer.get('comparison_estimate'))
            offer['all_in_complete']=bool(numeric and not missing_icpp)
        offer['pricing_model_code']=pricing_model_code(offer)
        # A promotional pricing model is itself structured evidence that the
        # offer must not enter standard headline comparisons.
        if offer['pricing_model_code']=='promo':
            offer['promo']=True
        schemes,profile=card_scheme_fields(offer)
        offer['card_schemes']=schemes
        offer['card_profile']=profile
        offer.setdefault('tax_treatment','included_or_not_applicable')
        if offer.get('package_effective_pct') is not None:
            offer['monthly_fee_mode']='package_effective'
        elif 'minimální měsíční poplatek' in (offer.get('condition') or '').lower():
            offer['monthly_fee_mode']='minimum_commitment'
        else:
            offer.setdefault('monthly_fee_mode','additional')
        if offer.get('id') in rental_ids:
            offer['monthly_fee_kind']='terminal_rental'
        if offer.get('id') in one_off_fees:
            offer.setdefault('additional_fees',[]).append(one_off_fees[offer['id']])
        fees=[]
        if (offer.get('monthly_fee') or 0)>0:
            fees.append({
                'kind':offer.get('monthly_fee_kind','monthly_service'),
                'amount':offer['monthly_fee'],
                'currency':offer.get('monthly_currency') or offer.get('fee_currency'),
                'interval':'monthly',
                'mode':offer['monthly_fee_mode'],
            })
        if offer.get('setup_fee') is not None:
            fees.append({
                'kind':offer.get('setup_fee_kind','activation'),
                'amount':offer['setup_fee'],
                'currency':offer.get('setup_currency') or offer.get('fee_currency'),
                'interval':'one_off',
            })
        fees.extend(deepcopy(offer.get('additional_fees') or []))
        offer['non_transaction_fees']=fees
        offer['monitoring_level']='price_parser' if offer.get('parser',{}).get('auto_parse') else ('source_monitor' if offer.get('source_id') else 'manual')
        offer.setdefault('source_last_attempt_at',None)
        offer.setdefault('source_last_attempt_status','not_attempted')
        if (
            not offer.get('source_checked_at')
            and offer.get('source_last_attempt_status')=='ok'
            and offer.get('source_last_attempt_at')
        ):
            offer['source_checked_at']=offer['source_last_attempt_at']
        offer['verification_state']=verification_state(offer)
        offer['verification_scope']='price' if numeric else 'service_or_role'
    return offers


def _iso_date(value: str | None) -> str | None:
    """Return a valid ISO calendar date without inventing a time of day."""
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        return None


def infer_price_verified_on(offer: dict) -> str | None:
    """Extract a real price-review date only when the evidence supports it.

    Manual notes often contain both a tariff effective date and the later date
    on which the price was checked. Effective dates are deliberately ignored.
    A precise manual source-fetch timestamp is a safe fallback for reviewed
    legacy rows whose prose records only a month.
    """
    explicit=_iso_date(offer.get('price_verified_on'))
    if explicit:
        return explicit
    if not is_source_reviewed_offer(offer):
        return None
    verification=offer.get('verification') or ''
    for match in reversed(list(VERIFICATION_DATE_RE.finditer(verification))):
        prefix=verification[max(0,match.start()-40):match.start()]
        if EFFECTIVE_DATE_PREFIX_RE.search(prefix):
            continue
        try:
            return date(int(match.group('year')),int(match.group('month')),int(match.group('day'))).isoformat()
        except ValueError:
            continue
    checked_at=offer.get('source_checked_at')
    if offer.get('source_status') in {'manual','ok'} and checked_at:
        try:
            return datetime.fromisoformat(checked_at).date().isoformat()
        except (TypeError, ValueError):
            pass
    return None


def same_price_verification_basis(current: dict, previous: dict) -> bool:
    """Whether an earlier price-review date still describes the same row."""
    return all(current.get(field)==previous.get(field) for field in PRICE_VERIFICATION_FIELDS)


def initialise_temporal_metadata(offer: dict, previous: dict | None = None) -> dict:
    """Keep build time, source access and semantic price review independent."""
    offer.setdefault('source_checked_at',None)
    if (
        not offer.get('source_checked_at')
        and offer.get('source_last_attempt_status')=='ok'
        and offer.get('source_last_attempt_at')
    ):
        # A successful source-monitor attempt is a real successful fetch, not
        # the build timestamp. Preserve it as the last successful access.
        offer['source_checked_at']=offer['source_last_attempt_at']
    verified_on=infer_price_verified_on(offer)
    if not verified_on and previous and same_price_verification_basis(offer,previous):
        verified_on=_iso_date(previous.get('price_verified_on'))
    offer['price_verified_on']=verified_on
    return offer


def validate_temporal_metadata(offers: list[dict], generated_at: str) -> None:
    """Reject malformed or future-dated temporal metadata before publication."""
    generated=datetime.fromisoformat(generated_at)
    if generated.tzinfo is None:
        raise ValueError('generated_at must be timezone-aware')
    for offer in offers:
        verified_on=offer.get('price_verified_on')
        if verified_on:
            try:
                verified=date.fromisoformat(verified_on)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid price_verified_on for {offer.get('id')}: {verified_on}") from exc
            if verified>generated.date():
                raise ValueError(f"Future price_verified_on for {offer.get('id')}: {verified_on}")
        checked_at=offer.get('source_checked_at')
        if checked_at:
            try:
                checked=datetime.fromisoformat(checked_at)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid source_checked_at for {offer.get('id')}: {checked_at}") from exc
            if checked.tzinfo is None:
                raise ValueError(f"source_checked_at must be timezone-aware for {offer.get('id')}")
            if checked>generated:
                raise ValueError(f"Future source_checked_at for {offer.get('id')}: {checked_at}")
            if offer.get('source_status') in {'manual','seeded'} and checked==generated and not offer.get('source_id'):
                raise ValueError(f"Build timestamp copied into source_checked_at for {offer.get('id')}")


def validate_offer_schema(offers: list[dict]) -> None:
    allowed_states={'verified_automated','verified_manual','reviewed_undated','legacy_unverified','review_needed'}
    allowed_models={'blended','icpp','from','up_to','subscription','promo','individual','not_public','free','package','other'}
    allowed_variable_bases={'provider_published','not_applicable'}
    for offer in offers:
        oid=offer.get('id')
        if not isinstance(offer.get('all_in_complete'),bool):
            raise ValueError(f'all_in_complete must be explicit for {oid}')
        if offer.get('verification_state') not in allowed_states:
            raise ValueError(f'Invalid verification_state for {oid}')
        if offer.get('pricing_model_code') not in allowed_models:
            raise ValueError(f'Invalid pricing_model_code for {oid}')
        if offer.get('variable_pct_basis') not in allowed_variable_bases:
            raise ValueError(f'Invalid variable_pct_basis for {oid}')
        if offer.get('method')=='card' and not offer.get('card_schemes'):
            raise ValueError(f'card_schemes missing for {oid}')
        if not isinstance(offer.get('non_transaction_fees'),list):
            raise ValueError(f'non_transaction_fees missing for {oid}')


def referenced_source_configs(offers:list[dict], sources:dict)->dict:
    """Return only source configs that are attached to a current offer."""
    referenced={offer.get('source_id') for offer in offers if offer.get('source_id')}
    missing=sorted(referenced-set(sources))
    if missing:
        raise ValueError(f"Offer references missing source configs: {', '.join(missing)}")
    return {sid:sources[sid] for sid in sorted(referenced)}


def append_dataset_changes(changes: list[dict], previous: list[dict], current: list[dict], now: str) -> list[dict]:
    """Record manual overlays as well as parser changes in the same audit log."""
    fields=(
        'provider','product','method','channel','pricing_model_code','variable_pct_min','variable_pct_max',
        'fixed_fee_min','fixed_fee_max','minimum_fee','monthly_fee','monthly_currency','monthly_fee_mode',
        'setup_fee','fee_currency','all_in_complete','comparison_estimate','condition','source_url',
        'card_schemes','card_profile','tax_treatment','variable_pct_basis',
    )
    before={row['id']:row for row in previous}
    after={row['id']:row for row in current}
    existing={(item.get('offer_id'),item.get('field'),json.dumps(item.get('new'),sort_keys=True,ensure_ascii=False)) for item in changes}
    schema_defaults={'pricing_model_code','monthly_fee_mode','all_in_complete','card_schemes','card_profile','tax_treatment','variable_pct_basis'}
    for oid in sorted(after.keys()-before.keys()):
        row=after[oid]
        signature=(oid,'offer',json.dumps('added'))
        if signature not in existing:
            changes.append({'detected_at':now,'offer_id':oid,'provider':row.get('provider'),'country_iso2':row.get('country_iso2'),'method':row.get('method'),'field':'offer','old':None,'new':'added','source_url':row.get('source_url'),'confidence':1.0,'change_origin':'dataset'})
    for oid in sorted(before.keys()-after.keys()):
        row=before[oid]
        signature=(oid,'offer',json.dumps('removed'))
        if signature not in existing:
            changes.append({'detected_at':now,'offer_id':oid,'provider':row.get('provider'),'country_iso2':row.get('country_iso2'),'method':row.get('method'),'field':'offer','old':'present','new':'removed','source_url':row.get('source_url'),'confidence':1.0,'change_origin':'dataset'})
    for oid in sorted(before.keys() & after.keys()):
        old,new=before[oid],after[oid]
        for field in fields:
            if field not in old and field in schema_defaults:
                continue
            if old.get(field)==new.get(field):
                continue
            signature=(oid,field,json.dumps(new.get(field),sort_keys=True,ensure_ascii=False))
            if signature in existing:
                continue
            changes.append({'detected_at':now,'offer_id':oid,'provider':new.get('provider'),'country_iso2':new.get('country_iso2'),'method':new.get('method'),'field':field,'old':old.get(field),'new':new.get(field),'source_url':new.get('source_url'),'confidence':1.0,'change_origin':'dataset'})
            existing.add(signature)
    return changes[-500:]


def ensure_current_overlay_additions(changes: list[dict], offers: list[dict], now: str) -> list[dict]:
    """Seed the audit for a newly introduced reviewed overlay exactly once."""
    if not PROVIDER_GAPS.exists():
        return changes
    overlay=json.loads(PROVIDER_GAPS.read_text(encoding='utf-8'))
    if overlay.get('as_of')!=now[:10]:
        return changes
    existing={(item.get('offer_id'),item.get('field'),json.dumps(item.get('new'),sort_keys=True,ensure_ascii=False)) for item in changes}
    by_id={offer['id']:offer for offer in offers}
    for raw in overlay.get('offers',[]):
        signature=(raw['id'],'offer',json.dumps('added'))
        if signature in existing:
            continue
        offer=by_id[raw['id']]
        changes.append({'detected_at':now,'offer_id':offer['id'],'provider':offer.get('provider'),'country_iso2':offer.get('country_iso2'),'method':offer.get('method'),'field':'offer','old':None,'new':'added','source_url':offer.get('source_url'),'confidence':1.0,'change_origin':'reviewed_overlay'})
    return changes[-500:]


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


def normalise_provider_types(offers: list[dict]) -> list[dict]:
    """Keep provider_type as a display label for the structured provider_role.

    Source provenance and pricing visibility belong to their own fields and
    must not leak into the organisation type used by exports and the UI.
    """
    labels={
        'acquirer':'Acquirer',
        'acquirer_sales_channel':'Kanál acquirera',
        'psp':'PSP',
        'gateway_processor':'Brána / procesor',
        'a2a_provider':'A2A poskytovatel',
        'other':'Ostatní',
    }
    for offer in offers:
        offer['provider_type']=labels[offer.get('provider_role','other')]
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
        dataset_keys={canonical_provider_key(item.get('provider')) for item in dataset}
        matched=[]; missing=[]
        for item in discovered:
            dataset_name=aliases.get((code,item['provider']),item['provider'])
            (matched if canonical_provider_key(dataset_name) in dataset_keys else missing).append(item['provider'])
        acquirers=[item for item in discovered if item['role'] in {'acquiring_bank','direct_acquirer'}]
        matched_acquirers=[]
        for item in acquirers:
            dataset_name=aliases.get((code,item['provider']),item['provider'])
            if canonical_provider_key(dataset_name) in dataset_keys:
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


def adyen_pricing_components(processing: dict, method_fee: dict | None, card: bool = False,
                             country_code: str | None = None) -> dict:
    components = {
        "processing_fee": {"amount": processing["amount"], "currency": processing["currency"]},
    }
    if card:
        if country_code == "CH":
            interchange_pct = ICPP_CH_CNP_DEBIT_INTERCHANGE_REFERENCE_PCT
            scheme_pct = ICPP_CH_SCHEME_FEE_REFERENCE_PCT
            profile = "Swiss domestic consumer debit card, e-commerce; conservative Mastercard reference"
            scheme_scenarios = None
        elif country_code == "GB":
            interchange_pct = ICPP_EEA_DEBIT_INTERCHANGE_REFERENCE_PCT
            scheme_pct = None
            profile = "UK domestic consumer debit card, authenticated Visa/Mastercard"
            scheme_scenarios = ICPP_UK_SCHEME_FEE_SCENARIOS
        else:
            interchange_pct = ICPP_EEA_DEBIT_INTERCHANGE_REFERENCE_PCT
            scheme_pct = None
            profile = "EEA consumer debit card, authenticated Visa/Mastercard"
            scheme_scenarios = ICPP_EEA_SCHEME_FEE_SCENARIOS
        components.update({
            "adyen_markup_pct": 0.6,
            "interchange": {
                "model": "pass-through",
                "eea_consumer_debit_reference_pct": ICPP_EEA_DEBIT_INTERCHANGE_REFERENCE_PCT,
                "eea_consumer_credit_reference_pct": 0.3,
            },
            "scheme_fees": "pass-through; variable",
            "comparison_reference": {
                "profile": profile,
                "interchange_pct": interchange_pct,
                "scheme_fee_source_url": ICPP_SCHEME_FEE_SOURCE_URL,
            },
        })
        if scheme_scenarios:
            components["comparison_reference"].update({
                "scheme_fee_scenarios": [dict(item) for item in scheme_scenarios],
                "total_addon_pct_min": round(interchange_pct + min(item["pct"] for item in scheme_scenarios), 4),
                "total_addon_pct_max": round(interchange_pct + max(item["pct"] for item in scheme_scenarios), 4),
                "reference_transaction_amount": REFERENCE_TRANSACTION_EUR,
                "reference_transaction_currency": "EUR",
            })
        else:
            components["comparison_reference"].update({
                "scheme_fee_pct": scheme_pct,
                "total_addon_pct": round(interchange_pct + scheme_pct, 4),
            })
        if country_code == "CH":
            components["interchange"]["ch_domestic_cnp_debit_reference_pct"] = interchange_pct
            components["comparison_reference"].update({
                "fixed_addon": {
                    "amount": ICPP_CH_SCHEME_FEE_REFERENCE_FIXED_CHF,
                    "currency": "CHF",
                },
                "interchange_source_url": ICPP_CH_INTERCHANGE_SOURCE_URL,
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
        "source_url": ADYEN_METHOD_SOURCE_URLS.get(slug, "https://www.adyen.com/pricing"),
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
        "source_checked_at": checked_at if live else None,
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
        components = adyen_pricing_components(processing, None, card=True, country_code=code)
        has_reference = code in ADYEN_ICPP_REFERENCE_COUNTRIES
        if code == "GB":
            components["comparison_reference"]["profile"] = "UK domestic consumer debit card, authenticated Visa/Mastercard"
        product = (
            "UK domácí spotřebitelská debetní karta (IC++ srovnávací odhad)"
            if code == "GB" else
            "CH domácí spotřebitelská debetní karta (IC++ srovnávací odhad)"
            if code == "CH" else
            "EEA spotřebitelská debetní karta (IC++ srovnávací odhad)"
        )
        offer.update({
            "provider_type": "Acquirer",
            "product": product,
            "pricing_model": "IC++",
            "variable_pct_min": 0.6,
            "variable_pct_max": 0.6,
            "fixed_fee_min": processing["amount"],
            "fixed_fee_max": processing["amount"],
            "fee_currency": processing["currency"],
            "monthly_currency": processing["currency"],
            "pricing_components": components,
            "all_in_complete": False,
            "comparison_estimate": has_reference,
            "variable_pct_basis": "provider_published",
            "notes": (
                "Adyen processing fee + 0.60% acquiring markup. For Switzerland the dashboard adds a conservative domestic e-commerce debit reference: 0.28% interchange plus Mastercard scheme fees of 0.138% + CHF 0.052. This is a modelled comparison value, not a guaranteed quote."
                if code == "CH" else
                "Adyen processing fee + 0.60% acquiring markup. The dashboard comparison uses a domestic UK consumer-debit reference of 0.20% interchange and separate Visa Debit / Mastercard Debit percentage-plus-fixed scheme-fee scenarios. This is a modelled comparison value, not a guaranteed transaction quote."
                if code == "GB" else
                "Adyen processing fee + 0.60% acquiring markup. The dashboard comparison uses an authenticated EEA consumer-debit reference of 0.20% interchange and separate Visa Debit / Mastercard Debit percentage-plus-fixed scheme-fee scenarios. This is a modelled comparison value, not a guaranteed transaction quote."
            ),
            "verification": "auto-checked by country (Adyen pricing + payment-method API)" if catalog else "retained from last verified Adyen country audit",
            "source_status": "ok" if catalog else "seeded",
            "source_checked_at": checked_at if catalog else None,
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
            "condition": (
                "Publikovaná cena je 0,60 % + zpracovatelský poplatek. Srovnávací sazba samostatně přičítá odhad 0,28 % interchange a 0,138 % scheme fee + 0,052 CHF."
                if code == "CH" else
                "Publikovaná cena je 0,60 % + zpracovatelský poplatek. Srovnávací sazba používá 0,20 % interchange a domácí UK Visa/Mastercard debit benchmark včetně procentní a pevné scheme složky."
                if code == "GB" else
                "Publikovaná cena je 0,60 % + zpracovatelský poplatek. Srovnávací sazba používá 0,20 % interchange a EEA Visa/Mastercard debit benchmark včetně procentní a pevné scheme složky."
            ),
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
    addon_min=comparison.get('total_addon_pct_min',comparison.get('total_addon_pct',0)) or 0
    addon_max=comparison.get('total_addon_pct_max',comparison.get('total_addon_pct',0)) or 0
    fixed_addon=comparison.get('fixed_addon') or {}
    fixed_addon_rate=fx.get(fixed_addon.get('currency'),{'czk_per_unit':1})['czk_per_unit']
    fixed_addon_czk=(fixed_addon.get('amount') or 0)*fixed_addon_rate
    package_effective_pct=offer.get('package_effective_pct')
    monthly_fee=offer.get('monthly_fee') or 0
    monthly_currency=offer.get('monthly_currency') or offer.get('fee_currency')
    monthly_rate=fx.get(monthly_currency,{'czk_per_unit':1})['czk_per_unit']
    monthly_turnover_czk=REFERENCE_MONTHLY_ACCEPTANCE_TURNOVER_EUR*fx.get('EUR',{'czk_per_unit':1})['czk_per_unit']
    monthly_pct=(monthly_fee*monthly_rate/monthly_turnover_czk*100) if monthly_fee and monthly_turnover_czk else 0

    def apply_monthly_fee(mn:float,mx:float)->tuple[float,float]:
        if not monthly_pct or package_effective_pct is not None:
            return mn,mx
        if offer.get('monthly_fee_mode')=='minimum_commitment':
            floor=amount*monthly_pct/100
            return max(mn,floor),max(mx,floor)
        addon=amount*monthly_pct/100
        return mn+addon,mx+addon
    if package_effective_pct is not None:
        variable_min=package_effective_pct
        variable_max=package_effective_pct
    else:
        variable_min=offer['variable_pct_min']+addon_min
        variable_max=offer['variable_pct_max']+addon_max
    scheme_scenarios=comparison.get('scheme_fee_scenarios') or []
    if scheme_scenarios and package_effective_pct is None:
        scenario_min=[]
        scenario_max=[]
        interchange_pct=comparison.get('interchange_pct',0) or 0
        for scenario in scheme_scenarios:
            scenario_rate=fx.get(scenario.get('currency'),{'czk_per_unit':1})['czk_per_unit']
            scenario_fixed=(scenario.get('fixed') or 0)*scenario_rate
            scenario_pct=interchange_pct+(scenario.get('pct') or 0)
            scenario_min.append(amount*(offer['variable_pct_min']+scenario_pct)/100+(offer.get('fixed_fee_min') or 0)*rate+scenario_fixed)
            scenario_max.append(amount*(offer['variable_pct_max']+scenario_pct)/100+(offer.get('fixed_fee_max') or 0)*rate+scenario_fixed)
        minimum=(offer.get('minimum_fee') or 0)*rate
        mn=max(min(scenario_min),minimum)
        mx=max(max(scenario_max),minimum)
        mn,mx=apply_monthly_fee(mn,mx)
        return {'fee_min_czk':round(mn,4),'fee_max_czk':round(mx,4),'effective_min_pct':round(mn/amount*100,4),'effective_max_pct':round(mx/amount*100,4)}
    mn=amount*variable_min/100+(offer.get('fixed_fee_min') or 0)*rate+fixed_addon_czk
    mx=amount*variable_max/100+(offer.get('fixed_fee_max') or 0)*rate+fixed_addon_czk
    minimum=(offer.get('minimum_fee') or 0)*rate
    mn=max(mn,minimum)
    mx=max(mx,minimum)
    mn,mx=apply_monthly_fee(mn,mx)
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
    verified_on=provider.get('verified_on','18. 8. 2026')
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
        'source_url':provider.get('pricing_url',provider['official_url']),
        'condition':provider.get('pricing_evidence',''),
        'price_lead_review':provider.get('price_lead_review'),
        'verification':provider.get('pricing_verification',f'ručně ověřena role a lokální nabídka na oficiálním zdroji {verified_on}; poskytovatel na něm neuvádí kompletní merchant sazbu'),
        'notes':'Role a lokální dostupnost jsou ověřené. Záznam zůstává bez čísla, protože oficiální zdroj kompletní merchant sazbu veřejně neuvádí; nejde o uzavřený nebo vyřazený výsledek.',
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


def apply_provider_gap_overlay(offers:list[dict], countries:dict)->list[dict]:
    """Add explicitly reviewed providers without duplicating country aliases."""
    if not PROVIDER_GAPS.exists():
        return offers
    overlay=json.loads(PROVIDER_GAPS.read_text(encoding='utf-8'))
    rows=[_normalise_verified_offer(raw,countries) for raw in overlay.get('offers',[])]
    ids={row['id'] for row in rows}
    merged=[offer for offer in offers if offer['id'] not in ids]
    merged.extend(rows)
    merged_ids=[offer['id'] for offer in merged]
    if len(merged_ids)!=len(set(merged_ids)):
        raise ValueError('Duplicate offer id after provider gap overlay')
    return merged


def apply_audit_corrections(offers:list[dict], *, generated_only:bool=False)->list[dict]:
    """Apply source-reviewed corrections by immutable offer id.

    Keeping these findings in a small overlay makes a later source rebuild
    unable to silently resurrect a superseded price or URL. Every correction
    must target exactly one existing row; typos therefore fail the build.
    """
    if not AUDIT_CORRECTIONS.exists():
        return offers
    payload=json.loads(AUDIT_CORRECTIONS.read_text(encoding='utf-8'))
    updates=[] if generated_only else list(payload.get('updates',[]))
    group_key='generated_group_updates' if generated_only else 'group_updates'
    for group in payload.get(group_key,[]):
        if not isinstance(group.get('set'),dict) or not group['set']:
            raise ValueError('Audit correction group has no fields')
        updates.extend({'id':oid,'set':group['set']} for oid in group.get('ids',[]))
    update_ids=[item.get('id') for item in updates]
    if len(update_ids)!=len(set(update_ids)):
        raise ValueError('Duplicate offer id in audit corrections')
    by_id={offer['id']:offer for offer in offers}
    missing=sorted(set(update_ids)-set(by_id))
    if missing:
        raise ValueError(f"Audit correction targets missing offer ids: {', '.join(missing)}")
    for item in updates:
        fields=item.get('set',{})
        if not isinstance(fields,dict) or not fields:
            raise ValueError(f"Audit correction {item.get('id')} has no fields")
        by_id[item['id']].update(deepcopy(fields))
    return offers


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


def expand_explicit_alternative_scheme_rates(offers:list[dict])->list[dict]:
    """Publish a separate row when an official tariff gives Amex a different price."""
    expanded=list(offers)
    existing={offer['id'] for offer in offers}
    for offer in offers:
        if offer.get('method')!='card':
            continue
        clone=None
        if offer.get('provider')=='Revolut' and offer.get('country_iso2') in ADYEN_CEE_COUNTRIES:
            clone=deepcopy(offer)
            clone.update({
                'id':offer['id']+'-amex','product':'Online American Express',
                'variable_pct_min':1.7,'variable_pct_max':1.7,
                'card_scheme':'amex','card_schemes':['amex'],'card_profile':'all',
                'condition':'American Express; stejný lokální fixní poplatek jako v oficiálním country ceníku.',
            })
        elif offer.get('id')=='BG-mypos-local-merchant-acquiring-acceptance-card':
            clone=deepcopy(offer)
            clone.update({
                'id':offer['id']+'-amex','product':'Online American Express',
                'variable_pct_min':2.5,'variable_pct_max':2.5,
                'fixed_fee_min':0.1,'fixed_fee_max':0.1,'fee_currency':'EUR',
                'card_scheme':'amex','card_schemes':['amex'],'card_profile':'all',
                'condition':'American Express podle oficiálního bulharského ceníku myPOS.',
            })
        if clone and clone['id'] not in existing:
            expanded.append(clone); existing.add(clone['id'])
    return expanded


def normalize_clearhaus_minimum_fees(offers:list[dict])->list[dict]:
    """Treat Clearhaus' ``min.`` amount as a floor, never a fixed add-on.

    The legacy seed stored e.g. ``1.45% / min. EUR 0.20`` as
    ``1.45% + EUR 0.20``.  That materially overstates the 20 EUR comparison.
    Local verified overlays can provide an exact local-currency minimum; the
    older rows retain their published EUR floor but no longer add it twice.
    """
    for offer in offers:
        if offer.get('provider')!='Clearhaus' or offer.get('method')!='card':
            continue
        if offer.get('minimum_fee') is None and offer.get('fixed_fee_min') in (0.2,0.4):
            offer['minimum_fee']=offer['fixed_fee_min']
        if offer.get('fixed_fee_min') in (0.2,0.4):
            offer['fixed_fee_min']=0
        if offer.get('fixed_fee_max') in (0.2,0.4):
            offer['fixed_fee_max']=0
        offer['provider_type']='Acquirer'
    return offers


def normalize_provider_names(offers:list[dict])->list[dict]:
    """Use one display name when a provider only appends a country branch."""
    aliases={
        'CCV Belgium':'CCV',
        'CCV Netherlands':'CCV',
        'Citadele Latvia / Klix':'Citadele / Klix',
        'Citadele Lithuania / Klix':'Citadele / Klix',
        'Dojo Ireland':'Dojo',
        'Dojo Italy':'Dojo',
        'Dojo Spain':'Dojo',
        'Elavon Germany':'Elavon',
        'Elavon Ireland':'Elavon',
        'Elavon Norway':'Elavon',
        'Elavon Poland':'Elavon',
        'Elavon UK':'Elavon',
        'Fiserv Austria':'Fiserv',
        'Fiserv UK':'Fiserv',
        'Flatpay Denmark':'Flatpay',
        'Flatpay Italy':'Flatpay',
        'Flatpay Netherlands':'Flatpay',
        'Getnet Portugal':'Getnet',
        'Global Payments Austria':'Global Payments',
        'Global Payments Croatia':'Global Payments',
        'Global Payments Hungary':'Global Payments',
        'Global Payments Romania':'Global Payments',
        'Global Payments Slovakia':'Global Payments',
        'Global Payments UK':'Global Payments',
        'Intesa Sanpaolo Bank Slovenia':'Intesa Sanpaolo Bank',
        'Nets / Nexi Denmark':'Nets / Nexi',
        'Nets / Nexi Finland':'Nets / Nexi',
        'Nets / Nexi Norway':'Nets / Nexi',
        'Nets / Nexi Sweden':'Nets / Nexi',
        'Nexi Croatia':'Nexi',
        'Nexi Czech Republic':'Nexi',
        'Nexi Germany / Concardis':'Nexi / Concardis',
        'Nexi Greece':'Nexi',
        'Nexi Hungary':'Nexi',
        'Nexi Italy':'Nexi',
        'Nexi Switzerland':'Nexi',
        'OTP banka Croatia':'OTP banka',
        'OTP banka Slovenia':'OTP banka',
        'PAYONE Austria':'PAYONE',
        'PayU Czech':'PayU',
        'PayU Poland':'PayU',
        'PayU Romania':'PayU',
        'Raiffeisen Hungary':'Raiffeisen',
        'Raiffeisen Romania':'Raiffeisen',
        'SEB Estonia':'SEB',
        'SEB Latvia':'SEB',
        'SEB Lithuania':'SEB',
        'Swedbank Estonia':'Swedbank',
        'Swedbank Latvia':'Swedbank',
        'Swedbank Lithuania':'Swedbank',
        'Teya Hungary':'Teya',
        'Teya Portugal':'Teya',
        'Trust Payments Malta':'Trust Payments',
        'Trust Payments UK':'Trust Payments',
        'UniCredit Bank Czech Republic and Slovakia':'UniCredit Bank',
        'UniCredit Bank Hungary':'UniCredit Bank',
        'UniCredit Bank Romania':'UniCredit Bank',
        'UniCredit Bank Slovakia':'UniCredit Bank',
        'UniCredit Bank Slovenia':'UniCredit Bank',
        'Unzer Austria':'Unzer',
        'Viva.com Cyprus':'Viva.com',
        'Viva.com Greece':'Viva.com',
        'Worldline / Axepta Italy':'Worldline / Axepta',
        'Worldline Belgium':'Worldline',
        'Worldline Croatia':'Worldline',
        'Worldline Finland':'Worldline',
        'Worldline France':'Worldline',
        'Worldline Greece':'Worldline',
        'Worldline Hungary':'Worldline',
        'Worldline Luxembourg':'Worldline',
        'Worldline Netherlands':'Worldline',
        'Worldline Norway / Bambora':'Worldline / Bambora',
        'Worldline Poland':'Worldline',
        'Worldline Slovakia':'Worldline',
        'Worldline Slovenia':'Worldline',
        'Worldline Sweden / Bambora':'Worldline / Bambora',
        'Worldline Switzerland':'Worldline',
    }
    for offer in offers:
        offer['provider']=aliases.get(offer.get('provider'),offer.get('provider'))
    return offers


def normalize_unpriced_pricing_models(offers:list[dict])->list[dict]:
    """Distinguish an explicit quote-only price from a price we did not find.

    Both cases remain excluded from numerical comparisons, but calling every
    unpriced row "individual" overstates what the source actually says.
    """
    quote_markers=(
        'individuální', 'individuálně', 'individualni', 'na poptávku', 'podle obchodníka',
        'podle potřeb', 'vyžaduje nabídku', 'cenovou nabídku', 'smluvní cenový model',
        'sjednáv', 'dohodnut', 'negotiable', 'upon agreement', 'personal offer', 'as agreed',
        'tailored pricing', 'rates independently', 'order form', 'merchant agreement',
    )
    for offer in offers:
        if offer.get('variable_pct_min') is not None:
            continue
        evidence=' '.join(str(offer.get(field) or '').lower() for field in ('condition','notes','verification'))
        offer['pricing_model']='Individual' if any(marker in evidence for marker in quote_markers) else 'Not public'
    return offers


def write_csv(output:dict)->None:
    cols=['id','country_iso2','country','provider','provider_type','provider_role','product','method','channel','pricing_model','pricing_model_code','variable_pct_basis','variable_pct_min','variable_pct_max','fixed_fee_min','fixed_fee_max','minimum_fee','fee_currency','monthly_fee','monthly_currency','monthly_fee_mode','non_transaction_fees','card_schemes','card_profile','all_in_complete','tax_treatment','reference_transaction_eur','fee_reference_min_czk','fee_reference_max_czk','effective_reference_min_pct','effective_reference_max_pct','condition','source_url','verification','verification_state','verification_scope','price_verified_on','monitoring_level','source_status','source_checked_at','source_last_attempt_at','source_last_attempt_status']
    with (DATA/'latest.csv').open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=cols,lineterminator='\n');w.writeheader()
        offers=sorted(output['offers'],key=lambda o:(o.get('country_iso2',''),(o.get('provider') or '').casefold(),o.get('method',''),o.get('product',''),o.get('id','')))
        for o in offers:
            c=o.get('calculation_reference',{})
            row={k:o.get(k) for k in cols}
            for field in ('non_transaction_fees','card_schemes'):
                row[field]=json.dumps(row.get(field),ensure_ascii=False)
            row.update({'reference_transaction_eur':REFERENCE_TRANSACTION_EUR,'fee_reference_min_czk':c.get('fee_min_czk'),'fee_reference_max_czk':c.get('fee_max_czk'),'effective_reference_min_pct':c.get('effective_min_pct'),'effective_reference_max_pct':c.get('effective_max_pct')})
            w.writerow(row)


def main()->int:
    baseline=json.loads(BASELINE.read_text(encoding='utf-8'))
    previous=load_previous(baseline)
    # DŮLEŽITÉ: vždy vycházet z čerstvého manual_offers.json, ne z minulého
    # vygenerovaného latest.json - jinak by každý další běh jen dokola
    # recykloval starý výstup a ignoroval jakékoliv ruční opravy v baseline.
    # 'previous' slouží níž jen ke sledování změn (prev_by_id), ne jako zdroj dat.
    offers=normalize_unpriced_pricing_models(normalize_provider_names(normalize_clearhaus_minimum_fees(normalize_card_schemes(normalize_revolut_cee_offers(
        apply_provider_gap_overlay(apply_europe_verified_overlay(
            apply_cee_verified_overlay(deepcopy(baseline['offers']),baseline['countries']),
            baseline['countries'],
        ),baseline['countries'])
    )))))
    offers=apply_audit_corrections(offers)
    prev_by_id={o['id']:o for o in previous.get('offers',[])}
    schema_fields={'pricing_model_code','monthly_fee_mode','all_in_complete','card_schemes','card_profile','tax_treatment','variable_pct_basis'}
    changes=[change for change in previous.get('change_log',[]) if not (
        change.get('change_origin')=='dataset' and change.get('old') is None and change.get('field') in schema_fields
    )][-250:]
    now=datetime.now(timezone.utc).isoformat(timespec='seconds')
    for offer in offers:
        initialise_temporal_metadata(offer,prev_by_id.get(offer['id']))

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
    active_sources=referenced_source_configs(offers,baseline['sources'])
    for sid,s in active_sources.items():
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
    offers=apply_audit_corrections(offers,generated_only=True)
    offers=expand_explicit_alternative_scheme_rates(offers)

    # A source access is not the same event as a semantic price verification.
    # Keep the last attempt visible even when the last successful state and
    # last good price must be retained.
    for offer in offers:
        sid=offer.get('source_id')
        if not sid:
            continue
        previous_offer=prev_by_id.get(offer['id'],{})
        source=fetched.get(sid,{})
        if offline:
            offer['source_last_attempt_at']=previous_offer.get('source_last_attempt_at')
            offer['source_last_attempt_status']=previous_offer.get('source_last_attempt_status','not_attempted')
            continue
        offer['source_last_attempt_at']=source.get('checked_at',now)
        offer['source_last_attempt_status']=source.get('status','not_checked')
        if source.get('status')=='ok':
            offer['source_checked_at']=source.get('checked_at')
            offer['source_hash']=source.get('hash') or offer.get('source_hash')

    for o in offers:
        sid=o.get('source_id')
        if o.get('parser',{}).get('type')=='adyen_country_method':
            src=fetched.get(sid,{})
            if adyen_catalog and src.get('status')=='ok':
                o['source_checked_at']=src.get('checked_at')
                if o.get('source_status')=='ok':
                    o['price_verified_on']=date.fromisoformat(now[:10]).isoformat()
            if src.get('hash'):o['source_hash']=src['hash']
            if not adyen_catalog:
                old=prev_by_id.get(o['id'])
                if old:
                    for field in ('source_status','source_checked_at','source_hash','verification','price_verified_on'):
                        o[field]=old.get(field)
                else:
                    o['source_status']=src.get('status','not checked')
                    o['verification']='retained – Adyen country/method source unavailable'
                    o['source_checked_at']=None
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
            o['calculation_reference']=calc(o,fx)
            continue
        src=fetched.get(sid,{})
        o['source_status']=src.get('status','not checked')
        if src.get('hash'):o['source_hash']=src['hash']
        if src.get('status')!='ok':
            old=prev_by_id.get(o['id'])
            if old:
                for field in ('source_status','source_checked_at','source_hash','verification','price_verified_on'):
                    o[field]=old.get(field)
            else:
                o['verification']='retained – source unavailable'
                o['source_checked_at']=None
            o['calculation_reference']=calc(o,fx);continue
        o['source_checked_at']=src.get('checked_at')
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
            o['price_verified_on']=date.fromisoformat(now[:10]).isoformat()
            if changed:o['last_changed_at']=now
        else:
            o['verification']=f'retained – review suggested ({conf:.0%}; {why})'
        o['calculation_reference']=calc(o,fx)

    # Re-apply generated-row corrections after parser/fallback handling so an
    # offline run cannot replace a later manual semantic review with legacy
    # automated wording from the previous snapshot.
    offers=apply_audit_corrections(offers,generated_only=True)
    for offer in offers:
        if not offer.get('price_verified_on'):
            offer['price_verified_on']=infer_price_verified_on(offer)
    normalise_offer_schema(offers)
    assign_provider_roles(offers)
    normalise_provider_types(offers)
    validate_temporal_metadata(offers,now)
    validate_offer_schema(offers)

    registry=json.loads(CEE_REGISTRY.read_text(encoding='utf-8')) if CEE_REGISTRY.exists() else {'providers':[]}
    europe_registry=json.loads(EUROPE_REGISTRY.read_text(encoding='utf-8')) if EUROPE_REGISTRY.exists() else {'providers':[]}
    watchlist=json.loads(EUROPE_WATCHLIST.read_text(encoding='utf-8')) if EUROPE_WATCHLIST.exists() else {'providers':[]}
    master_crosscheck=json.loads(PROVIDER_MASTER_CROSSCHECK.read_text(encoding='utf-8')) if PROVIDER_MASTER_CROSSCHECK.exists() else {'regulatory_master':{},'verified_country_additions':[]}
    cee_audit=build_cee_audit(registry,offers)
    europe_audit=build_registry_audit(europe_registry,offers)
    watchlist_audit=build_registry_audit(watchlist,offers)
    unresolved_acquiring_candidates=[
        item for item in master_crosscheck.get('regulatory_master',{}).get('new_candidate_groups',[])
        if item.get('category')=='regulatory_acquiring_candidate'
    ]
    unpriced=[offer for offer in offers if offer.get('variable_pct_min') is None]
    data_quality={
        'offer_row_count':len(offers),
        'numeric_price_row_count':len(offers)-len(unpriced),
        'no_numeric_price_row_count':len(unpriced),
        'quote_or_individual_price_row_count':sum(1 for offer in unpriced if offer.get('pricing_model')=='Individual'),
        'no_public_price_row_count':sum(1 for offer in unpriced if offer.get('pricing_model')=='Not public'),
        'source_reviewed_row_count':sum(1 for offer in offers if is_source_reviewed_offer(offer)),
        'unpriced_source_reviewed_row_count':sum(1 for offer in unpriced if is_source_reviewed_offer(offer)),
        'unpriced_needs_individual_source_review_row_count':sum(1 for offer in unpriced if not is_source_reviewed_offer(offer)),
        'price_verified_on_row_count':sum(1 for offer in offers if offer.get('price_verified_on')),
        'price_verification_date_missing_row_count':sum(1 for offer in offers if not offer.get('price_verified_on')),
        'successful_source_check_timestamp_row_count':sum(1 for offer in offers if offer.get('source_checked_at')),
        'active_price_parser_row_count':sum(1 for offer in offers if offer.get('monitoring_level')=='price_parser'),
        'source_monitor_row_count':sum(1 for offer in offers if offer.get('monitoring_level')=='source_monitor'),
        'manual_monitoring_row_count':sum(1 for offer in offers if offer.get('monitoring_level')=='manual'),
        'last_attempt_failed_row_count':sum(1 for offer in offers if str(offer.get('source_last_attempt_status','')).startswith('error')),
        'recurring_fee_row_count':sum(1 for offer in offers if (offer.get('monthly_fee') or 0)>0),
        'unresolved_acquiring_candidate_count':len(unresolved_acquiring_candidates),
        'all_offer_source_audit_frequency':'weekly',
        'counting_note':'Dashboard keeps distinct tariff, channel and card-profile rows. Exact duplicate economics may be collapsed; the export keeps every source row.',
    }
    changes=ensure_current_overlay_additions(append_dataset_changes(changes,previous.get('offers',[]),offers,now),offers,now)
    output={'generated_at':now,'update_frequency':'weekly','default_transaction_eur':REFERENCE_TRANSACTION_EUR,'methodology_version':'2.3',
            'scope_note':'Publicly displayed merchant acceptance prices. Acquirers, PSPs, gateways and A2A wallets are separated by provider type; they are not automatically treated as economically identical.',
            'comparison_profile':{'transaction_amount':REFERENCE_TRANSACTION_EUR,'transaction_currency':'EUR','monthly_acceptance_turnover_eur':REFERENCE_MONTHLY_ACCEPTANCE_TURNOVER_EUR,'monthly_fee_method':'Additional recurring fees are allocated over the reference monthly acceptance turnover; minimum commitments are applied as a floor.','headline_scope':'Online or channel-unspecified consumer-card offers; all-card tariffs are included because they also apply to consumer cards. POS, commercial-card and card-profile-unspecified offers are excluded.','icpp_profile':'authenticated EEA consumer debit / domestic UK consumer debit; Swiss domestic e-commerce debit','interchange_reference_pct':ICPP_EEA_DEBIT_INTERCHANGE_REFERENCE_PCT,'eea_scheme_fee_scenarios':[dict(item) for item in ICPP_EEA_SCHEME_FEE_SCENARIOS],'uk_scheme_fee_scenarios':[dict(item) for item in ICPP_UK_SCHEME_FEE_SCENARIOS],'scheme_fee_source_url':ICPP_SCHEME_FEE_SOURCE_URL,'switzerland':{'interchange_pct':ICPP_CH_CNP_DEBIT_INTERCHANGE_REFERENCE_PCT,'scheme_fee_pct':ICPP_CH_SCHEME_FEE_REFERENCE_PCT,'scheme_fee_fixed_chf':ICPP_CH_SCHEME_FEE_REFERENCE_FIXED_CHF,'interchange_source_url':ICPP_CH_INTERCHANGE_SOURCE_URL}},
            'cee_acquirer_registry':{'as_of':registry.get('as_of'),'provider_count':len(registry.get('providers',[]))},
            'europe_acquirer_registry':{'as_of':europe_registry.get('as_of'),'provider_count':len(europe_registry.get('providers',[])),'country_count':len({item.get('country_iso2') for item in europe_registry.get('providers',[])})},
            'europe_acquirer_watchlist':{'as_of':watchlist.get('as_of'),'provider_count':len(watchlist.get('providers',[])),'country_count':len({item.get('country_iso2') for item in watchlist.get('providers',[])})},
            'watchlist_audit':watchlist_audit,
            'provider_master_crosscheck':{'as_of':master_crosscheck.get('as_of'),'normalised_group_count':master_crosscheck.get('regulatory_master',{}).get('normalised_group_count',0),'new_candidate_group_count':len(master_crosscheck.get('regulatory_master',{}).get('new_candidate_groups',[])),'unresolved_acquiring_candidate_count':len(unresolved_acquiring_candidates),'verified_country_addition_count':len(master_crosscheck.get('verified_country_additions',[]))},
            'data_quality':data_quality,
            'cee_audit':cee_audit,
            'europe_audit':europe_audit,
            'fx':{'source':'Česká národní banka','source_url':CNB_URL,'date':fx_date,'rates':fx},'sources':baseline['sources'],'countries':baseline['countries'],'offers':offers,'change_log':changes[-250:]}
    DATA.mkdir(parents=True,exist_ok=True);HISTORY.mkdir(parents=True,exist_ok=True)
    (DATA/'latest.json').write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
    (DATA/'changes.json').write_text(json.dumps(changes[-250:],ensure_ascii=False,indent=2),encoding='utf-8')
    (DATA/'cee_audit.json').write_text(json.dumps(cee_audit,ensure_ascii=False,indent=2),encoding='utf-8')
    (DATA/'europe_audit.json').write_text(json.dumps(europe_audit,ensure_ascii=False,indent=2),encoding='utf-8')
    if os.environ.get('ACQ_TRACKER_SKIP_HISTORY')!='1':
        history_name=datetime.fromisoformat(now).astimezone(timezone.utc).strftime('%Y-%m-%dT%H%M%SZ.json')
        (HISTORY/history_name).write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
    idx=[]
    for p in sorted(HISTORY.glob('*.json')):
        if p.name!='index.json':idx.append({'date':p.stem[:10],'timestamp':p.stem,'file':p.name})
    (HISTORY/'index.json').write_text(json.dumps(idx,indent=2),encoding='utf-8')
    write_csv(output)
    log.info('Wrote %d offers, CNB FX %s, %d history points',len(offers),fx_date,len(idx))
    return 0

if __name__=='__main__':sys.exit(main())
