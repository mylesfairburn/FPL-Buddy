import json
import os
import time

import pandas as pd
import requests

import seasons

BASE = "https://fantasy.premierleague.com/api"


def get_bootstrap_data(cache_path=None, season=None):
    """All players, teams, positions and season totals in one call.

    ALWAYS returns a dict shaped like the API response ('elements', 'teams',
    'element_types', 'events') - including on the cache fallback path, so a
    caller doing `data['elements']` cannot die with a KeyError the moment the
    FPL API is unreachable, which would take down app startup and every
    scheduled job precisely when they should degrade quietly.

    Three tiers: live API, then the whole cached JSON payload, then a dict
    rebuilt from the individual CSVs (which is all older caches have).
    """
    season = season or seasons.current_season()
    json_cache = seasons.bootstrap_path(season)

    try:
        response = requests.get(f"{BASE}/bootstrap-static/", timeout=20)
        response.raise_for_status()
        data = response.json()
    except (requests.exceptions.RequestException, ValueError) as e:
        print(f"Bootstrap API unavailable ({e}); using cached copy")
        return _cached_bootstrap(season, json_cache)

    # Cache the payload whole AND keep the per-table CSVs the rest of the
    # project reads directly.
    try:
        os.makedirs(os.path.dirname(json_cache), exist_ok=True)
        with open(json_cache, "w", encoding="utf-8") as f:
            json.dump(data, f)
        if data.get("elements"):
            pd.DataFrame(data["elements"]).to_csv(
                cache_path or seasons.players_path(season, create_dir=True), index=False)
        if data.get("teams"):
            pd.DataFrame(data["teams"]).to_csv(seasons.teams_path(season), index=False)
        if data.get("element_types"):
            os.makedirs(os.path.dirname(seasons.positions_path()), exist_ok=True)
            pd.DataFrame(data["element_types"])[["id", "singular_name"]].to_csv(
                seasons.positions_path(), index=False)
    except OSError as e:
        print(f"Couldn't write bootstrap cache: {e}")

    return data


def _cached_bootstrap(season, json_cache):
    """Offline reconstruction of the bootstrap dict."""
    if os.path.exists(json_cache):
        try:
            with open(json_cache, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError) as e:
            print(f"Bootstrap cache unreadable ({e}); falling back to CSVs")

    # No JSON cache (or it's corrupt) - rebuild the tables we do have. Callers
    # that only need elements/teams/element_types work fine off this; `events`
    # is absent, which the season clock already treats as "can't tell".
    data = {}
    for key, path in (("elements", seasons.players_path(season)),
                      ("teams", seasons.teams_path(season)),
                      ("element_types", seasons.positions_path())):
        try:
            data[key] = pd.read_csv(path).to_dict("records")
        except (FileNotFoundError, OSError, pd.errors.EmptyDataError):
            data[key] = []
    if not data["elements"]:
        print("WARNING: no cached player data available - the app cannot rate players.")
    return data


def get_fixtures(cache_path=None, season=None):
    """All fixtures for the season - past and upcoming, with FPL's own
    difficulty ratings per team. Falls back to cache if the API is down."""
    season = season or seasons.current_season()
    cache_path = cache_path or seasons.fixtures_path(season, create_dir=True)
    try:
        response = requests.get(f"{BASE}/fixtures/", timeout=20)
        response.raise_for_status()
        df = pd.DataFrame(response.json())
        df.to_csv(cache_path, index=False)
        return df
    except (requests.exceptions.RequestException, ValueError):
        print("Fixtures API unavailable, falling back to cached fixtures")
        try:
            return pd.read_csv(cache_path)
        except (FileNotFoundError, OSError, pd.errors.EmptyDataError):
            print("No cached fixtures either - returning empty.")
            return pd.DataFrame()



