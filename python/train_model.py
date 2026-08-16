"""Train the per-position points models.

Trains on EVERY season present from 2025-26 onwards (see seasons.py), not just
the most recent one. Each extra season is more evidence for the thing the model
is really estimating - how underlying stats convert into FPL points - so
rerunning this next August with 2026-27 on disk sharpens the weights without any
code change.

Two things make multi-season training different from just concatenating files:

  * Rolling features must never span a season boundary. Grouping by player_id
    alone would let a player's last three gameweeks of 2025-26 become the
    "recent form" for his 2026-27 opener, which is leakage across a summer of
    transfers and pre-season.
  * player_id is reassigned by FPL every season. `code` is the stable
    per-person identifier, so rows are keyed on that where it's available.

Run from python/:  python train_model.py


Why the model has the shape it does
-----------------------------------
This used to be one Ridge regression per position over five features, fitted to
`total_points` across every row. It was badly and systematically conservative -
a premium midfielder topped out around 5.2 predicted points - and defenders
crowded the top of the rankings. Three reasons, all structural:

  * **Half the rows are not appearances.** 52% of gameweek rows have zero
    minutes. Least squares over that mixture learns "will he play" and drags
    every projection toward the position mean: the overall mean is 1.57 points,
    but a player who actually starts averages 3.64. The single number the site
    printed was an average over a world in which the player is often not on the
    pitch.
  * **Nothing distinguished an elite player from an average one.** The features
    were three-gameweek rolling averages, which are mostly noise at that window.
    A player's established level - his season-to-date rate - was not a feature
    at all, so the model had to re-derive "this is Bruno Fernandes" from three
    games, every week, and never could.
  * **Defenders' actual points sources were invisible.** Clean sheets, goals
    conceded and `defensive_contribution` (a real scoring category worth 2
    points) were not features, while minutes - a defender's strength - was.
    So the model saw their floor and none of their ceiling.

So: two stages per position, which is the standard shape for a zero-inflated
target.

  1. **Will he play?** Two classifiers, P(starts) and P(plays at all).
  2. **What does he score if he does?** A Poisson regressor fitted ONLY on rows
     where the player started. Poisson rather than squared error because points
     are a non-negative count with a long right tail, and squared error is
     precisely what flattens that tail.

  E[points] = P(start) x E[points | start] + P(cameo) x (mean cameo points)

The two stages are worth keeping separate for a second reason: `E[points |
start]` is a genuinely useful number to show a reader ("6.4 if he starts, but
he's a 60% starter"), and a single blended figure cannot say that.

Honest about what this does not fix: the very top of the distribution is still
shrunk. Bruno Fernandes averaged 6.71 points per start across 2025-26 and the
model puts the best midfielders around 5-6.5. A conditional mean fitted to one
season will always pull the extremes in, and with 1,836 forward rows there is
not enough evidence to do otherwise. It is a real improvement on 5.2-for-
everyone, not a solved problem. See MODEL-NOTES.md.
"""

import os

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import (HistGradientBoostingClassifier,
                              HistGradientBoostingRegressor)
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error
from sklearn.preprocessing import StandardScaler

import seasons

pd.set_option('display.max_columns', None)

# Bumped whenever the bundle's shape or its feature list changes. rating_model
# refuses to load a bundle whose version it doesn't recognise rather than
# predicting nonsense from a stale pickle - see the note in save_models().
MODEL_VERSION = 2

