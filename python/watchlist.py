"""Players a manager is keeping an eye on.

Keyed on `code`, not `element_id`: this is the only thing the app stores that
is meant to outlive a season, and FPL reassigns ids every August.

Unauthenticated in both directions, like the drafts - see the docstring in
drafts.py for that trade. What is stored is a list of publicly listed
footballers plus a note the reader typed, which is why it is acceptable here.
"""

import price_changes
from db import connect, utcnow

# Past a couple of dozen this is a worse copy of the player table - and the
# point at which an unauthenticated write endpoint becomes general storage.
MAX_ENTRIES = 30

# Long enough for "cheap enabler, watch the Newcastle run".
MAX_NOTE_CHARS = 120


class WatchlistError(ValueError):
    """Rejected change - the message is safe to show the user."""


def _clean_note(note):
    """Strip control characters and cap the length.

    Not about injection - the browser escapes on render. About a
    newline-stuffed string making one row a screenful, and about not storing
    unbounded input from an endpoint that takes no credential.
    """
    if note is None:
        return None
    text = "".join(ch if ch.isprintable() else " " for ch in str(note)).strip()
    if not text:
        return None
    if len(text) > MAX_NOTE_CHARS:
        raise WatchlistError(f"Keep the note under {MAX_NOTE_CHARS} characters.")
    return text


def _coerce_code(code):
    try:
        value = int(code)
    except (TypeError, ValueError):
        raise WatchlistError("That is not a player code.")
    if value <= 0:
        raise WatchlistError("That is not a player code.")
    return value


def get(fpl_id, pool=None):
    """The watchlist for `fpl_id`, newest first, joined against the pool.

    Joined at READ time - the row stores only a code, so there is one source of
    truth for who a player is. A code no longer in the pool still comes back
    with `available: False`: dropping it would look like the site had lost the
    entry rather than the player having left.

    Each entry also carries how close the player is to a price change, from the
    same transfer-momentum arithmetic the price-changes page runs - see
    price_changes.for_codes. Asked for the watched codes only, so a thirty-name
    list costs one pass over the snapshot history rather than one per player,
    and absent from the reading means "not measurable", which the table shows
    as a dash rather than as steady.
    """
    by_code = {p["code"]: p for p in (pool or []) if p.get("code") is not None}
    with connect() as conn:
        rows = list(conn.execute(
            """SELECT code, note, added_at FROM watchlist
               WHERE fpl_id = ? ORDER BY added_at DESC, code""", (int(fpl_id),)))

    # Never fatal: a shortlist that cannot say which way a price is drifting is
    # still a shortlist, and the snapshot table is empty for the first two days
    # of any deployment.
    try:
        momentum = price_changes.for_codes([r["code"] for r in rows])
    except Exception as e:
        print(f"couldn't read price momentum for the watchlist: {e}")
        momentum = {}

    out = []
    for r in rows:
        player = by_code.get(int(r["code"]))
        entry = {"code": int(r["code"]), "note": r["note"],
                 "added_at": r["added_at"], "available": player is not None}
        if player:
            entry.update({
                "id": player.get("id"), "web_name": player.get("web_name"),
                "team_name": player.get("team_name"), "team_code": player.get("team_code"),
                "pos": player.get("pos"), "cost": player.get("cost"),
                "predicted": player.get("predicted"), "rating": player.get("rating"),
                "form": player.get("form"), "owned": player.get("owned"),
                "status": player.get("status"), "path": player.get("path"),
                "next_gameweeks": player.get("next_gameweeks") or [],
            })
        drift = momentum.get(int(r["code"]))
        if drift:
            entry.update({
                "price_direction": drift["direction"],
                # Percent of the way to the threshold that has actually moved a
                # price, so the bar means the same thing as the one on the
                # price-changes page.
                "price_progress": drift["progress"],
            })
        out.append(entry)
    return out


def add(fpl_id, code, note=None):
    """Add or update one entry. Idempotent - adding twice updates the note."""
    code = _coerce_code(code)
    note = _clean_note(note)
    with connect() as conn:
        existing = conn.execute(
            "SELECT COUNT(*) FROM watchlist WHERE fpl_id = ?",
            (int(fpl_id),)).fetchone()[0]
        already = conn.execute(
            "SELECT 1 FROM watchlist WHERE fpl_id = ? AND code = ?",
            (int(fpl_id), code)).fetchone() is not None
        # Only for a NEW entry - editing a note must still work at the cap.
        if not already and existing >= MAX_ENTRIES:
            raise WatchlistError(
                f"A watchlist holds {MAX_ENTRIES} players. Remove one first.")
        conn.execute(
            """INSERT INTO watchlist (fpl_id, code, note, added_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(fpl_id, code) DO UPDATE SET note = excluded.note""",
            (int(fpl_id), code, note, utcnow()))
    return {"ok": True, "code": code, "note": note}


def remove(fpl_id, code):
    code = _coerce_code(code)
    with connect() as conn:
        removed = conn.execute(
            "DELETE FROM watchlist WHERE fpl_id = ? AND code = ?",
            (int(fpl_id), code)).rowcount
    return {"ok": True, "removed": removed}


def clear(fpl_id):
    with connect() as conn:
        removed = conn.execute(
            "DELETE FROM watchlist WHERE fpl_id = ?", (int(fpl_id),)).rowcount
    return {"ok": True, "removed": removed}
