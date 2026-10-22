"""A synthetic, seeded billing export for trying the package (not real data).

It looks like a typical invoice export: its own column names (so you have to map them), monthly invoices,
some customers on annual plans invoiced up front with service dates (so spreading matters), split invoices,
credit notes, churn, expansion and contraction, and customers who come back after a gap.

    saas-metrics sample billing.csv
    saas-metrics report billing.csv --customer "Customer" --date "Invoice Date" --amount "Amount" \
        --service-start "Service Start" --service-end "Service End" --segment "Plan Tier"
"""
from __future__ import annotations

import random
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from saas_metrics.calendar import month_end

SEGMENTS = {  # segment -> (starting monthly revenue range, monthly churn, monthly growth drift, share annual)
    "SMB": ((200, 1500), 0.035, 0.010, 0.10),
    "Mid-Market": ((1500, 8000), 0.015, 0.015, 0.25),
    "Enterprise": ((8000, 40000), 0.006, 0.020, 0.50),
}
CHANNELS = ["Direct", "Partner", "Self-serve"]


def generate(months: int = 24, start: date = date(2024, 1, 1), seed: int = 20261020) -> pd.DataFrame:
    rnd = random.Random(seed)
    rows, customers, next_id = [], [], 1
    month_starts = [date(start.year + (start.month - 1 + i) // 12, (start.month - 1 + i) % 12 + 1, 1) for i in range(months)]
    for mi, m in enumerate(month_starts):
        for _ in range(rnd.randint(8, 16) if mi else 120):
            seg = rnd.choices(list(SEGMENTS), weights=[6, 3, 1])[0]
            lo, hi = SEGMENTS[seg][0]
            customers.append({"id": f"cus_{next_id:05d}", "seg": seg, "ch": rnd.choice(CHANNELS),
                              "mrr": round(rnd.uniform(lo, hi), 2), "active": True, "away": 0,
                              "annual": rnd.random() < SEGMENTS[seg][3], "renew": m})
            next_id += 1
        for c in customers:
            _, churn, drift, _ = SEGMENTS[c["seg"]]
            if c["annual"]:
                if m != c["renew"]:
                    continue                                   # paid up front; nothing billed this month
                end = date(m.year + 1, m.month, 1) - timedelta(days=1)
                if rnd.random() < churn * 6 and mi > 0:          # some annual customers do not renew
                    c["annual"], c["active"] = False, False
                    continue
                c["mrr"] = max(50.0, round(c["mrr"] * (1 + rnd.gauss(drift * 12, 0.15)), 2))
                rows.append((c["id"], m.isoformat(), f"{c['mrr'] * 12:.2f}", m.isoformat(), end.isoformat(), c["seg"], c["ch"]))
                c["renew"] = date(m.year + 1, m.month, 1)
                continue
            if c["active"] and rnd.random() < churn:
                c["active"], c["away"] = False, 0
            elif not c["active"]:
                c["away"] += 1
                if c["away"] >= 2 and rnd.random() < 0.04:
                    c["active"] = True
            if not c["active"]:
                continue
            c["mrr"] = max(50.0, round(c["mrr"] * (1 + rnd.gauss(drift, 0.06)), 2))
            amount = c["mrr"]
            day = m + timedelta(days=rnd.randint(0, 27))
            if rnd.random() < 0.15:                             # invoice split into two lines
                part = round(amount * rnd.uniform(0.3, 0.7), 2)
                rows.append((c["id"], day.isoformat(), f"{part:.2f}", "", "", c["seg"], c["ch"]))
                amount = round(amount - part, 2)
            if rnd.random() < 0.03:                             # credit note on top of a grossed-up invoice
                credit = round(amount * rnd.uniform(0.05, 0.2), 2)
                rows.append((c["id"], day.isoformat(), f"{-credit:.2f}", "", "", c["seg"], c["ch"]))
                amount = round(amount + credit, 2)
            rows.append((c["id"], day.isoformat(), f"{amount:.2f}", "", "", c["seg"], c["ch"]))
    df = pd.DataFrame(rows, columns=["Customer", "Invoice Date", "Amount", "Service Start", "Service End",
                                     "Plan Tier", "Channel"])
    last = month_end(month_starts[-1].year, month_starts[-1].month).isoformat()
    return df[df["Invoice Date"] <= last].reset_index(drop=True)


MAPPING = {"customer_id": "Customer", "period": "Invoice Date", "revenue": "Amount",
           "service_start": "Service Start", "service_end": "Service End", "segment": "Plan Tier",
           "channel": "Channel"}


def write(path: str | Path, **kw) -> Path:
    p = Path(path)
    generate(**kw).to_csv(p, index=False)
    return p
