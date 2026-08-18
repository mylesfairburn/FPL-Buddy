import os
import re
from datetime import datetime, timedelta, timezone

import joblib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import seasons
import train_model
from train_model import predict_bundle

MODEL_DIR = seasons.MODELS_DIR
ROLL_WINDOW = 3


class StaleModelError(RuntimeError):
    """A bundle on disk predates the current feature set."""


def load_models(model_dir=MODEL_DIR):
    """Load the per-position bundle saved by train_model.py.

    Refuses a bundle from an older MODEL_VERSION rather than using it. That
    sounds unhelpful for a site that would rather serve something than nothing,
    but the failure it prevents is worse than an outage: the bundles live on the
    deployment's mounted volume, `seasons.ensure_seeded()` only ever copies
    files that are MISSING, and a deploy therefore hands new inference code the
    pickle the volume already had. Feature names have changed across that
    boundary, and a model asked for columns it has never seen either raises deep
    inside sklearn or - worse - silently scores every player off whatever
    happened to line up. Failing here names the fix instead:

        docker exec fpl-buddy python train_model.py
    """
    bundles = {}
    stale = []
    for filename in sorted(os.listdir(model_dir)):
        if not filename.endswith('_model.pkl'):
            continue
        position = filename.replace('_model.pkl', '').capitalize()
        bundle = joblib.load(f'{model_dir}/{filename}')
        if bundle.get('version') != train_model.MODEL_VERSION:
            stale.append(f"{filename} (version {bundle.get('version', 'unversioned')})")
            continue
        bundles[position] = bundle
    if stale:
        raise StaleModelError(
            f"{len(stale)} model bundle(s) predate this code and were not loaded: "
            f"{', '.join(stale)}. Expected version {train_model.MODEL_VERSION}. "
            "Retrain with `python train_model.py` (in production: "
            "`docker exec fpl-buddy python train_model.py`).")
    return bundles


def _current_strength_ranks():
    """This season's club strength percentiles, keyed by team id.

    The same table `train_model.team_strength_ranks` builds for a training
    season, for the season being predicted. Going through that one function is
    the point: the ranks the model was fitted on and the ranks it is asked to
    predict from are then produced by the same code, and cannot drift apart the
    way the old `difficulty * 220` scaling did.
    """
    ranks = train_model.team_strength_ranks(seasons.current_season())
    if ranks is None:
        return None
    return ranks.set_index('team_id')


def _opponent_rank_columns(rows, ranks):
    """Attach opp_*/own_* rank columns to a frame of upcoming fixtures.

    `rows` needs `team`, `opponent` and `was_home`. A club's HOME strength is
    what it brings to its own ground, so the opponent contributes its away
    rating when the player is at home - the same convention
    train_model.add_fixture_context uses.
    """
    empty = pd.Series(np.nan, index=rows.index)
    if ranks is None:
        for name in ('opp_strength_rank', 'opp_attack_rank', 'opp_defence_rank',
                     'own_attack_rank', 'own_defence_rank'):
            rows[name] = empty
        return rows

    home = rows['was_home'].astype(bool)

    def lookup(ids, column):
        if column not in ranks.columns:
            return empty
        return pd.Series(ids, index=rows.index).map(ranks[column])

    for out, kind in (('opp_strength_rank', 'overall'),
                      ('opp_attack_rank', 'attack'),
                      ('opp_defence_rank', 'defence')):
        rows[out] = np.where(home,
                             lookup(rows['opponent'], f'{kind}_away_rank'),
                             lookup(rows['opponent'], f'{kind}_home_rank'))
    for out, kind in (('own_attack_rank', 'attack'), ('own_defence_rank', 'defence')):
        rows[out] = np.where(home,
                             lookup(rows['team'], f'{kind}_home_rank'),
                             lookup(rows['team'], f'{kind}_away_rank'))
    return rows


def build_fixture_features(fixtures_df, n_fixtures=3):
    """For each team, the average strength of their next N unplayed fixtures.

    Averaged across the run rather than taken one at a time, because this feeds
    the single blended `predicted_points` figure. Per-gameweek numbers come from
    build_per_gameweek_fixture_features below.
    """
    upcoming = fixtures_df[fixtures_df['finished'] == False].sort_values('event')
    ranks = _current_strength_ranks()

    rows = []
    team_ids = pd.concat([upcoming['team_h'], upcoming['team_a']]).unique()
    for team_id in team_ids:
        home = upcoming[upcoming['team_h'] == team_id][['event', 'team_a']] \
            .rename(columns={'team_a': 'opponent'})
        home['was_home'] = True
        away = upcoming[upcoming['team_a'] == team_id][['event', 'team_h']] \
            .rename(columns={'team_h': 'opponent'})
        away['was_home'] = False

        fixtures = pd.concat([home, away]).sort_values('event').head(n_fixtures)
        if fixtures.empty:
            continue
        fixtures['team'] = team_id
        rows.append(fixtures)

    if not rows:
        return pd.DataFrame()

    frame = _opponent_rank_columns(pd.concat(rows, ignore_index=True), ranks)
    rank_cols = ['opp_strength_rank', 'opp_attack_rank', 'opp_defence_rank',
                 'own_attack_rank', 'own_defence_rank']
    return (frame.groupby('team')[rank_cols + ['was_home']]
            .mean().reset_index())


