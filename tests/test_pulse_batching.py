from pathlib import Path


def test_pulse_exposures_are_batched() -> None:
    template = (
        Path(__file__).parents[1] / "web/templates/pulse.html"
    ).read_text()

    assert "exposureQueue" in template
    assert "body:JSON.stringify({entries})" in template
    assert "setTimeout(flushExposures,250)" in template
