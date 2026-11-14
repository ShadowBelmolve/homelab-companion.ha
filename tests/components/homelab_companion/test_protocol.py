"""protocol.py alone: no Home Assistant."""

import json

import pytest

from custom_components.homelab_companion import protocol


def test_a_message_is_one_compact_utf8_line() -> None:
    line = protocol.encode(protocol.notify("deal", True, 60_000, "Atenção", "a\nb"))
    assert line == '{"t":"notify","kind":"deal","urgent":true,"ms":60000,"title":"Atenção","body":"a\\nb"}\n'.encode()
    assert line.count(b"\n") == 1


def test_a_line_the_companion_would_drop_is_refused() -> None:
    fits = {"t": "x", "pad": "a" * (protocol.LINE_MAX - len('{"t":"x","pad":""}'))}
    assert len(protocol.encode(fits)) == protocol.LINE_MAX + 1, "the line feed is not counted"
    with pytest.raises(ValueError, match="1024"):
        protocol.encode({**fits, "pad": fits["pad"] + "a"})
    with pytest.raises(ValueError):
        protocol.encode({"t": "x", "pad": "\ud800"})


def test_notify_cuts_the_texts_where_the_companion_does() -> None:
    message = protocol.notify("deal", False, 1, "t" * 300, "b" * 900)
    assert (len(message["title"]), len(message["body"])) == (protocol.TITLE_MAX, protocol.BODY_MAX)
    assert protocol.notify("email", False, 1, "3")["body"] == ""
    padded = protocol.notify("deal", False, 1, "  " + "t" * 85 + " ", "\nb\n")
    assert (padded["title"], padded["body"]) == ("t" * 80, "b"), "trimmed first, as the Companion trims"


def test_decode_takes_any_object_with_a_t() -> None:
    state = {"t": "state", "rssi": -61, "new": {"field": [1]}}
    assert protocol.decode(json.dumps(state).encode() + b"\n") == state
    for bad in (b"", b"deal|1|60|a|b", b"[]", b'"state"', b'{"rssi":1}', b'{"t":1}', b'{"t":"state"', b"\xff"):
        assert protocol.decode(bad) is None, bad
