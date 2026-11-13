"""Try the redondo integration in Home Assistant OS, in a QEMU/KVM virtual machine.

    uv run scripts/haos_vm.py start     # download HAOS if missing, boot the VM, wait for Home Assistant
    uv run scripts/haos_vm.py install   # copy the integration in, restart Core, onboard, add the Companion
    uv run scripts/haos_vm.py token     # print an access token for the REST API
    uv run scripts/haos_vm.py stop      # stop the VM (its disk stays, `start` boots it again)
    uv run scripts/haos_vm.py destroy   # stop it and delete its disk

A fresh HAOS, not the user's: nothing is restored into it. Cut down from the
homelab haos repo's scripts/start-vm.sh, with its own directory, unit, ports and
nftables table, so both can run at once. Needs qemu-system-x86, qemu-utils, ovmf,
xz-utils, /dev/kvm and sudo (for nft).

The guest sees this PC as 10.0.2.2 (QEMU user networking), which is where it
finds a Companion simulator (`make sim APP=companion` in the firmware repo).
The LAN is blocked for the guest; for a real board, pass its address to both
`start` and `install` with `--companion IPV4` (a name like redondo.local does
not resolve in the guest).
"""

import argparse
import base64
import io
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

import ha_dev

REPO = Path(__file__).resolve().parent.parent
DIR = Path.home() / "vms" / "redondo-ha"
UNIT = "redondo-haos-vm.service"
TABLE = "redondo_haos_vm"
# Host ports -> guest: 80 (the UI of a fresh install), 8123 (its legacy port).
UI_PORT, LEGACY_PORT = 18180, 18223
BASE = f"http://127.0.0.1:{UI_PORT}"
RELEASES = "https://api.github.com/repos/home-assistant/operating-system/releases"  # newest first
HOST_FROM_GUEST = "10.0.2.2"
# Where HAOS keeps Home Assistant's /config.
GUEST_CONFIG = "/mnt/data/supervisor/homeassistant"


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def download():
    """Fetch the latest HAOS disk image into DIR, once."""
    DIR.mkdir(parents=True, exist_ok=True)
    disk = DIR / "haos.qcow2"
    if not disk.exists():
        # Not /releases/latest: a release may hold images for some boards
        # only (18.5 has none for virtual machines).
        with urllib.request.urlopen(RELEASES) as reply:
            releases = [release for release in json.load(reply) if not release["prerelease"]]
        assets = [asset["browser_download_url"] for release in releases for asset in release["assets"]]
        url = next(url for url in assets if re.search(r"/haos_ova-[^/]+\.qcow2\.xz$", url))
        print(f"downloading {url}")
        run("curl", "-fL", "--progress-bar", "-o", f"{disk}.xz", url)
        run("xz", "-d", f"{disk}.xz")
    if not (DIR / "vars.fd").exists():
        shutil.copy("/usr/share/OVMF/OVMF_VARS_4M.fd", DIR / "vars.fd")


def running() -> bool:
    return subprocess.run(["systemctl", "--user", "is-active", "--quiet", UNIT]).returncode == 0


def cgroup() -> Path:
    uid = os.getuid()
    return Path(f"/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service/app.slice/{UNIT}")


def block_lan(companion: str, port: int) -> None:
    """Drop what the guest sends to private networks, except to a Companion's port there."""
    # The guest's traffic leaves from the qemu process: match its cgroup,
    # which must exist before the rule loads. Home Assistant in the VM never
    # talks to real devices, apart from the one Companion it is pointed at.
    private = "10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 100.64.0.0/10, 169.254.0.0/16, 224.0.0.0/4"
    match = f'socket cgroupv2 level 5 "{cgroup().relative_to("/sys/fs/cgroup")}"'
    hole = "" if companion == HOST_FROM_GUEST else f"    {match} ip daddr {companion} tcp dport {port} accept\n"
    rules = f"""table inet {TABLE}
delete table inet {TABLE}
table inet {TABLE} {{
  chain output {{
    type filter hook output priority 0; policy accept;
{hole}    {match} ip daddr {{ {private} }} ct state new counter drop
  }}
}}
"""
    run("sudo", "nft", "-f", "-", input=rules, text=True)


