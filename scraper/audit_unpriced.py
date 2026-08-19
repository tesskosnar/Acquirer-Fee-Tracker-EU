#!/usr/bin/env python3
"""Deep-scan official provider pages for public pricing leads.

The report is deliberately conservative: a detected expression is only a lead
for manual verification. It never writes a number into the published offers.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import time
import urllib.robotparser
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

import pdfplumber
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
LATEST = ROOT / "docs" / "data" / "latest.json"
OUTPUT = ROOT / "docs" / "data" / "unpriced_audit.json"
USER_AGENT = "AcquirerFeeTrackerBot/1.0 (public-interest research; read-only public pricing audit)"
TIMEOUT = 20

PRICE_LINK_RE = re.compile(
    r"pricing|prices?|fees?|charges?|rates?|tariff|tarif|cennik|cenik|cen[yi]|"
    r"preise|geb[uü]hren|kosten|tarieven|priser|ceny|cene|cijene|tarife|"
    r"comisioane|comisiones|comiss[oõ]es|taxas|custos|kond[ií]ci|d[ií]j|"
    r"op[lł]at|τιμ|цени|тариф",
    re.I,
)
PAYMENT_CONTEXT_RE = re.compile(
    r"merchant|acquir|card|payment|transaction|checkout|online|e-?commerce|"
    r"fee|commission|pricing|accept|betal|zahlung|paiement|pagament|platb|"
    r"kart|tranzac|transak|obchod|trgov|schem|interchange|direct debit|a2a",
    re.I,
)
FEE_EXPRESSION_RE = re.compile(
    r"(?:from|starting at|starts? at|ab|od|fra|från|alkaen|à partir de|da\s+)?"
    r"\d{1,2}(?:[.,]\d{1,4})?\s*%"
    r"(?:\s*(?:\+|plus)\s*(?:(?:€|£|CHF|EUR|GBP|DKK|SEK|NOK|PLN|CZK|RON|HUF|BGN)\s*)?"
    r"\d+(?:[.,]\d{1,4})?\s*(?:€|£|CHF|EUR|GBP|DKK|SEK|NOK|PLN|CZK|RON|HUF|BGN)?)?",
    re.I,
)
MONEY_PLUS_PERCENT_RE = re.compile(
    r"(?:€|£|CHF|EUR|GBP|DKK|SEK|NOK|PLN|CZK|RON|HUF|BGN)\s*"
    r"\d+(?:[.,]\d{1,4})?\s*(?:\+|plus)\s*\d{1,2}(?:[.,]\d{1,4})?\s*%",
    re.I,
)


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def same_host(left: str, right: str) -> bool:
    def host(value: str) -> str:
        return (urlparse(value).hostname or "").lower().removeprefix("www.")

    return host(left) == host(right)


def extract_price_leads(text: str, limit: int = 12) -> list[str]:
    leads: list[str] = []
    for regex in (FEE_EXPRESSION_RE, MONEY_PLUS_PERCENT_RE):
        for match in regex.finditer(text):
            start = max(0, match.start() - 120)
            end = min(len(text), match.end() + 120)
            context = clean_text(text[start:end])
            if not PAYMENT_CONTEXT_RE.search(context):
                continue
            if context not in leads:
                leads.append(context)
            if len(leads) >= limit:
                return leads
    return leads


class OfficialSiteScanner:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})
        self.robots: dict[str, urllib.robotparser.RobotFileParser] = {}
        self.cache: dict[str, dict] = {}
        self.last_request: dict[str, float] = defaultdict(float)

    def allowed(self, url: str) -> bool:
        site = origin(url)
        if site not in self.robots:
            parser = urllib.robotparser.RobotFileParser()
            try:
                response = self.session.get(site + "/robots.txt", timeout=10)
                parser.parse((response.text if response.ok else "").splitlines())
            except requests.RequestException:
                parser.parse([])
            self.robots[site] = parser
        return self.robots[site].can_fetch(USER_AGENT, url)

    def fetch(self, url: str) -> dict:
        url = urldefrag(url)[0]
        if url in self.cache:
            return self.cache[url]
        if not self.allowed(url):
            result = {"url": url, "status": "robots_disallowed", "text": "", "links": []}
            self.cache[url] = result
            return result

        site = origin(url)
        delay = 0.35 - (time.monotonic() - self.last_request[site])
        if delay > 0:
            time.sleep(delay)
        try:
            response = self.session.get(url, timeout=TIMEOUT, allow_redirects=True)
            self.last_request[site] = time.monotonic()
            if response.status_code in {401, 403, 429}:
                result = {"url": url, "final_url": response.url, "status": f"http_{response.status_code}", "text": "", "links": []}
            else:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()
                if "pdf" in content_type or response.url.lower().endswith(".pdf"):
                    with pdfplumber.open(io.BytesIO(response.content)) as pdf:
                        text = " ".join(page.extract_text() or "" for page in pdf.pages)
                    result = {"url": url, "final_url": response.url, "status": "ok", "text": clean_text(text), "links": []}
                else:
                    soup = BeautifulSoup(response.text, "html.parser")
                    links = []
                    for anchor in soup.find_all("a", href=True):
                        absolute = urldefrag(urljoin(response.url, anchor["href"]))[0]
                        label = clean_text(anchor.get_text(" ", strip=True))
                        if absolute.startswith("http") and same_host(response.url, absolute) and PRICE_LINK_RE.search(label + " " + absolute):
                            links.append(absolute)
                    for element in soup(["script", "style", "noscript", "svg"]):
                        element.decompose()
                    result = {
                        "url": url,
                        "final_url": response.url,
                        "status": "ok",
                        "text": clean_text(" ".join(soup.stripped_strings)),
                        "links": list(dict.fromkeys(links))[:8],
                    }
        except (requests.RequestException, ValueError) as exc:
            result = {"url": url, "status": "error", "error": f"{type(exc).__name__}: {exc}", "text": "", "links": []}
        self.cache[url] = result
        return result

    def scan(self, url: str, linked_page_limit: int = 3) -> dict:
        primary = self.fetch(url)
        pages = [primary]
        for candidate in primary.get("links", [])[:linked_page_limit]:
            pages.append(self.fetch(candidate))

        evidence = []
        for page in pages:
            leads = extract_price_leads(page.get("text", ""))
            if leads:
                evidence.append({"url": page.get("final_url", page["url"]), "price_leads": leads})

        statuses = {page["status"] for page in pages}
        if evidence:
            outcome = "public_price_lead_found"
        elif statuses == {"robots_disallowed"}:
            outcome = "robots_disallowed"
        elif "ok" in statuses:
            outcome = "no_numeric_price_found_on_scanned_official_pages"
        else:
            outcome = "source_unavailable_or_blocked"
        return {
            "source_url": url,
            "outcome": outcome,
            "pages_checked": [
                {
                    key: page.get(key)
                    for key in ("url", "final_url", "status", "error")
                    if page.get(key) is not None
                }
                for page in pages
            ],
            "evidence": evidence,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="Scan only the first N unique official URLs (for tests).")
    parser.add_argument("--linked-page-limit", type=int, default=3)
    args = parser.parse_args()

    dataset = json.loads(LATEST.read_text(encoding="utf-8"))
    rows = [offer for offer in dataset["offers"] if offer.get("variable_pct_min") is None]
    by_url: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row.get("source_url"):
            by_url[row["source_url"]].append(row)

    urls = sorted(by_url)
    if args.limit:
        urls = urls[: args.limit]
    scanner = OfficialSiteScanner()
    scans = {url: scanner.scan(url, args.linked_page_limit) for url in urls}

    audited_rows = []
    for url in urls:
        scan = scans[url]
        for row in by_url[url]:
            outcome = scan["outcome"]
            if row.get("pricing_model") == "Individual" and outcome != "public_price_lead_found":
                outcome = "quote_or_individual_price_confirmed_without_public_number"
            audited_rows.append(
                {
                    "id": row["id"],
                    "country_iso2": row["country_iso2"],
                    "provider": row["provider"],
                    "method": row["method"],
                    "provider_role": row.get("provider_role"),
                    "previous_pricing_model": row.get("pricing_model"),
                    "source_url": url,
                    "outcome": outcome,
                }
            )

    outcomes = defaultdict(int)
    for row in audited_rows:
        outcomes[row["outcome"]] += 1
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dataset_generated_at": dataset.get("generated_at"),
        "scope": "Official-source deep scan of rows without a numeric published merchant price. Detected expressions are leads requiring manual validation, never automatically published fees.",
        "total_unpriced_rows_in_dataset": len(rows),
        "rows_scanned": len(audited_rows),
        "unique_official_urls_scanned": len(urls),
        "outcomes": dict(sorted(outcomes.items())),
        "rows": audited_rows,
        "source_scans": list(scans.values()),
    }
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("total_unpriced_rows_in_dataset", "rows_scanned", "unique_official_urls_scanned", "outcomes")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
