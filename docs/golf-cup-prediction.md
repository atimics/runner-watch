# Presidents Cup data and forecast

The page for `golf:401824815` brings together the official 24-player roster,
ESPN pairings and scores, and world ranking points. The main card shows the
predicted winner, projected team points, and probabilities for both teams and
a tied Cup. Every announced pairing and all 24 player rankings appear below.

## Data collection

The existing sports worker collects three fixed public sources:

- [ESPN pairings and scores](https://www.espn.com/golf/leaderboard?tournamentId=401824815), cached for five minutes.
- [Official Cup rosters](https://www.presidentscup.com/teams), cached for six hours.
- [World rankings via ESPN](https://site.web.api.espn.com/apis/site/v2/sports/golf/all/rankings?region=us&lang=en&polls=1), cached for six hours.

Collection runs while the event is in the golf feed, through the day after its
end time. The page reads saved data and checks for updates every minute while
idle. A source failure keeps its last successful data and time. The page labels
a pending refresh, match data older than 30 minutes, or rankings older than 14 days.

The collector checks the event ID, team identities, score totals, official roster
headers, all 24 distinct names, and ranking dates. A complete Cup forecast needs
ranking points for every roster player and every unfinished pairing. The page
shows the available inputs while collection fills any gaps.

Migration 86 adds `sports_golf_analysis`. Each snapshot saves source URLs, source
times, raw-response SHA-256 hashes, normalized inputs, errors, the model version,
and the resulting analysis. Existing source receipts also retain each successful
parsed source and each failed fetch. Snapshots support replay and later scoring.

## Model `cup-ranking-v1`

This is an experimental ranking model. Its probabilities need validation against
completed results.

1. Player strength is the square root of average world ranking points.
2. A pairing's strength is the mean strength of its players. For a decided match,
   USA's win chance is `USA strength / (USA strength + International strength)`.
3. Each match has an assumed 12% tie chance. The other 88% is split between the
   two teams using their relative strength.
4. Announced pairings use their named players. Future pairings use each full
   roster's mean strength. This assumes equal player use and independent matches.
5. Completed team points are fixed. An exact calculation combines the remaining
   match outcomes in half-point units, up to the Cup's 30 points. An outright win
   means more than 15 points; a 15–15 result has its own probability.

Matches in progress use the ranking estimate until their result enters the team
totals. Pair chemistry, course fit, weather, and current hole leads are areas for
future model development. The square-root transform and 12% tie rate are explicit
assumptions to test during calibration.

## Verification

`tests/fixtures/golf_cup_2026.json` contains the normalized inputs from the public
sources, their URLs, capture times, and source hashes. It includes all 24 players
and the five announced Thursday matches. Replay it with `build_analysis` in
`runner_web.golf_cup`.

The tests cover source parsing, roster joins, missing evidence, team identity,
probability totals, equal-strength symmetry, changes in strength, completed
results, saved history, and failed refreshes. Browser checks cover the forecast,
pairings, rosters, keyboard access, and screens from 320 to 1280 pixels wide.
