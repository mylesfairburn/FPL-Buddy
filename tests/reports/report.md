# FPL Buddy — test report

**Run:** 2026-08-10 14:41:33 · **Duration:** 22.5s · **Python:** 3.14.2 · **Platform:** Windows 11

**534 passed · 5 failed · 539 total**

## Failures by severity

| Severity | Failures |
| --- | --- |
| critical | 0 |
| high | 0 |
| medium | 3 |
| low | 1 |
| info | 1 |

## By group

| Group | Passed | Failed | Total |
| --- | ---: | ---: | ---: |
| slugify | 48 | 0 | 48 |
| prose helpers | 20 | 0 | 20 |
| A-Z index | 3 | 0 | 3 |
| draft validation | 22 | 0 | 22 |
| storage detection | 10 | 0 | 10 |
| horizon points | 5 | 0 | 5 |
| page routes | 38 | 0 | 38 |
| tab routing | 12 | 0 | 12 |
| SEO metadata | 38 | 0 | 38 |
| robots / security.txt | 6 | 0 | 6 |
| sitemap.xml | 11 | 0 | 11 |
| player pages | 35 | 0 | 35 |
| A-Z index page | 3 | 0 | 3 |
| compression | 2 | 0 | 2 |
| static assets | 14 | 0 | 14 |
| API contract | 44 | 0 | 44 |
| API parameters | 19 | 0 | 19 |
| data shape | 6 | 0 | 6 |
| search | 15 | 0 | 15 |
| draft round-trip | 16 | 0 | 16 |
| AI endpoints | 7 | 0 | 7 |
| upstream proxies | 10 | 0 | 10 |
| SQL injection | 37 | 0 | 37 |
| regex injection | 10 | 0 | 10 |
| XSS | 15 | 0 | 15 |
| path traversal | 18 | 0 | 18 |
| authentication | 7 | 1 | 8 |
| security headers | 6 | 1 | 7 |
| information disclosure | 9 | 0 | 9 |
| input fuzzing | 31 | 0 | 31 |
| payload limits | 4 | 0 | 4 |
| third-party assets | 1 | 2 | 3 |
| privacy | 4 | 1 | 5 |
| HTTP methods | 4 | 0 | 4 |
| open redirect | 4 | 0 | 4 |

## Failures

