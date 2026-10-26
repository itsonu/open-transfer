"""The macOS menu-bar item of the background copy (no Dock icon, no window).

Its menu: Open Open Transfer · Pause syncing for an hour / Resume syncing ·
Quit. It runs the AppKit event loop on the main thread, so :func:`run` blocks
until Quit (or ``stop`` is set by ``POST /api/app/quit`` or a signal).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("open_transfer.desktop")


@dataclass
class Actions:
    open_window: Callable[[], object]
    pause: Callable[[], object]
    resume: Callable[[], object]
    paused: Callable[[], bool]
    stop: threading.Event
    #: Runs before the app exits (AppKit's terminate doesn't return).
    cleanup: Callable[[], object]


def available() -> bool:
    try:
        import AppKit  # type: ignore[import-not-found]  # noqa: F401
        import Foundation  # type: ignore[import-not-found]  # noqa: F401
    except Exception:
        return False
    return True


_target_class: Any = None


def _call(fn: Callable[[], object]) -> None:
    try:
        fn()
    except Exception:
        log.warning("menu action failed", exc_info=True)


def _target(actions: Actions) -> Any:
    """An Objective-C object whose methods the menu items call."""
    global _target_class
    import AppKit
    from Foundation import NSObject

    if _target_class is None:

        class OTMenuTarget(NSObject):  # type: ignore[misc]
            actions: Any = None

            def openWindow_(self, sender: Any) -> None:
                _call(self.actions.open_window)

            def togglePause_(self, sender: Any) -> None:
                _call(self.actions.resume if self.actions.paused() else self.actions.pause)

            def quit_(self, sender: Any) -> None:
                self.actions.stop.set()

            def menuNeedsUpdate_(self, menu: Any) -> None:
                item = menu.itemWithTag_(1)
                if item is not None:
                    item.setTitle_(
                        "Resume syncing" if self.actions.paused() else "Pause syncing for an hour"
                    )

            def tick_(self, timer: Any) -> None:
                if self.actions.stop.is_set():
                    timer.invalidate()
                    _call(self.actions.cleanup)
                    AppKit.NSApplication.sharedApplication().terminate_(None)

        _target_class = OTMenuTarget
    target = _target_class.alloc().init()
    target.actions = actions
    return target


def run(actions: Actions) -> bool:
    """Show the menu-bar item and run until Quit. False if it couldn't be shown."""
    try:
        import AppKit
        from Foundation import NSTimer
    except Exception:
        return False
    try:
        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)  # no Dock icon
        target = _target(actions)
        item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(
            AppKit.NSVariableStatusItemLength
        )
        button = item.button()
        image = None
        if hasattr(AppKit.NSImage, "imageWithSystemSymbolName_accessibilityDescription_"):
            image = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                "arrow.left.arrow.right.circle", "Open Transfer"
            )
        if image is not None:
            image.setTemplate_(True)
            button.setImage_(image)
        else:
            button.setTitle_("⇄")
        button.setToolTip_("Open Transfer: syncing your clipboard")

        menu = AppKit.NSMenu.alloc().init()
        menu.setDelegate_(target)
        for title, selector, key, tag in (
            ("Open Open Transfer", "openWindow:", "o", 0),
            ("Pause syncing for an hour", "togglePause:", "", 1),
            (None, None, "", 0),
            ("Quit Open Transfer", "quit:", "q", 0),
        ):
            if title is None:
                menu.addItem_(AppKit.NSMenuItem.separatorItem())
                continue
            entry = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                title, selector, key
            )
            entry.setTarget_(target)
            entry.setTag_(tag)
            menu.addItem_(entry)
        item.setMenu_(menu)
        # Python code (signal handlers, the stop flag) gets a chance to run every second.
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0, target, "tick:", None, True
        )
    except Exception:
        log.warning("Couldn’t show the menu-bar item", exc_info=True)
        return False
    app.run()
    return True
