"""Parses the candidate text syntax from DESIGN.md section 5:
`SYMBOL MON STRIKE CE|PE`, e.g. `TCS NOV 2500 CE`, with an optional thesis
`@2550 by 2026-11-10`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

_PATTERN = re.compile(
    r"^\s*(?P<symbol>[A-Za-z&\-]+)\s+(?P<mon>[A-Za-z]{3})\s+"
    r"(?P<strike>\d+(?:\.\d+)?)\s+(?P<opt_type>CE|PE)\s*"
    r"(?:@\s*(?P<target>\d+(?:\.\d+)?)\s+by\s+(?P<thesis_date>\d{4}-\d{2}-\d{2}))?\s*$",
    re.IGNORECASE,
)


class CandidateParseError(ValueError):
    pass


@dataclass(slots=True)
class ParsedCandidate:
    symbol: str
    month: str  # 3-letter abbreviation, upper-case
    strike: float
    opt_type: str
    thesis_target: float | None = None
    thesis_date: date | None = None


def parse_candidate(text: str) -> ParsedCandidate:
    m = _PATTERN.match(text)
    if not m:
        raise CandidateParseError(
            f"Could not parse {text!r}. Expected 'SYMBOL MON STRIKE CE|PE' "
            "e.g. 'TCS NOV 2500 CE', optionally '@2550 by 2026-11-10'."
        )
    mon = m.group("mon").upper()
    if mon not in MONTHS:
        raise CandidateParseError(f"Unknown month {m.group('mon')!r} in {text!r}")
    thesis_date = date.fromisoformat(m.group("thesis_date")) if m.group("thesis_date") else None
    return ParsedCandidate(
        symbol=m.group("symbol").upper(),
        month=mon,
        strike=float(m.group("strike")),
        opt_type=m.group("opt_type").upper(),
        thesis_target=float(m.group("target")) if m.group("target") else None,
        thesis_date=thesis_date,
    )


_THESIS_PATTERN = re.compile(
    r"^@\s*(?P<target>\d+(?:\.\d+)?)\s+by\s+(?P<date>\d{4}-\d{2}-\d{2})$", re.IGNORECASE
)


def parse_thesis(text: str) -> tuple[float, date]:
    m = _THESIS_PATTERN.match(text.strip())
    if not m:
        raise CandidateParseError(f"Could not parse thesis {text!r}. Expected '@2550 by 2026-11-10'.")
    return float(m.group("target")), date.fromisoformat(m.group("date"))


def resolve_month_year(month_abbr: str, today: date) -> tuple[int, int]:
    """`MON` resolves to the next listed expiry in that month -- i.e. the
    soonest calendar year in which that month is not already fully in the
    past. The caller still asks the provider for the exact listed expiry
    date within that (year, month)."""
    month = MONTHS[month_abbr.upper()]
    year = today.year
    if month < today.month:
        year += 1
    return year, month