# Everything that gets a rolling average, an expanding "level", or both. These
# are raw per-gameweek columns from FPL's own data; the derived names are built
# in build_rolling_features().
#
# total_points is in here deliberately, unlike the old model where it was
# excluded for double-counting its own components. That reasoning applied to a
# linear model splitting credit between a total and its ingredients. A tree
# splits on whichever is more informative, and a player's own recent points rate
# is the single best summary of how FPL's scoring - which includes bonus, an
# opaque function of BPS - actually treats him.
ROLLING_COLS = [
    # Attacking returns and the chances behind them.
    'expected_goals', 'expected_assists', 'expected_goal_involvements',
    'goals_scored', 'assists',
    # Defensive returns. `defensive_contribution` is a scoring category in its
    # own right from 2025-26 (2 points at 10 CBIT for a defender, 12 defensive
    # actions for everyone else), which the previous model could not see.
    'expected_goals_conceded', 'clean_sheets', 'goals_conceded',
    'defensive_contribution', 'tackles', 'recoveries',
    'clearances_blocks_interceptions', 'saves',
    # Playing time, and FPL's own composite ratings.
    'minutes', 'starts', 'bps', 'bonus',
    'ict_index', 'threat', 'creativity', 'influence',
    # The target's own history - see the note above.
    'total_points',
]

# Rates that only make sense per 90 minutes. A 60-minutes-a-week player is not
# two thirds as dangerous as a 90-minute one; he has two thirds of the time.
PER90_COLS = [
    'expected_goals', 'expected_assists', 'goals_scored', 'assists',
    'bps', 'threat', 'creativity', 'influence', 'total_points',
    'defensive_contribution', 'clearances_blocks_interceptions', 'recoveries',
]

# Short window for form, longer one for signal. Both are shifted before rolling
# so a gameweek never sees its own result.
ROLL_WINDOWS = (3, 6)
ROLL_WINDOW = 3          # kept: rating_model imports this name

# Hold out the run-in of the most recent season rather than a random split:
# the model is used to predict forwards in time, so it should be validated
# that way too. A random split would let a player's GW38 row train a model
# that's then tested on his GW20 row.
HOLDOUT_LAST_N_ROUNDS = 8

POSITION_MAP = {1: 'Goalkeeper', 2: 'Defender', 3: 'Midfielder', 4: 'Forward'}

# Shallow trees and a slow learning rate on purpose. One season is ~21,600 rows
# and only 1,836 of them are forwards; an unconstrained booster memorises that
# in seconds. Early stopping holds back its own validation slice on top.
_GBM_KWARGS = dict(max_depth=4, max_iter=400, learning_rate=0.06,
                   early_stopping=True, validation_fraction=0.15,
                   random_state=0)


def load_season(season):
    """One season's per-gameweek rows, tagged with the season and a stable
    player key. Returns None if the season has no usable stats file."""
    stats_path = seasons.gameweek_stats_path(season)
    players_path = seasons.players_path(season)
    if not os.path.exists(stats_path):
        return None
    try:
        stats = pd.read_csv(stats_path)
    except (pd.errors.EmptyDataError, OSError):
        return None
    if stats.empty or 'round' not in stats.columns:
        return None

    if 'element' in stats.columns and 'player_id' not in stats.columns:
        stats = stats.rename(columns={'element': 'player_id'})
    if 'player_id' not in stats.columns:
        return None

    stats['season'] = season
    stats['season_start'] = seasons.season_start_year(season)

    # Map to the season-stable `code` so a player's history lines up across
    # seasons even though FPL reissues `id` every year.
    try:
        players = pd.read_csv(players_path)
        id_to_code = dict(zip(players['id'], players['code']))
        stats['code'] = stats['player_id'].map(id_to_code)
        stats['element_type'] = stats['player_id'].map(
            dict(zip(players['id'], players['element_type'])))
    except (FileNotFoundError, OSError, KeyError):
        stats['code'] = np.nan
        stats['element_type'] = np.nan

    # Fall back to a season-scoped id where `code` is missing, so those rows
    # still get rolling features (just not linked across seasons).
    stats['player_key'] = stats['code'].fillna(
        stats['season_start'].astype(str) + '_' + stats['player_id'].astype(str))
    return stats


