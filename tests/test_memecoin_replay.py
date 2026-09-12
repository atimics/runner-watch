from __future__ import annotations

import copy
import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from io import BytesIO
from threading import Event

import pytest
from PIL import Image

from runner_web import db
from runner_web import memecoin_evidence as evidence
from runner_web import memecoin_replay as replay
from runner_web import memecoin_replay_store as store
from runner_web.helius_discovery import _encode
from runner_web.memecoin_chain_parser import (
    BUY,
    CREATE,
    PUMP,
    PUMP_CREATE_V2,
    PUMP_SWAP,
    parse_events,
)
from runner_web.memecoin_replay_gif import render_gif
from runner_web.memecoin_replay_posts import dispatch_memecoin_replays
from runner_web.memecoin_store import save_memecoin_snapshot
from runner_web.telegram import AnimationDeliveryError

AT = datetime(2026, 9, 12, 18, tzinfo=UTC)
MINT, WALLET, POOL, OTHER = [_encode(bytes([i]) * 32) for i in (9, 10, 11, 12)]
COIN = {
    "id": "solana_" + MINT.lower(),
    "network": "solana",
    "token_address": MINT,
    "symbol": "TEST",
    "name": "Test token",
    "price": 1,
    "volume_24h": 10000,
    "market_cap": None,
    "change_24h": 2,
    "observed_at": AT.isoformat(),
    "source_url": "https://www.geckoterminal.com/solana/pools/" + POOL,
    "pool_address": POOL,
    "liquidity_usd": 10000,
    "buys_24h": 12,
    "sells_24h": 2,
    "source": "GeckoTerminal",
}


def transaction(kind: str, number: int = 0, *, wallet: str = WALLET, mint: str = MINT) -> dict:
    tx = {
        "slot": 100 + number,
        "blockTime": int(AT.timestamp()) - 120 + number,
        "transaction": {
            "signatures": [_encode(bytes([number % 250 + 1]) * 64)],
            "message": {"instructions": []},
        },
        "meta": {
            "err": None,
            "innerInstructions": [],
            "preTokenBalances": [],
            "postTokenBalances": [],
        },
    }
    if kind == "launch":
        ix = {
            "programId": PUMP,
            "accounts": [mint, POOL, POOL, POOL, POOL, wallet],
            "data": _encode(PUMP_CREATE_V2 + bytes(12) + bytes([10]) * 32 + bytes(2)),
        }
    elif kind == "pool":
        ix = {
            "programId": PUMP_SWAP,
            "accounts": [POOL, POOL, wallet, mint, OTHER],
            "data": _encode(CREATE + bytes(18) + bytes([10]) * 32),
        }
    else:
        ix = {
            "programId": PUMP_SWAP,
            "accounts": [POOL, wallet, POOL, mint],
            "data": _encode(BUY + bytes(16)),
        }
        tx["meta"]["postTokenBalances"] = [
            {
                "accountIndex": i,
                "owner": wallet,
                "mint": token,
                "uiTokenAmount": {"amount": raw, "decimals": decimals},
            }
            for i, (token, raw, decimals) in enumerate(
                ((mint, "9007199254740993", 6), (OTHER, "17", 9))
            )
        ]
    tx["transaction"]["message"]["instructions"] = [ix]
    return tx


def receipts(txs: list[dict]) -> list[dict]:
    return [
        {
            "signature": tx["transaction"]["signatures"][0],
            "transaction": tx,
            "sha256": replay.digest(tx),
            "collected_at": AT.isoformat(),
        }
        for tx in txs
    ]


def payload() -> dict:
    return replay.build_replay(
        COIN, receipts([transaction("launch"), transaction("pool", 1), transaction("buy", 2)]), {}
    )