def start(companion: str | None, port: int):
    download()
    if running():
        # Only when asked: a plain `start` must not close the way to a board.
        if companion:
            block_lan(companion, port)
    else:
        # A socket left by a qemu that was killed is not this one's.
        (DIR / "monitor.sock").unlink(missing_ok=True)
        forwards = f"hostfwd=tcp:0.0.0.0:{UI_PORT}-:80,hostfwd=tcp:0.0.0.0:{LEGACY_PORT}-:8123"
        # fmt: off
        run(
            "systemd-run", "--user", f"--unit={UNIT}", "--collect", "--",
            # -S: paused until the LAN block below is loaded.
            "qemu-system-x86_64", "-S", "-enable-kvm", "-machine", "q35", "-cpu", "host", "-smp", "2", "-m", "4096",
            "-drive", "if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd",
            "-drive", f"if=pflash,format=raw,file={DIR}/vars.fd",
            "-drive", f"file={DIR}/haos.qcow2,if=virtio,format=qcow2",
            "-netdev", f"user,id=n0,ipv6=off,{forwards}", "-device", "virtio-net-pci,netdev=n0", "-display", "none",
            "-chardev", f"socket,id=ser0,path={DIR}/serial.sock,server=on,wait=off,logfile={DIR}/serial.log",
            "-serial", "chardev:ser0", "-monitor", f"unix:{DIR}/monitor.sock,server,nowait",
        )
        # fmt: on
        try:
            while not cgroup().is_dir() or not (DIR / "monitor.sock").is_socket():
                if not running():
                    sys.exit(f"qemu exited, see: journalctl --user -u {UNIT}")
                time.sleep(0.2)
            block_lan(companion or HOST_FROM_GUEST, port)
            run("socat", "-", f"UNIX-CONNECT:{DIR}/monitor.sock", input="cont\n", text=True, stdout=subprocess.DEVNULL)
        except BaseException:
            # Nothing paused left behind: the next `start` would wait for it.
            stop()
            raise
        print("VM started, LAN blocked; the first boot downloads Home Assistant (5 to 10 minutes)")
    ha_dev.wait_ready(BASE, timeout=1200, interval=5)
    print(f"Home Assistant up: http://<this host>:{UI_PORT}")


def guest(commands: list[str], timeout: float = 600) -> None:
    """Run shell commands in the guest through its serial console (root, no password)."""
    with socket.socket(socket.AF_UNIX) as console:
        console.connect(str(DIR / "serial.sock"))

        def read_until(pattern: bytes, wait: float) -> bytes:
            seen, deadline = b"", time.monotonic() + wait
            while not re.search(pattern, seen):
                console.settimeout(max(deadline - time.monotonic(), 0.01))
                try:
                    seen += console.recv(4096)
                except TimeoutError:
                    break
            return seen

        console.sendall(b"\n")
        if b"login:" in read_until(rb"login:|# ", wait=5):
            console.sendall(b"root\n")
            read_until(rb"# ", wait=10)
        for number, command in enumerate(commands):
            # The echo of the typed line holds `$?`, only its output a number.
            console.sendall(f"{command}; echo ended-{number}-$?\n".encode())
            seen = read_until(rb"ended-%d-\d" % number, wait=timeout)
            if b"ended-%d-0" % number not in seen:
                raise RuntimeError(f"in the guest: {command[:60]!r} failed:\n{seen.decode(errors='replace')[-600:]}")


def install(companion: str | None, companion_port: int):
    companion = companion or HOST_FROM_GUEST
    # A tar of the integration, typed into the console as base64: the guest
    # needs no network path to this PC and no extra tool. gzip's checksum
    # makes the extraction fail if the console dropped a byte.
    packed = io.BytesIO()
    with tarfile.open(fileobj=packed, mode="w:gz") as tar:
        tar.add(REPO / "custom_components" / "redondo", "redondo", filter=lambda f: None if "__pycache__" in f.name else f)
    text = base64.encodebytes(packed.getvalue()).decode()
    target = f"{GUEST_CONFIG}/custom_components"
    guest(
        [
            f"cat > /tmp/redondo.b64 <<'END'\n{text}END\ntrue",
            f"rm -rf {target}/redondo && mkdir -p {target} && base64 -d /tmp/redondo.b64 | tar -xz -C {target}",
            # Right after a boot the Supervisor refuses it: up to two minutes of tries.
            "n=0; until ha core restart || [ $((n+=1)) -ge 12 ]; do sleep 10; done; [ $n -lt 12 ]",
        ]
    )
    ha_dev.wait_ready(BASE, timeout=600, interval=5)
    # Right after a restart a request can outlast ha_dev's 10 s timeout. Both
    # calls can run again: onboarding does only the steps left, and a second
    # entry for the same host is refused.
    for tries_left in reversed(range(6)):
        try:
            token = ha_dev.onboard_and_login(BASE)
            result = ha_dev.add_config_entry(BASE, token, "redondo", {"host": companion, "port": companion_port})
            break
        except OSError:
            if not tries_left:
                raise
            time.sleep(10)
    added = "added" if result["type"] == "create_entry" else f"not added ({result.get('reason', result['type'])})"
    print(f"integration installed; Companion {companion}:{companion_port} {added}")
    print(f"http://<this host>:{UI_PORT} (user dev / password dev)")


def stop():
    subprocess.run(["systemctl", "--user", "stop", UNIT], check=False, stderr=subprocess.DEVNULL)
    subprocess.run(["sudo", "nft", "delete", "table", "inet", TABLE], check=False, stderr=subprocess.DEVNULL)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["start", "install", "token", "stop", "destroy"])
    p.add_argument(
        "--companion",
        type=lambda text: str(ipaddress.IPv4Address(text)),
        metavar="IPV4",
        help="start, install: a Companion on the LAN (a real board) instead of the simulator on this PC; `start` "
        f"opens the LAN block for that address and port only ({HOST_FROM_GUEST} closes it again), `install` adds it",
    )
    p.add_argument("--companion-port", type=int, default=7010, help="start, install: the Companion's port")
    a = p.parse_args(argv)
    if a.command == "start":
        start(a.companion, a.companion_port)
    elif a.command == "install":
        install(a.companion, a.companion_port)
    elif a.command == "token":
        print(ha_dev.login(BASE))
    else:
        stop()
        if a.command == "destroy":
            shutil.rmtree(DIR, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
