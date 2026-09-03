"""Which players are close to a price change, and how sure that is.

FPL moves prices about 01:30 and has never published the threshold, so every
tool that predicts them is reverse-engineering it. This one does that
explicitly and shows its working.

`transfers_in`/`transfers_out` are CUMULATIVE for the season, so one reading
says nothing - a player bought heavily in August looks identical to one being
bought tonight. The signal is the difference between two nights, and nothing
else stores yesterday. Hence `player_price_snapshot`, written nightly at 03:00:
this module is a diff engine and one row in it is worthless.

The threshold is measured rather than assumed. Snapshots record `now_cost`, so
the night a price actually moved is visible in our own history; the transfer
counters then give the momentum that preceded it. Each observed change
contributes one data point and the threshold is their median. Rises and falls
are calibrated separately because FPL is not symmetric about them.

Momentum is net transfers as a SHARE OF OWNERS, since that is what the
threshold scales with.

Two honest limits, both stated on the page:

  * For a player who has not changed price inside the recorded window, momentum
    counts from the first snapshot rather than from his real last change, so it
    is a floor rather than an estimate. It corrects itself the first time he
    moves.
  * Until MIN_OBSERVATIONS changes have been seen in each direction the
    threshold is the community's estimate, not a measurement, and
    `calibration()` says which is in force.
"""

import datetime

from db import connect

# The community's long-standing estimate, used until enough changes have been
# observed here to measure one. The page says which is in force.
FALLBACK_THRESHOLD = 0.055

# Below this, a median swings on one unusual player - which defeats the point
# of measuring rather than guessing.
MIN_OBSERVATIONS = 12

# An arithmetic floor, not a tidiness one. FPL publishes ownership to one
# decimal place, so a player listed at 0.1% is really 0.05-0.15% and the
# divisor carries +/-50% error; at 1.0% that rounding is worth +/-5%.
#
# Set to 0.1% originally, which produced a fall board made almost entirely of
# 0.1%-owned players moving on a couple of hundred transfers - several with net
# transfers going the OTHER WAY from the column they were in.
#
# The cost: a real differential stays off the board until he reaches 1%.
MIN_OWNERSHIP_PCT = 1.0

# Long enough to calibrate against; short enough that 626 rows a night cannot
# grow without bound.
SNAPSHOT_RETENTION_DAYS = 120


def _today():
    return datetime.date.today().isoformat()


def capture(bootstrap=None, day=None):
    """Write tonight's snapshot: one row per player.

    Pulls the bootstrap itself rather than reading whatever the pipeline last
    wrote, so it does not depend on its position in the nightly job.

    A second run on the same date REPLACES the first: both are observations of
    a thing that happens once a night, and keeping both would double-count the
    transfers between them.
    """
    if bootstrap is None:
        from fetch_data import get_bootstrap_data
        bootstrap = get_bootstrap_data() or {}

    elements = bootstrap.get("elements") or []
    if not elements:
        return {"written": 0, "detail": "no player data available"}

    total_players = bootstrap.get("total_players") or 0
    day = day or _today()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    rows = []
    for e in elements:
        code = e.get("code")
        cost = e.get("now_cost")
        if code is None or cost is None:
            continue
        try:
            owned = float(e.get("selected_by_percent") or 0.0)
            rows.append((day, int(code), int(cost),
                         int(e.get("transfers_in") or 0),
                         int(e.get("transfers_out") or 0),
                         owned, int(owned / 100.0 * total_players), now))
        except (TypeError, ValueError):
            continue

    with connect() as conn:
        conn.executemany(
            """INSERT INTO player_price_snapshot
                   (snapshot_date, code, now_cost, transfers_in, transfers_out,
                    selected_by, owners, captured_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(snapshot_date, code) DO UPDATE SET
                   now_cost      = excluded.now_cost,
                   transfers_in  = excluded.transfers_in,
                   transfers_out = excluded.transfers_out,
                   selected_by   = excluded.selected_by,
                   owners        = excluded.owners,
                   captured_at   = excluded.captured_at""", rows)

    return {"written": len(rows), "date": day, "total_players": total_players}


