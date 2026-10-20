"""Number and text check primitives shared by the narration validators (recap and trade guidance).

Moved out of the recap validator unchanged so the live map's guidance validator does not depend on
the recap (evidence) side."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100, "thousand": 1000,
    "million": 1000000, "dozen": 12, "single": 1, "double": 2, "triple": 3,
    "once": 1, "twice": 2, "thrice": 3,
    "half": Decimal("0.5"), "halves": Decimal("0.5"), "quarter": Decimal("0.25"), "quarters": Decimal("0.25"),
    "third": Decimal(1) / Decimal(3), "thirds": Decimal(2) / Decimal(3),
}
COUNT_WORDS = {"once", "twice", "thrice"}
ORDINALS = {"second", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth"}
WORD_RE = re.compile(r"\b(" + "|".join(sorted(set(NUMBER_WORDS) | ORDINALS, key=len, reverse=True)) + r")\b",
                     re.IGNORECASE)

# Units: what a number can mean, judged by the words right after it.
SUFFIX_UNITS = [
    (re.compile(r"^\s*(%|percent\b|per ?cent\b|pct\b|bps\b|basis points?\b)", re.I), "percent"),
    (re.compile(r"^\s*(x\s+)?ATRs?\b", re.I), "atr_multiple"),
    (re.compile(r"^[\s-]*(minutes?|mins?)\b", re.I), "minutes"),
    (re.compile(r"^\s*(billion|bn)\b", re.I), "billions"),
    (re.compile(r"^\s*times\b", re.I), "count"),
    (re.compile(r"^[\s-]*(points?|pts?|ticks?|cents?|dollars?|seconds?|hours?|days?|sessions?|shares?|"
                r"contracts?|million|thousand|k\b|m\b|x\b|st\b|nd\b|rd\b|th\b)", re.I), "other"),
]

TIME_RE = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)")
NUM_RE = re.compile(r"(?<![\w.])(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)")


def _dec(x) -> Decimal | None:
    try:
        return Decimal(str(x).replace(",", "")).normalize()
    except (InvalidOperation, ValueError):
        return None


def _unit_after(rest: str) -> str:
    for rx, unit in SUFFIX_UNITS:
        if rx.search(rest):
            return unit
    return "plain"


def sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", text.strip()) if s.strip()]


def sanitize(text: str) -> str:
    """Punctuation-only cleanup before validation: dashes become commas/hyphens, markdown stripped."""
    t = text.strip().replace("\u2014", ", ").replace("\u2013", "-")
    t = re.sub(r"\s+,", ",", t)
    t = re.sub(r"[*_`#]+", "", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()
