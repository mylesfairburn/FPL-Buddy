"""The squad a manager is working on in this app, stored server-side.

Keyed on FPL id alone, so the same team follows you between devices. This
replaces the old per-device localStorage draft.

The lifecycle mirrors the real game:

  * Before a deadline you edit the draft freely. It's yours, it's provisional,
    and it lives for the gameweek named in `gameweek`.
  * When that deadline passes the hourly watcher overwrites it with the picks
    you actually made in the official FPL game and re-points it at the next
    gameweek. Whatever you'd been experimenting with here is gone, because from
    that moment your real team is the only thing that counts.

`source` records which of the two a stored row is ('user' or 'official'), so
the UI can tell you whether you're looking at your own draft or your locked-in
team rather than silently blurring them.

Nothing here is authenticated, reads or writes. Anyone who knows an FPL id can
read, overwrite or delete that id's draft.

That was a deliberate reversal. Writes used to be gated by a trust-on-first-use
token, held in one browser's localStorage, so that a guessable integer couldn't
overwrite a stranger's squad. The token had no way to travel: saving on a phone
and then opening a laptop produced "claimed by another device", which is
precisely the cross-device promise this module was written to make in the first
place. The lock was defeating the feature it was attached to, and the thing it
protected is a PREVIEW - `replace_with_official` overwrites it with the
manager's real picks at every deadline anyway. Losing a draft to a stranger
costs one re-pick of a provisional squad; being unable to save from your own
second device cost the feature.

So the trade is stated rather than hidden: an FPL id is public, and a draft
stored against one is public too, in both directions.
"""

from db import connect, utcnow

SQUAD_SIZE = 15
VALID_POSITIONS = set(range(1, SQUAD_SIZE + 1))


class DraftError(ValueError):
    """Rejected draft - the message is safe to show the user."""


def validate_picks(picks):
    """Reject anything that isn't a structurally valid 15-man squad.

    The endpoint writes to a shared table, so it does not trust the client: a
    malformed or oversized payload is a 400, not something that lands in the DB
    and breaks every later read.

    Public because the endpoint runs it before touching the database at all.
    With no authentication left in front of it, this is the only thing standing
    between an arbitrary POST body and a stored row, so it stays strict."""
    if not isinstance(picks, list):
        raise DraftError("picks must be a list")
    if len(picks) != SQUAD_SIZE:
        raise DraftError(f"a squad needs exactly {SQUAD_SIZE} players, got {len(picks)}")

    seen_elements, seen_positions = set(), set()
    cleaned = []
    for p in picks:
        if not isinstance(p, dict):
            raise DraftError("each pick must be an object")
        try:
            element_id = int(p.get("element_id", p.get("id")))
            position = int(p["position"])
        except (TypeError, ValueError, KeyError):
            raise DraftError("each pick needs a numeric element id and position")
        if position not in VALID_POSITIONS:
            raise DraftError(f"position {position} is outside 1-{SQUAD_SIZE}")
        if position in seen_positions:
            raise DraftError(f"two players in position {position}")
        if element_id in seen_elements:
            raise DraftError(f"player {element_id} appears twice")
        seen_positions.add(position)
        seen_elements.add(element_id)
        cost = p.get("cost")
        cleaned.append({
            "element_id": element_id,
            "position": position,
            "is_captain": int(bool(p.get("is_captain"))),
            "is_vice_captain": int(bool(p.get("is_vice_captain"))),
            "cost": float(cost) if cost is not None else None,
        })

    if sum(p["is_captain"] for p in cleaned) > 1:
        raise DraftError("only one captain allowed")
    if sum(p["is_vice_captain"] for p in cleaned) > 1:
        raise DraftError("only one vice-captain allowed")
    return cleaned