def prune(days=SNAPSHOT_RETENTION_DAYS, today=None):
    """Drop snapshots older than the retention window."""
    today = today or datetime.date.today()
    cutoff = (today - datetime.timedelta(days=int(days))).isoformat()
    with connect() as conn:
        removed = conn.execute(
            "DELETE FROM player_price_snapshot WHERE snapshot_date < ?",
            (cutoff,)).rowcount
    return {"cutoff": cutoff, "removed": removed}


def _history():
    """Every snapshot, grouped by player, oldest first."""
    out = {}
    with connect() as conn:
        for r in conn.execute(
                """SELECT snapshot_date, code, now_cost, transfers_in,
                          transfers_out, selected_by, owners
                   FROM player_price_snapshot
                   ORDER BY code, snapshot_date"""):
            out.setdefault(int(r["code"]), []).append({
                "date": r["snapshot_date"], "cost": int(r["now_cost"]),
                "in": int(r["transfers_in"]), "out": int(r["transfers_out"]),
                "owned": float(r["selected_by"] or 0.0),
                "owners": int(r["owners"] or 0),
            })
    return out


def _owners(snap):
    """Owners on the night of `snap`, floored so it can never be a zero
    divisor. Read from the row rather than recomputed: ownership and the size
    of the game both drift - see the `owners` column in db.py."""
    return max(float(snap.get("owners") or 0), 1000.0)


def observed_changes(history=None):
    """Every price change in our own snapshots, with the momentum behind it.

    One entry per change: net transfers since that player's PREVIOUS change, as
    a share of the owners he had then. This is what calibrates the threshold.
    """
    history = history if history is not None else _history()
    changes = []
    for code, rows in history.items():
        if len(rows) < 2:
            continue
        # From the last change, because that is when FPL's counter resets.
        # The season total would make everyone look overdue by August.
        acc_in = acc_out = 0
        for prev, cur in zip(rows, rows[1:]):
            acc_in += cur["in"] - prev["in"]
            acc_out += cur["out"] - prev["out"]
            delta = cur["cost"] - prev["cost"]
            if delta == 0:
                continue
            owners = _owners(prev)
            changes.append({
                "code": code, "date": cur["date"], "direction": "rise" if delta > 0 else "fall",
                "momentum": (acc_in - acc_out) / owners,
                "owners": owners,
            })
            acc_in = acc_out = 0
    return changes


def _median(values):
    ordered = sorted(values)
    n = len(ordered)
    if not n:
        return None
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def calibration(changes):
    """Measured thresholds, per direction, and how much evidence is behind them.

    A fall's threshold keeps its negative sign, so `momentum / threshold` is
    positive either way and a caller need not remember which way round it goes.
    """
    rises = [c["momentum"] for c in changes if c["direction"] == "rise"]
    falls = [c["momentum"] for c in changes if c["direction"] == "fall"]
    rise_t, fall_t = _median(rises), _median(falls)
    measured = len(rises) >= MIN_OBSERVATIONS and len(falls) >= MIN_OBSERVATIONS
    return {
        "rise_threshold": rise_t if measured else FALLBACK_THRESHOLD,
        "fall_threshold": fall_t if measured else -FALLBACK_THRESHOLD,
        "measured": measured,
        "rise_observations": len(rises),
        "fall_observations": len(falls),
        "min_observations": MIN_OBSERVATIONS,
        "measured_rise": round(rise_t, 4) if rise_t is not None else None,
        "measured_fall": round(fall_t, 4) if fall_t is not None else None,
    }


