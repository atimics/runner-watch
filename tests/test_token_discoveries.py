from __future__ import annotations

import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from ratitrust.memecoin import LABELS
from runner_web import db, main, memecoin_lookup
from runner_web import token_discoveries as discoveries
from runner_web.helius_discovery import _encode
from runner_web.privacy import delete_user_content, delete_user_data, export_user_data
from runner_web.token_rejections import canonical, checkpoint, leaf_hash, proof, verify

AT = datetime(2026, 10, 10, 12, tzinfo=UTC)
MINT = _encode(bytes([4]) * 32)
POOL = _encode(bytes([5]) * 32)


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "discoveries.db")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    db.init_db()
    with db.connection() as connection:
        for user_id in ("alice", "bob"):
            connection.execute(
                "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
                (user_id, user_id, user_id, "active", AT.isoformat()),
            )
    main.RATE_LIMITS.clear()


def reading(mint=MINT, *, state=True):
    checks = [
        {"key": key, "label": label, "met": True, "applies": True, "detail": ""}
        for key, label in LABELS.items()
    ]
    checks[0]["met"] = state
    result = {
        "ratified": state is True,
        "met": sum(c["met"] is True for c in checks),
        "total": len(checks),
        "standards": checks,
    }
    return {
        "token_address": mint,
        "ratification": result,
        "discovery": {
            "token_address": mint,
            "pool_address": POOL,
            "venue": "pool",
            "found_by": "address_search",
        },
    }


def settle(row=None, *, at=AT):
    row = row or reading()
    discoveries.settle([row], {row["token_address"]: {"supply": 1_000_000}}, at=at)


def test_first_submitter_is_paid_once_through_ratification_changes(database):
    assert discoveries.submit(MINT, "alice", at=AT)["reward_status"] == "reserved"
    assert discoveries.submit(MINT, "bob", at=AT)["reward_status"] == "claimed"
    settle()
    settle()
    settle(reading(state=False), at=AT + timedelta(hours=1))
    settle(at=AT + timedelta(hours=2))
    with db.connection() as connection:
        assert (
            connection.execute(
                "SELECT balance FROM flash_wallets WHERE user_id='alice'"
            ).fetchone()["balance"]
            == 50
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM flash_transactions WHERE kind='memecoin_discovery'"
            ).fetchone()[0]
            == 1
        )
    saved = discoveries.discovery(MINT, user_id="alice")
    assert saved["status"] == "tracked" and saved["reward_status"] == "paid"
    assert saved["proof_url"] is not None
    assert discoveries.tracked_receipts(at=AT)[0]["token_address"] == MINT


def test_missing_checks_stay_in_review_and_earlier_rejections_remain_saved(database):
    discoveries.submit(MINT, "alice", at=AT)
    settle(reading(state=None))
    assert discoveries.discovery(MINT)["status"] == "review"
    with db.connection() as connection:
        assert checkpoint(connection)["tree_size"] == 0
    settle(reading(state=False))
    settle(reading(state=False), at=AT + timedelta(minutes=1))
    saved = discoveries.discovery(MINT)
    assert saved["status"] == "rejected" and len(saved["failed_checks"]) == 1
    with db.connection() as connection:
        receipt = proof(connection, 0)
        assert checkpoint(connection)["tree_size"] == 1
        assert connection.execute("SELECT COUNT(*) FROM flash_transactions").fetchone()[0] == 0
    assert verify(receipt)
    assert "alice" not in canonical(receipt)


def test_a_partial_or_contradictory_rule_set_keeps_admission_pending(database):
    discoveries.submit(MINT, "alice", at=AT)
    row = reading()
    row["ratification"]["standards"].pop()
    settle(row)
    assert discoveries.discovery(MINT)["status"] == "review"
    row = reading()
    row["ratification"]["ratified"] = False
    settle(row)
    assert discoveries.discovery(MINT)["status"] == "review"


def test_reward_and_admission_roll_back_together(database, monkeypatch):
    discoveries.submit(MINT, "alice", at=AT)
    real_credit = discoveries.credit_flash

    def broken(*args, **kwargs):
        real_credit(*args, **kwargs)
        raise RuntimeError("interrupted settlement")

    monkeypatch.setattr(discoveries, "credit_flash", broken)
    with pytest.raises(RuntimeError):
        settle()
    assert discoveries.discovery(MINT, user_id="alice")["status"] == "review"
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM flash_transactions").fetchone()[0] == 0
    monkeypatch.setattr(discoveries, "credit_flash", real_credit)
    settle()
    assert discoveries.discovery(MINT, user_id="alice")["reward_status"] == "paid"


