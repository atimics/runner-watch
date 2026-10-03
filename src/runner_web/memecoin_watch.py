"""Creator selling and bundled launches, watched with one-credit reads.

Creator selling: every cycle, read the creator's token account for each coin
on the board (100 accounts a credit) and compare with the last balance. A
drop is a sale or a move to another wallet, the usual step before selling.

Bundled launches: once per coin, list the bonding curve's signatures (a
credit per 1,000) back to the launch, and when several transactions landed in
the launch slot, read those few to see who bought and how much of the supply.

Both become findings in the shape the assessment already reads, so they set
AVOID and show their receipts, and both can alert the Telegram channel.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from runner_web.memecoin_chain_parser import parse_events, valid_transactions
from runner_web.memecoin_chain_prices import Rpc, read_accounts, token_amount
from runner_web.solana_keys import (
    TOKEN_2022_PROGRAM,
    TOKEN_PROGRAM,
    bonding_curve,
    token_account,
)

LOG = logging.getLogger(__name__)
# Pump mints one billion tokens with six decimals.
PUMP_SUPPLY = 1_000_000_000.0
# A drop smaller than this share of the creator's holding is dust or fees.
MIN_SELL_SHARE = 0.01
CREATOR_COINS = 200
# Launch bundles: this many transactions in the launch slot, from this many
# wallets, together taking this share of supply.
BUNDLE_MIN_TRANSACTIONS = 3
BUNDLE_MIN_WALLETS = 3
BUNDLE_MIN_SUPPLY_PCT = 10.0
BUNDLE_CHECKS_PER_CYCLE = 10
BUNDLE_SIGNATURE_PAGES = 5
BUNDLE_MAX_TRANSACTIONS = 8
# A coin whose check keeps failing is given up after this many cycles.
BUNDLE_MAX_ATTEMPTS = 3
FINDING_HOURS = 24


def _creator(row: dict[str, Any]) -> str | None:
    receipt = row.get("discovery") or {}
    for key in ("declared_creator", "pool_creator"):
        wallet = receipt.get(key)
        if wallet and wallet != "1" * 32:
            return str(wallet)
    return None


def _finding(
    kind: str,
    title: str,
    token: str,
    wallet: str,
    signatures: list[str],
    at: datetime,
    **extra: Any,
) -> dict[str, Any]:
    identity = hashlib.sha256(f"{kind}|{token}|{'|'.join(signatures)}".encode()).hexdigest()
    return {
        "id": identity,
        "kind": kind,
        "title": title,
        "basis": "observation",
        "token_address": token,
        "wallet": wallet,
        "observed_at": at.isoformat(),
        "signature": signatures[0],
        "source_url": f"https://solscan.io/tx/{signatures[0]}",
        "evidence": [
            {
                "event_id": f"{signature}:{kind}",
                "signature": signature,
                "source_url": f"https://solscan.io/tx/{signature}",
                "receipt_url": f"https://solscan.io/tx/{signature}",
                "kind": kind,
            }
            for signature in signatures
        ],
        **extra,
    }


def creator_sells(
    rows: list[dict[str, Any]],
    balances: dict[str, Any],
    *,
    rpc: Rpc,
    at: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Creator balance drops since last cycle, and the balances to compare next time.

    A new coin's creator account is looked up under both token programs; after
    that only the one that exists is read.
    """

    watched: dict[str, dict[str, Any]] = {}
    for row in rows[:CREATOR_COINS]:
        mint, creator = row.get("token_address"), _creator(row)
        if not mint or not creator:
            continue
        known = balances.get(mint)
        if known and known.get("creator") == creator and known.get("account"):
            accounts = [known["account"]]
        else:
            accounts = [
                token_account(creator, mint, program)
                for program in (TOKEN_2022_PROGRAM, TOKEN_PROGRAM)
            ]
        watched[mint] = {"creator": creator, "accounts": accounts, "before": known or {}}
    found = read_accounts([a for item in watched.values() for a in item["accounts"]], rpc)
    findings, updated = [], {}
    for mint, item in watched.items():
        account = next((a for a in item["accounts"] if found.get(a)), None)
        amount = token_amount(found.get(account)) if account else None
        now = amount[0] if amount else 0.0
        updated[mint] = {"creator": item["creator"], "account": account, "amount": now}
        before = float(item["before"].get("amount") or 0.0)
        sold = before - now
        if before <= 0 or sold < before * MIN_SELL_SHARE or not item["before"].get("account"):
            continue
        signatures = _latest_signatures(item["before"]["account"], rpc, limit=1)
        if not signatures:
            continue
        share = sold / before * 100
        findings.append(
            _finding(
                "creator_sell",
                f"Creator sold or moved {share:.0f}% of their tokens "
                f"({sold / PUMP_SUPPLY * 100:.1f}% of supply)",
                mint,
                item["creator"],
                signatures,
                at,
                sold_share_pct=round(share, 2),
                supply_pct=round(sold / PUMP_SUPPLY * 100, 3),
            )
        )
    return findings, updated


