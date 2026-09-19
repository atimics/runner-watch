"""Market screens use saved evidence for the selected token."""

from datetime import UTC, datetime

from runner_web import memecoins
from runner_web.market_screens import detail, listing


def test_saved_chain_findings_reach_list_and_detail_for_their_token(monkeypatch):
    at = datetime(2026, 9, 19, 12, tzinfo=UTC)
    coin = {
        'id': 'example', 'symbol': 'EX', 'name': 'Example',
        'token_address': 'selected-token', 'price': 0.01,
        'volume_24h': 1500, 'market_cap': 10000, 'change_24h': 2,
        'observed_at': at.isoformat(), 'source_url': 'https://example.com/token',
    }
    findings = [
        {'token_address': 'other-token', 'title': 'Other token finding'},
        {'token_address': 'selected-token', 'kind': 'liquidity_withdrawal',
         'title': 'Wallet withdrew pool liquidity', 'basis': 'observation',
         'source_url': 'https://example.com/receipt', 'observed_at': at.isoformat()},
    ]
    monkeypatch.setattr(memecoins, '_market_states', lambda **_: {
        'memecoins_snapshot': {'rows': [coin], 'collected_at': at.isoformat()},
        'memecoin_forensics': {'findings': findings},
    })
    monkeypatch.setattr(memecoins, 'memecoins_enabled', lambda: True)
    monkeypatch.setattr(memecoins, 'stored_memecoin', lambda _: None)
    monkeypatch.setattr(memecoins, 'memecoin_history', lambda *a, **kw: [])
    board = listing('memecoins', memecoins.memecoin_market(at=at)['rows'])
    subject = detail('memecoins', memecoins.memecoin_detail('example', at=at))
    for item in (board['rows'][0], subject['item']):
        assert item['score'] is None
        assert [driver['label'] for driver in item['assessment']['drivers']] == [
            'Wallet withdrew pool liquidity'
        ]
        assert item['assessment']['drivers'][0]['source_url'] == 'https://example.com/receipt'


def test_sports_filter_counts_match_the_saved_signal_labels():
    rows = [
        {'id': signal, 'start_time': '2026-09-20T12:00:00Z',
         'prediction': {'signal': signal, 'selection': 'pass'}}
        for signal in ('lean', 'pass', 'watch', 'model only')
    ]
    screen = listing('sports', rows)
    assert screen['counts'] == {'lean': 1, 'pass': 1, 'watch': 1, 'model-only': 1}


def test_paused_quote_keeps_saved_risk_state_visible():
    item = listing('memecoins', [{
        'id': 'example', 'symbol': 'EX', 'stale': True, 'rug_level': 'high',
    }])['rows'][0]
    assert item['tag'] == 'AVOID'
    assert item['assessment']['tag'] == 'AVOID'
    assert item['change'] == 'Price paused'


def test_token_subtitle_uses_its_contract_address():
    address = 'DezXAZ8z7PnrnRJjz3wXBoRgixCaDqdGX2FNBFpPB263'
    item = listing('memecoins', [{
        'id': 'bonk', 'symbol': 'BONK', 'name': 'Saved assessment example',
        'token_address': address,
    }])['rows'][0]
    assert item['subtitle'] == 'CA DezXAZ…B263'
    assert item['contract_address'] == address
