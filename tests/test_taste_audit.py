from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_shared_product_system_is_loaded_after_existing_styles() -> None:
    base = (ROOT / "web/templates/mobile_base.html").read_text()
    sports = (ROOT / "web/templates/sports.html").read_text()

    assert base.index("desktop-split.css") < base.index("product-system.css")
    assert '{% block product_head %}{% endblock %}' in base
    assert '{% include "_sports_styles.html" %}' in sports
    assert 'class="skip-link"' in base
    assert 'id="app-content"' in base
    assert 'class="tab-link product-tab-link"' in base
    assert 'data-desktop-workspace' in sports
    assert not (ROOT / "web/templates/sports_base.html").exists()
    assert 'class="product-switch"' not in base


def test_general_interface_keeps_its_editorial_edge() -> None:
    ticker = (ROOT / "web/templates/ticker.html").read_text()
    ticker_script = (ROOT / "web/static/ticker-detail.js").read_text()
    community = (ROOT / "web/templates/community.html").read_text()
    community += (ROOT / "web/templates/_alpha_ledger.html").read_text()
    navigation = (ROOT / "web/templates/mobile_base.html").read_text()

    assert "Movement first. Evidence before conviction." not in ticker
    assert "could send" not in ticker
    assert "RUG CHECK" in ticker
    assert "risk-evidence" in ticker
    assert "astrology for the tape" in ticker_script
    assert "Evidence review, not a trade alert." not in ticker
    assert "#1 most called" in community
    assert "🐺" in community
    assert '<span class="tab-icon alpha-icon" aria-hidden="true"></span>' in navigation


def test_sports_pulse_is_locked_to_the_shared_ticker_row() -> None:
    sports = (ROOT / "web/templates/sports.html").read_text()
    sports_live = (ROOT / "web/static/sports-live.js").read_text()
    ticker_row = (ROOT / "web/static/ticker-row.js").read_text()
    sports_ticker = (ROOT / "web/static/sports-ticker.css").read_text()
    sports_unified = (ROOT / "web/static/sports-unified.css").read_text()

    assert 'class="token-list" id="sportsPulseList"' in sports
    assert "TickerRow.renderShell" in sports_live
    assert "renderShell" in ticker_row
    assert "data-sports-pulse-row" in ticker_row
    assert "winner-card" not in sports + sports_live + sports_ticker + sports_unified
    assert "team-projection-card" not in sports + sports_live + sports_ticker + sports_unified
    assert "Sports Pulse rows intentionally use the shared ticker-row.css contract" in sports_ticker


def test_ai_report_carries_the_single_clear_disclaimer() -> None:
    report = (ROOT / "web/templates/research_report.html").read_text()
    all_templates = "\n".join(
        path.read_text() for path in (ROOT / "web/templates").glob("*.html")
    )

    assert "AI-generated opinion" in report
    assert "May be incomplete or wrong. Not financial advice." in report
    assert 'href="#report-sources"' in report
    assert 'id="report-sources"' in report
    assert report.index("AI-generated opinion") < report.index('id="report-sources"')
    assert all_templates.lower().count("not financial advice") == 1


def test_ai_report_does_not_repeat_its_opening_summary() -> None:
    report = (ROOT / "web/templates/research_report.html").read_text()

    assert 'class="research-summary"' not in report
    assert report.index('class="flash-forecast-receipt') < report.index(
        'class="research-thesis"'
    )
    assert "All {{ report.sources|length }} source links" in report
    assert "People in the filings" not in report
