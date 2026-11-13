"""End-to-end check in a real HA container: uv run pytest -m live"""

import socket
import time

import pytest

from scripts import ha_dev

pytestmark = pytest.mark.live
NAME, PORT = ha_dev.LIVE_TEST_NAME, 28134


@pytest.fixture
def ha(socket_enabled):
    ha_dev.down(NAME)  # leftovers of an aborted run
    try:
        yield ha_dev.up(NAME, PORT)
    finally:
        ha_dev.down(NAME)


def test_notify_reaches_a_companion_on_this_pc(ha):
    base, token = ha
    host = ha_dev.gateway()
    # The Companion's side: a TCP server the container can reach.
    with socket.create_server(("0.0.0.0", 0)) as companion:
        companion.settimeout(30)
        port = companion.getsockname()[1]
        result = ha_dev.add_config_entry(base, token, "redondo", {"host": host, "port": port})
        assert result["type"] == "create_entry"
        link, _ = companion.accept()

        sensor = f"{base}/api/states/binary_sensor.{host.replace('.', '_')}_connectivity"
        deadline = time.monotonic() + 10
        while (state := ha_dev.request("GET", sensor, token=token)[1])["state"] != "on":
            assert time.monotonic() < deadline, state
            time.sleep(0.2)

        note = {"kind": "deal", "urgent": True, "seconds": 60, "title": "SSD NVMe 2 TB", "body": "R$ 599 na Kabum"}
        call = {"config_entry_id": result["result"]["entry_id"], **note}
        status, body = ha_dev.request("POST", f"{base}/api/services/redondo/notify", json=call, token=token)
        assert status == 200, body
        link.settimeout(10)
        assert link.makefile("rb").readline() == "deal|1|60|SSD NVMe 2 TB|R$ 599 na Kabum\n".encode()
        link.close()
