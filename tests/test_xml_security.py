import io

import pytest

from runner_watch.xml_security import PayloadTooLargeError, read_limited, safe_xml_fromstring


def test_safe_xml_rejects_entity_definitions() -> None:
    payload = '<!DOCTYPE root [<!ENTITY example "expanded">]><root>&example;</root>'

    with pytest.raises(ValueError):
        safe_xml_fromstring(payload)


def test_safe_xml_rejects_oversized_documents() -> None:
    with pytest.raises(PayloadTooLargeError):
        safe_xml_fromstring("<root>too large</root>", max_bytes=8)


def test_limited_response_reader_stops_oversized_downloads() -> None:
    with pytest.raises(PayloadTooLargeError):
        read_limited(io.BytesIO(b"12345"), max_bytes=4)

    assert read_limited(io.BytesIO(b"1234"), max_bytes=4) == b"1234"
