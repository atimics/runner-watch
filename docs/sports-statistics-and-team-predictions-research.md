# Sports statistics and team prediction research for sports.rati.chat

Last researched: 2026-08-26
Scope: MLB, NFL, NBA, and NHL pre-game team predictions
Status: product, data, model, and compliance research; not legal advice

## Executive decision

RATi Sports should become a public forecasting record, not another page of anonymous betting
tips. Its strongest idea is already in the product: probabilities, source times, frozen odds,
versioned models, and paper-pick receipts. The next build should make that promise real.

The recommended direction is:

1. Stop treating the ESPN preview endpoint as a production source. It is correctly marked `poc_only`
   in the source catalog today.
2. Run a two-week licensed-provider test. Test MySportsFeeds, SportsDataIO, and Sportradar for team,
   schedule, result, lineup, injury, and history data. Test The Odds API as the separate odds source.
3. Keep the RATi forecast independent of the betting market. Compare it with a no-vig market
   consensus captured at the same time. A separate market-aware blend can be shown for forecast
   accuracy, but it must not be called an independent edge.
4. Replace the one shared `team-form-v1` rule with four league-specific models. Start with a simple,
   hard-to-fool rating baseline, then test richer models against it.
5. Publish every eligible forecast, not only winners or large edges. Lock forecasts at named times,
   keep corrections, and score them with Brier score, log loss, calibration, and market-relative
   skill.
6. Separate three records in the interface: the RATi model record, the whole community paper-pick
   record, and the signed-in user's record. The home-page ROI now combines all user paper picks and
   is not the model's performance.
7. Keep the first product team-level and pre-game. Do not add live betting, parlays, or player props
   until the data rights, model record, and responsible-product rules are mature.

The practical low-cost pilot is **commercial MySportsFeeds data plus The Odds API Business odds**.
The stronger long-term path is a commercial agreement with **Sportradar or SportsDataIO**, depending
on the provider trial, rights, and quote. All vendors must confirm public display, derived-model,
historical storage, model-training, and post-termination rights in writing before launch.

## What exists today

The current implementation in `src/runner_web/sports.py` does several good things:

- covers MLB, NFL, NBA, and NHL;
- normalizes events into one event record;
- stores changing odds as immutable snapshots;
- removes the margin from a two-sided moneyline;
- stores a model version, input hash, evidence, risks, and observation time;
- freezes the odds and time when a user makes a paper pick;
- settles paper picks from final scores;
- blocks preseason and exhibition games from being promoted as an edge; and
- labels the ESPN feed as a preview in the interface and as `poc_only` in
  `src/runner_web/source_catalog.py`.

This is a sound receipt system. It is not yet a sound prediction system.

### Current model audit

| Current behaviour | Why it is useful | Main problem | Recommended action |
| --- | --- | --- | --- |
| Uses season wins and losses with eight synthetic wins and losses | Shrinks small samples toward 50% | Ignores opponent strength, score margin, player availability, and most game context | Keep only as a named naive baseline |
| Applies one formula to all four leagues | Easy to explain | The sports have different scoring, schedule, lineup, and overtime systems | Use one model family and feature set per league |
| Adds a fixed home advantage in probability points | Includes an important effect | Fixed values are not documented, validated, season-aware, or neutral-site aware | Estimate home effects from past data inside each league model |
| Clamps every home probability to 18%–82% | Prevents extreme numbers | The limits are arbitrary and can damage calibration | Let regularization and calibration control extremes |
| Reads only a simple `W-L` record | Compact | NFL ties and NHL overtime losses are not represented correctly | Store league-specific standings fields, but predict from game-level history |
| Treats `unknown` season type as eligible | Avoids an empty slate | Unknown games can be promoted when their rules are not known | Unknown season type should force `not ready` |
| Takes the first offered odds provider | Simple | It is not a market consensus and may be stale or soft | De-vig each book separately, then combine comparable books |
| Uses provider fields named `close` for a current snapshot | Makes odds available | “Close” may mean the feed's price object, not a verified final closing line | Store market status, source publish time, and collection time explicitly |
| Uses fixed 2% and 5% edge labels | Easy interface | The thresholds have no out-of-sample support and ignore uncertainty | Learn and pre-register decision thresholds from validation data |
| Shows an estimated edge | Clear comparison | The model has no published calibration or out-of-time test record | Call it “model-market gap” until validation supports stronger language |
| Stores model snapshots | Strong audit base | Model predictions are not settled and scored in their own ledger | Add prediction outcomes and an all-forecast performance page |
| Shows settled W-L-P, units, and ROI on the slate | Makes receipts visible | `sports_pick_stats()` combines all community picks, not RATi forecasts | Label and separate model, community, and personal records |
| Refreshes every ten minutes | Adequate for a preview | The page says “LIVE EVIDENCE,” but a ten-minute poll is not always live | Say “latest snapshot” and show age and stale state |

### Immediate product wording changes

These changes should happen before a stronger model is ready:

- Rename `team-form-v1` to **Team-form baseline** everywhere.
- Replace “estimated edge” with **model-market gap**.
- Replace “LIVE EVIDENCE” with **LATEST SNAPSHOT**.
- Label the current result strip **Community paper picks**.
- Add “Unvalidated baseline” beside the current model.
- Force a pass when the season type, market rules, team mapping, or odds age is unknown.

## Product position

Most prediction sites compete on the number of picks, markets, books, or claimed return. RATi can
compete on whether a reader can audit the claim.

The product promise should be:

> Every forecast says what RATi knew, when it knew it, which model produced the number, what was
> missing, how the market was priced at that time, and what happened later.

That promise leads to five product rules:

1. **Probability before pick.** Show the full home and away probability even when RATi passes.
2. **Same-time comparison.** Compare a prediction only with a market snapshot available at the
   prediction time.
3. **No silent rewrite.** Corrections create a new source or feature version. They do not change the
   old receipt.
4. **All-game record.** The performance page includes every eligible game forecast.
5. **Visible uncertainty.** Missing starters, goalies, lineups, injuries, or stale odds lower the
   readiness level instead of becoming a hidden default.

### What to predict, in order

| Priority | Product | Reason |
| --- | --- | --- |
| 1 | Pre-game home/away win probability | Fits the current moneyline receipt system and has a clear outcome |
| 2 | Expected score and margin distribution | Supports a better win probability and later spread/total research |
| 3 | Season playoff and championship simulations | Reuses game probabilities and creates useful team pages |
| 4 | Spread and total probabilities | Requires exact line rules and stronger score-distribution calibration |
| 5 | Live win probability | Needs much faster licensed data, state models, and stronger operations |
| Later | Player props and parlays | High data, rights, injury, settlement, and responsible-product complexity |

RATi should publish team forecasts for every covered game. It should promote a “lean” only when the
input set is complete enough and a pre-declared rule is met. “Pass” should be a normal and common
result.

## Data-source research

### Production rule

An endpoint being reachable does not grant the right to build a public product from it. Direct
league and media endpoints are useful for private experiments, but their public terms are a poor
fit for a commercial prediction service:

- [NBA.com terms](https://api-hub.nba.com/termsofuse) limit NBA statistics to news reporting or
  private non-commercial uses, bar gambling-related use, and restrict comprehensive updated
  databases without consent.
- [NFL.com terms](https://www.nfl.com/legal/terms/) prohibit systematic data retrieval without
  prior written consent.
- [NHL.com terms](https://www.nhl.com/info/terms-of-service) describe the services and content as
  private, non-commercial use unless permission is granted.
- [MLB.com terms](https://www.mlb.com/official-information/terms-of-use) limit ordinary access to
  personal non-commercial use and require permission for broader reproduction or display.
- ESPN points users to the [Disney terms of use](https://disneytermsofuse.com/). The current RATi
  source catalog is right to keep the scoreboard endpoint at proof-of-concept status.

This does not decide every legal question about bare facts. It does mean RATi should not build its
business on an undocumented feed whose owner has not granted the intended access and display rights.

### Vendor shortlist

Prices below are public list prices seen on 2026-08-26. They can change. Enterprise prices and the
exact licensed rights require a quote and contract.

| Provider | What the public material supports | Public price signal | Rights or product concern | Research decision |
| --- | --- | --- | --- | --- |
| [Sportradar](https://developer.sportradar.com/getting-started/docs/get-started) | Deep league-specific MLB, NFL, NBA, and NHL APIs; schedules, play-by-play, stats, lineups/injuries in relevant feeds, odds, historical data, and trials | Contact sales; 30-day trials | Exact fields and display/model rights depend on the package and contract | Put on the long-term RFP shortlist |
| [SportsDataIO](https://sportsdata.io/developers) | Commercial big-four feeds with live data, play-by-play, injuries, lineups, odds from 10+ books, history, SLA, and support | Commercial pricing by use case; contact sales | The self-serve Discovery Lab is next-day delayed and explicitly not licensed for commercial redistribution | Put on the long-term RFP shortlist |
| [MySportsFeeds](https://www.mysportsfeeds.com/feed-pricing/) | Consistent big-four schedules, scores, standings, stats, box scores, lineups, play-by-play, injuries, and add-on odds | Commercial non-live core starts at CAD $25–$39 per league per month; details and faster data cost more | Advanced metrics, correction speed, source rights, and near-real-time price need testing | Best budget stats pilot if the contract permits derived forecasts |
| [The Odds API](https://the-odds-api.com/liveapi/guides/v4/) | Multi-book moneylines, spreads, totals, historical snapshots, and a simple JSON API | Business was listed at $99/month with historical data; Professional at $29/month without it | It is an odds source, not a deep performance source | Recommended odds pilot |
| [The Odds API terms](https://the-odds-api.com/terms-and-conditions.html) | Expressly support commercial websites, dashboards, and analytical tools when the raw data is not the primary product | Included with plan | No standalone resale; verify long-term storage and social-card display in writing | Better public fit than many self-serve terms |
| [SportsGameOdds](https://sportsgameodds.com/pricing) | Big-four events, results, odds, many books, some team/player stats, and historical data on higher plans | Pro was listed at $299/month | [Terms](https://sportsgameodds.com/terms) restrict derivative works, public benchmarks without consent, some ML uses, redistribution, and retained raw data after termination | Do not use for model research without a written addendum |
| [Stats Perform / Opta](https://www.statsperform.com/products/) | Deep data, advanced metrics, live feeds, predictions, and editorial products | Enterprise inquiry | Likely more product and cost than the first RATi release needs | Revisit at larger scale |
| [Genius Sports and NFL](https://www.nfl.com/news/nfl-extends-strategic-partnership-with-genius-sports) | Exclusive distribution of official real-time NFL play-by-play, Next Gen Stats, and official betting data through 2027–28 | Enterprise inquiry | NFL-only and enterprise-led | Relevant if official NFL tracking data becomes essential |

### Recommended provider test

Run the same test harness against each candidate for 14 days. Do not rely on a demo call.

Measure:

- event coverage and duplicate rate;
- event-ID stability through postponements and rescheduling;
- p50 and p95 delay for schedules, odds, scores, finals, and corrections;
- injury, probable-starter, starting-goalie, lineup, and inactive coverage;
- odds book coverage, suspended-market state, and source timestamps;
- neutral sites, doubleheaders, overtime, shootouts, and playoff rules;
- field null rate and unexplained schema changes;
- request cost for the real refresh plan;
- support response and status-page quality; and
- the right to store raw responses, normalized facts, features, predictions, and old receipts.

The first choice should be based on this measured report and the contract, not the longest feature
list.

### Recommended source stack

**Budget pilot**

- MySportsFeeds commercial near-real-time Core + Stats + Details for the four leagues.
- The Odds API Business for multi-book current and historical odds.
- A paid [Open-Meteo commercial plan](https://open-meteo.com/en/pricing) for consistent US and
  Canadian venue weather, with required attribution. The free API is for non-commercial use.
- [US National Weather Service API](https://www.weather.gov/documentation/services-web-api) as an
  open US weather cross-check, not the only cross-border source.

**Growth stack**

- One commercial big-four agreement with Sportradar or SportsDataIO if it passes the trial and
  reduces mapping and operations work enough to justify the price.
- Keep a separate odds vendor if it gives better book depth, historical snapshots, and price.

**Do not launch from**

- the current ESPN preview endpoint;
- direct NBA.com, NFL.com, NHL.com, or MLB.com endpoints without written permission;
- scraped injury reports or lineups from news pages;
- free hobby plans that exclude commercial display or model training; or
- a vendor whose terms require old receipts or training data to be deleted when the subscription
  ends.

### Rights to put in the vendor contract

Ask each vendor to answer these questions in writing:

1. Can RATi display team names, scores, standings, stats, lineups, injuries, and odds publicly?
2. Can RATi calculate and display derived ratings, fair probabilities, charts, and model explanations?
3. Can vendor data be used to train, validate, calibrate, and monitor RATi's own prediction models?
4. Can raw responses be stored for audit and replay? If not, which normalized facts may be stored?
5. Can feature snapshots, forecasts, source timestamps, and settled receipts be kept permanently?
6. What must be deleted after termination, and what derived or aggregated data survives?
7. Can odds history and sportsbook names be shown? What attribution and trademark rules apply?
8. Can public model-performance comparisons mention the provider?
9. Can RATi publish screenshots and social cards that contain the licensed fields?
10. What are the update, correction, outage, rate-limit, and deprecation promises?
11. Are Canadian users and a Canadian company covered by the license?
12. Are there different rights for media, analytics, fantasy, betting-content, and affiliate uses?

## Data design

The current immutable odds and prediction snapshots are worth keeping. The next schema should add
the facts needed to reconstruct exactly what a model knew.

### Canonical records

| Record | Important fields |
| --- | --- |
| Event identity | RATi event ID, provider IDs, league, season, season type, game number, venue, neutral-site flag, scheduled time, reschedule history, rule set |
| Team game | opponent, home/away, final score, regulation score, overtime/shootout state, possessions or opportunities, rest and travel |
| Player availability | player, team, status, role, source publish time, collection time, effective time, confirmed/projected flag |
| Lineup or starters | expected players/minutes/role, probable and confirmed state, batting order, starting pitcher, quarterback, or goalie |
| Market snapshot | book, market scope, outcome, price, points, open/suspended state, book publish time, collection time, source |
| Feature snapshot | event, feature-set version, cutoff time, values, missingness, source IDs, build hash |
| Model artifact | model ID, code version, training window, feature version, hyperparameters, calibration version, evaluation report |
| Prediction | event, model ID, forecast stage, cutoff time, probabilities, expected score/margin, readiness, missing inputs, explanation |
| Prediction outcome | final result, settlement rules, scored time, Brier/log loss, market comparison, correction status |
| Public decision | pass/lean, selected side if any, decision-rule version, exact available price and time |

Every source fact should preserve three times when they exist:

- when the real-world event happened;
- when the source published or changed it; and
- when RATi collected it.

That is the main defence against look-ahead bias.

### Identity and rules

Do not join data on team abbreviation or display name. Keep provider ID maps with valid-from and
valid-to dates. Model these cases directly:

- franchise moves and name changes;
- MLB doubleheaders and probable-pitcher changes;
- neutral-site and international games;
- postponements, resumptions, cancellations, and forfeits;
- regulation-only versus overtime-included markets;
- NFL ties;
- NHL regulation, overtime, and shootout results;
- playoff series and league rule changes; and
- book-specific void and settlement rules.

## Prediction system

### Three forecasts, not one hidden number

For each game and cutoff time, store these separate forecasts:

1. **Market baseline:** a same-time, no-vig consensus across comparable books.
2. **RATi independent:** a league model that does not use current odds as an input.
3. **RATi blended:** an optional market-aware forecast designed for best probability accuracy.

Only forecast 2 creates a clean model-market disagreement. If current odds are an input to the model
and then the output is compared with those odds, the product is partly comparing the market with
itself.

### Forecast stages

Use named, fixed cutoffs instead of continuously replacing one public prediction:

- **Early:** about 24 hours before the scheduled start;
- **Update:** about 6 hours before start;
- **Final:** about 60 minutes before start or after the league's key lineup/inactive deadline; and
- **Close:** the last valid market snapshot before start, used only as an evaluation benchmark.

If a game time moves, preserve the old forecasts and make a new schedule version. After the game
starts, no pre-game forecast can be changed.

### Model ladder

Do not start with a large neural network. The training set is small, especially for the NFL, and
many features are correlated.

Use this ladder for each league:

1. **Naive baseline:** home advantage plus long-run league rate.
2. **Market baseline:** no-vig book consensus at the same cutoff.
3. **Dynamic rating:** opponent-adjusted team strength with time decay, home/neutral site, and
   season-to-season shrinkage.
4. **Regularized statistical model:** logistic or margin regression using the approved feature set.
5. **Tree challenger:** gradient-boosted trees for non-linear effects and interactions.
6. **Calibrated ensemble:** combine only models that add out-of-time skill.

The simple rating stays in production as the fallback and audit reference. A challenger is promoted
only when it improves proper scoring and calibration on later, untouched games.

### MLB model

MLB should be the first deep model because it produces many games and has clear starting-pitcher
and lineup effects.

High-value features, in order:

1. confirmed or probable starting pitcher;
2. pitcher skill with recent and longer-term shrinkage;
3. projected batting order and platoon-adjusted offence;
4. bullpen skill and workload over the last three days;
5. opponent-adjusted team offence and defence;
6. park, roof, temperature, wind, and precipitation;
7. rest, travel, time-zone change, and day/night state; and
8. defence and catcher effects only after the simpler model is stable.

Useful inputs include quality-of-contact measures. MLB explains that
[xwOBA](https://www.mlb.com/glossary/statcast/expected-woba) uses exit velocity, launch angle, and
some sprint-speed information, while [xERA](https://www.mlb.com/glossary/statcast/expected-era)
translates xwOBA to an ERA scale. Those definitions support the feature idea; they do not grant RATi
commercial data rights.

Model expected runs for both teams, then derive win probability by simulation or a fitted run
distribution. This makes moneyline, total, and expected-score outputs agree with each other.

### NFL model

NFL data is sparse. Use several past seasons with strong time decay and season-to-season shrinkage.
Quarterback status must be a first-class input.

High-value features:

1. opponent-adjusted offensive and defensive expected points added per play;
2. quarterback starter, availability, and estimated effect;
3. early-down passing and rushing efficiency, success rate, and explosive-play rate;
4. offensive-line, pass-rush, and coverage indicators when licensed and stable;
5. injury report, final inactive list, and depth-chart changes;
6. special-teams strength;
7. rest, bye, travel, time zone, surface, and roof; and
8. wind, precipitation, and temperature for outdoor games.

The open-source [nflfastR model project](https://github.com/nflverse/fastrmodels) is useful research:
it keeps expected-points and win-probability models, including a version that uses the pre-game
spread. Its [model description](https://github.com/nflverse/open-source-football/blob/master/_posts/2020-09-28-nflfastr-ep-wp-and-cp-models/nflfastr-ep-wp-and-cp-models.Rmd)
also demonstrates explicit calibration checks. The project warns that NFL data still belongs to its
respective owners, so it is not a substitute for a production rights review.

Predict expected margin first, then transform the margin distribution into win probability. Model
ties under the applicable market rules.

### NBA model

Raw points per game mix team skill with pace. Use possession-based ratings.

High-value features:

1. opponent-adjusted offensive and defensive points per possession;
2. expected active players and expected minutes;
3. regularized player or lineup impact with strong shrinkage;
4. rest, back-to-back state, travel distance, time zones, and altitude;
5. pace and the shooting, turnover, rebound, and free-throw components of efficiency;
6. recent form with limited weight; and
7. home and neutral-site effects.

The [NBA stats glossary](https://www.nba.com/stats/help/glossary?hidenav=true) defines offensive
rating as points per 100 possessions and pace as possessions per 48 minutes. These are the right
units for the model, but the NBA terms mean RATi needs licensed production data.

Predict possessions and points per possession for both teams, or directly predict margin with a
regularized model. Turn the margin distribution into a calibrated win probability.

### NHL model

NHL results are noisy, and the starting goalie matters. Separate regulation from overtime and
shootout rules.

High-value features:

1. five-on-five expected-goal share and expected goals for and against;
2. shot volume, shot location, angle, type, rebound, and rush context;
3. starting goalie, recent workload, and shrinkage-heavy goalie quality;
4. power-play and penalty-kill quality;
5. injuries and expected skaters;
6. rest, back-to-back state, travel, and time zones;
7. score effects and empty-net events removed from team-strength features; and
8. home and neutral-site effects.

Published hockey research has used logistic regression to give each shot a goal probability from
its context; one early example is
[Evaluating NHL Goalies, Skaters, and Teams Using Weighted Shots](https://arxiv.org/abs/1205.1746).
RATi can sum or simulate shot quality into team goal distributions, then explicitly model regulation,
overtime, and shootout outcomes.

### Missing data and readiness

Do not mean-impute an absent starting quarterback, pitcher, goalie, or lineup and still call the
forecast final. Store a readiness state:

- `not_ready`: identity, market rule, source, or required input is missing;
- `early`: only long-horizon information is available;
- `projected`: probable starters and projected lineup are available;
- `confirmed`: the league-specific key inputs are confirmed; or
- `stale`: one or more required sources are older than their limit.

The interface should say which required input is missing.

## Odds and market baseline

### Convert odds correctly

For American odds `a`, raw implied probability is:

```text
if a > 0: 100 / (a + 100)
if a < 0: -a / (-a + 100)
```

For a two-way market, the current proportional no-vig rule is a good transparent baseline:

```text
q_home = p_home / (p_home + p_away)
q_away = p_away / (p_home + p_away)
```

But apply it to both sides from the **same sportsbook, same market scope, and same snapshot**. Do not
combine the best home price from one book with the best away price from another and call the result
a market probability.

Then create a consensus by taking the median or a tested weighted mean of the de-vig probabilities
from eligible books. Keep the number of books and the cross-book range. A wide range is useful
uncertainty evidence.

The simple proportional method does not remove every pricing bias. Research documents
favorite-longshot effects and alternative normalization methods, including this
[normalization study](https://journals.sagepub.com/doi/pdf/10.1177/155862351801300302). RATi should
first ship the simple method, then test power or bias-adjusted mappings by league and odds range on
old data. Any learned correction must be fit only on past games.

### Market rules

Never compare prices unless these fields match:

- event and participants;
- full game versus regulation only;
- overtime and shootout treatment;
- listed-pitcher or action rules in baseball;
- spread or total points;
- sportsbook and jurisdiction where relevant;
- price status and maximum age; and
- observation time.

### Public numbers

For every final forecast, show:

- RATi independent win probability;
- market no-vig consensus and book count;
- model-market gap in percentage points;
- fair American price implied by the model;
- exact market snapshot time and age;
- forecast readiness and missing inputs; and
- whether the public decision was pass or lean.

Expected value can be calculated internally from the exact available price, but it should not be
presented as a promised return.

## Validation and model governance

### The market is a hard baseline

Betting odds are often strong forecasts. Research on online football markets found odds to be the
most accurate forecast among the compared information sources in that setting
([study](https://www.sciencedirect.com/science/article/pii/S0169207018301134)). This does not prove
every market is efficient. It means a sports model should be judged against the same-time market,
not only against a 50% guess or last season's win rate.

### Time-ordered evaluation

Use rolling-origin or walk-forward tests:

1. train on games before date A;
2. choose model settings on a later validation block;
3. calibrate on another later block;
4. test once on the newest untouched block;
5. move time forward and repeat for the next production version.

Never use random game-level train/test splits. Never let final lineups, corrected stats, closing
odds, or end-of-season ratings enter an earlier forecast row.

### Primary metrics

Accuracy and win/loss record are not enough for probability forecasts. Use:

- **Log loss:** strongly penalizes confident wrong forecasts.
- **Brier score:** average squared probability error.
- **Calibration intercept and slope:** whether forecasts are systematically too high, low, or
  extreme.
- **Reliability chart:** predicted probability against observed frequency, using enough games per bin.
- **Sharpness:** how much forecasts move away from 50%, interpreted only with calibration.
- **Market-relative skill:** Brier and log-loss change versus the same-time no-vig consensus.
- **Coverage and pass rate:** how often the model was ready and how often it abstained.

The statistical reason to use Brier and log loss is that they are proper scoring rules: they reward
honest probability estimates. See
[Gneiting and Raftery, “Strictly Proper Scoring Rules, Prediction, and Estimation”](https://doi.org/10.1198/016214506000001437).

For expected margin and score, also use mean absolute error, root mean squared error, interval
coverage, and distribution scoring.

### Betting-style metrics

Track these as secondary diagnostics:

- result at the exact frozen price;
- theoretical return after the book's price, with void rules;
- model probability versus the closing no-vig market;
- closing-line movement from the frozen price;
- performance by league, season, cutoff, favourite/underdog, home/away, probability band, and
  readiness state; and
- bootstrap confidence intervals, clustered by game date where practical.

Closing-line value is useful evidence, not proof of profitability. Even Pinnacle's own educational
material calls the closing line a common benchmark while noting that its reliability cannot be
fully established without bookmaker profit-and-loss data
([source](https://www.pinnacle.bet/betting-resources/en/betting-strategy/using-the-closing-line-to-test-your-skill-in-betting/7e6jwjm5ykejuwkq?page=7)).

### Promotion gate

A challenger can replace a production model only when all are true:

1. it beats the old model on untouched later games in primary scoring;
2. it does not materially worsen calibration;
3. the gain appears in more than one time block and is not one league-month anomaly;
4. all features existed by the forecast cutoff;
5. source coverage and missingness are acceptable in production;
6. the explanation remains truthful; and
7. the model and calibration artifact can be replayed exactly.

Do not promote a model because it had the highest simulated ROI among many tried variants. That is
a multiple-testing trap.

## Public performance page

The performance page is central to the RATi difference. It should show:

- all eligible forecasts and all excluded games;
- model version and active dates;
- forecast cutoff and readiness;
- settled count and date range;
- Brier score and log loss versus the market baseline;
- a calibration chart with sample counts;
- a full downloadable receipt for each forecast if the data license permits it;
- paper return only at the exact captured odds;
- correction and void counts; and
- plain language such as “early sample” when uncertainty is wide.

Keep these records separate:

| Record | Meaning |
| --- | --- |
| RATi model | Results of the pre-declared model decision rule |
| All RATi forecasts | Proper scoring of every eligible probability, including passes |
| Community | Combined public paper picks by users |
| My picks | The signed-in user's own paper-pick history |

Do not average community picks into the model record. Do not let a user pick retroactively become a
RATi model pick merely because it referenced a prediction row.

## Game and team page research

### Game page

The game page should answer these questions in order:

1. Who is playing, when, where, and under which market rules?
2. What does RATi forecast?
3. What does the market forecast at the same time?
4. Why did RATi differ?
5. What important input is missing or uncertain?
6. How did the probability change from early to final?
7. Which source and model receipt supports every number?
8. What was the final result and how was the forecast scored?

Best evidence blocks:

- opponent-adjusted team strength;
- expected and confirmed starters;
- player availability effect;
- rest, travel, and venue context;
- league-specific efficiency matchups;
- market movement; and
- a short “what changed” timeline.

Do not use head-to-head records, tiny recent streaks, or “team is 7-1 on Tuesdays” as primary
evidence. They are usually weak, overlapping, and easy to cherry-pick.

### Team page

Each team page should contain:

- current opponent-adjusted rating and rank;
- offensive and defensive component ratings;
- rating history with model-version markers;
- schedule and forecast history;
- home/away and rest splits only when the sample is large enough;
- player availability and expected role;
- calibration and model error for that team's games, labelled as noisy; and
- sources, freshness, and corrections.

Avoid a single unexplained “power score.” A rating should link to its definition and update history.

## Competitor findings

| Product | Public strength | What RATi should learn | Gap RATi can own |
| --- | --- | --- | --- |
| [Dimers](https://www.dimers.com/) | Many sports and markets, model probability, edge, best price, props, parlays, and paid tools | Users understand probability + edge + best price quickly | A more complete immutable record, source times, missing inputs, and model calibration |
| [Action Network](https://www.actionnetwork.com/app) | Live odds, alerts, expert picks, synced bet tracking, and personal analytics | Odds movement and personal result tracking are useful daily loops | RATi does not need sportsbook sync; it can provide a cleaner public research ledger |
| [TeamRankings](https://www2.teamrankings.com/about/about-our-predictions/) | Multiple model outputs, confidence, matchup data, and a long public methodology | Show disagreement between models and connect confidence to past similar forecasts | RATi can be stricter about proper scoring, exact snapshots, and source provenance |
| [Opta Analyst](https://theanalyst.com/articles/opta-football-predictions) | Clear probability storytelling, power ratings, and large simulations | Explain a rating system and season simulation in public | RATi can expose more of the receipt and keep independent versus market-aware forecasts separate |

RATi should not try to win by publishing more “best bets.” It should win by making every forecast
inspectable and every historical claim reproducible.

## Responsible product and Canadian risk review

This section is a risk map, not a legal opinion.

### Keep the product on the research side

The current boundaries are good:

- no real-money wager placement;
- no sportsbook account connection;
- no automatic bet tracking;
- no personalized stake size;
- no promise of profit; and
- public paper picks only.

If RATi later takes sportsbook advertising, affiliate fees, or referral payments, get Canadian and
Ontario gaming counsel before launch. Ontario's operator advertising rules may not directly govern
an independent prediction site, but they become highly relevant when the site promotes or refers
users to operators. Ontario restricts misleading gambling ads, youth appeal, athlete use, and broad
public advertising of inducements. The current Canadian industry advertising code also took effect
in 2026; see the [Ad Standards overview](https://adstandards.ca/resources/library/advertising-of-igaming-in-ontario/).

### Claims and disclosures

The Competition Bureau says performance claims need adequate and proper testing and must not be
misleading. Its current summary says not to make a performance claim unless it can be proved
([source](https://competition-bureau.canada.ca/en/deceptive-marketing-practices/types-deceptive-marketing-practices/false-or-misleading-representations-and-deceptive-marketing-practices)).

For RATi this means:

- publish the full denominator and date range behind win rate or ROI;
- do not call a backtest “live results”;
- include passes, voids, and source outages;
- disclose model changes;
- do not use “proven,” “safe,” “guaranteed,” or “lock”;
- disclose any sportsbook, data-vendor, league, team, or affiliate relationship beside the content;
- keep the community record separate from the company model record; and
- make corrections visible.

### Safer design

Even without real wagering, the site shows prices and betting-style returns. Use calm product rules:

- default to probabilities and evidence, not profit;
- make “pass” visually normal;
- do not add loss-chasing prompts, streak celebrations, urgency timers, or “win it back” copy;
- do not rank users by money or units;
- do not send push alerts framed as guaranteed opportunities;
- if operator links ever appear, add age/jurisdiction messages, clear affiliate labels, and a
  responsible-gambling link; and
- keep minors out of gambling promotion and targeting.

### Privacy

Paper picks are account-linked personal activity even when the public handle is a pseudonym. The
[Office of the Privacy Commissioner of Canada](https://www.priv.gc.ca/en/privacy-topics/privacy-laws-in-canada/the-personal-information-protection-and-electronic-documents-act-pipeda/pipeda_brief/)
lists accountability, purpose, consent, limited collection, limited use/retention, safeguards,
openness, access, and challenge rights among the core PIPEDA principles. British Columbia also has
its own substantially similar private-sector privacy law.

RATi should:

- collect no real wagering or sportsbook credentials;
- state why account and paper-pick data are kept;
- let users delete or anonymize their account subject to the public-record policy;
- avoid behavioural ad profiles from picks;
- keep private account identity separate from the public animal handle; and
- set a retention rule for IP, security, and event logs.

### Names and logos

Team names, league names, logos, uniforms, and sportsbook marks are different rights questions. A
data feed does not automatically include logo rights. Keep the current text-first visual system and
do not add league, team, player, or sportsbook logos until the contract or separate license covers
them. Do not imply league or sportsbook endorsement.

## Ninety-day research and build plan

### Days 1–14: rights and provider bake-off

- Send the rights questionnaire to MySportsFeeds, The Odds API, SportsDataIO, and Sportradar.
- Obtain trial or sample access.
- Build provider adapters behind the existing normalized event contract.
- Run the 14-day coverage, freshness, correction, and cost test.
- Change current public language to unvalidated baseline, latest snapshot, and community picks.
- Design model, community, and personal performance records separately.

Exit gate: at least one stats provider and one odds provider permit the intended commercial display,
derived-model, training, audit, and retention uses in writing.

### Days 15–35: historical warehouse and hard baselines

- Backfill several seasons with point-in-time-safe facts and odds when licensed.
- Add provider ID maps, event versions, market rules, availability, lineup, feature snapshots,
  model artifacts, and prediction outcomes.
- Build league-specific naive and dynamic-rating baselines.
- Build the same-time no-vig market consensus.
- Add walk-forward evaluation and a private calibration dashboard.

Exit gate: every training row can be replayed from facts available at its cutoff, and every league
has a model and market baseline.

### Days 36–60: league challengers in shadow mode

- MLB: pitcher, lineup, bullpen, park, and weather model.
- NFL: opponent-adjusted efficiency, quarterback, injury, rest, and weather model.
- NBA: possession ratings, availability/minutes, rest, travel, and pace model.
- NHL: expected-goal, goalie, special-teams, rest, and travel model.
- Fit calibration only on later validation blocks.
- Record early, update, and final forecasts without changing public calls.

Exit gate: shadow forecasts run automatically, stale inputs are visible, and later-game scoring is
reliable.

### Days 61–90: one-league public beta

MLB is the best first deep-model test because it supplies many observations and the pitcher/lineup
receipt is easy to explain. NFL has strong demand, but its small schedule and quarterback sensitivity
make fast validation difficult. It can remain a transparent rating baseline while the richer model
learns through the season.

This plan starts in late August. Days 61–90 therefore fall near the end of the MLB season and the
start of the NBA and NHL seasons. Start the MLB shadow model immediately, but do not force a thin
postseason sample into a public launch. Publish the first league that passes every gate. If none does,
keep all challengers in shadow mode and launch MLB with a clean new-season record in spring 2027.

- Publish the game receipt, model card, data-status page, and performance page.
- Show model versus market without calling the difference a proven edge.
- Keep all other league challengers in shadow mode until their gates pass.
- Run a weekly forecast-quality and source-correction review.
- Pre-register the next model change before inspecting its final test block.

Exit gate: the public record matches the stored ledger, all forecast stages are immutable, and the
model has no unresolved data-rights or critical freshness issue.

## Definition of done

RATi Sports is ready to call itself an evidence-first team prediction product when:

- every public field comes from a licensed or clearly permitted source;
- every prediction can be replayed from an immutable feature snapshot;
- source publish time and RATi collection time are visible;
- each league has its own documented model and fallback;
- predictions are calibrated on later data and compared with a same-time market baseline;
- every eligible forecast is settled and scored;
- model, community, and personal results are separate;
- missing starters, lineups, goalies, injuries, or stale prices are visible;
- public claims include sample size, date range, and uncertainty;
- corrections and model changes are preserved; and
- the interface never implies that a probability or paper result guarantees a return.

## Recommended next ten actions

1. Relabel the current output as an unvalidated team-form baseline.
2. Split the public result strip into model, community, and personal records.
3. Send the vendor rights questionnaire.
4. Start MySportsFeeds and The Odds API commercial trials.
5. Request comparable quotes from SportsDataIO and Sportradar.
6. Build a provider quality report from 14 days of parallel collection.
7. Add model-outcome settlement and proper scoring to the database.
8. Build one opponent-adjusted rating model per league.
9. Backfill point-in-time history and run walk-forward tests.
10. Launch one league only after its source, calibration, and public-receipt gates pass.

## Research source index

### Data and rights

- [Sportradar developer portal](https://developer.sportradar.com/getting-started/docs/get-started)
- [Sportradar Sports Data API](https://sportradar.com/media-tech/data-content/sports-data-api/?lang=en-us)
- [SportsDataIO developer access and commercial products](https://sportsdata.io/developers)
- [MySportsFeeds commercial pricing and feed groups](https://www.mysportsfeeds.com/feed-pricing/)
- [The Odds API documentation](https://the-odds-api.com/liveapi/guides/v4/)
- [The Odds API pricing](https://theoddsapi.com/pricing)
- [The Odds API terms](https://the-odds-api.com/terms-and-conditions.html)
- [SportsGameOdds pricing](https://sportsgameodds.com/pricing)
- [SportsGameOdds terms](https://sportsgameodds.com/terms)
- [Stats Perform products](https://www.statsperform.com/products/)
- [NBA.com terms](https://api-hub.nba.com/termsofuse)
- [NFL.com terms](https://www.nfl.com/legal/terms/)
- [NHL.com terms](https://www.nhl.com/info/terms-of-service)
- [MLB.com terms](https://www.mlb.com/official-information/terms-of-use)
- [Disney terms used by ESPN](https://disneytermsofuse.com/)
- [Open-Meteo commercial pricing and data license](https://open-meteo.com/en/pricing)
- [US National Weather Service API](https://www.weather.gov/documentation/services-web-api)

### Prediction and evaluation

- [Strictly Proper Scoring Rules, Prediction, and Estimation](https://doi.org/10.1198/016214506000001437)
- [Forecasting sports outcomes through machine learning: a systematic review](https://www.frontiersin.org/journals/computer-science/articles/10.3389/fcomp.2026.1883327/abstract)
- [Systematic review of AI for professional basketball prediction](https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0326326)
- [nflfastR model artifacts](https://github.com/nflverse/fastrmodels)
- [nflfastR model and calibration description](https://github.com/nflverse/open-source-football/blob/master/_posts/2020-09-28-nflfastr-ep-wp-and-cp-models/nflfastr-ep-wp-and-cp-models.Rmd)
- [MLB xwOBA definition](https://www.mlb.com/glossary/statcast/expected-woba)
- [MLB xERA definition](https://www.mlb.com/glossary/statcast/expected-era)
- [NBA statistics glossary](https://www.nba.com/stats/help/glossary?hidenav=true)
- [Expected-goal weighted shots in the NHL](https://arxiv.org/abs/1205.1746)
- [Sports odds normalization study](https://journals.sagepub.com/doi/pdf/10.1177/155862351801300302)
- [Online betting-market forecast efficiency study](https://www.sciencedirect.com/science/article/pii/S0169207018301134)

### Product, claims, and privacy

- [Dimers](https://www.dimers.com/)
- [Action Network app](https://www.actionnetwork.com/app)
- [TeamRankings prediction method](https://www2.teamrankings.com/about/about-our-predictions/)
- [Opta football prediction method](https://theanalyst.com/articles/opta-football-predictions)
- [Competition Bureau guidance on false or misleading claims](https://competition-bureau.canada.ca/en/deceptive-marketing-practices/types-deceptive-marketing-practices/false-or-misleading-representations-and-deceptive-marketing-practices)
- [Competition Bureau influencer and material-connection guidance](https://competition-bureau.canada.ca/en/deceptive-marketing-practices/types-deceptive-marketing-practices/influencer-marketing-and-competition-act)
- [Ad Standards overview of Ontario iGaming advertising](https://adstandards.ca/resources/library/advertising-of-igaming-in-ontario/)
- [Office of the Privacy Commissioner of Canada: PIPEDA in brief](https://www.priv.gc.ca/en/privacy-topics/privacy-laws-in-canada/the-personal-information-protection-and-electronic-documents-act-pipeda/pipeda_brief/)
