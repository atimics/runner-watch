# Sports rows and lasting profiles

Sports shares the market pages' spacing, type, colors, filters, and right-aligned
numbers. Each number and symbol has a label that fits its sport.

## The game row

The left side holds the teams. Their order stays away then home, matching the
score on the right. During play, the leading team is larger and green. After
the final score, that treatment marks the winner. A tie gives both teams equal
weight. Pregame teams and games with pending scores also have equal weight.
The forecast glyph names the model favorite before play.

Each row has three columns: teams on the left, score toward the right, and
the forecast glyph at the far right. League and signal share one compact line
under the team names. The game status sits under the score. Every game has
one row with a simple divider; all its information stays within that row.

The WATCH, LEAN, PASS, and MODEL ONLY tags retain their current meaning and
palette. The saved value selection can be an underdog. Its team and signal
appear in the tag's title; the game page contains the full evidence and edge.

The blue ring means **pregame model win chance for the named team**. Its arc
and percentage use the same probability. The ring contains the team label and
percentage, with **Pregame** below it. That label stays through play and after
the final score. The title and accessible name spell out **Pregame model**, the
full team name, and the saved time when available. Long team names fit in the
left column and shorten with an ellipsis inside the glyph. An even model says
**Even 50%**. A pending model uses a quiet outline and **Pending** caption.

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
