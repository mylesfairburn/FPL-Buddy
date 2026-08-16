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

Reads are unauthenticated: anyone who knows an FPL id can see that id's draft,
which is inherent to FPL-id-as-identity. Writes are not - see `authorise_write`
and the draft_claim table - because an id being public means a guessable
integer could otherwise overwrite or delete a stranger's squad.
"""

import hashlib
import hmac
import secrets

from db import connect, utcnow

SQUAD_SIZE = 15
VALID_POSITIONS = set(range(1, SQUAD_SIZE + 1))


class DraftError(ValueError):
    """Rejected draft - the message is safe to show the user."""


class DraftForbidden(PermissionError):
    """The caller does not hold the write token for this FPL id."""


def _hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def authorise_write(fpl_id, token):
    """Check a caller may write to this id, and return the token it should keep.

    Trust on first use. An id nobody has claimed is claimed here and now, and
    the freshly minted token comes back for the client to store; from then on
    every write to that id has to present it. An id that IS claimed must match
    or this raises.

    Returned rather than stored-and-forgotten because the client has nowhere
    else to get it: there is no account and no email, so the one moment the
    plaintext exists is the response to the write that created it.
    """
    fpl_id = int(fpl_id)
    token = (token or "").strip()

    with connect() as conn:
        row = conn.execute(
            "SELECT token_hash FROM draft_claim WHERE fpl_id = ?", (fpl_id,)).fetchone()

        if row is None:
            minted = token or secrets.token_urlsafe(32)
            conn.execute(
                "INSERT INTO draft_claim (fpl_id, token_hash, created_at) VALUES (?, ?, ?)",
                (fpl_id, _hash(minted), utcnow()))
            return minted

    # compare_digest rather than ==, so a wrong token can't be recovered a
    # character at a time off the response timing.
    if not token or not hmac.compare_digest(_hash(token), row["token_hash"]):
        raise DraftForbidden(
            "This FPL ID's saved team is claimed by another device. Load it on "
            "the device you saved it from, or clear it there first.")
    return token


def validate_picks(picks):
    """Reject anything that isn't a structurally valid 15-man squad.

    The endpoint writes to a shared table, so it does not trust the client: a
    malformed or oversized payload is a 400, not something that lands in the DB
    and breaks every later read.

    Public because the endpoint runs it BEFORE authorise_write. Order matters:
    claiming first would mean a single junk payload aimed at an unclaimed id
    took ownership of it, and the real manager - with no token and no way to
    delete - would be locked out of their own squad permanently. Nothing here
    touches the database or the caller, so running it for an unauthorised
    caller gives nothing away."""
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
    """Delete the draft AND release the claim on the id.

    Releasing it is what makes the lockout recoverable. A token lives in one
    browser's localStorage and nowhere else - there is no account to reset it
    through - so a cleared browser would otherwise mean nobody could ever write
    to that id again. Delete is itself token-gated at the endpoint, so only the
    holder can hand the id back, and once handed back the next writer claims it
    the same way the first one did.
    """
    with connect() as conn:
        cur = conn.execute("DELETE FROM manager_draft WHERE fpl_id = ?", (int(fpl_id),))
        conn.execute("DELETE FROM draft_claim WHERE fpl_id = ?", (int(fpl_id),))
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
