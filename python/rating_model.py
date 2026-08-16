import os
import re
from datetime import datetime, timedelta, timezone

import joblib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import seasons

MODEL_DIR = seasons.MODELS_DIR
ROLL_WINDOW = 3


def load_models(model_dir=MODEL_DIR):
    """Loads the {model, scaler, feature_cols} bundle saved by train_model.py, per position."""
    bundles = {}
    for filename in os.listdir(model_dir):
        if filename.endswith('_model.pkl'):
            position = filename.replace('_model.pkl', '').capitalize()
            bundles[position] = joblib.load(f'{model_dir}/{filename}')
    return bundles


def build_fixture_features(fixtures_df, n_fixtures=3):
    """For each team, averages difficulty and home ratio across their next
    N unplayed fixtures - this replaces the neutral placeholders with real
    upcoming schedule strength."""
    upcoming = fixtures_df[fixtures_df['finished'] == False].sort_values('event')

    team_features = []
    team_ids = pd.concat([upcoming['team_h'], upcoming['team_a']]).unique()

    for team_id in team_ids:
        home_fixtures = upcoming[upcoming['team_h'] == team_id][['event', 'team_h_difficulty']] \
            .rename(columns={'team_h_difficulty': 'difficulty'})
        home_fixtures['was_home'] = True

        away_fixtures = upcoming[upcoming['team_a'] == team_id][['event', 'team_a_difficulty']] \
            .rename(columns={'team_a_difficulty': 'difficulty'})
        away_fixtures['was_home'] = False

        team_fixtures = pd.concat([home_fixtures, away_fixtures]).sort_values('event').head(n_fixtures)

        if team_fixtures.empty:
            continue

        team_features.append({
            'team': team_id,
            # FPL's difficulty is 1 (easiest) to 5 (hardest) - inverted here
            # so it aligns with opponent_strength direction the model was
            # trained on (higher strength = harder opponent).
            'opponent_strength': team_fixtures['difficulty'].mean() * 220,  # roughly matches teams_df's strength scale
            'was_home': team_fixtures['was_home'].mean(),
        })

    return pd.DataFrame(team_features)


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
        # Same 1-5 -> strength scaling the model was trained against.
        tf['opponent_strength'] = tf['fpl_difficulty'] * 220
        rows.append(tf)

    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def attach_per_gameweek_points(position_dfs, model_bundles, form_features,
                               per_gw_features, team_id_to_short=None, n_gameweeks=3):
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
        model, scaler, feature_cols = bundle['model'], bundle['scaler'], bundle['feature_cols']

        # Build features on a CLEAN frame of just the join keys. df here is the
        # already-rated df, which already carries was_home/opponent_strength/roll
        # columns from predict_ratings - merging onto it directly would collide
        # and get suffixed (_x/_y), so feature_cols would go missing.
        work = df[['code', 'team']].drop_duplicates()
        work = work.merge(form_features, on='code', how='left')
        work = work.merge(per_gw_features, on='team', how='left')  # -> one row per (player, upcoming GW)

        if any(c not in work.columns for c in feature_cols):
            # Defensive: never crash startup over a missing feature column.
            df['next_gameweeks'] = [[] for _ in range(len(df))]
            updated[position] = df
            continue

        has_features = work[feature_cols].notna().all(axis=1)
        work['gw_points'] = np.nan
        if has_features.any():
            X = scaler.transform(work.loc[has_features, feature_cols])
            work.loc[has_features, 'gw_points'] = model.predict(X)

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

        new_gameweeks, new_points = [], []
        for _, row in df.iterrows():
            gws = row.get("next_gameweeks")
            gws = list(gws) if isinstance(gws, list) else []
            predicted = row.get("predicted_points")

            if not _blocking_chance(row):
                new_gameweeks.append(gws)
                new_points.append(predicted)
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

        df["next_gameweeks"] = pd.Series(new_gameweeks, index=df.index, dtype=object)
        if "predicted_points" in df.columns:
            df["predicted_points"] = new_points
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

    stat_cols = ['expected_goal_involvements', 'minutes', 'bonus']

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

    for col in stat_cols:
        source[col] = pd.to_numeric(source[col], errors='coerce')

    if use_fallback:
        # Full-season average, not just the last 3 GWs - avoids being skewed by
        # end-of-season rotation/dead rubbers, which is exactly what was
        # producing unreliable ratings (e.g. Haaland ranking low due to a
        # rested run-in, fringe players ranking high off one lucky game).
        # Filter out players with minimal minutes so a single substitute
        # cameo doesn't produce a misleadingly high average.
        minutes_played = source.groupby('code')['minutes'].sum()
        eligible_codes = minutes_played[minutes_played >= 180].index  # ~2 full games minimum
        source = source[source['code'].isin(eligible_codes)]
        form = source.groupby('code')[stat_cols].mean()
    else:
        source = source.sort_values(['code', 'round'])
        recent = source.groupby('code').tail(ROLL_WINDOW)
        form = recent.groupby('code')[stat_cols].mean()

    form.columns = [f'{c}_roll{ROLL_WINDOW}' for c in stat_cols]
    return form.reset_index()


def predict_ratings(position_dfs, model_bundles, form_features, fixture_features):
    """Predicts expected points per player, converts to a 0-100 rating per position."""
    updated = {}

    for position, df in position_dfs.items():
        df = df.copy()

        if position not in model_bundles:
            print(f"No trained model for {position}, skipping")
            df['rating'] = np.nan
            updated[position] = df
            continue

        bundle = model_bundles[position]
        model, scaler, feature_cols = bundle['model'], bundle['scaler'], bundle['feature_cols']

        merged = df.merge(form_features, on='code', how='left')
        # Real upcoming fixture difficulty/home-ratio, keyed by the player's team
        merged = merged.merge(fixture_features, on='team', how='left')

        has_features = merged[feature_cols].notna().all(axis=1)

        predicted_points = pd.Series(np.nan, index=merged.index)
        if has_features.any():
            X = scaler.transform(merged.loc[has_features, feature_cols])
            predicted_points.loc[has_features] = model.predict(X)

        merged['predicted_points'] = predicted_points

        # 0-100 rating via percentile rank within position - players with no
        # predictable form (e.g. never played) naturally rank at the bottom
        merged['rating'] = (merged['predicted_points'].rank(pct=True) * 100).round(1)
        merged.loc[merged['predicted_points'].isna(), 'rating'] = 0

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

    rated = predict_ratings(position_dfs, model_bundles, form_features, fixture_features)

    # Per-gameweek points for the 'next 3 GWs' columns on ratings/search.
    per_gw_features = build_per_gameweek_fixture_features(fixtures_df, n_fixtures=n_gameweeks)
    teams_df = pd.DataFrame(get_bootstrap_data()['teams'])
    team_id_to_short = dict(zip(teams_df['id'], teams_df['short_name']))
    rated = attach_per_gameweek_points(
        rated, model_bundles, form_features, per_gw_features,
        team_id_to_short=team_id_to_short, n_gameweeks=n_gameweeks,
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