# redondo.ha

Home Assistant side of the Redondo Companion, the round touch display: the custom integration `redondo` (version 0.0.1).

Home Assistant connects to the Companion, as it does with ESPHome devices. The Companion listens on TCP port 7010 and knows no server address.

## What it gives

Add the integration (Settings → Devices & services → Add integration → Redondo Companion) once per Companion: host (default `redondo.local`; an IPv4 address also works) and port (default 7010). Home Assistant keeps the connection open and tries again every 5 s while it is not.

- **`binary_sensor.<host>_connectivity`**: on while the connection is open. It goes off at once when the Companion closes it, and within 30 s when the Companion vanishes (TCP keepalive: idle 10 s, then 4 probes 5 s apart; `TCP_USER_TIMEOUT` of 30 s for a line sent and never acknowledged).
- **Action `redondo.notify`**: sends one notification. It fails when that Companion is not connected; nothing is queued.

| Field | Type | Meaning |
|-------|------|---------|
| `config_entry_id` | config entry | Which Companion |
| `kind` | text | Picks the screen: `email`, `urgent-email`, `deal`; any other kind shows as text only. Not checked here, so a new screen in the firmware needs no update of the integration |
| `urgent` | boolean | Picks the Companion's queue: urgent ones show first |
| `seconds` | number, 1 to 4294967295 | The most it stays on screen |
| `title` | text | One line. For `email`, the number of unread messages |
| `body` | text, optional | Up to three lines |

```yaml
action: redondo.notify
data:
  config_entry_id: 01JEXAMPLE0000000000000000
  kind: deal
  urgent: true
  seconds: 60
  title: SSD NVMe 2 TB
  body: R$ 599 na Kabum, 32% abaixo do preço pedido
```

## Protocol

One line per notification over the connection, UTF-8, ending in a line feed:

```
kind|urgent|seconds|title|body
email|0|15|3|
deal|1|60|SSD NVMe 2 TB|R$ 599 na Kabum, 32% abaixo do preço pedido
```

The integration replaces line breaks in any field with spaces and refuses a `|` in `kind` or `title` (the body is the last field and may hold one). The Companion shows Latin-1 text only. The full rules live in the firmware repo: `ai-context/decisions/companion-ha-server.md` and `ai-context/decisions/companion-eyes-notifications.md`.

## Install on Home Assistant OS

Copy the folder `custom_components/redondo` to `/config/custom_components/redondo` (Samba, SSH or the file editor add-on), restart Home Assistant, then add the integration.

## Develop

```bash
uv sync
uv run pytest
docker run --rm -v "$PWD":/github/workspace ghcr.io/home-assistant/hassfest
```

- `pytest` runs a real in-process Home Assistant (`pytest-homeassistant-custom-component`, which pins the Home Assistant version) against a fake Companion: a TCP server on loopback (`tests/components/redondo/test_init.py`). Tests that open sockets take the plugin's `socket_enabled` fixture.
- `tests/components/conftest.py` imports `custom_components` first; without it Home Assistant loads the plugin's own `custom_components` and reports `Integration 'redondo' not found`.
- hassfest must end with `Invalid integrations: 0`.
- Without Home Assistant, the firmware repo's `tools/notify.py` sends lines to a Companion or to its simulator.
