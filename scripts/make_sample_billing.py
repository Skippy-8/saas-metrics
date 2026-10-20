"""Write tests/fixtures/sample_billing.csv.gz: a SYNTHETIC monthly billing tape.

Not real data. Seeded, so the file is reproducible. It exercises every path of the cohort
engine: three segments and channels, new logos every month, expansion and contraction, churn,
customers who return after a gap (reactivation), invoices split into several lines, and credit
notes (negative lines that leave the customer-month positive).

    .venv/bin/python scripts/make_sample_billing.py
"""
from __future__ import annotations

import csv
import gzip
import random
from datetime import date
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "sample_billing.csv.gz"   # regression fixture
SEED = 20261016
MONTHS = [date(2024 + (m // 12), m % 12 + 1, 1) for m in range(24)]          # Jan 2024 .. Dec 2025
SEGMENTS = {  # segment -> (starting monthly revenue range, monthly churn, monthly growth drift)
    "SMB": ((200, 1500), 0.035, 0.010),
    "Mid-Market": ((1500, 8000), 0.015, 0.015),
    "Enterprise": ((8000, 40000), 0.006, 0.020),
}
CHANNELS = ["Direct", "Partner", "Self-serve"]


def main() -> None:
    rnd = random.Random(SEED)
    rows = []
    customers = []
    next_id = 1
    for mi, month in enumerate(MONTHS):
        # new logos this month
        for _ in range(rnd.randint(8, 16) if mi else 120):
            seg = rnd.choices(list(SEGMENTS), weights=[6, 3, 1])[0]
            lo, hi = SEGMENTS[seg][0]
            customers.append({"id": f"C{next_id:05d}", "seg": seg, "ch": rnd.choice(CHANNELS),
                              "mrr": round(rnd.uniform(lo, hi), 2), "active": True, "away": 0})
            next_id += 1
        for c in customers:
            _, churn, drift = SEGMENTS[c["seg"]]
            if c["active"] and c["id"] != f"C{next_id - 1:05d}" and rnd.random() < churn:
                c["active"], c["away"] = False, 0
            elif not c["active"]:
                c["away"] += 1
                if c["away"] >= 2 and rnd.random() < 0.04:          # some churned customers come back
                    c["active"] = True
            if not c["active"]:
                continue
            c["mrr"] = max(50.0, round(c["mrr"] * (1 + rnd.gauss(drift, 0.06)), 2))
            amount = c["mrr"]
            if rnd.random() < 0.15:                                  # invoice split into two lines
                part = round(amount * rnd.uniform(0.3, 0.7), 2)
                rows.append((c["id"], month.isoformat(), f"{part:.2f}", c["seg"], c["ch"]))
                amount = round(amount - part, 2)
            if rnd.random() < 0.03:                                  # credit note on top of the invoice
                credit = round(amount * rnd.uniform(0.05, 0.2), 2)
                rows.append((c["id"], month.isoformat(), f"{-credit:.2f}", c["seg"], c["ch"]))
                amount = round(amount + credit, 2)                   # invoice gross; net stays the mrr
            rows.append((c["id"], month.isoformat(), f"{amount:.2f}", c["seg"], c["ch"]))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT, "wt", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["customer_id", "period", "revenue", "segment", "channel"])
        w.writerows(rows)
    print(f"{OUT.name}: {len(rows)} rows, {next_id - 1} customers, {len(MONTHS)} months (synthetic)")


if __name__ == "__main__":
    main()