def test_concurrent_submitters_and_workers_share_one_reward(database):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda user: discoveries.submit(MINT, user, at=AT), ("alice", "bob"))
        )
    assert sorted(r["reward_status"] for r in results) == ["claimed", "reserved"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: settle(), range(2)))
    with db.connection() as connection:
        assert connection.execute("SELECT SUM(amount) FROM flash_transactions").fetchone()[0] == 50


def test_existing_assets_and_already_ratified_discoveries_keep_their_provenance(database):
    with db.connection() as connection:
        connection.execute(
            "INSERT INTO memecoin_assets(coin_id,quote_json,collected_at,run_id) VALUES(?,?,?,?)",
            (discoveries.coin_id(MINT), "{}", AT.isoformat(), "test"),
        )
    assert discoveries.submit(MINT, "alice", at=AT)["reward_status"] == "recorded"
    other = _encode(bytes([8]) * 32)
    discoveries.remember(other, at=AT)
    settle(reading(other))
    assert discoveries.submit(other, "bob", at=AT)["reward_status"] == "recorded"


@pytest.mark.parametrize("delete", [delete_user_content, delete_user_data])
def test_privacy_export_and_erasure_preserve_public_receipts_and_reward_claim(delete, database):
    discoveries.submit(MINT, "alice", at=AT)
    settle(reading(state=False))
    assert export_user_data("alice")["token_submissions"][0]["mint"] == MINT
    delete("alice")
    assert discoveries.submit(MINT, "bob", at=AT)["reward_status"] == "claimed"
    with db.connection() as connection:
        assert verify(proof(connection, 0))
        assert (
            connection.execute(
                "SELECT submitter_id FROM token_discoveries WHERE mint=?", (MINT,)
            ).fetchone()[0]
            is None
        )


def reference_root(leaves):
    if len(leaves) == 1:
        return leaf_hash(leaves[0])
    split = 1 << ((len(leaves) - 1).bit_length() - 1)
    return hashlib.sha256(
        b"\x01"
        + bytes.fromhex(reference_root(leaves[:split]))
        + bytes.fromhex(reference_root(leaves[split:]))
    ).hexdigest()


def test_merkle_proofs_match_an_independent_tree_and_keep_old_checkpoints(database):
    for i in range(1, 18):
        mint = _encode(bytes([i]) * 32)
        discoveries.remember(mint, at=AT)
        settle(reading(mint, state=False))
    with db.connection() as connection:
        for size in range(1, 18):
            leaves = [
                row[0]
                for row in connection.execute(
                    "SELECT leaf_json FROM token_rejections ORDER BY leaf_index LIMIT ?", (size,)
                ).fetchall()
            ]
            assert checkpoint(connection, size)["root_hash"] == reference_root(leaves)
            for index in range(size):
                receipt = proof(connection, index, size)
                assert verify(receipt)
                modified = copy.deepcopy(receipt)
                modified["evidence"]["facts"]["supply"] = 0
                assert verify(modified) is False
                modified = copy.deepcopy(receipt)
                modified["leaf_json"] += " "
                assert verify(modified) is False
                modified = copy.deepcopy(receipt)
                modified["inclusion_path"].append("00" * 32)
                assert verify(modified) is False


def test_admitted_coins_rotate_after_failed_quotes_and_keep_pending_checks_under_review(database):
    other = _encode(bytes([8]) * 32)
    for mint in (MINT, other):
        discoveries.remember(mint, at=AT)
        settle(reading(mint))
    first = discoveries.tracked_receipts(limit=1, at=AT + timedelta(minutes=1))[0]
    second = discoveries.tracked_receipts(limit=1, at=AT + timedelta(minutes=2))[0]
    assert first["token_address"] != second["token_address"]
    settle(reading(state=None), at=AT + timedelta(days=8))
    assert MINT in {
        row["token_address"] for row in discoveries.tracked_receipts(at=AT + timedelta(days=8))
    }