def get_previous_season_fixture_strength(fixtures_path=None, teams_path=None, season=None):
    """Real attack/defence strength per team, computed from a completed
    season's actual results (goals scored/conceded) rather than a single Elo
    figure. Genuinely distinct attack vs defence numbers, and uses FPL's own
    short_name convention throughout - no external name-mapping needed."""
    season = season or seasons.previous_season() or seasons.FIRST_TRAINING_SEASON
    fixtures_path = fixtures_path or seasons.fixtures_path(season)
    teams_path = teams_path or seasons.teams_path(season)

    try:
        fixtures = pd.read_csv(fixtures_path)
        teams = pd.read_csv(teams_path)
    except (FileNotFoundError, OSError):
        start = seasons.season_start_year(season)
        tag = f"{start}-{str(start + 1)[2:]}"
        base = "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data"
        fixtures = pd.read_csv(f"{base}/{tag}/fixtures.csv")
        teams = pd.read_csv(f"{base}/{tag}/teams.csv")
        os.makedirs(os.path.dirname(fixtures_path), exist_ok=True)
        fixtures.to_csv(fixtures_path, index=False)
        teams.to_csv(teams_path, index=False)

    id_to_short_name = dict(zip(teams['id'], teams['short_name']))
    finished = fixtures[fixtures['finished'] == True].copy()

    home_stats = finished.groupby('team_h').agg(
        attack_home=('team_h_score', 'mean'),
        defence_home=('team_a_score', 'mean'),
    )
    away_stats = finished.groupby('team_a').agg(
        attack_away=('team_a_score', 'mean'),
        defence_away=('team_h_score', 'mean'),
    )

    strength = home_stats.join(away_stats, how='outer')
    strength['short_name'] = strength.index.map(id_to_short_name)

    # Defence is inverted - fewer goals conceded should mean HIGHER strength,
    # matching the direction FPL's own strength columns use.
    strength['defence_home'] = -strength['defence_home']
    strength['defence_away'] = -strength['defence_away']

    return strength.reset_index(drop=True)


# ClubElo is an optional enrichment with a working cache behind it, not a
# dependency: it only fills in preseason team strength before FPL's own numbers
# exist. It does not deserve the 20s the FPL API gets. Eight is generous for a
# 32 KB static CSV, and it caps what a ClubElo outage costs every app boot.
CLUBELO_TIMEOUT = 8


def clubelo_cache_is_current(data, on_date):
    """Is every cached rating still in force on `on_date`?

    ClubElo publishes a rating as valid over a date range - `From` until the
    club's next fixture, `To` - so the file itself says whether it has expired.
    That is worth using instead of the file's mtime, because
    `data/reference/clubelo_ratings.csv` is COMMITTED and therefore baked into
    the image. On a fresh volume `ensure_seeded()` copies it across, which
    stamps it with today's mtime and would make a file built months ago look
    like this morning's fetch. The dates inside it cannot be faked that way.

    Any row already expired means the cache is stale as a whole, since that
    club has played since it was written.
    """
    if data is None or data.empty or 'To' not in data.columns:
        return False
    to = pd.to_datetime(data['To'], errors='coerce')
    if to.isna().any():
        return False
    return bool(to.min() >= pd.Timestamp(on_date))


def _read_clubelo_cache(cache_path):
    try:
        return pd.read_csv(cache_path)
    except (FileNotFoundError, OSError, pd.errors.EmptyDataError,
            pd.errors.ParserError):
        return None


def get_clubelo_ratings(cache_path=None, on_date=None):
    """Current Elo rating for every club, from clubelo.com. Unlike FPL's own
    team strength fields, Elo carries over between seasons rather than
    resetting to 0 - usable as a strength proxy before the new season's
    matches have generated FPL's own numbers.

    The date is today's by default rather than a pinned string: a hardcoded
    date silently goes stale and starts returning last year's ratings.

    A cache that is still in force is used WITHOUT going to the network. That
    is not a staleness trade: the endpoint is keyed by date and the ratings
    carry the range they are valid over, so an unexpired cache is the same
    answer the request would return. It was worth doing because this call sat
    on the app's startup path and on every /api/refresh, and a single 20-second
    timeout against an unreachable ClubElo was costing more than rating the
    entire player pool - twenty of the forty-six seconds a cold boot took.
    """
    from datetime import date as _date
    cache_path = cache_path or seasons.clubelo_path()
    # An explicitly requested date must not be answered from a cache holding a
    # different one. Only the default (today) can be served locally.
    wants_today = on_date is None
    on_date = on_date or _date.today().isoformat()

    cached = _read_clubelo_cache(cache_path)
    if wants_today and clubelo_cache_is_current(cached, on_date):
        return cached

    try:
        # Plain HTTP, and it has to be. api.clubelo.com does not answer on 443
        # at all - a request there does not get a certificate error, it hangs
        # until the timeout - so switching to https means every fetch silently
        # falls through to the except below and the ratings quietly freeze at
        # whatever is in the cache. Checked 2026-08-31; if they ever do offer
        # TLS this should move, but confirm it responds first.
        #
        # What is actually at risk: an on-path attacker could change the Elo
        # numbers feeding team strength. That is a wrong fixture-difficulty
        # column, not code execution, and nothing here is trusted enough to be
        # worth more than this note.
        response = requests.get(f"http://api.clubelo.com/{on_date}",
                                timeout=CLUBELO_TIMEOUT)
        response.raise_for_status()
        from io import StringIO
        data = pd.read_csv(StringIO(response.text))
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        data.to_csv(cache_path, index=False)
        return data
    except (requests.exceptions.RequestException, ValueError):
        print("ClubElo unavailable, falling back to cached ratings")
        return cached if cached is not None else pd.DataFrame()


