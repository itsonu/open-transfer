"""End-to-end smoke test: a real transfer from the Android app (in the emulator) to an
Open Transfer node running on the CI machine.

    adb forward tcp:5000 tcp:5000      # the phone's server, as 127.0.0.1:5000 here
    python android/ci/smoke_transfer.py

Requests that arrive through ``adb forward`` reach the phone from its own loopback
address, so they count as the phone's owner (who may add devices, send, …). From inside
the emulator the CI machine is 10.0.2.2. The emulator's NAT means the runner can't open
connections to the phone, so the test goes phone → runner:

1. start a node on the runner (port chosen by the OS, listening on 0.0.0.0);
2. ask the phone to add it by address (``POST /api/devices``);
3. ask the phone to send ``hello.txt`` to it (``POST /api/send``);
4. accept on the runner (``POST /api/incoming/<id>/accept``);
5. wait until the phone sees "accepted", upload the bytes to the phone
   (``PUT /api/send/<job>/files/0``), which streams them on to the runner;
6. check the runner received exactly those bytes;
7. pair the phone with the runner (the phone enters the runner's code) and send
   again: the file must arrive encrypted (ot-file/1, docs/encryption.md);
8. the phone keeps its pairing keys in the Android Keystore (docs/encryption.md);
9. restart the app: the pairing survives (its key unwraps), a send is still encrypted;
10. time a plain and an encrypted 32 MB send (emulator numbers, not a phone's).

Standard library only (plus the ``open_transfer`` package itself).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

PHONE = os.environ.get("PHONE_URL", "http://127.0.0.1:5000")
HOST_FROM_EMULATOR = os.environ.get("HOST_FROM_EMULATOR", "10.0.2.2")
RUNNER_NAME = "CI runner"
FILE_NAME = "hello.txt"
FINAL_FAILURES = {"declined", "failed", "expired", "canceled"}
PKG = "io.github.itsonu.opentransfer"
BENCH = 32 * 1024 * 1024


class SmokeError(RuntimeError):
    pass


def call(
    base: str,
    method: str,
    path: str,
    body: Any = None,
    *,
    data: bytes | None = None,
    timeout: float = 30,
) -> tuple[int, Any]:
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    elif data is not None:
        headers["Content-Type"] = "application/octet-stream"
    request = urllib.request.Request(  # noqa: S310 - fixed http:// URLs
        base + path, data=data, method=method, headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        parsed = raw.decode(errors="replace")
    return status, parsed


def expect(
    base: str,
    method: str,
    path: str,
    body: Any = None,
    *,
    ok: tuple[int, ...] = (200,),
    quiet: bool = False,
    **kw: Any,
) -> Any:
    status, data = call(base, method, path, body, **kw)
    if not quiet or status not in ok:
        print(f"  {method} {base}{path} -> {status}")
    if status not in ok:
        raise SmokeError(f"{method} {path} returned {status}: {data!r}")
    return data


def wait_for(what: str, check, timeout: float = 60, every: float = 0.5) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = check()
        if last:
            return last
        time.sleep(every)
    raise SmokeError(f"Timed out after {timeout:.0f}s waiting for {what}")


def main() -> int:
    from open_transfer.config import Config
    from open_transfer.node import Node

    payload = b"Hello from the Open Transfer CI runner!\n" + os.urandom(1_000_000)

    with tempfile.TemporaryDirectory(prefix="ot-smoke-") as tmp:
        storage = Path(tmp) / "received"
        node = Node(
            Config(
                storage_dir=storage,
                port=0,
                discovery=False,
                device_name=RUNNER_NAME,
                reserve_disk_bytes=0,
            )
        )
        runner_port = node.start()
        runner = f"http://127.0.0.1:{runner_port}"
        print(f"Runner node '{RUNNER_NAME}' listening on 0.0.0.0:{runner_port}")
        try:
            print("1. Phone is up?")
            health = expect(PHONE, "GET", "/api/health")
            print(f"   {health}")
            phone_state = expect(PHONE, "GET", "/api/state")
            if phone_state.get("host", {}).get("platform") != "android":
                raise SmokeError(f"phone doesn't say it is Android: {phone_state.get('host')}")

            print("2. Phone adds the runner by address")
            address = f"{HOST_FROM_EMULATOR}:{runner_port}"
            added = expect(PHONE, "POST", "/api/devices", {"address": address})
            runner_id = added["device"]["id"]
            print(f"   added {added['device']}")

            def runner_listed() -> Any:
                state = expect(PHONE, "GET", "/api/state", quiet=True)
                for device in state.get("devices", []):
                    if device.get("name") == RUNNER_NAME and device.get("online"):
                        return device
                return None

            device = wait_for("the phone to list the runner as online", runner_listed, 30)
            if device["id"] != runner_id:
                raise SmokeError(f"device id mismatch: {device['id']} != {runner_id}")

            print("3. Phone offers hello.txt to the runner")
            created = expect(
                PHONE,
                "POST",
                "/api/send",
                {"to": [runner_id], "files": [{"name": FILE_NAME, "size": len(payload)}]},
                ok=(201,),
            )
            job_id = created["job"]["id"]
            print(f"   job {job_id}")

            print("4. Runner accepts")

            def runner_offer() -> Any:
                state = expect(runner, "GET", "/api/state", quiet=True)
                pending = [s for s in state.get("incoming", []) if s.get("state") == "pending"]
                return pending[0] if pending else None

            offer = wait_for("the offer to reach the runner", runner_offer, 30)
            print(f"   offer {offer['id']} from {offer['from'].get('name')!r}")
            expect(runner, "POST", f"/api/incoming/{offer['id']}/accept")

            print("5. Phone sees the acceptance, then streams the file")

            def target_state(job_id: str) -> Any:
                state = expect(PHONE, "GET", "/api/state", quiet=True)
                for job in state.get("outgoing", []):
                    if job.get("id") == job_id:
                        for target in job.get("targets", []):
                            if target.get("id") == runner_id:
                                if target.get("state") in FINAL_FAILURES:
                                    raise SmokeError(f"delivery ended early: {target}")
                                return target if target.get("state") == "accepted" else None
                return None

            def push(name: str, data: bytes, *, accept: bool) -> tuple[Any, float]:
                """Phone -> runner; returns the target and how long the bytes took."""
                job = expect(
                    PHONE,
                    "POST",
                    "/api/send",
                    {"to": [runner_id], "files": [{"name": name, "size": len(data)}]},
                    ok=(201,),
                )["job"]["id"]
                if accept:
                    offer = wait_for("the offer to reach the runner", runner_offer, 30)
                    expect(runner, "POST", f"/api/incoming/{offer['id']}/accept")
                target = wait_for("the phone to see 'accepted'", lambda: target_state(job), 30)
                start = time.monotonic()
                result = expect(PHONE, "PUT", f"/api/send/{job}/files/0", data=data, timeout=300)
                took = time.monotonic() - start
                if not result.get("targets", {}).get(runner_id, {}).get("ok"):
                    raise SmokeError(f"the phone reported a failed delivery: {result}")
                if (storage / name).read_bytes() != data:
                    raise SmokeError(f"{name} arrived different")
                return target, took

            wait_for("the phone to see 'accepted'", lambda: target_state(job_id), 30)
            result = expect(PHONE, "PUT", f"/api/send/{job_id}/files/0", data=payload, timeout=120)
            print(f"   {result}")
            outcome = result.get("targets", {}).get(runner_id, {})
            if not outcome.get("ok"):
                raise SmokeError(f"the phone reported a failed delivery: {result}")

            print("6. Runner has the identical file")
            received = storage / FILE_NAME
            wait_for(f"{received} to appear", received.is_file, 15)
            got = received.read_bytes()
            if got != payload:
                raise SmokeError(f"content differs: got {len(got)} bytes, sent {len(payload)}")
            print(f"OK: {len(payload)} bytes went phone -> runner intact.")
            blob = os.urandom(BENCH)
            _, plain_s = push("bench-plain.bin", blob, accept=True)

            print("7. Phone pairs with the runner and sends again, encrypted")
            code = expect(runner, "POST", "/api/pair/open")["code"]
            expect(PHONE, "POST", "/api/pair", {"code": code, "address": address})
            sealed = os.urandom(3_000_000)
            created = expect(
                PHONE,
                "POST",
                "/api/send",
                {"to": [runner_id], "files": [{"name": "sealed.bin", "size": len(sealed)}]},
                ok=(201,),
            )
            job_id = created["job"]["id"]
            # Paired: the runner accepts by itself.
            target = wait_for("the phone to see 'accepted'", lambda: target_state(job_id), 30)
            if target.get("encrypted") is not True:
                raise SmokeError(f"the paired transfer isn't encrypted: {target}")
            result = expect(PHONE, "PUT", f"/api/send/{job_id}/files/0", data=sealed, timeout=120)
            if not result.get("targets", {}).get(runner_id, {}).get("ok"):
                raise SmokeError(f"the phone reported a failed delivery: {result}")
            got = (storage / "sealed.bin").read_bytes()
            if got != sealed:
                raise SmokeError(f"content differs: got {len(got)} bytes, sent {len(sealed)}")
            state = expect(runner, "GET", "/api/state", quiet=True)
            done = [s for s in state.get("incoming", []) if s["files"][0]["name"] == "sealed.bin"]
            if (
                not done
                or done[0].get("encrypted") is not True
                or not done[0]["files"][0]["verified"]
            ):
                raise SmokeError(f"the runner didn't receive it encrypted and verified: {done}")
            print(f"OK: {len(sealed)} bytes went phone -> runner encrypted and verified.")

            print("8. The phone's pairing keys are in the Android Keystore")
            keys = expect(PHONE, "GET", "/api/state", quiet=True).get("keys", {})
            if keys.get("store") != "android-keystore" or keys.get("protected") is not True:
                raise SmokeError(f"pairing keys aren't in the Android Keystore: {keys}")
            print(f"   {keys}")

            print("9. Restart the app: the pairing must survive")
            subprocess.run(["adb", "shell", "am", "force-stop", PKG], check=True)  # noqa: S603, S607
            subprocess.run(  # noqa: S603
                ["adb", "shell", "am", "start", "-W", "-n", f"{PKG}/.MainActivity"],  # noqa: S607
                check=True,
                stdout=subprocess.DEVNULL,
            )

            def phone_up() -> bool:
                try:
                    return call(PHONE, "GET", "/api/health", timeout=3)[0] == 200
                except OSError:
                    return False

            wait_for("the phone's server to come back", phone_up, 120, every=1)
            expect(PHONE, "POST", "/api/devices", {"address": address})  # where to find it
            wait_for("the phone to list the runner again", runner_listed, 30)
            # Paired, so no accept: a lost key would leave the offer waiting for one.
            target, sealed_s = push("bench-sealed.bin", blob, accept=False)
            if target.get("encrypted") is not True:
                raise SmokeError(f"after a restart the transfer isn't encrypted: {target}")
            print("OK: after a restart the pairing held and the transfer was encrypted.")

            mb = BENCH / 1e6
            print("10. Emulator throughput, phone -> runner, 32 MB:")
            print(f"   plain     {mb / plain_s:6.1f} MB/s ({plain_s:.2f} s)")
            print(f"   encrypted {mb / sealed_s:6.1f} MB/s ({sealed_s:.2f} s)")
            return 0
        except (SmokeError, OSError, KeyError, TypeError) as exc:
            print(f"\nSMOKE TEST FAILED: {exc!r}", file=sys.stderr)
            for label, base in (("phone", PHONE), ("runner", runner)):
                try:
                    _, state = call(base, "GET", "/api/state", timeout=10)
                    print(f"--- {label} /api/state ---\n{json.dumps(state, indent=2)[:6000]}")
                except OSError as err:
                    print(f"--- {label} /api/state unavailable: {err!r}")
            return 1
        finally:
            node.stop()


if __name__ == "__main__":
    sys.exit(main())
