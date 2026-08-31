#!/usr/bin/env python3
"""holler tray — notification-area icon + menu for the holler daemon.

Same architecture as yap's tray: this process deliberately does NOT import
holler.py (which pulls sounddevice/pynput) — the daemon's status file is the
read contract and the `holler` CLI is the write contract. A tray crash can
never break dictation.

Icons are drawn with PIL at runtime (a mic dot in four states) so the repo
ships no image assets and the tray has no theme dependencies.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

# pystray/PIL are optional at import time so the pure helpers below stay
# testable without them. main() refuses to run without them.
try:
    import pystray
    from PIL import Image, ImageDraw
    HAVE_TRAY = True
except ImportError:
    HAVE_TRAY = False

WINDOWS = sys.platform == "win32"
POLL_SEC = 0.5

HOTKEY_CHOICES = [
    ("Right Ctrl", "ctrl_r"),
    ("Left Ctrl", "ctrl_l"),
    ("Right Shift", "shift_r"),
    ("Scroll Lock", "scroll_lock"),
    ("Pause", "pause"),
    ("F13", "f13"),
    ("F14", "f14"),
    ("F15", "f15"),
    ("F16", "f16"),
    ("F17", "f17"),
    ("F18", "f18"),
    ("F19", "f19"),
]
HOTKEY_ACTIONS = ("dictate", "dictate_type", "type_clipboard", "meeting")
ACTION_LABELS = {
    "dictate": "Dictate Hotkey (paste)",
    "dictate_type": "Dictate Hotkey (type)",
    "type_clipboard": "Type-Clipboard Hotkey",
    "meeting": "Meeting Hotkey",
}
CHANNELS = [("yell", "yell (stable)"), ("whisper", "whisper (beta)")]


def _localappdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or (Path.home() / ".local" / "share"))


def _holler_cli() -> list[str]:
    """Command prefix that runs the holler CLI: the shim if on PATH, else the
    venv python + holler.py beside this file."""
    shim = shutil.which("holler")
    if shim:
        return [shim]
    return [sys.executable, str(Path(__file__).resolve().parent / "holler.py")]


# --- pure helpers (unit-tested; no pystray) ---------------------------------


def status_path() -> Path:
    return _localappdata() / "holler" / "run" / "status.json"


def _pid_alive(pid: int) -> bool:
    """Mirror of holler.py's probe (kept tiny on purpose — see module doc).
    NEVER os.kill(pid, 0) here: on Windows that terminates the process."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if WINDOWS:
        import ctypes
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def read_status() -> dict | None:
    try:
        st = json.loads(status_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    pid = st.get("pid")
    if not isinstance(pid, int) or not _pid_alive(pid):
        return None
    return st


def pick_icon(status: dict | None) -> str:
    if status is None:
        return "down"
    if status.get("meeting_active"):
        return "meeting"
    if status.get("dictating"):
        return "dictating"
    return "idle"


def fmt_hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def meeting_label(status: dict | None, now: float | None = None) -> str:
    if status and status.get("meeting_active"):
        elapsed = (now if now is not None else time.time()) - status["meeting_started_at"]
        return f"Meeting: Stop Recording ({fmt_hms(elapsed)})"
    return "Meeting: Start Recording"


def version_label(status: dict | None) -> str:
    if status is None:
        return "Daemon: not running"
    v = status.get("version") or "?"
    ch = status.get("channel")
    return f"Version: {v} · {ch}" if ch else f"Version: {v}"


def update_label(status: dict | None) -> str:
    upd = (status or {}).get("update") or {}
    if upd.get("status") == "update_available":
        behind = upd.get("behind") or 0
        v = upd.get("version") or "?"
        return f"Update to {v} ({behind} behind)" if behind else f"Switch to {v}"
    return "Check for Updates"


# --- icons ------------------------------------------------------------------

_ICON_COLORS = {
    # (fill, outline). Drawn as a filled mic-dot on transparency.
    "idle": ((160, 160, 160, 255), (90, 90, 90, 255)),
    "dictating": ((220, 60, 50, 255), (130, 30, 25, 255)),
    "meeting": ((235, 150, 30, 255), (150, 90, 15, 255)),
    "down": ((70, 70, 70, 255), (40, 40, 40, 255)),
}


def draw_icon(state: str, size: int = 64):
    """A simple microphone glyph: capsule body + stand, tinted per state.
    'down' additionally gets a slash."""
    fill, outline = _ICON_COLORS.get(state, _ICON_COLORS["idle"])
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 64.0
    # capsule
    d.rounded_rectangle([24 * s, 8 * s, 40 * s, 36 * s], radius=8 * s,
                        fill=fill, outline=outline, width=max(1, int(2 * s)))
    # cradle arc
    d.arc([16 * s, 18 * s, 48 * s, 46 * s], start=0, end=180,
          fill=outline, width=max(1, int(3 * s)))
    # stem + base
    d.line([32 * s, 46 * s, 32 * s, 54 * s], fill=outline, width=max(1, int(3 * s)))
    d.line([22 * s, 55 * s, 42 * s, 55 * s], fill=outline, width=max(1, int(3 * s)))
    if state == "down":
        d.line([10 * s, 54 * s, 54 * s, 10 * s], fill=(200, 50, 50, 255),
               width=max(2, int(4 * s)))
    return img


# --- tray -------------------------------------------------------------------


class Tray:
    def __init__(self):
        self.status: dict | None = read_status()
        self.busy_label: str | None = None  # overrides the update item while working
        self._icons = {state: draw_icon(state) for state in _ICON_COLORS}
        self._current = pick_icon(self.status)
        self.icon = pystray.Icon(
            "holler", self._icons[self._current], "holler", menu=self._menu())
        threading.Thread(target=self._poll_loop, daemon=True).start()

    # -- menu construction. pystray menus built from callables re-evaluate on
    # open, so the menu is always a pure function of the latest status read.

    def _menu(self):
        return pystray.Menu(
            pystray.MenuItem(lambda _i: meeting_label(self.status), self.on_meeting,
                             enabled=lambda _i: self.status is not None),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Microphone", pystray.Menu(self._mic_items)),
            *[pystray.MenuItem(ACTION_LABELS[a],
                               pystray.Menu(self._hotkey_items_fn(a)))
              for a in HOTKEY_ACTIONS],
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda _i: version_label(self.status), None, enabled=False),
            pystray.MenuItem(lambda _i: self.busy_label or update_label(self.status),
                             self.on_update,
                             enabled=lambda _i: self.busy_label is None),
            pystray.MenuItem("Channel", pystray.Menu(self._channel_items)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Start at sign-in", self.on_startup_toggle,
                             checked=lambda _i: self._startup_enabled()),
            pystray.MenuItem("Restart daemon", self.on_restart),
            pystray.MenuItem("Quit tray", self.on_quit),
        )

    def _mic_items(self):
        st = self.status or {}
        devices = st.get("devices") or []
        current = st.get("mic") or ""
        if not devices:
            yield pystray.MenuItem("(no devices — daemon down?)", None, enabled=False)
            return
        for name in devices:
            yield pystray.MenuItem(
                name,
                (lambda n: (lambda _i, _it: self._run_bg(["mic", n])))(name),
                checked=(lambda n: (lambda _it: n == current))(name),
            )

    def _hotkey_items_fn(self, action: str):
        def items():
            st = self.status or {}
            hotkeys = st.get("hotkeys") or {}
            mine = hotkeys.get(action)
            others = {v for k, v in hotkeys.items() if k != action and v}
            for label, key in HOTKEY_CHOICES:
                yield pystray.MenuItem(
                    label,
                    (lambda k: (lambda _i, _it: self._run_bg(["hotkey", action, k])))(key),
                    checked=(lambda k: (lambda _it: k == mine))(key),
                    enabled=key not in others,
                )
            if action != "dictate":
                yield pystray.MenuItem(
                    "Unbound",
                    lambda _i, _it: self._run_bg(["hotkey", action, "none"]),
                    checked=lambda _it: mine is None,
                )
        return items

    def _channel_items(self):
        current = (self.status or {}).get("channel")
        for ch, label in CHANNELS:
            yield pystray.MenuItem(
                label,
                (lambda c: (lambda _i, _it: self._run_bg(["channel", c])))(ch),
                checked=(lambda c: (lambda _it: c == current))(ch),
            )

    # -- actions (fire-and-forget threads; the tray loop never blocks)

    def _run_bg(self, args: list[str]):
        def work():
            subprocess.run([*_holler_cli(), *args], capture_output=True, timeout=600)
        threading.Thread(target=work, daemon=True).start()

    def on_meeting(self, _icon, _item):
        self._run_bg(["meeting"])

    def on_update(self, _icon, _item):
        def work():
            self.busy_label = "Updating…" if "Update to" in update_label(self.status) \
                else "Checking…"
            self.icon.update_menu()
            r = subprocess.run([*_holler_cli(), "update"], capture_output=True,
                               text=True, timeout=1800)
            self.busy_label = "Update failed — see `holler logs`" if r.returncode != 0 else None
            self.icon.update_menu()
            if self.busy_label:
                time.sleep(5)
                self.busy_label = None
                self.icon.update_menu()
        threading.Thread(target=work, daemon=True).start()

    def on_restart(self, _icon, _item):
        self._run_bg(["restart"])

    def on_startup_toggle(self, _icon, _item):
        self._run_bg(["startup", "off" if self._startup_enabled() else "on"])

    def on_quit(self, _icon, _item):
        self.icon.stop()

    def _startup_enabled(self) -> bool:
        if not WINDOWS:
            return False
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
                winreg.QueryValueEx(k, "holler")
            return True
        except OSError:
            return False

    # -- polling

    def _poll_loop(self):
        while True:
            self.status = read_status()
            state = pick_icon(self.status)
            if state != self._current:
                self._current = state
                self.icon.icon = self._icons[state]
            time.sleep(POLL_SEC)

    def run(self):
        self.icon.run()


def main() -> int:
    if not HAVE_TRAY:
        print("holler-tray: pystray/Pillow not available — reinstall with install.ps1",
              file=sys.stderr)
        return 1
    Tray().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