def load_all_seasons(season_list=None):
    season_list = season_list or seasons.training_seasons()
    frames = []
    for s in season_list:
        df = load_season(s)
        if df is None or df.empty:
            print(f"  {s}: no usable gameweek stats, skipping")
            continue
        print(f"  {s}: {len(df):,} rows, GW{int(df['round'].min())}-{int(df['round'].max())}")
        frames.append(df)
    if not frames:
        raise RuntimeError(
            "No season has usable gameweek stats. Expected at least "
            f"{seasons.gameweek_stats_path(seasons.FIRST_TRAINING_SEASON)}")
    return pd.concat(frames, ignore_index=True)


def build_rolling_features(df):
    """Form, level and rate features, all built from strictly-earlier rows.

    Every one of these is shift(1) before it is aggregated, so a gameweek's own
    result can never leak into its own features - only gameweeks strictly before
    it are used. Grouped by (player, season) so nothing carries across a summer
    break.

    Three families, because they answer different questions:

      _roll3 / _roll6   recent form - what he has done lately
      _level            season to date - the player he has been all year, which
                        is what separates an elite player from an average one
                        and is far less noisy than three games
      _per90            the same history as a rate, so playing time and
                        productivity are not confounded

    `games_seen` is the row count behind all of it. Without it the model cannot
    tell "zero, reliably" from "no evidence yet", which are very different for a
    new signing in August.
    """
    df = df.sort_values(['player_key', 'season_start', 'round']).copy()
    for col in ROLLING_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')
    group = df.groupby(['player_key', 'season_start'], sort=False)

    # Built into a dict and attached in one concat at the end. Assigning ~90
    # columns one at a time to a 21,000-row frame is what pandas means by
    # "highly fragmented", and it warns about it on every training run.
    derived = {}

    for col in ROLLING_COLS:
        if col not in df.columns:
            continue
        for window in ROLL_WINDOWS:
            derived[f'{col}_roll{window}'] = group[col].transform(
                lambda x: x.shift(1).rolling(window, min_periods=1).mean())
        derived[f'{col}_level'] = group[col].transform(
            lambda x: x.shift(1).expanding().mean())

    derived['games_seen'] = group.cumcount()

    # Per-90 rates off the same shifted history. Guarded at one full match of
    # minutes: below that the denominator is small enough that the rate is
    # noise, and NaN is the honest answer - HistGradientBoosting handles it
    # natively rather than needing an imputed value invented here.
    minutes_to_date = group['minutes'].transform(
        lambda x: x.shift(1).expanding().sum())
    for col in PER90_COLS:
        if col not in df.columns:
            continue
        total = group[col].transform(lambda x: x.shift(1).expanding().sum())
        derived[f'{col}_per90'] = pd.Series(
            np.where(minutes_to_date >= 90, total / (minutes_to_date / 90.0), np.nan),
            index=df.index)

    if 'value' in df.columns:
        # Price in millions. Not a performance stat - it is the market's opinion
        # of the player, known before the deadline, and it carries information
        # about expected role that the per-gameweek numbers take months to show.
        derived['price'] = pd.to_numeric(df['value'], errors='coerce') / 10.0

    return pd.concat([df, pd.DataFrame(derived, index=df.index)], axis=1)


