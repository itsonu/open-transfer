"""Real-device check for clipboard sync: this computer, your Android devices and your other computers.

Run it on a Mac or Windows PC with the Open Transfer **desktop app** open, and
your Android devices on adb (USB or wireless debugging):

    python scripts/clipboard_check.py                 # everything, asks you to help with other computers
    python scripts/clipboard_check.py --apk app.zip   # install this APK on the Android devices first
    python scripts/clipboard_check.py --no-input      # only what needs no help from you

It turns clipboard sync on (and pairs this computer with each Android device if
needed), then checks with real clipboards, over your Wi-Fi:

- this computer → Android: copy here (through this computer's real clipboard),
  and the phone has it — with the app in front, in the background, and with
  the screen off;
- Android → this computer: "Send to my devices" (the share sheet) and the Quick
  Settings tile path, and it lands on this computer's real clipboard;
- private-looking text is not synced;
- Android with the app swiped away from Recents (you swipe it);
- this computer ↔ your other computers (it shows a code to copy there, and asks
  you whether one arrived);
- Android ↔ your other computers, so with a Mac, a Windows PC and a phone all
  six directions are covered;
- one copy reaching every device at once;
- with the window closed (the background copy).

Results go to ``clipboard-check-<time>.md``. Standard library only (it reuses
scripts/device_check.py); needs ``adb`` for the Android checks.
"""

from __future__ import annotations

import argparse
import contextlib
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent))

from device_check import (
    PKG,
    Adb,
    CheckError,
    Node,
    Report,
    android_node,
    apk_from,
    expect,
    find_adb,
    find_computer_app,
    launch_android,
    wait_for,
)

SYNC_WAIT = 15.0


# ------------------------------------------------------------ this clipboard


class Clipboard(Protocol):
    def read(self) -> str:
        """This computer's clipboard text."""

    def write(self, text: str) -> None:
        """Copy ``text`` on this computer, as a person would."""


class OsClipboard:
    """This computer's real clipboard, through the system's own tools."""

    def __init__(self, platform: str = sys.platform) -> None:
        if platform == "darwin":
            self._read, self._write = ["pbpaste"], ["pbcopy"]
        elif platform == "win32":
            ps = ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
            self._read = [*ps, "Get-Clipboard -Raw"]
            self._write = [*ps, "$input | Set-Clipboard"]
        elif shutil.which("wl-copy"):
            self._read, self._write = ["wl-paste", "--no-newline"], ["wl-copy"]
        elif shutil.which("xclip"):
            self._read = ["xclip", "-selection", "clipboard", "-out"]
            self._write = ["xclip", "-selection", "clipboard", "-in"]
        else:
            raise CheckError("No clipboard tool here (pbcopy, PowerShell, wl-copy or xclip).")

    def read(self) -> str:
        done = subprocess.run(self._read, capture_output=True, timeout=10, check=False)  # noqa: S603
        return done.stdout.decode("utf-8", "replace").rstrip("\r\n")

    def write(self, text: str) -> None:
        subprocess.run(self._write, input=text.encode(), timeout=10, check=True)  # noqa: S603


WORDS = (
    "amber",
    "brook",
    "cedar",
    "delta",
    "ember",
    "fjord",
    "grove",
    "harbor",
    "island",
    "juniper",
    "kettle",
    "lagoon",
    "meadow",
    "nectar",
    "orchid",
    "pebble",
    "quartz",
    "ripple",
    "saffron",
    "tundra",
    "umber",
    "violet",
    "willow",
    "yonder",
    "zephyr",
)


def marker(used: set[int]) -> str:
    """Plain words (nothing that looks like a password or a token, which sync rightly holds
    back), with a length not used yet, so a clip can be recognised by its size alone."""
    while True:
        words = [secrets.choice(WORDS) for _ in range(4 + secrets.randbelow(5))]
        text = "Open Transfer check " + " ".join(words)
        if len(text) not in used:
            used.add(len(text))
            return text


# --------------------------------------------------------------- the checks


def sync_status(node: Node) -> dict[str, Any]:
    return expect(node.base, "GET", "/api/sync")  # type: ignore[no-any-return]


