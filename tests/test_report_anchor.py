"""Saved market report summaries keep the complete report available."""
from pathlib import Path

import pytest
from jinja2 import ChainableUndefined, Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parents[1]


def render_report(subject_type, **changes):
    report = {
        'subject_type': subject_type, 'ticker': 'TEST', 'company': 'Test asset',
        'public_id': 'saved-example', 'headline': 'Saved headline', 'summary': 'Saved summary',
        'thesis': 'Complete thesis', 'completed_at': '2026-09-19T12:30:00+00:00',
        'usage': {'context': {}}, 'actor': {}, 'evidence_snapshot': {},
        'catalysts': ['First support', 'Second support', 'Third', 'Fourth', 'Fifth'],
        'risks': ['Saved risk'], 'watch': ['Saved watch'], 'unknowns': ['Saved unknown'],
        'citations': [{'claim': 'Linked claim', 'source_urls': ['https://example.com/proof']}],
        'sources': [{'label': 'Original source', 'url': 'https://example.com/proof'}],
        'company_profile': {}, 'people': [], 'filing_context': [],
        'disclosures': [], 'corrections': [], 'risk_heading': 'Risks',
        'flash_version_id': 'v1', 'sports_forecast': None,
    }
    report.update(changes)
    env = Environment(loader=FileSystemLoader(ROOT / 'web/templates'),
                      autoescape=True, undefined=ChainableUndefined)
    return env.get_template('research_report.html').render(
        report=report, request={'state': {'csp_nonce': 'test'}, 'url': {'path': '/research/test'}},
        app_origin='https://example.com', runners_origin='https://example.com',
        sports_origin='https://sports.example.com', static_version='test',
    )


@pytest.mark.parametrize('subject_type', ['coin', 'sports_game'])
def test_complete_report_and_sources_survive_summary(subject_type):
    html = render_report(subject_type)
    for text in ['Complete thesis', 'Second support', 'Fifth', 'Saved unknown',
                 'Linked claim', 'https://example.com/proof']:
        assert text in html
    assert '<details class="report-expand"><summary>Full report' in html
    assert '<details id="report-sources"' in html
    assert html.count('id="report-sources"') == 1
    assert 'Saved report' in html


def test_coin_snapshot_preserves_zero_and_marks_saved_data():
    html = render_report('coin', evidence_snapshot={
        'price': 0.000003, 'change_24h': 0, 'liquidity_usd': 0, 'volume_24h': 12450,
        'observed_at': '2026-09-18T23:00:00Z', 'stale': True,
    })
    for text in ['+0.0%', '$0', '$12,450', '2026-09-18T23:00:00Z',
                 'Marked stale at capture', 'Market snapshot']:
        assert text in html
    assert 'Market snapshot' not in render_report('coin')


def test_sports_probability_uses_saved_forecast_and_pass_has_no_meter():
    forecast = {'selection': 'home', 'selected_abbreviation': 'ABC',
                'selected_probability': 0.63, 'brier_score': None}
    html = render_report('sports_game', sports_forecast=forecast)
    assert 'value="0.63"' in html
    assert '63% win probability' in html
    forecast.update(selection='pass', selected_probability=None)
    html = render_report('sports_game', sports_forecast=forecast)
    assert '<meter ' not in html
    assert 'Pass · no winner call' in html


def test_stock_report_keeps_existing_layout():
    html = render_report('ticker')
    assert 'report-anchor.css' not in html
    assert '<main class="research-report">' in html
    assert 'report-expand' not in html
    assert 'Saved report' not in html
    assert '<section id="report-sources"' in html


def test_coin_report_uses_readable_small_prices_and_compact_volume():
    html = render_report('coin', evidence_snapshot={
        'price': 0.000018, 'volume_24h': 12450000,
    })
    assert '$0.000018</dd>' in html
    assert '$12.45M' in html
    assert 'title="$12,450,000"' in html
