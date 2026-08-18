"""Club colours, parsed out of static/kits.js so there is only one copy.

The gameweek report is server-rendered - crawlers and language models don't run
JavaScript - so it needs club colours in Python. Duplicating the table here is
how the two quietly disagree the next time a club is promoted.

Only the colours are read, not the shirt geometry: a report card wants a club
chip, not an 18px jersey. Nothing here touches club badges - those are
trademarks, which is why kits.js draws its own shirt in the first place.
"""

import os
import re

# `3:  { name: 'Arsenal', primary: '#EF0107', secondary: '#FFFFFF', ... }`
# Tolerant of both quote styles because one club is written with double quotes
# ("Nott'm Forest" contains an apostrophe).
_ROW = re.compile(
    r"(\d+)\s*:\s*\{\s*name:\s*(['\"])(?P<name>.*?)\2\s*,"
    r"\s*primary:\s*'(?P<primary>#[0-9A-Fa-f]{3,8})'\s*,"
    r"\s*secondary:\s*'(?P<secondary>#[0-9A-Fa-f]{3,8})'")

FALLBACK = {"name": "", "primary": "#9CA3AF", "secondary": "#E5E7EB"}

_CACHE = {}


def _kits_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "static", "kits.js")


def team_colours():
    """team_code -> {name, primary, secondary}.

    Parsed once and cached. An unreadable kits.js gives an empty map rather
    than an exception - chips fall back to neutral grey, which beats taking the
    page down over a stylesheet detail.
    """
    if not _CACHE:
        try:
            with open(_kits_path(), encoding="utf-8") as fh:
                source = fh.read()
        except OSError:
            _CACHE["_"] = {}
            return {}
        # Bounded to the TEAM_KITS literal so the goalkeeper colour table
        # further down the file can't leak in under the same codes.
        start = source.find("const TEAM_KITS")
        end = source.find("};", start)
        block = source[start:end] if start != -1 and end != -1 else ""
        _CACHE["_"] = {
            int(m.group(1)): {"name": m.group("name"),
                              "primary": m.group("primary"),
                              "secondary": m.group("secondary")}
            for m in _ROW.finditer(block)}
    return _CACHE["_"]


def colours_for(team_code):
    """One club's colours, or the neutral fallback for an unknown code.

    A code with no entry is normal - a club promoted since kits.js was last
    edited has one - and a grey chip beats a missing element.
    """
    if team_code is None:
        return FALLBACK
    try:
        return team_colours().get(int(team_code), FALLBACK)
    except (TypeError, ValueError):
        return FALLBACK