def received(node: Node, size: int, since: float, sender: str = "") -> dict[str, Any] | None:
    for entry in sync_status(node).get("recent", []):
        if (
            entry.get("direction") == "received"
            and entry.get("size") == size
            and entry.get("at", 0) >= since
            and (not sender or sender in entry.get("devices", []))
        ):
            return entry  # type: ignore[no-any-return]
    return None


def sent(node: Node, size: int, since: float) -> dict[str, Any] | None:
    for entry in sync_status(node).get("recent", []):
        if (
            entry.get("direction") == "sent"
            and entry.get("size") == size
            and entry.get("at", 0) >= since
        ):
            return entry  # type: ignore[no-any-return]
    return None


class Checker:
    def __init__(
        self,
        args: argparse.Namespace,
        report: Report,
        tmp: Path,
        *,
        clipboard: Clipboard | None = None,
        ask: Callable[[str], str] = input,
    ) -> None:
        self.args = args
        self.report = report
        self.tmp = tmp
        self.clipboard = clipboard
        self.ask = ask
        self.adb: Adb | None = None
        self.pc: Node | None = None
        self.androids: list[Node] = []
        self.sizes: set[int] = set()

    @property
    def here(self) -> Node:
        if self.pc is None:
            raise CheckError("This computer's app isn't set up yet.")
        return self.pc

    @property
    def clip(self) -> Clipboard:
        if self.clipboard is None:
            raise CheckError("No clipboard yet.")
        return self.clipboard

    @property
    def tool(self) -> Adb:
        if self.adb is None:
            raise CheckError("adb isn't set up.")
        return self.adb

    # ---------------------------------------------------------------- setup

    def setup(self) -> None:
        self.pc = Node("This computer", find_computer_app(self.args.pc_url), "computer")
        self.pc.refresh()
        self.report.env.append(f"**{self.pc.name}** (this computer) at {self.pc.base}")
        self._sync_on(self.pc)
        if self.clipboard is None:
            self.clipboard = OsClipboard()
        if not sync_status(self.pc).get("clipboard_access"):
            raise CheckError(
                "This computer's app can't reach the clipboard. Open the Open Transfer "
                "desktop app (not the command line), or start it with --clipboard."
            )
        if self.args.no_adb:
            return
        exe = find_adb(self.args.adb)
        if exe is None:
            self.report.add(
                "A0", "Android devices on adb", None, "adb not found: Android checks skipped"
            )
            return
        self.adb = Adb(exe)
        serials = [s for s in self.adb.serials() if not self.args.serial or s in self.args.serial]
        if not serials:
            self.report.add("A0", "Android devices on adb", None, "none connected (adb devices)")
        apk = apk_from(Path(self.args.apk), self.tmp) if self.args.apk else None
        for index, serial in enumerate(serials):
            node = android_node(self.adb, serial, index, self.report)
            if apk is not None:
                self.adb.run("install", "-r", "-g", str(apk), serial=serial, timeout=300)
            launch_android(self.adb, node)
            node.refresh()
            self._sync_on(node)
            self._pair(node)
            self.androids.append(node)

    def _sync_on(self, node: Node) -> None:
        if not sync_status(node).get("policy", {}).get("enabled"):
            expect(node.base, "PUT", "/api/sync/policy", {"enabled": True})
            print(f"  (turned clipboard sync on for {node.name})")

    def _pair(self, node: Node) -> None:
        if not self.here.paired_with(node):
            code = node.state()["pairing"]["code"]
            expect(self.here.base, "POST", "/api/pair", {"code": code, "address": node.address})
            print(f"  (paired {self.here.name} with {node.name})")
        pc = self.here

        def ready() -> bool:
            return any(
                d["id"] == node.id and d["online"] and d["syncing"]
                for d in sync_status(pc)["devices"]
            )

        wait_for(f"{node.name} to be ready to sync", ready, 30)

    # ---------------------------------------------------- computer ↔ Android

    def to_android(self, node: Node, ref: str, title: str) -> bool:
        text = marker(self.sizes)
        since = time.time() - 1
        self.clip.write(text)
        try:
            entry = wait_for(
                f"{node.name} to get the clip",
                lambda: received(node, len(text), since),
                SYNC_WAIT,
            )
        except CheckError as exc:
            delivered = sent(self.here, len(text), since)
            note = f"{exc}; this computer {'sent it' if delivered else 'never sent it (did its app notice the copy?)'}"
            return self.report.add(ref, title, False, note)
        return self.report.add(
            ref,
            title,
            bool(entry.get("applied")),
            "" if entry.get("applied") else "arrived but wasn't put on the clipboard",
        )

    def share_from(self, node: Node, text: str) -> None:
        """Share → Send to my devices on the phone (markers are plain words: no quoting)."""
        self.tool.shell(
            node.serial,
            f"am start -a android.intent.action.SEND -t text/plain --es android.intent.extra.TEXT '{text}' "
            f"-n {PKG}/.ClipboardSendActivity",
        )

    def from_android_share(self, node: Node) -> bool:
        text = marker(self.sizes)
        self.share_from(node, text)
        clipboard = self.clip
        try:
            wait_for("this computer's clipboard", lambda: clipboard.read() == text, SYNC_WAIT)
        except CheckError as exc:
            return self.report.add(
                "A2", f"{node.name} → this computer (Share → Send to my devices)", False, str(exc)
            )
        return self.report.add(
            "A2",
            f"{node.name} → this computer (Share → Send to my devices)",
            True,
            "on this computer's real clipboard",
        )

    def from_android_tile(self, node: Node) -> bool:
        """The tile reads the phone's own clipboard: proves the clip really landed there."""
        title = f"{node.name}'s clipboard holds what arrived (Send clipboard tile)"
        self.tool.shell(node.serial, "input keyevent KEYCODE_WAKEUP")
        if "mDreamingLockscreen=true" in self.tool.shell(
            node.serial, "dumpsys window | grep -i lockscreen || true"
        ):
            return self.report.add(
                "A3", title, None, "the phone is locked: unlock it and run again"
            )
        text = marker(self.sizes)
        since = time.time() - 1
        self.clip.write(text)
        try:
            wait_for(
                f"{node.name} to get the clip", lambda: received(node, len(text), since), SYNC_WAIT
            )
            since = time.time() - 1
            self.tool.shell(node.serial, f"am start -n {PKG}/.ClipboardSendActivity")
            pc = self.here
            wait_for(
                "the clip to come back from the phone's clipboard",
                lambda: received(pc, len(text), since, node.name),
                SYNC_WAIT,
            )
        except CheckError as exc:
            return self.report.add("A3", title, False, f"{exc} (is the phone unlocked?)")
        return self.report.add(
            "A3", title, True, "read back from the phone's clipboard by the tile's screen"
        )

    def android_background(self, node: Node) -> None:
        self.tool.shell(node.serial, "input keyevent KEYCODE_HOME")
        time.sleep(2)
        self.to_android(node, "A4", f"This computer → {node.name}, app in the background")
        self.tool.shell(node.serial, "input keyevent KEYCODE_SLEEP")
        time.sleep(5)
        self.to_android(node, "A5", f"This computer → {node.name}, screen off")
        self.tool.shell(node.serial, "input keyevent KEYCODE_WAKEUP")

    def swiped_away(self, node: Node) -> bool:
        """Swiped away from Recents, the service keeps the phone in the network."""
        title = f"This computer → {node.name}, app swiped away from Recents"
        if self.args.no_input:
            self.report.manual("A7", title, "Swipe the app away, copy here, paste on the phone.")
            return False
        self.ask(
            f"\n  On {node.name}, open Recents and swipe Open Transfer away, then press Enter… "
        )
        if not self.tool.shell(node.serial, f"pidof {PKG} || true").strip():
            return self.report.add(
                "A7",
                title,
                False,
                "the app stopped completely (Android or the phone's maker closed it)",
            )
        return self.to_android(node, "A7", title)

    def private_text(self, node: Node) -> bool:
        text = "password: " + secrets.token_hex(6)
        since = time.time() - 1
        self.clip.write(text)
        time.sleep(4)
        arrived = received(node, len(text), since)
        return self.report.add(
            "A6", f"Private-looking text isn't synced to {node.name}", arrived is None
        )

    # ------------------------------------------------------- other computers

    def other_computers(self) -> list[dict[str, Any]]:
        android_ids = {n.id for n in self.androids}
        return [
            d
            for d in sync_status(self.here)["devices"]
            if d["id"] not in android_ids
            and d.get("platform") in ("macos", "windows", "linux")
            and d["online"]
        ]

    def computers(self) -> None:
        others = self.other_computers()
        if not others:
            self.report.add(
                "C0",
                "Other computers",
                None,
                "none online with sync: open the app on them and turn sync on",
            )
            return
        for other in others:
            name = other["name"]
            if not other["syncing"]:
                self.report.add(
                    "C1",
                    f"This computer → {name}",
                    False,
                    "not syncing with it (paused, left out, or sync off there)",
                )
                continue
            text = marker(self.sizes)
            since = time.time() - 1
            self.clip.write(text)
            pc = self.here
            try:
                entry = wait_for(
                    f"{name} to accept it",
                    lambda pc=pc, text=text, since=since: sent(pc, len(text), since),
                    SYNC_WAIT,
                )
                ok = name in entry.get("devices", [])
            except CheckError:
                ok = False
            self.report.add("C1", f"This computer → {name} (its app accepted the clip)", ok)
            if self.args.no_input:
                self.report.manual(
                    "C2", f"Paste on {name}", f"Copy on this computer, paste on {name}."
                )
                self.report.manual("C3", f"{name} → this computer", f"Copy on {name}, paste here.")
                continue
            answer = (
                self.ask(f"\n  On {name}, paste anywhere. Do you see “{text}”? [y/n] ")
                .strip()
                .lower()
            )
            self.report.add("C2", f"Pasted on {name}", answer.startswith("y"))
            code = marker(self.sizes)
            print(f"\n  Now on {name}, copy this text (select it and copy):\n\n      {code}\n")
            clipboard = self.clip
            try:
                wait_for(
                    f"{code} to arrive",
                    lambda clipboard=clipboard, code=code: clipboard.read() == code,
                    90,
                    every=1,
                )
                self.report.add(
                    "C3", f"{name} → this computer", True, "on this computer's real clipboard"
                )
            except CheckError as exc:
                self.report.add("C3", f"{name} → this computer", False, str(exc))

    # ------------------------------------- Android ↔ the other computers

    def crossings(self) -> None:
        """The directions this computer isn't part of: each phone ↔ each other computer."""
        for node in self.androids:
            for other in self.other_computers():
                name = other["name"]
                self._android_to_computer(node, name)
                self._computer_to_android(node, name)

    def _android_to_computer(self, node: Node, name: str) -> None:
        title = f"{node.name} → {name}"
        text = marker(self.sizes)
        since = time.time() - 1
        self.share_from(node, text)
        try:
            entry = wait_for(
                f"{name} to accept it",
                lambda: sent(node, len(text), since),
                SYNC_WAIT,
            )
        except CheckError as exc:
            self.report.add("W1", title, False, f"{node.name} never sent it: {exc}")
            return
        if name not in entry.get("devices", []):
            self.report.add("W1", title, False, f"{name}'s app didn't accept it")
            return
        if self.args.no_input:
            self.report.add("W1", title, True, f"{name}'s app accepted it (paste there to be sure)")
            return
        answer = self.ask(f"\n  On {name}, paste anywhere. Do you see “{text}”? [y/n] ")
        self.report.add("W1", title, answer.strip().lower().startswith("y"), f"pasted on {name}")

    def _computer_to_android(self, node: Node, name: str) -> None:
        title = f"{name} → {node.name}"
        if self.args.no_input:
            self.report.manual("W2", title, f"Copy on {name}, paste on {node.name}.")
            return
        code = marker(self.sizes)
        since = time.time() - 1
        print(f"\n  Now on {name}, copy this text (select it and copy):\n\n      {code}\n")
        try:
            entry = wait_for(
                f"{node.name} to get it",
                lambda: received(node, len(code), since, name),
                90,
                every=1,
            )
        except CheckError as exc:
            self.report.add("W2", title, False, str(exc))
            return
        applied = bool(entry.get("applied"))
        self.report.add(
            "W2",
            title,
            applied,
            "on the phone's clipboard" if applied else "arrived but not applied",
        )

    # -------------------------------------------------- everyone at once

    def everyone(self) -> bool:
        """One copy here reaches every phone's clipboard and every other computer's app."""
        # Every computer that's online counts, also one left out of sync by mistake.
        computers = [d["name"] for d in self.other_computers()]
        title = "One copy reaches every device"
        if not self.androids and not computers:
            return self.report.add("M1", title, None, "no other devices online")
        text = marker(self.sizes)
        since = time.time() - 1
        self.clip.write(text)
        pc = self.here

        def missing() -> list[str]:
            gone = [
                n.name
                for n in self.androids
                if not (received(n, len(text), since) or {}).get("applied")
            ]
            accepted = (sent(pc, len(text), since) or {}).get("devices", [])
            return gone + [c for c in computers if c not in accepted]

        with contextlib.suppress(CheckError):
            wait_for("every device to get it", lambda: not missing(), SYNC_WAIT)
        left = missing()
        everyone = ", ".join([n.name for n in self.androids] + computers)
        if left:
            return self.report.add("M1", title, False, f"missing on {', '.join(left)}")
        return self.report.add("M1", title, True, everyone)

    # --------------------------------------------------- with no window

    def window_closed(self) -> None:
        app = expect(self.here.base, "GET", "/api/app")
        if app.get("background"):
            self.report.add(
                "D1", "Syncing with the window closed", True, "the background copy is running"
            )
            return
        if self.args.no_input or not self.androids:
            self.report.manual(
                "D1",
                "Syncing with the window closed",
                "Close the window, copy here, paste on another device.",
            )
            return
        self.ask("\n  Close the Open Transfer window on this computer, then press Enter… ")
        self.pc = Node("This computer", find_computer_app(None), "computer")
        pc = self.here
        wait_for(
            "the background copy",
            lambda: bool(expect(pc.base, "GET", "/api/app").get("background")),
            30,
        )
        pc.refresh()
        self.to_android(
            self.androids[0], "D1", "Syncing with the window closed (this computer → Android)"
        )

    # ---------------------------------------------------------------- run

    def run(self) -> None:
        for node in self.androids:
            self.to_android(node, "A1", f"This computer → {node.name}, app in front")
            self.from_android_share(node)
            self.from_android_tile(node)
            self.android_background(node)
            self.private_text(node)
            self.swiped_away(node)
        self.computers()
        self.crossings()
        self.everyone()
        self.window_closed()

    def logs(self, stamp: str) -> list[Path]:
        paths = []
        for node in self.androids:
            if self.adb is None:
                break
            path = Path(f"clipboard-check-{stamp}-{node.serial.replace(':', '_')}.log")
            with contextlib.suppress(CheckError, OSError, subprocess.TimeoutExpired):
                path.write_text(
                    self.tool.run("logcat", "-d", "-t", "2000", serial=node.serial),
                    encoding="utf-8",
                )
                paths.append(path)
        return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apk", help="the .apk (or CI artifact .zip) to install on Android first")
    parser.add_argument("--pc-url", help="this computer's app (default: found on 127.0.0.1)")
    parser.add_argument("--adb", help="path to adb")
    parser.add_argument("--serial", action="append", help="only these adb devices (repeatable)")
    parser.add_argument(
        "--no-input", action="store_true", help="don't ask for help with other computers"
    )
    parser.add_argument("--out", help="results file (default clipboard-check-<time>.md)")
    parser.add_argument("--no-adb", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # ✅ / ❌ on a Windows console
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    stamp = time.strftime("%Y%m%d-%H%M")
    report = Report()
    out = Path(args.out or f"clipboard-check-{stamp}.md")
    logs: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="ot-clipboard-check-") as tmp:
        checker = Checker(args, report, Path(tmp))
        try:
            checker.setup()
            checker.run()
        except (CheckError, OSError, subprocess.SubprocessError) as exc:
            report.add("!", "The check stopped early", False, str(exc)[:500])
        except KeyboardInterrupt:
            report.add("!", "Stopped by you", None)
        finally:
            out.write_text(
                report.markdown().replace("real-device check", "clipboard sync check"),
                encoding="utf-8",
            )
            if report.failed:
                logs = checker.logs(stamp)
    print(f"\nResults: {out.resolve()}")
    for path in logs:
        print(f"Android log: {path.resolve()}")
    print(f"{len(report.failed)} failed." if report.failed else "Everything checked passed.")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
