"""Pulls fresh FPL data and builds the position-split DataFrames the rest of
the project works from.
"""

import pandas as pd

import seasons
from fetch_data import get_bootstrap_data, refresh_gameweek_stats


def build_position_dfs(players_df, positions_df):
    """Splits players into one DataFrame per position."""
    return {
        name: players_df[players_df['element_type'] == pid].copy()
        for pid, name in zip(positions_df['id'], positions_df['singular_name'])
    }


def run_pipeline(pull_gameweek_history=False):
    """Runs the full data pull.

    pull_gameweek_history also refreshes this season's per-gameweek stats: one
    API call per player, so the nightly job does it, not app startup.
    """
    all_data = get_bootstrap_data()

    players_df = pd.DataFrame(all_data.get('elements') or [])
    teams_df = pd.DataFrame(all_data.get('teams') or [])
    positions_df = pd.DataFrame(all_data.get('element_types') or [])

    if players_df.empty or positions_df.empty:
        raise RuntimeError(
            "No player data available from the FPL API or any local cache - "
            f"expected a cache at {seasons.players_path()}.")

    if 'status' in players_df.columns:
        players_df = players_df[players_df['status'] != 'u']

    position_dfs = build_position_dfs(players_df, positions_df)

    gameweek_stats = None
    if pull_gameweek_history:
        gameweek_stats = refresh_gameweek_stats(players_df['id'])

    return {
        'players_df': players_df,
        'teams_df': teams_df,
        'positions_df': positions_df,
        'position_dfs': position_dfs,
        'gameweek_stats': gameweek_stats,
    }


if __name__ == '__main__':
    result = run_pipeline()
    print(f"Pulled {len(result['players_df'])} players across "
          f"{len(result['position_dfs'])} positions.")