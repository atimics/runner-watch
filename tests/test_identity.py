from __future__ import annotations

from pathlib import Path

import pytest
from pytest import MonkeyPatch

from runner_web import db as db_module
from runner_web.db import connection, init_db
from runner_web.identity import (
    attach_reference,
    ensure_entity,
    entity_group,
    identity_reconciliation_queue,
    link_market_actors,
    public_associations,
    record_claim,
    review_claim,
)
from runner_web.market_actors import _ensure_actor


@pytest.fixture
def identity_db(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(db_module, "DATABASE_PATH", tmp_path / "identity.db")
    init_db()


def test_entities_are_stable_and_idempotent_across_ingestion(identity_db) -> None:
    first = ensure_entity("person", dedupe_key="filing:cik-1:ada lovelace", created_at="a")
    second = ensure_entity("person", dedupe_key="filing:cik-1:ada lovelace", created_at="b")

    assert first["id"] == second["id"]
    with connection() as database:
        rows = database.execute("SELECT * FROM participant_entities").fetchall()
    assert len(rows) == 1


def test_two_people_with_the_same_name_stay_separate(identity_db) -> None:
    one = ensure_entity("person", dedupe_key="filing:cik-1:jordan smith", created_at="a")
    two = ensure_entity("person", dedupe_key="filing:cik-2:jordan smith", created_at="a")
    attach_reference(one["id"], "name", "Jordan Smith", learned_at="a")
    attach_reference(two["id"], "name", "Jordan Smith", learned_at="a")

    assert one["id"] != two["id"]
    queue = identity_reconciliation_queue()
    collisions = {
        collision["name"]: collision["entity_ids"] for collision in queue["name_collisions"]
    }
    assert collisions["jordan smith"] == [one["id"], two["id"]]


def test_one_person_under_two_names_groups_after_an_accepted_claim(identity_db) -> None:
    filing_identity = ensure_entity(
        "person", dedupe_key="filing:cik-1:ada lovelace", created_at="a"
    )
    chain_identity = ensure_entity("person", dedupe_key="chain:wallet-abc", created_at="a")
    attach_reference(filing_identity["id"], "name", "Ada Lovelace", learned_at="a")
    attach_reference(chain_identity["id"], "name", "Countess Ada", learned_at="a")
    assert entity_group(filing_identity["id"])["entity_ids"] == [filing_identity["id"]]

    claim = record_claim(
        filing_identity["id"],
        "same_participant",
        object_entity_id=chain_identity["id"],
        state="proposed",
        learned_at="b",
    )
    review_claim(claim, "accepted", note="SEC identifier matches the wallet declaration.")
    grouped = entity_group(filing_identity["id"])

    assert sorted(grouped["entity_ids"]) == sorted([filing_identity["id"], chain_identity["id"]])
    assert set(grouped["entity_ids"]) == set(entity_group(chain_identity["id"])["entity_ids"])


def test_retraction_rebuilds_the_group(identity_db) -> None:
    one = ensure_entity("person", dedupe_key="filing:cik-1:ada", created_at="a")
    two = ensure_entity("person", dedupe_key="chain:wallet-abc", created_at="a")
    claim = record_claim(
        one["id"], "same_participant", object_entity_id=two["id"], state="proposed", learned_at="a"
    )
    review_claim(claim, "accepted")
    assert sorted(entity_group(one["id"])["entity_ids"]) == sorted([one["id"], two["id"]])

    review_claim(claim, "retracted", note="The identifier belonged to a different filer.")
    assert entity_group(one["id"])["entity_ids"] == [one["id"]]
    assert entity_group(two["id"])["entity_ids"] == [two["id"]]


def test_alias_acceptance_requires_compatible_kinds(identity_db) -> None:
    person = ensure_entity("person", dedupe_key="filing:cik-1:ada", created_at="a")
    company = ensure_entity("organization", dedupe_key="org:acme", created_at="a")
    claim = record_claim(
        person["id"],
        "same_participant",
        object_entity_id=str(company["id"]),
        learned_at="a",
    )
    review_claim(claim, "accepted")

    assert entity_group(person["id"])["entity_ids"] == [person["id"]]


def test_growing_wallet_group_keeps_one_entity(identity_db) -> None:
    group = ensure_entity("provisional_group", dedupe_key="cluster:base", created_at="a")
    attach_reference(group["id"], "wallet", "wallet-a", chain="solana", learned_at="a")
    attach_reference(group["id"], "wallet", "wallet-b", chain="solana", learned_at="a")
    attach_reference(group["id"], "wallet", "wallet-c", chain="solana", learned_at="b")
    attach_reference(group["id"], "wallet", "wallet-d", chain="solana", learned_at="c")

    merged = entity_group(str(group["id"]))
    wallets = [item["value"] for item in merged["references"] if item["kind"] == "wallet"]
    assert wallets == ["wallet-a", "wallet-b", "wallet-c", "wallet-d"]
    assert merged["kind"] == "provisional_group"


def test_same_address_text_on_two_networks_is_two_references(identity_db) -> None:
    group = ensure_entity("provisional_group", dedupe_key="cluster:split", created_at="a")
    attach_reference(group["id"], "wallet", "addr-1", chain="solana", learned_at="a")
    other = ensure_entity("provisional_group", dedupe_key="cluster:other", created_at="a")
    attach_reference(other["id"], "wallet", "addr-1", chain="ethereum", learned_at="a")

    with connection() as database:
        rows = database.execute(
            "SELECT chain FROM participant_references WHERE value='addr-1' ORDER BY chain"
        ).fetchall()
    assert [str(row["chain"]) for row in rows] == ["ethereum", "solana"]


def test_claims_carry_two_clocks_policy_and_relationships(identity_db) -> None:
    person = ensure_entity("person", dedupe_key="filing:cik-1:ada", created_at="a")
    issuer = ensure_entity("organization", dedupe_key="org:acme", created_at="a")
    fund = ensure_entity("organization", dedupe_key="org:fenwick", created_at="a")

    ownership = record_claim(
        str(person["id"]),
        "ownership",
        object_entity_id=str(issuer["id"]),
        valid_from=None,
        valid_until="2026-08-01T00:00:00+00:00",
        learned_at="2026-09-01T00:00:00+00:00",
        evidence_kind="sec_filing",
        evidence_id="000-1",
        origin="filing",
        state="proposed",
    )
    record_claim(
        str(issuer["id"]),
        "custody",
        object_entity_id=str(fund["id"]),
        learned_at="2026-09-02T00:00:00+00:00",
        state="proposed",
    )
    review_claim(ownership, "accepted", note="13D names the stake.")

    with connection() as database:
        rows = {
            str(row["relationship"]): dict(row)
            for row in database.execute("SELECT * FROM participant_claims").fetchall()
        }
    assert rows["ownership"]["policy_version"] == "identity-claims-v1"
    assert rows["ownership"]["valid_from"] is None
    assert rows["ownership"]["valid_until"] == "2026-08-01T00:00:00+00:00"
    assert rows["ownership"]["learned_at"] == "2026-09-01T00:00:00+00:00"
    assert rows["ownership"]["state"] == "accepted"

    associations = public_associations(str(person["id"]))
    labels = {association["label"] for association in associations}
    assert "Owned by" in labels
    assert all("pe-" not in association["target_kind"] for association in associations)


def test_missing_identifiers_stay_unresolved_until_research_decides(identity_db) -> None:
    one = ensure_entity("person", dedupe_key="filing:cik-1:ada", created_at="a")
    two = ensure_entity("person", dedupe_key="chain:wallet-xyz", created_at="a")
    claim = record_claim(
        str(one["id"]), "same_participant", object_entity_id=str(two["id"]), learned_at="a"
    )
    review_claim(claim, "disputed", note="Conflicting filings.")
    group = entity_group(str(one["id"]))

    # The disputed association stays visible with its uncertainty; the group
    # itself does not form.
    assert group["entity_ids"] == [str(one["id"])]
    assert group["associations"] == [
        {
            "relationship": "same_participant",
            "label": "Same participant",
            "state": "disputed",
            "target_kind": "person",
            "current": True,
        }
    ]


def test_market_actor_migration_preserves_handles_and_deep_links(identity_db) -> None:
    with connection() as database:
        actor = _ensure_actor(
            database,
            kind="person",
            domain="stock",
            stable_key="insider:jane-roe",
        )
        refreshed = database.execute(
            "SELECT * FROM market_actors WHERE id=?", (actor["id"],)
        ).fetchone()
    assert refreshed["entity_id"] is not None
    assert refreshed["display_name"] == actor["display_name"]
    assert refreshed["user_id"] == actor["user_id"]

    merged = entity_group(str(refreshed["entity_id"]))
    assert merged["kind"] == "person"
    assert any(item["kind"] == "legacy_actor" for item in merged["references"])


def test_coin_cluster_migration_carries_wallet_references(identity_db) -> None:
    with connection() as database:
        actor = _ensure_actor(
            database,
            kind="cluster",
            domain="coin",
            stable_key="cluster:base",
        )
        database.execute(
            """
            INSERT INTO actor_cluster_members(actor_id,wallet,evidence_kind,evidence_id,created_at)
            VALUES(?,?,?,?,?)
            """,
            (actor["id"], "wallet-a", "chain_event", "sig-1", "2026-09-12T00:00:00+00:00"),
        )
        link_market_actors(database)
        refreshed = database.execute(
            "SELECT entity_id FROM market_actors WHERE id=?", (actor["id"],)
        ).fetchone()
        entity_id = str(refreshed["entity_id"])
    merged = entity_group(entity_id)
    wallets = [item["value"] for item in merged["references"] if item["kind"] == "wallet"]
    assert wallets == ["wallet-a"]
    assert merged["kind"] == "provisional_group"
