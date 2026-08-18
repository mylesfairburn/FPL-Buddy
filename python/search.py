import pandas as pd

def search_player(name, position_dfs):
    """Case-insensitive partial name search across all positions.

    regex=False is load-bearing: the argument comes straight off the query
    string, and .str.contains() would otherwise compile it as a pattern - "("
    raises re.error, and "(a+)+$" is a ReDoS against every name in the pool on
    an unauthenticated endpoint.

    A miss returns None silently. Logging the query would write attacker-chosen
    text to the log, forge lines through 
, and raise UnicodeEncodeError on
    a cp1252 console.
    """
    name = name.lower()
    results = []

    for position, df in position_dfs.items():
        matches = df[
            df['web_name'].str.lower().str.contains(name, na=False, regex=False) |
            df['first_name'].str.lower().str.contains(name, na=False, regex=False) |
            df['second_name'].str.lower().str.contains(name, na=False, regex=False)
        ]
        if not matches.empty:
            matches = matches.copy()
            matches['position'] = position
            results.append(matches)

    if not results:
        return None

    combined = pd.concat(results)
    cols = [c for c in ['web_name', 'first_name', 'second_name', 'team_code',
                        'position', 'rating', 'predicted_points', 'next_gameweeks']
            if c in combined.columns]
    return combined[cols]