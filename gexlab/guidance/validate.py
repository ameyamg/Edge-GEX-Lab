"""Validator for the guidance narrative. The narrative may use trading words (long, short, entry,
stop, target, bias) but it may only restate the guidance object:

1. Every number (digits or number words) must be a number the object states, as a field value or
   inside one of its strings. A number followed by % must be one of the object's percentages
   (number typing reused from the recap validator: NUM_RE, number words, the unit after a number).
2. Clock times must appear in the object's strings.
3. No ticker other than the object's; only a short list of acronyms.
4. No promises ("guaranteed", "will definitely", "sure thing", "can't lose", "risk free" ...).
5. No em or en dash, 4 to 7 sentences.
Any failure returns reasons and the caller serves the deterministic template instead.
"""
from __future__ import annotations

import re
from decimal import Decimal

from gexlab.textcheck import NUM_RE, NUMBER_WORDS, TIME_RE, WORD_RE, _dec, _unit_after, sanitize, sentences

__all__ = ["validate", "sanitize", "grounded_numbers"]

ACRONYMS = {"ATR", "GEX", "ADV", "OI", "IV", "DTE", "ET", "NY", "AM", "PM", "VWAP"}
PROMISES = re.compile(
    r"\b(guarantee\w*|will definitely|definitely will|sure thing|can'?t lose|cannot lose|can not lose|risk[- ]free|"
    r"no risk|zero risk|always works|never fails|easy money|free money|certain(?:ly)? to|bound to|"
    r"100% (?:sure|certain)|lock(?:ed)? in profits?)\b", re.IGNORECASE)
ORDINAL_OK = {"second"}          # "second target" names a list position, not a number claim
ZERO_DTE = re.compile(r"\b0\s?DTE\b", re.IGNORECASE)


def _walk(o, nums: set, pcts: set, strings: list, key: str = ""):
    if isinstance(o, dict):
        for k, v in o.items():
            _walk(v, nums, pcts, strings, k)
    elif isinstance(o, list):
        for v in o:
            _walk(v, nums, pcts, strings, key)
    elif isinstance(o, bool) or o is None:
        return
    elif isinstance(o, (int, float)):
        for v in {round(float(o), 2), abs(round(float(o), 2)), float(o)}:
            d = _dec(v)
            if d is not None:
                nums.add(d)
                if "pct" in key or key in ("share",) or key.endswith("_pct"):
                    pcts.add(d)
    elif isinstance(o, str):
        strings.append(o)
        masked = TIME_RE.sub(" ", ZERO_DTE.sub(" ", o))
        for m in NUM_RE.finditer(masked):
            d = _dec(m.group(0))
            if d is None:
                continue
            nums.add(d)
            if _unit_after(masked[m.end():m.end() + 12]) == "percent":
                pcts.add(d)


def grounded_numbers(g: dict) -> tuple[set, set, list]:
    nums: set = set()
    pcts: set = set()
    strings: list = []
    _walk(g, nums, pcts, strings)
    for c in g.get("computations") or []:
        d = _dec(c.get("value"))
        if d is not None and "share" in c.get("name", "").lower():
            pcts.add(d)
    return nums, pcts, strings


def validate(text: str, g: dict) -> list[str]:
    if not text or not text.strip():
        return ["empty text"]
    reasons: list[str] = []
    if "\u2014" in text or "\u2013" in text:
        reasons.append("dash character")
    nums, pcts, strings = grounded_numbers(g)
    obj_times = {f"{int(h)}:{m}" for s in strings for h, m in TIME_RE.findall(s)}
    for m in TIME_RE.finditer(text):
        if f"{int(m.group(1))}:{m.group(2)}" not in obj_times:
            reasons.append(f"time not in the guidance: {m.group(0)}")
    masked = TIME_RE.sub(lambda m: " " * len(m.group(0)), text)
    masked = ZERO_DTE.sub(lambda m: " " * len(m.group(0)), masked)
    for m in NUM_RE.finditer(masked):
        v = _dec(m.group(0))
        if v is None:
            reasons.append(f"unreadable number: {m.group(0)}")
            continue
        unit = _unit_after(masked[m.end():m.end() + 30])
        if unit == "percent":
            if v not in pcts:
                reasons.append(f"percentage not in the guidance: {m.group(0)}%")
        elif v not in nums:
            reasons.append(f"number not in the guidance: {m.group(0)}")
    for m in WORD_RE.finditer(masked):
        w = m.group(0).lower()
        if w in ORDINAL_OK or w not in NUMBER_WORDS:
            if w not in ORDINAL_OK:
                reasons.append(f"ordinal claim cannot be verified: {m.group(0)}")
            continue
        if Decimal(NUMBER_WORDS[w]) not in nums:
            reasons.append(f"number word not in the guidance: {m.group(0)}")
    # Acronyms the object itself states (ES and NQ in the index note) are grounded; any other is not.
    in_object = {t.rstrip(".") for x in strings for t in re.findall(r"\b[A-Z][A-Z.]{1,5}\b", x)}
    allowed = ACRONYMS | {g.get("ticker")} | in_object
    for m in re.finditer(r"\b[A-Z][A-Z.]{1,5}\b", masked):
        tok = m.group(0).rstrip(".")
        if tok not in allowed:
            reasons.append(f"ticker or acronym not in the guidance: {tok}")
    for m in PROMISES.finditer(text):
        reasons.append(f"promise: {m.group(0)}")
    n = len(sentences(text))
    if n < 4:
        reasons.append(f"too short: {n} sentences (4 to 7)")
    if n > 7:
        reasons.append(f"too long: {n} sentences (4 to 7)")
    seen, out = set(), []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out