def _latest_signatures(address: str, rpc: Rpc, *, limit: int) -> list[str]:
    payload = rpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSignaturesForAddress",
            "params": [address, {"limit": limit, "commitment": "finalized"}],
        },
        credits=1,
    )
    return [
        item["signature"]
        for item in payload.get("result") or []
        if isinstance(item, dict) and item.get("signature") and not item.get("err")
    ]


def launch_bundle(mint: str, *, rpc: Rpc, at: datetime) -> dict[str, Any] | None:
    """The launch-slot buys of one coin, or None when the launch looks clean.

    Raises LookupError when the launch is more than a few thousand
    transactions back, so the caller can stop checking that coin.
    """

    curve = bonding_curve(mint)
    before, oldest = None, []
    for _ in range(BUNDLE_SIGNATURE_PAGES):
        options: dict[str, Any] = {"limit": 1000, "commitment": "finalized"}
        if before:
            options["before"] = before
        payload = rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getSignaturesForAddress",
                "params": [curve, options],
            },
            credits=1,
        )
        page = [item for item in payload.get("result") or [] if isinstance(item, dict)]
        if not page:
            break
        oldest = page
        before = page[-1]["signature"]
        if len(page) < 1000:
            break
    else:
        raise LookupError("Launch is out of reach")
    if not oldest:
        return None
    launch_slot = min(int(item["slot"]) for item in oldest)
    in_slot = [
        item["signature"]
        for item in reversed(oldest)
        if int(item["slot"]) <= launch_slot + 1 and not item.get("err")
    ]
    if len(in_slot) < BUNDLE_MIN_TRANSACTIONS:
        return None
    entries = []
    for signature in in_slot[:BUNDLE_MAX_TRANSACTIONS]:
        try:
            payload = rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTransaction",
                    "params": [
                        signature,
                        {
                            "encoding": "jsonParsed",
                            "maxSupportedTransactionVersion": 0,
                            "commitment": "finalized",
                        },
                    ],
                },
                credits=1,
            )
        except ValueError:
            # One unreadable transaction should not sink the whole check.
            LOG.warning("Launch transaction unreadable: %s", signature)
            continue
        if isinstance(payload.get("result"), dict):
            entries.append(payload["result"])
    bought: dict[str, float] = {}
    signatures = []
    for entry in valid_transactions(entries, at=at):
        for event in parse_events(entry):
            if (
                event.get("kind") == "swap"
                and event.get("direction") == "buy"
                and event.get("token_address") == mint
            ):
                bought[event["wallet"]] = bought.get(event["wallet"], 0.0) + float(
                    event.get("net_token_amount") or 0
                )
                signatures.append(event["signature"])
    supply_pct = sum(bought.values()) / PUMP_SUPPLY * 100
    if len(bought) < BUNDLE_MIN_WALLETS or supply_pct < BUNDLE_MIN_SUPPLY_PCT:
        return None
    return _finding(
        "synchronized_buys",
        f"Launch bundle: {len(bought)} wallets took {supply_pct:.0f}% of supply "
        "in the launch block",
        mint,
        max(bought, key=bought.get),
        list(dict.fromkeys(signatures)),
        at,
        related_wallets=sorted(bought),
        supply_pct=round(supply_pct, 2),
        launch_slot=launch_slot,
    )


