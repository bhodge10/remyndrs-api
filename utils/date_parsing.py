"""
Date Parsing Utilities
Server-side extraction of explicit calendar dates from user messages,
used to verify AI-produced reminder dates.
"""

import re
from datetime import date
from typing import Optional

MONTH_NAMES = {
    'january': 1, 'jan': 1,
    'february': 2, 'feb': 2,
    'march': 3, 'mar': 3,
    'april': 4, 'apr': 4,
    'may': 5,
    'june': 6, 'jun': 6,
    'july': 7, 'jul': 7,
    'august': 8, 'aug': 8,
    'september': 9, 'sep': 9, 'sept': 9,
    'october': 10, 'oct': 10,
    'november': 11, 'nov': 11,
    'december': 12, 'dec': 12,
}

_MONTH_PATTERN = '|'.join(sorted(MONTH_NAMES.keys(), key=len, reverse=True))
_DATE_REGEX = re.compile(
    rf'\b({_MONTH_PATTERN})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:[,\s]+(\d{{4}}))?',
    re.IGNORECASE,
)
_ANCHOR_REGEX = re.compile(r'\b(on|for|by)\s+$', re.IGNORECASE)


def extract_explicit_date(message: str, reference_date: date) -> Optional[date]:
    """
    Extract an explicit "MONTH DAY [YEAR]" calendar date from a user message.

    Returns None when no date is found, or when multiple candidate dates exist
    without a clear anchor word ("on"/"for"/"by") — we'd rather defer to the AI
    than guess wrong.

    If the parsed month/day is already in the past relative to `reference_date`
    and no explicit year was given, rolls forward one year.
    """
    if not message:
        return None

    matches = list(_DATE_REGEX.finditer(message))
    if not matches:
        return None

    anchored = None
    for m in matches:
        preceding = message[max(0, m.start() - 8):m.start()]
        if _ANCHOR_REGEX.search(preceding):
            anchored = m
            break

    if anchored is not None:
        chosen = anchored
    elif len(matches) == 1:
        chosen = matches[0]
    else:
        return None

    month_str, day_str, year_str = chosen.groups()
    month = MONTH_NAMES[month_str.lower()]
    day = int(day_str)

    if not (1 <= day <= 31):
        return None

    year = int(year_str) if year_str else reference_date.year

    try:
        parsed = date(year, month, day)
    except ValueError:
        return None

    if not year_str and parsed < reference_date:
        try:
            parsed = date(year + 1, month, day)
        except ValueError:
            return None

    return parsed
