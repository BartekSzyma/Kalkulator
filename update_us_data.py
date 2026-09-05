#!/usr/bin/env python3
from __future__ import annotations

import csv
import io
import json
import math
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

HERE = Path(__file__).resolve().parent
TICKERS_FILE = HERE / "us_companies.txt"
OUT = HERE / "data" / "us_companies.json"

SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
SEC_FACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
STOOQ_CSV = "https://stooq.com/q/d/l/?s={ticker}.us&i=d"

HEADERS = {
    "User-Agent": "M3S valuation research BartekSzyma@users.noreply.github.com",
    "Accept-Encoding": "gzip, deflate",
    "Host": "www.sec.gov",
}

session = requests.Session()
session.headers.update({
    "User-Agent": HEADERS["User-Agent"],
    "Accept-Language": "en-US,en;q=0.9",
})


def get_json(url: str, sec=False, tries=3):
    last = None
    for attempt in range(tries):
        try:
            headers = {"User-Agent": HEADERS["User-Agent"], "Accept-Encoding": "gzip, deflate"} if sec else None
            r = session.get(url, headers=headers, timeout=40)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"{url}: {last}")


def get_text(url: str, tries=3):
    last = None
    for attempt in range(tries):
        try:
            r = session.get(url, timeout=40)
            r.raise_for_status()
            return r.text
        except Exception as e:
            last = e
            time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"{url}: {last}")


def read_universe():
    return [x.split("#", 1)[0].strip().upper() for x in TICKERS_FILE.read_text(encoding="utf-8").splitlines() if x.split("#", 1)[0].strip()]


def load_sec_ticker_map():
    raw = get_json(SEC_TICKERS, sec=True)
    out = {}
    for item in raw.values():
        ticker = str(item.get("ticker", "")).upper()
        if ticker:
            out[ticker] = {"cik": int(item["cik_str"]), "name": item.get("title") or ticker}
    return out


def fact_units(companyfacts, concept):
    fact = companyfacts.get("facts", {}).get("us-gaap", {}).get(concept)
    if not fact:
        return []
    units = fact.get("units", {})
    # Prefer USD/shares according to concept; otherwise first unit list.
    for k in ("USD", "shares", "USD/shares", "pure"):
        if k in units:
            return units[k]
    return next(iter(units.values()), [])


def first_existing(companyfacts, concepts):
    for c in concepts:
        units = fact_units(companyfacts, c)
        if units:
            return c, units
    return None, []


def iso_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None


def latest_instant_by_period(companyfacts, concepts):
    concept, units = first_existing(companyfacts, concepts)
    if not units:
        return {}, concept
    candidates = defaultdict(list)
    for f in units:
        if f.get("form") not in {"10-Q", "10-K", "10-Q/A", "10-K/A"}:
            continue
        end = f.get("end")
        fp = f.get("fp")
        fy = f.get("fy")
        val = f.get("val")
        if not end or fp not in {"Q1", "Q2", "Q3", "FY"} or fy is None or val is None:
            continue
        q = {"Q1": 1, "Q2": 2, "Q3": 3, "FY": 4}[fp]
        period = f"{int(fy)}/Q{q}"
        candidates[period].append(f)
    out = {}
    for period, rows in candidates.items():
        rows.sort(key=lambda x: (x.get("filed", ""), x.get("accn", "")))
        out[period] = rows[-1]["val"]
    return out, concept


def quarterly_flow(companyfacts, concepts):
    """Convert SEC YTD flow facts into standalone fiscal quarters.

    Q1 is the Q1 duration. Q2 = Q2 YTD-Q1, Q3 = Q3 YTD-Q2,
    Q4 = FY-Q3 YTD. For issuers that report standalone quarter facts,
    the shortest duration ending in the quarter is preferred.
    """
    concept, units = first_existing(companyfacts, concepts)
    if not units:
        return {}, concept

    by_fy_fp = defaultdict(list)
    for f in units:
        if f.get("form") not in {"10-Q", "10-K", "10-Q/A", "10-K/A"}:
            continue
        fp, fy, val = f.get("fp"), f.get("fy"), f.get("val")
        start, end = iso_date(f.get("start")), iso_date(f.get("end"))
        if fp not in {"Q1", "Q2", "Q3", "FY"} or fy is None or val is None or not start or not end:
            continue
        days = (end - start).days + 1
        if days < 60 or days > 430:
            continue
        by_fy_fp[(int(fy), fp)].append({**f, "days": days})

    # Choose one YTD/annual observation per FY/FP, preferring canonical duration.
    selected = {}
    targets = {"Q1": 91, "Q2": 182, "Q3": 273, "FY": 365}
    for key, rows in by_fy_fp.items():
        fp = key[1]
        rows.sort(key=lambda x: (abs(x["days"] - targets[fp]), -(int(x.get("filed", "")[:4] or 0)), x.get("filed", "")))
        # Recent amendments should win among similarly plausible durations.
        best_delta = abs(rows[0]["days"] - targets[fp])
        plausible = [r for r in rows if abs(r["days"] - targets[fp]) <= best_delta + 10]
        plausible.sort(key=lambda x: x.get("filed", ""))
        selected[key] = plausible[-1]

    out = {}
    fys = sorted({fy for fy, _ in selected})
    for fy in fys:
        vals = {fp: selected.get((fy, fp), {}).get("val") for fp in ("Q1", "Q2", "Q3", "FY")}
        if vals["Q1"] is not None:
            out[f"{fy}/Q1"] = vals["Q1"]
        if vals["Q2"] is not None:
            out[f"{fy}/Q2"] = vals["Q2"] - (vals["Q1"] or 0)
        if vals["Q3"] is not None:
            out[f"{fy}/Q3"] = vals["Q3"] - (vals["Q2"] or 0)
        if vals["FY"] is not None:
            out[f"{fy}/Q4"] = vals["FY"] - (vals["Q3"] or 0)
    return out, concept


