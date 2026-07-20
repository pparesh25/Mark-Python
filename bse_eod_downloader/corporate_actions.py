"""Pure parsers for BSE corporate-action ratios."""

from __future__ import annotations

import re

SPLIT_PATTERN = re.compile(r"(\d+\.?\d*)[\/\- a-z\.]+(\d+\.?\d*)")
BONUS_PATTERN = re.compile(r"(\d+) ?: ?(\d+)")


def parse_split_factor(subject: str) -> float | None:
    """Return the price adjustment factor for a BSE split subject.
    
    BSE subjects contain ratios like "10 to 2", "10-2" or "10/2".
    """
    match = SPLIT_PATTERN.search(subject.lower())
    if match is None:
        return None

    try:
        return float(match.group(1)) / float(match.group(2))
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def parse_bonus_factor(subject: str) -> float | None:
    """Return the price adjustment factor for a BSE bonus subject."""
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