@pytest.fixture(autouse=True)
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "replay.db")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    monkeypatch.setenv("TELEGRAM_MEMECOIN_ALERTS", "0")
    monkeypatch.setenv("TELEGRAM_API_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "test-channel")
    db.init_db()


def collect(*, at: datetime = AT, txs: list[dict] | None = None) -> None:
    txs = txs or [transaction("launch"), transaction("pool", 1), transaction("buy", 2)]
    evidence.save_batch("program:test", txs, [e for tx in txs for e in parse_events(tx)], {}, at=at)
    save_memecoin_snapshot([COIN], run_id="local-fixture", collected_at=at)


def test_replay_starts_at_launch_and_keeps_receipts_and_exact_balances():
    data = payload()
    assert replay.verify_replay(data)
    assert data["launch"]["slot"] == 100
    assert [n["id"] for n in data["frames"][0]["nodes"]] == ["launch"]
    assert data["frames"][0]["edges"] == []
    assert all(e["slot"] <= frame["slot"] for frame in data["frames"][1:] for e in frame["edges"])
    shared = next(e for e in data["events"] if e["kind"] == "shared_holdings")
    assert shared["balances"][0]["raw"] == "9007199254740993"
    assert shared["balances"][1]["decimals"] == 9
    assert data["receipt_root"] == replay.merkle_root(data["receipts"])
    assert replay.build_replay(COIN, list(reversed(data["receipts"])), {}) == data


def test_unobserved_launch_remains_pending_and_other_token_scope_is_bounded():
    data = replay.build_replay(COIN, receipts([transaction("buy", 2)]), {})
    assert data["launch"] is None
    assert data["frames"][0]["label"] == "Launch evidence pending"
    tx = transaction("buy", 3, wallet=_encode(bytes([30]) * 32), mint=OTHER)
    data = replay.build_replay(COIN, data["receipts"] + receipts([tx]), {})
    assert all(e["wallet"] == WALLET for e in data["events"])


@pytest.mark.parametrize("field", ["receipt", "event", "edge", "subject", "policy"])
def test_tampering_fails_even_after_rehashing_the_package(field):
    data = payload()
    if field == "receipt":
        data["receipts"][0]["transaction"]["slot"] = 1
    elif field == "event":
        data["events"][-1]["wallet"] = OTHER
    elif field == "edge":
        data["frames"][-1]["edges"][0]["role"] = "owns liquidity"
    elif field == "subject":
        data["token_address"] = OTHER
    else:
        data["policy"] = {**data["policy"], "owner": "someone else"}
    data["id"] = replay.digest({k: v for k, v in data.items() if k != "id"})
    assert not replay.verify_replay(data)


def test_failed_receipts_and_conflicting_copies_block_publication():
    tx = transaction("launch")
    tx["meta"]["err"] = {"InstructionError": [0, "failed"]}
    with pytest.raises(ValueError, match="decoder_replay"):
        replay.build_replay(COIN, receipts([tx]), {})
    items = receipts([transaction("launch")])
    with pytest.raises(ValueError, match="conflicting_receipt"):
        replay.build_replay(COIN, items + [{**items[0], "sha256": "0" * 64}], {})


def test_omitting_an_observed_event_cannot_pass_by_rehashing():
    data = payload()
    data["events"] = data["events"][1:]
    view = replay.project(data["events"], MINT)
    data.update(frames=view["frames"], launch=view["launch"])
    data["id"] = replay.digest({k: v for k, v in data.items() if k != "id"})
    assert not replay.verify_replay(data)


def test_renderer_failure_retries_saved_evidence_without_another_source_fetch():
    collect()

    def failed(_):
        raise OSError("render failed")

    assert store.render_pending_replays(at=AT, renderer=failed)["deferred"] == 1
    assert store.render_pending_replays(at=AT + timedelta(seconds=30))["ready"] == 0
    assert store.render_pending_replays(at=AT + timedelta(seconds=61))["ready"] == 1


def test_animation_endpoints_and_history_preserve_the_selected_evidence():
    data = payload()
    first, last = data["frames"][0], data["frames"][-1]
    original = copy.deepcopy(data)
    mid = replay.blend(first, last, 0.5)
    wallet = next(n for n in mid["nodes"] if n["id"].startswith("wallet:"))
    assert 0 < wallet["r"] < next(n["r"] for n in last["nodes"] if n["id"] == wallet["id"])
    assert replay.blend(last, first, 0.2)["edges"] == []
    assert len(replay.blend(last, first, 0.5, reset=True)["nodes"]) > 1
    assert all(
        n["r"] == 0
        for n in replay.blend(last, first, 1, reset=True)["nodes"]
        if n["id"] != "launch"
    )
    assert data == original


def test_event_node_and_keyframe_limits_are_recorded(monkeypatch):
    monkeypatch.setitem(replay.POLICY, "max_events", 12)
    monkeypatch.setitem(replay.POLICY, "max_nodes", 6)
    data = replay.build_replay(
        COIN,
        receipts(
            [transaction("launch")]
            + [transaction("buy", i, wallet=_encode(bytes([i + 20]) * 32)) for i in range(1, 20)]
        ),
        {"available_token_events": 20},
    )
    assert replay.verify_replay(data)
    assert len(data["events"]) <= 12
    assert len(data["frames"][-1]["nodes"]) <= 6
    assert data["coverage"]["drawn_events"] < data["coverage"]["saved_events"]
    assert data["launch"] is not None


def test_gif_is_animated_and_loops_with_readable_keyframe_holds():
    content = render_gif(payload())
    with Image.open(BytesIO(content)) as image:
        assert image.size == (800, 600)
        assert image.info["loop"] == 0
        assert image.n_frames > 40
        first = image.convert("RGB").tobytes()
        assert image.info["duration"] == 950
        images, durations = set(), []
        for i in range(image.n_frames):
            image.seek(i)
            images.add(hashlib.sha256(image.convert("RGB").tobytes()).hexdigest())
            durations.append(image.info["duration"])
        assert len(images) > 20
        assert max(durations) >= 1700
        assert sum(durations) == 6850
        assert first != image.convert("RGB").tobytes() or len(images) > 20
    assert len(content) < replay.POLICY["max_gif_bytes"]


def test_saved_replay_survives_source_retention_and_does_not_render_twice():
    collect()
    assert store.render_pending_replays(at=AT)["ready"] == 1
    first = store.saved_replay(COIN["id"], with_gif=True)
    assert first and first["gif"].startswith(b"GIF89a")
    assert (
        store.render_pending_replays(at=AT, renderer=lambda _: pytest.fail("duplicate render"))[
            "ready"
        ]
        == 0
    )
    evidence.prune_evidence(at=AT + timedelta(days=31))
    assert store.saved_replay(COIN["id"])["payload"] == first["payload"]


def test_bad_source_retains_receipt_and_records_a_quality_failure():
    collect()
    with db.connection() as database:
        database.execute("UPDATE memecoin_chain_transactions SET evidence_sha256=?", ("0" * 64,))
    assert store.render_pending_replays(at=AT)["deferred"] == 1
    assert store.saved_replay(COIN["id"]) is None
    with db.connection() as database:
        assert (
            database.execute("SELECT COUNT(*) FROM memecoin_chain_transactions").fetchone()[0] == 3
        )
        assert (
            database.execute("SELECT last_error FROM memecoin_replay_cases").fetchone()[0]
            == "receipt_hash"
        )


def test_migration_baselines_existing_assets_and_retains_them():
    collect()
    with db.connection() as database:
        db._migration_068_memecoin_replays(database)
        assert database.execute("SELECT COUNT(*) FROM memecoin_assets").fetchone()[0] == 1
        assert (
            database.execute("SELECT status FROM memecoin_replay_posts").fetchone()[0] == "baseline"
        )


def test_concurrent_render_claim_has_one_owner_and_recovers_after_lease():
    collect()
    with db.connection() as database:
        database.execute(
            "UPDATE memecoin_replay_cases SET lease_token='old',lease_until=?",
            ((AT + timedelta(minutes=1)).isoformat(),),
        )
    assert store.render_pending_replays(at=AT)["ready"] == 0
    entered, finish = Event(), Event()

    def renderer(data):
        entered.set()
        assert finish.wait(10)
        return render_gif(data)

    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(
            store.render_pending_replays, at=AT + timedelta(minutes=2), renderer=renderer
        )
        assert entered.wait(10)
        assert store.render_pending_replays(at=AT + timedelta(minutes=2))["ready"] == 0
        finish.set()
        assert future.result()["ready"] == 1


def test_new_detection_queues_one_frozen_gif_and_delivers_once(monkeypatch):
    monkeypatch.setenv("TELEGRAM_MEMECOIN_ALERTS", "1")
    collect()
    store.render_pending_replays(at=AT)
    sent = []

    def receiver(config, gif, caption):
        sent.append((config.chat_id, gif, caption))
        return 42

    for _ in range(2):
        dispatch_memecoin_replays(origin="https://app.test", at=AT, sender=receiver)
    assert len(sent) == 1
    assert sent[0][0] == "test-channel"
    assert sent[0][1] == store.saved_replay(COIN["id"], with_gif=True)["gif"]
    assert MINT in sent[0][2] and "?replay=" in sent[0][2]
    collect(at=AT + timedelta(minutes=5))
    store.render_pending_replays(at=AT + timedelta(minutes=5))
    assert dispatch_memecoin_replays(origin="https://app.test", at=AT, sender=receiver)["sent"] == 0


def test_channel_opt_in_and_destination_are_bound_to_the_new_detection(monkeypatch):
    collect()
    store.render_pending_replays(at=AT)
    assert dispatch_memecoin_replays(origin="https://app.test")["status"] == "disabled"
    monkeypatch.setenv("TELEGRAM_MEMECOIN_ALERTS", "1")
    assert (
        dispatch_memecoin_replays(
            origin="https://app.test", at=AT, sender=lambda *_: pytest.fail("baseline sent")
        )["sent"]
        == 0
    )


@pytest.mark.parametrize(
    "outcome,expected", [("retry", "retry"), ("uncertain", "uncertain"), ("failed", "failed")]
)
def test_delivery_records_retry_and_uncertain_outcomes(monkeypatch, outcome, expected):
    monkeypatch.setenv("TELEGRAM_MEMECOIN_ALERTS", "1")
    collect()
    store.render_pending_replays(at=AT)

    def receiver(*_):
        raise AnimationDeliveryError(outcome, retry_after=60)

    dispatch_memecoin_replays(origin="https://app.test", at=AT, sender=receiver)
    with db.connection() as database:
        row = database.execute("SELECT status,attempts FROM memecoin_replay_posts").fetchone()
    assert row["status"] == expected and row["attempts"] == 1
    assert (
        dispatch_memecoin_replays(
            origin="https://app.test",
            at=AT + timedelta(seconds=30),
            sender=lambda *_: pytest.fail("premature retry"),
        )["sent"]
        == 0
    )
    if outcome == "retry":
        assert (
            dispatch_memecoin_replays(
                origin="https://app.test", at=AT + timedelta(seconds=61), sender=lambda *_: 42
            )["sent"]
            == 1
        )


def test_details_routes_return_the_same_saved_gif_and_package(monkeypatch):
    from fastapi.testclient import TestClient

    from runner_web import main

    collect()
    store.render_pending_replays(at=AT)
    monkeypatch.setattr(main, "enforce_rate", lambda *args, **kwargs: None)
    client = TestClient(main.app)
    try:
        response = client.get(f"/api/memecoins/{COIN['id']}/replay")
        assert response.status_code == 200
        record = response.json()
        assert "receipts" not in record["payload"]
        gif = client.get(record["gif_url"])
        assert gif.headers["content-type"] == "image/gif"
        assert gif.content.startswith(b"GIF89a")
        package = client.get(record["evidence_url"])
        assert replay.verify_replay(package.json())
        evidence.prune_evidence(at=AT + timedelta(days=31))
        signature = package.json()["receipts"][0]["signature"]
        retained = client.get(record["receipt_base"] + signature)
        assert retained.status_code == 200
        assert retained.json()["sha256"] == package.json()["receipts"][0]["sha256"]
        pinned = client.get(f"/api/memecoins/{COIN['id']}/replay?revision={record['id']}")
        assert pinned.json()["id"] == record["id"]
        assert (
            client.get(f"/api/memecoins/{COIN['id']}/replay?revision={'0' * 64}").status_code == 404
        )
        page = client.get(f"/memecoins/coin/{COIN['id']}")
        assert 'id="token-replay"' in page.text
        assert "/static/memecoin-replay.js" in page.text
    finally:
        client.close()
