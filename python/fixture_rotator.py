import itertools

import pandas as pd


def rank_rotation_pairs(rotation_df, difficulty_col, exclude_top_n=4, top_n=8,
                        min_improvement=0.0):
    """Ranks team pairs by how well their EASY WEEKS ALTERNATE, so a manager
    who owns a cheap player from each always has at least one good fixture to
    field. This is a deliberate rethink of the old 'lowest combined average'
    approach, which just surfaced the strongest one or two clubs as everyone's
    partner (they're the easier pick almost every week, so they 'carried' every
    pairing).

    Two ideas fix that:

    1. Drop the auto-starters. The `exclude_top_n` teams with the easiest
       fixtures overall (Man City, Arsenal, ...) are the ones you'd never bench
       anyway - they're set-and-forget starters, not rotation candidates. This
       also naturally biases the pool toward cheaper mid-table teams, which is
       where the interchangeable budget picks actually live.

    2. Score by the WORST week, then by genuine alternation. `worst_week` is the
       hardest fixture your better option still faces in any gameweek - keeping
       it low is exactly 'never stuck with no one to play'. Ties break on
       `improvement` (how much rotating beats just always starting the stronger
       single team), so a pair whose good weeks genuinely swap outranks one team
       quietly carrying a passenger.

    Lower difficulty = easier throughout (matches build_rotation_table)."""

    pivot = rotation_df.pivot_table(index='team_name', columns='event', values=difficulty_col)

    # Easiest overall = smallest average difficulty = the teams you always start.
    team_avg = pivot.mean(axis=1, skipna=True).sort_values()
    auto_starters = set(team_avg.head(exclude_top_n).index)
    pool = [t for t in pivot.index if t not in auto_starters]

    pairs = []
    for team_a, team_b in itertools.combinations(pool, 2):
        a_vals, b_vals = pivot.loc[team_a], pivot.loc[team_b]

        # Each week you'd play whichever of the two has the easier fixture.
        covered = pd.concat([a_vals, b_vals], axis=1).min(axis=1, skipna=True)
        if covered.isna().all():
            continue

        worst_week = covered.max()          # hardest your best option gets - lower is better
        avg_covered = covered.mean()
        best_solo_avg = min(a_vals.mean(skipna=True), b_vals.mean(skipna=True))
        improvement = best_solo_avg - avg_covered   # >0 = genuine rotation, not carrying

        pairs.append({
            'team_a': team_a,
            'team_b': team_b,
            'worst_week': worst_week,
            'avg_difficulty': avg_covered,
            'improvement': improvement,
            'gameweeks_covered': int(covered.count()),
        })

    pairs_df = pd.DataFrame(pairs)
    if pairs_df.empty:
        return pairs_df

    # Only keep pairs that actually rotate (both teams contribute at least a
    # little), then rank: never-stuck first, genuine alternation as tie-break.
    pairs_df = pairs_df[pairs_df['improvement'] >= min_improvement]
    pairs_df = pairs_df.sort_values(
        ['worst_week', 'improvement'], ascending=[True, False]
    ).head(top_n).reset_index(drop=True)
    return pairs_df


# category -> which rated position DataFrames to pull rotation players from.
# Which positions to recommend per rotation category, in display order.
# Defensive rotation -> a keeper pair and a defender pair; attacking rotation
# -> a midfielder pair and a forward pair. One player per team per position, so
# each row is a genuine same-slot swap (own both, start whoever has the easier week).
ROTATION_CATEGORY_POSITIONS = {
    'defender': ['Goalkeeper', 'Defender'],
    'attacker': ['Midfielder', 'Forward'],
}

POSITION_LABELS = {
    'Goalkeeper': 'GKP', 'Defender': 'DEF', 'Midfielder': 'MID', 'Forward': 'FWD',
}


def recommend_pair_players(pairs_df, position_dfs, rotation_df, category='defender'):
    """For each ranked rotation pair, surfaces the players to actually buy,
    broken out BY POSITION so each row is a same-slot swap: for a defensive
    pairing it's a GKP-vs-GKP and a DEF-vs-DEF recommendation; for attacking,
    MID-vs-MID and FWD-vs-FWD. You own the best-rated (affordable) player of that
    position from each club and start whichever has the easier fixture.

    Returns, per pair, a `position_pairs` list of
    {position, label, player_a, player_b} where each player carries web_name,
    team_code (for the shirt), rating and cost."""
    if pairs_df is None or pairs_df.empty:
        return []

    positions = ROTATION_CATEGORY_POSITIONS.get(category, ['Goalkeeper', 'Defender'])
    # rotation_df already carries team id -> short_name; reuse it so we don't
    # need teams_df here. Players key on team id, pairs key on short_name.
    short_to_id = dict(zip(rotation_df['team_name'], rotation_df['team']))

    def best_player(short_name, position):
        team_id = short_to_id.get(short_name)
        if team_id is None or position not in position_dfs:
            return None
        squad = position_dfs[position]
        squad = squad[(squad['team'] == team_id) & (squad['rating'] > 0)] \
            .sort_values('rating', ascending=False)
        if squad.empty:
            return None
        p = squad.iloc[0]
        return {
            'web_name': p.get('web_name'),
            'team_code': int(p['team_code']) if pd.notna(p.get('team_code')) else None,
            'rating': float(p['rating']) if pd.notna(p.get('rating')) else None,
            # now_cost is in tenths of a million (55 -> £5.5m)
            'cost': round(float(p['now_cost']) / 10, 1) if pd.notna(p.get('now_cost')) else None,
            'position': position,
        }

    recommendations = []
    for _, row in pairs_df.iterrows():
        position_pairs = []
        for position in positions:
            player_a = best_player(row['team_a'], position)
            player_b = best_player(row['team_b'], position)
            if player_a is None and player_b is None:
                continue
            position_pairs.append({
                'position': position,
                'label': POSITION_LABELS.get(position, position[:3].upper()),
                'player_a': player_a,
                'player_b': player_b,
            })
        recommendations.append({
            'team_a': row['team_a'],
            'team_b': row['team_b'],
            'position_pairs': position_pairs,
        })
    return recommendations


