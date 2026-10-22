"""Monthly-bucket normalisation: turn any contract term into a standard monthly recurring amount.

SaaS exports mix monthly subscriptions, annual pre-pays and multi-year ramps in one file. Three
things have to be standardised before anything downstream can compare customers:

1. **The recurring rate.** `mrr_amount` is the monthly recurring amount, whatever the term. An annual
   invoice of $12,000 is $1,000/month; a 13-month invoice is $923.08/month.
2. **The partial first (and last) bucket.** A contract starting on the 17th does not bill a whole
   month. Its recognised revenue is prorated by day, in whole cents, with the leftover cents assigned
   to the buckets with the largest remainders so the buckets sum exactly to what was billed. That is
   the same rule `saas_metrics.inputs.spread` applies, and this module reuses it rather than
   reimplementing it.
3. **The flag.** A row whose service period is not whole calendar months is marked `prorated`, so a
   later report can say how much of the book is mid-cycle instead of hiding it.

Days-per-month is 30.44 (the mean Gregorian month); a term is rounded to the nearest whole month and
never below one.
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta
from decimal import Decimal

import pandas as pd

from saas_metrics.contracts import MONEY

DAYS_PER_MONTH = Decimal("30.44")


def month_end(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def next_month_start(d: date) -> date:
    return date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def month_ends(start: date, end: date) -> list[date]:
    """Every month end the service period touches, in order."""
    out, d = [], start
    while d <= end:
        me = month_end(d)
        out.append(me)
        d = next_month_start(me)
    return out


def days_in_month_bucket(start: date, end: date, month_end_date: date) -> int:
    """Service days inside the month that ends on `month_end_date`."""
    first = month_end_date.replace(day=1)
    lo, hi = max(start, first), min(end, month_end_date)
    return max((hi - lo).days + 1, 0)


def months_covered(start: date, end: date) -> int:
    """The term in whole months, rounded from the day count and floored at 1."""
    days = (end - start).days + 1
    if days <= 0:
        return 0
    return max(int((Decimal(days) / DAYS_PER_MONTH).to_integral_value(rounding="ROUND_HALF_UP")), 1)


def monthly_mrr(total: Decimal | float | str, start: date, end: date) -> Decimal:
    """The monthly recurring amount implied by a total billed over [start, end]."""
    months = months_covered(start, end)
    if months == 0:
        return Decimal(0).quantize(MONEY)
    return (Decimal(str(total)) / Decimal(months)).quantize(MONEY)


def is_partial(start: date, end: date) -> bool:
    """True when the service period is not whole calendar months (a mid-cycle start or end)."""
    return start.day != 1 or end != month_end(end)


def prorate(amount: Decimal | float | str, start: date, end: date, grain: str = "month") -> list[tuple[date, Decimal]]:
    """Spread `amount` across the covered month (or fiscal quarter) ends, in whole cents.

    Returns [(bucket_end, amount)]. The pieces sum exactly to `amount`: each bucket's exact share is
    computed, everything is floored to the cent, and the remaining cents go to the buckets with the
    largest fractional remainders (largest-remainder, the method the billing engine already uses).
    """
    total_cents = int((Decimal(str(amount)) * 100).to_integral_value(rounding="ROUND_HALF_UP"))
    if grain == "quarter":
        ends = _quarter_ends(start, end)
        weights = [_days_in_span(start, end, a, b) for a, b in ends]
        buckets = [b for _, b in ends]
    else:
        buckets = month_ends(start, end)
        weights = [days_in_month_bucket(start, end, b) for b in buckets]
    days = sum(weights)
    if days == 0:
        return []
    sign = -1 if total_cents < 0 else 1
    cents = abs(total_cents)
    base = [cents * w // days for w in weights]
    remainders = sorted(range(len(buckets)), key=lambda i: (-(cents * weights[i] % days), i))
    for i in remainders[: cents - sum(base)]:
        base[i] += 1
    return [(b, (Decimal(sign * c) / 100).quantize(MONEY)) for b, c in zip(buckets, base, strict=True)]


def _days_in_span(start: date, end: date, lo: date, hi: date) -> int:
    a, b = max(start, lo), min(end, hi)
    return max((b - a).days + 1, 0)


def _quarter_ends(start: date, end: date, fy_end_month: int = 12) -> list[tuple[date, date]]:
    """[(quarter_start, quarter_end)] covering the span, on a fiscal calendar ending `fy_end_month`."""
    from saas_metrics.calendar import is_quarter_end, period_end

    qe = period_end(start, "quarter", fy_end_month)
    out: list[tuple[date, date]] = []
    while qe - timedelta(days=90) <= end:
        # quarter start: the day after the previous quarter end, three months back
        m = qe.month - 2
        y = qe.year + ((m - 1) // 12)
        qs = date(y, (m - 1) % 12 + 1, 1)
        out.append((qs, qe))
        if qe >= end:
            break
        m = qe.month + 1
        y = qe.year + ((m - 1) // 12)
        nxt = month_end(date(y, (m - 1) % 12 + 1, 1))
        while not is_quarter_end(nxt, fy_end_month):
            m = nxt.month + 1
            y = nxt.year + ((m - 1) // 12)
            nxt = month_end(date(y, (m - 1) % 12 + 1, 1))
        qe = nxt
    return out


def flag_proration(frame: pd.DataFrame, *, start_col: str = "service_start", end_col: str = "service_end",
                   out_col: str = "prorated") -> pd.DataFrame:
    """Set `prorated` on rows whose service period is not whole calendar months."""
    df = frame.copy()
    flags = []
    for s, e in zip(df[start_col], df[end_col], strict=True):
        flags.append(is_partial(_as_date(s), _as_date(e)))
    df[out_col] = flags
    return df


def _as_date(v) -> date:
    if isinstance(v, date):
        return v
    return pd.Timestamp(v).date()
