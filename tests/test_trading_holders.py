from ratitrust.memecoin import top10_share_from


def test_split_token_accounts_are_combined_before_concentration_check():
    accounts = [{"address": str(i), "amount": "25", "decimals": 0} for i in range(20)]
    owners = {str(i): "same-wallet" for i in range(20)}
    share = top10_share_from(accounts, owners, {}, supply=1000, program_address=lambda _: False)
    assert share >= 50  # The old calculation returned 25% and passed the 30% gate.