def build_per_gameweek_fixture_features(fixtures_df, n_fixtures=3):
    """Like build_fixture_features, but keeps each of the next N fixtures
    SEPARATE (one row per team per upcoming event) instead of averaging them
    into a single number. This is what lets us predict points per gameweek for
    the 'next 3 GWs' columns, rather than one blended figure."""
    upcoming = fixtures_df[fixtures_df['finished'] == False].sort_values('event')

    rows = []
    team_ids = pd.concat([upcoming['team_h'], upcoming['team_a']]).unique()
    for team_id in team_ids:
        home = upcoming[upcoming['team_h'] == team_id][['event', 'team_a', 'team_h_difficulty']] \
            .rename(columns={'team_a': 'opponent', 'team_h_difficulty': 'fpl_difficulty'})
        home['was_home'] = True

        away = upcoming[upcoming['team_a'] == team_id][['event', 'team_h', 'team_a_difficulty']] \
            .rename(columns={'team_h': 'opponent', 'team_a_difficulty': 'fpl_difficulty'})
        away['was_home'] = False

        tf = pd.concat([home, away]).sort_values('event').head(n_fixtures)
        if tf.empty:
            continue
        tf['team'] = team_id
        rows.append(tf)

    if not rows:
        return pd.DataFrame()
    # `fpl_difficulty` is carried through untouched for the UI only - it
    # colours each fixture cell on the same 1-5 key as the rotator page. Not a
    # model input.
    return _opponent_rank_columns(pd.concat(rows, ignore_index=True),
                                  _current_strength_ranks())


def attach_per_gameweek_points(position_dfs, model_bundles, form_features,
                               per_gw_features, team_id_to_short=None, n_gameweeks=3,
                               calibrators=None):
    """Adds a `next_gameweeks` column to each position df: a per-player list of
    {event, opponent, was_home, difficulty, points} for the next N gameweeks.
    `difficulty` is FPL's own 1 (easy) - 5 (hard) rating, so the front end can
    colour each cell with the SAME key the fixtures/rotator pages use.

    Predicted points are the model run per fixture (home/away and opponent
    strength vary by gameweek; a player's form features stay constant), so the
    three numbers genuinely differ across an easy/hard run rather than repeating
    one blended figure."""
    updated = {}
    team_id_to_short = team_id_to_short or {}

    for position, df in position_dfs.items():
        df = df.copy()

        if position not in model_bundles or per_gw_features.empty or df.empty:
            df['next_gameweeks'] = [[] for _ in range(len(df))]
            updated[position] = df
            continue

        bundle = model_bundles[position]

        # Build features on a CLEAN frame of just the join keys. df here is the
        # already-rated df, which already carries the fixture and rolling
        # columns from predict_ratings - merging onto it directly would collide
        # and get suffixed (_x/_y), so the feature columns would go missing.
        keys = ['code', 'team'] + (['now_cost'] if 'now_cost' in df.columns else [])
        work = df[keys].drop_duplicates()
        work = work.merge(form_features, on='code', how='left')
        work = work.merge(per_gw_features, on='team', how='left')  # -> one row per (player, upcoming GW)
        work = _attach_price(work)

        seen = work.get('games_seen')
        has_features = seen.notna() & (seen > 0) if seen is not None else pd.Series(
            False, index=work.index)
        work['gw_points'] = np.nan
        if has_features.any():
            rows = work.loc[has_features]
            expected, if_starts, p_start = predict_bundle(bundle, rows)
            # Same dispersion correction as the headline figure, from the same
            # fitted map. Without it the "next 3 GWs" cells would sit a point
            # below the projection printed beside them on the same row.
            calibrator = (calibrators or {}).get(position)
            if calibrator is not None:
                raw = pd.Series(if_starts, index=rows.index)
                mapped = apply_calibration(raw, calibrator)
                expected = np.clip(expected + p_start * (mapped - raw), 0, None)
            work.loc[has_features, 'gw_points'] = expected

        # One ordered list of gameweeks per player code.
        by_code = {}
        for _, r in work[work['event'].notna()].sort_values(['code', 'event']).iterrows():
            opp_id = r.get('opponent')
            by_code.setdefault(r['code'], []).append({
                'event': int(r['event']),
                'opponent': team_id_to_short.get(opp_id, opp_id),
                'was_home': bool(r['was_home']) if pd.notna(r.get('was_home')) else None,
                'difficulty': int(r['fpl_difficulty']) if pd.notna(r.get('fpl_difficulty')) else None,
                'points': round(float(r['gw_points']), 1) if pd.notna(r.get('gw_points')) else None,
            })

        df['next_gameweeks'] = df['code'].map(lambda c: by_code.get(c, [])[:n_gameweeks])
        updated[position] = df

    return updated


# ---------------------------------------------------------------------------
#  Availability
# ---------------------------------------------------------------------------
# The model predicts what a player would score if he plays. It has no idea
# whether he is fit, because fitness isn't one of its features - so a player
# ruled out with a torn hamstring still came out of it with a full projection,
# and that projection was then printed on the players table, his own page, the
# My Team pitch and the gameweek briefing. It is the most obviously wrong number
# the site can show: everybody reading it already knows he isn't playing.
#
# So availability is applied as a post-process here rather than taught to the
# model. One pass, at the one point every consumer reads from, which is why it
# lives in get_rated_position_dfs() alongside the per-gameweek attach.