def _momentum_rows(history, cal, names=None, codes=None):
    """One row per player with usable momentum, unsorted.

    Split out of `board` so the watchlist can ask about a handful of specific
    players without building - and then throwing away - the whole league. Same
    arithmetic either way, which is the point: two implementations of "how
    close is he to a rise" would drift, and one of them would be the one on the
    page that promises a price change.
    """
    wanted = None if codes is None else {int(c) for c in codes}
    rows = []
    for code, snaps in history.items():
        if wanted is not None and code not in wanted:
            continue
        if len(snaps) < 2:
            continue
        # See MIN_OWNERSHIP_PCT: no usable owner count, no usable momentum.
        if snaps[-1]["owned"] < MIN_OWNERSHIP_PCT:
            continue
        # Must accumulate the same way observed_changes does, or progress is
        # measured against a threshold derived from a different quantity.
        acc_in = acc_out = 0
        for prev, cur in zip(snaps, snaps[1:]):
            if cur["cost"] != prev["cost"]:
                acc_in = acc_out = 0
                continue
            acc_in += cur["in"] - prev["in"]
            acc_out += cur["out"] - prev["out"]

        latest, previous = snaps[-1], snaps[-2]
        owners = _owners(latest)
        momentum = (acc_in - acc_out) / owners
        threshold = cal["rise_threshold"] if momentum >= 0 else cal["fall_threshold"]
        progress = (momentum / threshold * 100.0) if threshold else 0.0

        rows.append({
            "code": code,
            # A code identifies the row; three rows all reading "Player" do not.
            "name": (names or {}).get(code, {}).get("name") or f"#{code}",
            "path": (names or {}).get(code, {}).get("path"),
            "team": (names or {}).get(code, {}).get("team"),
            "cost": latest["cost"] / 10.0,
            "owned": round(latest["owned"], 1),
            "direction": "rise" if momentum >= 0 else "fall",
            "net_last_night": (latest["in"] - previous["in"]) - (latest["out"] - previous["out"]),
            "momentum": round(momentum, 4),
            "progress": round(min(progress, 105.0), 1),
        })
    return rows


def board(limit=20, history=None, names=None):
    """Who is closest to a rise and who to a fall, most advanced first.

    `progress` is momentum as a share of whatever threshold is in force, capped
    just over 100%: that a player crossed the line matters, by how far does not.
    """
    history = history if history is not None else _history()
    cal = calibration(observed_changes(history))
    rows = _momentum_rows(history, cal, names)

    risers = sorted((r for r in rows if r["direction"] == "rise"),
                    key=lambda r: -r["progress"])[:limit]
    fallers = sorted((r for r in rows if r["direction"] == "fall"),
                     key=lambda r: -r["progress"])[:limit]
    return {"risers": risers, "fallers": fallers, "calibration": cal}


def for_codes(codes, history=None, names=None):
    """{code: row} for the codes asked about, and nothing else.

    Missing from the result means "no usable reading", which is a real answer
    and not a zero: a player with one snapshot has no momentum to measure and
    one owned by fewer than MIN_OWNERSHIP_PCT of the game has no denominator
    worth dividing by. The caller shows a dash rather than a bar.
    """
    codes = [int(c) for c in codes if c is not None]
    if not codes:
        return {}
    history = history if history is not None else _history()
    cal = calibration(observed_changes(history))
    return {r["code"]: r for r in _momentum_rows(history, cal, names, codes)}


def recent_changes(changes, names=None, limit=30):
    """The observed changes, newest first - the page's track record."""
    out = []
    for c in sorted(changes, key=lambda c: c["date"], reverse=True)[:limit]:
        meta = (names or {}).get(c["code"], {})
        out.append({**c, "momentum": round(c["momentum"], 4),
                    "name": meta.get("name") or f"#{c['code']}",
                    "path": meta.get("path"), "team": meta.get("team")})
    return out


def snapshot_dates():
    with connect() as conn:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT snapshot_date FROM player_price_snapshot "
            "ORDER BY snapshot_date")]


def summary(names=None, limit=20):
    """Everything the page renders. `available` is False below two snapshots,
    because until then every difference would be a difference from nothing."""
    dates = snapshot_dates()
    if len(dates) < 2:
        return {"available": False, "snapshot_days": len(dates),
                "dates": dates, "risers": [], "fallers": [],
                "recent": [], "calibration": calibration([])}

    history = _history()
    data = board(limit=limit, history=history, names=names)
    changes = observed_changes(history)
    return {
        "available": True,
        "snapshot_days": len(dates),
        "dates": dates,
        "first_date": dates[0],
        "last_date": dates[-1],
        "risers": data["risers"],
        "fallers": data["fallers"],
        "calibration": data["calibration"],
        "recent": recent_changes(changes, names),
        "observed_total": len(changes),
    }