def rolling4(flow):
    def key(p):
        y, q = p.split("/Q")
        return int(y), int(q)
    periods = sorted(flow, key=key)
    out = {}
    for i, p in enumerate(periods):
        if i < 3:
            out[p] = None
            continue
        prev = periods[i-3:i+1]
        # Require consecutive quarters.
        seq = [(int(x.split('/Q')[0]), int(x.split('/Q')[1])) for x in prev]
        ords = [y * 4 + q for y, q in seq]
        vals = [flow.get(x) for x in prev]
        out[p] = sum(vals) if all(v is not None for v in vals) and all(ords[j] + 1 == ords[j+1] for j in range(3)) else None
    return out


def sum_maps(*maps):
    keys = set().union(*[set(m) for m in maps])
    return {k: sum((m.get(k) or 0) for m in maps) for k in keys}


def stooq_prices(ticker):
    text = get_text(STOOQ_CSV.format(ticker=ticker.lower()))
    if "Date,Open" not in text:
        raise RuntimeError("Stooq nie zwrócił CSV")
    df = pd.read_csv(io.StringIO(text))
    if df.empty:
        raise RuntimeError("Stooq zwrócił pustą historię")
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date", "Close"]).sort_values("Date")
    latest = float(df.iloc[-1]["Close"])
    return df, latest


def period_end_lookup(companyfacts):
    # Use Assets instant facts as the reporting-date spine.
    _, units = first_existing(companyfacts, ["Assets"])
    out = {}
    for f in units:
        if f.get("form") not in {"10-Q", "10-K", "10-Q/A", "10-K/A"}:
            continue
        fp, fy, end = f.get("fp"), f.get("fy"), f.get("end")
        if fp not in {"Q1", "Q2", "Q3", "FY"} or fy is None or not end:
            continue
        q = {"Q1":1,"Q2":2,"Q3":3,"FY":4}[fp]
        out[f"{int(fy)}/Q{q}"] = end
    return out


def price_on_or_before(df, date_str):
    if not date_str:
        return None
    d = pd.Timestamp(date_str)
    sub = df[df["Date"] <= d]
    return float(sub.iloc[-1]["Close"]) if not sub.empty else None


