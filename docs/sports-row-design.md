# Sports rows and lasting profiles

Sports shares the market pages' spacing, type, colors, filters, and right-aligned
numbers. Each number and symbol has a label that fits its sport.

## The game row

The left side holds the teams. Their order stays away then home, matching the
score on the right. During play, the leading team is larger and green. After
the final score, that treatment marks the winner. A tie gives both teams equal
weight. Pregame teams and games with pending scores also have equal weight.
The forecast glyph names the saved pregame model favorite.

The stock page is the layout reference. Sports uses the same 66-pixel minimum
row height on desktop and 62-pixel height on phones, padding, gaps, score type,
and 40/30-pixel glyph footprint. A separate signal column sits on the left.
Teams occupy the identity column, followed by the score and glyph on the right.
MLB sits beside the teams on phones and below the names on desktop. Narrow
screens allow the league to wrap within the identity column. Each game is one
continuous row with a divider.

The WATCH, LEAN, PASS, and MODEL ONLY tags retain their current meaning and
palette. The saved value selection can be an underdog. Its team and signal
appear in the tag's title; the game page contains the full evidence and edge.

The blue outer ring means **pregame model win chance for the named team**. Its arc
and accessible percentage use the same probability. The inner pieces show the
saved model's season-record and venue adjustments, plus a clamp piece when it
changes the output. Their sizes show shares of the absolute adjustment. Hatching
marks a piece that lowers the named team's chance. The title and accessible
name include the team, percentage, saved time, and signed factors. Earlier saved
calls retain the outer ring and identify their factor breakdown as pending.
An even model gives each team a 50% chance. A pending model uses a dashed
outline with an accessible **Pregame model pending** label.

The game page shows both teams' model and saved no-vig market chances in one
card. A team can be the model favorite while the other has the positive gap
against market. The headline names that gap, and the forecast glyph sits with
the team whose factors it depicts. The chart traces the selected team's model
and market chances across saved pregame readings. Its labels name both lines,
the team, the time range, and the first and latest gap. The page refreshes this
card with each game-detail poll and keeps an open factor panel open. The factor
panel contains the saved starting point, signed adjustments, input records, and
model version. The list and game page select the same last pregame model
snapshot. Later feed updates remain in history.

The green team highlight follows the scoreboard during play. The blue forecast
ring follows the saved pregame estimate. This lets a reader see when a forecast
and the actual game differ. A future live model should have a separate **Live
win chance** label and its own saved time.

## Proposed profile model

All of these are entities. Their kinds and relationships give them meaning:

| Kind | Lasting identity | Connected records |
| --- | --- | --- |
| Company | Company ID | Stocks, filings, people, reports |
| Token | Chain and contract address | Pools, wallets, findings, reports |
| Team | League and provider team ID | Athletes, games, seasons, reports |
| Athlete | Provider person ID | Team memberships, appearances, results |
| Game or tournament | Provider event ID | Participants, scores, forecasts, results |

Teams and athletes should be followable profiles, much like a company or token.
The game is a dated event with its own page. A team page can collect form,
schedule, roster news, and past forecast results. An athlete page can collect
appearances, availability, and performance. For individual sports such as golf,
the athlete is the natural lasting profile and the tournament connects the field.

Abbreviations such as ARI or BOS are display labels. IDs include the league and
provider so repeated names remain distinct. Team membership has start and end
dates, allowing transfers to preserve history. Forecasts belong to a game and
name their target participant, model, capture time, inputs, and outcome.

Games remains the landing view for scores. The game page links to team profiles;
team profiles link to players seen in saved games; golf tournament rows link to
golfer profiles. These pages use stored provider IDs and show saved appearances
as history. A **Games · Teams · Athletes** navigation and follow actions are
later product steps.

## Other useful signals

Use plain labels for **Lineup pending**, **Odds updated 12m ago**, or **Score
pending**. If a later glyph summarizes coverage, its segments should each name
an available input: form, lineup, venue, and market. Put popularity on team and
athlete profiles as its own observed activity measure. Each signal should open
the evidence that gives it meaning.
