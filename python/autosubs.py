"""FPL's automatic substitutions, applied to a stored fifteen.

The AI squads are recorded as a starting eleven plus a bench, exactly as a real
manager's are, and until now they were scored as though the eleven always
played. A real entry is not: FPL replaces any starter who recorded no minutes
with the first bench player who did, provided the resulting formation is still
legal. Skipping that step does not make the bot's score conservative, it makes
it wrong - GW1's keeper was an unused substitute, so the AI Manager was scored a
nought in goal while the keeper on its own bench kept a clean sheet.

Two rules do all the work and both are about not guessing:

  * A starter is only replaced once we can SEE he did not play. Zero minutes
    while his match is still on is a player who might come on at half time, so
    the caller passes the set of clubs whose gameweek is over and nobody else
    is touched. Mid-round the pitch therefore shows the eleven that were picked
    and settles into the eleven that counted, rather than shuffling itself
    every time a substitute warms up.

  * A bench player only comes on if he played. FPL's rule exactly, and the
    reason the bench is walked in its stored order rather than by who scored
    most - the order is a decision the manager made before the deadline, and
    reordering it afterwards would be scoring a team nobody picked.

The captain's armband moves to the vice-captain when the captain doesn't play,
which is a separate rule and applies even under a Bench Boost - the one chip
where no substitutions happen at all, because all fifteen are already scoring.
"""

# Legal starting formations: one keeper, and the outfield minimums FPL enforces.
# Maximums are implied - eleven players with these minimums met cannot exceed
# 5 DEF / 5 MID / 3 FWD - so only the floors are stated.
FORMATION_MIN = {"GK": 1, "DEF": 3, "MID": 2, "FWD": 1}


def player_id(player):
    """Squad dicts reach here from two tables and one optimiser, which between
    them call this key `id` and `element_id`. One accessor rather than three
    call sites each remembering to try both."""
    return player.get("id", player.get("element_id"))


def _minutes(player, minutes):
    return int((minutes or {}).get(player_id(player)) or 0)


def _legal_xi(players):
    """Eleven players meeting FPL's positional minimums.

    A missing `pos` counts as nothing, so a squad we can't classify fails this
    and no substitution is made - which is the safe direction: an unmade sub
    leaves the score as it was, an illegal one invents a lineup.
    """
    if len(players) != 11:
        return False
    counts = {}
    for p in players:
        counts[p.get("pos")] = counts.get(p.get("pos"), 0) + 1
    if counts.get("GK", 0) != 1:
        return False
    return all(counts.get(pos, 0) >= need
               for pos, need in FORMATION_MIN.items() if pos != "GK")


def _decided(player, decided_teams):
    """Do we yet know this player's gameweek is over?

    `decided_teams` is None for a settled round - everything is decided - and
    otherwise the clubs whose fixtures have all finished. A player with no club
    recorded can't be placed either way; treated as decided so a squad missing
    that field scores the way it always did rather than silently refusing to
    substitute anybody.
    """
    if decided_teams is None:
        return True
    team = player.get("team")
    return team is None or int(team) in decided_teams


def apply(squad, minutes, decided_teams=None, chip=None):
    """Work out which eleven actually counted, and who wore the armband.

    Returns {"starters": [...], "subs": [{"out":…, "in":…}], "captain_id": …},
    where `starters` is the effective eleven in the order the substitutions
    produced. The squad passed in is never modified.

    A Bench Boost makes the whole question moot - all fifteen score, so there is
    nothing to substitute - but the armband still moves, so it takes the same
    path with substitutions skipped.
    """
    starters = [p for p in squad if p.get("starting")]
    bench = sorted((p for p in squad if not p.get("starting")),
                   key=lambda p: p.get("position") or 99)

    lineup, subs = list(starters), []
    if chip != "bboost":
        used = set()
        for out in starters:
            if _minutes(out, minutes) > 0 or not _decided(out, decided_teams):
                continue
            for cand in bench:
                if player_id(cand) in used or _minutes(cand, minutes) <= 0:
                    continue
                trial = [p for p in lineup if player_id(p) != player_id(out)] + [cand]
                if _legal_xi(trial):
                    lineup = trial
                    used.add(player_id(cand))
                    subs.append({"out": out, "in": cand})
                    break

    captain = next((p for p in squad if p.get("is_captain")), None)
    captain_id = player_id(captain) if captain else None
    if captain is not None and _minutes(captain, minutes) <= 0 \
            and _decided(captain, decided_teams):
        vice = next((p for p in squad if p.get("is_vice_captain")), None)
        if vice is not None and _minutes(vice, minutes) > 0:
            captain_id = player_id(vice)

    return {"starters": lineup, "subs": subs, "captain_id": captain_id}


def multipliers(squad, result, chip=None):
    """{element_id: what this pick was actually multiplied by}.

    FPL's own encoding, so a stored row reads the same way its picks API does:
    0 for a player whose points did not count, 1 for one whose did, 2 or 3 for
    whoever ended up with the armband.

    The reason to persist this rather than recompute it is that a settled
    gameweek is read far more often than it is scored, and recomputing would
    mean fetching a whole round's minutes on every page view. It is also the
    only record of WHY the total is what it is: without it the pitch shows the
    eleven that were picked, the container shows a score built from a different
    eleven, and nothing on the page connects the two.
    """
    counting = {player_id(p) for p in result["starters"]}
    out = {}
    for p in squad:
        pid = player_id(p)
        if chip != "bboost" and pid not in counting:
            out[pid] = 0
        elif pid == result.get("captain_id"):
            out[pid] = 3 if chip == "3xc" else 2
        else:
            out[pid] = 1
    return out


def score(squad, points, result, chip=None):
    """Total a squad from `result`, the dict `apply()` returned.

    Players with no score are skipped rather than counted as nought: an element
    absent from the live feed is one FPL hasn't reported, not one who blanked.
    """
    counting = {player_id(p) for p in result["starters"]}
    total = 0
    for p in squad:
        pts = (points or {}).get(player_id(p))
        if pts is None:
            continue
        if chip != "bboost" and player_id(p) not in counting:
            continue
        multiplier = 1
        if player_id(p) == result.get("captain_id"):
            multiplier = 3 if chip == "3xc" else 2
        total += pts * multiplier
    return total
