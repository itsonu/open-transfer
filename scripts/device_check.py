"""Real-device check: drive the real apps on your computer, phone and tablet.

Run it on the Windows PC or Mac, with the Open Transfer app open there and your
Android devices connected over adb (USB or wireless debugging):

    python scripts/device_check.py --apk open-transfer-android.zip

It installs the APK on every connected Android device (``--apk`` takes the .apk
or the CI artifact .zip), launches it, and then runs the checklist in
docs/device-testing.md that can run without a person: discovery, renaming,
leaving and coming back, both-direction pairing by code (and the exact request
a QR scan makes), transfers in every direction between every two devices
(verified byte for byte, and on Android in Download/Open Transfer), one-to-one,
one-to-many, everyone, decline, expiry, cancel, too-big files, a receiver
leaving mid-transfer and "Send now". Everything travels over your Wi-Fi between
the real apps; the script only presses the buttons, through the same requests
the apps' own screens make.

Results go to ``device-check-<time>.md`` (✅ / ❌ per check, plus the checks that
need a person) and, if anything fails, Android logs next to it.

How the script reaches each app: the computer's app at http://127.0.0.1:5000
or the next port up (``--pc-url`` to choose), each Android app through ``adb forward``. Both arrive from the
device's own loopback address, which is what lets the script act as the owner.

Standard library only; Python 3.10+. Needs ``adb`` (Android platform-tools) on
PATH, in ANDROID_HOME, or given with ``--adb``.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

PKG = "io.github.itsonu.opentransfer"
DEVICE_PORT = 5000
FORWARD_BASE = 5101
ANDROID_FOLDER = "/sdcard/Download/Open Transfer"
FINAL = {"done", "declined", "expired", "canceled", "failed"}
MB = 1024 * 1024

# --------------------------------------------------------------------- output

PASS, FAIL, SKIP, MANUAL = "✅", "❌", "⏭️", "👤"


@dataclass
class Result:
    ref: str
    title: str
    status: str
    note: str = ""


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)
    env: list[str] = field(default_factory=list)

    def add(self, ref: str, title: str, ok: bool | None, note: str = "") -> bool:
        status = SKIP if ok is None else PASS if ok else FAIL
        self.results.append(Result(ref, title, status, note))
        print(f"  {status} {ref} {title}" + (f" — {note}" if note else ""), flush=True)
        return bool(ok)

    def manual(self, ref: str, title: str, how: str) -> None:
        self.results.append(Result(ref, title, MANUAL, how))

    @property
    def failed(self) -> list[Result]:
        return [r for r in self.results if r.status == FAIL]

    def markdown(self) -> str:
        counts = {s: sum(r.status == s for r in self.results) for s in (PASS, FAIL, SKIP, MANUAL)}
        lines = [
            "# Open Transfer — real-device check",
            "",
            f"**{counts[PASS]} passed · {counts[FAIL]} failed · {counts[SKIP]} skipped · "
            f"{counts[MANUAL]} need a person**",
            "",
            "## Devices",
            "",
            *[f"- {line}" for line in self.env],
            "",
            "## Automated checks",
            "",
            "| | # | Check | Note |",
            "|---|---|---|---|",
        ]
        for r in self.results:
            if r.status != MANUAL:
                lines.append(f"| {r.status} | {r.ref} | {_cell(r.title)} | {_cell(r.note)} |")
        lines += [
            "",
            "## Still needs a person (fill in ✅ / ❌)",
            "",
            "| Result | # | Check | How |",
            "|---|---|---|---|",
        ]
        for r in self.results:
            if r.status == MANUAL:
                lines.append(f"|  | {r.ref} | {_cell(r.title)} | {_cell(r.note)} |")
        return "\n".join(lines) + "\n"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def step(title: str) -> None:
    print(f"\n== {title}", flush=True)


# ----------------------------------------------------------------------- HTTP


class CheckError(RuntimeError):
    pass


class TooFast(Exception):
    """A transfer finished before the script could interrupt it."""


def request(
    base: str,
    method: str,
    path: str,
    body: Any = None,
    *,
    upload: Path | None = None,
    timeout: float = 30,
) -> tuple[int, Any]:
    """One request (no proxies: everything here is on this machine or the LAN)."""
    url = urlsplit(base)
    conn = http.client.HTTPConnection(
        url.hostname or "127.0.0.1", url.port or 80, timeout=timeout, blocksize=MB
    )
    headers = {"Accept": "application/json"}
    try:
        if upload is not None:
            headers["Content-Type"] = "application/octet-stream"
            headers["Content-Length"] = str(upload.stat().st_size)
            with upload.open("rb") as fh:
                conn.request(method, path, body=fh, headers=headers)
                response = conn.getresponse()
        else:
            data = None
            if body is not None:
                data = json.dumps(body).encode()
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=data, headers=headers)
            response = conn.getresponse()
        raw = response.read()
    finally:
        conn.close()
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        parsed = raw.decode(errors="replace")
    return response.status, parsed


def expect(
    base: str, method: str, path: str, body: Any = None, ok: tuple[int, ...] = (200,), **kw: Any
) -> Any:
    status, data = request(base, method, path, body, **kw)
    if status not in ok:
        raise CheckError(f"{method} {path} → {status}: {data!r}"[:400])
    return data


def download_sha256(base: str, path: str, timeout: float = 120) -> tuple[int, str]:
    url = urlsplit(base)
    conn = http.client.HTTPConnection(url.hostname or "127.0.0.1", url.port or 80, timeout=timeout)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        if response.status != 200:
            raise CheckError(f"GET {path} → {response.status}")
        digest, size = hashlib.sha256(), 0
        while chunk := response.read(MB):
            digest.update(chunk)
            size += len(chunk)
    finally:
        conn.close()
    return size, digest.hexdigest()


def wait_for(what: str, check: Callable[[], Any], timeout: float, every: float = 0.5) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise CheckError(f"timed out after {timeout:.0f}s waiting for {what}")
        time.sleep(every)


# ------------------------------------------------------------------------ adb


class Adb:
    def __init__(self, exe: str) -> None:
        self.exe = exe

    def run(self, *args: str, serial: str | None = None, timeout: float = 120) -> str:
        cmd = [self.exe, *(["-s", serial] if serial else []), *args]
        done = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        if done.returncode != 0:
            raise CheckError(f"{' '.join(cmd)} failed: {(done.stderr or done.stdout).strip()}")
        return done.stdout

    def shell(self, serial: str, command: str, timeout: float = 60) -> str:
        return self.run("shell", command, serial=serial, timeout=timeout)

    def serials(self) -> list[str]:
        out = self.run("devices")
        return [
            line.split()[0]
            for line in out.splitlines()[1:]
            if line.strip() and line.split()[-1] == "device"
        ]


def find_adb(given: str | None) -> str | None:
    if given:
        return given
    if found := shutil.which("adb"):
        return found
    for env in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if home := os.environ.get(env):
            for name in ("adb", "adb.exe"):
                candidate = Path(home) / "platform-tools" / name
                if candidate.is_file():
                    return str(candidate)
    return None


def apk_from(path: Path, tmp: Path) -> Path:
    """Accept the .apk or the CI artifact .zip that contains it."""
    if path.suffix.lower() != ".zip":
        return path
    with zipfile.ZipFile(path) as archive:
        names = [n for n in archive.namelist() if n.lower().endswith(".apk")]
        if not names:
            raise CheckError(f"{path} has no .apk inside")
        archive.extract(names[0], tmp)
        return tmp / names[0]


# ---------------------------------------------------------------------- nodes


@dataclass
class Node:
    label: str
    base: str
    kind: str  # "computer" | "android"
    lan_host: str = ""
    lan_port: int = DEVICE_PORT
    serial: str = ""
    forward_port: int = 0
    expected_form: str = ""
    expected_platform: str = ""
    id: str = ""
    name: str = ""
    auto_accept: bool = False

    def state(self) -> dict[str, Any]:
        return expect(self.base, "GET", "/api/state")  # type: ignore[no-any-return]

    @property
    def address(self) -> str:
        return f"{self.lan_host}:{self.lan_port}"

    def refresh(self) -> None:
        state = self.state()
        if "pairing" not in state:
            raise CheckError(
                f"{self.base} doesn't treat this script as its owner — is it this device's app?"
            )
        self.id, self.name = state["host"]["id"], state["host"]["name"]
        self.auto_accept = bool(state.get("settings", {}).get("auto_accept"))

    def device(self, other: Node) -> dict[str, Any] | None:
        for d in self.state().get("devices", []):
            if d.get("id") == other.id:
                return d  # type: ignore[no-any-return]
        return None

    def sees(self, other: Node) -> bool:
        d = self.device(other)
        return bool(d and d.get("online"))

    def paired_with(self, other: Node) -> bool:
        d = self.device(other)
        return bool(d and d.get("paired"))


def sees_all(node: Node, others: list[Node]) -> bool:
    return all(node.sees(o) for o in others)


def local_platform() -> str:
    return {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")


def lan_ip_towards(host: str) -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((host, 9))
        return str(s.getsockname()[0])


def android_node(adb: Adb, serial: str, index: int, report: Report) -> Node:
    def prop(key: str) -> str:
        return adb.shell(serial, f"getprop {key}").strip()

    model = f"{prop('ro.product.manufacturer')} {prop('ro.product.model')}".strip()
    release, sdk = prop("ro.build.version.release"), prop("ro.build.version.sdk")
    size = re.findall(r"(\d+)x(\d+)", adb.shell(serial, "wm size"))[-1]
    density = int(re.findall(r"(\d+)", adb.shell(serial, "wm density"))[-1])
    smallest_dp = min(int(size[0]), int(size[1])) * 160 // density
    form = "tablet" if smallest_dp >= 600 else "phone"
    ip_out = adb.shell(serial, "ip -f inet addr show wlan0")
    ips = re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", ip_out)
    report.env.append(
        f"**{model}** — Android {release} (API {sdk}), {form} (sw{smallest_dp}dp), "
        f"Wi-Fi {ips[0] if ips else 'unknown'}, adb `{serial}`"
    )
    port = FORWARD_BASE + index
    return Node(
        label=f"{model} ({form})",
        base=f"http://127.0.0.1:{port}",
        kind="android",
        lan_host=ips[0] if ips else "",
        serial=serial,
        forward_port=port,
        expected_form=form,
        expected_platform="android",
    )


def is_open_transfer(base: str) -> bool:
    try:
        status, data = request(base, "GET", "/api/health", timeout=3)
    except OSError:
        return False
    return status == 200 and isinstance(data, dict) and data.get("status") == "ok"


def find_computer_app(given: str | None) -> str:
    """The app on this computer: --pc-url, else the first of 5000-5019 that answers.

    (The app moves up from 5000 when it's taken — on a Mac, AirPlay Receiver has it.)
    """
    if given:
        return given.rstrip("/")
    for port in range(DEVICE_PORT, DEVICE_PORT + 20):
        if is_open_transfer(f"http://127.0.0.1:{port}"):
            return f"http://127.0.0.1:{port}"
    raise CheckError(
        "No Open Transfer app answered on 127.0.0.1:5000–5019. Open the app on this "
        "computer first, or pass --pc-url."
    )


def launch_android(adb: Adb, node: Node, timeout: float = 180) -> None:
    """Start the app and forward to its server (port 5000, or the next free one)."""
    adb.shell(node.serial, f"am start -W -n {PKG}/.MainActivity")
    ports = [node.lan_port, *(p for p in range(DEVICE_PORT, DEVICE_PORT + 5) if p != node.lan_port)]
    tries = 0

    def up() -> bool:
        nonlocal tries
        port = ports[tries % len(ports)] if tries >= 5 else node.lan_port
        tries += 1
        adb.run("forward", f"tcp:{node.forward_port}", f"tcp:{port}", serial=node.serial)
        if is_open_transfer(node.base):
            node.lan_port = port
            return True
        return False

    wait_for(f"{node.label}'s app to start", up, timeout, every=2)


def stop_android(adb: Adb, node: Node) -> None:
    adb.shell(node.serial, f"am force-stop {PKG}")


def background(work: Callable[[], Any]) -> threading.Thread:
    """Run an upload that is expected to be cut short; its errors don't matter."""

    def run() -> None:
        with contextlib.suppress(CheckError, OSError):
            work()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


# ------------------------------------------------------------------- the check


class Checker:
    def __init__(self, args: argparse.Namespace, report: Report, tmp: Path) -> None:
        self.args = args
        self.report = report
        self.tmp = tmp
        self.adb: Adb | None = None
        self.pc: Node | None = None
        self.androids: list[Node] = []
        self.files: dict[int, tuple[Path, str]] = {}
        self.run_id = time.strftime("%H%M%S")
        self.received: list[tuple[Node, str]] = []
        self.counter = 0

    @property
    def computer(self) -> Node:
        if self.pc is None:
            raise CheckError("setup didn't finish")
        return self.pc

    @property
    def nodes(self) -> list[Node]:
        return ([self.pc] if self.pc else []) + self.androids

    # ------------------------------------------------------------ helpers

    def test_file(self, size: int) -> tuple[Path, str]:
        if size not in self.files:
            path = self.tmp / f"payload-{size}.bin"
            digest = hashlib.sha256()
            with path.open("wb") as fh:
                left = size
                while left:
                    chunk = os.urandom(min(left, 4 * MB))
                    fh.write(chunk)
                    digest.update(chunk)
                    left -= len(chunk)
            self.files[size] = (path, digest.hexdigest())
        return self.files[size]

    def file_name(self, src: Node, dst: Node, what: str, ext: str) -> str:
        self.counter += 1

        def slug(node: Node) -> str:
            return re.sub(r"[^A-Za-z0-9]+", "-", node.name).strip("-")[:16]

        return f"ot-check-{self.run_id}-{self.counter:02d}-{slug(src)}-to-{slug(dst)}-{what}{ext}"

    def offer(self, src: Node, targets: list[Node], name: str, size: int) -> str:
        job = expect(
            src.base,
            "POST",
            "/api/send",
            {
                "to": [t.id for t in targets],
                "files": [{"name": name, "size": size, "mime": "application/octet-stream"}],
            },
            ok=(201,),
        )
        return str(job["job"]["id"])

    def incoming(self, dst: Node, name: str) -> dict[str, Any] | None:
        for s in dst.state().get("incoming", []):
            if s.get("files") and s["files"][0].get("name") == name:
                return s  # type: ignore[no-any-return]
        return None

    def target(self, src: Node, job_id: str, dst: Node) -> dict[str, Any] | None:
        for job in src.state().get("outgoing", []):
            if job.get("id") == job_id:
                for t in job.get("targets", []):
                    if t.get("id") == dst.id:
                        return t  # type: ignore[no-any-return]
        return None

    def answer(self, dst: Node, name: str, accept: bool) -> dict[str, Any]:
        session = wait_for(f"the offer to reach {dst.label}", lambda: self.incoming(dst, name), 30)
        if session["state"] == "pending":
            decision = "accept" if accept else "decline"
            expect(dst.base, "POST", f"/api/incoming/{session['id']}/{decision}")
        return session  # type: ignore[no-any-return]

    def wait_answered(self, src: Node, job_id: str, targets: list[Node]) -> dict[str, str]:
        def answered() -> dict[str, str] | None:
            states = {t.id: (self.target(src, job_id, t) or {}).get("state", "") for t in targets}
            if any(s in {"offering", "waiting", ""} for s in states.values()):
                return None
            return states

        return wait_for("every receiver to answer", answered, 40)  # type: ignore[no-any-return]

    def upload(self, src: Node, job_id: str, path: Path) -> dict[str, Any]:
        size = path.stat().st_size
        return expect(  # type: ignore[no-any-return]
            src.base, "PUT", f"/api/send/{job_id}/files/0", upload=path, timeout=max(120, size / MB)
        )

    def verify_received(self, dst: Node, name: str, size: int, digest: str) -> tuple[bool, str]:
        def done() -> dict[str, Any] | None:
            s = self.incoming(dst, name)
            if s and s["state"] in FINAL:
                return s
            return None

        session = wait_for(f"{dst.label} to finish receiving", done, 60)
        if session["state"] != "done":
            return False, f"receiver says {session['state']} {session.get('reason') or ''}".strip()
        saved = session["files"][0].get("saved_name") or name
        self.received.append((dst, saved))
        got_size, got_digest = download_sha256(dst.base, f"/files/{quote(saved)}")
        if (got_size, got_digest) != (size, digest):
            return False, f"content differs ({got_size} of {size} bytes)"
        if dst.kind == "android" and self.adb:
            listing = self.adb.shell(dst.serial, f"ls -l '{ANDROID_FOLDER}/{saved}' 2>&1")
            if str(size) not in listing:
                return False, f"not in {ANDROID_FOLDER}: {listing.strip()[:120]}"
        if not self.args.keep:  # don't fill the phone with gigabytes of test files
            request(dst.base, "DELETE", f"/api/files/{quote(saved)}")
            self.received.remove((dst, saved))
        return True, ""

    def send(
        self,
        src: Node,
        dst: Node,
        size: int,
        what: str,
        *,
        prompt: bool = True,
    ) -> tuple[bool, str]:
        """One file src → dst; True if it arrived byte for byte. Checks the prompt."""
        path, digest = self.test_file(size)
        name = self.file_name(src, dst, what, ".jpg" if what == "photo" else ".bin")
        try:
            job_id = self.offer(src, [dst], name, size)
            session = self.answer(dst, name, accept=True)
            asked = session["state"] == "pending"
            self.wait_answered(src, job_id, [dst])
            started = time.monotonic()
            result = self.upload(src, job_id, path)
            seconds = time.monotonic() - started
            if not result.get("targets", {}).get(dst.id, {}).get("ok"):
                return False, f"sender reports failure: {result}"
            ok, why = self.verify_received(dst, name, size, digest)
            if not ok:
                return False, why
            speed = f"{size / MB / max(seconds, 0.001):.1f} MB/s"
            if asked != (prompt and not dst.auto_accept):
                expected = "an Accept prompt" if prompt else "no prompt (paired)"
                return False, f"arrived ({speed}) but expected {expected}"
            return True, speed
        except (CheckError, OSError) as exc:
            return False, str(exc)[:300]

    # -------------------------------------------------------------- stages

    def setup(self) -> None:
        step("Setup")
        args = self.args
        self.pc = Node(
            label=f"{local_platform()} computer",
            base=find_computer_app(args.pc_url),
            kind="computer",
            expected_form="computer",
            expected_platform=local_platform(),
        )
        self.pc.lan_port = urlsplit(self.pc.base).port or 80
        try:
            health = expect(self.pc.base, "GET", "/api/health", timeout=5)
            self.pc.refresh()
        except (CheckError, OSError) as exc:
            raise CheckError(
                f"No Open Transfer app answered at {self.pc.base} ({exc}). Open the app on this "
                "computer first, or pass --pc-url."
            ) from exc
        self.pc.label = f"{self.pc.name} ({local_platform()})"
        self.report.env.append(
            f"**{self.pc.name}** — this computer ({local_platform()}), app {health.get('version')}"
        )

        if args.no_adb:
            self.pc.lan_host = "127.0.0.1"
            for i, url in enumerate(args.node):
                node = Node(label=f"node {i + 1}", base=url.rstrip("/"), kind="computer")
                node.refresh()
                node.label, node.lan_host = node.name, "127.0.0.1"
                node.lan_port = urlsplit(url).port or 80
                self.androids.append(node)
                self.report.env.append(f"**{node.name}** — {url}")
            return

        exe = find_adb(args.adb)
        if not exe:
            raise CheckError("adb not found. Install Android platform-tools or pass --adb.")
        self.adb = Adb(exe)
        serials = args.serial or self.adb.serials()
        if not serials:
            raise CheckError(
                "No Android device connected. Phone: Settings → Developer options → Wireless "
                "debugging → Pair device with pairing code, then on this computer run\n"
                "  adb pair <ip>:<pairing port>   and   adb connect <ip>:<port>"
            )
        apk = apk_from(Path(args.apk), self.tmp) if args.apk else None
        for i, serial in enumerate(serials):
            node = android_node(self.adb, serial, i, self.report)
            if apk:
                print(f"  installing {apk.name} on {node.label}…", flush=True)
                if args.reinstall:
                    with contextlib.suppress(CheckError):
                        self.adb.run("uninstall", PKG, serial=serial)
                try:
                    self.adb.run("install", "-r", "-g", str(apk), serial=serial, timeout=300)
                except CheckError as exc:
                    if "INCOMPATIBLE" in str(exc) or "SIGNATURES" in str(exc):
                        raise CheckError(
                            f"{node.label} has Open Transfer from another build (signed "
                            "differently). Run again with --reinstall — that removes the app "
                            "and its settings first; files in Download/Open Transfer stay."
                        ) from exc
                    raise
                with contextlib.suppress(CheckError):  # no such permission before Android 13
                    self.adb.shell(serial, f"pm grant {PKG} android.permission.POST_NOTIFICATIONS")
            print(f"  launching on {node.label} (first start unpacks Python)…", flush=True)
            launch_android(self.adb, node)
            node.refresh()
            version = expect(node.base, "GET", "/api/health").get("version")
            self.report.env[-1] += f", app {version}"
            if not node.lan_host:
                raise CheckError(f"{node.label} has no Wi-Fi address (wlan0) — is Wi-Fi on?")
            self.androids.append(node)
        self.pc.lan_host = lan_ip_towards(self.androids[0].lan_host)
        self.report.env[0] += f", Wi-Fi {self.pc.lan_host}"
        self.report.add("0.1", "App installed and running on every Android device", True)

    def unpair_all(self) -> None:
        for a in self.nodes:
            for b in self.nodes:
                if a is not b and a.paired_with(b):
                    request(a.base, "DELETE", f"/api/pairs/{b.id}")

    def discovery(self) -> None:
        step("1. Discovery and identity")
        for node in self.nodes:
            others = [o for o in self.nodes if o is not node]
            try:
                wait_for(
                    f"{node.label} to list the others",
                    functools.partial(sees_all, node, others),
                    self.args.discovery_timeout,
                )
                self.report.add("1.1", f"{node.label} lists every other device on its own", True)
            except CheckError:
                missing = [o.label for o in others if not node.sees(o)]
                why = (
                    "discovery is on but they weren't heard: multicast may be blocked "
                    "(guest Wi-Fi, router isolation, firewall)"
                    if node.state().get("discovery")
                    else "discovery couldn't start on this device"
                )
                self.report.add(
                    "1.1",
                    f"{node.label} lists every other device on its own",
                    False,
                    f"missing {', '.join(missing)} after {self.args.discovery_timeout:.0f}s; "
                    f"{why}. Added them by address to go on",
                )
                for o in others:
                    if not node.sees(o):
                        expect(node.base, "POST", "/api/devices", {"address": o.address})
                wait_for(
                    "devices added by address",
                    functools.partial(sees_all, node, others),
                    20,
                )
        for node in self.nodes:
            problems = []
            for o in self.nodes:
                if o is node:
                    continue
                d = o.device(node) or {}
                if d.get("name") != node.name:
                    problems.append(f"{o.label} shows name {d.get('name')!r}")
                if node.expected_form and d.get("form") != node.expected_form:
                    problems.append(f"{o.label} shows form {d.get('form')!r}")
                if node.expected_platform and d.get("platform") != node.expected_platform:
                    problems.append(f"{o.label} shows platform {d.get('platform')!r}")
            self.report.add(
                "1.2",
                f"{node.label} shown with the right name, "
                f"{node.expected_form or 'form'} and {node.expected_platform or 'platform'}",
                not problems,
                "; ".join(problems),
            )

        node = self.androids[0]
        new_name = f"{node.name[:28]} (check)"
        try:
            expect(node.base, "POST", "/api/me", {"name": new_name})
            others = [o for o in self.nodes if o is not node]

            def renamed() -> bool:
                return all((o.device(node) or {}).get("name") == new_name for o in others)

            started = time.monotonic()
            wait_for("the new name to show", renamed, 20)
            self.report.add(
                "1.3",
                "Rename shows on the other devices",
                True,
                f"{time.monotonic() - started:.0f}s",
            )
        except CheckError as exc:
            self.report.add("1.3", "Rename shows on the other devices", False, str(exc))
        finally:
            request(node.base, "POST", "/api/me", {"name": node.name})

        if self.adb and node.kind == "android":
            try:
                stop_android(self.adb, node)
                gone = lambda: not self.computer.sees(node)  # noqa: E731
                started = time.monotonic()
                wait_for("the closed app to drop off", gone, 45)
                self.report.add(
                    "1.4",
                    f"{node.label} closed → disappears from the computer",
                    True,
                    f"{time.monotonic() - started:.0f}s",
                )
            except CheckError as exc:
                self.report.add("1.4", f"{node.label} closed → disappears", False, str(exc))
            try:
                launch_android(self.adb, node)
                started = time.monotonic()
                wait_for("it to come back", lambda: self.computer.sees(node), 45)
                self.report.add(
                    "1.5",
                    f"{node.label} reopened → back without restarting others",
                    True,
                    f"{time.monotonic() - started:.0f}s",
                )
            except CheckError as exc:
                self.report.add("1.5", f"{node.label} reopened → comes back", False, str(exc))
                expect(self.computer.base, "POST", "/api/devices", {"address": node.address})

    def directions(self) -> None:
        step("3. Transfers — every direction (unpaired: each one asks first)")
        big = int(self.args.big_mb * MB)
        for src in self.nodes:
            for dst in self.nodes:
                if src is dst:
                    continue
                for what, size in (("photo", 3 * MB), (f"{self.args.big_mb:g}MB", big)):
                    ok, note = self.send(src, dst, size, what if what == "photo" else "large")
                    self.report.add("3", f"{src.label} → {dst.label}: {what}", ok, note)

    def groups(self) -> None:
        step("4. One-to-one, one-to-many, everyone")
        pc = self.computer
        if len(self.androids) < 2:
            for ref in ("4.1", "4.2", "4.4", "4.6", "4.7"):
                self.report.add(ref, "one-to-many checks", None, "connect a second Android device")
            return
        a, b = self.androids[0], self.androids[1]
        size = 3 * MB
        path, digest = self.test_file(size)

        # 4.1 only the chosen device is asked
        name = self.file_name(pc, a, "only", ".bin")
        try:
            job = self.offer(pc, [a], name, size)
            self.answer(a, name, accept=True)
            time.sleep(4)
            leaked = self.incoming(b, name) is not None
            self.wait_answered(pc, job, [a])
            self.upload(pc, job, path)
            ok, why = self.verify_received(a, name, size, digest)
            self.report.add(
                "4.1",
                f"Send to {a.label} only → {b.label} gets nothing",
                ok and not leaked,
                why or (f"{b.label} was asked too" if leaked else ""),
            )
        except (CheckError, OSError) as exc:
            self.report.add("4.1", "Send to one device only", False, str(exc))

        # 4.2 two devices, each with its own progress
        name = self.file_name(pc, a, "two", ".bin")
        try:
            job = self.offer(pc, [a, b], name, size)
            for t in (a, b):
                self.answer(t, name, accept=True)
            self.wait_answered(pc, job, [a, b])
            result = self.upload(pc, job, path)
            notes = []
            for t in (a, b):
                ok, why = self.verify_received(t, name, size, digest)
                sent = (self.target(pc, job, t) or {}).get("sent")
                if not ok:
                    notes.append(f"{t.label}: {why}")
                elif sent != size:
                    notes.append(f"{t.label}: progress shows {sent} of {size}")
            if not all(v.get("ok") for v in result.get("targets", {}).values()):
                notes.append(f"sender: {result}")
            self.report.add(
                "4.2",
                "Send to two devices → both get it, each with progress",
                not notes,
                "; ".join(notes),
            )
        except (CheckError, OSError) as exc:
            self.report.add("4.2", "Send to two devices", False, str(exc))

        # 4.4 everyone; one declines, the others still get it
        everyone = self.androids
        name = self.file_name(pc, a, "everyone", ".bin")
        try:
            job = self.offer(pc, everyone, name, size)
            for t in everyone:
                self.answer(t, name, accept=t is not b)
            states = self.wait_answered(pc, job, everyone)
            self.upload(pc, job, path)
            notes = [f"{b.label} state {states[b.id]}"] if states[b.id] != "declined" else []
            for t in everyone:
                if t is b:
                    continue
                ok, why = self.verify_received(t, name, size, digest)
                if not ok:
                    notes.append(f"{t.label}: {why}")
            others = [
                d["name"]
                for d in pc.state()["devices"]
                if d.get("online") and d["id"] not in {n.id for n in everyone}
            ]
            extra = (
                f" (also nearby, not driven by this script: {', '.join(others)})" if others else ""
            )
            self.report.add(
                "4.4",
                f"Send to every device, {b.label} declines → the others still get it",
                not notes,
                "; ".join(notes) + extra,
            )
        except (CheckError, OSError) as exc:
            self.report.add("4.4", "Send to everyone with one decline", False, str(exc))

        # 4.7 "Send now": one accepts, the other never answers. The button withdraws
        # the offers nobody answered, then the file goes to the ones who accepted.
        name = self.file_name(pc, a, "sendnow", ".bin")
        try:
            job = self.offer(pc, [a, b], name, size)
            self.answer(a, name, accept=True)
            wait_for(f"{b.label} to be asked", lambda: self.incoming(b, name), 20)
            wait_for(
                f"{pc.label} to see {a.label} accept",
                lambda: (self.target(pc, job, a) or {}).get("state") == "accepted",
                20,
            )
            expect(pc.base, "DELETE", f"/api/send/{job}/targets/{b.id}")
            withdrawn = wait_for(
                f"the offer on {b.label} to be withdrawn",
                lambda: (self.incoming(b, name) or {}).get("state") == "canceled",
                20,
            )
            self.upload(pc, job, path)
            ok, why = self.verify_received(a, name, size, digest)
            self.report.add(
                "4.7",
                f"Send now while {b.label} hasn't answered → {a.label} gets it, "
                f"{b.label}'s prompt goes away",
                ok and withdrawn,
                why,
            )
        except (CheckError, OSError) as exc:
            self.report.add("4.7", "Send now", False, str(exc))

        # 4.6 a receiver leaves mid-transfer
        if self.adb:
            big = max(int(self.args.big_mb * MB), 200 * MB)
            bpath, bdigest = self.test_file(big)
            name = self.file_name(pc, a, "leaves", ".bin")
            try:
                job = self.offer(pc, [a, b], name, big)
                for t in (a, b):
                    self.answer(t, name, accept=True)
                self.wait_answered(pc, job, [a, b])
                thread = background(lambda: self.upload(pc, job, bpath))
                wait_for(
                    "bytes to flow",
                    lambda: ((self.target(pc, job, b) or {}).get("sent") or 0) > 5 * MB,
                    60,
                )
                stop_android(self.adb, b)
                thread.join(timeout=max(300, big / MB))
                ok, why = self.verify_received(a, name, big, bdigest)
                b_state = (self.target(pc, job, b) or {}).get("state")
                self.report.add(
                    "4.6",
                    f"{b.label} leaves mid-transfer → {a.label} still finishes; "
                    f"{b.label} shows failed",
                    ok and b_state == "failed",
                    why or ("" if b_state == "failed" else f"{b.label} shows {b_state}"),
                )
            except (CheckError, OSError) as exc:
                self.report.add("4.6", "A receiver leaves mid-transfer", False, str(exc))
            try:  # bring the stopped app back for the checks that follow
                launch_android(self.adb, b)
                wait_for(f"{pc.label} to see {b.label} again", functools.partial(pc.sees, b), 60)
            except CheckError as exc:
                self.report.add("4.6", f"{b.label} reopened after leaving", False, str(exc))

    def states(self) -> None:
        step("6. Declined, expired, canceled, too big")
        pc, a = self.computer, self.androids[0]
        size = 3 * MB
        name = self.file_name(pc, a, "decline", ".bin")
        try:
            job = self.offer(pc, [a], name, size)
            self.answer(a, name, accept=False)
            got = wait_for(
                "the decline", lambda: (self.target(pc, job, a) or {}).get("state") in FINAL, 20
            )
            state = (self.target(pc, job, a) or {}).get("state")
            self.report.add(
                "6.2", "Decline → sender sees Declined", got and state == "declined", f"{state}"
            )
        except CheckError as exc:
            self.report.add("6.2", "Decline → sender sees Declined", False, str(exc))

        if not self.args.skip_slow:
            name = self.file_name(pc, a, "expire", ".bin")
            try:
                job = self.offer(pc, [a], name, size)
                wait_for("the offer", lambda: self.incoming(a, name), 20)
                print("  waiting 2 minutes for the offer to expire…", flush=True)
                wait_for(
                    "the offer to expire",
                    lambda: (self.target(pc, job, a) or {}).get("state") in FINAL,
                    170,
                    every=5,
                )
                state = (self.target(pc, job, a) or {}).get("state")
                self.report.add(
                    "6.2", "No answer for 2 minutes → Didn't answer", state == "expired", f"{state}"
                )
            except CheckError as exc:
                self.report.add("6.2", "No answer for 2 minutes → expired", False, str(exc))

        big = max(int(self.args.big_mb * MB), 200 * MB)
        bpath, _ = self.test_file(big)
        name = self.file_name(pc, a, "cancel", ".bin")
        try:
            job = self.offer(pc, [a], name, big)
            self.answer(a, name, accept=True)
            self.wait_answered(pc, job, [a])
            thread = background(lambda: self.upload(pc, job, bpath))
            wait_for(
                "bytes to flow",
                lambda: ((self.target(pc, job, a) or {}).get("sent") or 0) > MB,
                60,
                every=0.05,
            )
            expect(pc.base, "DELETE", f"/api/send/{job}")
            thread.join(timeout=60)
            session = wait_for(
                "the receiver to stop",
                lambda: (s := self.incoming(a, name)) and s["state"] in FINAL and s,
                30,
            )
            if session["state"] == "done":  # the whole file got there before the cancel
                saved = session["files"][0].get("saved_name") or name
                request(a.base, "DELETE", f"/api/files/{quote(saved)}")
                raise TooFast
            listed = {f["name"] for f in expect(a.base, "GET", "/api/files").get("files", [])}
            leftovers = [n for n in listed if n.startswith(name.rsplit(".", 1)[0])]
            if self.adb:
                out = self.adb.shell(a.serial, f"ls '{ANDROID_FOLDER}' 2>&1")
                leftovers += [line for line in out.splitlines() if name.rsplit(".", 1)[0] in line]
            ok = session["state"] in {"canceled", "failed"} and not leftovers
            self.report.add(
                "6.3",
                "Cancel mid-transfer → receiver stops, no half file",
                ok,
                f"receiver {session['state']}"
                + (f"; left behind {leftovers}" if leftovers else ""),
            )
        except TooFast:
            self.report.add(
                "6.3", "Cancel mid-transfer", None, "the file finished first; try a larger --big-mb"
            )
        except (CheckError, OSError) as exc:
            self.report.add("6.3", "Cancel mid-transfer", False, str(exc))

        name = self.file_name(pc, a, "toobig", ".bin")
        try:
            job = self.offer(pc, [a], name, 10**15)
            got = wait_for(
                "a refusal",
                lambda: (t := self.target(pc, job, a)) and t["state"] in FINAL and t,
                30,
            )
            asked = (self.incoming(a, name) or {}).get("state") == "pending"
            self.report.add(
                "6.4",
                "Too big for the free space → refused up front with a reason",
                got["state"] == "declined" and bool(got.get("reason")) and not asked,
                f"{got['state']}: {got.get('reason')}",
            )
        except CheckError as exc:
            self.report.add("6.4", "Too big → refused up front", False, str(exc))

    def pair(self, enters: Node, shows: Node, *, address: bool) -> tuple[bool, str]:
        code = shows.state()["pairing"]["code"]
        body: dict[str, Any] = {"code": code}
        if address:
            body["address"] = shows.address
        status, data = request(enters.base, "POST", "/api/pair", body, timeout=30)
        if status != 200:
            return False, f"{status} {data}"
        try:
            wait_for(
                "both sides to show paired",
                lambda: enters.paired_with(shows) and shows.paired_with(enters),
                15,
            )
        except CheckError as exc:
            return False, str(exc)
        return True, ""

    def unpair(self, a: Node, b: Node) -> None:
        request(a.base, "DELETE", f"/api/pairs/{b.id}")
        request(b.base, "DELETE", f"/api/pairs/{a.id}")

    def pairing(self) -> None:
        step("2. Pairing (code, both directions; the request a QR scan makes)")
        pc = self.computer
        state = pc.state()
        code = state["pairing"]["code"]
        status, svg = request(pc.base, "GET", "/api/pair/qr.svg")
        self.report.add(
            "2.1",
            f"{pc.label} shows a 6-digit code and a QR code",
            bool(re.fullmatch(r"\d{6}", code)) and status == 200 and "<svg" in str(svg),
        )
        for a in self.androids:
            wrong = f"{(int(code) + 1) % 1_000_000:06d}"
            body = {"code": wrong, "address": pc.address}
            status, _ = request(a.base, "POST", "/api/pair", body, timeout=30)
            if status == 429:  # still blocked by a run less than a minute ago
                print("  pairing is rate-limited from an earlier run; waiting a minute…")
                time.sleep(62)
                status, _ = request(a.base, "POST", "/api/pair", body, timeout=30)
            self.report.add(
                "2.4", f"{a.label}: a wrong code is refused", status == 403, f"{status}"
            )

            ok, why = self.pair(a, pc, address=False)
            if not ok:
                self.unpair(a, pc)
                ok2, why2 = self.pair(a, pc, address=True)
                why = f"by code alone: {why}; with the address: {'ok' if ok2 else why2}"
            self.report.add("2.2", f"{a.label} enters {pc.label}'s code → paired on both", ok, why)
            for src, dst in ((pc, a), (a, pc)):
                ok, note = self.send(src, dst, 3 * MB, "paired", prompt=False)
                self.report.add(
                    "2.5", f"Paired: {src.label} → {dst.label} arrives without asking", ok, note
                )

            self.unpair(a, pc)
            ok, why = self.pair(pc, a, address=False)
            if not ok:
                self.unpair(a, pc)
                ok2, why2 = self.pair(pc, a, address=True)
                why = f"by code alone: {why}; with the address: {'ok' if ok2 else why2}"
            self.report.add("2.6", f"Other way round: {pc.label} enters {a.label}'s code", ok, why)

            self.unpair(a, pc)
            ok, why = self.pair(a, pc, address=True)
            self.report.add(
                "2.3",
                f"{a.label} pairs from what {pc.label}'s QR code holds (code + address)",
                ok,
                why,
            )

    def rate_limit(self) -> None:
        pc, a = self.computer, self.androids[0]
        code = pc.state()["pairing"]["code"]
        wrong = f"{(int(code) + 7) % 1_000_000:06d}"
        statuses = [
            request(
                a.base, "POST", "/api/pair", {"code": wrong, "address": pc.address}, timeout=30
            )[0]
            for _ in range(6)
        ]
        self.report.add(
            "2.4",
            "5 wrong codes in a minute → rate-limited",
            429 in statuses,
            " ".join(map(str, statuses)) + " (pairing from that device is blocked for a minute)",
        )

    def cleanup(self) -> None:
        if self.args.keep:
            return
        for node, name in self.received:
            request(node.base, "DELETE", f"/api/files/{quote(name)}")

    def manual_items(self) -> None:
        r = self.report
        r.manual(
            "1.4",
            "Close the computer's app → it disappears on the phone/tablet",
            "Quit the app; watch the phone",
        )
        r.manual(
            "2.3",
            "Tablet app: Add device → Scan its QR code → camera scan of the computer's QR → paired",
            "The script tested what happens after the scan; this checks the camera",
        )
        r.manual("2.5", "🔗 badge shows on paired devices", "Look at the device tiles")
        r.manual(
            "3",
            "A received photo opens correctly; the receiver saw an Accept / Decline prompt on screen",
            "Send a real photo from the Gallery / Photos",
        )
        r.manual(
            "3",
            "Windows ↔ Mac, both directions",
            "Needs both computers' screens; send a photo and a 1 GB file each way",
        )
        r.manual(
            "4.3",
            "Select all → Send to N asks “Send to everyone nearby?”; Cancel sends nothing",
            "On the computer's screen",
        )
        r.manual("4.5", "Drop a file onto a device tile → only that device", "Desktop app")
        r.manual(
            "4.6",
            "Failed delivery shows Retry and Retry works",
            "Repeat 4.6 by hand: Wi-Fi off on one device mid-transfer",
        )
        r.manual(
            "5.1–5.5",
            "Browsers without the app (iPhone/iPad, direct between browsers, home-screen app, privacy)",
            "docs/device-testing.md section 5",
        )
        r.manual("6.1", "Wi-Fi off → Reconnecting…; on → Reconnected", "Any device")
        r.manual(
            "6.5",
            "Android: switch apps during a transfer → keeps going, notification shown",
            "Phone and tablet",
        )
        r.manual(
            "6.6",
            "Android: offer while the app is in the background → notification",
            "Phone and tablet",
        )
        r.manual(
            "—",
            "First launch: Windows firewall (Private networks) / macOS Local Network prompt appeared and allowing it worked",
            "Computers",
        )

    def logs(self, stamp: str) -> list[Path]:
        saved = []
        if self.adb:
            for node in self.androids:
                try:
                    out = self.adb.run("logcat", "-d", serial=node.serial, timeout=60)
                except CheckError:
                    continue
                keep = [
                    line
                    for line in out.splitlines()
                    if re.search(
                        r"python|open_transfer|opentransfer|chaquopy|AndroidRuntime", line, re.I
                    )
                ]
                path = Path(
                    f"device-check-{stamp}-{re.sub(r'[^A-Za-z0-9]+', '_', node.serial)}-logcat.txt"
                )
                path.write_text("\n".join(keep[-3000:]) + "\n", encoding="utf-8")
                saved.append(path)
        return saved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apk", help="the .apk (or CI artifact .zip) to install first")
    parser.add_argument("--pc-url", help="this computer's app (default: found on 127.0.0.1)")
    parser.add_argument("--adb", help="path to adb")
    parser.add_argument("--serial", action="append", help="only these adb devices (repeatable)")
    parser.add_argument("--big-mb", type=float, default=1024, help="size of the large file (MB)")
    parser.add_argument(
        "--quick", action="store_true", help="100 MB large file, skip the 2-minute expiry"
    )
    parser.add_argument("--skip-slow", action="store_true", help="skip the 2-minute expiry check")
    parser.add_argument(
        "--reinstall",
        action="store_true",
        help="uninstall the app first (needed when the installed one is signed differently)",
    )
    parser.add_argument("--keep", action="store_true", help="keep the received test files")
    parser.add_argument("--discovery-timeout", type=float, default=20)
    parser.add_argument("--out", help="results file (default device-check-<time>.md)")
    parser.add_argument("--no-adb", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--node", action="append", default=[], help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # ✅ / ❌ on a Windows console
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    if args.quick:
        args.big_mb, args.skip_slow = min(args.big_mb, 100), True

    stamp = time.strftime("%Y%m%d-%H%M")
    report = Report()
    out = Path(args.out or f"device-check-{stamp}.md")
    with tempfile.TemporaryDirectory(prefix="ot-device-check-") as tmp:
        checker = Checker(args, report, Path(tmp))
        try:
            checker.setup()
            checker.unpair_all()
            checker.discovery()
            checker.directions()
            checker.groups()
            checker.states()
            checker.pairing()
            checker.rate_limit()
        except (CheckError, OSError, subprocess.TimeoutExpired) as exc:
            report.add("!", "The check stopped early", False, str(exc)[:500])
        except KeyboardInterrupt:
            report.add("!", "Stopped by you", None)
        finally:
            with contextlib.suppress(CheckError, OSError):
                checker.cleanup()
            checker.manual_items()
            out.write_text(report.markdown(), encoding="utf-8")
            logs = checker.logs(stamp) if report.failed else []
    print(f"\nResults: {out.resolve()}")
    for path in logs:
        print(f"Android log: {path.resolve()}")
    print(f"{len(report.failed)} failed." if report.failed else "Everything automated passed.")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
