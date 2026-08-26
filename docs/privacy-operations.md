# Privacy operations

This is the working GDPR runbook for Runner Watch. It is an engineering and operations control, not a claim that external contracts have already been signed.

## Record of processing

Keep this record current whenever a feature, table, provider, region, or retention period changes.

| Activity | Data | People | Purpose and basis | Recipient | Retention |
| --- | --- | --- | --- | --- | --- |
| Passkey account | Account name, credential ID, public key, device flags | Members | Provide login and account service; contract | Hosting and database provider | Account life; deleted on account deletion |
| Essential session | Hashed random session token, account ID, dates | Members | Keep a signed-in session; contract and security | Hosting, database, cache | 30 days maximum |
| Public comment | Body, ticker, internal account link, thread emoji alias | Comment authors | Publish the comment the member requested; contract | Hosting and public readers | Until author deletes it or deletes the account |
| Community call | Body, ticker, caller-ID link | Call authors | Publish the call under the selected caller ID; contract | Hosting and public readers | Until caller ID or account deletion |
| Caller-ID claim | Random animal name, internal owner, payment receipt | Members who claim IDs | Provide the requested identity; contract | Hosting, Stripe for paid claims | Ownership until deletion; payment records as legally required; name tombstone indefinitely with no owner or calls |
| Private journal and cases | Ticker, prices, times, thesis, evidence, outcomes | Members | Provide saved private tools; contract | Hosting and database provider | Until member or account deletion |
| AI research | Requested ticker, market evidence, report, model metadata | Members | Produce requested research; contract | OpenRouter and routed model provider | Local report until deletion; provider must use zero-data-retention routing |
| Billing | Stripe IDs, plan and subscription state, webhook ID | Paying members | Perform contract and meet accounting duties | Stripe and hosting | Webhook ID up to 400 days; Stripe records under its legal schedule |
| Abuse protection | IP-derived short-lived rate key, request and error logs | Visitors and members | Security and availability; legitimate interests | Hosting and cache provider | Rate windows minutes; logs 30 days maximum |

The app must not write visitor IDs, page views, dwell time, share events, seen state, notification state, reactions, advertising IDs, or cross-thread identity graphs.

## Retention schedule

- Authentication challenges: expire after 5 minutes and prune daily.
- Unfinished accounts with no passkey: prune after 15 minutes.
- Login sessions: expire after 30 days and prune daily.
- Passive tracking tables: purge on migration and every retention run; never write new rows.
- Stripe webhook event IDs: prune after 400 days.
- Member content: keep until the member deletes the item or account, unless a documented legal hold applies.
- Deleted caller identities: keep only the random animal handle, tombstone state, and deletion time. Remove owner, payment reference, claim time, cost, and calls.
- Application and security logs: configure providers for 30 days maximum. Do not log request bodies, cookies, passkey data, research evidence, exports, or deletion payloads.
- Backups: configure a 30-day maximum. Fly Volume snapshots currently default to 5 days. A deletion reaches live data immediately and backup copies through expiry. Keep the restricted request log outside the restored database, and replay every deletion recorded after a snapshot before the service reopens.
- Review this schedule every six months and test pruning against a restored non-production backup.

## Data subject requests

1. Accept access requests through the signed-in export. Accept deletion and other rights requests at privacy@rati.foundation until the signed-in deletion control is released.
2. Record the received date, request type, scope, verifier, owner, deadline, actions, recipients told, and completion date in the restricted request log.
3. Verify through a current passkey session where possible. Ask only for the minimum extra proof needed.
4. Search the account, passkeys, sessions, comments, aliases, caller identities, calls, private journal, cases, reports, billing mirror, provider tickets, logs, and current backups.
5. Respond without undue delay and normally within one month. A complex request may take up to two more months only when the person is told, with reasons, within the first month.
6. Send corrections or erasure to processors that received the data when required. Record exceptions and the legal reason.
7. Exports must exclude authentication secrets and session hashes. Use a secure authenticated download and `Cache-Control: no-store`.