| # | Severity | Group | Test | Input | Expected | Actual | Note |
| ---: | --- | --- | --- | --- | --- | --- | --- |
| 1 | **medium** | security headers | content-security-policy is set | `GET / response headers` | content-security-policy present | absent | KNOWN GAP, deliberately deferred: the templates carry inline <script> blocks, so a real policy needs nonces first. No known injection path today — autoescape is on and no template uses \|safe — so this is defence in depth, not an open hole |
| 2 | **medium** | third-party assets | SRI on cdn.jsdelivr.net | `https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css` | integrity= and crossorigin= present | <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet"> | without SRI, a compromised CDN executes arbitrary script on every page of the site |
| 3 | **medium** | privacy | any caller can read any id's draft | `GET /api/draft/<someone else's id>` | documented as unauthenticated | true | deliberate - FPL id is the whole identity - but it means one guessable integer exposes and overwrites a stored squad |
| 4 | **low** | third-party assets | no inline event handlers in the shell | `GET / markup` | no onclick=/onload=/onerror= attributes | [" onerror=\"this.style.display='none'\"", " onerror=\"this.style.display='none'\""] | two logo onerror fallbacks. Harmless in themselves, but each one needs a hash or a refactor before a CSP can be added, so this is part of the same job |
| 5 | **info** | authentication | FPL_REFRESH_TOKEN is configured in this environment | `os.environ['FPL_REFRESH_TOKEN']` | set in production; unset locally is expected | unset | not a code defect - a deployment requirement. Unset, both /api/refresh and /api/mode are anonymously callable and each costs a full pipeline run |

## All cases

| # | Result | Severity | Group | Test | Input | Expected | Actual |
| ---: | --- | --- | --- | --- | --- | --- | --- |
| 1 | PASS | high | slugify | slugify('Bukayo Saka') — the ordinary case | `'Bukayo Saka'` | bukayo-saka | bukayo-saka |
| 2 | PASS | high | slugify | slugify('Gabriel Jesus') — two words | `'Gabriel Jesus'` | gabriel-jesus | gabriel-jesus |
| 3 | PASS | high | slugify | slugify('Guéhi') — acute accent folds to ASCII | `'Guéhi'` | guehi | guehi |
| 4 | PASS | high | slugify | slugify('Ødegaard') — Ø has no NFKD decomposition | `'Ødegaard'` | odegaard | odegaard |
| 5 | PASS | high | slugify | slugify('Højbjerg') — ø mid-word | `'Højbjerg'` | hojbjerg | hojbjerg |
| 6 | PASS | high | slugify | slugify('Weiß') — ß expands to two letters | `'Weiß'` | weiss | weiss |
| 7 | PASS | high | slugify | slugify('Łukasz') — Ł has no decomposition | `'Łukasz'` | lukasz | lukasz |
| 8 | PASS | high | slugify | slugify('Đorđe') — Đ has no decomposition | `'Đorđe'` | dorde | dorde |
| 9 | PASS | high | slugify | slugify('Æneas') — Æ expands | `'Æneas'` | aeneas | aeneas |
| 10 | PASS | high | slugify | slugify("N'Golo Kanté") — apostrophe becomes a separator | `"N'Golo Kanté"` | n-golo-kante | n-golo-kante |
| 11 | PASS | high | slugify | slugify('Alexander-Arnold') — existing hyphen survives | `'Alexander-Arnold'` | alexander-arnold | alexander-arnold |
| 12 | PASS | high | slugify | slugify('  spaced  out  ') — leading/trailing space stripped | `' spaced out '` | spaced-out | spaced-out |
| 13 | PASS | high | slugify | slugify("O'Brien-Smith Jr.") — trailing punctuation trimmed | `"O'Brien-Smith Jr."` | o-brien-smith-jr | o-brien-smith-jr |
| 14 | PASS | high | slugify | slugify('...') — punctuation only collapses to empty | `'...'` |  |  |
| 15 | PASS | high | slugify | slugify('') — empty string | `''` |  |  |
| 16 | PASS | high | slugify | slugify(None) — None must not raise | `None` |  |  |
| 17 | PASS | high | slugify | slugify('ALL CAPS') — lowercased | `'ALL CAPS'` | all-caps | all-caps |
| 18 | PASS | high | slugify | slugify('van Dijk') — lowercase particle | `'van Dijk'` | van-dijk | van-dijk |
| 19 | PASS | high | slugify | slugify('Sánchez') — acute on a vowel | `'Sánchez'` | sanchez | sanchez |
| 20 | PASS | high | slugify | slugify('Müller') — umlaut drops rather than expanding to ue | `'Müller'` | muller | muller |
| 21 | PASS | high | slugify | slugify('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa') — long name is not truncated | `'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'` | aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa | aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa |
| 22 | PASS | high | slugify | slugify('Player 7') — digits survive | `'Player 7'` | player-7 | player-7 |
| 23 | PASS | high | slugify | slugify('<script>alert(1)</script>') — HTML cannot survive slugify | `'<script>alert(1)</script>'` | script-alert-1-script | script-alert-1-script |
| 24 | PASS | high | slugify | slugify('../../etc/passwd') — traversal cannot survive slugify | `'../../etc/passwd'` | etc-passwd | etc-passwd |
| 25 | PASS | high | slugify | slugify output charset for 'Bukayo Saka' | `'Bukayo Saka'` | only [a-z0-9-] | bukayo-saka |
| 26 | PASS | high | slugify | slugify output charset for 'Gabriel Jesus' | `'Gabriel Jesus'` | only [a-z0-9-] | gabriel-jesus |
| 27 | PASS | high | slugify | slugify output charset for 'Guéhi' | `'Guéhi'` | only [a-z0-9-] | guehi |
| 28 | PASS | high | slugify | slugify output charset for 'Ødegaard' | `'Ødegaard'` | only [a-z0-9-] | odegaard |
| 29 | PASS | high | slugify | slugify output charset for 'Højbjerg' | `'Højbjerg'` | only [a-z0-9-] | hojbjerg |
| 30 | PASS | high | slugify | slugify output charset for 'Weiß' | `'Weiß'` | only [a-z0-9-] | weiss |
| 31 | PASS | high | slugify | slugify output charset for 'Łukasz' | `'Łukasz'` | only [a-z0-9-] | lukasz |
| 32 | PASS | high | slugify | slugify output charset for 'Đorđe' | `'Đorđe'` | only [a-z0-9-] | dorde |
| 33 | PASS | high | slugify | slugify output charset for 'Æneas' | `'Æneas'` | only [a-z0-9-] | aeneas |
| 34 | PASS | high | slugify | slugify output charset for "N'Golo Kanté" | `"N'Golo Kanté"` | only [a-z0-9-] | n-golo-kante |
| 35 | PASS | high | slugify | slugify output charset for 'Alexander-Arnold' | `'Alexander-Arnold'` | only [a-z0-9-] | alexander-arnold |
| 36 | PASS | high | slugify | slugify output charset for '  spaced  out  ' | `' spaced out '` | only [a-z0-9-] | spaced-out |
| 37 | PASS | high | slugify | slugify output charset for "O'Brien-Smith Jr." | `"O'Brien-Smith Jr."` | only [a-z0-9-] | o-brien-smith-jr |
| 38 | PASS | high | slugify | slugify output charset for '...' | `'...'` | only [a-z0-9-] |  |
| 39 | PASS | high | slugify | slugify output charset for '' | `''` | only [a-z0-9-] |  |
| 40 | PASS | high | slugify | slugify output charset for None | `None` | only [a-z0-9-] |  |
| 41 | PASS | high | slugify | slugify output charset for 'ALL CAPS' | `'ALL CAPS'` | only [a-z0-9-] | all-caps |
| 42 | PASS | high | slugify | slugify output charset for 'van Dijk' | `'van Dijk'` | only [a-z0-9-] | van-dijk |
| 43 | PASS | high | slugify | slugify output charset for 'Sánchez' | `'Sánchez'` | only [a-z0-9-] | sanchez |
| 44 | PASS | high | slugify | slugify output charset for 'Müller' | `'Müller'` | only [a-z0-9-] | muller |
| 45 | PASS | high | slugify | slugify output charset for 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' | `'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'` | only [a-z0-9-] | aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa |
| 46 | PASS | high | slugify | slugify output charset for 'Player 7' | `'Player 7'` | only [a-z0-9-] | player-7 |
| 47 | PASS | high | slugify | slugify output charset for '<script>alert(1)</script>' | `'<script>alert(1)</script>'` | only [a-z0-9-] | script-alert-1-script |
| 48 | PASS | high | slugify | slugify output charset for '../../etc/passwd' | `'../../etc/passwd'` | only [a-z0-9-] | etc-passwd |
| 49 | PASS | low | prose helpers | _article('Arsenal') | `'Arsenal'` | an | an |
| 50 | PASS | low | prose helpers | _article('Everton') | `'Everton'` | an | an |
| 51 | PASS | low | prose helpers | _article('Ipswich') | `'Ipswich'` | an | an |
| 52 | PASS | low | prose helpers | _article('Aston Villa') | `'Aston Villa'` | an | an |
| 53 | PASS | low | prose helpers | _article('Chelsea') | `'Chelsea'` | a | a |
| 54 | PASS | low | prose helpers | _article('Liverpool') | `'Liverpool'` | a | a |
| 55 | PASS | low | prose helpers | _article('Wolves') | `'Wolves'` | a | a |
| 56 | PASS | low | prose helpers | _article('') | `''` | a | a |
| 57 | PASS | low | prose helpers | _article(None) | `None` | a | a |
| 58 | PASS | low | prose helpers | _fmt(3.0, 0) | `3.0, dp=0` | 3 | 3 |
| 59 | PASS | low | prose helpers | _fmt(3.456, 1) | `3.456, dp=1` | 3.5 | 3.5 |
| 60 | PASS | low | prose helpers | _fmt(1234.0, 0) separates thousands | `1234.0, dp=0` | 1,234 | 1,234 |
| 61 | PASS | low | prose helpers | _fmt(None) is falsy or dash | `None` | no crash, no 'nan' | null |
| 62 | PASS | medium | prose helpers | _fmt(nan) degrades gracefully | `float('nan')` | None or a dash — never a crash, never the text 'nan' | null |
| 63 | PASS | medium | prose helpers | _fmt(nan, 1) degrades gracefully | `float('nan'), dp=1` | no 'nan' in output | null |
| 64 | PASS | low | prose helpers | _plural(1, 'goal') | `1` | goal | goal |
| 65 | PASS | low | prose helpers | _plural(2, 'goal') | `2` | goals | goals |
| 66 | PASS | low | prose helpers | _plural(0, 'goal') | `0` | goals | goals |
| 67 | PASS | low | prose helpers | fixture_label returns a string for a normal fixture | `{'opponent': 'ARS', 'is_home': True, 'difficulty': 3}` | non-empty string | ARS |
| 68 | PASS | low | prose helpers | fixture_label tolerates a blank fixture | `{}` | no exception | TBC |
| 69 | PASS | medium | A-Z index | every player lands in a group | `[1, 2, 3]` | 3 players across the groups | [{"letter": "A", "players": [{"code": 3, "web_name": "Alexander-Arnold", "full_name": "Trent Alexander-Arno ... g": "bukayo-saka-1", "path": "/player/bukayo-saka-1", "pos": "MID", "team_name": "Arsenal", "cost": 5.0}]}] |
| 70 | PASS | medium | A-Z index | group keys are single A-Z letters | `['A', 'O', 'S']` | each key is one letter A-Z | ["A", "O", "S"] |
| 71 | PASS | medium | A-Z index | accented surname files under the folded letter | `Ødegaard` | filed under O not Ø | ["A", "O", "S"] |
| 72 | PASS | high | draft validation | a valid 15-man squad is accepted | `15 unique players, positions 1-15` | returns 15 cleaned picks | [{"element_id": 100, "position": 1, "is_captain": 0, "is_vice_captain": 0, "cost": null}, {"element_id": 10 ... 0, "cost": null}, {"element_id": 114, "position": 15, "is_captain": 0, "is_vice_captain": 0, "cost": null}] |
| 73 | PASS | high | draft validation | rejects: picks is not a list | `'nope'` | DraftError | DraftError: picks must be a list |
| 74 | PASS | high | draft validation | rejects: picks is None | `None` | DraftError | DraftError: picks must be a list |
| 75 | PASS | high | draft validation | rejects: picks is a dict | `{}` | DraftError | DraftError: picks must be a list |
| 76 | PASS | high | draft validation | rejects: 14 players | `14 picks` | DraftError | DraftError: a squad needs exactly 15 players, got 14 |
| 77 | PASS | high | draft validation | rejects: 16 players | `16 picks` | DraftError | DraftError: a squad needs exactly 15 players, got 16 |
| 78 | PASS | high | draft validation | rejects: 0 players | `[]` | DraftError | DraftError: a squad needs exactly 15 players, got 0 |
| 79 | PASS | high | draft validation | rejects: a pick is not an object | `['x'] * 15` | DraftError | DraftError: each pick must be an object |
| 80 | PASS | high | draft validation | rejects: a pick has no position | `15 picks, one missing position` | DraftError | DraftError: each pick needs a numeric element id and position |
| 81 | PASS | high | draft validation | rejects: position 0 | `position 0 present` | DraftError | DraftError: position 0 is outside 1-15 |
| 82 | PASS | high | draft validation | rejects: position 16 | `position 16 present` | DraftError | DraftError: position 16 is outside 1-15 |
| 83 | PASS | high | draft validation | rejects: negative position | `position -1` | DraftError | DraftError: position -1 is outside 1-15 |
| 84 | PASS | high | draft validation | rejects: duplicate position | `two picks in position 1` | DraftError | DraftError: two players in position 1 |
| 85 | PASS | high | draft validation | rejects: duplicate player | `same element_id twice` | DraftError | DraftError: player 100 appears twice |
| 86 | PASS | high | draft validation | rejects: non-numeric element id | `element_id 'abc'` | DraftError | DraftError: each pick needs a numeric element id and position |
| 87 | PASS | high | draft validation | rejects: two captains | `is_captain on two picks` | DraftError | DraftError: only one captain allowed |
| 88 | PASS | high | draft validation | rejects: two vice-captains | `is_vice_captain on two picks` | DraftError | DraftError: only one vice-captain allowed |
| 89 | PASS | high | draft validation | rejects: oversized payload | `20,000 picks` | DraftError | DraftError: a squad needs exactly 15 players, got 20000 |
| 90 | PASS | high | draft validation | captain flag normalised to 0/1 | `is_captain=1` | int 0 or 1 | 1 |
| 91 | PASS | high | draft validation | cost coerced to float | `cost='5.5'` | float 5.5 | 5.5 |
| 92 | PASS | high | draft validation | missing cost becomes None rather than raising | `no cost key` | None | null |
| 93 | PASS | critical | draft validation | SQL in element_id is rejected at the validator | `element_id = "1); DROP TABLE manager_draft;--"` | DraftError (int() refuses it) | DraftError |
| 94 | PASS | high | storage detection | classifies bind mount from an explicit -v | `/srv/fpl-companion/state` | bind | bind |
| 95 | PASS | high | storage detection | classifies named docker volume | `/var/lib/docker/volumes/fpl_state/_data` | volume | volume |
| 96 | PASS | high | storage detection | classifies anonymous docker volume | `/var/lib/docker/volumes/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/_data` | anon | anon |
| 97 | PASS | high | storage detection | a 63-char id is not treated as anonymous | `63 hex chars` | no match | null |
| 98 | PASS | high | storage detection | a 64-char id is treated as anonymous | `64 hex chars` | match | "<re.Match object; span=(0, 79), match='/volumes/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa>" |
| 99 | PASS | high | storage detection | a readable name is not anonymous | `/volumes/fpl_state/_data` | no match | null |
| 100 | PASS | high | storage detection | storage_kind(None) never raises | `None` | a verdict string, not an exception | image |
| 101 | PASS | high | storage detection | storage_kind('/nonexistent/path/x.db') never raises | `'/nonexistent/path/x.db'` | a verdict string, not an exception | image |
| 102 | PASS | high | storage detection | storage_kind('') never raises | `''` | a verdict string, not an exception | image |
| 103 | PASS | high | storage detection | storage_kind('relative.db') never raises | `'relative.db'` | a verdict string, not an exception | image |
| 104 | PASS | medium | horizon points | sums only the first n gameweeks | `10 gameweeks of 4.0 points, n=8` | 32.0 | 32.0 |
| 105 | PASS | medium | horizon points | n larger than the fixture list | `10 gameweeks of 4.0, n=20` | 40.0 | 40.0 |
| 106 | PASS | medium | horizon points | empty fixture list gives None | `{'next_gameweeks': []}` | null | null |
| 107 | PASS | medium | horizon points | all-None points gives None | `3 gameweeks with points=None` | null | null |
| 108 | PASS | low | horizon points | a record with no fixture key degrades gracefully | `{}` | None rather than KeyError | null |
| 109 | PASS | critical | page routes | GET / status | `GET /` | 200 | 200 |
| 110 | PASS | critical | page routes | GET / is HTML | `GET /` | text/html content-type | text/html; charset=utf-8 |
| 111 | PASS | critical | page routes | GET / has a non-trivial body | `GET /` | >2000 bytes of HTML | 13439 |
| 112 | PASS | critical | page routes | GET / document is not cached | `GET /` | no-cache on the HTML document | no-cache, must-revalidate |
| 113 | PASS | critical | page routes | GET /my-team status | `GET /my-team` | 200 | 200 |
| 114 | PASS | critical | page routes | GET /my-team is HTML | `GET /my-team` | text/html content-type | text/html; charset=utf-8 |
| 115 | PASS | critical | page routes | GET /my-team has a non-trivial body | `GET /my-team` | >2000 bytes of HTML | 24243 |
| 116 | PASS | critical | page routes | GET /my-team document is not cached | `GET /my-team` | no-cache on the HTML document | no-cache, must-revalidate |
| 117 | PASS | critical | page routes | GET /ai-teams status | `GET /ai-teams` | 200 | 200 |
| 118 | PASS | critical | page routes | GET /ai-teams is HTML | `GET /ai-teams` | text/html content-type | text/html; charset=utf-8 |
| 119 | PASS | critical | page routes | GET /ai-teams has a non-trivial body | `GET /ai-teams` | >2000 bytes of HTML | 24243 |
| 120 | PASS | critical | page routes | GET /ai-teams document is not cached | `GET /ai-teams` | no-cache on the HTML document | no-cache, must-revalidate |
| 121 | PASS | critical | page routes | GET /players status | `GET /players` | 200 | 200 |
| 122 | PASS | critical | page routes | GET /players is HTML | `GET /players` | text/html content-type | text/html; charset=utf-8 |
| 123 | PASS | critical | page routes | GET /players has a non-trivial body | `GET /players` | >2000 bytes of HTML | 24267 |
| 124 | PASS | critical | page routes | GET /players document is not cached | `GET /players` | no-cache on the HTML document | no-cache, must-revalidate |
| 125 | PASS | critical | page routes | GET /fixture-rotator status | `GET /fixture-rotator` | 200 | 200 |
| 126 | PASS | critical | page routes | GET /fixture-rotator is HTML | `GET /fixture-rotator` | text/html content-type | text/html; charset=utf-8 |
| 127 | PASS | critical | page routes | GET /fixture-rotator has a non-trivial body | `GET /fixture-rotator` | >2000 bytes of HTML | 24241 |
| 128 | PASS | critical | page routes | GET /fixture-rotator document is not cached | `GET /fixture-rotator` | no-cache on the HTML document | no-cache, must-revalidate |
| 129 | PASS | critical | page routes | GET /players/a-z status | `GET /players/a-z` | 200 | 200 |
| 130 | PASS | critical | page routes | GET /players/a-z is HTML | `GET /players/a-z` | text/html content-type | text/html; charset=utf-8 |
| 131 | PASS | critical | page routes | GET /players/a-z has a non-trivial body | `GET /players/a-z` | >2000 bytes of HTML | 139858 |
| 132 | PASS | critical | page routes | GET /players/a-z document is not cached | `GET /players/a-z` | no-cache on the HTML document | no-cache, must-revalidate |
| 133 | PASS | critical | page routes | GET /about status | `GET /about` | 200 | 200 |
| 134 | PASS | critical | page routes | GET /about is HTML | `GET /about` | text/html content-type | text/html; charset=utf-8 |
| 135 | PASS | critical | page routes | GET /about has a non-trivial body | `GET /about` | >2000 bytes of HTML | 10499 |
| 136 | PASS | critical | page routes | GET /about document is not cached | `GET /about` | no-cache on the HTML document | no-cache, must-revalidate |
| 137 | PASS | critical | page routes | GET /privacy status | `GET /privacy` | 200 | 200 |
| 138 | PASS | critical | page routes | GET /privacy is HTML | `GET /privacy` | text/html content-type | text/html; charset=utf-8 |
| 139 | PASS | critical | page routes | GET /privacy has a non-trivial body | `GET /privacy` | >2000 bytes of HTML | 9999 |
| 140 | PASS | critical | page routes | GET /privacy document is not cached | `GET /privacy` | no-cache on the HTML document | no-cache, must-revalidate |
| 141 | PASS | critical | page routes | GET /contact status | `GET /contact` | 200 | 200 |
| 142 | PASS | critical | page routes | GET /contact is HTML | `GET /contact` | text/html content-type | text/html; charset=utf-8 |
| 143 | PASS | critical | page routes | GET /contact has a non-trivial body | `GET /contact` | >2000 bytes of HTML | 7557 |
| 144 | PASS | critical | page routes | GET /contact document is not cached | `GET /contact` | no-cache on the HTML document | no-cache, must-revalidate |
| 145 | PASS | critical | page routes | unknown path 404s | `GET /nonexistent-page` | 404 | 404 |
| 146 | PASS | critical | page routes | trailing slash does not 500 | `GET /my-team/` | redirect or 404, never 5xx | 200 |
| 147 | PASS | high | tab routing | /my-team opens pane-team | `GET /my-team` | __INITIAL_PANE__ = "pane-team" | <!DOCTYPE html> <html lang="en"> <head> <meta charset="UTF-8"> <meta name="viewport" content="width=device- ... /static/kits.js?v=f68b2c3086"></script> <script src="/static/app.js?v=f68b2c3086"></script> </body> </html> |
| 148 | PASS | high | tab routing | /my-team marks its own nav link active | `GET /my-team` | the matching nav-link carries .active | <!DOCTYPE html> <html lang="en"> <head> <meta charset="UTF-8"> <meta name="viewport" content="width=device- ... /static/kits.js?v=f68b2c3086"></script> <script src="/static/app.js?v=f68b2c3086"></script> </body> </html> |
| 149 | PASS | high | tab routing | /my-team renders all four tab links as anchors | `GET /my-team` | 4 <a class=nav-link> elements | <!DOCTYPE html> <html lang="en"> <head> <meta charset="UTF-8"> <meta name="viewport" content="width=device- ... /static/kits.js?v=f68b2c3086"></script> <script src="/static/app.js?v=f68b2c3086"></script> </body> </html> |
| 150 | PASS | high | tab routing | /ai-teams opens pane-ai-teams | `GET /ai-teams` | __INITIAL_PANE__ = "pane-ai-teams" | <!DOCTYPE html> <html lang="en"> <head> <meta charset="UTF-8"> <meta name="viewport" content="width=device- ... /static/kits.js?v=f68b2c3086"></script> <script src="/static/app.js?v=f68b2c3086"></script> </body> </html> |
| 151 | PASS | high | tab routing | /ai-teams marks its own nav link active | `GET /ai-teams` | the matching nav-link carries .active | <!DOCTYPE html> <html lang="en"> <head> <meta charset="UTF-8"> <meta name="viewport" content="width=device- ... /static/kits.js?v=f68b2c3086"></script> <script src="/static/app.js?v=f68b2c3086"></script> </body> </html> |
| 152 | PASS | high | tab routing | /ai-teams renders all four tab links as anchors | `GET /ai-teams` | 4 <a class=nav-link> elements | <!DOCTYPE html> <html lang="en"> <head> <meta charset="UTF-8"> <meta name="viewport" content="width=device- ... /static/kits.js?v=f68b2c3086"></script> <script src="/static/app.js?v=f68b2c3086"></script> </body> </html> |
| 153 | PASS | high | tab routing | /players opens pane-players | `GET /players` | __INITIAL_PANE__ = "pane-players" | <!DOCTYPE html> <html lang="en"> <head> <meta charset="UTF-8"> <meta name="viewport" content="width=device- ... /static/kits.js?v=f68b2c3086"></script> <script src="/static/app.js?v=f68b2c3086"></script> </body> </html> |
| 154 | PASS | high | tab routing | /players marks its own nav link active | `GET /players` | the matching nav-link carries .active | <!DOCTYPE html> <html lang="en"> <head> <meta charset="UTF-8"> <meta name="viewport" content="width=device- ... /static/kits.js?v=f68b2c3086"></script> <script src="/static/app.js?v=f68b2c3086"></script> </body> </html> |
| 155 | PASS | high | tab routing | /players renders all four tab links as anchors | `GET /players` | 4 <a class=nav-link> elements | <!DOCTYPE html> <html lang="en"> <head> <meta charset="UTF-8"> <meta name="viewport" content="width=device- ... /static/kits.js?v=f68b2c3086"></script> <script src="/static/app.js?v=f68b2c3086"></script> </body> </html> |
| 156 | PASS | high | tab routing | /fixture-rotator opens pane-rotator | `GET /fixture-rotator` | __INITIAL_PANE__ = "pane-rotator" | <!DOCTYPE html> <html lang="en"> <head> <meta charset="UTF-8"> <meta name="viewport" content="width=device- ... /static/kits.js?v=f68b2c3086"></script> <script src="/static/app.js?v=f68b2c3086"></script> </body> </html> |
| 157 | PASS | high | tab routing | /fixture-rotator marks its own nav link active | `GET /fixture-rotator` | the matching nav-link carries .active | <!DOCTYPE html> <html lang="en"> <head> <meta charset="UTF-8"> <meta name="viewport" content="width=device- ... /static/kits.js?v=f68b2c3086"></script> <script src="/static/app.js?v=f68b2c3086"></script> </body> </html> |
| 158 | PASS | high | tab routing | /fixture-rotator renders all four tab links as anchors | `GET /fixture-rotator` | 4 <a class=nav-link> elements | <!DOCTYPE html> <html lang="en"> <head> <meta charset="UTF-8"> <meta name="viewport" content="width=device- ... /static/kits.js?v=f68b2c3086"></script> <script src="/static/app.js?v=f68b2c3086"></script> </body> </html> |
| 159 | PASS | high | SEO metadata | / title matches PAGES | `GET /` | FPL Buddy — Fantasy Premier League ratings, AI teams and fixture rotation | FPL Buddy — Fantasy Premier League ratings, AI teams and fixture rotation |
| 160 | PASS | high | SEO metadata | / has a description of usable length | `GET /` | 50-320 characters | 203 chars |
| 161 | PASS | high | SEO metadata | / canonical is absolute and correct | `GET /` | https://fpl.mfhost.co.uk/ | https://fpl.mfhost.co.uk/ |
| 162 | PASS | medium | SEO metadata | / has og:title | `GET /` | og:title present | <!DOCTYPE html> <html lang="en"> <head> <meta charset="UTF-8"> <meta name="viewport" content="width=device- ... n', function (e) { if (e.key === 'Enter') { e.preventDefault(); go(); } }); }()); </script> </body> </html> |
| 163 | PASS | high | SEO metadata | /my-team title matches PAGES | `GET /my-team` | My FPL team — squad, lineup and transfer analysis \| FPL Buddy | My FPL team — squad, lineup and transfer analysis \| FPL Buddy |
| 164 | PASS | high | SEO metadata | /my-team has a description of usable length | `GET /my-team` | 50-320 characters | 178 chars |
| 165 | PASS | high | SEO metadata | /my-team canonical is absolute and correct | `GET /my-team` | https://fpl.mfhost.co.uk/my-team | https://fpl.mfhost.co.uk/my-team |
| 166 | PASS | medium | SEO metadata | /my-team has og:title | `GET /my-team` | og:title present | <!DOCTYPE html> <html lang="en"> <head> <meta charset="UTF-8"> <meta name="viewport" content="width=device- ... /static/kits.js?v=f68b2c3086"></script> <script src="/static/app.js?v=f68b2c3086"></script> </body> </html> |
| 167 | PASS | high | SEO metadata | /ai-teams title matches PAGES | `GET /ai-teams` | AI Fantasy Premier League teams — AI Manager and best XV \| FPL Buddy | AI Fantasy Premier League teams — AI Manager and best XV \| FPL Buddy |
| 168 | PASS | high | SEO metadata | /ai-teams has a description of usable length | `GET /ai-teams` | 50-320 characters | 169 chars |
| 169 | PASS | high | SEO metadata | /ai-teams canonical is absolute and correct | `GET /ai-teams` | https://fpl.mfhost.co.uk/ai-teams | https://fpl.mfhost.co.uk/ai-teams |
| 170 | PASS | medium | SEO metadata | /ai-teams has og:title | `GET /ai-teams` | og:title present | <!DOCTYPE html> <html lang="en"> <head> <meta charset="UTF-8"> <meta name="viewport" content="width=device- ... /static/kits.js?v=f68b2c3086"></script> <script src="/static/app.js?v=f68b2c3086"></script> </body> </html> |
| 171 | PASS | high | SEO metadata | /players title matches PAGES | `GET /players` | FPL player ratings and predicted points \| FPL Buddy | FPL player ratings and predicted points \| FPL Buddy |
| 172 | PASS | high | SEO metadata | /players has a description of usable length | `GET /players` | 50-320 characters | 195 chars |
| 173 | PASS | high | SEO metadata | /players canonical is absolute and correct | `GET /players` | https://fpl.mfhost.co.uk/players | https://fpl.mfhost.co.uk/players |
| 174 | PASS | medium | SEO metadata | /players has og:title | `GET /players` | og:title present | <!DOCTYPE html> <html lang="en"> <head> <meta charset="UTF-8"> <meta name="viewport" content="width=device- ... /static/kits.js?v=f68b2c3086"></script> <script src="/static/app.js?v=f68b2c3086"></script> </body> </html> |
| 175 | PASS | high | SEO metadata | /fixture-rotator title matches PAGES | `GET /fixture-rotator` | FPL fixture rotation planner — best team pairs \| FPL Buddy | FPL fixture rotation planner — best team pairs \| FPL Buddy |
| 176 | PASS | high | SEO metadata | /fixture-rotator has a description of usable length | `GET /fixture-rotator` | 50-320 characters | 174 chars |
| 177 | PASS | high | SEO metadata | /fixture-rotator canonical is absolute and correct | `GET /fixture-rotator` | https://fpl.mfhost.co.uk/fixture-rotator | https://fpl.mfhost.co.uk/fixture-rotator |
| 178 | PASS | medium | SEO metadata | /fixture-rotator has og:title | `GET /fixture-rotator` | og:title present | <!DOCTYPE html> <html lang="en"> <head> <meta charset="UTF-8"> <meta name="viewport" content="width=device- ... /static/kits.js?v=f68b2c3086"></script> <script src="/static/app.js?v=f68b2c3086"></script> </body> </html> |
| 179 | PASS | high | SEO metadata | /players/a-z title matches PAGES | `GET /players/a-z` | Every Fantasy Premier League player A-Z \| FPL Buddy | Every Fantasy Premier League player A-Z \| FPL Buddy |
| 180 | PASS | high | SEO metadata | /players/a-z has a description of usable length | `GET /players/a-z` | 50-320 characters | 165 chars |
| 181 | PASS | high | SEO metadata | /players/a-z canonical is absolute and correct | `GET /players/a-z` | https://fpl.mfhost.co.uk/players/a-z | https://fpl.mfhost.co.uk/players/a-z |
| 182 | PASS | medium | SEO metadata | /players/a-z has og:title | `GET /players/a-z` | og:title present | <!DOCTYPE html> <html lang="en"> <head> <meta charset="UTF-8"> <meta name="viewport" content="width=device- ... et.d; el.setAttribute('href', 'mailto:' + address); el.textContent = address; }); </script> </body> </html> |
| 183 | PASS | high | SEO metadata | /about title matches PAGES | `GET /about` | About FPL Buddy — how the ratings and AI squads work | About FPL Buddy — how the ratings and AI squads work |
| 184 | PASS | high | SEO metadata | /about has a description of usable length | `GET /about` | 50-320 characters | 110 chars |
| 185 | PASS | high | SEO metadata | /about canonical is absolute and correct | `GET /about` | https://fpl.mfhost.co.uk/about | https://fpl.mfhost.co.uk/about |
| 186 | PASS | medium | SEO metadata | /about has og:title | `GET /about` | og:title present | <!DOCTYPE html> <html lang="en"> <head> <meta charset="UTF-8"> <meta name="viewport" content="width=device- ... et.d; el.setAttribute('href', 'mailto:' + address); el.textContent = address; }); </script> </body> </html> |
| 187 | PASS | high | SEO metadata | /privacy title matches PAGES | `GET /privacy` | Privacy policy — FPL Buddy | Privacy policy — FPL Buddy |
| 188 | PASS | high | SEO metadata | /privacy has a description of usable length | `GET /privacy` | 50-320 characters | 69 chars |
| 189 | PASS | high | SEO metadata | /privacy canonical is absolute and correct | `GET /privacy` | https://fpl.mfhost.co.uk/privacy | https://fpl.mfhost.co.uk/privacy |
| 190 | PASS | medium | SEO metadata | /privacy has og:title | `GET /privacy` | og:title present | <!DOCTYPE html> <html lang="en"> <head> <meta charset="UTF-8"> <meta name="viewport" content="width=device- ... et.d; el.setAttribute('href', 'mailto:' + address); el.textContent = address; }); </script> </body> </html> |
| 191 | PASS | high | SEO metadata | /contact title matches PAGES | `GET /contact` | Contact FPL Buddy | Contact FPL Buddy |
| 192 | PASS | high | SEO metadata | /contact has a description of usable length | `GET /contact` | 50-320 characters | 73 chars |
| 193 | PASS | high | SEO metadata | /contact canonical is absolute and correct | `GET /contact` | https://fpl.mfhost.co.uk/contact | https://fpl.mfhost.co.uk/contact |
| 194 | PASS | medium | SEO metadata | /contact has og:title | `GET /contact` | og:title present | <!DOCTYPE html> <html lang="en"> <head> <meta charset="UTF-8"> <meta name="viewport" content="width=device- ... et.d; el.setAttribute('href', 'mailto:' + address); el.textContent = address; }); </script> </body> </html> |
| 195 | PASS | high | SEO metadata | every page title is unique | `['/', '/my-team', '/ai-teams', '/players', '/fixture-rotator', '/players/a-z', '/about', '/privacy', '/contact']` | no two pages share a title | 9 |
| 196 | PASS | high | SEO metadata | every description is unique | `['/', '/my-team', '/ai-teams', '/players', '/fixture-rotator', '/players/a-z', '/about', '/privacy', '/contact']` | no two pages share a description | 9 |
| 197 | PASS | medium | robots / security.txt | robots.txt 200 | `GET /robots.txt` | 200 | 200 |
| 198 | PASS | high | robots / security.txt | robots.txt points at the sitemap | `GET /robots.txt` | Sitemap: https://fpl.mfhost.co.uk/sitemap.xml | User-agent: * Allow: / Disallow: /api/ Sitemap: https://fpl.mfhost.co.uk/sitemap.xml |
| 199 | PASS | medium | robots / security.txt | robots.txt disallows /api/ | `GET /robots.txt` | Disallow: /api/ | User-agent: * Allow: / Disallow: /api/ Sitemap: https://fpl.mfhost.co.uk/sitemap.xml |
| 200 | PASS | critical | robots / security.txt | robots.txt does not block the whole site | `GET /robots.txt` | no bare 'Disallow: /' | User-agent: * Allow: / Disallow: /api/ Sitemap: https://fpl.mfhost.co.uk/sitemap.xml |
| 201 | PASS | medium | robots / security.txt | security.txt 200 | `GET /.well-known/security.txt` | 200 | 200 |
| 202 | PASS | medium | robots / security.txt | security.txt has Contact and Expires | `GET /.well-known/security.txt` | both RFC 9116 mandatory fields | Contact: mailto:security@fpl.mfhost.co.uk Expires: 2027-08-10T13:41:12Z Preferred-Languages: en Canonical: https://fpl.mfhost.co.uk/.well-known/security.txt |
| 203 | PASS | critical | sitemap.xml | sitemap 200 | `GET /sitemap.xml` | 200 | 200 |
| 204 | PASS | high | sitemap.xml | sitemap content-type is XML | `GET /sitemap.xml` | application/xml | application/xml |
| 205 | PASS | critical | sitemap.xml | sitemap parses as XML | `GET /sitemap.xml` | valid urlset | 576 <loc> entries |
| 206 | PASS | high | sitemap.xml | sitemap URL count | `len(PAGES) + len(player_page_index())` | 576 | 576 |
| 207 | PASS | high | sitemap.xml | every loc is absolute | `all <loc> values` | all start with https://fpl.mfhost.co.uk | 576 |
| 208 | PASS | high | sitemap.xml | no duplicate URLs | `all <loc> values` | all unique | 0 |
| 209 | PASS | high | sitemap.xml | under the 50,000 URL limit | `len(locs)` | <= 50000 | 576 |
| 210 | PASS | high | sitemap.xml | no unescaped ampersands | `raw sitemap body` | no bare & outside an entity | <?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc ... 05</loc><lastmod>2026-08-10</lastmod><changefreq>weekly</changefreq><priority>0.6</priority></url></urlset> |
| 211 | PASS | high | sitemap.xml | priorities are in range | `all <priority> values` | 0.0-1.0 | checked |
| 212 | PASS | high | sitemap.xml | changefreq values are valid | `all <changefreq> values` | sitemaps.org vocabulary | checked |
| 213 | PASS | critical | sitemap.xml | sampled sitemap URLs all resolve 200 | `24 sampled URLs (head/middle/tail)` | every one returns 200 | all 200 |
| 214 | PASS | critical | player pages | player index is populated | `main.player_page_index()` | several hundred records | 567 |
| 215 | PASS | critical | player pages | slugs are unique | `all page records` | no collisions | 0 |
| 216 | PASS | high | player pages | every slug ends in its code | `all page records` | slug ends with the record's code | checked |
| 217 | PASS | high | player pages | every path is /player/<slug> | `all page records` | consistent paths | checked |
| 218 | PASS | high | player pages | GET /player/david-raya-martin-154561 | `GET /player/david-raya-martin-154561` | 200 | 200 |
| 219 | PASS | high | player pages | /player/david-raya-martin-154561 names the player | `/player/david-raya-martin-154561` | web_name appears in the body | Raya |
| 220 | PASS | high | player pages | /player/david-raya-martin-154561 carries Person JSON-LD | `/player/david-raya-martin-154561` | "@type": "Person" | checked |
| 221 | PASS | high | player pages | /player/david-raya-martin-154561 prose contains no 'nan' | `/player/david-raya-martin-154561` | no NaN leaking into sentences | checked |
| 222 | PASS | medium | player pages | /player/david-raya-martin-154561 prose contains no 'None' | `/player/david-raya-martin-154561` | no Python None in sentences | checked |
| 223 | PASS | high | player pages | GET /player/kepa-arrizabalaga-revuelta-109745 | `GET /player/kepa-arrizabalaga-revuelta-109745` | 200 | 200 |
| 224 | PASS | high | player pages | /player/kepa-arrizabalaga-revuelta-109745 names the player | `/player/kepa-arrizabalaga-revuelta-109745` | web_name appears in the body | Arrizabalaga |
| 225 | PASS | high | player pages | /player/kepa-arrizabalaga-revuelta-109745 carries Person JSON-LD | `/player/kepa-arrizabalaga-revuelta-109745` | "@type": "Person" | checked |
| 226 | PASS | high | player pages | /player/kepa-arrizabalaga-revuelta-109745 prose contains no 'nan' | `/player/kepa-arrizabalaga-revuelta-109745` | no NaN leaking into sentences | checked |
| 227 | PASS | medium | player pages | /player/kepa-arrizabalaga-revuelta-109745 prose contains no 'None' | `/player/kepa-arrizabalaga-revuelta-109745` | no Python None in sentences | checked |
| 228 | PASS | high | player pages | GET /player/illan-meslier-437495 | `GET /player/illan-meslier-437495` | 200 | 200 |
| 229 | PASS | high | player pages | /player/illan-meslier-437495 names the player | `/player/illan-meslier-437495` | web_name appears in the body | Meslier |
| 230 | PASS | high | player pages | /player/illan-meslier-437495 carries Person JSON-LD | `/player/illan-meslier-437495` | "@type": "Person" | checked |
| 231 | PASS | high | player pages | /player/illan-meslier-437495 prose contains no 'nan' | `/player/illan-meslier-437495` | no NaN leaking into sentences | checked |
| 232 | PASS | medium | player pages | /player/illan-meslier-437495 prose contains no 'None' | `/player/illan-meslier-437495` | no Python None in sentences | checked |
| 233 | PASS | high | player pages | GET /player/emiliano-martinez-romero-98980 | `GET /player/emiliano-martinez-romero-98980` | 200 | 200 |
| 234 | PASS | high | player pages | /player/emiliano-martinez-romero-98980 names the player | `/player/emiliano-martinez-romero-98980` | web_name appears in the body | Martinez |
| 235 | PASS | high | player pages | /player/emiliano-martinez-romero-98980 carries Person JSON-LD | `/player/emiliano-martinez-romero-98980` | "@type": "Person" | checked |
| 236 | PASS | high | player pages | /player/emiliano-martinez-romero-98980 prose contains no 'nan' | `/player/emiliano-martinez-romero-98980` | no NaN leaking into sentences | checked |
| 237 | PASS | medium | player pages | /player/emiliano-martinez-romero-98980 prose contains no 'None' | `/player/emiliano-martinez-romero-98980` | no Python None in sentences | checked |
| 238 | PASS | high | player pages | GET /player/marco-bizot-72147 | `GET /player/marco-bizot-72147` | 200 | 200 |
| 239 | PASS | high | player pages | /player/marco-bizot-72147 names the player | `/player/marco-bizot-72147` | web_name appears in the body | M.Bizot |
| 240 | PASS | high | player pages | /player/marco-bizot-72147 carries Person JSON-LD | `/player/marco-bizot-72147` | "@type": "Person" | checked |
| 241 | PASS | high | player pages | /player/marco-bizot-72147 prose contains no 'nan' | `/player/marco-bizot-72147` | no NaN leaking into sentences | checked |
| 242 | PASS | medium | player pages | /player/marco-bizot-72147 prose contains no 'None' | `/player/marco-bizot-72147` | no Python None in sentences | checked |
| 243 | PASS | high | player pages | non-canonical slug 301s | `GET /player/wrong-words-154561` | 301 | 301 |
| 244 | PASS | high | player pages | 301 points at the canonical path | `GET /player/wrong-words-154561` | /player/david-raya-martin-154561 | /player/david-raya-martin-154561 |
| 245 | PASS | high | player pages | 404 for a slug with no trailing number | `GET /player/no-digits-here` | 404 (or 307 for the empty case) | 404 |
| 246 | PASS | high | player pages | 404 for a slug with unknown code | `GET /player/999999999` | 404 (or 307 for the empty case) | 404 |
| 247 | PASS | high | player pages | 404 for a slug with empty slug | `GET /player/` | 404 (or 307 for the empty case) | 404 |
| 248 | PASS | high | player pages | 404 for a slug with just a hyphen | `GET /player/-` | 404 (or 307 for the empty case) | 404 |
| 249 | PASS | medium | A-Z index page | A-Z page 200 | `GET /players/a-z` | 200 | 200 |
| 250 | PASS | high | A-Z index page | A-Z links to every player | `GET /players/a-z` | 567 | 567 |
| 251 | PASS | high | A-Z index page | A-Z links all exist in the index | `GET /players/a-z` | no dangling links | checked |
| 252 | PASS | medium | compression | large API response is gzipped | `GET /api/all_players (accept gzip)` | content-encoding: gzip | gzip |
| 253 | PASS | medium | compression | HTML is gzipped | `GET / (accept gzip)` | content-encoding: gzip | gzip |
| 254 | PASS | high | static assets | /static/app.js served | `GET /static/app.js` | 200 | 200 |
| 255 | PASS | medium | static assets | /static/app.js is cacheable | `GET /static/app.js` | max-age=86400 | public, max-age=86400 |
| 256 | PASS | high | static assets | /static/style.css served | `GET /static/style.css` | 200 | 200 |
| 257 | PASS | medium | static assets | /static/style.css is cacheable | `GET /static/style.css` | max-age=86400 | public, max-age=86400 |
| 258 | PASS | high | static assets | /static/kits.js served | `GET /static/kits.js` | 200 | 200 |
| 259 | PASS | medium | static assets | /static/kits.js is cacheable | `GET /static/kits.js` | max-age=86400 | public, max-age=86400 |
| 260 | PASS | high | static assets | /static/favicon.png served | `GET /static/favicon.png` | 200 | 200 |
| 261 | PASS | medium | static assets | /static/favicon.png is cacheable | `GET /static/favicon.png` | max-age=86400 | public, max-age=86400 |
| 262 | PASS | high | static assets | /static/icon_180.png served | `GET /static/icon_180.png` | 200 | 200 |
| 263 | PASS | medium | static assets | /static/icon_180.png is cacheable | `GET /static/icon_180.png` | max-age=86400 | public, max-age=86400 |
| 264 | PASS | high | static assets | /static/style.css is version-stamped on / | `GET /` | /static/style.css?v=<token> | checked |
| 265 | PASS | high | static assets | /static/style.css is version-stamped on /my-team | `GET /my-team` | /static/style.css?v=<token> | checked |
| 266 | PASS | high | static assets | /static/app.js is version-stamped on /my-team | `GET /my-team` | /static/app.js?v=<token> | checked |
| 267 | PASS | high | static assets | /static/kits.js is version-stamped on /my-team | `GET /my-team` | /static/kits.js?v=<token> | checked |
| 268 | PASS | high | API contract | GET /api/ratings status | `GET /api/ratings` | 200 | 200 |
| 269 | PASS | high | API contract | GET /api/ratings returns JSON | `GET /api/ratings` | parseable JSON object | dict |
| 270 | PASS | high | API contract | GET /api/ratings has 'results' | `GET /api/ratings` | key 'results' present | ["results"] |
| 271 | PASS | high | API contract | GET /api/ratings contains no NaN/Infinity | `GET /api/ratings` | strictly valid JSON numbers | checked |
| 272 | PASS | high | API contract | GET /api/ratings?position=MID&top_n=5 status | `GET /api/ratings?position=MID&top_n=5` | 200 | 200 |
| 273 | PASS | high | API contract | GET /api/ratings?position=MID&top_n=5 returns JSON | `GET /api/ratings?position=MID&top_n=5` | parseable JSON object | dict |
| 274 | PASS | high | API contract | GET /api/ratings?position=MID&top_n=5 has 'results' | `GET /api/ratings?position=MID&top_n=5` | key 'results' present | ["results"] |
| 275 | PASS | high | API contract | GET /api/ratings?position=MID&top_n=5 contains no NaN/Infinity | `GET /api/ratings?position=MID&top_n=5` | strictly valid JSON numbers | checked |
| 276 | PASS | high | API contract | GET /api/all_players status | `GET /api/all_players` | 200 | 200 |
| 277 | PASS | high | API contract | GET /api/all_players returns JSON | `GET /api/all_players` | parseable JSON object | dict |
| 278 | PASS | high | API contract | GET /api/all_players has 'players' | `GET /api/all_players` | key 'players' present | ["players"] |
| 279 | PASS | high | API contract | GET /api/all_players contains no NaN/Infinity | `GET /api/all_players` | strictly valid JSON numbers | checked |
| 280 | PASS | high | API contract | GET /api/underperforming?top_n=5 status | `GET /api/underperforming?top_n=5` | 200 | 200 |
| 281 | PASS | high | API contract | GET /api/underperforming?top_n=5 returns JSON | `GET /api/underperforming?top_n=5` | parseable JSON object | dict |
| 282 | PASS | high | API contract | GET /api/underperforming?top_n=5 contains no NaN/Infinity | `GET /api/underperforming?top_n=5` | strictly valid JSON numbers | checked |
| 283 | PASS | high | API contract | GET /api/rotation?category=defender status | `GET /api/rotation?category=defender` | 200 | 200 |
| 284 | PASS | high | API contract | GET /api/rotation?category=defender returns JSON | `GET /api/rotation?category=defender` | parseable JSON object | dict |
| 285 | PASS | high | API contract | GET /api/rotation?category=defender has 'pairs' | `GET /api/rotation?category=defender` | key 'pairs' present | ["gameweeks", "teams", "pairs"] |
| 286 | PASS | high | API contract | GET /api/rotation?category=defender contains no NaN/Infinity | `GET /api/rotation?category=defender` | strictly valid JSON numbers | checked |
| 287 | PASS | high | API contract | GET /api/rotation?category=attacker status | `GET /api/rotation?category=attacker` | 200 | 200 |
| 288 | PASS | high | API contract | GET /api/rotation?category=attacker returns JSON | `GET /api/rotation?category=attacker` | parseable JSON object | dict |
| 289 | PASS | high | API contract | GET /api/rotation?category=attacker has 'pairs' | `GET /api/rotation?category=attacker` | key 'pairs' present | ["gameweeks", "teams", "pairs"] |
| 290 | PASS | high | API contract | GET /api/rotation?category=attacker contains no NaN/Infinity | `GET /api/rotation?category=attacker` | strictly valid JSON numbers | checked |
| 291 | PASS | high | API contract | GET /api/ai/status status | `GET /api/ai/status` | 200 | 200 |
| 292 | PASS | high | API contract | GET /api/ai/status returns JSON | `GET /api/ai/status` | parseable JSON object | dict |
| 293 | PASS | high | API contract | GET /api/ai/status has 'db' | `GET /api/ai/status` | key 'db' present | ["db", "data", "mode", "current_gameweek", "next_gameweek", "processed_deadlines", "known_managers"] |
| 294 | PASS | high | API contract | GET /api/ai/status contains no NaN/Infinity | `GET /api/ai/status` | strictly valid JSON numbers | checked |
| 295 | PASS | high | API contract | GET /api/ai/history status | `GET /api/ai/history` | 200 | 200 |
| 296 | PASS | high | API contract | GET /api/ai/history returns JSON | `GET /api/ai/history` | parseable JSON object | dict |
| 297 | PASS | high | API contract | GET /api/ai/history has 'snapshots' | `GET /api/ai/history` | key 'snapshots' present | ["available", "snapshots"] |
| 298 | PASS | high | API contract | GET /api/ai/history contains no NaN/Infinity | `GET /api/ai/history` | strictly valid JSON numbers | checked |
| 299 | PASS | high | API contract | GET /api/ai/manager/history status | `GET /api/ai/manager/history` | 200 | 200 |
| 300 | PASS | high | API contract | GET /api/ai/manager/history returns JSON | `GET /api/ai/manager/history` | parseable JSON object | dict |
| 301 | PASS | high | API contract | GET /api/ai/manager/history has 'history' | `GET /api/ai/manager/history` | key 'history' present | ["available", "history"] |
| 302 | PASS | high | API contract | GET /api/ai/manager/history contains no NaN/Infinity | `GET /api/ai/manager/history` | strictly valid JSON numbers | checked |
| 303 | PASS | high | API contract | GET /api/search?q=sa status | `GET /api/search?q=sa` | 200 | 200 |
| 304 | PASS | high | API contract | GET /api/search?q=sa returns JSON | `GET /api/search?q=sa` | parseable JSON object | dict |
| 305 | PASS | high | API contract | GET /api/search?q=sa has 'results' | `GET /api/search?q=sa` | key 'results' present | ["results"] |
| 306 | PASS | high | API contract | GET /api/search?q=sa contains no NaN/Infinity | `GET /api/search?q=sa` | strictly valid JSON numbers | checked |
| 307 | PASS | high | API contract | /api/ratings is publicly cacheable | `GET /api/ratings` | public, max-age=300 | public, max-age=300 |
| 308 | PASS | high | API contract | /api/all_players is publicly cacheable | `GET /api/all_players` | public, max-age=300 | public, max-age=300 |
| 309 | PASS | high | API contract | /api/underperforming is publicly cacheable | `GET /api/underperforming` | public, max-age=300 | public, max-age=300 |
| 310 | PASS | high | API contract | /api/rotation is publicly cacheable | `GET /api/rotation` | public, max-age=300 | public, max-age=300 |
| 311 | PASS | high | API contract | /api/news is uncacheable | `GET /api/news` | no-store | no-store, max-age=0 |
| 312 | PASS | high | API parameters | position=Goalkeeper returns rows | `GET /api/ratings?position=Goalkeeper` | non-empty results | 20 |
| 313 | PASS | high | API parameters | position=Defender returns rows | `GET /api/ratings?position=Defender` | non-empty results | 20 |
| 314 | PASS | high | API parameters | position=Midfielder returns rows | `GET /api/ratings?position=Midfielder` | non-empty results | 20 |
| 315 | PASS | high | API parameters | position=Forward returns rows | `GET /api/ratings?position=Forward` | non-empty results | 20 |
| 316 | PASS | high | API parameters | position=All returns rows | `GET /api/ratings?position=All` | non-empty results | 20 |
| 317 | PASS | high | API parameters | position=GK (short code) returns rows | `GET /api/ratings?position=GK` | non-empty results | 20 |
| 318 | PASS | high | API parameters | position=DEF (short code) returns rows | `GET /api/ratings?position=DEF` | non-empty results | 20 |
| 319 | PASS | high | API parameters | position=MID (short code) returns rows | `GET /api/ratings?position=MID` | non-empty results | 20 |
| 320 | PASS | high | API parameters | position=FWD (short code) returns rows | `GET /api/ratings?position=FWD` | non-empty results | 20 |
| 321 | PASS | high | API parameters | position=gk (short code) returns rows | `GET /api/ratings?position=gk` | non-empty results | 20 |
| 322 | PASS | high | API parameters | position=Mid (short code) returns rows | `GET /api/ratings?position=Mid` | non-empty results | 20 |
| 323 | PASS | high | API parameters | position=GKP (short code) returns rows | `GET /api/ratings?position=GKP` | non-empty results | 20 |
| 324 | PASS | high | API parameters | GK alias selects goalkeepers | `GET /api/ratings?position=GK&top_n=5` | every row's position is Goalkeeper | "{'Goalkeeper'}" |
| 325 | PASS | medium | API parameters | unknown position returns empty, not an error | `GET /api/ratings?position=NOPE` | [] | [] |
| 326 | PASS | medium | API parameters | top_n is respected | `GET /api/ratings?top_n=5` | 5 rows | 5 |
| 327 | PASS | medium | API parameters | top_n=0 does not error | `GET /api/ratings?top_n=0` | 200 | 200 |
| 328 | PASS | high | API parameters | negative top_n does not 500 | `GET /api/ratings?top_n=-5` | status < 500 | 200 |
| 329 | PASS | medium | API parameters | absurd top_n does not 500 | `GET /api/ratings?top_n=999999999` | status < 500 | 200 |
| 330 | PASS | medium | API parameters | non-numeric top_n is a 422 | `GET /api/ratings?top_n=abc` | 422 | 422 |
| 331 | PASS | high | data shape | ratings are sorted descending | `GET /api/ratings?top_n=50` | rating monotonically non-increasing | checked |
| 332 | PASS | medium | data shape | every row has a web_name | `GET /api/ratings?top_n=50` | no blank names | checked |
| 333 | PASS | high | data shape | all_players is the full pool | `GET /api/all_players` | >400 players | 567 |
| 334 | PASS | critical | data shape | every player has a season-stable code | `GET /api/all_players` | no missing 'code' | checked |
| 335 | PASS | high | data shape | every player carries its page path | `GET /api/all_players` | no missing 'path' | none missing |
| 336 | PASS | critical | data shape | player codes are unique | `GET /api/all_players` | no duplicates | 0 |
| 337 | PASS | high | search | search short prefix: 'sa' | `GET /api/search?q='sa'` | 200 with a results list | 200 |
| 338 | PASS | high | search | search full surname: 'Salah' | `GET /api/search?q='Salah'` | 200 with a results list | 200 |
| 339 | PASS | high | search | search uppercase: 'SALAH' | `GET /api/search?q='SALAH'` | 200 with a results list | 200 |
| 340 | PASS | high | search | search no match: 'zzzzzzzz' | `GET /api/search?q='zzzzzzzz'` | 200 with a results list | 200 |
| 341 | PASS | high | search | search accented single char: 'é' | `GET /api/search?q='é'` | 200 with a results list | 200 |
| 342 | PASS | high | search | search SQL-ish: "' OR 1=1 --" | `GET /api/search?q="' OR 1=1 --"` | 200 with a results list | 200 |
| 343 | PASS | high | search | search HTML-ish: '<script>' | `GET /api/search?q='<script>'` | 200 with a results list | 200 |
| 344 | PASS | high | search | search SQL wildcard: '%' | `GET /api/search?q='%'` | 200 with a results list | 200 |
| 345 | PASS | high | search | search SQL single-char wildcard: '_' | `GET /api/search?q='_'` | 200 with a results list | 200 |
| 346 | PASS | high | search | search single letter: 'o' | `GET /api/search?q='o'` | 200 with a results list | 200 |
| 347 | PASS | high | search | search common particle: 'van' | `GET /api/search?q='van'` | 200 with a results list | 200 |
| 348 | PASS | medium | search | search with no q is a 422 | `GET /api/search` | 422 | 422 |
| 349 | PASS | low | search | a real surname finds someone | `GET /api/search?q=salah` | at least one result | 0 |
| 350 | PASS | high | search | a broad query still returns 200 | `GET /api/search?q=a` | 200 | 200 |
| 351 | PASS | high | search | absent predictions serialise as null, not NaN | `GET /api/search?q=a` | null in the JSON body | 188 of 474 rows are null |
| 352 | PASS | high | draft round-trip | unsaved id reports unavailable | `GET /api/draft/999999999` | false | false |
| 353 | PASS | high | draft round-trip | save returns 200 | `POST /api/draft/999999999 (15 valid picks)` | 200 | 200 |
| 354 | PASS | high | draft round-trip | save reports 15 picks stored | `POST body` | 15 | 15 |
| 355 | PASS | high | draft round-trip | saved draft reads back | `GET /api/draft/999999999` | true | true |
| 356 | PASS | high | draft round-trip | squad has 15 players | `GET /api/draft/999999999` | 15 | 15 |
| 357 | PASS | high | draft round-trip | captain survived the round-trip | `GET /api/draft/999999999` | exactly one is_captain | checked |
| 358 | PASS | high | draft round-trip | first 11 are marked starting | `GET /api/draft/999999999` | 11 starters | 11 |
| 359 | PASS | high | draft round-trip | re-saving replaces rather than appends | `POST twice, then GET` | 15 | 15 |
| 360 | PASS | high | draft round-trip | rejects empty picks | `POST /api/draft/999999999 empty picks` | 400 Bad Request | 400 |
| 361 | PASS | high | draft round-trip | rejects 14 picks | `POST /api/draft/999999999 14 picks` | 400 Bad Request | 400 |
| 362 | PASS | high | draft round-trip | rejects duplicate player | `POST /api/draft/999999999 duplicate player` | 400 Bad Request | 400 |
| 363 | PASS | high | draft round-trip | rejects picks not a list | `POST /api/draft/999999999 picks not a list` | 400 Bad Request | 400 |
| 364 | PASS | high | draft round-trip | rejects no picks key | `POST /api/draft/999999999 no picks key` | 400 Bad Request | 400 |
| 365 | PASS | high | draft round-trip | rejects two captains | `POST /api/draft/999999999 two captains` | 400 Bad Request | 400 |
| 366 | PASS | high | draft round-trip | delete returns 200 | `DELETE /api/draft/999999999` | 200 | 200 |
| 367 | PASS | high | draft round-trip | deleted draft is gone | `GET /api/draft/999999999` | false | false |
| 368 | PASS | high | AI endpoints | GET /api/ai/best_xv status | `GET /api/ai/best_xv` | 200 | 200 |
| 369 | PASS | high | AI endpoints | /api/ai/best_xv states availability | `GET /api/ai/best_xv` | 'available' key present | ["available", "detail"] |
| 370 | PASS | high | AI endpoints | GET /api/ai/manager status | `GET /api/ai/manager` | 200 | 200 |
| 371 | PASS | high | AI endpoints | /api/ai/manager states availability | `GET /api/ai/manager` | 'available' key present | ["available", "detail"] |
| 372 | PASS | medium | AI endpoints | best_xv accepts a gameweek | `GET /api/ai/best_xv?gameweek=1` | 200 | 200 |
| 373 | PASS | high | AI endpoints | negative gameweek does not 500 | `GET /api/ai/best_xv?gameweek=-1` | status < 500 | 200 |
| 374 | PASS | high | AI endpoints | zero budget does not 500 | `GET /api/ai/best_xv?budget=0` | status < 500, unavailable is fine | 200 |
| 375 | PASS | high | upstream proxies | GET /api/live/1 never 5xx | `GET /api/live/1` | status < 500 | 200 |
| 376 | PASS | medium | upstream proxies | GET /api/live/1 states availability | `GET /api/live/1` | 'available' key | ["available", "gameweek", "detail"] |
| 377 | PASS | high | upstream proxies | GET /api/live/38 never 5xx | `GET /api/live/38` | status < 500 | 200 |
| 378 | PASS | medium | upstream proxies | GET /api/live/38 states availability | `GET /api/live/38` | 'available' key | ["available", "gameweek", "detail"] |
| 379 | PASS | high | upstream proxies | GET /api/live/0 never 5xx | `GET /api/live/0` | status < 500 | 200 |
| 380 | PASS | medium | upstream proxies | GET /api/live/0 states availability | `GET /api/live/0` | 'available' key | ["available", "gameweek", "detail"] |
| 381 | PASS | high | upstream proxies | GET /api/live/9999 never 5xx | `GET /api/live/9999` | status < 500 | 200 |
| 382 | PASS | medium | upstream proxies | GET /api/live/9999 states availability | `GET /api/live/9999` | 'available' key | ["available", "gameweek", "detail"] |
| 383 | PASS | high | upstream proxies | player summary never 5xx | `GET /api/player/1` | status < 500 | 200 |
| 384 | PASS | high | upstream proxies | news never 5xx | `GET /api/news?limit=5` | status < 500 | 200 |
| 385 | PASS | critical | SQL injection | int path param rejects '1 OR 1=1' | `GET /api/draft/1 OR 1=1` | 422 — handler never runs | 422 |
| 386 | PASS | critical | SQL injection | int path param rejects '1 OR 1=1' | `GET /api/league/1 OR 1=1` | 422 — handler never runs | 422 |
| 387 | PASS | critical | SQL injection | int path param rejects '1 OR 1=1' | `GET /api/player/1 OR 1=1` | 422 — handler never runs | 422 |
| 388 | PASS | critical | SQL injection | int path param rejects '1 OR 1=1' | `GET /api/manager/1 OR 1=1/history` | 422 — handler never runs | 422 |
| 389 | PASS | critical | SQL injection | int path param rejects '1; DROP TABLE manager_draft;--' | `GET /api/draft/1; DROP TABLE manager_draft;--` | 422 — handler never runs | 422 |
| 390 | PASS | critical | SQL injection | int path param rejects '1; DROP TABLE manager_draft;--' | `GET /api/league/1; DROP TABLE manager_draft;--` | 422 — handler never runs | 422 |
| 391 | PASS | critical | SQL injection | int path param rejects '1; DROP TABLE manager_draft;--' | `GET /api/player/1; DROP TABLE manager_draft;--` | 422 — handler never runs | 422 |
| 392 | PASS | critical | SQL injection | int path param rejects '1; DROP TABLE manager_draft;--' | `GET /api/manager/1; DROP TABLE manager_draft;--/history` | 422 — handler never runs | 422 |
| 393 | PASS | critical | SQL injection | int path param rejects "' OR '1'='1" | `GET /api/draft/' OR '1'='1` | 422 — handler never runs | 422 |
| 394 | PASS | critical | SQL injection | int path param rejects "' OR '1'='1" | `GET /api/league/' OR '1'='1` | 422 — handler never runs | 422 |
| 395 | PASS | critical | SQL injection | int path param rejects "' OR '1'='1" | `GET /api/player/' OR '1'='1` | 422 — handler never runs | 422 |
| 396 | PASS | critical | SQL injection | int path param rejects "' OR '1'='1" | `GET /api/manager/' OR '1'='1/history` | 422 — handler never runs | 422 |
| 397 | PASS | critical | SQL injection | int path param rejects "1' UNION SELECT name FROM sqlite_master--" | `GET /api/draft/1' UNION SELECT name FROM sqlite_master--` | 422 — handler never runs | 422 |
| 398 | PASS | critical | SQL injection | int path param rejects "1' UNION SELECT name FROM sqlite_master--" | `GET /api/league/1' UNION SELECT name FROM sqlite_master--` | 422 — handler never runs | 422 |
| 399 | PASS | critical | SQL injection | int path param rejects "1' UNION SELECT name FROM sqlite_master--" | `GET /api/player/1' UNION SELECT name FROM sqlite_master--` | 422 — handler never runs | 422 |
| 400 | PASS | critical | SQL injection | int path param rejects "1' UNION SELECT name FROM sqlite_master--" | `GET /api/manager/1' UNION SELECT name FROM sqlite_master--/history` | 422 — handler never runs | 422 |
| 401 | PASS | critical | SQL injection | int path param rejects '1/**/OR/**/1=1' | `GET /api/draft/1/**/OR/**/1=1` | 404 or 422 — handler never runs | 404 |
| 402 | PASS | critical | SQL injection | int path param rejects '1/**/OR/**/1=1' | `GET /api/league/1/**/OR/**/1=1` | 404 or 422 — handler never runs | 404 |
| 403 | PASS | critical | SQL injection | int path param rejects '1/**/OR/**/1=1' | `GET /api/player/1/**/OR/**/1=1` | 404 or 422 — handler never runs | 404 |
| 404 | PASS | critical | SQL injection | int path param rejects '1/**/OR/**/1=1' | `GET /api/manager/1/**/OR/**/1=1/history` | 404 or 422 — handler never runs | 404 |
| 405 | PASS | critical | SQL injection | int path param rejects "admin'--" | `GET /api/draft/admin'--` | 422 — handler never runs | 422 |
| 406 | PASS | critical | SQL injection | int path param rejects "admin'--" | `GET /api/league/admin'--` | 422 — handler never runs | 422 |
| 407 | PASS | critical | SQL injection | int path param rejects "admin'--" | `GET /api/player/admin'--` | 422 — handler never runs | 422 |
| 408 | PASS | critical | SQL injection | int path param rejects "admin'--" | `GET /api/manager/admin'--/history` | 422 — handler never runs | 422 |
| 409 | PASS | critical | SQL injection | draft body rejects element_id='1 OR 1=1' | `POST /api/draft/999999998 element_id='1 OR 1=1'` | 400 Bad Request | 400 |
| 410 | PASS | critical | SQL injection | draft body rejects element_id='1; DROP TABLE manager_draft;--' | `POST /api/draft/999999998 element_id='1; DROP TABLE manager_draft;--'` | 400 Bad Request | 400 |
| 411 | PASS | critical | SQL injection | draft body rejects element_id="' OR '1'='1" | `POST /api/draft/999999998 element_id="' OR '1'='1"` | 400 Bad Request | 400 |
| 412 | PASS | critical | SQL injection | draft body rejects element_id="1' UNION SELECT name FROM sqlite_master--" | `POST /api/draft/999999998 element_id="1' UNION SELECT name FROM sqlite_master--"` | 400 Bad Request | 400 |
| 413 | PASS | critical | SQL injection | draft body rejects element_id='1/**/OR/**/1=1' | `POST /api/draft/999999998 element_id='1/**/OR/**/1=1'` | 400 Bad Request | 400 |
| 414 | PASS | critical | SQL injection | draft body rejects element_id="admin'--" | `POST /api/draft/999999998 element_id="admin'--"` | 400 Bad Request | 400 |
| 415 | PASS | high | SQL injection | search survives '1 OR 1=1' | `GET /api/search?q='1 OR 1=1'` | 200, no error | 200 |
| 416 | PASS | high | SQL injection | search survives '1; DROP TABLE manager_draft;--' | `GET /api/search?q='1; DROP TABLE manager_draft;--'` | 200, no error | 200 |
| 417 | PASS | high | SQL injection | search survives "' OR '1'='1" | `GET /api/search?q="' OR '1'='1"` | 200, no error | 200 |
| 418 | PASS | high | SQL injection | search survives "1' UNION SELECT name FROM sqlite_master--" | `GET /api/search?q="1' UNION SELECT name FROM sqlite_master--"` | 200, no error | 200 |
| 419 | PASS | high | SQL injection | search survives '1/**/OR/**/1=1' | `GET /api/search?q='1/**/OR/**/1=1'` | 200, no error | 200 |
| 420 | PASS | high | SQL injection | search survives "admin'--" | `GET /api/search?q="admin'--"` | 200, no error | 200 |
| 421 | PASS | critical | SQL injection | schema intact after injection attempts | `GET /api/ai/status` | manager_draft still present | ["ai_team_snapshot", "ai_team_snapshot_picks", "ai_transfer_log", "known_manager", "manager_draft", "manager_draft_picks", "manager_team", "manager_team_picks", "processed_deadline"] |
| 422 | PASS | high | regex injection | invalid regex '**' (multiple repeat) | `GET /api/search?q='**'` | 200 with no matches — the input is data, not a pattern | 200 |
| 423 | PASS | high | regex injection | invalid regex '(' (unbalanced group) | `GET /api/search?q='('` | 200 with no matches — the input is data, not a pattern | 200 |
| 424 | PASS | high | regex injection | invalid regex '[' (unterminated character class) | `GET /api/search?q='['` | 200 with no matches — the input is data, not a pattern | 200 |
| 425 | PASS | high | regex injection | invalid regex 'a{99999999}' (absurd repeat count) | `GET /api/search?q='a{99999999}'` | 200 with no matches — the input is data, not a pattern | 200 |
| 426 | PASS | high | regex injection | invalid regex '(?P<n>a)(?P<n>b)' (duplicate group name) | `GET /api/search?q='(?P<n>a)(?P<n>b)'` | 200 with no matches — the input is data, not a pattern | 200 |
| 427 | PASS | high | regex injection | invalid regex '\\' (trailing backslash) | `GET /api/search?q='\\'` | 200 with no matches — the input is data, not a pattern | 200 |
| 428 | PASS | high | regex injection | ReDoS pattern '(a+)+$' (nested quantifier) is not evaluated | `GET /api/search?q='(a+)+$'` | returns in under 1s | 0.01s, status 200 |
| 429 | PASS | high | regex injection | ReDoS pattern '(.*a){20}' (repeated group) is not evaluated | `GET /api/search?q='(.*a){20}'` | returns in under 1s | 0.01s, status 200 |
| 430 | PASS | high | regex injection | ReDoS pattern '(x+x+)+y' (classic evil regex) is not evaluated | `GET /api/search?q='(x+x+)+y'` | returns in under 1s | 0.01s, status 200 |
| 431 | PASS | high | regex injection | '.*' does not match the entire pool | `GET /api/search?q=.*` | 200, and 0 results — '.*' is not a substring of any name | status 200, 0 results |
| 432 | PASS | critical | XSS | search does not reflect '<script>alert(1)</script>' unescaped | `GET /api/search?q='<script>alert(1)</script>'` | no raw <script> or onerror= in the response | 200 |
| 433 | PASS | critical | XSS | player route does not reflect '<script>alert(1)</script>' | `GET /player/'<script>alert(1)</script>'` | 404/307, and no raw payload echoed | 404 |
| 434 | PASS | critical | XSS | search does not reflect '"><script>alert(1)</script>' unescaped | `GET /api/search?q='"><script>alert(1)</script>'` | no raw <script> or onerror= in the response | 200 |
| 435 | PASS | critical | XSS | player route does not reflect '"><script>alert(1)</script>' | `GET /player/'"><script>alert(1)</script>'` | 404/307, and no raw payload echoed | 404 |
| 436 | PASS | critical | XSS | search does not reflect '<img src=x onerror=alert(1)>' unescaped | `GET /api/search?q='<img src=x onerror=alert(1)>'` | no raw <script> or onerror= in the response | 200 |
| 437 | PASS | critical | XSS | player route does not reflect '<img src=x onerror=alert(1)>' | `GET /player/'<img src=x onerror=alert(1)>'` | 404/307, and no raw payload echoed | 404 |
| 438 | PASS | critical | XSS | search does not reflect 'javascript:alert(1)' unescaped | `GET /api/search?q='javascript:alert(1)'` | no raw <script> or onerror= in the response | 200 |
| 439 | PASS | critical | XSS | player route does not reflect 'javascript:alert(1)' | `GET /player/'javascript:alert(1)'` | 404/307, and no raw payload echoed | 404 |
| 440 | PASS | critical | XSS | search does not reflect '\'"><svg/onload=alert(1)>' unescaped | `GET /api/search?q='\'"><svg/onload=alert(1)>'` | no raw <script> or onerror= in the response | 200 |
| 441 | PASS | critical | XSS | player route does not reflect '\'"><svg/onload=alert(1)>' | `GET /player/'\'"><svg/onload=alert(1)>'` | 404/307, and no raw payload echoed | 404 |
| 442 | PASS | critical | XSS | search does not reflect '%3Cscript%3Ealert(1)%3C/script' unescaped | `GET /api/search?q='%3Cscript%3Ealert(1)%3C/script%3E'` | no raw <script> or onerror= in the response | 200 |
| 443 | PASS | critical | XSS | player route does not reflect '%3Cscript%3Ealert(1)%3C/script' | `GET /player/'%3Cscript%3Ealert(1)%3C/script%3E'` | 404/307, and no raw payload echoed | 404 |
| 444 | PASS | critical | XSS | Jinja autoescape is enabled | `main.templates.env.autoescape` | True | true |
| 445 | PASS | critical | XSS | template escapes a script tag | `render("{{ v }}", v="<script>alert(1)</script>")` | &lt;script&gt;alert(1)&lt;/script&gt; | &lt;script&gt;alert(1)&lt;/script&gt; |
| 446 | PASS | critical | XSS | obf_mail escapes a hostile address | `obf_mail('"><script>alert(1)</script>@x.com')` | no raw <script> | <a class="obf-mail" href="#" data-u="&#34;&gt;&lt;script&gt;alert(1)&lt;/script&gt;" data-d="x.com">&#34;&gt;&lt;script&gt;alert(1)&lt;/script&gt; <span aria-hidden="true">(at)</span> x.com</a> |
| 447 | PASS | critical | path traversal | static refuses '../../etc/passwd' | `GET /static/../../etc/passwd` | 404/400/403, never 200 | 404 |
| 448 | PASS | critical | path traversal | no file contents leak for '../../etc/passwd' | `GET /static/../../etc/passwd` | no /etc/passwd or win.ini content | checked |
| 449 | PASS | high | path traversal | player route refuses '../../etc/passwd' | `GET /player/../../etc/passwd` | 404/307, never 200 | 404 |
| 450 | PASS | critical | path traversal | static refuses '..%2f..%2fetc%2fpasswd' | `GET /static/..%2f..%2fetc%2fpasswd` | 404/400/403, never 200 | 404 |
| 451 | PASS | critical | path traversal | no file contents leak for '..%2f..%2fetc%2fpasswd' | `GET /static/..%2f..%2fetc%2fpasswd` | no /etc/passwd or win.ini content | checked |
| 452 | PASS | high | path traversal | player route refuses '..%2f..%2fetc%2fpasswd' | `GET /player/..%2f..%2fetc%2fpasswd` | 404/307, never 200 | 404 |
| 453 | PASS | critical | path traversal | static refuses '....//....//etc/passwd' | `GET /static/....//....//etc/passwd` | 404/400/403, never 200 | 404 |
| 454 | PASS | critical | path traversal | no file contents leak for '....//....//etc/passwd' | `GET /static/....//....//etc/passwd` | no /etc/passwd or win.ini content | checked |
| 455 | PASS | high | path traversal | player route refuses '....//....//etc/passwd' | `GET /player/....//....//etc/passwd` | 404/307, never 200 | 404 |
| 456 | PASS | critical | path traversal | static refuses '/etc/passwd' | `GET /static//etc/passwd` | 404/400/403, never 200 | 404 |
| 457 | PASS | critical | path traversal | no file contents leak for '/etc/passwd' | `GET /static//etc/passwd` | no /etc/passwd or win.ini content | checked |
| 458 | PASS | high | path traversal | player route refuses '/etc/passwd' | `GET /player//etc/passwd` | 404/307, never 200 | 404 |
| 459 | PASS | critical | path traversal | static refuses '..\\..\\windows\\win.ini' | `GET /static/..\..\windows\win.ini` | 404/400/403, never 200 | 404 |
| 460 | PASS | critical | path traversal | no file contents leak for '..\\..\\windows\\win.ini' | `GET /static/..\..\windows\win.ini` | no /etc/passwd or win.ini content | checked |
| 461 | PASS | high | path traversal | player route refuses '..\\..\\windows\\win.ini' | `GET /player/..\..\windows\win.ini` | 404/307, never 200 | 404 |
| 462 | PASS | critical | path traversal | static refuses '%2e%2e%2f%2e%2e%2fetc%2fpasswd' | `GET /static/%2e%2e%2f%2e%2e%2fetc%2fpasswd` | 404/400/403, never 200 | 404 |
| 463 | PASS | critical | path traversal | no file contents leak for '%2e%2e%2f%2e%2e%2fetc%2fpasswd' | `GET /static/%2e%2e%2f%2e%2e%2fetc%2fpasswd` | no /etc/passwd or win.ini content | checked |
| 464 | PASS | high | path traversal | player route refuses '%2e%2e%2f%2e%2e%2fetc%2fpasswd' | `GET /player/%2e%2e%2f%2e%2e%2fetc%2fpasswd` | 404/307, never 200 | 404 |
| 465 | PASS | critical | authentication | /api/refresh rejects a missing token | `POST /api/refresh (no header, token configured)` | 403 | 403 |
| 466 | PASS | critical | authentication | /api/refresh rejects a wrong token | `POST /api/refresh x-refresh-token: wrong` | 403 | 403 |
| 467 | PASS | critical | authentication | /api/refresh rejects an empty token | `POST /api/refresh x-refresh-token: ''` | 403 | 403 |
| 468 | PASS | critical | authentication | /api/mode rejects a missing token | `POST /api/mode?mode=preseason (no header, token configured)` | 403 | 403 |
| 469 | PASS | critical | authentication | /api/mode rejects a wrong token | `POST /api/mode?mode=preseason x-refresh-token: wrong` | 403 | 403 |
| 470 | PASS | critical | authentication | /api/mode rejects an empty token | `POST /api/mode?mode=preseason x-refresh-token: ''` | 403 | 403 |
| 471 | **FAIL** | info | authentication | FPL_REFRESH_TOKEN is configured in this environment | `os.environ['FPL_REFRESH_TOKEN']` | set in production; unset locally is expected | unset |
| 472 | PASS | medium | authentication | /api/mode validates its argument | `POST /api/mode?mode=nonsense` | rejected | error |
| 473 | PASS | medium | security headers | x-content-type-options is set | `GET / response headers` | x-content-type-options present | nosniff |
| 474 | PASS | medium | security headers | x-frame-options is set | `GET / response headers` | x-frame-options present | SAMEORIGIN |
| 475 | PASS | low | security headers | referrer-policy is set | `GET / response headers` | referrer-policy present | strict-origin-when-cross-origin |
| 476 | PASS | medium | security headers | strict-transport-security is set over HTTPS | `GET https://testserver/` | max-age present | max-age=31536000; includeSubDomains |
| 477 | PASS | low | security headers | strict-transport-security is NOT set over HTTP | `GET http://testserver/` | absent — would break local dev | absent |
| 478 | **FAIL** | medium | security headers | content-security-policy is set | `GET / response headers` | content-security-policy present | absent |
| 479 | PASS | low | security headers | no server banner leaking a version | `GET / response headers` | no version in Server | absent |
| 480 | PASS | high | information disclosure | no filesystem paths or tracebacks in API errors | `GET five endpoints that return str(e)` | no paths, no tracebacks | none |
| 481 | PASS | medium | information disclosure | ai/status does not expose an absolute DB path | `GET /api/ai/status` | a bare filename, not a directory layout | fpl_test_state.db |
| 482 | PASS | low | information disclosure | ai/status still reports DB availability | `GET /api/ai/status` | 'available' present | ["available", "path", "storage", "persisted", "journal_mode", "counts"] |
| 483 | PASS | medium | information disclosure | ai/status does not expose an absolute data root | `GET /api/ai/status` | a bare directory name | data |
| 484 | PASS | high | information disclosure | ai/status reports db storage kind | `GET /api/ai/status` | one of bind/volume/anon/image/unknown | image |
| 485 | PASS | medium | information disclosure | ai/status reports db persisted flag | `GET /api/ai/status` | a boolean | false |
| 486 | PASS | high | information disclosure | ai/status reports data storage kind | `GET /api/ai/status` | one of bind/volume/anon/image/unknown | image |
| 487 | PASS | medium | information disclosure | ai/status reports data persisted flag | `GET /api/ai/status` | a boolean | false |
| 488 | PASS | high | information disclosure | unknown API path returns a clean 404 | `GET /api/nonexistent` | 404, no traceback | 404 |
| 489 | PASS | high | input fuzzing | search handles null byte | `GET /api/search?q='a\x00b'` | status < 500 | 200 |
| 490 | PASS | high | input fuzzing | ratings position handles null byte | `GET /api/ratings?position='a\x00b'` | status < 500 | 200 |
| 491 | PASS | high | input fuzzing | search handles very long string | `GET /api/search?q='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'` | status < 500 | 200 |
| 492 | PASS | high | input fuzzing | ratings position handles very long string | `GET /api/ratings?position='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'` | status < 500 | 200 |
| 493 | PASS | high | input fuzzing | search handles unicode | `GET /api/search?q='𝕏𝕐ℤ'` | status < 500 | 200 |
| 494 | PASS | high | input fuzzing | ratings position handles unicode | `GET /api/ratings?position='𝕏𝕐ℤ'` | status < 500 | 200 |
| 495 | PASS | high | input fuzzing | search handles rtl override | `GET /api/search?q='\u202eabc'` | status < 500 | 200 |
| 496 | PASS | high | input fuzzing | ratings position handles rtl override | `GET /api/ratings?position='\u202eabc'` | status < 500 | 200 |
| 497 | PASS | high | input fuzzing | search handles newlines | `GET /api/search?q='a\nb\rc'` | status < 500 | 200 |
| 498 | PASS | high | input fuzzing | ratings position handles newlines | `GET /api/ratings?position='a\nb\rc'` | status < 500 | 200 |
| 499 | PASS | high | input fuzzing | search handles format string | `GET /api/search?q='%s%s%s%n'` | status < 500 | 200 |
| 500 | PASS | high | input fuzzing | ratings position handles format string | `GET /api/ratings?position='%s%s%s%n'` | status < 500 | 200 |
| 501 | PASS | high | input fuzzing | search handles template injection | `GET /api/search?q='{{7*7}}'` | status < 500 | 200 |
| 502 | PASS | high | input fuzzing | ratings position handles template injection | `GET /api/ratings?position='{{7*7}}'` | status < 500 | 200 |
| 503 | PASS | high | input fuzzing | search handles jinja injection | `GET /api/search?q='{{config.items()}}'` | status < 500 | 200 |
| 504 | PASS | high | input fuzzing | ratings position handles jinja injection | `GET /api/ratings?position='{{config.items()}}'` | status < 500 | 200 |
| 505 | PASS | high | input fuzzing | search handles negative | `GET /api/search?q='-1'` | status < 500 | 200 |
| 506 | PASS | high | input fuzzing | ratings position handles negative | `GET /api/ratings?position='-1'` | status < 500 | 200 |
| 507 | PASS | high | input fuzzing | search handles huge int | `GET /api/search?q='9999999999999999999999999999999999999999'` | status < 500 | 200 |
| 508 | PASS | high | input fuzzing | ratings position handles huge int | `GET /api/ratings?position='9999999999999999999999999999999999999999'` | status < 500 | 200 |
| 509 | PASS | high | input fuzzing | search handles float | `GET /api/search?q='1.5'` | status < 500 | 200 |
| 510 | PASS | high | input fuzzing | ratings position handles float | `GET /api/ratings?position='1.5'` | status < 500 | 200 |
| 511 | PASS | high | input fuzzing | search handles empty | `GET /api/search?q=''` | status < 500 | 200 |
| 512 | PASS | high | input fuzzing | ratings position handles empty | `GET /api/ratings?position=''` | status < 500 | 200 |
| 513 | PASS | critical | input fuzzing | no server-side template evaluation | `GET /api/search?q={{7*7}}` | '49' does not appear | {"results":[]} |
| 514 | PASS | high | input fuzzing | live gameweek rejects 'abc' | `GET /api/live/abc` | 422 or a clean non-5xx | 422 |
| 515 | PASS | high | input fuzzing | live gameweek rejects '1.5' | `GET /api/live/1.5` | 422 or a clean non-5xx | 422 |
| 516 | PASS | high | input fuzzing | live gameweek rejects '-1' | `GET /api/live/-1` | 422 or a clean non-5xx | 200 |
| 517 | PASS | high | input fuzzing | live gameweek rejects '1e999' | `GET /api/live/1e999` | 422 or a clean non-5xx | 422 |
| 518 | PASS | high | input fuzzing | live gameweek rejects 'null' | `GET /api/live/null` | 422 or a clean non-5xx | 422 |
| 519 | PASS | high | input fuzzing | live gameweek rejects '[]' | `GET /api/live/[]` | 422 or a clean non-5xx | 422 |
| 520 | PASS | high | payload limits | oversized draft body is rejected | `POST 50,000 picks (~2 MB)` | 400, and quickly | 400 |
| 521 | PASS | high | payload limits | malformed JSON body is a 422 | `POST 'not json'` | 422, never 500 | 422 |
| 522 | PASS | high | payload limits | 15 identical picks are rejected | `POST 15 copies of one player` | 400 | 400 |
| 523 | PASS | medium | payload limits | deeply nested body does not crash | `POST 200-level nested JSON` | status < 500 | 400 |
| 524 | PASS | info | third-party assets | external assets are enumerated | `GET / markup` | list of third-party origins | ["https://fpl.mfhost.co.uk/", "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css", "h ... tasy.premierleague.com/api/bootstrap-static/", "http://clubelo.com/API", "https://ko-fi.com/mylesfairburn"] |
| 525 | **FAIL** | medium | third-party assets | SRI on cdn.jsdelivr.net | `https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css` | integrity= and crossorigin= present | <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet"> |
| 526 | **FAIL** | low | third-party assets | no inline event handlers in the shell | `GET / markup` | no onclick=/onload=/onerror= attributes | [" onerror=\"this.style.display='none'\"", " onerror=\"this.style.display='none'\""] |
| 527 | PASS | medium | privacy | privacy policy covers what is stored | `GET /privacy` | one of ['fpl id'] | ["fpl id"] |
| 528 | PASS | medium | privacy | privacy policy covers cookies / local storage | `GET /privacy` | one of ['cookie', 'local storage', 'localstorage'] | ["cookie", "local storage"] |
| 529 | PASS | medium | privacy | privacy policy covers how long it is kept | `GET /privacy` | one of ['retention', 'months', 'deleted automatically'] | ["months", "deleted automatically"] |
| 530 | PASS | medium | privacy | privacy policy covers how to have it removed | `GET /privacy` | one of ['delete', 'erasure', 'removed'] | ["delete", "removed"] |
| 531 | **FAIL** | medium | privacy | any caller can read any id's draft | `GET /api/draft/<someone else's id>` | documented as unauthenticated | true |
| 532 | PASS | medium | HTTP methods | TRACE is not enabled | `TRACE /` | 405 or 404 | 405 |
| 533 | PASS | low | HTTP methods | POST to a GET-only page is 405 | `POST /` | 405 | 405 |
| 534 | PASS | low | HTTP methods | DELETE on a read endpoint is 405 | `DELETE /api/ratings` | 405 | 405 |
| 535 | PASS | high | HTTP methods | no permissive CORS | `OPTIONS /api/ratings Origin: evil.example` | no Access-Control-Allow-Origin: * | absent |
| 536 | PASS | high | open redirect | no redirect off-site for '//evil.example' | `GET /player///evil.example` | no Location pointing at another host | no redirect |
| 537 | PASS | high | open redirect | no redirect off-site for 'https://evil.example' | `GET /player/https://evil.example` | no Location pointing at another host | no redirect |
| 538 | PASS | high | open redirect | no redirect off-site for '/\\evil.example' | `GET /player//\evil.example` | no Location pointing at another host | no redirect |
| 539 | PASS | high | open redirect | no redirect off-site for 'http:/evil.example' | `GET /player/http:/evil.example` | no Location pointing at another host | no redirect |