# FPL publishes availability as a status letter plus an optional percentage:
#   'a' available, 'd' doubtful, 'i' injured, 's' suspended, 'u' unavailable
# ('u' never reaches here - run_pipeline() drops those rows outright.) Only a
# stated 0% is treated as "cannot play". A 75% player is left completely alone:
# he might play, and the squad optimiser already scales his points by that
# probability, so zeroing him here would be double-counting the same risk.
BLOCKED_STATUS = {"i", "s", "u"}

# "Ankle injury - Expected back 23 Aug" / "Suspended until 29 Aug". FPL omits
# the year, hence _return_year below. The month is matched on its first three
# letters, which is the form FPL uses in every one of these strings.
_RETURN_RE = re.compile(
    r"(?:expected\s+back|suspended\s+until|out\s+until)\s+"
    r"(\d{1,2})\s+([a-z]{3})[a-z]*\.?\s*(\d{4})?",
    re.IGNORECASE)

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def _parse_stamp(value):
    """FPL's ISO timestamps ('2026-08-08T19:00:05.613036Z') as aware UTC."""
    if not value or value != value:      # None or NaN
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def parse_return_date(news, news_added=None, now=None):
    """The date a flagged player is expected back, from FPL's `news` string.

    Returns an aware UTC datetime at midnight, or None when the string carries
    no date - which is the common case, since "Unknown return date" is what FPL
    writes whenever a club hasn't given one.

    The year is the interesting part: FPL never states it, so "Expected back
    10 Jan" posted in December means NEXT January. It's resolved against the
    timestamp on the news item rather than against today, because that is the
    date the club was talking about - reading it against today would flip the
    answer by a year every time the calendar crossed a new year while the item
    stood."""
    if not news or news != news:
        return None
    m = _RETURN_RE.search(str(news))
    if not m:
        return None
    day, month_word, year = m.group(1), m.group(2).lower(), m.group(3)
    month = _MONTHS.get(month_word)
    if month is None:
        return None

    if year:
        try:
            return datetime(int(year), month, int(day), tzinfo=timezone.utc)
        except ValueError:
            return None

    # No year given: take the one that puts the date at or after the news item.
    # A day of slack absorbs items posted the same morning as the return.
    anchor = _parse_stamp(news_added) or now or datetime.now(timezone.utc)
    for candidate in (anchor.year, anchor.year + 1):
        try:
            when = datetime(candidate, month, int(day), tzinfo=timezone.utc)
        except ValueError:
            return None                    # 31 Feb and friends
        if when >= anchor - timedelta(days=1):
            return when
    return None


def fixture_kickoffs(fixtures_df):
    """{(team_id, event): earliest kickoff} for every unplayed fixture.

    Earliest rather than only, because a double gameweek gives a club two
    fixtures in one event and the first is the one that decides whether a
    player was still injured when the gameweek came around."""
    kickoffs = {}
    if fixtures_df is None or fixtures_df.empty:
        return kickoffs
    for _, row in fixtures_df.iterrows():
        event = row.get("event")
        when = _parse_stamp(row.get("kickoff_time"))
        if when is None or event is None or event != event:
            continue
        for side in ("team_h", "team_a"):
            team = row.get(side)
            if team is None or team != team:
                continue
            key = (int(team), int(event))
            if key not in kickoffs or when < kickoffs[key]:
                kickoffs[key] = when
    return kickoffs


def _blocking_chance(row):
    """True when FPL is saying this player cannot play the next round.

    A stated 0% is the signal. Status 'i'/'s'/'u' with no percentage at all
    counts too - that's FPL saying "out" without attaching a number, which is
    the same fact stated less precisely. Everything else, including every
    partial percentage, is left alone."""
    chance = row.get("chance_of_playing_next_round")
    if chance is not None and chance == chance:      # not None, not NaN
        try:
            return float(chance) <= 0
        except (TypeError, ValueError):
            return False
    return str(row.get("status") or "a").lower() in BLOCKED_STATUS


