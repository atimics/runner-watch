# Sports profiles, game opinions, and a layered glyph

Research date: 24 September 2026 UTC.

## Recommendation

Make teams and athletes lasting profiles that people can follow. Give each game or tournament a dated page that connects its participants, facts, forecasts, and result.

Keep the stock row layout: tag, identity, right-aligned value, then a small glyph at the far right. A game's actual score remains the large value. The actual leader or winner gets the larger, highlighted name on the left. Keep both names equally strong before play and during a tie. Keep league text with the identity.

The sports glyph should explain a named forecast, such as **ARI to win this game**. Its outer ring shows the forecast probability. Its inner pieces show the measured changes that produced that probability. Opening it shows the saved calculation, inputs, sources, and time.

At row size, start with two layers. Give source coverage and open questions space in the detail view. This is a design hypothesis to test with users.

## What we have today

This audit uses main at [084ee69748f63c83d811fa30920281a8ad681f11](https://github.com/atimics/runner-watch/tree/084ee69748f63c83d811fa30920281a8ad681f11). The compact sports row in [PR #391](https://github.com/atimics/runner-watch/pull/391), head b0a9775, was open at the time of research. The prototype in this directory is a separate design study.

| Area | Current implementation | Product meaning |
| --- | --- | --- |
| Game identity | League and provider event ID; embedded team IDs, names, records, scores, start time and venue | A dated event already has a useful identity |
| Team/player history | Stored player appearances linked to event and team | A base for lasting profiles and past participation |
| Numeric forecast | Season records plus a fixed league home adjustment | A simple team baseline |
| Model versus market | Removes the two-sided bookmaker margin; compares the implied chance with the baseline | A separate price comparison |
| Flash opinion | Separate probabilities, reason, author/model, evidence fingerprint and capture time | Another attributed opinion |
| Evaluation | Pregame results, accuracy, Brier score, probability buckets and market comparisons | A base for judging forecasts over time |
| Golf | Event leaderboard, rounds, leaders and team formats | A field of participants with sport-specific results |

Code: [event and forecast normalization](https://github.com/atimics/runner-watch/blob/084ee69748f63c83d811fa30920281a8ad681f11/src/runner_web/sports.py#L194), [player context](https://github.com/atimics/runner-watch/blob/084ee69748f63c83d811fa30920281a8ad681f11/src/runner_web/sports.py#L3818), [Flash evidence and recording](https://github.com/atimics/runner-watch/blob/084ee69748f63c83d811fa30920281a8ad681f11/src/runner_web/sports.py#L3985), [model evaluation](https://github.com/atimics/runner-watch/blob/084ee69748f63c83d811fa30920281a8ad681f11/src/runner_web/sports.py#L3086).

The current player context uses each team's latest stored game as a roster reference. It looks back up to 210 days and reports the team's win rate when that player appeared. Label this **past appearances**. Confirmed lineup status and a player's measured contribution require their own data and methods.

The sports attention value is a sorting rule: signal priority, model–market gap, and market movement. Give it an ordering role. Reserve a percentage label for a defined probability. The projected game score also comes from the probability and a total; treat it as another presentation of that forecast. [Current ranking and score projection](https://github.com/atimics/runner-watch/blob/084ee69748f63c83d811fa30920281a8ad681f11/src/runner_web/sports.py#L2310).

The shared sports assessment already distinguishes model chance, market chance, and model edge. Preserve these three meanings in the interface. [Assessment adapter](https://github.com/atimics/runner-watch/blob/084ee69748f63c83d811fa30920281a8ad681f11/src/runner_web/market_assessments.py#L126).

## Teams and players as the lasting subjects

Use **Games / Teams / Players** as views of one sports system. Keep Games as the first view for a score-focused visit. Following a team or athlete builds a personal feed of related games and saved calls.

| Type | Row identity | Right-side value | Profile/page contents |
| --- | --- | --- | --- |
| Team | Team name, abbreviation, league | Season record or a clearly selected performance measure | Roster, form, upcoming games, past results, news and forecast history |
| Athlete | Name, team or tour, position | A named, sport-specific performance measure | Season splits, availability, participation, upcoming events and related opinions |
| Game | Participants, league, start/status | Actual score or start time | State of play, participants, saved pregame call, later live calls and final result |
| Tournament | Event name, tour, round | Leader/result or start time | Field, leaderboard, rounds and named outcome forecasts |

A team abbreviation is a display label. Use a stable internal ID plus provider mappings for identity. A player can change teams; store membership with dates. Store expected, confirmed and actual participation as different states. This keeps historical games accurate after a trade.

This approach fits established sports data structures: Sportradar separates competitors, players, seasons and events, and exposes historical membership and ID mappings. That is a useful reference for our data model; provider choice remains a separate decision. [Sportradar overview](https://developer.sportradar.com/soccer/reference/soccer-overview).

The relationship is:

    Athlete → dated membership → Team
    Team or athlete → participation → Game or tournament
    Opinion → event + participant + outcome + time
    Evidence → opinion + source + source time

Use an event-participant relation for team games, individual matches, doubles and tournament fields. Keep sport-specific scores: baseball runs, tennis sets, golf strokes/to-par, and match-play points. A typed score structure supports those differences.

A profile-level outlook also needs a named target: next game, season wins, playoff qualification, top-10 finish, or another defined outcome. The label and time horizon travel with its glyph.

## What “our opinion” should mean

Every opinion needs a subject, outcome, author and time. A useful public sentence is:

**RATi baseline: ARI 56.7% to win. Saved before the game.**

The detail view can add: **Season record +10.2 percentage points; playing away −3.5 points. Market comparison 51.1%; model gap +5.6 points.** These figures are illustrative.

Keep the meanings explicit:

| Meaning | Question it answers | Display |
| --- | --- | --- |
| Forecast probability | How often would this outcome happen in similar situations? | Named participant, outcome, percentage and phase |
| Forecast explanation | What changed this model's output? | Signed model terms and their inputs |
| Evidence coverage | Which inputs were available and current? | Plain status, source time, expected/confirmed status |
| Model track record | How well did past calls hold up? | Model version, period, league, sample size and measured results |
| Market gap | How far is this forecast from a comparable market price? | Same outcome and time, percentage-point difference |
| Community opinion | What do people think? | Named callers, sample size and saved calls |

Use **RATi baseline**, **Flash view**, and **Community calls** as attributed voices. Choose the primary house view through an explicit policy. Keep previous calls intact when that policy changes.

The current baseline can select a value side that differs from the favorite. Flash's current validation requires its chosen side to have the larger forecast probability. Give those distinct fields: **forecast favorite** and **market value side**. The numeric model supplies numeric contributions; written analysis supplies linked claims and context. [Flash validation](https://github.com/atimics/runner-watch/blob/084ee69748f63c83d811fa30920281a8ad681f11/src/runner_web/sports.py#L528).

The current Flash confidence default follows the size of its probability. A public quality label should instead come from measured forecast performance and the current evidence state. A 60% forecast and a well-supported forecast answer different questions.

## An exact explanation we can build now

The current model is team-form-v1. It smooths each season record:

    team rate = (wins + 8) / (wins + losses + 16)
    home chance = 50% + 65 × (home rate − away rate) + home adjustment
    final home chance = clamp(home chance, 18%, 82%)

The league home adjustments are MLB +3.5, NFL +5.5, NBA +6.0, and NHL +4.0 percentage points. The away terms reverse the home terms. With missing records, the baseline starts at 50% and applies the home adjustment, with a thin-data state. [Exact current formula](https://github.com/atimics/runner-watch/blob/084ee69748f63c83d811fa30920281a8ad681f11/src/runner_web/sports.py#L409).

For illustrative ARI 90–60 at COL 64–86:

| Step for ARI | Change | Running probability |
| --- | ---: | ---: |
| Starting point | — | 50.0% |
| Smoothed season record | +10.1807 pp | 60.1807% |
| Away venue adjustment | −3.5 pp | 56.6807% |
| Clamp adjustment | 0.0 pp | 56.6807% |

ARI −115 and COL −105 yield a normalized market chance of 51.0834% for ARI. The gap is +5.5973 points after the model's stored rounding, shown as +5.6. The current WATCH rule starts at a five-point gap; LEAN starts at two points. These are current application rules and illustrative inputs.

The attached [replay](replay.py) runs the real forecast function and checks this reconstruction. Its [saved output](replay.json) records the numbers.

## The glyph

Keep a consistent family with stocks and coins: a compact circular mark at the far right, an internal explanation, and a clear path to detail. Give each domain its own stated units. Current stock and coin indicators use attention contributions and other market-specific signals. Sports gets forecast probability and measured forecast terms. [Current shared indicator implementation](https://github.com/atimics/runner-watch/blob/084ee69748f63c83d811fa30920281a8ad681f11/src/runner_web/stock_indicator.py#L137).

| Layer | Meaning | Rule |
| --- | --- | --- |
| Outer ring | Probability for the named outcome | Fixed outer size; arc length from 0–100% |
| Inner pieces | Share of absolute model adjustment | One stable category per factor; hatch terms that lower the named outcome's chance |
| Detail view | Exact calculation and evidence | Signed numbers, baseline, final result, inputs, source times and model version |

For the example, season record makes up about 74.4% of the absolute adjustment and venue 25.6%. Those shares describe the adjustment mix. The outer ring holds the 56.7% win probability. Show exact signed amounts in detail, where their meaning is easiest to read.

Keep category order and colors stable. Use at most four groups in a row glyph. Group small terms into Other and expose them in detail. A zero-effect trace has an empty inner area. A missing trace has an explicit unavailable state. Keep a 44px touch target around a 30px phone glyph or 40px desktop glyph.

Opening the glyph shows a side panel on a wide app page or a full detail view on a phone. The game list retains its row rhythm. The prototype uses a full detail view at both sizes to keep this study focused.

The proposed default has two visual layers. A center evidence marker is a later option if users can reliably distinguish it. Start with plain **Record model · lineup context available** or **Lineup pending** text in the detail view.

For precise comparison, use aligned bars and signed values in that view. Graphical-perception research found position judgments more accurate than angle/area judgments in its tested tasks. This supports the detail treatment; the small glyph still needs its own usability test. [Heer and Bostock, CHI 2010](https://idl.cs.washington.edu/files/2010-MTurk-CHI.pdf).

Pair color with sign, labels and hatching. Essential values remain available through keyboard, touch and visible text. [W3C guidance on color](https://www.w3.org/WAI/WCAG22/Understanding/use-of-color.html).

## Adding richer sport models

The following are proposed research inputs. Their numeric weights require training, testing and a saved model trace.

| Sport | Candidate groups | Model output and display |
| --- | --- | --- |
| MLB | Team hitting; starting pitcher/bullpen; confirmed lineup; park/weather/rest | Pregame win probability; a separate live model as play advances |
| NBA | Opponent-adjusted offense/defense; expected player minutes; availability; rest/travel | Win probability and a separate score distribution |
| NFL/NHL | Team strength; key-player availability; matchup; venue/rest | League-specific models with explicit overtime/settlement rules |
| Golf | Baseline skill; course fit/history; field; conditions | Win/top-N/make-cut chances from a tournament model |
| Player outlook | Role, minutes/usage, opponent and availability | A named game statistic or threshold, with its own units |

NBA's own teaching material uses possession-based rates for meaningful team comparisons. This is a better direction for future NBA inputs than raw point totals alone. [Jr. NBA](https://jr.nba.com/basictraditional-stats-vs-advanced-stats/).

MLB win expectancy uses score, inning, outs, baserunners, and run environment. [MLB glossary](https://www.mlb.com/glossary/advanced-stats/win-expectancy).

Data Golf's published methodology separates baseline skill, course adjustments and tournament simulation. It offers a useful pattern for player-to-event forecasting. The methodology page dates to 2021; use it as a design reference. [Data Golf methodology](https://datagolf.com/predictive-model-methodology).

For future models, save contributions in their actual units. SHAP's waterfall documentation shows that some classifier explanations use log-odds. A probability-point display needs a matching explanation method or an explicit conversion, plus any calibration/clamp term. [SHAP waterfall documentation](https://shap.readthedocs.io/en/latest/example_notebooks/api_examples/plots/waterfall.html).

Describe these terms as effects on the model's forecast. Claims about an athlete causing an outcome need a separate causal method and evidence. [SHAP guidance on causal interpretation](https://shap.readthedocs.io/en/latest/example_notebooks/overviews/Be%20careful%20when%20interpreting%20predictive%20models%20in%20search%20of%20causal%20insights.html).

## Timing comes first

The audit found different snapshot rules in the list and detail paths:

- The list selects the latest stored prediction for the event.
- The detail page selects the last prediction observed at or before the game start.
- Ingestion can store a new baseline after play starts or finishes.

The local replay saved a pregame call at 19:00 and a final-state update at 23:00 for a 20:00 game. The list returned 56.8402% at 23:00. The detail returned the sealed 56.6807% call at 19:00. This is a reproduced code-path difference using synthetic data.

Use one opinion ID and one explicit phase across list, detail and explanation. A pregame label should select the saved pregame call everywhere. A later live call gets its own model, inputs and time. Preserve both through settlement. [List selection](https://github.com/atimics/runner-watch/blob/084ee69748f63c83d811fa30920281a8ad681f11/src/runner_web/sports.py#L2479), [detail selection](https://github.com/atimics/runner-watch/blob/084ee69748f63c83d811fa30920281a8ad681f11/src/runner_web/sports.py#L3929), [ingestion](https://github.com/atimics/runner-watch/blob/084ee69748f63c83d811fa30920281a8ad681f11/src/runner_web/sports.py#L1428).

A saved opinion needs:

- Event ID, named participant, outcome, author/model and version.
- Phase, observation time, source times and the event's scheduled/actual start history.
- Input values and source references, input hash and evidence fingerprint.
- Starting value, output units, signed component values, final probability, calibration and clamp adjustments.
- A matching market snapshot when making a comparison.
- Coverage states and reasons, settlement rules, result and evaluation record.

Odds and records need the same historical discipline as the forecast. Capture the input values themselves so an old view remains explainable after the team record changes.

## Build order and acceptance checks

1. **Unify saved opinions.** Resolve one explicit pregame/live opinion for row, detail and result history. Cover changed start times, postponed/cancelled games, missing snapshots and postgame backfills.
2. **Expose the existing calculation.** Store season-record and venue contributions, plus any clamp term. Exact terms must reconstruct the stored probability within its rounding precision.
3. **Add the two-layer glyph.** Keep current stock-style geometry. Show the subject and phase beside the number. Let a user open the same saved view by mouse, touch or keyboard.
4. **Add team and athlete profiles.** Build stable IDs, dated memberships and event participation. Link current appearance context with an honest label.
5. **Improve one sport model at a time.** Begin with the data and outcomes that users value most. Save each model's components and evaluate it before making it the main view.

Test the design with these tasks: identify the score leader; name the forecast subject; tell pregame from live; find the biggest supporting and opposing terms; find pending lineup information; return to the list. Include a game where the leader differs from the forecast favorite, a tied game, a missing-input case and a golf tournament.

For forecast quality, use chronological evaluation by league, season and model version. Compare against simple and market baselines at matching times. Report sample size, coverage and uncertainty alongside Brier/log loss and reliability plots. Calibration means that outcomes assigned a given probability occur at roughly that rate over many cases. Brier score also reflects other aspects of predictive performance. [Scikit-learn calibration guide](https://scikit-learn.org/stable/modules/calibration.html).

This study delivers the recommended structure, a replayable audit, and an interactive row/detail prototype. Production delivery follows the first three steps above.
