# Database storage and backups

Production Uptime checks free space on every running `runner-watch-db` machine
every five minutes. It reports a failure at 80% use or less than 5 GiB free.
The check reads the `/data` filesystem, so it also works while PostgreSQL is
recovering. Stopped copies keep their existing state.

The disk check and daily snapshot job use `FLY_DATABASE_API_TOKEN` from GitHub
Actions secrets. Configure a Fly token scoped to `runner-watch-db` before
enabling these workflow changes. Record its expiry and renew it before that
date. The app deployment keeps its own `FLY_API_TOKEN`.

For a full disk, inspect the active machine, its volume, and available
snapshots. Expand the active volume, verify filesystem space, and check
PostgreSQL recovery. Once database checks pass, start any stopped worker and
verify worker health, feed timestamps, and the production browser sweep.

Use `python3 scripts/check-database-storage.py --app runner-watch-db` for a
manual capacity check. The backup script discovers `pg_data` volumes attached
to running database machines and waits for a new completed snapshot for each
volume. It requires at least one running database volume. This keeps scheduled
backups focused on the current database, including any running replicas.
The job schedules all selected volumes before waiting and allows 30 minutes
for their snapshots to finish. A retry resumes any snapshot that is already
running and still verifies its completed state. The workflow has a 35-minute
limit to allow time for setup and the snapshot requests.

## Replay GIF storage

Set `MEMECOIN_REPLAY_BUCKET` to a private bucket, with `AWS_ENDPOINT_URL_S3`,
`AWS_REGION`, `AWS_ACCESS_KEY_ID`, and `AWS_SECRET_ACCESS_KEY` supplied by the
storage provider. Fly's `fly storage create --app runner-watch-ratimics` sets
the AWS secrets. Set the replay bucket name separately. Keep the bucket for
the lifetime of the saved replay links; its objects use SHA-256 keys.

After deployment, take a completed database snapshot. Run these commands in
the deployed app environment:

```sh
python -m runner_web.storage_maintenance migrate-gifs --all --limit 100 --workers 2
python -m runner_web.storage_maintenance compact-metadata --all
python -m runner_web.storage_maintenance prune-logs --all --limit 500
```

Each GIF upload is read back and verified before its row switches to the object
key. The command resumes from inline GIF rows after an interruption. Each row
commits separately. Keep the same bucket configured when rolling back code.
After migration, rollback targets must support object-backed GIFs.

Verify that every replay has an object key, that inline GIF bytes are zero,
and that old pinned GIF, JSON, and receipt routes still pass their hash checks.
Ordinary vacuum makes retired database pages reusable. To return pages to the
filesystem, use a planned `VACUUM (FULL, ANALYZE)` on `memecoin_replays`, then
`ingestion_runs` and `ingestion_items`. This holds a table lock and needs space
for the remaining live rows and rebuilt indexes. Set a short lock timeout,
verify current free space, and run one table at a time during a quiet period.
Take a fresh completed snapshot after the migration and verify storage checks.

## Collection history

Coverage metadata records stream counts, a stream-list hash, event totals, and
budget information. Current stream cursors remain in `memecoin_chain_cursors`.
Each saved run's metadata has a 16 KiB ceiling; larger metadata gets a compact
hash and count summary. Raw source documents and normalized facts keep their
existing retention rules.

The outcome worker clears diagnostic item rows and metadata in batches of 500
runs every ten minutes. It retains details for 30 days or the newest 100,000
runs, whichever window is smaller. Summaries retain status, source, timestamps,
counts, errors, and content hashes for 90 days or the newest 500,000 runs.
Headers referenced by normalized facts, coin records, and each feed's latest
status remain available. These provenance exceptions can exceed the summary cap.
Configure the windows with `COLLECTION_DETAIL_DAYS`, `COLLECTION_DETAIL_RUNS`,
`COLLECTION_SUMMARY_DAYS`, and `COLLECTION_SUMMARY_RUNS`. The worker records each
batch under `worker_state.collection_last_prune`.