## Processor register

| Processor | Work | Likely location | Required file before EEA launch |
| --- | --- | --- | --- |
| Fly.io | App, database, network, logs and backups | United States; current app region is San Jose | DPA, subprocessor list, transfer mechanism, deletion and log settings |
| Stripe | Checkout, customer, subscription and payment records | Global, including United States | DPA, transfer mechanism, retention and deletion procedure |
| OpenRouter | Routes private research evidence to a model | United States unless an EU service is contracted | DPA, zero-data-retention setting, provider policy filter, routed-provider list |
| Routed model provider | Generates a requested research report | Varies by selected endpoint | No-training and zero-data-retention endpoint proof, location and DPA/subprocessor terms |

Record the database and Redis vendors separately if they are not supplied by Fly.io. Review each subprocessor list quarterly and on change notices. Do not silently add analytics, session replay, advertising, support, or email processors.

## International transfers

The present Fly `sjc` region is San Jose, United States. Before serving people in the EEA, UK, or Switzerland, record the applicable adequacy decision or execute the correct Standard Contractual Clauses and transfer assessment for each non-local processor. Apply any required supplementary encryption, access, or notice controls. OpenRouter must use an EU endpoint where contracted or a zero-data-retention provider route with a documented transfer safeguard.

## Security controls

- Passkeys with user verification; no passwords or biometric templates on the server.
- Random sessions stored only as hashes; Secure, HTTP-only, SameSite=Lax cookies in production.
- TLS for public traffic, PostgreSQL and Redis; fail startup in production if database or cache transport is not encrypted.
- Hash rate-limit subjects before writing short-lived Redis keys. Set the same secret `RATE_LIMIT_HASH_KEY` on every web machine so the limit remains shared without storing an IP address or account ID in the key.
- Origin checks on state-changing browser requests, content security policy, HSTS, frame restrictions, and bounded rate limits.
- No request-body, cookie, credential, research-evidence, export, or deletion logging.
- Least-privilege production access, multi-factor authentication for infrastructure and processors, separate production credentials, secret rotation, and a reviewed access list each quarter.
- Daily backups with a 30-day maximum, restore tests, deletion replay, and encryption in transit and at rest.
- Dependency, container, and secret scanning in CI; patch critical issues promptly.

## Personal data breach

1. Start an incident record immediately. Preserve necessary evidence without copying unrelated personal data.
2. Record when the team became aware, systems and processors involved, data and people affected, likely effects, containment, recovery, and the risk decision.
3. Revoke exposed sessions and secrets, stop the leak, preserve deletion requests, and contact affected processors without undue delay.
4. Notify the competent authority within **72 hours** of awareness when the breach is likely to risk people's rights and freedoms. If facts are incomplete, file the first notice and update it in phases. Record reasons for any delay.
5. Tell affected people without undue delay when the risk is high, using plain language and concrete protective steps.
6. Record every personal-data breach, including the reason a notification was not required. Run a follow-up review and track every corrective action.

## DPIA screening

Screen every new feature before build. Start a DPIA when a change may create high risk, including large-scale profiling, systematic monitoring, special-category or criminal data, location or biometric processing, automated decisions with significant effects, data matching across contexts, vulnerable people, or a new intrusive technology.

The current design deliberately removes systematic behaviour monitoring. Re-screen before adding analytics, recommendations based on reading behaviour, public identity linking, social graphs, private-message scanning, location, biometrics, financial-account imports, or automated trading decisions. Record the screen even when a full DPIA is not required.

Primary references: [GDPR text](https://eur-lex.europa.eu/eli/reg/2016/679/oj), [EDPB individual-rights guide](https://www.edpb.europa.eu/sme/be-compliant/respect-individuals-rights_en), [EDPB breach guide](https://www.edpb.europa.eu/sme/assess-the-risks/data-breaches_en), [Fly.io regions](https://fly.io/docs/reference/regions/), and [OpenRouter zero data retention](https://openrouter.ai/docs/guides/features/zdr).
