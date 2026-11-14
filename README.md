# redondo.ha

Home Assistant side of the Redondo Companion, the round touch display: the custom integration `redondo` (version 0.1.0).

Home Assistant connects to the Companion, as it does with ESPHome devices. The Companion listens on TCP port 7010 and knows no server address. The connection is encrypted with a key the two share; nothing crosses the network in clear.

## What it gives

**Discovery.** A board announces itself on the network (the mDNS service `_redondo._tcp.local`), so Home Assistant shows it under Settings → Devices & services as a discovered Redondo Companion, with its name. The form asks only for the encryption key.

- The entry is created with the host `<name>.local` and the announced port, and only if the Companion that answers there, with that key, says it has the announced name (`wrong_device` otherwise).
- The host is built from the announced name, never from the address or the target in the announcement: an announcement is not authenticated, the handshake is.
- An announcement whose name a board cannot have (lowercase letters, digits and `-`, not at the ends, 1 to 63 characters) is dropped.
- A Companion already configured gets no card: its entry's host becomes `<name>.local` and its port the announced one, without a handshake (the Companion's one connection is the entry's), and the entry reloads only when that changed anything. So one added by IPv4 address follows its name once Home Assistant hears it.
- Known limit: somebody on the LAN who announces services can show cards that no key completes, or change a configured entry's port, which turns its sensor off until the board announces again. The key never leaves Home Assistant and no notification goes to a peer without it.

**By hand.** Add the integration (Settings → Devices & services → Add integration → Redondo Companion): host (default `redondo.local`, the name of a board built without `REDONDO_NAME`; a board with a name is `<name>.local`; an IPv4 address also works), port (default 7010) and the encryption key. It is the way for what Home Assistant cannot hear: the simulator, which does not announce itself, and a Home Assistant that mDNS does not reach (a container behind the Docker bridge, the HAOS virtual machine below). A discovery card of the same Companion left waiting does not stop it, and goes away when the entry is created.

- **The key** is the `COMPANION_KEY` the Companion was built with: 32 bytes in base64, 44 characters, printed by `make key` in the firmware repo. Home Assistant stores it in the config entry as text, as it does with an ESPHome key, and never logs it.
- **Both forms connect and do the handshake** before they create anything. Their errors: `invalid_key` (not 32 bytes of base64), `cannot_connect` (nothing answered at that host and port, or the Companion is already connected to Home Assistant: it takes one connection at a time), `invalid_auth` (the key was refused, or it is not a Companion).
- **The Companion's name** arrives in the handshake and is the entry's title and identity, whatever host was typed. Adding by hand a Companion that is already configured creates nothing: it updates that entry's host, port and key. That is how to follow a Companion added by IPv4 whose address changed, or a key that changed; there is no reauthentication flow.
- Home Assistant keeps the connection open and tries again every 5 s while it is not. A Companion that answers with another name than the entry's is not used. A refused key is logged as a warning once, then tried again like the rest.
- Entries of version 0.0.1 hold no key and are not migrated: delete them and add the Companion again.

A host ending in `.local` is resolved at every try through Home Assistant's own mDNS (its `zeroconf` integration), then through the system resolver if mDNS gives no address; Home Assistant's container has no mDNS in its system resolver. So a Companion added by name is found again after its address changes, with the same device, entity and `config_entry_id`. One added by IPv4 address is not: add it again with the new address, which moves its entry.

- **`binary_sensor.<name>_connectivity`** (`<name>` is the Companion's, with `-` as `_`): on while the encrypted connection is open, that is from the end of the handshake. It goes off at once when the Companion closes it, and within 30 s when the Companion vanishes (TCP keepalive: idle 10 s, then 4 probes 5 s apart; `TCP_USER_TIMEOUT` of 30 s for a line sent and never acknowledged).
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

The session, as ESPHome's native API encrypts its own (`custom_components/redondo/session.py`; the full wire format is in the firmware repo's `apps/companion/SPEC.md`):