def team_strength_ranks(season):
    """Each club's strength as a WITHIN-SEASON percentile rank, 0 (weakest) to
    1 (strongest), for both attack and defence, home and away.

    Ranks rather than raw values, and this is load-bearing rather than tidiness.
    FPL has already changed the scale under this code once: 2025-26's teams.csv
    carries `strength_overall_home` on a 975-1355 scale, and 2026-27's carries
    the same column on a 2-5 scale. A model trained on the first and asked to
    predict from the second was reading a difficulty-2 fixture as a value seven
    standard deviations below anything it had ever seen.

    A percentile rank within the season cannot break that way. Whatever units
    FPL invents next, the best team in the league ranks 1.0.

    Returns None when the season has no usable teams file, which the caller
    turns into NaN features rather than a crash.
    """
    try:
        teams = pd.read_csv(seasons.teams_path(season))
    except (FileNotFoundError, OSError):
        return None
    if 'id' not in teams.columns:
        return None

    out = pd.DataFrame({'team_id': teams['id']})
    if 'short_name' in teams.columns:
        out['short_name'] = teams['short_name']

    def ranked(column):
        """A column's percentile rank, or None when there is nothing to rank.

        A constant column carries no information, and ranking it would invent a
        spread that isn't there - every club would come out at 1.0."""
        if column not in teams.columns:
            return None
        values = pd.to_numeric(teams[column], errors='coerce')
        if values.notna().sum() == 0 or values.nunique(dropna=True) <= 1:
            return None
        return values.rank(pct=True)

    for side in ('home', 'away'):
        overall = ranked(f'strength_overall_{side}')
        for kind in ('overall', 'attack', 'defence'):
            column = ranked(f'strength_{kind}_{side}')
            # Fall back to the club's overall rating when FPL has not filled in
            # the split ones. It does that every preseason: 2026-27's teams.csv
            # ships strength_attack_* and strength_defence_* as all-zero for all
            # twenty clubs, with only strength_overall_* populated.
            #
            # Leaving them missing is worse than approximating them. The model
            # was fitted on a season where all six were real, so at inference
            # every player would take the same "unknown" branch and the fixture
            # would stop mattering at all - which is the exact bug this rewrite
            # exists to fix. A good team is a good team; using the one rating
            # FPL does publish keeps the ordering right even though it cannot
            # tell a great attack from a great defence.
            if column is None:
                column = overall
            out[f'{kind}_{side}_rank'] = np.nan if column is None else column
    return out


def add_fixture_context(df):
    """Opponent and own-team strength for each row, as within-season ranks.

    Both sides matter and they do different work. The opponent's attack rank is
    what threatens a defender's clean sheet; the opponent's defence rank is what
    stands between a forward and a goal; a player's OWN team's attack rank is
    most of why a midfielder at a good club outscores the same player at a bad
    one. The previous model had one blended opponent number and neither of the
    others.
    """
    pieces = []
    for season, chunk in df.groupby('season', sort=False):
        chunk = chunk.copy()
        ranks = team_strength_ranks(season)
        if ranks is None:
            print(f"  no teams.csv for {season}; strength features will be blank")
            for name in ('opp_strength_rank', 'opp_attack_rank', 'opp_defence_rank',
                         'own_attack_rank', 'own_defence_rank'):
                chunk[name] = np.nan
            pieces.append(chunk)
            continue

        was_home = chunk['was_home'].astype(bool).to_numpy()

        opp = ranks.add_prefix('opp_').rename(columns={'opp_team_id': 'opponent_team'})
        merged = chunk.merge(opp.drop(columns=[c for c in opp.columns
                                               if c == 'opp_short_name']),
                             on='opponent_team', how='left')

        def pick(frame, prefix, kind):
            """The opponent's away rating when the player is at home, and vice
            versa - a club's home strength is what it brings to its own ground."""
            home_col, away_col = f'{prefix}{kind}_home_rank', f'{prefix}{kind}_away_rank'
            if home_col not in frame.columns or away_col not in frame.columns:
                return np.nan
            return np.where(was_home, frame[away_col], frame[home_col])

        merged['opp_strength_rank'] = pick(merged, 'opp_', 'overall')
        merged['opp_attack_rank'] = pick(merged, 'opp_', 'attack')
        merged['opp_defence_rank'] = pick(merged, 'opp_', 'defence')

        # The player's own club, joined on the short name the stats file carries.
        if 'short_name' in ranks.columns and 'team' in merged.columns:
            own = ranks.drop(columns=['team_id']).add_prefix('own_').rename(
                columns={'own_short_name': 'team'})
            merged = merged.merge(own, on='team', how='left')
            merged['own_attack_rank'] = np.where(
                was_home, merged.get('own_attack_home_rank'), merged.get('own_attack_away_rank'))
            merged['own_defence_rank'] = np.where(
                was_home, merged.get('own_defence_home_rank'), merged.get('own_defence_away_rank'))
        else:
            merged['own_attack_rank'] = np.nan
            merged['own_defence_rank'] = np.nan

        pieces.append(merged)
    return pd.concat(pieces, ignore_index=True)


