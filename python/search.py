import pandas as pd

def search_player(name, position_dfs):
    """Case-insensitive partial name search across all positions. Returns matching rows with their rating.

    regex=False is load-bearing, not a style choice. .str.contains() compiles
    its argument as a regular expression by default, and this argument comes
    straight off the query string - so "(" returned a 500 (re.error escaping as
    an unhandled exception), and a pattern like "(a+)+$" would have been
    compiled and run against every name in the pool on an unauthenticated,
    uncached endpoint. Treating the input as literal text closes both.

    A miss returns None silently. It used to print the query, which was three
    problems in one line: on a cp1252 console any non-Latin-1 character raised
    UnicodeEncodeError from inside the request and turned a normal empty search
    into a 500; a query containing \\r\\n forged extra log lines; and every miss
    on a public endpoint wrote attacker-chosen text to the log.
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