- `Noise_NNpsk0_25519_ChaChaPoly_SHA256` with the Python package `noiseprotocol` 0.3.1, a requirement in the manifest that Home Assistant installs by itself. Home Assistant is the initiator, the key is the pre-shared key, the prologue is `redondo/1`.
- Every Noise message travels after its length, 2 bytes big endian, and has at most 1024 bytes.
- Two handshake messages: Home Assistant's, then the Companion's, whose encrypted payload is the Companion's name. Connecting and the handshake have 10 s together.
- After it the Companion sends nothing, and Home Assistant sends the lines below as encrypted messages (a line longer than 1008 bytes spans several).
- A Companion that refuses the key closes the connection without a reply.

One line per notification inside the session, UTF-8, ending in a line feed:

```
kind|urgent|seconds|title|body
email|0|15|3|
deal|1|60|SSD NVMe 2 TB|R$ 599 na Kabum, 32% abaixo do preço pedido
```

The integration replaces line breaks in any field with spaces and refuses a `|` in `kind` or `title` (the body is the last field and may hold one). The Companion shows Latin-1 text only. The full rules live in the firmware repo: `ai-context/decisions/companion-ha-server.md`, `ai-context/decisions/companion-name-encryption-discovery.md` and `ai-context/decisions/companion-eyes-notifications.md`.

## Install on Home Assistant OS

Copy the folder `custom_components/redondo` to `/config/custom_components/redondo` (Samba, SSH or the file editor add-on), restart Home Assistant, then add the integration with the Companion's key. Home Assistant downloads `noiseprotocol` on the first start if it does not have it (the Home Assistant image ships it for ESPHome).

## Develop

```bash
uv sync
uv run pytest
docker run --rm -v "$PWD":/github/workspace ghcr.io/home-assistant/hassfest
```