def scrape_us(ticker, meta):
    cik = meta["cik"]
    cf = get_json(SEC_FACTS.format(cik=cik), sec=True)

    equity, equity_tag = latest_instant_by_period(cf, [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        "PartnersCapital",
    ])
    goodwill, goodwill_tag = latest_instant_by_period(cf, ["Goodwill"])
    cash, cash_tag = latest_instant_by_period(cf, [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ])
    debt_short_a, debt_short_tag_a = latest_instant_by_period(cf, [
        "LongTermDebtCurrent", "CurrentPortionOfLongTermDebt", "ShortTermBorrowings"
    ])
    debt_short_b, debt_short_tag_b = latest_instant_by_period(cf, ["ShortTermBorrowings"])
    debt_long, debt_long_tag = latest_instant_by_period(cf, [
        "LongTermDebtNoncurrent", "LongTermDebtAndFinanceLeaseObligationsNoncurrent"
    ])
    lease_cur, lease_cur_tag = latest_instant_by_period(cf, [
        "OperatingLeaseLiabilityCurrent", "FinanceLeaseLiabilityCurrent"
    ])
    lease_noncur, lease_noncur_tag = latest_instant_by_period(cf, [
        "OperatingLeaseLiabilityNoncurrent", "FinanceLeaseLiabilityNoncurrent"
    ])
    shares, shares_tag = latest_instant_by_period(cf, ["CommonStocksIncludingAdditionalPaidInCapitalMember", "CommonStockSharesOutstanding"])
    # CommonStockSharesOutstanding is often DEI rather than US-GAAP; override from DEI below when needed.
    if not shares:
        fact = cf.get("facts", {}).get("dei", {}).get("EntityCommonStockSharesOutstanding", {})
        units = fact.get("units", {}).get("shares", [])
        candidates = defaultdict(list)
        for f in units:
            if f.get("form") not in {"10-Q", "10-K", "10-Q/A", "10-K/A"} or f.get("fy") is None or f.get("fp") not in {"Q1","Q2","Q3","FY"}:
                continue
            q={"Q1":1,"Q2":2,"Q3":3,"FY":4}[f["fp"]]
            candidates[f"{int(f['fy'])}/Q{q}"].append(f)
        for p, rows in candidates.items():
            rows.sort(key=lambda x:x.get("filed","")); shares[p]=rows[-1]["val"]
        shares_tag = "dei:EntityCommonStockSharesOutstanding"

    net_income_q, ni_tag = quarterly_flow(cf, [
        "NetIncomeLoss",
        "ProfitLoss",
        "NetIncomeLossAvailableToCommonStockholdersBasic",
    ])
    dividends_q, div_tag = quarterly_flow(cf, [
        "PaymentsOfDividendsCommonStock",
        "PaymentsOfDividends",
        "PaymentsOfOrdinaryDividends",
    ])
    buyback_q, bb_tag = quarterly_flow(cf, [
        "PaymentsForRepurchaseOfCommonStock",
        "PaymentsForRepurchaseOfEquity",
    ])

    ni_ttm = rolling4(net_income_q)
    div_ttm = rolling4({k: abs(v) for k, v in dividends_q.items()})
    bb_ttm = rolling4({k: abs(v) for k, v in buyback_q.items()})

    price_df, latest_price = stooq_prices(ticker)
    period_ends = period_end_lookup(cf)

    short_debt = sum_maps(debt_short_a, debt_short_b)
    lease = sum_maps(lease_cur, lease_noncur)

    periods = sorted(set(equity) & set(ni_ttm) & set(shares), key=lambda p:(int(p.split('/Q')[0]), int(p.split('/Q')[1])))
    history = []
    for p in periods[-12:]:
        if equity.get(p) is None or ni_ttm.get(p) is None or not shares.get(p):
            continue
        history.append({
            "period": p,
            "equity": equity.get(p),
            "goodwill": goodwill.get(p) or 0,
            "net_profit_ttm": ni_ttm.get(p),
            "dividend_ttm": div_ttm.get(p) or 0,
            "buyback_ttm": bb_ttm.get(p) or 0,
            "cash": cash.get(p) or 0,
            "short_debt": short_debt.get(p) or 0,
            "long_debt": debt_long.get(p) or 0,
            "lease": lease.get(p) or 0,
            "shares": shares.get(p),
            "price": price_on_or_before(price_df, period_ends.get(p)),
            "report_end": period_ends.get(p),
        })
    if not history:
        raise RuntimeError("brak kompletnego okresu SEC z equity, TTM i shares")

    current = dict(history[-1])
    current["price"] = latest_price

    warnings = []
    if not goodwill:
        warnings.append("SEC nie raportuje osobnego tagu goodwill; przyjęto 0.")
    if not dividends_q:
        warnings.append("Nie znaleziono standardowego tagu dywidend; przyjęto 0.")
    if not buyback_q:
        warnings.append("Nie znaleziono standardowego tagu buybacku; przyjęto 0.")
    if not debt_long and not short_debt:
        warnings.append("Nie znaleziono standardowych tagów długu; dług netto może wymagać ręcznej korekty.")

    return {
        "ticker": ticker,
        "name": meta["name"],
        "cik": cik,
        "currency": "USD",
        "current": current,
        "history": history[-8:],
        "warnings": warnings,
        "tags": {
            "equity": equity_tag,
            "goodwill": goodwill_tag,
            "cash": cash_tag,
            "short_debt": [debt_short_tag_a, debt_short_tag_b],
            "long_debt": debt_long_tag,
            "lease": [lease_cur_tag, lease_noncur_tag],
            "shares": shares_tag,
            "net_income": ni_tag,
            "dividends": div_tag,
            "buyback": bb_tag,
        },
        "sources": {
            "sec": SEC_FACTS.format(cik=cik),
            "prices": STOOQ_CSV.format(ticker=ticker.lower()),
        },
    }


def main():
    tickers = read_universe()
    sec_map = load_sec_ticker_map()
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "market": "USA",
        "companies": {},
        "errors": {},
    }
    for i, ticker in enumerate(tickers, 1):
        print(f"[{i}/{len(tickers)}] {ticker}", flush=True)
        try:
            if ticker not in sec_map:
                raise RuntimeError("ticker nie znaleziony w oficjalnej mapie SEC")
            payload["companies"][ticker] = scrape_us(ticker, sec_map[ticker])
            print("  OK", flush=True)
        except Exception as e:
            payload["errors"][ticker] = str(e)
            print("  ERROR:", e, flush=True)
        time.sleep(0.18)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved {len(payload['companies'])} USA companies, {len(payload['errors'])} errors -> {OUT}")


if __name__ == "__main__":
    main()
