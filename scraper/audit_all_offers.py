#!/usr/bin/env python3
"""Audit every published offer against its current official source.

This scanner is deliberately evidence-producing, not self-editing. A numeric
match is a triage signal; a mismatch or newly found lead must be reviewed
before the merchant price is changed.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

try:
    from scraper.audit_unpriced import OfficialSiteScanner, extract_price_leads, origin
except ModuleNotFoundError:  # direct ``python scraper/audit_all_offers.py`` execution
    from audit_unpriced import OfficialSiteScanner, extract_price_leads, origin

ROOT = Path(__file__).resolve().parents[1]
LATEST = ROOT / "docs" / "data" / "latest.json"
OUTPUT = ROOT / "docs" / "data" / "full_source_audit.json"

CURRENCY_MARKERS = {
    "EUR": ("EUR", "€"), "GBP": ("GBP", "£"), "USD": ("USD", "$"),
    "CHF": ("CHF",), "CZK": ("CZK", "KČ", "Kč"), "PLN": ("PLN", "ZŁ", "zł"),
    "HUF": ("HUF", "FT", "Ft"), "RON": ("RON", "LEI", "lei"), "BGN": ("BGN", "ЛВ", "лв"),
    "DKK": ("DKK", "KR", "kr"), "SEK": ("SEK", "KR", "kr"), "NOK": ("NOK", "KR", "kr"),
    "ISK": ("ISK", "KR", "kr"),
}


def number_pattern(value: float) -> str:
    rendered = f"{value:.4f}".rstrip("0").rstrip(".")
    whole, dot, fraction = rendered.partition(".")
    if not dot:
        return rf"(?<!\d){re.escape(whole)}(?:[.,]0+)?(?!\d)"
    return rf"(?<!\d){re.escape(whole)}[.,]{re.escape(fraction)}0*(?!\d)"


def percent_present(text: str, value: float | None) -> bool:
    if value is None:
        return False
    return bool(re.search(number_pattern(float(value)) + r"\s*%", text, re.I))


def money_present(text: str, value: float | None, currency: str | None) -> bool:
    if value in (None, 0):
        return True
    amount = number_pattern(float(value))
    markers = CURRENCY_MARKERS.get(currency or "", (currency or "",))
    marker = "(?:" + "|".join(re.escape(item) for item in markers if item) + ")"
    if marker == "(?:)":
        return bool(re.search(amount, text, re.I))
    return bool(re.search(rf"(?:{marker}\s*{amount}|{amount}\s*{marker})", text, re.I))


def evidence_match(row: dict, texts: list[str]) -> dict:
    text = " ".join(texts)
    variable_values = list(dict.fromkeys(
        value for value in (row.get("variable_pct_min"), row.get("variable_pct_max"))
        if value not in (None, 0)
    ))
    fixed_values = list(dict.fromkeys(
        value for value in (row.get("fixed_fee_min"), row.get("fixed_fee_max"), row.get("minimum_fee"))
        if value not in (None, 0)
    ))
    monthly_values = [row.get("monthly_fee")] if (row.get("monthly_fee") or 0) > 0 else []
    setup_values = [row.get("setup_fee")] if row.get("setup_fee") not in (None, 0) else []
    variable_matches = [percent_present(text, value) for value in variable_values]
    fixed_matches = [money_present(text, value, row.get("fee_currency")) for value in fixed_values]
    monthly_matches = [money_present(text, value, row.get("monthly_currency") or row.get("fee_currency")) for value in monthly_values]
    setup_matches = [money_present(text, value, row.get("setup_currency") or row.get("fee_currency")) for value in setup_values]
    checks = variable_matches + fixed_matches + monthly_matches + setup_matches
    return {
        "variable_values": variable_values,
        "variable_matches": variable_matches,
        "fixed_values": fixed_values,
        "fixed_matches": fixed_matches,
        "monthly_values": monthly_values,
        "monthly_matches": monthly_matches,
        "setup_values": setup_values,
        "setup_matches": setup_matches,
        "all_stored_components_found": bool(checks) and all(checks),
    }


def scan_url(url: str, linked_page_limit: int, timeout: int) -> tuple[str, dict]:
    """Scan one source independently so one slow provider cannot stall a domain batch."""
    scanner = OfficialSiteScanner(timeout=timeout)
    scan = scanner.scan(url, linked_page_limit)
    texts = []
    for page in scan["pages_checked"]:
        fetched = scanner.fetch(page["url"])
        if fetched.get("text"):
            texts.append(fetched["text"])
    return url, {"scan": scan, "texts": texts}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--linked-page-limit", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    dataset = json.loads(LATEST.read_text(encoding="utf-8"))
    by_url: dict[str, list[dict]] = defaultdict(list)
    for row in dataset["offers"]:
        if row.get("source_url"):
            by_url[row["source_url"]].append(row)
    urls = sorted(by_url)
    if args.limit:
        urls = urls[:args.limit]
    fetched = {}
    completed = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {
            pool.submit(scan_url, url, args.linked_page_limit, args.timeout): url
            for url in urls
        }
        for future in as_completed(futures):
            url, result = future.result()
            fetched[url] = result
            completed += 1
            if completed % 10 == 0 or completed == len(urls):
                print(f"audited {completed}/{len(urls)} official URLs", file=sys.stderr, flush=True)

    rows = []
    for url in urls:
        source = fetched[url]
        scan = source["scan"]
        source_ok = any(page.get("status") == "ok" for page in scan["pages_checked"])
        for row in by_url[url]:
            previously_verified = (
                row.get("verification_state") in {"verified_manual", "verified_automated"}
                and bool(row.get("price_verified_on"))
            )
            if row.get("variable_pct_min") is None:
                leads = []
                for text in source["texts"]:
                    leads.extend(extract_price_leads(text, url, limit=8))
                leads = list(dict.fromkeys(leads))[:8]
                if leads and row.get("price_lead_review") == "non_merchant_or_incomplete":
                    outcome = "unpriced_numeric_lead_rejected_after_manual_review"
                elif leads:
                    outcome = "unpriced_public_price_lead_requires_review"
                elif source_ok and previously_verified:
                    outcome = "unpriced_manually_verified_without_public_number"
                elif source_ok:
                    outcome = "unpriced_no_numeric_lead_on_scanned_official_pages"
                elif previously_verified:
                    outcome = "source_blocked_but_previously_verified"
                else:
                    outcome = "source_unavailable_or_blocked"
                match = None
            else:
                match = evidence_match(row, source["texts"])
                if not source_ok and previously_verified:
                    outcome = "source_blocked_but_previously_verified"
                elif not source_ok:
                    outcome = "source_unavailable_or_blocked"
                elif match["all_stored_components_found"]:
                    outcome = "stored_components_found_on_official_source"
                elif previously_verified:
                    outcome = "priced_row_manually_verified_parser_mismatch"
                else:
                    outcome = "priced_row_requires_manual_source_review"
                leads = []
            rows.append({
                "id": row["id"], "country_iso2": row["country_iso2"], "provider": row["provider"],
                "method": row["method"], "pricing_model": row.get("pricing_model"),
                "verification_state": row.get("verification_state"), "price_verified_on": row.get("price_verified_on"),
                "price_lead_review": row.get("price_lead_review"),
                "source_url": url, "source_outcome": scan["outcome"], "outcome": outcome,
                "evidence_match": match, "price_leads": leads,
            })

    counts = Counter(row["outcome"] for row in rows)
    source_counts = Counter(
        "ok" if any(page.get("status") == "ok" for page in fetched[url]["scan"]["pages_checked"])
        else fetched[url]["scan"]["outcome"]
        for url in urls
    )
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dataset_generated_at": dataset.get("generated_at"),
        "scope": "All-offer official-source audit. Automated matches are triage evidence, never authority to change a merchant price without semantic review.",
        "offer_rows_scanned": len(rows), "unique_official_urls_scanned": len(urls),
        "source_outcomes": dict(sorted(source_counts.items())), "row_outcomes": dict(sorted(counts.items())),
        "rows": rows,
        "sources": [fetched[url]["scan"] for url in urls],
    }
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("offer_rows_scanned", "unique_official_urls_scanned", "source_outcomes", "row_outcomes")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