def zero_unavailable_points(position_dfs, fixtures_df, now=None):
    """Rewrite the projections of players who cannot play to zero.

    Three cases, and the middle one is the point of the whole function:

      * A 0% player whose news names a return date keeps his projection for
        every gameweek kicking off on or after it. A hamstring that costs three
        weeks should not blank out October.
      * A 0% player with no date loses the next gameweek only. Guessing further
        than FPL is willing to would be inventing a recovery timeline.
      * Anyone else - including every partial percentage - is untouched.

    Nothing here is sticky. The nightly pipeline re-reads `news` on every run,
    so a player's projections come back on their own the moment his return date
    passes or his flag is lifted; there is no state to reset and no job to
    remember to run."""
    now = now or datetime.now(timezone.utc)
    kickoffs = fixture_kickoffs(fixtures_df)
    updated = {}

    for position, df in position_dfs.items():
        df = df.copy()
        if df.empty:
            updated[position] = df
            continue

        new_gameweeks, new_points, new_ratings = [], [], []
        for _, row in df.iterrows():
            gws = row.get("next_gameweeks")
            gws = list(gws) if isinstance(gws, list) else []
            predicted = row.get("predicted_points")
            rating = row.get("rating")

            if not _blocking_chance(row):
                new_gameweeks.append(gws)
                new_points.append(predicted)
                new_ratings.append(rating)
                continue

            back = parse_return_date(row.get("news"), row.get("news_added"), now=now)
            team = row.get("team")
            team = int(team) if team is not None and team == team else None

            out = []
            for i, gw in enumerate(gws):
                if back is None:
                    # No published return date: only the round FPL is actually
                    # talking about, which is the next one.
                    blocked = i == 0
                else:
                    kick = kickoffs.get((team, gw.get("event")))
                    # An unknown kickoff can't be compared, so it isn't
                    # zeroed - saying nothing beats saying something wrong.
                    blocked = kick is not None and kick < back
                out.append({**gw, "points": 0.0} if blocked else gw)

            # predicted_points is the single blended figure the tables sort and
            # display, and it describes the round being picked for. It follows
            # the first gameweek's verdict; with no fixture list to go on, it
            # follows the flag itself.
            if out:
                blocked_now = out[0].get("points") == 0.0
            else:
                blocked_now = back is None or back > now
            new_gameweeks.append(out)
            new_points.append(0.0 if blocked_now else predicted)
            # The rating goes with it, or a torn hamstring shows "0.0
            # predicted, rating 94". Ranking on points_if_starts makes leaving
            # it alone worse, not better: ability is exactly what an injury
            # doesn't touch, so the best unavailable players would sit at the
            # top of a table sorted by rating. A player who cannot play this
            # round is not a 94.
            new_ratings.append(0.0 if blocked_now else rating)

        df["next_gameweeks"] = pd.Series(new_gameweeks, index=df.index, dtype=object)
        if "predicted_points" in df.columns:
            df["predicted_points"] = new_points
        if "rating" in df.columns:
            df["rating"] = new_ratings
        updated[position] = df

    return updated


# How many completed current-season gameweeks the form features need before
# they stop falling back to last season's average. Three, because that is the
# window the rolling average is built over - below it the "rolling" figure is a
# partial one dressed up as a full one.
MIN_CURRENT_GAMEWEEKS = 3


def completed_current_gameweeks(current_gw_path=None):
    """How many distinct current-season gameweeks have stats on disk.

    Zero when the file is missing, unreadable, or has no `round` column. That
    is the preseason state - the stats pull doesn't write the file until a
    round has been played - and it is also the honest answer for a file that
    arrived half-written, which is worth failing back to last season's average
    over rather than raising out of a job nobody is watching."""
    current_gw_path = current_gw_path or seasons.gameweek_stats_path()
    if not os.path.exists(current_gw_path):
        return 0
    try:
        current = pd.read_csv(current_gw_path)
    except (OSError, pd.errors.ParserError, pd.errors.EmptyDataError):
        return 0
    if 'round' not in current.columns:
        return 0
    return int(current['round'].nunique())


def using_fallback_form(mode=None, current_gw_path=None,
                        min_current_gameweeks=MIN_CURRENT_GAMEWEEKS):
    """Whether ratings are standing on last season's average rather than
    current-season form.

    Asked from outside the pipeline to find out whether a projection is a real
    ranking or a cold-start estimate. While this is true every player's "form"
    is a whole-season average, which flattens the variance that separates
    attackers far harder than it touches a defender's appearance-and-clean-sheet
    floor: the projections bunch, and defenders drift to the top of them. See
    `gw_report.captain_picks`, which declines to print a ranking on that basis.

    `build_current_form_features` decides the same question by calling this, so
    the page and the pipeline cannot disagree about which mode produced the
    numbers on it."""
    if mode == 'preseason':
        return True
    if mode == 'inseason':
        return False
    return completed_current_gameweeks(current_gw_path) < min_current_gameweeks