def test_submission_requires_a_session_origin_and_a_verified_mint(database, monkeypatch):
    client = TestClient(main.app, base_url=main.RUNNERS_ORIGIN)
    path = "/api/memecoin-discoveries/" + MINT
    monkeypatch.setattr(main, "enforce_rate", lambda *_args, **_kwargs: None)
    assert client.post(path).status_code == 403
    assert client.post(path, headers={"Origin": main.RUNNERS_ORIGIN}).status_code == 401
    monkeypatch.setattr(main, "require_user", lambda _: {"id": "alice"})
    monkeypatch.setattr(
        memecoin_lookup, "lookup_memecoin", lambda *_args, **_kwargs: {"status": "pending"}
    )
    # The route imports the lookup function at module scope.
    import runner_web.memecoin_routes as routes

    monkeypatch.setattr(routes, "lookup_memecoin", lambda *_args, **_kwargs: {"status": "pending"})
    assert client.post(path, headers={"Origin": main.RUNNERS_ORIGIN}).status_code == 409
    monkeypatch.setattr(routes, "lookup_memecoin", lambda *_args, **_kwargs: {"status": "ready"})
    response = client.post(
        path,
        headers={"Origin": main.RUNNERS_ORIGIN},
        json={"ratified": True, "reward_amount": 1000000},
    )
    assert response.status_code == 200 and response.json()["reward_amount"] == 50
    assert response.json()["status"] == "review"
    assert response.headers["cache-control"] == "private, no-store"


def test_proof_endpoints_serve_the_saved_receipt_and_historical_root(database, monkeypatch):
    discoveries.remember(MINT, at=AT)
    settle(reading(state=False))
    monkeypatch.setattr(main, "enforce_rate", lambda *_args, **_kwargs: None)
    client = TestClient(main.app)
    assert verify(client.get("/api/memecoin-rejections/0", params={"size": 1}).json())
    assert client.get("/api/memecoin-rejections/root", params={"size": 1}).json()["tree_size"] == 1
    assert client.get("/api/memecoin-rejections/1").status_code == 404
    assert client.get("/api/memecoin-rejections/root", params={"size": 2}).status_code == 404


def test_the_worker_settles_a_saved_submission_with_its_ratification_evidence(
    database, monkeypatch
):
    from runner_web import memecoins

    discoveries.submit(MINT, "alice", at=AT)
    row = reading()

    def ratify(rows, saved, *, facts, **_kwargs):
        facts[MINT] = {"source": "worker fixture"}
        return {}

    monkeypatch.setattr(memecoins, "ratify_rows", ratify)
    memecoins._ratify([row], {}, rpc=None, at=AT)
    assert discoveries.discovery(MINT, user_id="alice")["reward_status"] == "paid"
    with db.connection() as connection:
        assert json.loads(
            connection.execute(
                "SELECT facts_json FROM ratification_records WHERE subject=?", (MINT,)
            ).fetchone()[0]
        ) == {"source": "worker fixture"}


def test_a_rules_digest_change_gets_a_new_rejection_receipt(database, monkeypatch):
    discoveries.remember(MINT, at=AT)
    settle(reading(state=False))
    monkeypatch.setattr(discoveries, "rules_digest", lambda: "a" * 64)
    settle(reading(state=False), at=AT + timedelta(hours=1))
    with db.connection() as connection:
        assert checkpoint(connection)["tree_size"] == 2
        assert verify(proof(connection, 0, 1))
        assert json.loads(proof(connection, 1)["leaf_json"])["rules_digest"] == "a" * 64


def test_schema_setup_recovers_after_an_interrupted_migration(database):
    discoveries.submit(MINT, "alice", at=AT)
    with db.connection() as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version=102")
        connection.execute("DROP TABLE token_rejection_nodes")
    db.init_db()
    assert discoveries.discovery(MINT, user_id="alice")["reward_status"] == "reserved"
    settle(reading(state=False))
    with db.connection() as connection:
        assert verify(proof(connection, 0))


def test_initial_sigil_counts_only_the_mint_permissions_it_has_read():
    from runner_web.token_sigil import pending_detail

    controls = {
        "mint_authority": None,
        "freeze_authority": None,
        "risky_extensions": [],
        "supply": 1000,
    }
    result = pending_detail(MINT, {"controls": controls})["coin"]["ratification"]
    assert result["met"] == 3 and result["unchecked"] == 6
    assert next(check for check in result["standards"] if check["key"] == "record")["met"] is None
    assert result["ratified"] is False
