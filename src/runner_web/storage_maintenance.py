"""Resumable storage migration: python -m runner_web.storage_maintenance --help."""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor

from runner_web import replay_objects
from runner_web.collection_retention import compact_metadata, prune_collection_history
from runner_web.db import close_database_pool, connection


def migrate_gif(replay_id: str) -> dict:
    with connection() as database:
        row = database.execute(
            "SELECT gif_bytes,gif_sha256,gif_object_key FROM memecoin_replays WHERE id=?",
            (replay_id,),
        ).fetchone()
    if not row or row["gif_object_key"]:
        return {"migrated": 0, "bytes": 0}
    content = bytes(row["gif_bytes"])
    key = replay_objects.save_gif(content, row["gif_sha256"])
    # Only a verified object replaces an inline copy. Each row commits independently.
    with connection() as database:
        changed = database.execute(
            "UPDATE memecoin_replays SET gif_object_key=?,gif_bytes=? "
            "WHERE id=? AND gif_object_key IS NULL AND gif_sha256=?",
            (key, b"", replay_id, row["gif_sha256"]),
        ).rowcount
    return {"migrated": max(0, changed), "bytes": len(content) if changed else 0}


def migrate_gifs(*, limit: int = 100, workers: int = 2) -> dict:
    if not replay_objects.bucket():
        raise ValueError("MEMECOIN_REPLAY_BUCKET is required")
    with connection() as database:
        ids = [
            row[0]
            for row in database.execute(
                "SELECT id FROM memecoin_replays WHERE gif_object_key IS NULL ORDER BY id LIMIT ?",
                (max(1, min(limit, 1000)),),
            ).fetchall()
        ]
    if ids:
        replay_objects.client()
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 4))) as pool:
        results = list(pool.map(migrate_gif, ids))
    return {key: sum(result[key] for result in results) for key in ("migrated", "bytes")}


def compact_helius_metadata(*, after_id: str = "", limit: int = 100) -> dict:
    with connection() as database:
        ids = [
            row[0]
            for row in database.execute(
                "SELECT id FROM ingestion_runs WHERE source='helius' AND feed='memecoins' "
                "AND id>? ORDER BY id LIMIT ?",
                (after_id, limit),
            ).fetchall()
        ]
    changed = 0
    for run_id in ids:
        with connection() as database:
            row = database.execute(
                "SELECT metadata_json FROM ingestion_runs WHERE id=?",
                (run_id,),
            ).fetchone()
            if not row:
                continue
            original = json.loads(row[0])
            compact = compact_metadata(original)
            if compact != original:
                database.execute(
                    "UPDATE ingestion_runs SET metadata_json=? WHERE id=? AND metadata_json=?",
                    (json.dumps(compact, separators=(",", ":")), run_id, row[0]),
                )
                changed += 1
    return {"scanned": len(ids), "changed": changed, "after_id": ids[-1] if ids else after_id}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("migrate-gifs", "compact-metadata", "prune-logs"))
    parser.add_argument("--all", action="store_true", help="Continue until the backlog is empty")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    after_id = ""
    while True:
        if args.action == "migrate-gifs":
            result = migrate_gifs(limit=args.limit, workers=args.workers)
            more = result["migrated"]
        elif args.action == "compact-metadata":
            result = compact_helius_metadata(after_id=after_id, limit=args.limit)
            after_id = result["after_id"]
            more = result["scanned"]
        else:
            result = prune_collection_history(limit=args.limit)
            more = result["detail_runs"] + result["summaries"] or result.get("busy", False)
        print(json.dumps({"action": args.action, **result}), flush=True)
        if not args.all or not more:
            break
        if result.get("busy"):
            time.sleep(2)


if __name__ == "__main__":
    try:
        main()
    finally:
        close_database_pool()