def build_current_form_features(
    current_gw_path=None,
    fallback_path=None,
    current_players_path=None,
    previous_players_path=None,
    min_current_gameweeks=MIN_CURRENT_GAMEWEEKS,
    mode=None,
):
    """Builds a rolling-3-gameweek 'current form' row per player, keyed by
    'code' rather than 'id' - FPL reassigns 'id' every season, so a brand
    new signing can end up sharing last season's id with a departed player.
    'code' is the one identifier that stays consistent for a given real
    player across seasons, so it's the only safe join key here.

    mode: 'preseason' or 'inseason' forces which source is used. Leave as
    None to auto-detect based on how many current-season gameweeks exist.

    Paths default to the current/previous season directories via seasons.py.
    The current-season file used to be written by one path and read by another,
    so 'inseason' could never find it however far into the season you were."""

    prev = seasons.previous_season() or seasons.FIRST_TRAINING_SEASON
    current_gw_path = current_gw_path or seasons.gameweek_stats_path()
    fallback_path = fallback_path or seasons.gameweek_stats_path(prev)
    current_players_path = current_players_path or seasons.players_path()
    previous_players_path = previous_players_path or seasons.players_path(prev)

    if mode == 'preseason':
        use_fallback = True
    elif mode == 'inseason':
        use_fallback = False
        if not os.path.exists(current_gw_path):
            raise ValueError("No current-season gameweek data exists yet - can't use inseason mode")
        current = pd.read_csv(current_gw_path)
        if 'element' in current.columns:
            current = current.rename(columns={'element': 'player_id'})
        if current['round'].nunique() < 1:
            raise ValueError("Current-season gameweek data is empty - can't use inseason mode")
    else:
        use_fallback = using_fallback_form(mode, current_gw_path,
                                           min_current_gameweeks)
        if not use_fallback:
            current = pd.read_csv(current_gw_path)
            if 'element' in current.columns:
                current = current.rename(columns={'element': 'player_id'})

    if not use_fallback and os.path.exists(current_gw_path):
        current = pd.read_csv(current_gw_path)
        if 'element' in current.columns:
            current = current.rename(columns={'element': 'player_id'})

    if use_fallback:
        print("Not enough current-season gameweeks yet - using last season's full-season average as current form")
        source = pd.read_csv(fallback_path)
        if 'element' in source.columns:
            source = source.rename(columns={'element': 'player_id'})

        previous_players = pd.read_csv(previous_players_path)
        id_to_code = dict(zip(previous_players['id'], previous_players['code']))

    else:
        source = current
        current_players = pd.read_csv(current_players_path)
        id_to_code = dict(zip(current_players['id'], current_players['code']))

    source['code'] = source['player_id'].map(id_to_code)
    source = source.dropna(subset=['code'])
    source['code'] = source['code'].astype(int)

    if use_fallback:
        # Filter out players with minimal minutes so a single substitute cameo
        # doesn't produce a misleadingly high per-game average.
        minutes_played = pd.to_numeric(source['minutes'], errors='coerce') \
            .groupby(source['code']).sum()
        eligible = minutes_played[minutes_played >= 180].index  # ~2 full games
        source = source[source['code'].isin(eligible)]

    form = _features_for_next_gameweek(source)

    if use_fallback:
        # Preseason only: flatten the short windows onto the season-long level.
        #
        # The rolling columns would otherwise hold last season's FINAL three and
        # six gameweeks, which is the run-in - rested title winners, rotated
        # squads, dead rubbers. That is the exact bug this fallback was written
        # to avoid: Haaland ranked low off a rested finish, fringe players
        # ranked high off one lucky game. Across a summer of transfers those
        # last three games carry no more information than the season did, so
        # they are set to the season average rather than trusted as "form".
        for col in train_model.ROLLING_COLS:
            level = f'{col}_level'
            if level not in form.columns:
                continue
            for window in train_model.ROLL_WINDOWS:
                if f'{col}_roll{window}' in form.columns:
                    form[f'{col}_roll{window}'] = form[level]

    return form.reset_index()


def _features_for_next_gameweek(source):
    """Per-player features describing the state going INTO the next gameweek.

    The trainer's features are all `shift(1)`-then-aggregate, so the row for
    gameweek R carries the history of everything before R. The numbers wanted
    here are the ones a hypothetical *next* row would carry - so that row is
    exactly what gets appended: one blank gameweek per player, at one past the
    last real round. `train_model.build_rolling_features` then fills it in, and
    the appended rows are read back out.

    Going through the trainer's own function rather than recomputing the
    aggregates is the whole point. There are 86 features across four families
    and two window lengths; a second implementation of that arithmetic would
    agree with the first on the day it was written and quietly stop agreeing
    later. This cannot: there is only one implementation.
    """
    source = source.copy()
    source['round'] = pd.to_numeric(source['round'], errors='coerce')
    source = source.dropna(subset=['round'])
    # One season's worth of history at a time, so the grouping key the trainer
    # uses is constant here.
    source['season_start'] = 0
    source['player_key'] = source['code']

    future = source.sort_values('round').groupby('code', as_index=False).tail(1).copy()
    future['round'] = source['round'].max() + 1
    for col in train_model.ROLLING_COLS + ['value']:
        if col in future.columns:
            future[col] = np.nan
    future['is_future'] = True
    source['is_future'] = False

    combined = train_model.build_rolling_features(
        pd.concat([source, future], ignore_index=True))
    rows = combined[combined['is_future']].set_index('code')

    keep = [c for c in rows.columns
            if c.endswith(('_roll3', '_roll6', '_level', '_per90'))
            or c == 'games_seen']
    return rows[keep]


def _attach_price(df):
    """`price` in millions, from the live bootstrap rather than last season.

    The trainer derives it from each gameweek row's `value`; here the only
    honest source is what the player costs today, which is what `now_cost`
    is."""
    if 'now_cost' in df.columns:
        df['price'] = pd.to_numeric(df['now_cost'], errors='coerce') / 10.0
    else:
        df['price'] = np.nan
    return df


# How much rotation risk the preseason rating carries, as the exponent on
# start probability in `rating_basis`.
#
# The two ends are both wrong. Ranking on ability alone (0.0) puts Diop top of
# the defenders on a 26% chance of starting and 1.6 projected points; ranking on
# predicted_points (1.0) drops Saka to 12th of the midfielders. Both questions
# are real, so the exponent picks a point between them.
#
# Measured on the current pool, as the share of the ordering's variance coming
# from ability rather than minutes, with two players as landmarks:
#
#     w     ability   Diop (DEF)   Saka (MID)
#   0.00      100%        1/105        3/141
#   0.10       65%       13/105        3/141
#   0.15       50%       16/105        3/141
#   0.25       30%       38/105        3/141   <- here
#   0.40       16%       61/105        5/141
#   1.00        3%       89/105       12/141
#
# A quarter-power sounds like a light touch and isn't: at 0.25 minutes still
# carry 70% of the ordering, because the inputs have wildly different spreads.
# Start probability runs 0.03-0.98 while points_if_starts is squeezed to sd 0.40
# by the preseason fallback, so the 50/50 crossover is near 0.15, not 0.5. 0.25
# is where the clearly-wrong cases at both ends are both gone.
#
# Retune by moving this one number. It stops mattering once three gameweeks
# exist, when the fallback lifts and predicted_points becomes the basis again.
PRESEASON_START_WEIGHT = 0.25


