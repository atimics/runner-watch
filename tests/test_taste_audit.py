from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_shared_product_system_is_loaded_after_existing_styles() -> None:
    base = (ROOT / "web/templates/mobile_base.html").read_text()
    sports = (ROOT / "web/templates/sports_base.html").read_text()

    assert base.index("desktop-split.css") < base.index("product-system.css")
    assert sports.index("sports-dashboard.css") < sports.index("product-system.css")
    assert 'class="skip-link"' in base
    assert 'class="skip-link"' in sports
    assert 'id="app-content"' in base
    assert 'class="tab-link product-tab-link"' in base
    assert 'class="sports-tab-icon product"' in sports
    assert 'class="product-switch"' not in base


def test_general_interface_keeps_its_editorial_edge() -> None:
    ticker = (ROOT / "web/templates/ticker.html").read_text()
    ticker_script = (ROOT / "web/static/ticker-detail.js").read_text()
    community = (ROOT / "web/templates/community.html").read_text()
    community += (ROOT / "web/templates/_alpha_ledger.html").read_text()
    navigation = (ROOT / "web/templates/mobile_base.html").read_text()

    assert "Movement first. Evidence before conviction." in ticker
    assert "RUG CHECK" in ticker
    assert "astrology for the tape" in ticker_script
    assert "Evidence review, not a trade alert." not in ticker
    assert "#1 most called" in community
    assert "🐺" in community
    assert "🐺" in navigation


def test_ai_report_carries_the_single_clear_disclaimer() -> None:
    report = (ROOT / "web/templates/research_report.html").read_text()
    all_templates = "\n".join(
        path.read_text() for path in (ROOT / "web/templates").glob("*.html")
    )

    assert "AI-generated opinion" in report
    assert "They may be incomplete or inaccurate and are not financial advice." in report
    assert 'href="#report-sources"' in report
    assert 'id="report-sources"' in report
    assert report.index("AI-generated opinion") < report.index('id="report-sources"')
    assert all_templates.count("not financial advice") == 1
