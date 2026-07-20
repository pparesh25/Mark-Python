"""Pure parsers for NSE corporate-action ratios.

Keeping ratio parsing independent from filesystem and NSE client code makes
it easy to test against new subject formats before they reach production.
"""

from __future__ import annotations

import re


SPLIT_PATTERN = re.compile(r"(\d+\.?\d*)[\/\- a-z\.]+(\d+\.?\d*)")
BONUS_PATTERN = re.compile(r"(\d+) ?: ?(\d+)")


def parse_split_factor(subject: str) -> float | None:
    """Return the price adjustment factor for an NSE split subject.

    NSE subjects generally contain the old and new face values, for example
    ``From Rs 5/- Per Share To Rs 2/- Per Share``.  A missing or invalid ratio
    returns ``None`` so the caller can log and skip the action safely.
    """

    match = SPLIT_PATTERN.search(subject.lower())
    if match is None:
        return None

    try:
        return float(match.group(1)) / float(match.group(2))
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def parse_bonus_factor(subject: str) -> float | None:
    """Return the price adjustment factor for an NSE bonus subject."""

    match = BONUS_PATTERN.search(subject.lower())
    if match is None:
        return None

    try:
        denominator = int(match.group(2))
        if denominator == 0:
            return None
        return 1 + int(match.group(1)) / denominator
    except (TypeError, ValueError, ZeroDivisionError):
        return None