# ---------------------------------------------------------------------------
#  Dispersion calibration
# ---------------------------------------------------------------------------
# The preseason model is centred correctly and far too narrow at the edges.
# Measured against last season's realised points per start:
#
#                        model    real
#     mean                3.79    3.73     <- centred correctly
#     sd                  0.47    0.96     <- half the spread
#     p90                 4.34    4.94
#     share >= 5.0        1.8%    9.7%     <- five times too few
#
# That is a calibration fault, not the model honestly declining to guess, and it
# has a standard correction: map each player's points-per-start onto the
# quantile of last season's realised distribution at the same rank.
#
# The map is monotone, so the ORDER is untouched - it adds no information about
# who is better than whom, because there is none to add. It only fixes the
# shape. A player the model cannot separate from his neighbour still isn't
# separated; he is just no longer reported as a 3.7 when his rank scored 4.9.
#
# Preseason only. In season the rolling windows carry real signal and the
# model's own dispersion recovers, so applying this on top would over-correct.

# Cut-offs for the target distribution: enough starts that a per-start average
# means something, and 60 minutes as the definition of a start (matching the
# trainer's fallback when `starts` is absent).
_CALIB_MIN_STARTS = 5
_CALIB_QUANTILES = np.linspace(0.0, 1.0, 101)
_TARGET_CACHE = {}

_POSITION_FROM_SHORT = {'GK': 'Goalkeeper', 'GKP': 'Goalkeeper', 'DEF': 'Defender',
                        'MID': 'Midfielder', 'FWD': 'Forward'}


def realised_points_per_start(season=None):
    """{position: array of last season's points-per-start}, one per player.

    The target the projections are mapped onto. Read from the previous
    season's gameweek stats, which is the same file the preseason form
    fallback already stands on - so this introduces no new data dependency
    and no new way for a deployment to be missing something.

    Returns {} if the file is unreadable, which turns calibration off rather
    than failing a nightly job nobody is watching.
    """
    season = season or seasons.previous_season() or seasons.FIRST_TRAINING_SEASON
    if season in _TARGET_CACHE:
        return _TARGET_CACHE[season]

    try:
        gw = pd.read_csv(seasons.gameweek_stats_path(season))
    except (FileNotFoundError, OSError, pd.errors.EmptyDataError, pd.errors.ParserError):
        _TARGET_CACHE[season] = {}
        return {}

    needed = {'minutes', 'total_points', 'position'}
    if not needed.issubset(gw.columns):
        _TARGET_CACHE[season] = {}
        return {}

    key = 'element' if 'element' in gw.columns else 'player_id'
    if key not in gw.columns:
        _TARGET_CACHE[season] = {}
        return {}

    gw = gw.copy()
    gw['minutes'] = pd.to_numeric(gw['minutes'], errors='coerce')
    gw['total_points'] = pd.to_numeric(gw['total_points'], errors='coerce')
    if 'starts' in gw.columns:
        started = pd.to_numeric(gw['starts'], errors='coerce') == 1
    else:
        started = gw['minutes'] >= 60
    gw = gw[started & gw['total_points'].notna()]

    gw['position'] = gw['position'].map(
        lambda v: _POSITION_FROM_SHORT.get(str(v).upper(), v))

    targets = {}
    for position, block in gw.groupby('position'):
        per_player = block.groupby(key)['total_points'].agg(['mean', 'size'])
        per_player = per_player[per_player['size'] >= _CALIB_MIN_STARTS]['mean']
        if len(per_player) >= 20:          # too few and the quantiles are noise
            targets[position] = np.sort(per_player.to_numpy(dtype=float))

    _TARGET_CACHE[season] = targets
    return targets


def fit_calibrators(position_dfs, season=None):
    """{position: (src, dst)} quantile pairs for mapping points_if_starts.

    `src` is where the model's own projections sit, `dst` is where last
    season's players actually landed, both read at the same 101 quantiles.
    Applying it is then a single np.interp.

    Fitted from the whole player pool once, and reused for the per-gameweek
    numbers rather than refitted there. Refitting would rank a player among
    (player x gameweek) rows instead of among players, which is a different
    question with a different answer - and the headline figure and the next-3
    cells beside it would stop agreeing.
    """
    targets = realised_points_per_start(season)
    calibrators = {}
    for position, df in position_dfs.items():
        dst = targets.get(position)
        if dst is None or df.empty or 'points_if_starts' not in df.columns:
            continue
        values = pd.to_numeric(df['points_if_starts'], errors='coerce').dropna()
        if len(values) < 20:
            continue
        src = np.quantile(values.to_numpy(dtype=float), _CALIB_QUANTILES)
        # np.interp needs a strictly increasing x. Ties are common at the
        # squeezed centre of the model's range, and a flat segment there would
        # make the map ambiguous, so they collapse to one point.
        src, keep = np.unique(src, return_index=True)
        calibrators[position] = (src, np.quantile(dst, _CALIB_QUANTILES)[keep])
    return calibrators