def strength_data_is_empty(teams_df):
    """FPL's strength columns are 0 across the board before the season's
    first matches are played."""
    strength_cols = ['strength_attack_home', 'strength_attack_away',
                      'strength_defence_home', 'strength_defence_away']
    return (teams_df[strength_cols].sum().sum() == 0)


def apply_previous_season_strength(teams_df, strength_df, elo_df=None, name_map=None):
    """Substitutes previous-season attack/defence strength for FPL's own
    (still-empty) columns. Newly promoted teams have no top-flight history to
    compute this from, so they fall back to ClubElo instead of a flat league
    average - Elo ratings stay comparable across divisions, so a promoted
    team's rating correctly comes out below an established Prem side's,
    rather than assuming they're merely average."""
    teams_df = teams_df.merge(strength_df, on='short_name', how='left')

    missing = teams_df[teams_df['attack_home'].isna()]['short_name'].tolist()

    if missing and elo_df is not None and name_map is not None:
        print(f"No previous-season Prem data for: {missing} - using ClubElo instead (likely newly promoted)")
        elo_df = elo_df[elo_df['Club'].isin(name_map.keys())].copy()
        elo_df['short_name'] = elo_df['Club'].map(name_map)
        elo_lookup = dict(zip(elo_df['short_name'], elo_df['Elo']))

        # Raw Elo (~1400-2000) is a completely different scale to the
        # goals-per-game numbers used elsewhere (~0.5-2.5) - filling directly
        # would make promoted teams look absurdly extreme once scaled. So
        # instead: find each promoted team's relative standing (z-score)
        # against the RETURNING teams' Elo, then place them at the same
        # relative standing on the goals-based scale. This is what actually
        # produces "below average", calibrated correctly rather than mixing scales.
        returning_mask = teams_df['attack_home'].notna()
        returning_short_names = teams_df.loc[returning_mask, 'short_name']

        returning_elo = returning_short_names.map(elo_lookup)
        elo_mean, elo_std = returning_elo.mean(), returning_elo.std()

        promoted_elo = teams_df.loc[~returning_mask, 'short_name'].map(elo_lookup)
        z_scores = (promoted_elo - elo_mean) / elo_std

        for col in ['attack_home', 'attack_away', 'defence_home', 'defence_away']:
            col_mean = teams_df.loc[returning_mask, col].mean()
            col_std = teams_df.loc[returning_mask, col].std()
            fallback_values = col_mean + z_scores * col_std
            teams_df[col] = teams_df[col].fillna(fallback_values)
    elif missing:
        print(f"No previous-season data for: {missing} - and no ClubElo data supplied, using league average")
        league_avg = strength_df[['attack_home', 'attack_away', 'defence_home', 'defence_away']].mean()
        for col, avg in league_avg.items():
            teams_df[col] = teams_df[col].fillna(avg)

    still_missing = teams_df[teams_df['attack_home'].isna()]['short_name'].tolist()
    if still_missing:
        print(f"Warning: still no strength data for {still_missing} - check ClubElo name mapping")

    # Scale onto roughly FPL's usual strength range so downstream difficulty
    # maths (originally built around FPL's own numbers) behaves consistently.
    scale = 220
    teams_df['strength_attack_home'] = teams_df['attack_home'] * scale
    teams_df['strength_attack_away'] = teams_df['attack_away'] * scale
    teams_df['strength_defence_home'] = teams_df['defence_home'] * scale
    teams_df['strength_defence_away'] = teams_df['defence_away'] * scale

    return teams_df


