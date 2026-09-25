# Sports as prediction tickers

Research and code audit: 25 September 2026 UTC.

## Product decision

The event is the ticker. A game, a Cup, and a golf tournament each have a stable
identity and their own history. Teams and players are lasting entities linked to
those events. A contract asks a specific question about an event. An outcome is
one possible answer to that question.

The reader chooses an outcome once. Its name stays beside the model value, market
value, gap, and chart. For ATL–GB, selecting GB shows GB in every comparison. The
ATL selector opens the opposite outcome. This addresses the earlier screen where
the favorite's 59.1% appeared above the other team's 40.9% with similar labels.

The common layout follows the stock and token pages: identity on the left, score
on the right, a small glyph at the far right, and uninterrupted rows. On the event
page, the chart is a history of the selected contract. The actual score remains
visible as game context. A price difference is called a **gap**, measured in
percentage points. Its sign describes RATi's opinion relative to the named venue.
Row labels say Above, Below, or Level for that same outcome and venue. Model means
a saved forecast is available while a fresh, comparable market price is pending.

The glyph has two rings. The outside ring is RATi's value; the inside ring is the
market's value. The accessible label names the outcome and source. The method and
inputs sit in a disclosure below the chart. This gives the glyph the same role as
the stock glyph: a compact view of the evidence behind the opinion.

## What assigns the current chances

### Team games: `team-form-v1`

The implementation in `sports.predict_event` uses this formula:

```
home_rate = (home_wins + 8) / (home_wins + home_losses + 16)
away_rate = (away_wins + 8) / (away_wins + away_losses + 16)
p_home = clip(0.50 + 0.65 * (home_rate - away_rate) + home_edge, 0.18, 0.82)
p_away = 1 - p_home
```

| League | Home advantage |
| --- | ---: |
| MLB | 3.5 percentage points |
| NFL | 5.5 percentage points |
| NBA | 6.0 percentage points |
| NHL | 4.0 percentage points |

The 8–8 prior pulls short season records toward 50%. The home advantage and 0.65
weight are fixed settings. Missing records use the 50% starting point plus home
advantage. The model's present scope is season record and venue. Lineups, injuries,
starting pitchers, goalkeeper choice, rest, travel, and opponent strength belong
in the next model's input set.

The two probabilities describe a decisive result. Sportsbook moneylines are
converted to implied probabilities and divided by their sum. The current team
venue adapter also scales its two outcomes to 100%. NFL ties therefore require
care when comparing the result with the exact cash settlement of a venue.
The interface states that condition beside the model method.

The saved signal rules use a 2 pp minimum gap, with WATCH at 5 pp. These are
thresholds in a baseline system. The probability and its gap are separate from
the team's chance of being the most likely winner.

The existing team score estimate is derived from the win probability. It splits
either a sportsbook total or a league total with a sport-specific exponent.
The interface labels this dependency. Its score error should be measured
separately from win-probability error.

### Observed scorecard

The public `/api/sports/stats` records returned the following on 25 September:

| League | Settled games | Favorite accuracy | Brier error | Paper value calls | Paper return |
| --- | ---: | ---: | ---: | ---: | ---: |
| MLB | 312 | 58.0% | 0.241 | 251 | −9.3% |
| NFL | 32 | 50.0% | 0.255 | 31 | +9.3% |
| NBA | 0 | Pending | Pending | 0 | Pending |
| NHL | 0 | Pending | Pending | 0 | Pending |

These are the application's saved results. Counts differ by league and time. The
MLB figures show why favorite accuracy alone is a weak guide to the value of a
gap. A 50% constant forecast has Brier error 0.25 on binary outcomes. A useful
market comparison also measures the model and market on the same games and at
the same forecast time. This change adds that paired Brier comparison to the
scorecard and the event's method disclosure.

