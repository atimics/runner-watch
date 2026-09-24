# Sports rows and lasting profiles

Sports shares the market pages' spacing, type, colors, filters, and right-aligned
numbers. Each number and symbol has a label that fits its sport.

## The game row

The left side holds the teams. Their order stays away then home, matching the
score on the right. During play, the leading team is larger and green. After
the final score, that treatment marks the winner. A tie gives both teams equal
weight. Before play, the higher model probability earns the emphasis and the
caption says **Model favorite**. A started game with pending scores keeps both
teams equal until a score arrives.

The WATCH, LEAN, PASS, and MODEL ONLY tags retain their current meaning and
palette. The saved value selection can be an underdog. Its team and signal
appear in the tag's title; the game page contains the full evidence and edge.

The blue ring means **pregame model win chance for the named team**. Its arc
and percentage use the same probability. The caption names the team and
always says **Pregame model**, including during play and after the final score.
The title and accessible name include the saved time when available. An even
model says **Even 50–50**. A usable saved probability is required to draw a ring.

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

The next navigation step is **Games · Teams · Athletes**. Games remains the
landing view for scores. Team and athlete views support longer-term research
and following. This PR delivers the game row; profile pages and follow actions
are the next product step.

## Other useful signals

Use plain labels for **Lineup pending**, **Odds updated 12m ago**, or **Score
pending**. If a later glyph summarizes coverage, its segments should each name
an available input: form, lineup, venue, and market. Put popularity on team and
athlete profiles as its own observed activity measure. Each signal should open
the evidence that gives it meaning.
