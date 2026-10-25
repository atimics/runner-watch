from runner_web.memecoin_ratify import top10_share
from tests.test_memecoin_ratify import MINT, WALLET, FakeRpc, owned_by


def test_split_token_accounts_are_combined_before_concentration_check():
    accounts = [{"address": str(i), "amount": "25", "decimals": 0} for i in range(20)]
    rpc = FakeRpc(mints={str(i): owned_by(WALLET) for i in range(20)}, largest=accounts)
    share = top10_share(MINT, supply=1000, exclude=set(), rpc=rpc)
    assert share == 50  # The old calculation returned 25% and passed the 30% gate.
