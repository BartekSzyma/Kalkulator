#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import re
import time
from io import StringIO
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

BASE = "https://www.biznesradar.pl"
HERE = Path(__file__).resolve().parent
OUT = HERE / "data" / "companies.json"
TICKERS = HERE / "companies.txt"

S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; M3S-research/1.0; +https://github.com/BartekSzyma/Kalkulator)",
    "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.7",
})

def norm(s):
    s = str(s or "").replace("\xa0", " ").strip().lower()
    trans = str.maketrans("ąćęłńóśźż", "acelnoszz")
    return re.sub(r"\s+", " ", s.translate(trans))

def parse_number(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    s = str(v).replace("\xa0", " ").strip()
    if not s or s in {"-", "nan", "None"}:
        return None
    m = re.search(r"[-+]?\d[\d\s]*(?:[,.]\d+)?", s)
    if not m:
        return None
    x = m.group(0).replace(" ", "").replace(",", ".")
    try:
        return float(x)
    except ValueError:
        return None

def get(url, tries=3):
    last = None
    for i in range(tries):
        try:
            r = S.get(url, timeout=35, allow_redirects=True)
            r.raise_for_status()
            return r
        except Exception as e:
            last = e
            time.sleep(1.2 * (i + 1))
    raise RuntimeError(f"{url}: {last}")

def slug_and_profile(ticker):
    r = get(f"{BASE}/notowania/{ticker}")
    slug = r.url.rstrip("/").split("/")[-1]
    soup = BeautifulSoup(r.text, "html.parser")
    text = soup.get_text(" ", strip=True)

    current_price = None
    title = soup.find("h1")
    if title:
        block = title.parent.get_text(" ", strip=True)
        m = re.search(r"\b(\d{1,6}[,.]\d{1,4})\s*[+-]\d", block)
        if m:
            current_price = parse_number(m.group(1))
    if current_price is None:
        m = re.search(r"\b(\d{1,6}[,.]\d{1,4})\s*[+-]\d", text[:3000])
        if m:
            current_price = parse_number(m.group(1))

    company_name = ticker
    for h in soup.find_all(["h1", "h2"]):
        t = h.get_text(" ", strip=True)
        if "SPÓŁKA" in t.upper() or "S.A." in t.upper() or " SA" in t.upper():
            company_name = t
            break

    return slug, company_name, current_price

def read_tables(url):
    r = get(url)
    try:
        return pd.read_html(StringIO(r.text), decimal=",", thousands=" ")
    except ValueError:
        return []

def flatten_col(c):
    if isinstance(c, tuple):
        parts = [str(x) for x in c if str(x) != "nan" and not str(x).startswith("Unnamed")]
        return " ".join(parts).strip()
    return str(c)

def period_from_col(c):
    s = flatten_col(c)
    m = re.search(r"(\d{4})/Q([1-4])", s)
    return f"{m.group(1)}/Q{m.group(2)}" if m else None

def table_to_rows(tables, required_periods=4):
    best = None
    best_score = -1
    for df in tables:
        periods = [period_from_col(c) for c in df.columns]
        score = sum(p is not None for p in periods)
        if score > best_score:
            best, best_score = df, score
    if best is None or best_score < required_periods:
        raise RuntimeError(f"nie znaleziono tabeli kwartalnej (rozpoznano {best_score} okresów)")

    period_cols = [(c, period_from_col(c)) for c in best.columns if period_from_col(c)]
    label_col = next((c for c in best.columns if not period_from_col(c)), best.columns[0])
    rows = []
    for _, row in best.iterrows():
        label = str(row.get(label_col, "")).strip()
        if not label or label.lower() == "nan":
            continue
        values = {p: parse_number(row.get(c)) for c, p in period_cols}
        rows.append((label, values))
    periods = [p for _, p in period_cols]
    return periods, rows

def choose_row(rows, exact=(), contains=(), occurrence=0):
    candidates = []
    exact_n = [norm(x) for x in exact]
    contains_n = [norm(x) for x in contains]
    for label, values in rows:
        nl = norm(label)
        if any(nl == x for x in exact_n):
            candidates.append((label, values))
    if not candidates:
        for label, values in rows:
            nl = norm(label)
            if any(x in nl for x in contains_n):
                candidates.append((label, values))
    if len(candidates) > occurrence:
        return candidates[occurrence][1]
    return {}

def rolling4(periods, values):
    out = {}
    for i, p in enumerate(periods):
        if i < 3:
            out[p] = None
            continue
        xs = [values.get(periods[j]) for j in range(i-3, i+1)]
        out[p] = sum(xs) if all(x is not None for x in xs) else None
    return out

def abs_map(d):
    return {k: (abs(v) if v is not None else None) for k, v in d.items()}

def scrape(ticker):
    slug, company_name, live_price = slug_and_profile(ticker)

    balance_url = f"{BASE}/raporty-finansowe-bilans/{slug},Q,0"
    income_url = f"{BASE}/raporty-finansowe-rachunek-zyskow-i-strat/{slug},Q"
    cashflow_url = f"{BASE}/raporty-finansowe-przeplywy-pieniezne/{slug},Q"
    market_url = f"{BASE}/wskazniki-wartosci-rynkowej/{slug}"

    bp, br = table_to_rows(read_tables(balance_url))
    ip, ir = table_to_rows(read_tables(income_url))
    cp, cr = table_to_rows(read_tables(cashflow_url), required_periods=1)
    mp, mr = table_to_rows(read_tables(market_url))

    equity = choose_row(br, exact=("Kapitał własny akcjonariuszy jednostki dominującej", "Kapitał własny"), contains=("Kapitał własny akcjonariuszy jednostki dominującej", "Kapitał własny"))
    goodwill = choose_row(br, exact=("Wartość firmy", "Goodwill"), contains=("Wartość firmy", "Goodwill"))
    cash = choose_row(br, exact=("Środki pieniężne i inne aktywa pieniężne", "Środki pieniężne"), contains=("Środki pieniężne i inne aktywa pieniężne",))

    debts = [(label, vals) for label, vals in br if "kredyty i pozyczki" in norm(label)]
    leases = [(label, vals) for label, vals in br if "leasing" in norm(label)]
    long_debt = debts[0][1] if len(debts) >= 1 else {}
    short_debt = debts[1][1] if len(debts) >= 2 else {}
    long_lease = leases[0][1] if len(leases) >= 1 else {}
    short_lease = leases[1][1] if len(leases) >= 2 else {}

    net_income_q = choose_row(ir, exact=("Zysk netto akcjonariuszy jednostki dominującej", "Zysk netto"), contains=("Zysk netto akcjonariuszy jednostki dominującej", "Zysk netto"))
    net_income_ttm = rolling4(ip, net_income_q)

    dividend_q = abs_map(choose_row(cr, exact=("Dywidenda",), contains=("Dywidenda",)))
    buyback_q = abs_map(choose_row(cr, exact=("Skup akcji",), contains=("Skup akcji", "Nabycie akcji własnych")))
    dividend_ttm = rolling4(cp, dividend_q)
    buyback_ttm = rolling4(cp, buyback_q)

    price_q = choose_row(mr, exact=("Kurs",), contains=("Kurs",))
    shares_q = choose_row(mr, exact=("Liczba akcji",), contains=("Liczba akcji",))

    common = [p for p in bp if p in ip and p in mp]
    history = []
    for p in common[-12:]:
        lease = (long_lease.get(p) or 0) + (short_lease.get(p) or 0)
        history.append({
            "period": p,
            "equity": equity.get(p),
            "goodwill": goodwill.get(p) or 0,
            "net_profit_ttm": net_income_ttm.get(p),
            "dividend_ttm": dividend_ttm.get(p) or 0,
            "buyback_ttm": buyback_ttm.get(p) or 0,
            "cash": cash.get(p) or 0,
            "short_debt": short_debt.get(p) or 0,
            "long_debt": long_debt.get(p) or 0,
            "lease": lease,
            "shares": shares_q.get(p),
            "price": price_q.get(p),
        })

    valid = [h for h in history if h["equity"] is not None and h["net_profit_ttm"] is not None and h["shares"]]
    if not valid:
        raise RuntimeError("brak kompletnego okresu z kapitałem, TTM i liczbą akcji")

    current = dict(valid[-1])
    current["price"] = live_price if live_price is not None else current.get("price")

    warnings = []
    if not goodwill:
        warnings.append("Nie znaleziono osobnego wiersza goodwill; przyjęto 0.")
    if len(debts) < 2:
        warnings.append("Nie rozdzielono pewnie długu długo- i krótkoterminowego.")
    if len(leases) < 2:
        warnings.append("Leasing może być niepełny albo niewykazywany osobno.")
    if not dividend_q:
        warnings.append("Nie znaleziono dywidend w cash flow; przyjęto 0.")
    if not buyback_q:
        warnings.append("Nie znaleziono skupu akcji w cash flow; przyjęto 0.")

    return {
        "ticker": ticker,
        "slug": slug,
        "name": company_name,
        "currency": "PLN",
        "current": current,
        "history": valid[-8:],
        "warnings": warnings,
        "sources": {"balance": balance_url, "income": income_url, "cashflow": cashflow_url, "market": market_url},
    }

def main():
    tickers = []
    for line in TICKERS.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip().upper()
        if line:
            tickers.append(line)

    payload = {"generated_at": pd.Timestamp.utcnow().isoformat(), "companies": {}, "errors": {}}
    for i, ticker in enumerate(tickers, 1):
        print(f"[{i}/{len(tickers)}] {ticker}", flush=True)
        try:
            payload["companies"][ticker] = scrape(ticker)
            print("  OK", flush=True)
        except Exception as e:
            payload["errors"][ticker] = str(e)
            print("  ERROR:", e, flush=True)
        time.sleep(0.7)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved {len(payload['companies'])} companies, {len(payload['errors'])} errors -> {OUT}")

if __name__ == "__main__":
    main()
