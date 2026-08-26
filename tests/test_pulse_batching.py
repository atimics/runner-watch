from pathlib import Path


def test_pulse_does_not_collect_exposures() -> None:
    template = (
        Path(__file__).parents[1] / "web/templates/pulse.html"
    ).read_text()

    assert "exposureQueue" not in template
    assert "body:JSON.stringify({entries})" not in template
    assert "setTimeout(flushExposures,250)" not in template
