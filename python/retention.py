"""Deleting the things that have stopped being useful.

Three kinds of generated content accumulate on the data volume, at three
different rates, and none of them was ever removed:

    state/social/gwNN.txt           one per gameweek     ~38 a season
    state/social/roundup_gwNN.txt   one per gameweek     ~38 a season
    state/social/players/*.txt      one per NIGHT        ~250 a season
    player_post rows                one per night        ~250 a season

The last two are the reason this module exists. A directory that gains a file
a night is one nobody ever reads twice, and a table that gains a row a night
is one whose "what did I post recently" query gets slower every day for no
benefit.

What is deleted, and what deliberately is not
---------------------------------------------
Only the DRAFTS and the nightly posts. The `gw_report` and `gw_roundup` rows -
the published pages themselves - are never touched by anything in here. Those
are the archive, they are indexed, and they are the fallback if a draft is ever
needed again: a draft can be regenerated from a stored payload, but nothing can
regenerate a payload.

When, and why then
------------------
Each rule fires at a moment that has already happened for another reason,
rather than on an age in days:

    a briefing is frozen      -> the PREVIOUS gameweek's briefing drafts go
    a roundup is saved        -> the previous roundup's drafts go
    a roundup is saved        -> every player post for that gameweek goes

Tying deletion to those events rather than to a timer means the thing being
deleted is always provably superseded. "Delete drafts older than 21 days" would
be a rule about the calendar; "delete last week's drafts once this week's are
final" is a rule about the drafts.

Everything here is safe to call repeatedly and safe to call when there is
nothing to delete, because all three triggers fire on jobs that run every night
for the rest of the season.
"""

import os

import db
import social


def _log(lines, message):
    lines.append(message)
    return lines


def purge_briefing_drafts(current_gameweek, dry_run=False):
    """Remove the drafts for every gameweek BEFORE `current_gameweek`.

    Called when a briefing freezes. Deliberately a sweep of everything older
    rather than just the one previous gameweek: a fortnight where the box was
    down would otherwise leave two orphaned files behind permanently, and there
    is no reason to keep them that doesn't also apply to the one being removed.

    The stored `gw_report` payload is untouched - that is the fallback, and the
    drafts can be rewritten from it with `social.write_drafts` if one is ever
    wanted back.
    """
    removed, log = [], []
    for gw in social.list_drafts():
        if gw >= current_gameweek:
            continue
        path = os.path.join(social.social_dir(), f"gw{gw:02d}.txt")
        if dry_run:
            removed.append(gw)
            continue
        try:
            os.remove(path)
            removed.append(gw)
        except (FileNotFoundError, OSError) as e:
            _log(log, f"  could not remove {path}: {e}")
    if removed:
        _log(log, f"  briefing drafts removed for GW{', GW'.join(str(g) for g in sorted(removed))}"
                  + (" (dry run)" if dry_run else ""))
    return {"removed": sorted(removed), "log": log}


def purge_roundup_drafts(current_gameweek, dry_run=False):
    """Remove the roundup drafts for every gameweek before `current_gameweek`.

    Called when a new roundup is saved. Same reasoning and same guarantee as
    the briefing drafts: the `gw_roundup` payload stays, so the file can be
    rewritten from it.
    """
    removed, log = [], []
    for gw in social.list_roundup_drafts():
        if gw >= current_gameweek:
            continue
        path = os.path.join(social.social_dir(), f"roundup_gw{gw:02d}.txt")
        if dry_run:
            removed.append(gw)
            continue
        try:
            os.remove(path)
            removed.append(gw)
        except (FileNotFoundError, OSError) as e:
            _log(log, f"  could not remove {path}: {e}")
    if removed:
        _log(log, f"  roundup drafts removed for GW{', GW'.join(str(g) for g in sorted(removed))}"
                  + (" (dry run)" if dry_run else ""))
    return {"removed": sorted(removed), "log": log}


