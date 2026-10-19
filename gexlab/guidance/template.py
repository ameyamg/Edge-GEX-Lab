"""Deterministic narrative of a guidance object: 4 to 7 present-tense sentences, every number copied
from the object. With fewer than two scenarios the setup notes (low influence, no setup at a wall)
fill the scenario slots. This is the fallback whenever the AI narrative is unavailable or rejected."""
from __future__ import annotations

from .build import fp


def _scenario_sentence(s: dict) -> str:
    if s["bias"] == "flat" or not s["entry"]:
        return f"{s['name']}: if {_lower(s['trigger'])}, the bias is flat."
    e = s["entry"]
    entry = fp(e["low"]) if e["low"] == e["high"] else f"{fp(e['low'])} to {fp(e['high'])}"
    tg = " then ".join(fp(t["price"]) for t in s["targets"])
    rr = f", reward to risk {fp(s['rr'])} to 1" if s.get("rr") is not None else ""
    return (f"{s['name']}: if {_lower(s['trigger'])}, the idea is {s['bias']} from {entry} with a stop at "
            f"{fp(s['stop']['price'])} and targets at {tg}{rr}.")


def _lower(x: str) -> str:
    return x[:1].lower() + x[1:]


def render(g: dict) -> str:
    env = g["environment"]
    out = [g["regime"]["summary"].rstrip(".") + f", which reads as {env['label'].lower().replace(' / ', ' and ')}.",
           g["regime"]["size_text"]]
    lv = [x for x in g["levels"] if x["key"] in ("call_wall", "put_wall", "gamma_flip")
          and not x.get("frozen")]
    if lv:
        parts = [f"the {x['label']} at {fp(x['price'])} ({fp(abs(x['distance']))} {x['side']})" for x in lv]
        out.append("Key levels are " + ", ".join(parts) + ".")
    lines = [_scenario_sentence(s) for s in g["scenarios"][:2]]
    lines += list(g.get("setup_notes") or [])[:max(0, 2 - len(lines))]
    out += lines
    if g["regime_change"]:
        out.append("What changes this: " + _lower(g["regime_change"][0]))
    if len(out) < 7:
        behind = any("one session behind" in c for c in g["confidence"])
        tail = ((["open interest is one session behind"] if behind else [])
                + ([("these ideas are untested" if g["scenarios"] else "this reading is untested")]
                   if not g["status"]["tested"] else []))
        if tail:
            joined = " and ".join(tail)
            out.append(joined[:1].upper() + joined[1:] + ".")
    text = " ".join(x if x.endswith(".") else x + "." for x in out[:7])
    return text.replace("..", ".")