# ClubElo uses its own naming convention - map to FPL's short_name per team.
# One-time mapping, only needs updating if a club's ClubElo name changes.
CLUBELO_NAME_MAP = {
    'Arsenal': 'ARS', 'Aston Villa': 'AVL', 'Brighton': 'BHA', 'Bournemouth': 'BOU',
    'Brentford': 'BRE', 'Chelsea': 'CHE', 'Coventry': 'COV', 'Crystal Palace': 'CRY',
    'Everton': 'EVE', 'Fulham': 'FUL', 'Hull': 'HUL', 'Ipswich': 'IPS',
    'Leeds': 'LEE', 'Liverpool': 'LIV', 'Man City': 'MCI', 'Man United': 'MUN',
    'Newcastle': 'NEW', 'Forest': 'NFO', 'Sunderland': 'SUN', 'Tottenham': 'TOT',
    'Burnley': 'BUR', 'Wolves': 'WOL', 'West Ham': 'WHU',
}


def get_player_history(player_id, cache_dir=None, season=None):
    """Gameweek-by-gameweek history for a single player.

    Cached under the CURRENT season's directory - a completed season's data is
    never written to, so last season's training set can't be overwritten by a
    live fetch."""
    cache_dir = cache_dir or seasons.element_summaries_dir(season, create=True)
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = f"{cache_dir}/{player_id}.json"

    try:
        response = requests.get(f"{BASE}/element-summary/{player_id}/", timeout=20)
        response.raise_for_status()
        data = response.json()
        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        return data
    except (requests.exceptions.RequestException, ValueError):
        if os.path.exists(cache_path):
            with open(cache_path, encoding='utf-8') as f:
                return json.load(f)
        return None


def get_all_gameweek_history(player_ids, season=None, polite_delay=0.3, progress_every=100):
    """Per-gameweek history for every player id given, as one DataFrame.

    Slow by design (one API call per player, ~700 of them), so this belongs in
    the nightly job, not in app startup or a request path."""
    all_history = []
    ids = list(player_ids)

    for n, player_id in enumerate(ids, start=1):
        data = get_player_history(player_id, season=season)
        if data is None:
            continue
        history = pd.DataFrame(data.get('history', []))
        if history.empty:
            continue
        history['player_id'] = player_id
        all_history.append(history)
        if progress_every and n % progress_every == 0:
            print(f"  gameweek history: {n}/{len(ids)} players", flush=True)
        time.sleep(polite_delay)  # be polite to the API

    if not all_history:
        print("No gameweek history retrieved (season may not have started yet).")
        return pd.DataFrame()

    return pd.concat(all_history, ignore_index=True)


def refresh_gameweek_stats(player_ids, season=None):
    """Pull the current season's per-gameweek rows and write them to THIS
    season's gameweek_stats.csv.

    This is the file 'inseason' ratings are built from. It previously had two
    problems: nothing ever called the code that wrote it, and the writer used a
    repo-root-relative path while the reader used a python/-relative one, so
    even when it did run the reader couldn't find it. Both are fixed by going
    through seasons.gameweek_stats_path().
    """
    season = season or seasons.current_season()
    df = get_all_gameweek_history(player_ids, season=season)
    if df.empty:
        return {"written": False, "rows": 0, "season": season,
                "detail": "no gameweek history available yet"}
    path = seasons.gameweek_stats_path(season, create_dir=True)
    df.to_csv(path, index=False)
    rounds = sorted(df['round'].dropna().unique()) if 'round' in df.columns else []
    return {"written": True, "rows": len(df), "season": season, "path": path,
            "gameweeks": [int(r) for r in rounds]}
