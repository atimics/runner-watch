# Data source ingestion roadmap

Last reviewed: 2026-08-30

## Goal

Runner Watch already collects Yahoo price bars, the Nasdaq Trader symbol directory, SEC filings,
SEC Company Facts, and optional Fintel short and borrow facts. The Nasdaq halt path is built and
connected, but its public-use review still blocks approval. The next sources should answer three
four questions that the app cannot answer well today:

1. Is the current quote tradable, or is the spread too wide?
2. Is there a real catalyst outside an SEC filing?
3. Is the move crowded, and what market regime is it happening in?
4. Do reviewed people linked to the issuer appear in relevant labor or federal court records?

The best next additions are a licensed quote pilot, reviewed biotech catalyst links, and licensed
news and corporate actions. They cover the largest remaining gaps and fit the ingestion system that
already exists.

## Ranked source backlog

| Rank | Source and feed | What it adds | Access and freshness | First use | Main caution |
| --- | --- | --- | --- | --- | --- |
| 1 | [Nasdaq Trader halt RSS](https://www.nasdaqtrader.com/Trader.aspx?id=TradeHaltRSS) | Halt reason, pause price, quote-resume time, and trade-resume time for Nasdaq and other exchange-listed securities | Free RSS; Nasdaq says it updates once a minute and must not be polled more often | Show a clear halt banner and stop treating a halted name as a normal runner | Accept and follow the feed terms. Keep Nasdaq attribution and do not imply the feed is ours. |
| 2 | [SEC Company Facts and Submissions APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) | Cash, debt, operating cash flow, shares outstanding, public float when reported, and filing history | No key; SEC says submissions are normally updated in under a second and XBRL in under a minute | Add dilution, cash-runway, and share-growth risk to ticker evidence | XBRL tags and units vary. Use filing acceptance time, not report end date, to avoid future data leaking into old training rows. |
| 3 | [Alpaca real-time stock data](https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data) | Trades, quotes, sizes, conditions, minute bars, and status events over WebSocket | API keys required; free IEX, paid full-market SIP, or delayed SIP | Measure spread, quote age, trade count, and cleaner intraday bars | IEX is only one exchange and is not a full-market volume source. Do not label IEX quotes as NBBO. Check public-display and storage rights before launch. |
| 4 | [Alpaca news](https://docs.alpaca.markets/us/reference/news-3) and [corporate actions](https://docs.alpaca.markets/us/reference/corporateactions-1) | Symbol-linked news plus splits, reverse splits, mergers, reorganizations, dividends, and name changes | API keys and plan rules apply; news may be delayed by 15 minutes without real-time access; corporate-action creation can be delayed | Add non-SEC catalyst evidence and adjust historical bars around splits | Store headline, timestamps, symbols, provider, and URL first. Do not store or republish full article text until the license is checked. |
| 5 | [ClinicalTrials.gov API v2](https://clinicaltrials.gov/data-api/api) | Trial phase, sponsor, status, enrollment, primary completion date, results, and record changes | Public REST API; refreshed Monday through Friday, usually by 9 a.m. ET | Add biotech catalyst and trial-status evidence | Sponsor names do not map cleanly to stock tickers. Only publish high-confidence issuer links. A trial date is not the same as an FDA decision date. |
| 6 | [FDA Drugs@FDA](https://www.fda.gov/drugs/drug-approvals-and-databases/drugsfda-data-files) | Application, sponsor, product, approval action, status date, priority review, and public review documents | Public ZIP and openFDA API; FDA says the source file updates each weekday morning | Confirm approvals and important application changes for mapped biotech issuers | FDA sponsor and SEC issuer names need a reviewed alias table. This source confirms actions; it is not a complete future PDUFA calendar. |
| 7 | [FINRA Equity Short Interest](https://www.finra.org/finra-data/browse-catalog/equity-short-interest) and [daily short-sale volume](https://developer.finra.org/docs/api-explorer/query_api-equity_reg_sho_daily_short_sale_volume) | Actual open short positions twice a month and daily off-exchange short-sale volume | Public data; the API may use a free public credential; short interest is published on the seventh business day after its settlement date | Add dated crowding evidence and historical features | Short interest and short-sale volume are different facts. Never present daily short-sale volume as current short interest. |
| 8 | [FRED observations API](https://fred.stlouisfed.org/docs/api/fred/series_observations.html) | VIX close, policy rates, Treasury yields, credit spreads, and other regime inputs | Free API key; cadence depends on the series | Add daily market-regime features to every scan group | Use ALFRED vintage dates for backtests when a series can be revised. This is context, not a live stock catalyst. |
| 9 | [SEC fails-to-deliver files](https://www.sec.gov/data-research/sec-markets-data/fails-deliver-data) | Settlement date, symbol, failed share balance, and reference price | Free ZIP files; published twice a month with a material delay | Add a slow historical crowding and settlement-risk feature | Fails are cumulative balances, can come from long or short sales, and are not proof of naked shorting. Keep this source out of live alerts. |
| 10 | [USAspending API](https://api.usaspending.gov/docs/endpoints) | Federal awards and award changes without an API key | Public API; ingest by award update date | Add verified government-contract evidence | Recipient names and subsidiaries are hard to map to public issuers. Start with a small reviewed issuer map. |
| 11 | [openFDA drug shortages](https://open.fda.gov/apis/drug/drugshortages/) and [enforcement reports](https://open.fda.gov/apis/drug/enforcement/) | Shortage status and dates, company names, recall class, affected products, and recall status | Public API and full downloads; shortages update daily and enforcement reports update weekly; a free key raises the request allowance | Add supply and recall risk for reviewed drug-company links | A product event is not automatically material to the public company. Keep old versions because FDA can revise existing records. |
| 12 | [NIH RePORTER API](https://api.reporter.nih.gov/) | Federal research awards, project dates, funding, principal investigators, organization IDs, and publications | Public JSON API; award records follow agency reporting cycles | Add source-backed grant awards and research-program context for biotech issuers | A grant to a researcher, hospital, or university is not an issuer award. Match the funded organization ID, not only keywords. |
| 13 | [GLEIF LEI API](https://www.gleif.org/en/lei-data/gleif-api) | Legal names, former names, addresses, parent relationships, and mapped identifiers such as ISIN | Free public API with no registration; Golden Copy and delta files update three times daily | Improve issuer, sponsor, recipient, and subsidiary matching before more event feeds are added | LEI coverage is not universal and an ISIN still needs a reviewed ticker mapping. Treat fuzzy name matches as proposals, not facts. |
| 14 | [FINRA OTC transparency](https://www.finra.org/filing-reporting/otc-transparency) | Delayed weekly or monthly ATS and non-ATS trade counts and share volume by security | Public delayed aggregates and file downloads | Add off-exchange activity context beside volume and short data | This is trade activity, not short volume and not proof of hidden buying. Automated or commercial use needs a specific terms review; attribution is required where allowed. |
| 15 | [SEC Form 13F](https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets) and [Form N-PORT](https://www.sec.gov/data-research/sec-markets-data/form-n-port-data-sets) data sets | Reported institutional and fund holdings, position value, shares, and filing identifiers | Free quarterly ZIP bundles built from filed structured data; amendments remain possible | Add slow ownership context and test whether reported fund interest helps longer-horizon research | Holdings are delayed, may omit positions, and are not a live flow signal. Resolve CUSIP, FIGI, or LEI to the issuer as of the report date. |
| 16 | [SAM.gov contract opportunities API](https://open.gsa.gov/api/get-opportunities-public-api/) | Solicitation, award, and sole-source notices with agency, dates, NAICS codes, and place of performance | Public search and API; the API needs a free registered-user key and updates active notices daily | Find early federal-contract context before an award appears in USAspending | An opportunity is not an award and often names products rather than issuers. Keep opportunity and award events separate. |
| 17 | [Federal Register API](https://www.federalregister.gov/developers/documentation/api/v1) | Agency rules, notices, public-inspection dates, document numbers, and source links | Public API with no key; documents cover 1994 onward | Add reviewed agency actions for sectors such as biotech, energy, mining, and defense | Broad keyword searches create noise. Require an agency filter and a reviewed issuer or product link before publishing. |
| 18 | [CFTC Commitments of Traders](https://www.cftc.gov/MarketReports/CommitmentsofTraders/index.htm) | Weekly open interest split by trader class for equity index, rate, energy, metal, and other futures | Free weekly files and public reporting API; normally released Friday at 3:30 p.m. ET for Tuesday positions | Add slow risk-regime context for the whole market or sector | This is futures positioning, not stock-level evidence. Preserve report and release dates and never attach it to one issuer. |
| 19 | [CourtListener RECAP](https://www.courtlistener.com/help/api/) | Searchable federal docket and filing metadata already present in the public RECAP archive | API account and plan rules apply; coverage is broad but incomplete because RECAP is not a complete copy of PACER | Use as the low-cost first pass for reviewed people and issuers | Missing RECAP results do not mean no case exists. A name match is only a review candidate. |
| 20 | [PACER Case Locator API](https://pacer.uscourts.gov/file-case/developer-resources) | Nationwide federal case and party index with court, case number, filing dates, title, and party fields | PACER account and authentication token required; searches are billable by result page and automation must not disrupt the service | Fill gaps after RECAP for a small, approved person watchlist | The PCL API is an index, not a full docket feed. Set a hard page budget. Never equate being a party with wrongdoing or material issuer risk. |
| 21 | [DOL OALJ adjudicatory decisions](https://www.dol.gov/agencies/oalj/topics/information/DECISIONS) | Labor-related ALJ decisions, orders, case numbers, program areas, parties, and public docket links | Public search tools exist, but no supported public collection API is documented; start with a reviewed metadata export or written access decision | Find reviewed whistleblower, wage, safety, OFCCP, and other labor-case context | Do not scrape an undocumented application at scale. Many matters are routine, dismissed, appealed, or unrelated to an issuer. Preserve disposition and role. |

“Free” means that a useful public or free-tier path exists; it does not mean unrestricted use or
free redistribution in a public app. Before any data is shown to users, record the exact endpoint,
account tier, terms URL, request limit, allowed storage period, display rights, attribution, and
termination rules in the source registry. Government sources still need their access rules and
request limits followed. Vendor free tiers must also have a paid-exit plan so a limit change cannot
silently break Pulse.

### Free access summary

- **No account or key:** SEC data sets, ClinicalTrials.gov, FDA downloads, USAspending, GLEIF,
  Federal Register, CFTC files, and DOL OALJ public decisions have a useful public path without
  sign-up. OALJ automation still needs an approved collection method.
- **Free key or account:** Alpaca's IEX tier, FRED, the higher openFDA allowance, FINRA query APIs,
  SAM.gov automation, and a CourtListener pilot need a credential or account. Limits and display
  rights still vary.
- **Terms-gated public files:** Nasdaq halt and FINRA OTC data can be viewed without paying, but
  public-product use must wait for a written terms decision.
- **Paid or metered access:** full-market SIP quotes and dependable licensed news still need a paid
  plan if the free path cannot meet coverage, freshness, or display-rights gates. PACER searches
  are metered even when quarterly fees may later be waived.

## Roadmap

The time estimates assume one engineer. Each phase should ship in shadow mode first. A source can
move to the public score only after its quality gate passes.

Current build status: the shared layer and normalized tables are complete. The Nasdaq halt parser,
raw archive, deduplication, worker, stale-feed health, evidence label, safety penalty, and hard veto
are connected for internal review. The public event gate prevents the halt evidence, penalty, and
veto from affecting users until the feed's terms review is approved. SEC Company Facts,
issuer facts, dilution evidence, and point-in-time ranker features are also built. SEC reporting
owner CIKs, private person/issuer review records, and private legal-case candidates are now built.
OFAC SDN, SAM.gov exclusions, and HHS LEIE have opt-in official collectors. Another 18 free
archives are registered behind the same private reviewed-import contract. All legal-risk collectors
remain disabled by default. `/roadmap` is the source of truth for product decisions; this document
keeps the detailed data-source work.

### Phase 0 — Source rules and shared data model (2–3 days)

Status: built and tested on 2026-08-24.

- Add a source registry with owner, terms URL, credentials, expected cadence, storage rule, and
  public-display rule.
- Reuse `SourceFetch` and `record_source_fetch` for every new collector.
- Preserve three times where they exist: when the event happened, when the source published it,
  and when Runner Watch collected it.
- Add stable entity links for CIK, ticker, exchange, and outside IDs. Store a confidence score and
  mapping method for sponsor or recipient name matches.
- Use GLEIF names, parent links, and mapped identifiers to suggest entity links. A reviewer must
  still approve fuzzy matches and ticker mappings.
- Add fixture-based parser tests, deduplication tests, and stale-source alerts before a collector is
  scheduled.

Done when every source has a visible last-success time, a clear dedupe key, and a written display
rule. No ranking change is part of this phase.

### Phase 1 — Trading halts (3–4 days)

Status: built and connected; public approval remains blocked by the feed terms review.

- Poll `https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts` once per minute during the extended
  US session.
- Store each state change using symbol, halt date/time, reason code, and resume times as the key.
- Add `is_halted`, `halt_reason`, `resume_quote_at`, and `resume_trade_at` to ticker evidence.
- Put a halt banner above price signals. Suppress normal “EARLY” wording while a halt is active.
- Keep halt data out of the runner score at first; treat it as a safety state.

Quality gate: parser fixtures cover active halt, updated resume time, and resumed trading; repeated
RSS rows do not create duplicate events; a stale feed is visible within two missed polling windows.

### Phase 2 — SEC fundamentals and dilution risk (5–7 days)

Status: built and connected; broader issuer review remains a quality task.

- Fetch Company Facts only for the active penny-stock universe, watched tickers, and new SEC
  catalyst tickers. Use nightly bulk data later if this becomes less efficient.
- Normalize the latest filed facts for cash, debt, current assets and liabilities, operating cash
  flow, shares outstanding, and public float when present.
- Compute simple, explainable evidence: quarter-over-quarter and year-over-year share growth,
  cash-to-market-cap, current ratio, and rough cash runway.
- Preserve accession number, form, period, unit, source tag, and filing acceptance time for every
  chosen fact. Keep the original Company Facts response in the existing document archive.
- Show missing or stale facts as unknown. Do not turn missing data into a negative score.

Quality gate: manually check 30 issuers across US GAAP, IFRS, and foreign-filer forms; historical
training rows only see facts accepted before their scan time; conflicting duplicate facts have a
deterministic selection rule and keep their provenance.

### Phase 3 — Quote and news vendor pilot (1–2 weeks, overlapping)

- Use Alpaca IEX on a limited watchlist to test the connection, retry logic, quote schema, trade
  conditions, and news/corporate-action formats.
- Store quote snapshots separately from bars: bid, ask, sizes, last trade, source feed, conditions,
  exchange, and observed time.
- Calculate `spread_pct`, `quote_age_seconds`, and `trade_count_5m`. Mark all IEX-derived values as
  single-exchange data.
- Compare Yahoo and Alpaca coverage over at least ten full trading sessions. Log missing symbols,
  late bars, bad prices, reconnect gaps, and corporate-action mismatches.
- Decide after the pilot whether to buy SIP. Use SIP before replacing Yahoo volume, calling a quote
  NBBO, or using the new bars as the main ranking feed.
- Ingest news metadata in shadow mode. Keep full article content out of storage until its rights are
  confirmed.

Quality gate: at least 98% of subscribed-symbol minutes arrive during the measured session, quote
freshness and reconnect gaps are reported, and the source-license registry allows the exact fields
that the public app will display. The comparison report, not preference, decides whether Yahoo is
kept as fallback or replaced.

### Phase 4 — Biotech catalysts (1–2 weeks)

- Build a reviewed alias table that links SEC issuers to ClinicalTrials.gov sponsors and FDA
  sponsors. Seed it only for biotech names in the active universe.
- Poll ClinicalTrials.gov after its weekday refresh. Save material changes in status, enrollment,
  phase, primary completion, results posting, and study dates.
- Poll Drugs@FDA each weekday and create approval or application-action events only after a
  high-confidence sponsor-to-issuer match.
- Add openFDA shortage and enforcement changes as separate risk events. Keep old record versions
  because the source can revise earlier records.
- Add NIH RePORTER awards only when the funded organization ID maps to the issuer. Do not publish
  keyword-only matches to a researcher, hospital, or university.
- Show the source link and state whether an event is a trial update or an FDA action.

Quality gate: at least 95% precision on a reviewed set of 100 proposed issuer links; low-confidence
links stay internal; revised or removed trial dates remain in history instead of overwriting what
was previously known.

### Phase 5 — Crowding and market regime (1 week)

- Ingest FINRA actual short interest twice a month and Reg SHO daily short-sale volume after each
  trading day. Keep the two datasets in separate tables and labels.
- Add FRED daily observations for a small fixed list such as VIX close, the effective federal funds
  rate, the 2-year Treasury yield, and a high-yield spread.
- Backfill SEC fails-to-deliver data for research, but do not use it in live alerts.
- Test FINRA OTC activity beside normal volume, short interest, and short-sale volume. Keep all four
  measures separate in storage and labels.
- Test CFTC futures positioning only as weekly market or sector context.
- Save the vintage or collection time used by each training group.

Quality gate: feature values reproduce the original source for a 30-symbol sample; labels clearly
show each dataset's age; backtests use only values that were public at the scan time.

### Phase 6 — Reviewed people and legal cases (1–2 weeks for a pilot)

Status: the private review schema, SEC reporting-owner CIK capture, PACER/OALJ parsers,
point-in-time research-context route, and reviewed official-archive importer are built. OFAC SDN,
SAM.gov exclusions, and HHS LEIE have scheduled opt-in collectors. No collector or public product
effect is enabled by default.

- Stage names from SEC ownership filings. Prefer the SEC reporting-owner CIK from Form 4 over a
  name-only identity. Keep funds, trusts, and other organizations separate from people.
- Require a reviewer to approve both the person identity and the person-to-issuer relationship
  before any legal search can run.
- Search CourtListener RECAP first. Use PACER PCL only for approved gaps, one immediate page at a
  time, with a daily billable-page cap and a client code for cost attribution.
- Use DOL OALJ decisions only through a documented, approved collection path. Store case number,
  party role, program area, decision date, disposition, and the official source link.
- Pull OFAC SDN and HHS LEIE as official bulk snapshots, but retain only the source hash and the
  minimal matched fields rather than the full personal-data files. Query SAM.gov only for approved
  people, using a small daily request budget and never writing the API key to the audit trail.
- Normalize GovInfo, SEC/DOJ/FTC/PCAOB/OCC/FDIC/FINRA enforcement, EPA/OSHA/NLRB/FDA/USITC
  records, and CFPB complaints through one official-record contract. Search-only archives enter
  through reviewed imports until a supported API or bulk route is confirmed.
- Keep evidence type explicit. A complaint, inspection, recall, contribution, payment, or award is
  not a final legal finding. Every imported record starts with `risk_label='unknown'`.
- Send every name or case hit to a private review queue. Exact names are not enough: verify middle
  name or initial, role, location, dates, SEC identity, and the case document.
- Keep identity approval separate from materiality. A reviewed match remains `unknown` until a
  reviewer explicitly labels it `watch` or `material` with a note.
- Never describe a complaint, party listing, open case, settlement, dismissal, or appeal as proof
  of wrongdoing. Sealed, expunged, corrected, or misidentified records must be removed from product
  context promptly.
- Do not change the runner score in this phase. Reviewed cases may enter private research context
  with source and review provenance only.

Quality gate: 100 sampled person links and 100 sampled case or official-record hits have documented
identity and role decisions; false-positive identity rate is below 1%; every billable PACER request
has a recorded page count; zero pending or rejected matches appear in research output; no API key
appears in ingestion locators, metadata, or archived source documents.

### Phase 7 — Lower-priority event sources (after the core proves value)

- Pilot USAspending for a small reviewed list of defense, energy, and health issuers.
- Pair SAM.gov notices with later USAspending awards, but never label a solicitation as revenue.
- Pilot Federal Register notices only for reviewed agency, issuer, and product links.
- Test SEC 13F and N-PORT holdings as delayed research context, not current institutional flow.
- Consider paid exchange corporate-action data only if Alpaca coverage is not good enough.
- Expand social sentiment only after price, halt, filing, and news provenance is reliable.

Do not prioritize scraped social posts, broad web scraping, or options flow yet. They add large
licensing and identity-mapping work, and many penny stocks have little or no useful options data.

## Shared tables to add

Keep raw fetch audit data in the current ingestion tables. Add only small normalized read tables:

- `security_quotes`: source, feed, ticker, observed time, bid, ask, sizes, last trade, conditions.
- `market_events`: source, outside event ID, ticker, event type, event time, effective time, status,
  source URL, and compact payload.
- `issuer_facts`: CIK, concept, value, unit, period, filed time, accession, form, and source tag.
- `entity_links`: source, outside entity ID, CIK, ticker, confidence, method, valid dates.
- `macro_observations`: series ID, observation date, vintage date, value, and collected time.

Use a source-specific ID plus its version or update time for deduplication. Never dedupe only by
headline or ticker.

## How sources earn a place in the score

1. **Collect:** save the source with full timestamps and provenance.
2. **Observe:** show it only in internal status and evidence views.
3. **Validate:** measure coverage, freshness, mapping precision, and revision rate.
4. **Shadow:** add candidate features to saved scan rows without changing the public order.
5. **Promote:** require better out-of-time validation and no new data-quality failure before the
   feature affects the score.

Useful source-level measures are success rate, item coverage, p50 and p95 delay, duplicate rate,
revision rate, unmatched-entity rate, and cost per trading day. Useful model measures remain hit
rate, false-positive rate, calibration, and performance on the newest untouched scan groups.

## Recommended first release

The next release should contain only:

1. An Alpaca IEX comparison collector for a limited watchlist, with no public score change.
2. Completion of the Nasdaq halt terms review and public approval decision.
3. A reviewed biotech issuer-link prototype before any catalyst calendar is published.

This gives Runner Watch a measured path away from unofficial Yahoo data, closes the current halt
policy blocker, and tests the highest-value new catalyst area without broadening into options tools.
