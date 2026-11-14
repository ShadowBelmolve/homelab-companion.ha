"""Run the homelab_companion integration in a real Home Assistant container (copied from the homelab haos repo).

    uv run scripts/ha_dev.py up        # start (or reuse) the container, onboard, print URL
    uv run scripts/ha_dev.py restart   # reload code after editing custom_components/
    uv run scripts/ha_dev.py logs      # follow the HA log
    uv run scripts/ha_dev.py token     # print an access token for the REST API
    uv run scripts/ha_dev.py down      # remove the container and its config volume

`--lan` on every command: the container on this PC's network (port 8123), where
mDNS works, for a real board added as `redondo.local`.

The image tag is the homeassistant version installed in the venv, so the container
runs the same HA the pytest suite tests against. The container dials out to the
Companion: one on this PC (the simulator) is at the address `gateway()` returns.
"""

import argparse
import importlib.metadata
import json as jsonlib
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LABEL = "homelab-companion.ha"
LIVE_TEST_NAME = "homelab-companion-ha-live-test"
USER = {"name": "Dev", "username": "dev", "password": "dev", "language": "en"}


def image() -> str:
    return f"ghcr.io/home-assistant/home-assistant:{importlib.metadata.version('homeassistant')}"


def request(method, url, json=None, form=None, token=None):
    """Return (status, parsed JSON body or None). Network errors raise OSError."""
    headers, data = {}, None
    if json is not None:
        headers["Content-Type"], data = "application/json", jsonlib.dumps(json).encode()
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data, headers, method=method), timeout=10) as r:
            status, body = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read()
    try:
        return status, jsonlib.loads(body) if body else None
    except ValueError:
        return status, None


def wait_ready(base, timeout=300, interval=2):
    # /api/onboarding answers 200 before onboarding and 404 after it, both mean HTTP is up.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if request("GET", f"{base}/api/onboarding")[0] in (200, 404):
                return
        except OSError:
            pass
        time.sleep(interval)
    raise TimeoutError(f"Home Assistant at {base} not ready after {timeout}s")


def _ok(result, what):
    status, body = result
    if status != 200:
        raise RuntimeError(f"{what} failed: HTTP {status} {body}")
    return body


def login(base):
    client = {"client_id": f"{base}/"}
    flow = _ok(request("POST", f"{base}/auth/login_flow", json={
        **client, "handler": ["homeassistant", None], "redirect_uri": f"{base}/?auth_callback=1",
    }), "login flow")
    step = _ok(request("POST", f"{base}/auth/login_flow/{flow['flow_id']}", json={
        **client, "username": USER["username"], "password": USER["password"],
    }), "login")
    if step.get("type") != "create_entry":
        raise RuntimeError(f"login rejected: {step}")
    tokens = _ok(request("POST", f"{base}/auth/token", form={
        **client, "grant_type": "authorization_code", "code": step["result"],
    }), "token")
    return tokens["access_token"]


def onboard_and_login(base):
    """Finish whatever onboarding is left and return an access token for the dev user."""
    status, steps = request("GET", f"{base}/api/onboarding")
    pending = [] if status == 404 else [s["step"] for s in _ok((status, steps), "onboarding") if not s["done"]]
    if "user" in pending:
        _ok(request("POST", f"{base}/api/onboarding/users", json={"client_id": f"{base}/", **USER}), "create user")
    token = login(base)
    for step in ("core_config", "analytics", "integration"):
        if step in pending:
            body = {"client_id": f"{base}/", "redirect_uri": f"{base}/?auth_callback=1"} if step == "integration" else {}
            _ok(request("POST", f"{base}/api/onboarding/{step}", json=body, token=token), f"onboarding {step}")
    return token


def add_config_entry(base, token, domain, user_input):
    """Run a config flow that has a single form step, like the UI does."""
    flow = _ok(request("POST", f"{base}/api/config/config_entries/flow", json={"handler": domain}, token=token), "start flow")
    return _ok(request("POST", f"{base}/api/config/config_entries/flow/{flow['flow_id']}", json=user_input, token=token), "flow step")


def docker(*args, check=True):
    r = subprocess.run(["docker", *args], check=False, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"docker {args[0]} failed: {r.stderr.strip()}")
    return r


def gateway() -> str:
    """This PC's address as the container sees it (the default Docker bridge)."""
    return docker("network", "inspect", "bridge", "--format", "{{(index .IPAM.Config 0).Gateway}}").stdout.strip()


def up(name, port, lan=False):
    base = f"http://127.0.0.1:{port}"
    existing = docker("container", "inspect", "--format", "{{.Config.Image}}", name, check=False)
    if existing.returncode == 0 and existing.stdout.strip() != image():
        raise RuntimeError(f"{name} runs {existing.stdout.strip()}, not {image()}: run `down` first")
    if existing.returncode != 0:
        docker(
            "run", "-d", "--name", name, "--label", f"{LABEL}={name}", "-e", "TZ=America/Sao_Paulo",
            # On the PC's network mDNS works (a board as redondo.local); behind the bridge it does not.
            *(["--network", "host"] if lan else ["-p", f"0.0.0.0:{port}:8123"]),
            "-v", f"{name}-config:/config",
            "-v", f"{REPO / 'custom_components'}:/config/custom_components:ro",
            "-v", f"{REPO / 'dev' / 'configuration.yaml'}:/config/configuration.yaml:ro",
            image(),
        )
    else:
        docker("start", name)
    wait_ready(base)
    return base, onboard_and_login(base)


def down(name):
    docker("rm", "-f", name, check=False)
    docker("volume", "rm", f"{name}-config", check=False)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["up", "restart", "logs", "token", "down"])
    p.add_argument("--name", default="homelab-companion-ha-dev")
    p.add_argument("--port", type=int, default=28133)
    p.add_argument("--lan", action="store_true", help="on this PC's network, for a real board by its mDNS name; the port is then 8123")
    a = p.parse_args(argv)
    if a.lan:
        a.port = 8123
    base = f"http://127.0.0.1:{a.port}"
    if a.command == "up":
        up(a.name, a.port, a.lan)
        print(f"Home Assistant {image().rsplit(':', 1)[1]} up: http://<this host>:{a.port} (user dev / password dev)")
        print(f"A Companion on this PC (make sim APP=companion) is host {gateway()}, port 7010; the key is its COMPANION_KEY")
    elif a.command == "restart":
        docker("restart", a.name)
        wait_ready(base)
        print("restarted")
    elif a.command == "logs":
        subprocess.run(["docker", "logs", "-f", "--tail", "100", a.name], check=False)
    elif a.command == "token":
        print(login(base))
    else:
        down(a.name)


if __name__ == "__main__":
    sys.exit(main())
