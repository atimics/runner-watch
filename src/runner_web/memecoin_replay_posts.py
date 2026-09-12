"""Deliver queued memecoin GIFs during the replay worker's scheduled cycle."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from runner_web.db import connection
from runner_web.memecoin_replay_store import saved_replay
from runner_web.telegram import (
    AnimationDeliveryError,
    config_from_env,
    memecoin_alerts_enabled,
    send_animation,
)


def caption(payload: dict, *, origin: str) -> str:
    launch = "Launch recorded on chain" if payload["launch"] else "Launch evidence pending"
    label = str(payload["symbol"] or payload["token_address"][:8])
    return (
        f"New memecoin detected · {label}\n\n{payload['token_address']}\n"
        f"{launch}\n{len(payload['events'])} saved events · "
        f"{len(payload['frames'])} replay keyframes\n"
        "Coverage follows collected transactions.\n\n"
        f"Replay the network: {origin.rstrip('/')}/memecoins/coin/"
        f"{quote(payload['coin_id'], safe='')}?replay={payload['id']}#token-replay"
    )


def dispatch_memecoin_replays(
    *, origin: str, at: datetime | None = None, sender=send_animation, limit: int = 2
) -> dict:
    if not memecoin_alerts_enabled():
        return {"status": "disabled", "sent": 0}
    config = config_from_env()
    if not config.configured:
        return {"status": "unconfigured", "sent": 0}
    current = at or datetime.now(UTC)
    stamp = current.isoformat()
    with connection() as database:
        database.execute(
            "UPDATE memecoin_replay_posts SET status='uncertain',last_error='acknowledgement_lost' "
            "WHERE status='sending' AND updated_at<?",
            ((current - timedelta(minutes=5)).isoformat(),),
        )
        rows = database.execute(
            "SELECT coin_id,replay_id FROM memecoin_replay_posts "
            "WHERE status IN ('pending','retry') AND replay_id IS NOT NULL AND chat_id=? "
            "AND attempts<3 AND (retry_at IS NULL OR retry_at<=?) ORDER BY created_at LIMIT ?",
            (config.chat_id, stamp, max(1, min(limit, 5))),
        ).fetchall()
    sent = 0
    for row in rows:
        with connection() as database:
            claimed = database.execute(
                "UPDATE memecoin_replay_posts SET status='sending',"
                "attempts=attempts+1,updated_at=? "
                "WHERE coin_id=? AND status IN ('pending','retry') AND attempts<3 "
                "AND chat_id=? AND (retry_at IS NULL OR retry_at<=?) RETURNING attempts",
                (stamp, row["coin_id"], config.chat_id, stamp),
            ).fetchone()
        if not claimed:
            continue
        status, error, message_id, retry_at = "sent", None, None, None
        try:
            record = saved_replay(row["coin_id"], row["replay_id"], with_gif=True)
            if not record:
                raise ValueError("missing_replay")
            message_id = sender(config, record["gif"], caption(record["payload"], origin=origin))
            if type(message_id) is not int or message_id <= 0:
                raise AnimationDeliveryError("uncertain")
            sent += 1
        except AnimationDeliveryError as exc:
            status = exc.status
            if status == "retry":
                status = "failed" if claimed["attempts"] >= 3 else "retry"
                retry_at = (current + timedelta(seconds=exc.retry_after)).isoformat()
            error = "telegram_" + status
        except ValueError:
            status, error = "failed", "replay_quality_failed"
        except Exception:
            status, error = "uncertain", "acknowledgement_lost"
        with connection() as database:
            database.execute(
                "UPDATE memecoin_replay_posts SET status=?,message_id=?,retry_at=?,last_error=?,"
                "updated_at=? WHERE coin_id=? AND status='sending'",
                (status, message_id, retry_at, error, stamp, row["coin_id"]),
            )
    return {"status": "checked", "sent": sent}