def apply_calibration(if_starts, calibrator):
    """Map model points-per-start onto the realised scale. Monotone by
    construction, so ranks are preserved exactly.

    Values outside the fitted range clamp to its ends, which is np.interp's
    default and the right behaviour here: the map is only evidence about the
    span it was fitted over, and Haaland sitting above every projection in the
    pool should stay at the top rather than be extrapolated into fiction.
    """
    if calibrator is None:
        return if_starts
    src, dst = calibrator
    values = pd.to_numeric(if_starts, errors='coerce')
    mapped = np.interp(values.to_numpy(dtype=float), src, dst)
    return pd.Series(np.where(values.isna(), np.nan, mapped), index=if_starts.index)


def calibrate_position_dfs(position_dfs, calibrators, start_weight=None):
    """Rewrite points_if_starts onto the realised scale, and carry
    predicted_points and the rating with it.

    predicted_points is adjusted rather than recomputed, because the cameo term
    it also contains does not depend on points_if_starts:

        expected = p_start * if_starts + p_cameo * cameo_points
        expected' = expected + p_start * (mapped - if_starts)

    which is exact, and avoids either duplicating the blend here or making
    predict_bundle return a fourth number for one caller's benefit.

    The rating is recomputed rather than left alone. Mapping is monotone in
    points_if_starts, so a pure-ability rating would be unchanged - but the
    preseason basis mixes in start probability, and rescaling one factor of a
    product does change the order. Ranking on the calibrated scale is also the
    more defensible of the two: it is the scale the numbers are read on.
    """
    updated = {}
    for position, df in position_dfs.items():
        df = df.copy()
        calibrator = calibrators.get(position)
        if calibrator is None or df.empty or 'points_if_starts' not in df.columns:
            updated[position] = df
            continue

        raw = df['points_if_starts']
        mapped = apply_calibration(raw, calibrator)
        if 'predicted_points' in df.columns and 'start_probability' in df.columns:
            shift = df['start_probability'] * (mapped - raw)
            df['predicted_points'] = (df['predicted_points'] + shift).clip(lower=0)
        df['points_if_starts'] = mapped
        _assign_rating(df, start_weight)
        updated[position] = df
    return updated


def _assign_rating(frame, start_weight=None):
    """0-100 percentile rank within the frame, in place. Players with no
    predictable form (never played) land at the bottom on 0."""
    basis = rating_basis(frame, start_weight)
    frame['rating'] = (basis.rank(pct=True) * 100).round(1)
    frame.loc[basis.isna(), 'rating'] = 0
    return frame


def rating_basis(frame, start_weight=None):
    """The series the 0-100 rating is a percentile of.

    Kept separate from predict_ratings so the choice can be tested directly,
    and re-derived rather than stored: it is an ordering device, not a number
    the site quotes at anyone.
    """
    if start_weight is None:
        return frame['predicted_points']
    if_starts = frame['points_if_starts']
    p_start = frame['start_probability'].clip(lower=0)
    return if_starts * p_start ** float(start_weight)


def predict_ratings(position_dfs, model_bundles, form_features, fixture_features,
                    start_weight=None):
    """Expected points per player, and a 0-100 rating per position.

    Three numbers come out of this rather than one:

      predicted_points  P(start) x points-if-he-starts, plus the cameo term.
                        What the squad optimiser works in and what the site
                        shows as the expected haul.
      points_if_starts  What the model expects from him on the pitch, with the
                        rotation risk taken back out. The more useful number
                        when comparing a nailed starter to a 60% one, and the
                        one a write-up should quote.
      start_probability The other half of that comparison.

    `start_weight` decides how much rotation risk the 0-100 rating carries:

        None -> rank on predicted_points, unchanged. Correct in season.
        w    -> rank on points_if_starts x start_probability ** w

    which is one family with the two extremes at either end: w=1 is roughly
    predicted_points again, w=0 is pure ability with rotation risk ignored
    entirely. See PRESEASON_START_WEIGHT for why the preseason value sits
    where it does.

    The knob exists because predicted_points is the wrong basis on the
    preseason fallback. `points_if_starts` collapses to near-constant when
    every player's "form" is a whole-season average - sd 0.40 for defenders and
    0.18 for keepers, against last season's realised 0.96 - and a product with
    one near-constant factor is just the other factor. That left 96% of the
    order's variance coming from start_probability, ranking the table by who
    was nailed on rather than who was good.

    None of this pretends the model can separate players it cannot: the spread
    stays as narrow as it honestly is. It only decides what sorts the page.
    """
    updated = {}

    for position, df in position_dfs.items():
        df = df.copy()

        if position not in model_bundles:
            print(f"No trained model for {position}, skipping")
            df['rating'] = np.nan
            updated[position] = df
            continue

        bundle = model_bundles[position]

        merged = df.merge(form_features, on='code', how='left')
        # Real upcoming fixture strength, keyed by the player's team.
        if not fixture_features.empty:
            merged = merged.merge(fixture_features, on='team', how='left')
        merged = _attach_price(merged)

        # A player needs SOME history to be scored at all. The boosters handle
        # missing values natively, so this is not "every feature present" as it
        # was under the old scaler - it is "we have seen him play", which is the
        # honest floor. Everyone else keeps a NaN projection and ranks last,
        # which is what a player with no evidence deserves.
        seen = merged.get('games_seen')
        has_features = seen.notna() & (seen > 0) if seen is not None else pd.Series(
            False, index=merged.index)

        for col in ('predicted_points', 'points_if_starts', 'start_probability'):
            merged[col] = np.nan
        if has_features.any():
            rows = merged.loc[has_features]
            expected, if_starts, p_start = predict_bundle(bundle, rows)
            merged.loc[has_features, 'predicted_points'] = expected
            merged.loc[has_features, 'points_if_starts'] = if_starts
            merged.loc[has_features, 'start_probability'] = p_start

        _assign_rating(merged, start_weight)

        updated[position] = merged

    return updated


