"""Read facts (including dimensional ones) from an XBRL instance document.

companyfacts carries only facts without dimensions; revenue by product line lives on
srt:ProductOrServiceAxis, so it has to come from each filing's instance file.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass

NS = {
    "xbrli": "http://www.xbrl.org/2003/instance",
    "xbrldi": "http://xbrl.org/2006/xbrldi",
}


@dataclass
class Fact:
    concept: str          # prefixed, e.g. "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
    start: str | None
    end: str              # period end, or the instant
    dims: tuple[tuple[str, str], ...]  # sorted (axis, member) pairs; () for undimensioned facts
    value: float
    unit: str
    decimals: str


def _prefixes(path: str) -> dict[str, str]:
    """namespace URI -> prefix, read from the document's own declarations."""
    out = {}
    for _event, (prefix, uri) in ET.iterparse(path, events=("start-ns",)):
        out.setdefault(uri, prefix)
    return out


def parse(path: str, concepts: set[str] | None = None) -> list[Fact]:
    prefix_of = _prefixes(path)
    root = ET.parse(path).getroot()

    contexts: dict[str, tuple[str | None, str, tuple]] = {}
    for c in root.findall("xbrli:context", NS):
        per = c.find("xbrli:period", NS)
        inst = per.findtext("xbrli:instant", namespaces=NS)
        start = per.findtext("xbrli:startDate", namespaces=NS)
        end = inst or per.findtext("xbrli:endDate", namespaces=NS)
        explicit = [(m.get("dimension"), (m.text or "").strip())
                    for m in c.findall(".//xbrldi:explicitMember", NS)]
        # typed members carry the value in a child element, e.g. the 2026 taxonomy's
        # StatementOfIncomeLocationBalanceAxis -> <us-gaap:QName.domain>us-gaap:ResearchAndDevelopmentExpense
        typed = [(m.get("dimension"), "".join(m.itertext()).strip())
                 for m in c.findall(".//xbrldi:typedMember", NS)]
        dims = tuple(sorted(explicit + typed))
        contexts[c.get("id")] = (None if inst else start, end, dims)

    facts = []
    for el in root:
        ctx = el.get("contextRef")
        if ctx is None or el.text is None:
            continue
        uri, _, local = el.tag[1:].partition("}")
        concept = f"{prefix_of.get(uri, uri)}:{local}"
        if concepts and concept not in concepts:
            continue
        try:
            val = float(el.text.strip())
        except ValueError:
            continue  # text blocks and non-numeric facts
        start, end, dims = contexts[ctx]
        facts.append(Fact(concept, start, end, dims, val, el.get("unitRef", ""), el.get("decimals", "")))
    return facts
