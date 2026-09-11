from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from runner_web import db
from runner_web import helius_discovery as helius
from runner_web import memecoin_evidence as evidence
from runner_web.memecoin_chain_ingestion import ingest_stream
from runner_web.memecoin_chain_parser import (
    INITIALIZE,
    PUMP,
    PUMP_CREATE_V2,
    RAYDIUM,
    parse_events,
)

AT = datetime(2026, 9, 8, 17, tzinfo=UTC)
MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
CREATOR = "So11111111111111111111111111111111111111112"
POOL = helius.PUMP_SWAP


@pytest.fixture(autouse=True)
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "evidence.db")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    monkeypatch.setenv("HELIUS_DAILY_CREDITS", "10000")
    db.init_db()


def transaction():
    return {
        "slot": 100,
        "blockTime": int(AT.timestamp()) - 120,
        "meta": {"err": None, "innerInstructions": []},
        "transaction": {
            "signatures": ["1" * 64],
            "message": {
                "instructions": [
                    {
                        "programId": POOL,
                        "accounts": [POOL, POOL, CREATOR, MINT, CREATOR],
                        "data": helius._encode(
                            helius.CREATE_POOL + bytes(18) + helius._decode(CREATOR)
                        ),
                    }
                ]
            },
        },
    }


def test_budget_is_shared_atomic_and_resets_at_utc_day_boundary(monkeypatch):
    monkeypatch.setenv("HELIUS_DAILY_CREDITS", "30")

    def reserve(_):
        try:
            evidence.reserve_credits(10, at=AT)
            return True
        except evidence.CreditBudgetReached:
            return False

    with ThreadPoolExecutor(max_workers=4) as executor:
        assert sum(executor.map(reserve, range(8))) == 3
    assert evidence.budget_status(at=AT)["remaining_credits"] == 0
    evidence.reserve_credits(10, at=AT + timedelta(days=1))
    assert evidence.budget_status(at=AT + timedelta(days=1))["reserved_credits"] == 10


def test_budget_cannot_exceed_user_cap(monkeypatch):
    monkeypatch.setenv("HELIUS_DAILY_CREDITS", "1000000")
    evidence.reserve_credits(10000, at=AT)
    with pytest.raises(evidence.CreditBudgetReached):
        evidence.reserve_credits(1, at=AT)
    assert evidence.credit_limit() == 10000


def test_cursor_and_receipts_commit_together_and_replay_is_idempotent():
    tx = transaction()
    events = parse_events(tx)
    evidence.save_batch("program:test", [tx], events, {"cursor": "100:1"}, at=AT)
    evidence.save_batch("program:test", [tx], events, {"cursor": "100:1"}, at=AT)
    assert len(evidence.recent_events(at=AT)) == 1
    receipt = evidence.transaction_receipt("1" * 64)
    assert receipt["transaction"]["slot"] == 100
    assert len(receipt["sha256"]) == 64
    broken = {**events[0], "event_id": "broken"}
    broken.pop("signature")
    with pytest.raises(KeyError):
        evidence.save_batch("program:test", [], [broken], {"cursor": "200:1"}, at=AT)
    assert evidence.stream_state("program:test")["cursor"] == "100:1"


def test_ingestion_resumes_same_window_and_keeps_cursor_on_provider_failure():
    calls = []

    def first(body):
        calls.append(body)
        return {"result": {"data": [transaction()], "paginationToken": "100:1"}}

    ingest_stream("program:test", POOL, at=AT, rpc=first)
    first_filter = calls[0]["params"][1]["filters"]

    def failed(body):
        assert body["params"][1]["paginationToken"] == "100:1"
        assert body["params"][1]["filters"] == first_filter
        return {"error": {"message": "failed"}}

    with pytest.raises(ValueError):
        ingest_stream("program:test", POOL, at=AT + timedelta(minutes=5), rpc=failed)
    assert evidence.stream_state("program:test")["cursor"] == "100:1"
    ingest_stream(
        "program:test",
        POOL,
        at=AT + timedelta(minutes=10),
        rpc=lambda _: {"result": {"data": [], "paginationToken": None}},
    )
    state = evidence.stream_state("program:test")
    assert state["completed_until"] == int(AT.timestamp()) - 60
    assert state["window_end"] is None
    assert state["backlog_seconds"] == 660


