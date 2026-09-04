import re
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_shared_product_system_has_one_component_and_one_theme_file() -> None:
    base = (ROOT / "web/templates/mobile_base.html").read_text()
    sports = (ROOT / "web/templates/sports.html").read_text()

    styles = re.findall(r'href="/static/([^"?]+\.css)', base)
    assert styles == ["mobile.css", "product-system.css"]
    assert len(styles) == len(set(styles))
    assert all((ROOT / "web/static" / style).is_file() for style in styles)
    assert '{% block product_head %}{% endblock %}' in base
    assert '{% include "_sports_styles.html" %}' in sports
    sports_styles = re.findall(
        r'href="/static/([^"?]+\.css)',
        (ROOT / "web/templates/_sports_styles.html").read_text(),
    )
    assert sports_styles == ["sports.css"]
    assert all((ROOT / "web/static" / style).is_file() for style in sports_styles)
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

    assert "Movement first. Evidence before conviction." in ticker
    assert "RUG CHECK" in ticker
    assert "astrology for the tape" in ticker_script
    assert "Evidence review, not a trade alert." not in ticker
    assert "#1 most called" in community
    assert "🐺" in community
    assert '<span class="tab-icon alpha-icon" aria-hidden="true"></span>' in navigation


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