- `pytest` runs a real in-process Home Assistant (`pytest-homeassistant-custom-component`, which pins the Home Assistant version) against a fake Companion: a TCP server on loopback that answers the handshake as the firmware does (`tests/components/redondo/test_init.py`, 21 tests; the discovery tests hand the flow what Home Assistant's zeroconf would). Tests that open sockets take the plugin's `socket_enabled` fixture. `noiseprotocol` is a dev dependency too: the plugin does not install an integration's requirements.
- `tests/components/conftest.py` imports `custom_components` first; without it Home Assistant loads the plugin's own `custom_components` and reports `Integration 'redondo' not found`.
- hassfest must end with `Invalid integrations: 0`.
- Without Home Assistant, the firmware repo's `make notify [HOST=<host>]` sends lines to a Companion or to its simulator, with the `COMPANION_KEY` of its `local.mk`.

### A real Home Assistant in Docker

`scripts/ha_dev.py` (copied from the homelab `haos` repo) runs Home Assistant Core in a container, with `custom_components/` mounted read-only and the image of the Home Assistant version in the venv, the one pytest uses.

```bash
uv run scripts/ha_dev.py up        # container redondo-ha-dev, http://<this host>:28133, user dev / password dev
uv run scripts/ha_dev.py restart   # after editing the integration (Python loads once)
uv run scripts/ha_dev.py logs      # dev/configuration.yaml turns debug logs on for custom_components
uv run scripts/ha_dev.py token     # an access token for the REST API
uv run scripts/ha_dev.py down      # removes the container and its config volume
```

`--lan` on every command puts the container on this PC's network instead of the Docker bridge: the UI is then on port 8123 and mDNS works, so a real board can be added as `redondo.local` (behind the bridge the name does not resolve). That Home Assistant sees the devices of the LAN.

`uv run pytest -m live` is the automated check (about 30 s; the default `pytest` run skips it): a separate container `redondo-ha-live-test` on port 28134, the config flow through the REST API with a random key, a TCP server on this PC that answers the handshake as the Companion, the sensor `on`, a `redondo.notify` call, the exact line received once decrypted and its text absent from the bytes on the wire; the container is removed afterwards.

### With the Companion's simulator

The container dials out, so nothing of the Companion needs publishing. `up` prints the address this PC has from inside the container (the Docker bridge gateway, usually `172.17.0.1`).

1. In the firmware repo: `make key` once, its line pasted into `local.mk`, then `make sim APP=companion`. The simulator listens on port 7010 (without a key it does not listen); its eyes are tired while nobody is connected. Its name is the board's with `-sim` at the end.
2. `uv run scripts/ha_dev.py up`, open the URL, add the integration Redondo Companion with the host `up` printed, port 7010 and the key of `local.mk`. The eyes wake up and `binary_sensor.<name>_connectivity` turns on.
3. Developer tools → Actions → `redondo.notify`: pick the Companion, fill the fields, perform. The notification shows in the simulator.
4. Close the simulator: the sensor turns off. `uv run scripts/ha_dev.py down` when done.

Run on 2026-11-13 with Home Assistant 2026.10.4 and version 0.1.0, the entry added through the REST API: a wrong key gave `invalid_auth`, the right key created an entry named after the simulator, its sensor turned on, a `redondo.notify` call returned 200 and the notification showed on the simulator's screen.

Not run with version 0.1.0: a real board (version 0.0.1 reached one in clear, added as `redondo.local` with `--lan`), and an address change on a real board (only the automated test, with two loopback addresses).

Discovery, run on 2026-11-13 with Home Assistant 2026.10.4 started with `--lan`, the simulator on this PC and its service announced by a python-zeroconf script (the simulator does not announce itself, and no board was on): Home Assistant opened the flow by itself with the Companion's name in the card, a wrong key gave `invalid_auth`, the right key created the entry named after the Companion with the host `<name>.local`, its sensor turned on, and a `redondo.notify` returned 200 and showed on the simulator's screen. Not run against a board's own announcement.

### Home Assistant OS in a virtual machine

`scripts/haos_vm.py` boots a fresh Home Assistant OS in QEMU/KVM and installs the integration the way it is installed on a real one: the folder copied into `/config/custom_components`. It needs `qemu-system-x86 qemu-utils ovmf xz-utils`, `/dev/kvm` and `sudo` (for `nft`).

```bash
uv run scripts/haos_vm.py start     # downloads HAOS once, boots, waits for Home Assistant
uv run scripts/haos_vm.py install   # copies the integration in, restarts Core, onboards, adds the Companion (needs its key)
uv run scripts/haos_vm.py token     # an access token for the REST API
uv run scripts/haos_vm.py stop      # stops the VM; its disk stays and `start` boots it again
uv run scripts/haos_vm.py destroy   # stops it and deletes ~/vms/redondo-ha
```

- The VM is the user unit `redondo-haos-vm` (2 vCPUs, 4 GiB), its files in `~/vms/redondo-ha`. UI: `http://<this host>:18180` (user `dev`, password `dev` after `install`); 18223 is the legacy port 8123. The first boot downloads Home Assistant Core: 5 to 10 minutes.
- The guest sees this PC as `10.0.2.2`. `install` adds the Companion at `10.0.2.2:7010`, the simulator (`make sim APP=companion` in the firmware repo); `--companion-port` picks another port. It needs the Companion's key: `--key <COMPANION_KEY>`, or the variable `COMPANION_KEY`; without one it stops before touching the VM. Run `install` again after editing the integration.
- The guest cannot reach the LAN: the nftables table `inet redondo_haos_vm` drops what the VM sends to private networks, so this Home Assistant never talks to real devices. `stop` removes the table.
- A real board: pass its IPv4 address to both commands, `start --companion <ipv4>` (opens the block for that address and the Companion's port only; on a running VM it just reloads the rule) and `install --companion <ipv4>` (adds it). A plain `start` leaves the rule as it is; `start --companion 10.0.2.2` closes it again. A name such as `redondo.local` does not resolve in the guest (mDNS does not reach it) and is refused.
- `install` retries `ha core restart` for up to two minutes: right after a boot the Supervisor refuses it.
- `install` types the integration into the guest's serial console (root, no password) as a base64 tar. To look around by hand: `socat - UNIX-CONNECT:$HOME/vms/redondo-ha/serial.sock`; the console's output is also in `~/vms/redondo-ha/serial.log`.
- The image is the newest stable HAOS release that has one for virtual machines (`haos_ova-*.qcow2.xz`); a release can lack it, as 18.5 did.
- Run on 2026-11-13 with HAOS 18.4 and version 0.0.1, in clear: against the simulator (the sensor on, a `redondo.notify` call shown, the sensor off when the simulator closed) and against a real board (the sensor on, a `redondo.notify` call returned 200, the screen not recorded). Not run with version 0.1.0 and a key.
- Cut down from the homelab `haos` repo's `scripts/start-vm.sh`, with its own unit, ports and table, so both VMs can run at once.
