"""Per-season data layout, and the rules for which seasons feed which job.

Everything under data/ is addressed through here rather than by hardcoded path,
so adding a season is creating one directory - no code edit, no renaming a
"previous_season_*" file into place each August.

    data/
      seasons/
        2025-26/
          gameweek_stats.csv     per-gameweek player rows (the training target)
          players.csv            season's element list
          teams.csv              season's teams
          fixtures.csv           season's fixtures
        2026-27/
          ... same, plus:
          bootstrap.json         full bootstrap-static payload (offline fallback)
          element_summaries/     per-player /element-summary/ cache
      reference/                 season-independent (positions, ClubElo)
      models/                    trained model bundles

A completed season's directory is READ-ONLY to the app. Only the current
season's files are ever written, so a live fetch cannot clobber last season's
training data.
"""

import os
import re
from datetime import date

# Paths are relative to python/, which is the working directory both locally
# (uvicorn) and in the container (Dockerfile WORKDIR).
DATA_ROOT = os.environ.get("FPL_DATA_ROOT", "../data")
SEASONS_DIR = os.path.join(DATA_ROOT, "seasons")
REFERENCE_DIR = os.path.join(DATA_ROOT, "reference")
MODELS_DIR = os.path.join(DATA_ROOT, "models")

# First season with per-gameweek data in the modern FPL shape (expected goals,
# expected goal involvements, BPS). Anything earlier lacks the underlying-stat
# columns the model is built on, so it can't be trained against.
FIRST_TRAINING_SEASON = "2025-26"

SEASON_RE = re.compile(r"^(\d{4})-(\d{2})$")

# A new FPL season's data starts appearing well before the first deadline, and
# the old season is finished by late May. July is a safe changeover: the new
# season's bootstrap is up, and nothing is still being written for the old one.
SEASON_ROLLOVER_MONTH = 7


def season_key(start_year):
    """2026 -> '2026-27'."""
    return f"{start_year}-{str(start_year + 1)[2:]}"


def season_start_year(season):
    m = SEASON_RE.match(season)
    if not m:
        raise ValueError(f"Not a season key: {season!r} (expected e.g. '2026-27')")
    return int(m.group(1))


def current_season(today=None):
    """The season we're currently collecting data for."""
    today = today or date.today()
    start = today.year if today.month >= SEASON_ROLLOVER_MONTH else today.year - 1
    return season_key(start)


def season_dir(season=None, create=False):
    season = season or current_season()
    path = os.path.join(SEASONS_DIR, season)
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def season_file(name, season=None, create_dir=False):
    return os.path.join(season_dir(season, create=create_dir), name)


# ---- Named files ----------------------------------------------------------

def players_path(season=None, create_dir=False):
    return season_file("players.csv", season, create_dir)


def teams_path(season=None, create_dir=False):
    return season_file("teams.csv", season, create_dir)


def fixtures_path(season=None, create_dir=False):
    return season_file("fixtures.csv", season, create_dir)


def gameweek_stats_path(season=None, create_dir=False):
    """Per-gameweek player rows. Last season's is the training set; the current
    season's is what 'inseason' form features are built from."""
    return season_file("gameweek_stats.csv", season, create_dir)


def bootstrap_path(season=None, create_dir=False):
    """Full bootstrap-static JSON. Cached whole (not just the elements table)
    so an offline fallback can hand back the same dict shape the live call
    returns, instead of a DataFrame that callers then can't subscript."""
    return season_file("bootstrap.json", season, create_dir)


def element_summaries_dir(season=None, create=False):
    path = season_file("element_summaries", season, create_dir=create)
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def positions_path():
    return os.path.join(REFERENCE_DIR, "positions.csv")


def clubelo_path():
    return os.path.join(REFERENCE_DIR, "clubelo_ratings.csv")


# ---- Season discovery -----------------------------------------------------

def available_seasons():
    """Every season directory present, oldest first."""
    if not os.path.isdir(SEASONS_DIR):
        return []
    return sorted(d for d in os.listdir(SEASONS_DIR)
                  if SEASON_RE.match(d) and os.path.isdir(os.path.join(SEASONS_DIR, d)))


def previous_season(season=None):
    """The season before this one, if its directory exists."""
    season = season or current_season()
    prev = season_key(season_start_year(season) - 1)
    return prev if prev in available_seasons() else None


def training_seasons(min_season=FIRST_TRAINING_SEASON, include_current=True):
    """Seasons whose gameweek_stats.csv exists and is worth training on.

    Grows by itself: once 2026-27 finishes with a full gameweek_stats.csv it
    joins 2025-26 automatically, and the model gets two seasons of evidence for
    how underlying stats convert to points. `include_current` is on because a
    part-played season is still real signal - the trainer weights by how much of
    it exists rather than discarding it.
    """
    floor_year = season_start_year(min_season)
    cur = current_season()
    out = []
    for s in available_seasons():
        if season_start_year(s) < floor_year:
            continue
        if not include_current and s == cur:
            continue
        path = gameweek_stats_path(s)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            out.append(s)
    return out


# Where the image's baked-in copy lives, regardless of where FPL_DATA_ROOT
# points. Used to seed a fresh volume on first boot.
IMAGE_DATA_ROOT = "../data"


def ensure_seeded(source_root=IMAGE_DATA_ROOT):
    """Copy immutable data into the writable root if it isn't there yet.

    FPL_DATA_ROOT points at a mounted volume in production so the nightly
    gameweek_stats pull survives a redeploy - but a fresh volume starts empty,
    and completed-season training data, trained models and reference tables all
    ship inside the image. This copies anything missing across on startup.

    Per-file and missing-only: it will never overwrite something the volume
    already has, so a nightly-updated file can't be reverted to the image's
    stale copy on the next restart. No-op when FPL_DATA_ROOT isn't set (local
    runs read the repo directly).
    """
    src = os.path.abspath(source_root)
    dst = os.path.abspath(DATA_ROOT)
    if src == dst or not os.path.isdir(src):
        return {"seeded": False, "reason": "data root is the image copy"}

    import shutil
    copied = []
    for dirpath, _dirnames, filenames in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        target_dir = os.path.join(dst, rel) if rel != "." else dst
        for name in filenames:
            target = os.path.join(target_dir, name)
            if os.path.exists(target):
                continue
            os.makedirs(target_dir, exist_ok=True)
            shutil.copy2(os.path.join(dirpath, name), target)
            copied.append(os.path.relpath(target, dst))
    if copied:
        print(f"Seeded {len(copied)} file(s) into {dst}")
    return {"seeded": bool(copied), "count": len(copied), "root": dst}


def describe():
    """Layout summary - surfaced by /api/ai/status so a mis-mounted volume or a
    missing season shows up as data rather than as a confusing empty table."""
    cur = current_season()
    return {
        "data_root": os.path.abspath(DATA_ROOT),
        "current_season": cur,
        "available_seasons": available_seasons(),
        "training_seasons": training_seasons(),
        "current_gameweek_stats": os.path.exists(gameweek_stats_path(cur)),
        "element_summaries": (len(os.listdir(element_summaries_dir(cur)))
                              if os.path.isdir(element_summaries_dir(cur)) else 0),
    }