def attach_position(df):
    """Position from the season's own element list, falling back to the
    'position' string some exports already carry."""
    pos = df['element_type'].map(POSITION_MAP)
    if 'position' in df.columns:
        short = {'GK': 'Goalkeeper', 'GKP': 'Goalkeeper', 'DEF': 'Defender',
                 'MID': 'Midfielder', 'FWD': 'Forward'}
        pos = pos.fillna(df['position'].map(short)).fillna(df['position'])
    df['position'] = pos
    return df


def feature_columns(df):
    """Every derived feature present on the frame, in a stable order.

    Derived from the frame rather than hardcoded so a season missing one of
    FPL's newer columns (defensive_contribution did not exist before 2025-26)
    simply trains without it instead of raising.
    """
    derived = sorted(c for c in df.columns
                     if c.endswith(('_roll3', '_roll6', '_level', '_per90')))
    context = [c for c in ['price', 'was_home', 'games_seen',
                           'opp_strength_rank', 'opp_attack_rank', 'opp_defence_rank',
                           'own_attack_rank', 'own_defence_rank']
               if c in df.columns]
    return derived + context


def split_train_test(df):
    """Hold out the tail of the most recent season."""
    latest = df['season_start'].max()
    latest_rounds = df.loc[df['season_start'] == latest, 'round']
    cutoff = latest_rounds.max() - HOLDOUT_LAST_N_ROUNDS
    is_test = (df['season_start'] == latest) & (df['round'] > cutoff)
    return df[~is_test], df[is_test], latest, cutoff


def _spearman(a, b):
    """Rank correlation, via pandas rather than scipy.

    scipy is installed - scikit-learn depends on it - but importing it directly
    here would be a direct dependency satisfied by somebody else's, which is one
    `pip` resolution away from an ImportError. Same reasoning as the markupsafe
    note in requirements.txt.
    """
    return pd.Series(np.asarray(a)).corr(pd.Series(np.asarray(b)), method='spearman')


def calibration_table(y_true, y_pred, bins=10):
    """Mean predicted vs mean actual, by predicted decile.

    This is the report that exposes the failure the old model had and MAE hid.
    A model that shrinks everything toward the middle still scores well on MAE
    against a target that is 52% zeros - it wins by under-predicting - but its
    top decile reads "predicted 4.2, actual 5.9", and the top decile is the only
    part of the table anyone picks a squad from.
    """
    frame = pd.DataFrame({'y': np.asarray(y_true), 'p': np.asarray(y_pred)})
    if len(frame) < bins * 2 or frame['p'].nunique() < bins:
        return None
    frame['decile'] = pd.qcut(frame['p'].rank(method='first'), bins, labels=False)
    return frame.groupby('decile').agg(
        predicted=('p', 'mean'), actual=('y', 'mean'), n=('y', 'size'))


def _fit_ridge_benchmark(train, test):
    """The model this replaced, refitted on the same split, as the bar to clear.

    Kept in the file rather than deleted: "the new one is better" is a claim,
    and a claim about a model is worth exactly as much as the number printed
    next to it. This prints that number on every training run.
    """
    cols = ['expected_goal_involvements_roll3', 'minutes_roll3', 'bonus_roll3',
            'was_home', 'opp_strength_rank']
    if any(c not in train.columns for c in cols):
        return None
    fit = train.dropna(subset=cols + ['total_points'])
    check = test.dropna(subset=cols + ['total_points'])
    if fit.empty or check.empty:
        return None
    scaler = StandardScaler().fit(fit[cols])
    model = Ridge(alpha=1.0).fit(scaler.transform(fit[cols]), fit['total_points'])
    pred = model.predict(scaler.transform(check[cols]))
    return {
        'mae': mean_absolute_error(check['total_points'], pred),
        'spearman': _spearman(check['total_points'], pred),
        'max': float(np.max(pred)),
    }


