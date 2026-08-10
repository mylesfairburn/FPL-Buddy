import os

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


def build_current_form_features(
    current_gw_path=None,
    fallback_path=None,
    current_players_path=None,
    previous_players_path=None,
    min_current_gameweeks=3,
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
        use_fallback = True
        if os.path.exists(current_gw_path):
            current = pd.read_csv(current_gw_path)
            if 'element' in current.columns:
                current = current.rename(columns={'element': 'player_id'})
            if current['round'].nunique() >= min_current_gameweeks:
                use_fallback = False

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