Public records: [MLB](https://sports.rati.chat/api/sports/stats?league=mlb),
[NFL](https://sports.rati.chat/api/sports/stats?league=nfl),
[NBA](https://sports.rati.chat/api/sports/stats?league=nba),
[NHL](https://sports.rati.chat/api/sports/stats?league=nhl).

### Presidents Cup: `cup-ranking-v1`

The deployed scoring page previously showed team points and match results. The
forecast code was in a separate open change. This rebuild brings its source
collection and saved forecasts into the event ticker.

The model joins the official 24-player roster to ESPN's world-ranking average
points. Each player's strength is the square root of those points. A pairing's
strength is its player average. Its share of the two sides' strength is the win
chance conditional on a decisive match. A fixed 12% tie assumption leaves 88%
for the two wins. A win adds one point and a tie adds half a point.

Match input comes from ESPN's structured leaderboard feed, which supplies team
points, player pairings, and completed results in one response. Production checks
found a different page format in ESPN's HTML response, so the forecast uses the
same data feed as the scoring view.

An exact probability calculation combines the remaining matches with the earned
team points. Announced matches use their named players. Future pairings use the
roster averages. The output includes USA win, International win, a 15–15 tie, and
expected final points. Changes to earned points update the Cup forecast.
The active hole-by-hole state sits outside this model's present scope.

This is an experimental ranking baseline. The 12% tie rate and the ranking-to-
strength formula are assumptions awaiting validation. Cup matches share players,
conditions, and team selection decisions. Those links are the next step for a
stronger model of uncertainty.

A real local collection loaded 24 ranked players, 10 announced matches, and five
completed matches. At USA 3–2 International, the saved baseline gave USA 84.4%,
International 11.0%, tie 4.5%, and expected final points 17.6–12.4. Display rounding
can make the three percentages sum to 99.9%. Full precision is retained.

The official format has 30 matches. A 15–15 finish shares the Cup.
[Presidents Cup rules](https://www.presidentscup.com/faq).

## Market rules change the quantity being priced

The Cup is a useful test of a general prediction-market model:

| Contract | Possible outcomes | RATi quantity |
| --- | --- | --- |
| Kalshi Cup winner | USA, International, Tie | Outright win probability |
| Polymarket Cup winner | USA, International; half payout on a tie | Win probability + half the tie probability |

The two contracts have separate selectors and histories. Kalshi's three raw
midpoints stay as quoted. Each is a separate $1 contract; their sum can differ
from 100%. Polymarket's description specifies a 50–50 settlement on a 15–15 tie.
The corresponding model comparison is **fair value**, shown on a 0–100 scale.
[Kalshi event](https://external-api.kalshi.com/trade-api/v2/events/KXPRESCUP-26),
[Polymarket event](https://polymarket.com/event/presidents-cup-2026).

At collection, Kalshi's USA bid and ask were 85¢ and 86¢. Polymarket's were 1¢ and
99¢, with $9.62 listed liquidity. The latter is shown as a wide spread. Gap
promotion needs a usable spread and fresh model and price observations.

The venue's update field and our price-check time are different fields. Kalshi's
metadata update was several days earlier even while its bid and ask changed.
The chart uses our observation time. Source update time and full settlement rules
remain in the saved quote. Repeated prices and a return from A to B to A each
keep their observation. This preserves the path of the price.

Kalshi documents event and market tickers, bid/ask fields, and settlement rules.
Polymarket groups markets under events and gives each outcome its own token ID.
These are the source identities to preserve for broader coverage.
[Kalshi market API](https://docs.kalshi.com/api-reference/market/get-market),
[Polymarket data model](https://docs.polymarket.com/market-data/overview).

## Stroke-play golf and the next statistical model

A stroke-play tournament needs a field model. Its player-win probabilities must
share one field and one set of settlement rules. The retained score pages already
hold actual round strokes, score to par, player IDs, and holes completed. Upcoming
fields become available as the source publishes them. The event ticker shows
which model inputs are pending.

A sound next scoring model starts with historical player rounds, adjusted for
field and course difficulty. It estimates player skill with recency weighting
and shrinkage for small samples. It then simulates score distributions over the
remaining holes, with the cut, withdrawals, ties, and playoffs included.

DataGolf describes that general approach and separate treatment of strokes-gained
categories. Its live model also uses remaining holes and changing course
conditions. This is evidence for the input requirements, rather than coefficients
that RATi can copy into a new league model.
[DataGolf methodology](https://datagolf.com/predictive-model-methodology/),
[DataGolf FAQ](https://datagolf.com/frequently-asked-questions).

Collecting full-field round histories is the next data priority for stroke play.
For team games, the priorities are opponent-adjusted performance and confirmed
participants: starting pitchers for MLB, quarterbacks and injuries for NFL,
lineups and rest for NBA, and goalies and rest for NHL. Keep provider identity,
source time, and collection time with every feature.

## Saved histories and evaluation

The new contract quote store is keyed by event, contract, outcome, venue, and
observation time. Its payload keeps source IDs, settlement rules, bid, ask,
spread, volume/liquidity when available, and the price basis. Saved Cup forecasts
keep the model version, source receipts, and complete inputs for replay.
Team forecasts retain their existing versioned receipts.

The sports worker runs on its existing interval (10 minutes by default, five
minutes minimum). Unchanged pregame models receive a new saved check after ten
minutes. Team-model and team-price histories stop at kickoff. Cup histories
continue as completed matches change the score. Each chart line uses its own
clock. A collection gap longer than two hours breaks the line.

The next model comparison should use:

1. A fixed forecast horizon for each test, such as one hour before a game.
2. Chronological training and test windows, grouped by event.
3. Model and market Brier error on the same contracts and events.
4. Calibration bins, sample counts, and uncertainty intervals.
5. Separate score error, winner accuracy, and return after spread and fees.
6. A saved result for every issued forecast, including void and tied outcomes.

Live snapshots from one tournament share an outcome. Event-level grouping keeps
frequent polling from inflating the effective sample. DataGolf's evaluation also
separates forecast time and explains this correlation.
[Live model evaluation](https://datagolf.com/live-model-evaluation).

The current release covers saved team-game baselines and the Presidents Cup
ranking baseline. Stroke-play score models, live team win models, and broader
contract discovery have explicit data and evaluation requirements above.