def save_draft(fpl_id, picks, gameweek=None, bank=None, source="user"):
    """Create or replace a manager's draft. Idempotent by fpl_id."""
    cleaned = validate_picks(picks)
    with connect() as conn:
        conn.execute("DELETE FROM manager_draft WHERE fpl_id = ?", (int(fpl_id),))
        cur = conn.execute(
            """INSERT INTO manager_draft (fpl_id, gameweek, bank, source, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (int(fpl_id), gameweek, bank, source, utcnow()))
        draft_id = cur.lastrowid
        conn.executemany(
            """INSERT INTO manager_draft_picks
                   (draft_id, element_id, position, is_captain, is_vice_captain, cost)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [(draft_id, p["element_id"], p["position"], p["is_captain"],
              p["is_vice_captain"], p["cost"]) for p in cleaned])
    return {"saved": True, "fpl_id": int(fpl_id), "gameweek": gameweek,
            "source": source, "picks": len(cleaned)}


def get_draft(fpl_id, pool=None):
    """A manager's draft, joined against the live pool for names and shirts.
    Returns None when nothing is stored."""
    with connect() as conn:
        head = conn.execute(
            "SELECT * FROM manager_draft WHERE fpl_id = ?", (int(fpl_id),)).fetchone()
        if head is None:
            return None
        picks = conn.execute(
            "SELECT * FROM manager_draft_picks WHERE draft_id = ? ORDER BY position",
            (head["id"],)).fetchall()

    by_id = {p["id"]: p for p in (pool or [])}
    squad = []
    for row in picks:
        p = by_id.get(row["element_id"], {})
        squad.append({
            "id": row["element_id"],
            "web_name": p.get("web_name") or f"#{row['element_id']}",
            "pos": p.get("pos"),
            "team": p.get("team"),
            "team_code": p.get("team_code"),
            "team_name": p.get("team_name"),
            # Live cost/rating, not the stored ones: a draft is a team you're
            # still building, so it should reflect today's prices.
            "cost": p.get("cost", row["cost"]),
            "rating": p.get("rating"),
            "predicted": p.get("predicted"),
            "form": p.get("form"),
            "status": p.get("status", "a"),
            "news": p.get("news", ""),
            "next_gameweeks": p.get("next_gameweeks") or [],
            "position": row["position"],
            "starting": row["position"] <= 11,
            "is_captain": bool(row["is_captain"]),
            "is_vice_captain": bool(row["is_vice_captain"]),
            "multiplier": 2 if row["is_captain"] else 1,
        })
    return {
        "fpl_id": head["fpl_id"], "gameweek": head["gameweek"],
        "bank": head["bank"], "source": head["source"],
        "updated_at": head["updated_at"], "squad": squad,
    }


def delete_draft(fpl_id):
    """Delete the draft. Idempotent - deleting nothing reports deleted: False
    rather than raising."""
    with connect() as conn:
        cur = conn.execute("DELETE FROM manager_draft WHERE fpl_id = ?", (int(fpl_id),))
    return {"deleted": cur.rowcount > 0}


def replace_with_official(fpl_id, squad, gameweek, bank=None, next_gameweek=None):
    """Deadline handling: overwrite the draft with the manager's REAL picks.

    Called once per manager when a deadline passes. Whatever they were drafting
    is discarded - the official team is now the truth - and the draft is
    re-pointed at the next gameweek so they can start editing again for it.
    """
    picks = [{
        "element_id": p.get("id", p.get("element_id")),
        "position": p.get("position"),
        "is_captain": p.get("is_captain"),
        "is_vice_captain": p.get("is_vice_captain"),
        "cost": p.get("cost"),
    } for p in squad]
    return save_draft(fpl_id, picks,
                      gameweek=next_gameweek if next_gameweek is not None else gameweek,
                      bank=bank, source="official")


def drafts_for(fpl_ids):
    """Which of these managers have a stored draft - lets the watcher skip
    anyone who has never used the app."""
    ids = [int(i) for i in fpl_ids]
    if not ids:
        return set()
    marks = ",".join("?" * len(ids))
    with connect() as conn:
        rows = conn.execute(
            f"SELECT fpl_id FROM manager_draft WHERE fpl_id IN ({marks})", ids).fetchall()
    return {r["fpl_id"] for r in rows}
