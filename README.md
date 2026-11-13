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

### A real Home Assistant in Docker

`scripts/ha_dev.py` (copied from the homelab `haos` repo) runs Home Assistant Core in a container, with `custom_components/` mounted read-only and the image of the Home Assistant version in the venv, the one pytest uses.

```bash
uv run scripts/ha_dev.py up        # container redondo-ha-dev, http://<this host>:28133, user dev / password dev
uv run scripts/ha_dev.py restart   # after editing the integration (Python loads once)
uv run scripts/ha_dev.py logs      # dev/configuration.yaml turns debug logs on for custom_components
uv run scripts/ha_dev.py token     # an access token for the REST API
uv run scripts/ha_dev.py down      # removes the container and its config volume
```

`uv run pytest -m live` is the automated check (about 30 s; the default `pytest` run skips it): a separate container `redondo-ha-live-test` on port 28134, the config flow through the REST API, a TCP server on this PC as the Companion, the sensor `on`, a `redondo.notify` call and the exact line received; the container is removed afterwards.

### With the Companion's simulator

The container dials out, so nothing of the Companion needs publishing. `up` prints the address this PC has from inside the container (the Docker bridge gateway, usually `172.17.0.1`).

1. In the firmware repo: `make sim APP=companion`. The simulator listens on port 7010; its eyes are tired while nobody is connected.
2. `uv run scripts/ha_dev.py up`, open the URL, add the integration Redondo Companion with the host `up` printed and port 7010. The eyes wake up and `binary_sensor.<host>_connectivity` turns on.
3. Developer tools → Actions → `redondo.notify`: pick the Companion, fill the fields, perform. The notification shows in the simulator.
4. Close the simulator: the sensor turns off. `uv run scripts/ha_dev.py down` when done.

Run on 2026-11-13 with Home Assistant 2026.10.4: the sensor turned on, an `urgent-email` notification showed in the simulator, the sensor turned off when the simulator closed.

### Home Assistant OS in a virtual machine

`scripts/haos_vm.py` boots a fresh Home Assistant OS in QEMU/KVM and installs the integration the way it is installed on a real one: the folder copied into `/config/custom_components`. It needs `qemu-system-x86 qemu-utils ovmf xz-utils`, `/dev/kvm` and `sudo` (for `nft`).

```bash
uv run scripts/haos_vm.py start     # downloads HAOS once, boots, waits for Home Assistant
uv run scripts/haos_vm.py install   # copies the integration in, restarts Core, onboards, adds the Companion
uv run scripts/haos_vm.py token     # an access token for the REST API
uv run scripts/haos_vm.py stop      # stops the VM; its disk stays and `start` boots it again
uv run scripts/haos_vm.py destroy   # stops it and deletes ~/vms/redondo-ha
```

- The VM is the user unit `redondo-haos-vm` (2 vCPUs, 3 GiB), its files in `~/vms/redondo-ha`. UI: `http://<this host>:18180` (user `dev`, password `dev` after `install`); 18223 is the legacy port 8123. The first boot downloads Home Assistant Core: 5 to 10 minutes.
- The guest sees this PC as `10.0.2.2`. `install` adds the Companion at `10.0.2.2:7010`, the simulator (`make sim APP=companion` in the firmware repo); `--companion-port` picks another port. Run `install` again after editing the integration.
- The guest cannot reach the LAN: the nftables table `inet redondo_haos_vm` drops what the VM sends to private networks, so this Home Assistant never talks to real devices, a real board included. `stop` removes the table.
- `install` types the integration into the guest's serial console (root, no password) as a base64 tar. To look around by hand: `socat - UNIX-CONNECT:$HOME/vms/redondo-ha/serial.sock`; the console's output is also in `~/vms/redondo-ha/serial.log`.
- The image is the newest stable HAOS release that has one for virtual machines (`haos_ova-*.qcow2.xz`); a release can lack it, as 18.5 did.
- Run on 2026-11-13 with HAOS 18.4 and the simulator: `start`, `install`, the sensor `binary_sensor.10_0_2_2_connectivity` turned on, a `redondo.notify` call showed in the simulator, the sensor turned off when the simulator closed.
- Cut down from the homelab `haos` repo's `scripts/start-vm.sh`, with its own unit, ports and table, so both VMs can run at once.
