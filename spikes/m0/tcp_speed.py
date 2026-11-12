"""M0 spike (throwaway): raw TCP speed over whatever link two devices share.

    python3 tcp_speed.py sink              listen on :47900, report what arrives
    python3 tcp_speed.py send HOST [MB]    stream MB (default 100) to HOST:47900
    python3 tcp_speed.py gateway           this machine's default gateway (on a hotspot: the host)

Same protocol as the Android spike app and windows/m0.ps1: the sender streams bytes
and closes; the sink reports the count. Standard library only.
"""

import socket
import subprocess
import sys
import time

PORT = 47900
CHUNK = b"." * (256 * 1024)


def sink() -> None:
    with socket.create_server(("", PORT)) as server:
        print(f"TCP sink listening on :{PORT}", flush=True)
        while True:
            conn, (peer, _) = server.accept()
            with conn:
                started, total = time.monotonic(), 0
                while data := conn.recv(len(CHUNK)):
                    total += len(data)
                secs = max(time.monotonic() - started, 1e-3)
                print(f"RECEIVED {total // 2**20} MB from {peer} in {secs:.1f} s = {total / 2**20 / secs:.1f} MB/s", flush=True)


def send(host: str, mb: int) -> None:
    with socket.create_connection((host, PORT), timeout=5) as conn:
        started, left = time.monotonic(), mb * 2**20
        while left > 0:
            conn.sendall(CHUNK[: min(left, len(CHUNK))])
            left -= len(CHUNK)
        conn.shutdown(socket.SHUT_WR)
    secs = max(time.monotonic() - started, 1e-3)
    print(f"SENT {mb} MB to {host} in {secs:.1f} s = {mb / secs:.1f} MB/s")


def gateway() -> str:
    if sys.platform == "darwin":
        out = subprocess.run(["route", "-n", "get", "default"], capture_output=True, text=True).stdout
        return next((line.split()[-1] for line in out.splitlines() if "gateway:" in line), "")
    out = subprocess.run(["ip", "-4", "route", "show", "default"], capture_output=True, text=True).stdout
    return out.split()[2] if out.split() else ""


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "sink":
        sink()
    elif cmd == "send" and len(sys.argv) > 2:
        send(sys.argv[2] if sys.argv[2] != "gateway" else gateway(), int(sys.argv[3]) if len(sys.argv) > 3 else 100)
    elif cmd == "gateway":
        print(gateway())
    else:
        print(__doc__)