def purge_player_posts(gameweek, dry_run=False):
    """Remove every nightly player write-up for `gameweek` - rows and files.

    Called when that gameweek's roundup is saved, which is the moment the posts
    stop being about anything: they argue about who to pick for a round that
    has now been played.

    Rows first, then the files that match them, in that order. If the process
    dies between the two, what is left behind is a file with no row - which the
    admin index displays as "file only" and which the next purge for that
    gameweek cannot find, but which is at least visible. The other order would
    leave a row pointing at a file that no longer exists, which reads as a
    working post until you open it.

    Files without a row are swept too, by date, for exactly that reason.
    """
    log = []
    rows = db.player_posts_for_gameweek(gameweek)
    dates = [r["post_date"] for r in rows]

    if dry_run:
        _log(log, f"  would remove {len(dates)} player write-up(s) for GW{gameweek}"
                  + (f": {', '.join(dates)}" if dates else ""))
        return {"gameweek": gameweek, "rows": len(dates), "files": 0,
                "dates": dates, "log": log}

    deleted_dates = db.delete_player_posts_for_gameweek(gameweek)
    files = social.delete_player_drafts(deleted_dates)
    if deleted_dates:
        _log(log, f"  removed {len(deleted_dates)} player write-up(s) for "
                  f"GW{gameweek} ({files} draft file(s))")
    return {"gameweek": gameweek, "rows": len(deleted_dates), "files": files,
            "dates": deleted_dates, "log": log}


def orphaned_player_drafts(dry_run=False):
    """Remove player draft files that have no ledger row.

    The sweeper for the gap in `purge_player_posts` above, and for files left
    by a run that wrote the draft and then failed before storing the row. Not
    tied to any event - it just runs alongside the others and is almost always
    a no-op.
    """
    log = []
    known = {r["post_date"] for r in db.recent_player_posts(limit=1000)}
    orphans = [day for day in social.list_player_drafts() if day not in known]
    if not orphans:
        return {"removed": 0, "dates": [], "log": log}
    if dry_run:
        _log(log, f"  would remove {len(orphans)} orphaned draft file(s): "
                  f"{', '.join(orphans)}")
        return {"removed": 0, "dates": orphans, "log": log}
    removed = social.delete_player_drafts(orphans)
    _log(log, f"  removed {removed} orphaned player draft file(s)")
    return {"removed": removed, "dates": orphans, "log": log}


def on_briefing_frozen(gameweek, dry_run=False):
    """Everything that should happen when the GW`gameweek` briefing freezes."""
    return purge_briefing_drafts(gameweek, dry_run=dry_run)


def on_roundup_saved(gameweek, dry_run=False):
    """Everything that should happen when the GW`gameweek` roundup is saved.

    Two rules fire together here, and the pairing is the point: the roundup
    existing is simultaneously what makes the previous roundup's drafts
    redundant and what makes that gameweek's player posts historical.
    """
    drafts = purge_roundup_drafts(gameweek, dry_run=dry_run)
    posts = purge_player_posts(gameweek, dry_run=dry_run)
    orphans = orphaned_player_drafts(dry_run=dry_run)
    return {
        "roundup_drafts": drafts["removed"],
        "player_posts": posts["rows"],
        "player_files": posts["files"],
        "orphans": orphans["removed"],
        "log": drafts["log"] + posts["log"] + orphans["log"],
    }


def sweep(dry_run=False):
    """Run every rule against the current state of the world.

    For the `jobs.py purge` command: it works out for itself what should
    already have been deleted, rather than waiting for the next trigger. Useful
    after a stretch where the jobs weren't running, and as the thing to run with
    `--dry-run` when you want to know what is on the volume and why.
    """
    log = []
    briefings = db.gw_report_index()
    roundups = db.gw_roundup_index()

    # The newest briefing is for the upcoming gameweek, so everything before it
    # is superseded by definition.
    if briefings:
        result = purge_briefing_drafts(briefings[0]["gameweek"], dry_run=dry_run)
        log += result["log"]

    if roundups:
        newest = roundups[0]["gameweek"]
        result = purge_roundup_drafts(newest, dry_run=dry_run)
        log += result["log"]
        # Player posts for every gameweek that HAS a roundup - not just the
        # newest. A gameweek whose roundup landed while this wasn't running
        # still has posts arguing about a round that has been played.
        for row in roundups:
            result = purge_player_posts(row["gameweek"], dry_run=dry_run)
            log += result["log"]

    log += orphaned_player_drafts(dry_run=dry_run)["log"]
    if not log:
        log = ["  nothing to remove"]
    return {"log": log, "dry_run": dry_run}
