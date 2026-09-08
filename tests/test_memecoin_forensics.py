from datetime import UTC, datetime, timedelta

from runner_web.memecoin_forensics import analyze_events

AT = datetime(2026, 9, 8, 17, tzinfo=UTC)


def event(kind, identity, *, second=0, **fields):
    return {
        "event_id": identity,
        "signature": identity,
        "kind": kind,
        "observed_at": (AT + timedelta(seconds=second)).isoformat(),
        "source_url": "https://solscan.io/tx/" + identity,
        **fields,
    }


def swap(identity, wallet, direction="buy", amount="10", second=0):
    return event(
        "swap",
        identity,
        second=second,
        wallet=wallet,
        token_address="mint",
        direction=direction,
        net_token_amount=amount,
    )


def test_launch_linked_trade_keeps_observation_and_relationship_receipts():
    launch = event("token_launch", "launch", second=-100, wallet="creator", token_address="mint")
    report = analyze_events([launch, swap("sell", "creator", "sell")])
    row = report["findings"][0]
    assert row["kind"] == "creator_sell"
    assert {proof["signature"] for proof in row["evidence"]} == {"launch", "sell"}
    assert row["basis"] == "observation"
    assert report["token_metrics"]["mint"]["observed_sellers"] == 1


def test_shared_funder_is_a_relationship_with_both_funding_receipts():
    events = [swap("one", "a"), swap("two", "b")]
    events += [
        event(
            "sol_transfer",
            "fund-" + wallet,
            second=-10,
            source_wallet="root",
            wallet=wallet,
            lamports="1000",
        )
        for wallet in ("a", "b")
    ]
    report = analyze_events(events)
    row = next(row for row in report["findings"] if row["kind"] == "common_funder")
    assert row["basis"] == "relationship"
    assert row["related_wallets"] == ["a", "b"]
    assert len(row["evidence"]) == 4
    assert len(report["wallet_links"]) == 2
    assert "Shared services" in row["explanation"]


def test_later_funding_and_single_buyer_do_not_imply_shared_funding():
    events = [swap("one", "a"), swap("two", "b")]
    events += [
        event(
            "sol_transfer",
            "fund-" + wallet,
            second=10,
            source_wallet="root",
            wallet=wallet,
            lamports="1000",
        )
        for wallet in ("a", "b")
    ]
    assert analyze_events(events)["findings"] == []


def test_synchronized_buys_need_distinct_wallets_and_similar_sizes():
    rows = [swap(str(i), str(i), second=i) for i in range(3)]
    report = analyze_events(rows)
    assert report["findings"][0]["kind"] == "synchronized_buys"
    assert report["findings"][0]["basis"] == "pattern"
    assert analyze_events([swap(str(i), "same", second=i) for i in range(3)])["findings"] == []
    assert (
        analyze_events([swap(str(i), str(i), amount=str(i + 1), second=i) for i in range(3)])[
            "findings"
        ]
        == []
    )


def test_round_trips_require_alternating_trades_and_balanced_amounts():
    rows = [
        swap(str(i), "wallet", "buy" if i % 2 == 0 else "sell", second=i * 20) for i in range(4)
    ]
    assert analyze_events(rows)["findings"][0]["kind"] == "repeated_round_trips"
    rows[-1]["net_token_amount"] = "1"
    assert analyze_events(rows)["findings"] == []


def test_liquidity_and_sol_moves_are_facts_with_unknown_recipient_identity():
    rows = [
        event("pool_created", "pool", second=-10, wallet="creator", token_address="mint"),
        event(
            "liquidity_withdrawal",
            "withdraw",
            wallet="creator",
            token_address="mint",
            net_token_amount="100",
        ),
        event(
            "sol_transfer",
            "exit",
            wallet="recipient",
            source_wallet="creator",
            lamports="1000000000",
        ),
    ]
    report = analyze_events(rows)
    assert {row["kind"] for row in report["findings"]} == {
        "liquidity_withdrawal",
        "creator_sol_transfer",
    }
    assert all(row["basis"] == "observation" for row in report["findings"])
    exit_row = next(row for row in report["findings"] if row["kind"] == "creator_sol_transfer")
    assert exit_row["recipient"] == "recipient"
    assert "unknown" in exit_row["explanation"]


def test_instruction_duplicates_do_not_inflate_wallet_metrics():
    row = swap("one", "wallet")
    report = analyze_events([row, {**row, "event_id": "another-instruction"}])
    assert report["token_metrics"]["mint"]["observed_swaps"] == 1
