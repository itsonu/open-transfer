"""Benchmark ot-file/1 (docs/encryption.md): encrypted vs plain transfers.

    python scripts/bench_encryption.py            # 256 MB, prints a Markdown table
    python scripts/bench_encryption.py --mb 1024

Two apps run in one process on 127.0.0.1, so the network is never the limit:
what's measured is the apps' own cost. "plain" is an unpaired pair of apps,
"encrypted" a paired one. Each run is its own process (clean peak memory).
Latency is the median time to send a 1 KiB file once it's accepted.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

BLOCK = os.urandom(1 << 20)
os.environ.setdefault("OPEN_TRANSFER_KEYSTORE", "file")  # leave the real keychain alone


def peak_mb() -> int:
    if sys.platform == "win32":
        import ctypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("faults", ctypes.c_ulong)] + [
                (f"f{i}", ctypes.c_size_t)
                for i in range(8)  # f0 = PeakWorkingSetSize
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        process = ctypes.windll.kernel32.GetCurrentProcess()  # type: ignore[attr-defined]
        ctypes.windll.psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb)  # type: ignore[attr-defined]
        return round(counters.f0 / (1 << 20))
    import resource

    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(rss / (1 << 20 if sys.platform == "darwin" else 1 << 10))


class Payload:
    """``size`` bytes, streamed: nothing is held in memory."""

    def __init__(self, size: int) -> None:
        self.left = size

    def read(self, n: int = -1) -> bytes:
        n = min(self.left, len(BLOCK) if n < 0 else min(n, len(BLOCK)))
        self.left -= n
        return BLOCK[:n]


def call(
    port: int, method: str, path: str, body: Any = None, data: Any = None, length: int = 0
) -> Any:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=600)
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    elif data is not None:
        headers["Content-Length"] = str(length)
    conn.request(method, path, body=data, headers=headers)
    res = conn.getresponse()
    raw = res.read()
    conn.close()
    return json.loads(raw) if raw else None


def wait(check: Any, timeout: float = 30) -> Any:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if got := check():
            return got
        time.sleep(0.05)
    raise TimeoutError


def one(mode: str, mb: int, streams: int) -> dict[str, Any]:
    from open_transfer.config import Config
    from open_transfer.node import Node

    with tempfile.TemporaryDirectory() as tmp:
        nodes = []
        for name in ("Alpha", "Bravo"):
            node = Node(
                Config(
                    storage_dir=Path(tmp) / name,
                    port=0,
                    discovery=False,
                    device_name=name,
                    reserve_disk_bytes=0,
                    auto_accept=True,
                )
            )
            nodes.append((node, node.start()))
        (a, pa), (b, pb) = nodes
        try:
            if mode == "encrypted":
                code = call(pb, "POST", "/api/pair/open")["code"]
                call(pa, "POST", "/api/pair", {"code": code, "address": f"127.0.0.1:{pb}"})
                wait(lambda: a.mesh.trust.get(b.mesh.id) and a.mesh.trust.get(b.mesh.id).enc)
            else:
                call(pa, "POST", "/api/devices", {"address": f"127.0.0.1:{pb}"})

            def small() -> float:
                job = call(
                    pa,
                    "POST",
                    "/api/send",
                    {"to": [b.mesh.id], "files": [{"name": "s", "size": 1024}]},
                )["job"]["id"]
                wait(lambda: any(
                    t["state"] == "accepted" for j in call(pa, "GET", "/api/state")["outgoing"]
                    if j["id"] == job for t in j["targets"]
                ))  # fmt: skip
                start = time.perf_counter()
                call(pa, "PUT", f"/api/send/{job}/files/0", data=BLOCK[:1024], length=1024)
                return time.perf_counter() - start

            latency = statistics.median(small() for _ in range(5))
            size = mb * 1024 * 1024 // streams
            jobs = [
                call(
                    pa,
                    "POST",
                    "/api/send",
                    {"to": [b.mesh.id], "files": [{"name": f"f{i}", "size": size}]},
                )["job"]["id"]
                for i in range(streams)
            ]

            def accepted() -> bool:
                state = call(pa, "GET", "/api/state")
                return all(
                    t["state"] == "accepted"
                    for j in state["outgoing"]
                    if j["id"] in jobs
                    for t in j["targets"]
                )

            wait(accepted)
            cpu, start = time.process_time(), time.perf_counter()
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(streams) as pool:
                results = list(
                    pool.map(
                        lambda j: call(
                            pa, "PUT", f"/api/send/{j}/files/0", data=Payload(size), length=size
                        ),
                        jobs,
                    )
                )
            seconds = time.perf_counter() - start
            if not all(t["ok"] for r in results for t in r["targets"].values()):
                raise SystemExit(f"a transfer failed: {results}")
            state = call(pa, "GET", "/api/state")
            encrypted = {t["encrypted"] for j in state["outgoing"] for t in j["targets"]}
            if encrypted != {mode == "encrypted"}:
                raise SystemExit(f"expected {mode}, got encrypted={encrypted}")
            return {
                "mode": mode,
                "streams": streams,
                "mb": mb,
                "seconds": round(seconds, 2),
                "mb_s": round(mb / seconds),
                "cpu_s": round(time.process_time() - cpu, 2),
                "peak_mb": peak_mb(),
                "latency_ms": round(latency * 1000, 1),
            }
        finally:
            a.stop()
            b.stop()


def cipher_only(mb: int) -> dict[str, Any]:
    from open_transfer import filecrypt

    piece = 32 * filecrypt.RECORD
    size = mb * 1024 * 1024
    c = filecrypt.Cipher(os.urandom(32), size)
    data = BLOCK * 2
    start = time.perf_counter()
    for offset in range(0, size, piece):
        c.decrypt_range(offset, c.encrypt_range(offset, data[: min(piece, size - offset)]))
    return {"mb_s": round(mb / (time.perf_counter() - start))}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mb", type=int, default=256)
    parser.add_argument("--one", choices=["plain", "encrypted"])
    parser.add_argument("--streams", type=int, default=1)
    args = parser.parse_args()
    if args.one:
        print(json.dumps(one(args.one, args.mb, args.streams)))
        return
    rows = []
    for streams in (1, 4):
        for mode in ("plain", "encrypted"):
            out = subprocess.run(  # noqa: S603 - this script, by its own path
                [sys.executable, __file__, "--one", mode, "--mb", str(args.mb), "--streams", str(streams)],
                capture_output=True, text=True, check=True,
            )  # fmt: skip
            rows.append(json.loads(out.stdout.strip().splitlines()[-1]))
    print(f"AES-256-GCM alone (encrypt + decrypt, one core): {cipher_only(args.mb)['mb_s']} MB/s\n")
    print("| Mode | Streams | Size | Time | Throughput | CPU | Peak memory | 1 KiB latency |")
    print("|---|---|---|---|---|---|---|---|")
    for r in rows:
        print(
            f"| {r['mode']} | {r['streams']} | {r['mb']} MB | {r['seconds']} s | {r['mb_s']} MB/s"
            f" | {r['cpu_s']} s | {r['peak_mb']} MB | {r['latency_ms']} ms |"
        )


if __name__ == "__main__":
    main()
