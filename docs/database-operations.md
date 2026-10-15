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