def predict_bundle(bundle, frame):
    """Blended expected points, and the two parts it is built from.

    Returns (expected, if_starts, p_start). `expected` is what the site shows;
    `if_starts` is the more useful number to a reader deciding between a nailed
    starter and a rotation risk, and is kept separate for that reason.

    Lives here rather than in rating_model so training and inference cannot
    drift apart - the trainer scores its own holdout through this same function.
    """
    features = bundle['feature_cols']
    X = frame.reindex(columns=features)
    p_start = bundle['start_clf'].predict_proba(X)[:, 1]
    p_play = bundle['play_clf'].predict_proba(X)[:, 1]
    if_starts = np.clip(bundle['points_reg'].predict(X), 0, None)
    p_cameo = np.clip(p_play - p_start, 0, 1)
    expected = p_start * if_starts + p_cameo * bundle['cameo_points']
    return expected, if_starts, p_start


def train_position_models(df):
    feature_cols = feature_columns(df)
    df = df.dropna(subset=['total_points', 'position'])
    df = df[df['minutes'].notna()]
    df = df.assign(
        did_start=(df['starts'] == 1).astype(int) if 'starts' in df.columns
        else (df['minutes'] >= 60).astype(int),
        played=(df['minutes'] > 0).astype(int),
    )

    train, test, latest, cutoff = split_train_test(df)
    print(f"Training on {len(train):,} rows across {df['season'].nunique()} season(s) "
          f"with {len(feature_cols)} features; holding out {len(test):,} rows "
          f"(season starting {latest}, rounds > {cutoff}).\n")

    models = {}
    for position in sorted(df['position'].dropna().unique()):
        pos_train = train[train['position'] == position]
        pos_test = test[test['position'] == position]
        if pos_train.empty:
            print(f"Skipping {position} - no training rows")
            continue

        starters = pos_train[pos_train['did_start'] == 1]
        cameos = pos_train[(pos_train['played'] == 1) & (pos_train['did_start'] == 0)]
        if starters.empty:
            print(f"Skipping {position} - no rows where anyone started")
            continue

        start_clf = HistGradientBoostingClassifier(**_GBM_KWARGS).fit(
            pos_train[feature_cols], pos_train['did_start'])
        play_clf = HistGradientBoostingClassifier(**_GBM_KWARGS).fit(
            pos_train[feature_cols], pos_train['played'])
        # Clipped at zero for the Poisson loss, which is undefined below it.
        # Costs almost nothing: 74 of 11,244 appearances in 2025-26 scored
        # negative, and every one of them is a red card the features could not
        # have predicted anyway.
        points_reg = HistGradientBoostingRegressor(loss='poisson', **_GBM_KWARGS).fit(
            starters[feature_cols], starters['total_points'].clip(lower=0))
        cameo_points = float(cameos['total_points'].clip(lower=0).mean()) if not cameos.empty else 0.0

        bundle = {
            'version': MODEL_VERSION,
            'kind': 'two-stage-gbm',
            'start_clf': start_clf,
            'play_clf': play_clf,
            'points_reg': points_reg,
            'cameo_points': cameo_points,
            'feature_cols': feature_cols,
            'trained_on_seasons': sorted(df['season'].unique().tolist()),
            'train_rows': int(len(pos_train)),
            'start_rows': int(len(starters)),
        }

        print(f"--- {position} ---")
        print(f"Train rows: {len(pos_train):,} ({len(starters):,} starts)   "
              f"Test rows: {len(pos_test):,}")
        print(f"Mean points: all rows {pos_train['total_points'].mean():.2f}, "
              f"starts only {starters['total_points'].mean():.2f}   "
              f"cameo {cameo_points:.2f}")

        if not pos_test.empty:
            expected, if_starts, p_start = predict_bundle(bundle, pos_test)
            actual = pos_test['total_points']
            mae = mean_absolute_error(actual, expected)
            baseline = np.full(len(pos_test), pos_train['total_points'].mean())

            print(f"MAE: {mae:.3f}")
            print(f"MAE (predict-the-mean baseline): "
                  f"{mean_absolute_error(actual, baseline):.3f}")
            print(f"Rank correlation (Spearman): {_spearman(actual, expected):.3f}")

            ridge = _fit_ridge_benchmark(pos_train, pos_test)
            if ridge:
                print(f"Previous model (Ridge, 5 features): MAE {ridge['mae']:.3f}, "
                      f"rho {ridge['spearman']:.3f}, ceiling {ridge['max']:.2f}")

            print(f"Spread of expected points: "
                  f"p50 {np.percentile(expected, 50):.2f}  "
                  f"p90 {np.percentile(expected, 90):.2f}  "
                  f"p99 {np.percentile(expected, 99):.2f}  max {expected.max():.2f}")
            print(f"Spread of points-if-he-starts: "
                  f"p50 {np.percentile(if_starts, 50):.2f}  "
                  f"p90 {np.percentile(if_starts, 90):.2f}  "
                  f"max {if_starts.max():.2f}   "
                  f"(actual, on real starts: mean "
                  f"{pos_test.loc[pos_test.did_start == 1, 'total_points'].mean():.2f})")

            table = calibration_table(actual, expected)
            if table is not None:
                top = table.iloc[-1]
                print("Calibration by predicted decile (predicted vs actual):")
                for decile, row in table.iterrows():
                    print(f"   {decile:2d}  {row.predicted:6.2f}  {row.actual:6.2f}  "
                          f"{row.predicted - row.actual:+6.2f}")
                print(f"   top decile miss: {top.predicted - top.actual:+.2f}")

            bundle['metrics'] = {
                'mae': float(mae),
                'spearman': float(_spearman(actual, expected)),
                'ridge_mae': float(ridge['mae']) if ridge else None,
                'ridge_spearman': float(ridge['spearman']) if ridge else None,
            }
        print()

        models[position] = bundle
    return models


