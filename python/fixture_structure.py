"""Season-wide fixture shape: which gameweeks double, which blank, and when.

Read from `fixtures.csv` rather than the player pool's `next_gameweeks`, which
is only built eight gameweeks deep - in GW1 that leaves the chip planner unable
to see GW19, let alone that it is a deadline. Two consequences:

  * A fixture with no `event` is one FPL has not scheduled yet, and is excluded
    rather than guessed at. An unscheduled fixture is exactly how a double
    gameweek is born, so assigning it now would invent doubles that don't exist.

  * The structure is only as good as today's fixture list. Doubles and blanks
    appear mid-season as postponements are rearranged - in 2025-26 every one
    landed after GW26 - so the planner re-reads this each gameweek rather than
    deciding in August.

`outlook_from_pool` keeps the pool-derived version as a fallback, so an
unreadable fixtures file degrades rather than leaving the planner with nothing.
"""

import os

import pandas as pd

import seasons

# A team playing twice is a double gameweek only if enough of them do it that
# the fixture list is meaningfully different: two teams doubling is a quirk,
# six is a chip week. Likewise a blank.
DOUBLE_MIN_TEAMS = 4
BLANK_MAX_TEAMS = 14

# The gameweek after which FPL returns every chip. Chips are scheduled against
# the end of the half they are in, not the end of the season.
CHIP_HALVES = ((1, 19), (20, 38))
LAST_GAMEWEEK = CHIP_HALVES[-1][1]


def chip_half(gameweek):
    """The (first, last) gameweek of the chip half `gameweek` falls in."""
    gw = int(gameweek)
    for start, end in CHIP_HALVES:
        if start <= gw <= end:
            return (start, end)
    # Off the end of the season - treat it as the final half so callers get a
    # sane window rather than an exception during a fixture-list oddity.
    return CHIP_HALVES[-1]


def half_deadline(gameweek):
    """Last gameweek a chip held in `gameweek` can still be played."""
    return chip_half(gameweek)[1]


def load_fixtures(season=None):
    """The season's fixture list, or None if it isn't on disk."""
    path = seasons.fixtures_path(season)
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return None
    try:
        df = pd.read_csv(path)
    except Exception:
        return None
    if not {"event", "team_h", "team_a"}.issubset(df.columns):
        return None
    return df


def _appearances(fixtures):
    """{gameweek: {team: fixtures played}} from a fixture frame."""
    scheduled = fixtures.dropna(subset=["event"])
    counts = {}
    for event, team_h, team_a in zip(scheduled["event"], scheduled["team_h"],
                                     scheduled["team_a"]):
        gw = counts.setdefault(int(event), {})
        for team in (int(team_h), int(team_a)):
            gw[team] = gw.get(team, 0) + 1
    return counts


def _describe(gameweek, appearances, all_teams):
    playing = set(appearances)
    doubling = {t for t, n in appearances.items() if n >= 2}
    return {
        "gameweek": gameweek,
        "teams_playing": len(playing),
        "teams_doubling": len(doubling),
        "doubling_teams": sorted(doubling),
        "blanking_teams": sorted(all_teams - playing) if all_teams else [],
        "is_double": len(doubling) >= DOUBLE_MIN_TEAMS,
        # `0 <` guards a gameweek with no fixtures at all, which is a gap in the
        # data rather than a blank week everyone should chip around.
        "is_blank": 0 < len(playing) <= BLANK_MAX_TEAMS,
    }


def season_outlook(season=None, gameweeks=None, fixtures=None):
    """Per-gameweek fixture shape for the whole season.

    `gameweeks` restricts the output; omit it to get every gameweek the fixture
    list knows about. Returns None when there is no fixture list to read, so the
    caller can fall back rather than mistake an empty dict for "no doubles".
    """
    if fixtures is None:
        fixtures = load_fixtures(season)
    if fixtures is None:
        return None

    counts = _appearances(fixtures)
    if not counts:
        return None
    all_teams = {t for gw in counts.values() for t in gw}

    wanted = sorted(counts) if gameweeks is None else sorted(set(gameweeks))
    return {gw: _describe(gw, counts.get(gw, {}), all_teams) for gw in wanted}


def outlook_from_pool(pool, gameweeks):
    """The original pool-derived shape, kept as a fallback.

    Agrees with the numbers the optimiser is using by construction, because it
    reads the same `next_gameweeks` lists - but it only sees as far as those
    lists go, and a team with no rated players would vanish from it entirely.
    """
    outlook = {}
    for gw in gameweeks:
        teams_playing, teams_doubling = set(), set()
        for p in pool:
            team = p.get("team")
            if team is None:
                continue
            n = sum(1 for e in (p.get("next_gameweeks") or []) if e.get("event") == gw)
            if n >= 1:
                teams_playing.add(team)
            if n >= 2:
                teams_doubling.add(team)
        outlook[gw] = {
            "gameweek": gw,
            "teams_playing": len(teams_playing),
            "teams_doubling": len(teams_doubling),
            "doubling_teams": sorted(teams_doubling),
            "blanking_teams": [],
            "is_double": len(teams_doubling) >= DOUBLE_MIN_TEAMS,
            "is_blank": 0 < len(teams_playing) <= BLANK_MAX_TEAMS,
        }
    return outlook


def combined_outlook(pool, gameweeks=None, season=None, through=LAST_GAMEWEEK):
    """Fixture shape the planner should use: the fixture list, falling back to
    the player pool only where the fixture list can't be read.

    The two are equally fresh - `rating_model` builds the pool from
    `fetch_data.get_fixtures()`, which is the same call that writes
    fixtures.csv - so there is nothing to gain from preferring the pool, and
    two things to lose. It counts only teams that have a rated player, and it
    is built from fixtures where `finished == False`, so a gameweek already
    part-played reads as a blank. The fixture list has neither problem, and it
    is the only one of the two that reaches the GW19 deadline.
    """
    pool_gws = {e.get("event") for p in (pool or [])
                for e in (p.get("next_gameweeks") or []) if e.get("event")}
    if gameweeks is None:
        first = min(pool_gws) if pool_gws else 1
        gameweeks = range(int(first), int(through) + 1)
    wanted = sorted({int(g) for g in gameweeks})

    from_fixtures = season_outlook(season, gameweeks=wanted)
    if from_fixtures is not None:
        return from_fixtures
    return outlook_from_pool(pool or [], wanted)


def context(entry):
    """'double' | 'blank' | 'normal' - the key chip priors are bucketed by."""
    if not entry:
        return "normal"
    if entry.get("is_double"):
        return "double"
    if entry.get("is_blank"):
        return "blank"
    return "normal"
