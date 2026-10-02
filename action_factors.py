"""Read a corporate action's price factor from NSE's or BSE's purpose text.

The factor MULTIPLIES the prices before the ex-date (the convention of the
market-data lake this method comes from):

    bonus a:b        a new shares for every b held       factor b / (a + b)      ("Bonus 10:1" -> 1/11)
    split X to Y     face value from X to Y              factor Y / X            ("From Rs 10/- ... To Re 1/-" -> 0.1)
    consolidation    face value from X to Y (Y > X)      factor Y / X            (BSE's "Consolidation of Shares": none)
    demerger, spin off, capital reduction                no factor -- the price on the ex-date decides it
    dividend         the amount a share                  no factor (prices are not dividend-adjusted)
    rights, buyback, amalgamation, scheme, other         no factor

A purpose with several parts ("Bonus 1:1 / Face Value Split ...") is one kind
(bonus+split) with the product of the factors. A bonus of something that is not
an equity share (NCRPS, debentures, preference shares) is not a bonus. The
patterns cover the 161 wordings NSE used for splits, bonuses and demergers
from 2002 to 2026 and BSE's own ("Bonus issue 1:1", "Stock  Split From
Rs.10/- to Rs.5/-"), measured 2026-10-01.
"""

from __future__ import annotations

import re

MONEY = r"(?:RS|RE|INR)?\.?\s*(\d+(?:\.\d+)?)"
# Digits must follow "BONUS" at once, so "BONUS NCRPS 4:1" and "BONUS - 1 DEBENTURE" are not bonuses.
BONUS = re.compile(r"BONUS(?:\s+ISSUE|\s+SHARES IN THE RATIO OF)?\s*-?\s*(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)")
SPLIT = re.compile(r"(?:SPLIT|SUB-?\s?DIVISION)\b.*?FROM\s*" + MONEY + r".*?TO\s*(?:FACE VALUE\s*)?" + MONEY)
SPLIT_SHORT = re.compile(r"(?:FV|FACE VALU[ES])\s+SPLIT\s*-?\s*" + MONEY + r".*?TO\s*" + MONEY)
SPLIT_DASH = re.compile(r"SPLIT\s*-\s*" + MONEY + r"\s*/?-?\s*TO\s*" + MONEY)          # "SPLIT-RS.10TORS.2"
SPLIT_PARTS = re.compile(r"SPLIT\b.*?INTO\s+(\d+)\s+PARTS")                          # "Split Us 64 Into 2 Parts"
CONSOLIDATION = re.compile(r"CONSOLIDATION\b.*?" + MONEY + r".*?TO\s*" + MONEY)
DIVIDEND = re.compile(r"DIVIDEND\b[^/]*?(?:RS|RE|INR)\.?\s*(\d+(?:\.\d+)?)")
PRICE_DECIDES = {"demerger": ("DEMERG", "SPIN OFF", "SPIN-OFF", "SPINOFF"),
                 "capital_reduction": ("CAPITAL REDUCTION", "REDUCTION OF CAPITAL")}
OTHER = {"rights": ("RIGHT",), "buyback": ("BUY BACK", "BUYBACK"), "amalgamation": ("AMALGAMATION", "MERGER"),
         "scheme": ("SCHEME",), "resolution": ("RESOLUTION PLAN",)}
# Kinds whose ratio the list gives, and kinds the ex-date's price must decide.
LISTED_KINDS = ("bonus", "split", "consolidation")
PRICE_KINDS = ("demerger", "capital_reduction", "consolidation")


def parse_purpose(text: str) -> tuple[str, float | None, float | None]:
    """(kind, factor, dividend amount) of one purpose text."""
    t = re.sub(r"\s+", " ", (text or "").upper()).strip()
    kinds, factor, amount = [], 1.0, None
    for part in re.split(r"\s*/\s*(?=[A-Z])|\bAND\b", t):
        if (m := BONUS.search(part)) and float(m.group(2)) > 0:
            new, held = float(m.group(1)), float(m.group(2))
            kinds.append("bonus")
            factor *= held / (new + held)
        if "SPLIT" in part or "SUB-DIVISION" in part or "SUB DIVISION" in part:
            m = SPLIT.search(part) or SPLIT_SHORT.search(part) or SPLIT_DASH.search(part)
            if m and float(m.group(1)) > 0 and float(m.group(2)) > 0:
                kinds.append("split")
                factor *= float(m.group(2)) / float(m.group(1))
            elif (m := SPLIT_PARTS.search(part)) and int(m.group(1)) > 0:
                kinds.append("split")
                factor /= int(m.group(1))
        if "CONSOLIDATION" in part:
            m = CONSOLIDATION.search(part)
            ratio = float(m.group(2)) / float(m.group(1)) if m and float(m.group(1)) > 0 else None
            kinds.append("consolidation")
            factor = factor * ratio if ratio else float("nan")
        for kind, words in PRICE_DECIDES.items():
            if any(w in part for w in words) and kind not in kinds:
                kinds.append(kind)
                factor = float("nan")
        if "DIVIDEND" in part and "dividend" not in kinds:
            kinds.append("dividend")
            if m := DIVIDEND.search(part):
                amount = float(m.group(1))
    moving = bool(kinds) and kinds != ["dividend"]
    if not kinds:
        kinds = [next((k for k, words in OTHER.items() if any(w in t for w in words)), "other")]
    adjusting = [k for k in kinds if k != "dividend"]
    kind = "+".join(adjusting) if adjusting else "dividend"
    if not moving or factor != factor:          # nothing that moves the price, or the price must decide (NaN)
        factor = None
    return kind, factor, amount


def moves_price(kind: str) -> bool:
    """True for a kind that changes the price basis: a listed ratio or one the price decides."""
    return any(k in kind for k in LISTED_KINDS + PRICE_KINDS)
