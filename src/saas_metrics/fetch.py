"""Download and cache the SEC files this project uses. Re-runs skip anything already on disk.

Layout under data/raw/:
  companyfacts.json, submissions.json
  filings/<accession>/<primary>.htm      10-Q / 10-K documents
  filings/<accession>/instance.xml       their XBRL instance (dimensional facts)
  8k/<accession>/<exhibit>.htm           earnings press releases (guidance)
"""
from __future__ import annotations

import json
from pathlib import Path

from saas_metrics import config
from saas_metrics.sec_client import fetch

ARCHIVE = "https://www.sec.gov/Archives/edgar/data"


def _get(url: str, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    data = fetch(url)
    if b"Undeclared Automated Tool" in data[:2000]:
        raise RuntimeError(f"SEC refused {url}: check SEC_USER_AGENT")
    dest.write_bytes(data)
    return dest


def _index(accn: str) -> list[dict]:
    folder = accn.replace("-", "")
    dest = config.RAW / "index" / f"{accn}.json"
    _get(f"{ARCHIVE}/{config.CIK}/{folder}/index.json", dest)
    return json.loads(dest.read_text())["directory"]["item"]


def filings(form_types: tuple[str, ...], start: str, end: str, by: str) -> list[dict]:
    """Filings from submissions.json filtered by form and by `by` ('reportDate' or 'filingDate')."""
    sub = json.loads((config.RAW / "submissions.json").read_text())["filings"]["recent"]
    rows = [{k: sub[k][i] for k in ("form", "filingDate", "reportDate", "accessionNumber", "primaryDocument", "items")}
            for i in range(len(sub["form"]))]
    return [r for r in rows if r["form"] in form_types and start <= r[by] <= end]


def run() -> dict:
    raw = config.RAW
    _get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{config.CIK10}.json", raw / "companyfacts.json")
    # submissions changes every filing; always refresh it
    sub = raw / "submissions.json"
    sub.unlink(missing_ok=True)
    _get(f"https://data.sec.gov/submissions/CIK{config.CIK10}.json", sub)

    n_periodic = n_8k = 0
    for f in filings(("10-Q", "10-K"), config.FILINGS_FROM, config.LAST_QUARTER_END, by="reportDate"):
        accn, folder = f["accessionNumber"], f["accessionNumber"].replace("-", "")
        base = f"{ARCHIVE}/{config.CIK}/{folder}"
        _get(f"{base}/{f['primaryDocument']}", raw / "filings" / accn / f["primaryDocument"])
        names = [i["name"] for i in _index(accn)]
        inst = [n for n in names if n.endswith("_htm.xml")]
        if inst:
            _get(f"{base}/{inst[0]}", raw / "filings" / accn / "instance.xml")
        n_periodic += 1

    for f in filings(("8-K",), config.EIGHT_K_FROM, config.EIGHT_K_TO, by="filingDate"):
        if "2.02" not in (f["items"] or ""):
            continue  # Item 2.02 = results of operations; that is where guidance lives
        accn, folder = f["accessionNumber"], f["accessionNumber"].replace("-", "")
        ex = [i["name"] for i in _index(accn)
              if i["name"].lower().endswith((".htm", ".html")) and "ex99" in i["name"].lower().replace("-", "").replace("_", "")]
        for name in ex[:1]:
            _get(f"{ARCHIVE}/{config.CIK}/{folder}/{name}", raw / "8k" / accn / name)
            n_8k += 1
    return {"periodic": n_periodic, "earnings_8k": n_8k}
