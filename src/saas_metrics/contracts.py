"""The canonical billing ledger: the contract every adapter must satisfy, and its diagnostics.

A billing export from Stripe, Chargebee, Salesforce or an ERP arrives in that system's own shape.
An adapter's job is to translate it into one canonical row per contract-period:

    customer_id   text      required   who is billed (never blank)
    contract_id   text      required   the agreement the invoice belongs to
    invoice_date  date      required   when the line was billed
    service_start date      required   first day of service it pays for
    service_end   date      required   last day of service it pays for (inclusive)
    mrr_amount    decimal   required   the *monthly recurring* amount, in `currency`
    currency      text      required   ISO-4217, upper case (normalised to USD by the FX hook)
    product_tier  text      optional   plan / SKU / tier, becomes the cohort `segment`

Everything else an adapter learns (the invoice total, the term, whether a step is a contracted ramp
or organic upsell) travels in the optional fields below and never displaces these eight.

Diagnostics are the point. A bad export is reported, not silently coerced:

    error    blocks: the row cannot be trusted (missing customer, service_end before service_start,
             an unparseable date, a currency that is not an ISO-4217 code)
    warning  blocks unless the caller opts in (negative MRR, an implausible duration, a gap)
    info     never blocks: what was normalised (currency case, derived contract ids, duplicates)

`validate_ledger()` runs the checks vectorised over a whole frame and returns a `ValidationReport`;
`BillingLedgerRow` is the same contract as a Pydantic model for one row, for callers who want it at
an API boundary (the MCP server, a notebook) rather than a whole file.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

REQUIRED = ("customer_id", "contract_id", "invoice_date", "service_start", "service_end", "mrr_amount", "currency")
OPTIONAL = ("product_tier", "recognized_amount", "term_months", "expansion_kind", "source_adapter",
            "source_row", "source_currency", "fx_rate", "grace_bridged", "gap_days", "prorated", "channel")
CANONICAL = REQUIRED + OPTIONAL

CURRENCY_RE = re.compile(r"^[A-Za-z]{3}$")
# the codes that actually appear in SaaS billing exports; anything else is flagged, not assumed
KNOWN_CURRENCIES = frozenset(
    "USD EUR GBP JPY CAD AUD CHF SEK NOK DKK PLN CZK HUF RON BGN HRK TRY RUB UAH ILS AED SAR QAR KWD BHD OMR "
    "JOD EGP ZAR NGN KES GHS MAD TND INR PKR BDT LKR NPR CNY HKD TWD KRW SGD MYR THB IDR PHP VND BRL MXN ARS "
    "CLP COP PEN UYU NZD ISK".split()
)
MAX_TERM_DAYS = 3650                     # ten years; beyond this the row is more likely a typo than a contract
MONEY = Decimal("0.01")
ZERO = Decimal("0")


class ContractError(ValueError):
    """Raised when a single row is handed to the Pydantic model and does not satisfy the contract."""


@dataclass(frozen=True)
class Diagnostic:
    """One finding about the ledger. `row` is the 0-based position in the frame before normalisation."""
    level: str          # "error" | "warning" | "info"
    code: str           # stable machine-readable name, e.g. "negative_duration"
    message: str
    row: int | None = None
    field: str | None = None
    value: Any = None

    def __str__(self) -> str:
        where = f" row {self.row}" if self.row is not None else ""
        what = f" [{self.field}={self.value!r}]" if self.field else ""
        return f"{self.level.upper():<7} {self.code}{where}{what}: {self.message}"


@dataclass
class ValidationReport:
    diagnostics: list[Diagnostic] = field(default_factory=list)
    frame: pd.DataFrame = field(default_factory=pd.DataFrame, repr=False)
    notes: list[str] = field(default_factory=list)

    def add(self, level: str, code: str, message: str, row: int | None = None, field_: str | None = None,
            value: Any = None) -> None:
        self.diagnostics.append(Diagnostic(level, code, message, row, field_, value))

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.level == "error"]

    @property
    def warnings(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.level == "warning"]

    @property
    def infos(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.level == "info"]

    @property
    def blocking(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.level in ("error", "warning")]

    @property
    def ok(self) -> bool:
        """True when nothing blocks: no errors, and no warnings either."""
        return not self.blocking

    def codes(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for d in self.diagnostics:
            out[d.code] = out.get(d.code, 0) + 1
        return out

    def summary(self) -> str:
        if not self.diagnostics:
            return "no diagnostics"
        return ", ".join(f"{code} x{n}" for code, n in sorted(self.codes().items()))

    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame([{"level": d.level, "code": d.code, "row": d.row, "field": d.field,
                              "value": d.value, "message": d.message} for d in self.diagnostics],
                            columns=["level", "code", "row", "field", "value", "message"])

    def raise_if_blocking(self, allow_warnings: bool = False) -> None:
        bad = self.errors if allow_warnings else self.blocking
        if bad:
            first = "; ".join(str(d) for d in bad[:5])
            more = f" (+{len(bad) - 5} more)" if len(bad) > 5 else ""
            raise ContractError(f"{len(bad)} blocking diagnostic(s): {first}{more}")


# ---- the row model (Pydantic v2) -----------------------------------------------------------------
class BillingLedgerRow(BaseModel):
    """One canonical ledger row. Validated field by field, with cross-field checks in the validator."""
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True, arbitrary_types_allowed=True)

    customer_id: str = Field(min_length=1, description="who is billed")
    contract_id: str = Field(min_length=1, description="the agreement this line belongs to")
    invoice_date: date
    service_start: date
    service_end: date
    mrr_amount: Decimal = Field(description="monthly recurring amount, in `currency`")
    currency: str = Field(description="ISO-4217 code")
    product_tier: str | None = None
    recognized_amount: Decimal | None = Field(default=None, description="what the invoice pays for over the term")
    term_months: int | None = None
    expansion_kind: str | None = Field(default=None, description="new | renewal | contracted_expansion | upsell")
    source_currency: str | None = None
    fx_rate: Decimal | None = None
    grace_bridged: bool = False
    gap_days: int = 0
    prorated: bool = False
    source_adapter: str | None = None
    source_row: int | None = None

    @field_validator("currency")
    @classmethod
    def _currency(cls, v: str) -> str:
        v = (v or "").strip().upper()
        if not CURRENCY_RE.match(v):
            raise ValueError(f"currency {v!r} is not a three-letter ISO-4217 code")
        return v

    @field_validator("mrr_amount", "recognized_amount")
    @classmethod
    def _money(cls, v: Decimal | None) -> Decimal | None:
        return None if v is None else Decimal(v).quantize(MONEY)

    @model_validator(mode="after")
    def _cross_checks(self) -> BillingLedgerRow:
        if self.service_end < self.service_start:
            raise ValueError(f"service_end {self.service_end} is before service_start {self.service_start}")
        days = (self.service_end - self.service_start).days + 1
        if days > MAX_TERM_DAYS:
            raise ValueError(f"service term of {days} days ({self.service_start}..{self.service_end}) is implausible")
        return self

    @property
    def term_days(self) -> int:
        return (self.service_end - self.service_start).days + 1


def parse_row(row: dict[str, Any]) -> BillingLedgerRow:
    """Validate a single row into the canonical model, raising `ContractError` with a readable message."""
    from pydantic import ValidationError
    try:
        return BillingLedgerRow(**row)
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors())
        raise ContractError(problems) from e


# ---- vectorised validation ------------------------------------------------------------------------
def _to_dates(s: pd.Series) -> pd.Series:
    out = pd.to_datetime(s, errors="coerce", format="mixed")
    return out.dt.date


def _to_money(s: pd.Series) -> pd.Series:
    txt = s.astype("string").str.replace(r"[,$\s]", "", regex=True)
    return pd.to_numeric(txt, errors="coerce")


def _blank(s: pd.Series) -> pd.Series:
    return s.isna() | (s.astype("string").str.strip() == "")


def validate_ledger(raw: pd.DataFrame, *, allow_warnings: bool = False, today: date | None = None,
                    max_examples: int = 5) -> ValidationReport:
    """Check a raw frame against the canonical contract and normalise the columns it can.

    Vectorised, so it runs on a full export. Every finding is a `Diagnostic`; `report.frame` is the
    normalised ledger (dates as `date`, money as `Decimal` to the cent, currency upper-cased, and
    `recognized_amount` defaulted to `mrr_amount x term_months` where it is absent).
    """
    rep = ValidationReport()
    if raw is None or len(raw.columns) == 0:
        rep.add("error", "empty_input", "the ledger has no columns")
        return rep
    df = raw.copy()
    df.columns = [str(c).strip() for c in df.columns]

    missing = [c for c in ("customer_id", "invoice_date", "service_start", "service_end", "mrr_amount", "currency")
               if c not in df.columns]
    if "contract_id" not in df.columns:
        rep.add("info", "derived_contract_id",
                "no contract_id column: derived from customer_id and service_start "
                "(pass one to keep renewals distinct from new contracts)")
    if missing:
        rep.add("error", "missing_columns", f"missing required column(s) {missing}; found {list(df.columns)}")
        rep.frame = df
        return rep

    n = len(df)

    # customer_id
    blank_customer = _blank(df["customer_id"])
    if blank_customer.any():
        row = int(blank_customer[blank_customer].index[0])
        rep.add("error", "missing_customer_id", f"{int(blank_customer.sum())} row(s) have no customer_id; a blank "
                                                "customer cannot be attributed to a cohort", row, "customer_id", None)

    # contract_id (present but blank)
    if "contract_id" in df.columns:
        blank_contract = _blank(df["contract_id"])
        if blank_contract.any():
            rep.add("warning", "missing_contract_id", f"{int(blank_contract.sum())} row(s) have a blank contract_id; "
                                                       "renewals cannot be told apart from new contracts",
                    int(blank_contract[blank_contract].index[0]), "contract_id", None)

    # dates
    dates = {c: _to_dates(df[c]) for c in ("invoice_date", "service_start", "service_end")}
    for col, parsed in dates.items():
        bad = parsed.isna() & ~_blank(df[col])
        if bad.any():
            rep.add("error", "unparseable_date", f"{int(bad.sum())} row(s) have an unparseable {col}",
                    int(bad[bad].index[0]), col, df.loc[bad[bad].index[0], col])
        if _blank(df[col]).any():
            rep.add("error", "missing_date", f"{int(_blank(df[col]).sum())} row(s) have no {col}",
                    int(_blank(df[col])[_blank(df[col])].index[0]), col, None)

    # service_end before service_start: the prompt's "negative contract duration"
    have = dates["service_start"].notna() & dates["service_end"].notna()
    neg = have & (dates["service_end"] < dates["service_start"])
    if neg.any():
        row = int(neg[neg].index[0])
        rep.add("error", "negative_duration",
                f"{int(neg.sum())} row(s) end service before it starts "
                f"(e.g. {dates['service_start'][row]}..{dates['service_end'][row]}); the term cannot be negative",
                row, "service_end", dates["service_end"][row])
    span = (pd.to_datetime(dates["service_end"]) - pd.to_datetime(dates["service_start"])).dt.days + 1
    long_term = have & (span > MAX_TERM_DAYS)
    if long_term.any():
        rep.add("warning", "implausible_duration",
                f"{int(long_term.sum())} contract(s) run longer than {MAX_TERM_DAYS} days "
                f"(longest {int(span[long_term].max())} days)", int(long_term[long_term].index[0]),
                "service_end", dates["service_end"][long_term[long_term].index[0]])
    one_day = have & (span == 1)
    if one_day.any():
        rep.add("info", "single_day_term", f"{int(one_day.sum())} row(s) cover a single day")

    # currency
    cur_raw = df["currency"].astype("string").str.strip()
    blank_cur = cur_raw.isna() | (cur_raw == "")
    if blank_cur.any():
        rep.add("error", "missing_currency", f"{int(blank_cur.sum())} row(s) have no currency",
                int(blank_cur[blank_cur].index[0]), "currency", None)
    cur = cur_raw.str.upper()
    bad_fmt = ~blank_cur & ~cur.str.match(CURRENCY_RE, na=False)
    if bad_fmt.any():
        row = int(bad_fmt[bad_fmt].index[0])
        rep.add("error", "unnormalized_currency",
                f"{int(bad_fmt.sum())} row(s) have a currency that is not a three-letter code; provide an ISO-4217 "
                "code or an explicit mapping (unusual codes go in the FX rate table)", row, "currency", cur_raw[row])
    case = ~blank_cur & cur.str.match(CURRENCY_RE, na=False) & (cur_raw != cur)
    if case.any():
        rep.add("info", "currency_case", f"{int(case.sum())} currency code(s) normalised to upper case")
    unknown = ~blank_cur & cur.str.match(CURRENCY_RE, na=False) & ~cur.isin(KNOWN_CURRENCIES)
    if unknown.any():
        rep.add("warning", "unknown_currency_code",
                f"{int(unknown.sum())} row(s) use {sorted(set(cur[unknown]))}, which is not a known ISO-4217 code",
                int(unknown[unknown].index[0]), "currency", cur[unknown[unknown].index[0]])

    # money
    mrr = _to_money(df["mrr_amount"])
    bad_money = mrr.isna() & ~_blank(df["mrr_amount"])
    if bad_money.any():
        rep.add("error", "invalid_mrr_amount",
                f"{int(bad_money.sum())} row(s) have a non-numeric mrr_amount",
                int(bad_money[bad_money].index[0]), "mrr_amount", df.loc[bad_money[bad_money].index[0], "mrr_amount"])
    if _blank(df["mrr_amount"]).any():
        rep.add("error", "missing_mrr_amount", f"{int(_blank(df['mrr_amount']).sum())} row(s) have no mrr_amount",
                int(_blank(df["mrr_amount"])[_blank(df["mrr_amount"])].index[0]), "mrr_amount", None)
    negative = mrr < 0
    if negative.any():
        row = int(negative[negative].index[0])
        rep.add("warning", "negative_mrr_amount",
                f"{int(negative.sum())} row(s) have negative MRR (credits); they net within the customer-period",
                row, "mrr_amount", float(mrr[row]))
    zero = (mrr == 0) & mrr.notna()
    if zero.any():
        rep.add("info", "zero_mrr", f"{int(zero.sum())} row(s) have zero MRR")

    if rep.errors:
        rep.frame = df
        rep.notes.append("no normalisation applied: fix the errors above first")
        return rep

    # ---- normalise -------------------------------------------------------------------------------
    out = pd.DataFrame(index=df.index)
    out["customer_id"] = df["customer_id"].astype("string").str.strip()
    if "contract_id" in df.columns:
        out["contract_id"] = df["contract_id"].astype("string").str.strip()
    else:
        out["contract_id"] = out["customer_id"] + ":" + pd.to_datetime(dates["service_start"]).dt.strftime("%Y-%m-%d")
    for col, parsed in dates.items():
        out[col] = parsed
    out["currency"] = cur
    out["product_tier"] = (df["product_tier"].astype("string").str.strip() if "product_tier" in df.columns
                           else pd.Series([None] * n, index=df.index, dtype="string"))
    out["mrr_amount"] = [Decimal(f"{v:.2f}") for v in mrr]
    term = (pd.to_datetime(dates["service_end"]) - pd.to_datetime(dates["service_start"])).dt.days + 1
    out["term_months"] = (term / 30.44).round().clip(lower=1).astype("int64")
    if "recognized_amount" in df.columns:
        rec = _to_money(df["recognized_amount"])
        out["recognized_amount"] = [Decimal(f"{v:.2f}") if pd.notna(v) else None for v in rec]
    else:
        out["recognized_amount"] = [None] * n
    # default the invoice total to MRR x months only where the export did not carry one
    defaulted = out["recognized_amount"].isna()
    if defaulted.any():
        rep.add("info", "derived_recognized_amount",
                f"{int(defaulted.sum())} row(s) had no invoice total; recognised amount defaulted to "
                "mrr_amount x term_months")
        out.loc[defaulted, "recognized_amount"] = [
            (m * Decimal(tm)).quantize(MONEY) for m, tm in zip(out.loc[defaulted, "mrr_amount"],
                                                              out.loc[defaulted, "term_months"], strict=True)]
    for col in ("expansion_kind", "source_adapter", "source_currency", "channel", "fx_rate", "source_kind",
                "contract_source", "co_term_end"):
        out[col] = df[col] if col in df.columns else None
    out["source_row"] = df["source_row"] if "source_row" in df.columns else range(n)
    out["gap_days"] = df["gap_days"] if "gap_days" in df.columns else 0
    for col in ("grace_bridged", "prorated", "stitched", "co_terminated"):
        out[col] = df[col].astype("boolean") if col in df.columns else False

    dup = out.duplicated(["customer_id", "contract_id", "service_start"], keep=False)
    if dup.any():
        rep.add("info", "duplicate_contract_period",
                f"{int(dup.sum())} row(s) repeat a customer, contract and service_start (split invoices); "
                "they are kept and sum in the engine")

    if today is not None:
        future = pd.to_datetime(out["service_start"]) > pd.Timestamp(today)
        if future.any():
            rep.add("warning", "future_service", f"{int(future.sum())} row(s) start service after {today}",
                    int(future[future].index[0]), "service_start", out.loc[future[future].index[0], "service_start"])

    rep.frame = out.reset_index(drop=True)
    if allow_warnings:
        rep.notes.append("warnings allowed: rows with warnings were kept")
    return rep


def to_decimal(value: Any) -> Decimal:
    """Parse a money-ish value to a 2dp `Decimal`, raising `InvalidOperation` only on junk."""
    if isinstance(value, Decimal):
        return value.quantize(MONEY)
    return Decimal(str(value)).quantize(MONEY)