# These were the four official Premier League brand colours (yellow #ffcc29,
# cyan #04f5ff, magenta #e90052, purple #37003c) used verbatim. Replaced with
# the site's own teal-based palette: red/amber/orange are reserved for status
# meanings in the UI, so they're avoided here too.
POSITION_COLORS = {
    'Goalkeeper': '#00767A',
    'Defender': '#1E40AF',
    'Midfielder': '#2E9B57',
    'Forward': '#334155',
}


def plot_top_ratings(position_dfs, top_n=10, out_dir='../data/plots'):
    """Saves one bar chart per position showing the top N rated players."""
    os.makedirs(out_dir, exist_ok=True)
    plt.style.use('seaborn-v0_8-whitegrid')

    for position, df in position_dfs.items():
        top = df.sort_values('rating', ascending=False).head(top_n).iloc[::-1]
        if top.empty:
            continue

        fig, ax = plt.subplots(figsize=(9, 5.5))
        color = POSITION_COLORS.get(position, '#334155')
        bars = ax.barh(top['web_name'], top['rating'], color=color, edgecolor='none')

        for bar, rating in zip(bars, top['rating']):
            ax.text(bar.get_width() + 1, bar.get_y() + bar.get_height() / 2,
                     f'{rating:.0f}', va='center', fontsize=9, color='#333333')

        ax.set_xlabel('Rating (0-100)', fontsize=10)
        ax.set_title(f'Top {top_n} {position}s', fontsize=13, fontweight='bold', pad=12)
        ax.set_xlim(0, 110)
        ax.spines[['top', 'right']].set_visible(False)
        ax.tick_params(axis='y', labelsize=10)
        fig.tight_layout()

        path = f'{out_dir}/top_{position.lower()}s.png'
        fig.savefig(path, dpi=150)
        plt.close(fig)
        print(f"Saved chart to {path}")


def get_rated_position_dfs(position_dfs, mode='preseason', n_gameweeks=8):
    """Runs the full rating pipeline (form features, fixtures, model
    inference) for the given mode, then attaches per-gameweek predicted points
    for the next N gameweeks. Shared entry point for the CLI and the web app."""
    from fetch_data import get_fixtures, get_bootstrap_data

    model_bundles = load_models()
    form_features = build_current_form_features(mode=mode)

    fixtures_df = get_fixtures()
    fixture_features = build_fixture_features(fixtures_df, n_fixtures=3)

    # Asked of the same function build_current_form_features just used, so the
    # rating basis cannot disagree with the form source it is ranking. Reverts
    # to predicted_points on its own once three gameweeks exist - there is no
    # switch to remember to flip back.
    fallback = using_fallback_form(mode)
    start_weight = PRESEASON_START_WEIGHT if fallback else None
    rated = predict_ratings(position_dfs, model_bundles, form_features,
                            fixture_features, start_weight=start_weight)

    # Preseason only, and for the same reason the start weight is: the model's
    # spread is squeezed by the flattened form features, not by anything about
    # the players. Fitted before the per-gameweek pass so both use one map.
    calibrators = fit_calibrators(rated) if fallback else {}
    if calibrators:
        rated = calibrate_position_dfs(rated, calibrators, start_weight=start_weight)

    # Per-gameweek points for the 'next 3 GWs' columns on ratings/search.
    per_gw_features = build_per_gameweek_fixture_features(fixtures_df, n_fixtures=n_gameweeks)
    teams_df = pd.DataFrame(get_bootstrap_data()['teams'])
    team_id_to_short = dict(zip(teams_df['id'], teams_df['short_name']))
    rated = attach_per_gameweek_points(
        rated, model_bundles, form_features, per_gw_features,
        team_id_to_short=team_id_to_short, n_gameweeks=n_gameweeks,
        calibrators=calibrators,
    )

    # Last, and deliberately so: the model knows nothing about fitness, so a
    # player who is out still has a full projection until this pass zeroes it.
    # Every consumer of the rated pool reads it after this point.
    rated = zero_unavailable_points(rated, fixtures_df)

    return rated


if __name__ == '__main__':
    from pipeline import run_pipeline

    # ---- The one line you change ----
    MODE = 'preseason'   # switch to 'inseason' once real gameweeks exist

    data = run_pipeline()
    position_dfs = get_rated_position_dfs(data['position_dfs'], mode=MODE)

    for position, df in position_dfs.items():
        print(f"\n--- Top 10 {position}s ---")
        print(df[['web_name', 'rating', 'predicted_points']]
              .sort_values('rating', ascending=False)
              .head(10)
              .to_string(index=False))

    plot_top_ratings(position_dfs)