def test_pump_launch_ignores_metadata_values():
    tx = transaction()
    ix = tx["transaction"]["message"]["instructions"][0]
    ix["programId"] = PUMP
    ix["accounts"] = [MINT, POOL, POOL, POOL, POOL, CREATOR]

    def data(name):
        raw = PUMP_CREATE_V2
        for value in (name, b"SYM", b"https://marketing.invalid"):
            raw += len(value).to_bytes(4, "little") + value
        return helius._encode(raw + helius._decode(CREATOR) + bytes(2))

    ix["data"] = data(b"promoted")
    original = parse_events(tx)
    ix["data"] = data(b"")
    assert parse_events(tx) == original
    assert original[0]["kind"] == "token_launch"
    assert original[0]["declared_creator"] == CREATOR


def test_raydium_pool_identity_comes_from_instruction_accounts():
    tx = transaction()
    ix = tx["transaction"]["message"]["instructions"][0]
    ix.update(
        programId=RAYDIUM,
        accounts=[CREATOR, POOL, POOL, POOL, CREATOR, MINT],
        data=helius._encode(INITIALIZE + bytes(24)),
    )
    row = parse_events(tx)[0]
    assert row["kind"] == "pool_created"
    assert row["pool_address"] == POOL
    assert row["wallet"] == CREATOR
    assert row["token_address"] == MINT


def test_receipt_endpoint_returns_hash_and_bounded_lookup(monkeypatch):
    from fastapi.testclient import TestClient

    from runner_web import main

    tx = transaction()
    evidence.save_batch("program:test", [tx], parse_events(tx), {}, at=AT)
    monkeypatch.setattr(main, "enforce_rate", lambda *args, **kwargs: None)
    client = TestClient(main.app)
    try:
        response = client.get("/api/memecoins/evidence/" + "1" * 64)
        assert response.status_code == 200
        assert response.json()["sha256"] == evidence.transaction_receipt("1" * 64)["sha256"]
        assert client.get("/api/memecoins/evidence/bad").status_code == 404
    finally:
        client.close()


def test_retention_removes_events_and_receipts_together():
    tx = transaction()
    evidence.save_batch("program:test", [tx], parse_events(tx), {}, at=AT)
    evidence.prune_evidence(at=AT + timedelta(days=31))
    assert evidence.transaction_receipt("1" * 64) is None
    assert evidence.recent_events(at=AT + timedelta(days=31)) == []


def test_busy_program_records_coverage_gap_before_returning_to_live_window():
    evidence.save_batch(
        "program:test",
        [],
        [],
        {
            "window_start": int(AT.timestamp()) - 7200,
            "window_end": int(AT.timestamp()) - 6900,
            "cursor": "old:1",
            "completed_until": int(AT.timestamp()) - 7200,
        },
        at=AT - timedelta(hours=2),
    )
    requests = []

    def rpc(body):
        requests.append(body)
        return {"result": {"data": [], "paginationToken": None}}

    ingest_stream("program:test", POOL, at=AT, rpc=rpc)
    options = requests[0]["params"][1]
    assert "paginationToken" not in options
    assert options["filters"]["blockTime"]["gte"] == int(AT.timestamp()) - 360
    assert evidence.evidence_status(at=AT)["recorded_coverage_gaps"] == 1
    assert (
        evidence.stream_state("program:test")["coverage_gaps"][0]["reason"]
        == "credit_bounded_backlog"
    )


def test_live_helius_transaction_fixture_decodes_the_observed_swap():
    import json
    from pathlib import Path

    from runner_web.memecoin_chain_parser import valid_transactions

    path = Path(__file__).parent / "fixtures" / "helius_pumpswap_transaction.json"
    entry = json.loads(path.read_text())
    at = datetime.fromtimestamp(entry["blockTime"], UTC) + timedelta(seconds=60)
    assert len(valid_transactions([entry], at=at)) == 1
    swaps = [event for event in parse_events(entry) if event["kind"] == "swap"]
    assert len(swaps) == 1
    assert swaps[0]["direction"] == "buy"
    assert swaps[0]["net_token_amount"] == "0.010227"
    assert swaps[0]["signature"] == entry["transaction"]["signatures"][0]
