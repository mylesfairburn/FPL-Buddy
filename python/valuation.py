"""How a player or a squad is valued over a run of gameweeks.

Split out of ai_manager so the chip planner can use the same numbers without
importing the manager itself - the manager decides and persists, the chip
planner only scores, and scoring is the part both of them need. Keeping it here
means there is one definition of "what is this player worth over the next four
weeks" rather than one per caller that quietly drift apart.

Everything in this module is a pure function of the rated pool. No database, no
solver state, no I/O.
"""

from squad_optimiser import availability

# How far ahead a transfer decision looks, and how quickly later gameweeks stop
# mattering. 0.75 means gameweek n+2 counts for about half of the next one -
# far enough to avoid chasing a single fixture, short enough that the model's
# accuracy (which decays fast) still means something.
HORIZON = 4
HORIZON_DECAY = 0.75

# Buying a player is a season-long commitment, so the SQUAD is chosen over a
# longer view than a transfer is judged on, with a slower decay. This is what
# separates the AI Manager from Best XI: pick the same 15 on next-gameweek
# points and you get Best XI's team, which is the right answer only if you
# intend to rebuild from scratch every week.
SQUAD_HORIZON = 8
SQUAD_DECAY = 0.9
# How hard the long view pulls against this week's XI when choosing who to own.
SQUAD_WEIGHT = 1.0
# Owned players who must have a fixture in each gameweek of the horizon, so a
# blank week can't leave the squad unable to field eleven.
MIN_COVERAGE = 11


def gameweek_points(player, gameweek):
    """A player's predicted points in one gameweek.

    Sums rather than takes the first match, so a double gameweek - two entries
    under the same event - contributes both fixtures without a special case.
    Returns 0.0 for a gameweek he has no fixture in, which is what makes this
    safe to call across a blank week.
    """
    total = 0.0
    for entry in player.get("next_gameweeks") or []:
        if entry.get("event") == gameweek and entry.get("points") is not None:
            total += float(entry["points"])
    return total


def has_fixture(player, gameweek):
    """Whether a player's club plays at all in `gameweek`."""
    return any(e.get("event") == gameweek
               for e in (player.get("next_gameweeks") or []))


def horizon_value(player, from_gameweek, horizon=HORIZON, decay=HORIZON_DECAY):
    """Decayed sum of a player's predicted points over the next few gameweeks.

    This is the number transfers are judged on. A double gameweek shows up
    naturally through gameweek_points. Risk-adjusted, so a doubtful player isn't
    valued as if he's certain to play.
    """
    total, weight = 0.0, 1.0
    for gw in range(from_gameweek, from_gameweek + horizon):
        total += gameweek_points(player, gw) * weight
        weight *= decay
    return total * availability(player)


def squad_selection_values(pool, gameweek):
    """{player_id: season-long value} for the ownership decision."""
    return {p["id"]: horizon_value(p, gameweek, horizon=SQUAD_HORIZON, decay=SQUAD_DECAY)
            for p in pool}


def coverage_requirement(gameweek, horizon=SQUAD_HORIZON, minimum=MIN_COVERAGE):
    """Minimum players with a fixture, per gameweek across the horizon."""
    return {gw: minimum for gw in range(gameweek, gameweek + horizon)}


def squad_horizon_value(squad, gameweek, horizon=SQUAD_HORIZON, decay=SQUAD_DECAY):
    """What a whole squad is worth over the long view.

    Used to compare a held squad against a rebuild, which is the wildcard
    question. Every player counts, not just the eleven who start: a wildcard
    buys the squad, and the bench is part of what it buys.
    """
    return sum(horizon_value(p, gameweek, horizon=horizon, decay=decay) for p in squad)