def build_rotation_table(fixtures_df, teams_df, n_gameweeks=8):
    """Builds one row per team per upcoming fixture, with defensive and
    attacking difficulty scored as a NET difference - a team's own
    defence/attack strength relative to the opponent's attack/defence, not
    the opponent's strength alone. A strong defence facing a weak attack
    should look easier than a weak defence facing that same attack."""

    upcoming = fixtures_df[fixtures_df['finished'] == False].sort_values('event')

    next_events = sorted(upcoming['event'].dropna().unique())[:n_gameweeks]
    upcoming = upcoming[upcoming['event'].isin(next_events)]

    team_id_to_name = dict(zip(teams_df['id'], teams_df['short_name']))
    strength = teams_df.set_index('id')[
        ['strength_attack_home', 'strength_attack_away',
         'strength_defence_home', 'strength_defence_away']
    ]

    rows = []
    for _, fixture in upcoming.iterrows():
        home, away, event = fixture['team_h'], fixture['team_a'], fixture['event']

        # Home team (X) vs away team (Y):
        # defensive_difficulty = how X's own defence compares to Y's attack
        # attacking_difficulty = how X's own attack compares to Y's defence
        # Positive = harder than average, negative = easier than average
        rows.append({
            'team': home, 'event': event, 'opponent': team_id_to_name.get(away, away),
            'was_home': True,
            'defensive_difficulty': strength.loc[away, 'strength_attack_away'] - strength.loc[home, 'strength_defence_home'],
            'attacking_difficulty': strength.loc[away, 'strength_defence_away'] - strength.loc[home, 'strength_attack_home'],
        })
        # Away team (Y) vs home team (X): mirror of the above
        rows.append({
            'team': away, 'event': event, 'opponent': team_id_to_name.get(home, home),
            'was_home': False,
            'defensive_difficulty': strength.loc[home, 'strength_attack_home'] - strength.loc[away, 'strength_defence_away'],
            'attacking_difficulty': strength.loc[home, 'strength_defence_home'] - strength.loc[away, 'strength_attack_away'],
        })

    df = pd.DataFrame(rows)
    df['team_name'] = df['team'].map(team_id_to_name)
    # team_code (not id) is what FPL's shirt image URLs key on.
    if 'code' in teams_df.columns:
        team_id_to_code = dict(zip(teams_df['id'], teams_df['code']))
        df['team_code'] = df['team'].map(team_id_to_code)
    return df


def display_rotation_table(rotation_df, difficulty_col, title):
    """Prints a team x gameweek pivot of difficulty, sorted easiest overall first."""
    pivot = rotation_df.pivot_table(index='team_name', columns='event', values=difficulty_col, aggfunc='mean')
    pivot = pivot.round(0)

    averages = rotation_df.groupby('team_name')[difficulty_col].mean().round(1)
    pivot['average'] = averages
    pivot = pivot.sort_values('average')

    print(f"\n--- {title} (lower = easier) ---")
    print(pivot.to_string())


def team_fixture_map(rotation_df, difficulty_col):
    """{team_name: {event: {opponent, difficulty}}} - so the table can show the
    actual opponent per gameweek, not just a raw difficulty number."""
    result = {}
    for team, group in rotation_df.groupby('team_name'):
        result[team] = {
            int(row['event']): {'opponent': row['opponent'], 'difficulty': row[difficulty_col]}
            for _, row in group.iterrows()
        }
    return result


def get_rotation_data(mode='preseason', n_gameweeks=8):
    """Fetches fixtures/teams and builds the rotation table for the given
    mode. Shared by the CLI and the web app so the logic only exists once."""
    from fetch_data import (get_fixtures, get_bootstrap_data, get_previous_season_fixture_strength,
                             get_clubelo_ratings, CLUBELO_NAME_MAP)

    fixtures_df = get_fixtures()
    all_data = get_bootstrap_data()
    teams_df = pd.DataFrame(all_data['teams'])

    if mode == 'preseason':
        strength_df = get_previous_season_fixture_strength()
        elo_df = get_clubelo_ratings()
        teams_df = apply_previous_season_strength(teams_df, strength_df, elo_df, CLUBELO_NAME_MAP)
    else:
        if strength_data_is_empty(teams_df):
            print("Warning: inseason mode selected, but FPL strength data is still empty - "
                  "results may look flat until real matches have been played")

    return build_rotation_table(fixtures_df, teams_df, n_gameweeks=n_gameweeks)


if __name__ == '__main__':
    # ---- The one line you change ----
    MODE = 'preseason'   # switch to 'inseason' once FPL's own strength data is populated

    rotation_df = get_rotation_data(mode=MODE, n_gameweeks=8)

    display_rotation_table(
        rotation_df, 'defensive_difficulty',
        'Defender Fixture Rotation (opponent attack strength)'
    )
    display_rotation_table(
        rotation_df, 'attacking_difficulty',
        'Attacker Fixture Rotation (opponent defence strength)'
    )

    print("\n--- Top Defensive Rotation Pairs (mid-table, easy weeks alternate) ---")
    print(rank_rotation_pairs(rotation_df, 'defensive_difficulty', top_n=8).to_string(index=False))

    print("\n--- Top Attacking Rotation Pairs (mid-table, easy weeks alternate) ---")
    print(rank_rotation_pairs(rotation_df, 'attacking_difficulty', top_n=8).to_string(index=False))