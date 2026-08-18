"""Drops gameweek rows for players who are no longer in the players file.

A one-off cleanup for a season's cached stats, not part of the nightly run.
"""

import pandas as pd

import seasons

# Defaults to the last completed season.
SEASON = seasons.previous_season() or seasons.FIRST_TRAINING_SEASON


def align_player_sets(gameweek_stats, players):
    """Keeps only gameweek rows for players still present in the players file."""
    valid_ids = set(players['id'])

    before = gameweek_stats['player_id'].nunique()
    gameweek_stats = gameweek_stats[gameweek_stats['player_id'].isin(valid_ids)]
    after = gameweek_stats['player_id'].nunique()

    print(f"Filtered gameweek_stats: {before} -> {after} unique players")
    return gameweek_stats


def main(season=SEASON):
    stats_path = seasons.gameweek_stats_path(season)
    gameweek_stats = pd.read_csv(stats_path)
    players = pd.read_csv(seasons.players_path(season))

    if 'element' in gameweek_stats.columns:
        gameweek_stats = gameweek_stats.rename(columns={'element': 'player_id'})

    align_player_sets(gameweek_stats, players).to_csv(stats_path, index=False)
    print(f"Saved filtered gameweek_stats back to {stats_path}")


if __name__ == '__main__':
    main()