def save_models(model_bundles, out_dir=None):
    """Write one bundle per position.

    Note for anyone deploying this: in production FPL_DATA_ROOT points at a
    mounted volume, so these land on the volume and NOT in the image - and
    seasons.ensure_seeded() only ever copies files that are missing, so it will
    never overwrite a bundle that is already there. A deploy therefore ships new
    inference code against whatever pickle the volume already holds. That is
    what MODEL_VERSION exists to catch: rating_model.load_models() refuses a
    bundle from an older version rather than feeding it features it has never
    seen. After deploying a change to this file, retrain on the box:

        docker exec fpl-buddy python train_model.py
    """
    out_dir = out_dir or seasons.MODELS_DIR
    os.makedirs(out_dir, exist_ok=True)
    for position, bundle in model_bundles.items():
        path = f"{out_dir}/{position.lower()}_model.pkl"
        joblib.dump(bundle, path)
        print(f"Saved {position} model to {path}")


def main(season_list=None):
    season_list = season_list or seasons.training_seasons()
    print(f"Training seasons: {', '.join(season_list) if season_list else '(none found)'}")
    df = load_all_seasons(season_list)
    df = build_rolling_features(df)
    df = add_fixture_context(df)
    df = attach_position(df)
    models = train_position_models(df)
    save_models(models)
    return models


if __name__ == '__main__':
    main()