def launch_bundles(
    rows: list[dict[str, Any]],
    checked: dict[str, Any],
    *,
    rpc: Rpc,
    at: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Check up to ten unchecked tagged coins a cycle, SETUP first.

    Only coins tagged SETUP, RUNNING or EXTENDED are checked: a check costs up
    to 13 credits and most board coins are never tagged. A launch never
    changes, so each coin is checked once and remembered for a week. A failed
    read leaves the coin for a later cycle.
    """

    rank = {"setup": 0, "running": 1, "extended": 2}
    candidates = sorted(
        (
            row
            for row in rows
            if row.get("token_address")
            and (row.get("early") or {}).get("state") in rank
            and not (checked.get(row["token_address"]) or {}).get("checked_at")
        ),
        key=lambda row: rank[row["early"]["state"]],
    )
    findings = []
    remembered = {
        mint: entry
        for mint, entry in checked.items()
        if not entry.get("checked_at")
        or at - datetime.fromisoformat(entry["checked_at"]) <= timedelta(days=7)
    }
    for row in candidates[:BUNDLE_CHECKS_PER_CYCLE]:
        mint = row["token_address"]
        try:
            finding = launch_bundle(mint, rpc=rpc, at=at)
        except LookupError:
            finding = None
        except Exception:
            LOG.warning("Launch bundle check failed for %s", mint, exc_info=True)
            attempts = int((checked.get(mint) or {}).get("attempts") or 0) + 1
            if attempts < BUNDLE_MAX_ATTEMPTS:
                remembered[mint] = {"attempts": attempts}
                continue
            finding = None  # stop paying for a check that keeps failing
        remembered[mint] = {"checked_at": at.isoformat(), "finding": finding}
        if finding:
            findings.append(finding)
    return findings, remembered


def recent_findings(findings: list[dict[str, Any]], at: datetime) -> list[dict[str, Any]]:
    """Keep a day of findings, one per id."""

    kept = {}
    for finding in findings:
        try:
            seen = datetime.fromisoformat(finding["observed_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=UTC)
        if at - seen <= timedelta(hours=FINDING_HOURS):
            kept.setdefault(finding["id"], finding)
    return list(kept.values())


# Coins people were watching: an alert for anything else would be noise.
ALERT_STATES = {"setup", "running", "extended"}
ALERT_TITLES = {"creator_sell": "Creator selling", "synchronized_buys": "Launch bundle"}


def coin_id_for(mint: str) -> str:
    return "chain-" + hashlib.sha256(f"solana:{mint}".encode()).hexdigest()


def alert_text(finding: dict[str, Any], *, origin: str) -> str:
    """Markdown V2: the contract address leads, never a creator-set name."""

    from runner_web.telegram import escape_markdown_v2, markdown_link

    link = f"{origin.rstrip('/')}/memecoins/coin/{coin_id_for(finding['token_address'])}"
    heading = escape_markdown_v2(ALERT_TITLES.get(finding["kind"], "Risk found"))
    return (
        f"\U0001f534 *{heading}*\n`{finding['token_address']}`\n"
        f"{escape_markdown_v2(finding['title'])}\n\n" + markdown_link("Open the coin page", link)
    )


def dispatch_risk_alerts(*, origin: str, at: datetime | None = None, sender: Any = None) -> dict:
    """Post new findings on coins with an open Call.

    Everyone else hears a launch bundle as a lost ratification (transition alerts).

    One post per cycle through the channel's own pacing; a finding is posted
    once, and one that fails to send is tried again next cycle.
    """

    import json

    from runner_web.db import connection
    from runner_web.telegram import (
        TelegramDeliveryError,
        config_from_env,
        memecoin_alerts_enabled,
        send_post,
    )
    from runner_web.telegram_outbox import reserve_channel_slot

    if not memecoin_alerts_enabled():
        return {"status": "disabled", "sent": 0}
    config = config_from_env()
    if not config.configured:
        return {"status": "unconfigured", "sent": 0}
    current = at or datetime.now(UTC)
    with connection() as database:
        stored = {
            row["key"]: json.loads(row["value"])
            for row in database.execute(
                "SELECT key,value FROM worker_state WHERE key IN "
                "('memecoin_watch_findings','memecoin_risk_alerts_sent')"
            ).fetchall()
        }
        watched_calls = {
            row["coin_id"]
            for row in database.execute(
                "SELECT DISTINCT coin_id FROM memecoin_calls WHERE status='active'"
            ).fetchall()
        }
    sent_before = stored.get("memecoin_risk_alerts_sent") or {}
    due = [
        finding
        for finding in recent_findings(stored.get("memecoin_watch_findings") or [], current)
        if finding["id"] not in sent_before
        and coin_id_for(finding["token_address"]) in watched_calls
    ]
    if not due:
        return {"status": "idle", "sent": 0}
    finding = due[0]
    with connection() as database:
        if not reserve_channel_slot(
            database, config.chat_id, ["coin:" + coin_id_for(finding["token_address"])], current
        ):
            return {"status": "paced", "sent": 0}
    try:
        (sender or send_post)(config, alert_text(finding, origin=origin))
    except TelegramDeliveryError as exc:
        LOG.warning("Risk alert not delivered: %s", exc.status)
        return {"status": exc.status, "sent": 0}
    kept = {
        key: stamp
        for key, stamp in sent_before.items()
        if current - datetime.fromisoformat(stamp) <= timedelta(hours=FINDING_HOURS * 2)
    }
    kept[finding["id"]] = current.isoformat()
    with connection() as database:
        database.execute(
            "INSERT INTO worker_state(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            ("memecoin_risk_alerts_sent", json.dumps(kept), current.isoformat()),
        )
    return {"status": "sent", "sent": 1, "finding": finding["id"]}
