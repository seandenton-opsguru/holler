#!/usr/bin/env python3
"""
holler — Windows push-to-talk dictation daemon.

Hold the configured hotkey, speak, release. A Parakeet-TDT model transcribes
locally (onnx-asr, int8, CPU) and the result lands at the cursor — pasted via
the clipboard with a synthesized Ctrl+V (and the prior clipboard restored), or
typed as raw keystrokes for VDIs and remote desktops that mangle paste.

Sibling of blurt (macOS) and yap (Linux). Same engine seam, same VAD, same
transcript edge cleanup, same status-file contract; the platform layer is
pynput global hooks + Win32 clipboard + winsound + a Run-key autostart.

Windows has no POSIX signals worth having, so where yap uses SIGHUP/SIGUSR1
the CLI talks to the daemon through a small control file the hotkey loop
polls. And note: os.kill(pid, 0) on Windows TERMINATES the process — liveness
probes here go through OpenProcess instead. Do not "simplify" that.
"""

from __future__ import annotations

import ctypes
import json
import os
import queue
import re
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np

# sounddevice needs PortAudio; pynput needs a desktop session. Both are
# guarded so the CLI fast paths (status/config/help) and the test suite can
# import this module on machines where neither is available.
try:
    import sounddevice as sd
    _HAVE_SD = True
except Exception as _sd_err:  # pragma: no cover
    sd = None
    _HAVE_SD = False

try:
    from pynput.keyboard import Controller as KBController, Key, Listener
    _HAVE_PYNPUT = True
except Exception as _pynput_err:  # pragma: no cover
    KBController = Key = Listener = None
    _HAVE_PYNPUT = False

WINDOWS = sys.platform == "win32"

# --- Config ----------------------------------------------------------------


def _appdata() -> Path:
    return Path(os.environ.get("APPDATA") or (Path.home() / ".config"))


def _localappdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or (Path.home() / ".local" / "share"))


CONFIG_PATH = _appdata() / "holler" / "config.json"
RUNTIME_DIR = _localappdata() / "holler" / "run"
LOG_PATH = _localappdata() / "holler" / "holler.log"
MEETING_DIR = Path.home() / "Documents" / "holler-meetings"

TARGET_RATE = 16000  # Parakeet expects 16 kHz mono float32.
MIN_UTTERANCE_SEC = 0.2  # Anything shorter is treated as a key bounce.
CONTINUATION_SEC = 15.0  # Back-to-back dictations within this get a joining space.
CLIPBOARD_RESTORE_DELAY = 0.8  # Seconds after Ctrl+V before the old clipboard returns.
MEETING_CHUNK_SEC = 30.0
CONTROL_POLL_SEC = 0.2

# dictate/dictate_type = press-and-hold; type_clipboard = hold, types the
# clipboard on release (no mic); meeting = tap to toggle.
HOTKEY_ACTIONS = ("dictate", "dictate_type", "type_clipboard", "meeting")
UPDATE_CHANNELS = ("yell", "whisper")  # yell = stable (default), whisper = beta.

# (menu label, pynput Key attribute). Alt-family keys are deliberately absent:
# a lone Alt tap focuses menu bars in Electron and classic Win32 apps alike,
# and Right Alt is AltGr on many non-US layouts.
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
_HOTKEY_ATTRS = {attr for _, attr in HOTKEY_CHOICES}

DEFAULTS: dict = {
    "microphone": None,             # substring match against input devices; null = default.
    "hotkeys": {                    # pynput Key attribute per action; null = unbound.
        "dictate": "ctrl_r",        #   hold to talk -> clipboard paste (the fast path).
        "dictate_type": None,       #   hold to talk -> typed keystrokes (VDI-proof).
        "type_clipboard": None,     #   hold + release -> type clipboard as keystrokes.
        "meeting": None,            #   tap to start/stop a meeting recording.
    },
    "model": "nvidia/parakeet-tdt-0.6b-v2",
    "silence_rms": 0.01,            # Peak-window RMS below this = silence; skip ASR.
    "sound_start": r"C:\Windows\Media\Speech On.wav",   # null = no start cue.
    "sound_stop": r"C:\Windows\Media\Speech Sleep.wav",  # null = no stop cue.
    "type_key_delay": 0.0,          # Per-char sleep in type mode. 0.005-0.01 for cranky VDIs.
    "update_channel": "yell",       # "yell" (stable) or "whisper" (beta). Floating git tags.
}


def merge_hotkeys(user_hotkeys) -> tuple[dict, list[str]]:
    """Defaults <- user's per-action map. Unknown actions are dropped; unknown
    key names are dropped with a warning; two actions bound to one key is a
    collision resolved in priority order (dictate > dictate_type >
    type_clipboard > meeting) — the loser is unbound. Returns (hotkeys, warnings)."""
    hotkeys = dict(DEFAULTS["hotkeys"])
    warnings: list[str] = []
    if isinstance(user_hotkeys, dict):
        for action, key in user_hotkeys.items():
            if action not in HOTKEY_ACTIONS:
                warnings.append(f"unknown hotkey action {action!r} ignored")
                continue
            if key is not None and key not in _HOTKEY_ATTRS:
                warnings.append(f"unknown {action} hotkey {key!r} ignored")
                continue
            hotkeys[action] = key
    seen: dict[str, str] = {}
    for action in HOTKEY_ACTIONS:  # tuple order IS the priority order
        key = hotkeys[action]
        if key is None:
            continue
        if key in seen:
            warnings.append(
                f"hotkey collision: {action!r} and {seen[key]!r} both bound to "
                f"{key!r}; unbinding {action!r}")
            hotkeys[action] = None
        else:
            seen[key] = action
    return hotkeys, warnings


def load_config() -> dict:
    cfg = {k: v for k, v in DEFAULTS.items()}
    user: dict = {}
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if not isinstance(user, dict):
                print("holler: config is not a JSON object; using defaults", file=sys.stderr)
                user = {}
            cfg.update({k: v for k, v in user.items() if k in DEFAULTS and k != "hotkeys"})
        except (OSError, json.JSONDecodeError) as e:
            print(f"holler: config parse error ({e}); using defaults", file=sys.stderr)
            user = {}
    cfg["hotkeys"], warnings = merge_hotkeys(user.get("hotkeys"))
    for w in warnings:
        print(f"holler: {w}", file=sys.stderr)
    if cfg["update_channel"] not in UPDATE_CHANNELS:
        print(f"holler: unknown update_channel {cfg['update_channel']!r}; "
              f"using {DEFAULTS['update_channel']!r}", file=sys.stderr)
        cfg["update_channel"] = DEFAULTS["update_channel"]
    return cfg


def save_config(cfg: dict) -> None:
    """Atomic JSON write. Unknown keys are dropped to avoid persisting typos."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    clean = {k: cfg[k] for k in DEFAULTS if k in cfg}
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(clean, indent=2) + "\n", encoding="utf-8")
    tmp.replace(CONFIG_PATH)


# --- Process liveness (the os.kill(pid, 0) trap) ---------------------------

_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def pid_alive(pid: int) -> bool:
    """True if `pid` is a live process. On Windows this MUST NOT use
    os.kill(pid, 0): any signal value other than CTRL_C/CTRL_BREAK terminates
    the target there. OpenProcess + GetExitCodeProcess is the honest probe."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if WINDOWS:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)  # safe on POSIX only
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# --- Status file -----------------------------------------------------------
# Written by the daemon so the CLI (`holler status`, `holler meeting ...`) and
# the tray can see daemon state without touching the heavy import path. This
# file is the daemon's one outward-facing contract.

REPO_ROOT = Path(__file__).resolve().parent

_version_cache: str | None = None


def current_version() -> str:
    """Highest v* tag at HEAD, else the short SHA. Cached for the process."""
    global _version_cache
    if _version_cache is None:
        _version_cache = "unknown"
        try:
            r = subprocess.run(
                ["git", "tag", "--points-at", "HEAD", "--sort=-v:refname", "--list", "v*"],
                capture_output=True, text=True, cwd=REPO_ROOT, timeout=5)
            tags = r.stdout.split()
            if tags:
                _version_cache = tags[0]
            else:
                r = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                                   capture_output=True, text=True, cwd=REPO_ROOT, timeout=5)
                if r.stdout.strip():
                    _version_cache = r.stdout.strip()
        except Exception:
            pass
    return _version_cache


def status_path() -> Path:
    return RUNTIME_DIR / "status.json"


def control_path() -> Path:
    return RUNTIME_DIR / "control.json"


STATUS: dict = {
    "meeting": None,      # MeetingRecorder | None
    "mic": None,          # active mic name
    "devices": None,      # input-capable device names (for the tray's mic picker)
    "hotkeys": None,      # active hotkeys map
    "dictating": False,   # a dictate hotkey is currently held
    "channel": None,      # active update_channel
    "update": None,       # {"status", "version", "behind"} once the checker runs
}


def write_status() -> None:
    """Best-effort atomic write; daemon state must never depend on it."""
    try:
        p = status_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        m = STATUS["meeting"]
        active = bool(m is not None and m.is_active())
        data = {
            "pid": os.getpid(),
            "version": current_version(),
            "channel": STATUS["channel"],
            "mic": STATUS["mic"],
            "devices": STATUS["devices"],
            "hotkeys": STATUS["hotkeys"],
            "dictating": STATUS["dictating"],
            "update": STATUS["update"],
            "meeting_active": active,
            "meeting_started_at": m.started_at if active else None,
            "wav_path": str(m.wav_path) if active else None,
            "txt_path": str(m.txt_path) if active else None,
        }
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(p)
    except OSError as e:
        print(f"holler: status file write failed: {e}", file=sys.stderr)


def clear_status() -> None:
    try:
        status_path().unlink(missing_ok=True)
    except OSError:
        pass


def read_status() -> dict | None:
    """Status of a *live* daemon, else None (missing/stale/corrupt file)."""
    try:
        st = json.loads(status_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    pid = st.get("pid")
    if not isinstance(pid, int) or not pid_alive(pid):
        return None
    return st


# --- Control file ----------------------------------------------------------
# The CLI/tray -> daemon channel. yap uses SIGUSR1/SIGHUP; Windows offers no
# usable equivalent, so commands are a tiny JSON file the hotkey loop polls
# (and deletes) every CONTROL_POLL_SEC. Last-writer-wins is fine at this rate.

CONTROL_COMMANDS = ("meeting", "reload", "quit")


def send_control(cmd: str) -> bool:
    if cmd not in CONTROL_COMMANDS:
        return False
    try:
        p = control_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"cmd": cmd, "ts": time.time()}), encoding="utf-8")
        tmp.replace(p)
        return True
    except OSError as e:
        print(f"holler: control write failed: {e}", file=sys.stderr)
        return False


def poll_control() -> str | None:
    """Consume and return a pending command, else None. Stale commands
    (older than 30 s — e.g. left behind by a dead CLI) are discarded."""
    p = control_path()
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    cmd = data.get("cmd")
    if cmd not in CONTROL_COMMANDS:
        return None
    if time.time() - float(data.get("ts") or 0) > 30.0:
        return None
    return cmd


# --- Self-update -----------------------------------------------------------
# Family scheme: releases are channel tags (yell/whisper); updating is fetch
# tags -> resolve the channel tag to a SHA once -> reset --hard -> re-run
# install.ps1 (idempotent, our dep-sync step) -> restart the daemon.


def _git(args: list[str], timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          cwd=REPO_ROOT, timeout=timeout)


def check_for_updates(channel: str) -> dict:
    """Statuses: up_to_date | update_available | dirty | not_on_main | check_failed.
    update_available carries target (SHA), version, behind."""
    try:
        if _git(["status", "--porcelain"]).stdout.strip():
            return {"status": "dirty", "error": "local changes present"}
        branch = _git(["symbolic-ref", "--short", "HEAD"]).stdout.strip()
        if branch != "main":
            return {"status": "not_on_main", "error": f"on branch {branch!r}, not main"}
        fetch = _git(["fetch", "origin", "--tags", "--force"], timeout=60)
        if fetch.returncode != 0:
            return {"status": "check_failed", "error": fetch.stderr.strip() or "git fetch failed"}
        r = _git(["rev-parse", "--verify", "--quiet", f"refs/tags/{channel}^{{commit}}"])
        target = r.stdout.strip()
        if r.returncode != 0 or not target:
            return {"status": "check_failed",
                    "error": f"channel tag {channel!r} not found — cut a release first"}
        version_r = _git(["tag", "--points-at", target, "--sort=-v:refname", "--list", "v*"])
        version = (version_r.stdout.split() or [target[:7]])[0]
        if _git(["rev-parse", "HEAD"]).stdout.strip() == target:
            return {"status": "up_to_date", "version": version, "behind": 0}
        behind_r = _git(["rev-list", "--count", f"HEAD..{target}"])
        behind = int(behind_r.stdout.strip() or 0) if behind_r.returncode == 0 else 0
        return {"status": "update_available", "version": version,
                "behind": behind, "target": target}
    except Exception as e:
        return {"status": "check_failed", "error": str(e)}


def apply_update(check: dict) -> dict:
    """Reset to the already-resolved SHA (immune to the tag moving mid-update),
    then re-run install.ps1 as the dependency sync. Statuses: applied |
    sync_failed | apply_failed."""
    reset = _git(["reset", "--hard", check["target"]], timeout=60)
    if reset.returncode != 0:
        return {"status": "apply_failed", "error": reset.stderr.strip()}
    global _version_cache
    _version_cache = None  # HEAD moved
    sync = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(REPO_ROOT / "install.ps1")],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=1800)
    if sync.returncode != 0:
        tail = "\n".join((sync.stderr or sync.stdout).splitlines()[-5:])
        return {"status": "sync_failed",
                "error": f"install.ps1 failed (tree is on {check['version']}):\n{tail}"}
    return {"status": "applied", "version": check["version"]}


def _startup_update_check(channel: str) -> None:
    """Background thread at daemon start: populate the status file's update
    slot so `holler status` and the tray can show 'update available'."""
    chk = check_for_updates(channel)
    STATUS["update"] = {k: chk.get(k) for k in ("status", "version", "behind")}
    write_status()
    if chk["status"] == "update_available":
        print(f"holler: update available: {chk['version']} on {channel} "
              f"({chk['behind']} behind) — run `holler update`", flush=True)


# --- Sound cues ------------------------------------------------------------


class SoundCues:
    """Fire-and-forget start/stop chirps so mic state is audible.

    winsound with SND_ASYNC never blocks the hotkey path. A missing file or a
    non-Windows platform degrades to a no-op with a single warning."""

    def __init__(self, cfg: dict):
        self._start = self._check_file(cfg["sound_start"])
        self._stop = self._check_file(cfg["sound_stop"])

    @staticmethod
    def _check_file(path: str | None) -> Path | None:
        if not path:
            return None
        p = Path(path)
        if not p.exists():
            print(f"holler: sound file not found: {p}; cue disabled", file=sys.stderr)
            return None
        return p

    @staticmethod
    def _play(path: Path | None) -> None:
        if path is None or not WINDOWS:
            return
        try:
            import winsound
            winsound.PlaySound(str(path),
                               winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        except Exception:
            pass

    def play_start(self) -> None:
        self._play(self._start)

    def play_stop(self) -> None:
        self._play(self._stop)


# --- Clipboard (Win32, no pywin32) -----------------------------------------

_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002


def _open_clipboard_retry(user32, attempts: int = 5) -> bool:
    """The clipboard is a global mutex another app may briefly hold."""
    for _ in range(attempts):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.03)
    return False


def get_clipboard() -> str:
    """Current clipboard text ('' if empty, non-text, or unavailable)."""
    if not WINDOWS:  # pragma: no cover
        return ""
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    if not _open_clipboard_retry(user32):
        return ""
    try:
        handle = user32.GetClipboardData(_CF_UNICODETEXT)
        if not handle:
            return ""
        kernel32.GlobalLock.restype = ctypes.c_void_p
        locked = kernel32.GlobalLock(ctypes.c_void_p(handle))
        if not locked:
            return ""
        try:
            return ctypes.wstring_at(locked)
        finally:
            kernel32.GlobalUnlock(ctypes.c_void_p(handle))
    except Exception:
        return ""
    finally:
        user32.CloseClipboard()


def set_clipboard(text: str) -> bool:
    if not WINDOWS:  # pragma: no cover
        return False
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    if not _open_clipboard_retry(user32):
        return False
    try:
        user32.EmptyClipboard()
        buf = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(buf)
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        handle = kernel32.GlobalAlloc(_GMEM_MOVEABLE, size)
        if not handle:
            return False
        kernel32.GlobalLock.restype = ctypes.c_void_p
        locked = kernel32.GlobalLock(ctypes.c_void_p(handle))
        if not locked:
            kernel32.GlobalFree(ctypes.c_void_p(handle))
            return False
        ctypes.memmove(locked, buf, size)
        kernel32.GlobalUnlock(ctypes.c_void_p(handle))
        if not user32.SetClipboardData(_CF_UNICODETEXT, ctypes.c_void_p(handle)):
            kernel32.GlobalFree(ctypes.c_void_p(handle))
            return False
        return True  # the system owns the handle now
    except Exception:
        return False
    finally:
        user32.CloseClipboard()


# --- Delivery --------------------------------------------------------------

_kb = None  # KBController, created lazily by the daemon


def _keyboard() -> "KBController":
    global _kb
    if _kb is None:
        _kb = KBController()
    return _kb


# Monotonic time of the last successful delivery. Module-level on purpose: it
# must survive config reloads; last-writer-wins across transcription threads
# is acceptable for a 15 s heuristic.
_last_paste_ts: float | None = None


def needs_continuation_space(text: str, now: float, last_ts: float | None) -> bool:
    """Joining space between back-to-back dictations, so two utterances into
    the same field don't run together as 'Hello.How are you?'. Only when the
    new transcript starts with a letter/digit — punctuation starts are
    deliberate."""
    if last_ts is None or now - last_ts > CONTINUATION_SEC:
        return False
    return bool(text) and text[0].isalnum()


def paste_text(text: str) -> None:
    """Clipboard + synthesized Ctrl+V, then restore the prior clipboard.

    Restore only happens when the prior content was text we could read —
    an image on the clipboard is lost to a paste, which is the same tradeoff
    blurt makes and worth it for the speed."""
    global _last_paste_ts
    if not text:
        return
    prior = get_clipboard()
    if not set_clipboard(text):
        print("holler: clipboard unavailable; falling back to typing", file=sys.stderr)
        type_text(text, 0.0)
        return
    time.sleep(0.05)
    kb = _keyboard()
    kb.press(Key.ctrl)
    kb.press("v")
    kb.release("v")
    kb.release(Key.ctrl)
    _last_paste_ts = time.monotonic()

    def restore():
        time.sleep(CLIPBOARD_RESTORE_DELAY)
        set_clipboard(prior)
    if prior:
        threading.Thread(target=restore, daemon=True).start()


def _emit_keystrokes(text: str, key_delay: float,
                     abort: "threading.Event | None" = None) -> int:
    """Type text via synthesized keystrokes; returns chars emitted. Iterates
    per character (even at delay 0) so the abort flag, when supplied, is
    honored before each keystroke."""
    kb = _keyboard()
    n = 0
    for ch in text:
        if abort is not None and abort.is_set():
            break
        kb.type(ch)
        n += 1
        if key_delay > 0.0:
            time.sleep(key_delay)
    return n


def type_text(text: str, key_delay: float) -> None:
    """Deliver text as raw keystrokes (for VDIs/remote desktops that mangle
    Ctrl+V or ignore synthetic paste)."""
    global _last_paste_ts
    if not text:
        return
    _emit_keystrokes(text, key_delay)
    _last_paste_ts = time.monotonic()


def type_clipboard(abort: "threading.Event", key_delay: float) -> None:
    """Type the current clipboard contents out verbatim as keystrokes.

    No continuation-space munging and no _last_paste_ts update — this is a
    deliberate bulk entry, not a dictation continuation. Honors `abort`."""
    text = get_clipboard()
    if not text.strip():
        print("holler: clipboard empty; nothing to type", flush=True)
        return
    n = _emit_keystrokes(text, key_delay, abort=abort)
    if abort.is_set():
        print(f"holler: clipboard type aborted after {n} chars", flush=True)
    else:
        print(f"holler: clipboard typed {n} chars", flush=True)


# --- Audio capture ---------------------------------------------------------


def find_mic(name_substr: str | None) -> tuple[int | None, int, str]:
    """Return (device_index_or_None, native_sample_rate, device_name)."""
    devices = sd.query_devices()
    if name_substr:
        needle = name_substr.lower()
        for i, d in enumerate(devices):
            if d["max_input_channels"] > 0 and needle in d["name"].lower():
                return i, int(d["default_samplerate"]), d["name"]
        print(f"holler: mic substring {name_substr!r} not found; using default", file=sys.stderr)
    default_idx = sd.default.device[0]
    if default_idx is None or default_idx < 0:
        for i, d in enumerate(devices):
            if d["max_input_channels"] > 0:
                return i, int(d["default_samplerate"]), d["name"]
        raise RuntimeError("no input-capable audio devices found")
    d = devices[default_idx]
    return default_idx, int(d["default_samplerate"]), d["name"]


def resample_to_16k(audio_f32: np.ndarray, src_rate: int) -> np.ndarray:
    audio_f32 = audio_f32.astype(np.float32, copy=False).flatten()
    if src_rate == TARGET_RATE:
        return audio_f32
    n_out = int(len(audio_f32) * TARGET_RATE / src_rate)
    if n_out <= 0:
        return np.zeros(0, dtype=np.float32)
    idx = np.linspace(0, len(audio_f32) - 1, n_out, dtype=np.float64)
    return np.interp(idx, np.arange(len(audio_f32), dtype=np.float64), audio_f32).astype(np.float32)


def buffer_peak_rms(audio: np.ndarray, sample_rate: int, window_ms: int = 100) -> float:
    """Loudest 100 ms window's RMS. Used for VAD instead of whole-buffer mean
    RMS so a short utterance after a thinking pause isn't averaged into
    silence: ambient stays ~0.002 across every window, but one word in 3 s of
    dead air hits 0.05+ in the window containing it."""
    if len(audio) == 0:
        return 0.0
    w = max(1, int(sample_rate * window_ms / 1000))
    n = (len(audio) // w) * w
    if n == 0:
        return float(np.sqrt(np.mean(audio.astype(np.float64, copy=False) ** 2)))
    frames = audio[:n].astype(np.float64, copy=False).reshape(-1, w)
    return float(np.sqrt((frames * frames).mean(axis=1)).max())


class Recorder:
    """Thread-safe single-channel rolling capture driven by hotkey press/release."""

    def __init__(self, mic_idx: int | None, mic_rate: int):
        self.mic_idx = mic_idx
        self.mic_rate = mic_rate
        self._stream = None
        self._frames: list[np.ndarray] = []
        self._active = False
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._active:
                return
            self._frames = []
            self._active = True

            def cb(indata, _frames, _time_info, status):
                if status:
                    print(f"holler: audio status {status}", file=sys.stderr)
                if self._active:
                    self._frames.append(indata.copy())

            self._stream = sd.InputStream(
                device=self.mic_idx, samplerate=self.mic_rate,
                channels=1, dtype="float32", callback=cb,
            )
            self._stream.start()

    def stop(self) -> np.ndarray | None:
        with self._lock:
            if not self._active:
                return None
            self._active = False
            stream, self._stream = self._stream, None
            frames = self._frames
            self._frames = []
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        if not frames:
            return None
        audio = np.concatenate(frames).flatten().astype(np.float32)
        return resample_to_16k(audio, self.mic_rate)


# --- ASR model -------------------------------------------------------------

_asr_lock = threading.Lock()

# HF ids -> onnx-asr registry names, so configs stay portable across the family.
_ONNX_MODEL_ALIASES = {
    "nvidia/parakeet-tdt-0.6b-v2": "nemo-parakeet-tdt-0.6b-v2",
    "nvidia/parakeet-tdt-0.6b-v3": "nemo-parakeet-tdt-0.6b-v3",
}


def onnx_model_name(model: str) -> str:
    return _ONNX_MODEL_ALIASES.get(model, model)


class OnnxParakeetASR:
    """Parakeet via onnx-asr + onnxruntime (int8, CPU). Same seam as the
    family's other engines: 16 kHz float32 ndarray in, text out, all inference
    behind _asr_lock (dictation and meeting chunks must not run concurrently)."""

    def __init__(self, model_name: str):
        t0 = time.time()
        name = onnx_model_name(model_name)
        print(f"holler: loading {name} (onnx int8, cpu)...", flush=True)
        import onnx_asr  # deferred: keep the CLI fast path clean
        self.model = onnx_asr.load_model(name, quantization="int8")
        print(f"holler: loaded in {((time.time() - t0) * 1000):.0f} ms; warming...", flush=True)
        silence = np.zeros(int(0.5 * TARGET_RATE), dtype=np.float32)
        w0 = time.time()
        self.transcribe(silence)
        print(f"holler: warmup {((time.time() - w0) * 1000):.0f} ms", flush=True)

    def transcribe(self, audio_16k_f32: np.ndarray) -> str:
        with _asr_lock:
            text = self.model.recognize(audio_16k_f32, sample_rate=TARGET_RATE)
            return (text or "").strip()


# --- Transcript post-processing --------------------------------------------

# Parakeet hallucinates short backchannels during silence and often appends
# "Thank you." / "Thanks." (training-data artifact from narration corpora).
# Edges only — mid-utterance content is never touched. \b anchors are load-
# bearing: without them "um"/"ah" match word prefixes ("umbrella" -> "brella").
_BACKCHANNEL = r"(?:mm+-?h+m+|uh-?huh|ah-?ha|aha|uh|um|ah)"
_TRAIL_THANKS = r"thank(?:s|\s+you)(?:\s+(?:very|so)\s+much)?(?:\s+for\s+(?:watching|listening))?"
_CLEAN_LEAD = re.compile(rf"^(?:\s*{_BACKCHANNEL}\b[\.,!?]?\s*)+", re.IGNORECASE)
_CLEAN_TRAIL = re.compile(
    rf"(?:\s*\b(?:{_BACKCHANNEL}|{_TRAIL_THANKS})\b[\.,!?]?\s*)+$",
    re.IGNORECASE,
)


def cleanup(text: str) -> str:
    if not text:
        return ""
    text = _CLEAN_LEAD.sub("", text)
    text = _CLEAN_TRAIL.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# --- Meeting recorder ------------------------------------------------------


def _fmt_hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def meeting_paths(start_dt: datetime, directory: Path = MEETING_DIR) -> tuple[Path, Path]:
    """Return (txt_path, wav_path) sharing a timestamped stem. Disambiguates
    with -2, -3, ... so two recordings in one minute never clobber each other."""
    base = f"{start_dt.strftime('%Y-%m-%d-%H-%M')}-meeting"
    candidate, n = base, 2
    while (directory / f"{candidate}.txt").exists() or (directory / f"{candidate}.wav").exists():
        candidate, n = f"{base}-{n}", n + 1
    return directory / f"{candidate}.txt", directory / f"{candidate}.wav"


def _wav_header(sample_rate: int, data_bytes: int) -> bytes:
    """44-byte PCM16 mono WAV header, written by hand so the length fields can
    be re-patched while recording — a hard kill then loses at most one chunk's
    worth of header accounting instead of an unplayable 0-length header."""
    return (
        b"RIFF" + struct.pack("<I", 36 + data_bytes) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
        + b"data" + struct.pack("<I", data_bytes)
    )


class MeetingRecorder:
    """Long-form capture from the mic -> chunked live transcript.

    Capture runs on the sounddevice callback thread (blocks pushed to a
    queue); a worker streams them to the WAV as int16 and every
    MEETING_CHUNK_SEC runs resample -> VAD -> ASR -> cleanup, appending
    "[hh:mm:ss] text" lines with a flush per write. Both files grow
    incrementally so a crash mid-meeting leaves a playable WAV and a partial
    transcript."""

    def __init__(self, asr):
        self._asr = asr
        self._state_lock = threading.Lock()
        self._active = threading.Event()
        self._stop_evt = threading.Event()
        self._queue: queue.Queue = queue.Queue()
        self._stream = None
        self._worker: threading.Thread | None = None
        self._wav_file = None
        self._wav_bytes = 0
        self.txt_path: Path | None = None
        self.wav_path: Path | None = None
        self.started_at: float | None = None  # epoch, for the CLI status surface
        self._rate = 0
        self._silence_rms = 0.01
        self._chunk: list[np.ndarray] = []
        self._chunk_samples = 0
        self._done_samples = 0  # samples already attributed to earlier chunks
        self._start_mono = 0.0
        self._done = True

    def is_active(self) -> bool:
        return self._active.is_set()

    def elapsed(self) -> float:
        return time.monotonic() - self._start_mono if self.is_active() else 0.0

    def start(self, mic_idx: int | None, mic_rate: int, silence_rms: float) -> bool:
        """Open outputs + stream and begin recording. False on failure (no state left)."""
        if self.is_active():
            return False
        try:
            MEETING_DIR.mkdir(parents=True, exist_ok=True)
            start_dt = datetime.now()
            self.txt_path, self.wav_path = meeting_paths(start_dt)
            self.txt_path.write_text(
                f"Meeting — {start_dt.strftime('%Y-%m-%d %H:%M')}\n\n", encoding="utf-8")
            self._wav_file = open(self.wav_path, "wb")
            self._wav_bytes = 0
            self._wav_file.write(_wav_header(mic_rate, 0))
            self._rate = mic_rate
            self._silence_rms = silence_rms
            self._stop_evt.clear()
            self._done = False
            self._chunk, self._chunk_samples, self._done_samples = [], 0, 0
            self._queue = queue.Queue()
            self._stream = sd.InputStream(
                device=mic_idx, samplerate=mic_rate, channels=1, dtype="float32",
                callback=self._cb,
            )
            self._stream.start()
        except Exception as e:
            print(f"holler: meeting start failed: {e}", file=sys.stderr)
            self._close_outputs()
            self._done = True
            return False
        self._start_mono = time.monotonic()
        self.started_at = time.time()
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()
        self._active.set()
        print(f"holler: meeting recording -> {self.txt_path}", flush=True)
        return True

    def _cb(self, indata, _frames, _time_info, status):
        if status:
            print(f"holler: meeting audio status: {status}", file=sys.stderr)
        self._queue.put(indata.copy().reshape(-1))

    def _run(self) -> None:
        chunk_target = int(MEETING_CHUNK_SEC * self._rate)
        while True:
            try:
                block = self._queue.get(timeout=0.1)
            except queue.Empty:
                if self._stop_evt.is_set():
                    break
                continue
            try:
                pcm = (np.clip(block, -1.0, 1.0) * 32767.0).astype("<i2")
                self._wav_file.write(pcm.tobytes())
                self._wav_bytes += pcm.nbytes
            except Exception as e:
                print(f"holler: meeting wav write: {e}", file=sys.stderr)
            self._chunk.append(block)
            self._chunk_samples += len(block)
            if self._chunk_samples >= chunk_target:
                self._flush_chunk()
        self._flush_chunk()

    def _patch_wav_header(self) -> None:
        try:
            f = self._wav_file
            f.flush()
            pos = f.tell()
            f.seek(0)
            f.write(_wav_header(self._rate, self._wav_bytes))
            f.seek(pos)
        except Exception as e:
            print(f"holler: meeting wav header patch: {e}", file=sys.stderr)

    def _flush_chunk(self) -> None:
        if self._chunk_samples == 0:
            return
        audio = np.concatenate(self._chunk)
        self._chunk, self._chunk_samples = [], 0
        stamp = _fmt_hms(self._done_samples / self._rate)
        self._done_samples += len(audio)
        self._patch_wav_header()  # every ~30 s: a crash loses ≤1 chunk of accounting
        audio16 = resample_to_16k(audio.astype(np.float32, copy=False), self._rate)
        if buffer_peak_rms(audio16, TARGET_RATE) < self._silence_rms:
            return
        try:
            text = cleanup(self._asr.transcribe(audio16))
        except Exception as e:
            # One bad chunk must not end the meeting; the WAV still has the audio.
            print(f"holler: meeting chunk transcribe: {e}", file=sys.stderr)
            return
        if text:
            try:
                with open(self.txt_path, "a", encoding="utf-8") as f:
                    f.write(f"[{stamp}] {text}\n")
            except Exception as e:
                print(f"holler: meeting txt write: {e}", file=sys.stderr)

    def stop(self) -> Path | None:
        """Finalize: stop capture, drain + transcribe the tail, close files,
        open the transcript. Idempotent."""
        with self._state_lock:
            if self._done:
                return None
            self._done = True
        elapsed = time.monotonic() - self._start_mono
        self._stop_evt.set()
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as e:
                print(f"holler: meeting stream close: {e}", file=sys.stderr)
            self._stream = None
        if self._worker is not None:
            self._worker.join(timeout=120.0)
            self._worker = None
        self._close_outputs()
        txt_path = self.txt_path
        try:
            with open(txt_path, "a", encoding="utf-8") as f:
                f.write(f"\n— ended {datetime.now().strftime('%H:%M')}, "
                        f"duration {_fmt_hms(elapsed)} —\n")
        except Exception as e:
            print(f"holler: meeting footer write: {e}", file=sys.stderr)
        self._active.clear()
        print(f"holler: meeting saved -> {txt_path}", flush=True)
        try:
            if WINDOWS:
                os.startfile(str(txt_path))  # type: ignore[attr-defined]
        except OSError:
            pass
        return txt_path

    def _close_outputs(self) -> None:
        if self._wav_file is not None:
            self._patch_wav_header()
            try:
                self._wav_file.close()
            except Exception:
                pass
            self._wav_file = None


# --- Hotkey controller (pynput global hooks) --------------------------------


def key_from_name(name: str | None):
    """pynput Key attribute for a config hotkey name, else None."""
    if not name:
        return None
    return getattr(Key, name, None)


def hotkey_key_map(hotkeys: dict) -> dict:
    """pynput Key object -> action name. Unknown dictate key is fatal (the
    daemon is useless without it); unknown keys elsewhere warn and unbind."""
    key_map: dict = {}
    for action in HOTKEY_ACTIONS:
        name = hotkeys.get(action)
        if name is None:
            continue
        key = key_from_name(name)
        if key is None:
            if action == "dictate":
                print(f"holler: unknown hotkey {name!r}", file=sys.stderr)
                sys.exit(2)
            print(f"holler: unknown {action} hotkey {name!r}; unbound", file=sys.stderr)
            continue
        key_map[key] = action
    return key_map


class HotkeyController:
    """State machine over pynput's global hook. Windows auto-repeats key-down
    while a key is held, so every press handler is guarded by held-state.
    Callbacks stay tiny; anything slow (ASR, typing) runs on its own thread."""

    def __init__(self, key_map: dict, recorder: Recorder, asr, cfg: dict,
                 cues: SoundCues, meeting: MeetingRecorder,
                 meeting_toggle: threading.Event):
        self.key_map = key_map
        self.recorder = recorder
        self.asr = asr
        self.silence_rms = float(cfg["silence_rms"])
        self.key_delay = float(cfg["type_key_delay"])
        self.cues = cues
        self.meeting = meeting
        self.meeting_toggle = meeting_toggle
        self.held: str | None = None      # dictate action currently held, else None
        self._clip_abort = threading.Event()
        self._clip_thread: threading.Thread | None = None
        self._listener = None

    def start(self):
        self._listener = Listener(on_press=self._on_press, on_release=self._on_release)
        self._listener.start()

    def stop(self):
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
        if self.held is not None:
            # Torn down mid-hold (reload/quit): close the stream cleanly.
            self.recorder.stop()
            self.held = None
            STATUS["dictating"] = False
            write_status()

    # -- press/release plumbing

    def _on_press(self, key):
        action = self.key_map.get(key)
        if action is None:
            return
        if action == "type_clipboard":
            # Press during an in-flight clipboard type = abort it.
            if self._clip_thread is not None and self._clip_thread.is_alive():
                self._clip_abort.set()
            return
        if action == "meeting":
            return  # tap actions fire on release, so a hold can't retrigger
        # dictate / dictate_type: press-and-hold. Auto-repeat guard: `held`.
        if self.held is not None:
            return
        if self.meeting.is_active():
            print("holler: dictation disabled during meeting", flush=True)
            return
        self.held = action
        self.recorder.start()
        self.cues.play_start()
        STATUS["dictating"] = True
        write_status()

    def _on_release(self, key):
        action = self.key_map.get(key)
        if action is None:
            return
        if action == "meeting":
            self.meeting_toggle.set()
            return
        if action == "type_clipboard":
            self._release_type_clipboard()
            return
        if self.held != action:
            return
        self.held = None
        audio = self.recorder.stop()
        self.cues.play_stop()
        STATUS["dictating"] = False
        write_status()
        if audio is None or len(audio) < int(MIN_UTTERANCE_SEC * TARGET_RATE):
            print("holler: (too short)")
            return
        rms = buffer_peak_rms(audio, TARGET_RATE)
        if rms < self.silence_rms:
            print(f"holler: (silence peak-rms={rms:.4f} < {self.silence_rms:.4f})")
            return
        mode = "type" if action == "dictate_type" else "paste"
        threading.Thread(
            target=_transcribe_and_deliver,
            args=(audio, self.asr, mode, self.key_delay),
            daemon=True,
        ).start()

    def _release_type_clipboard(self):
        if self._clip_thread is not None and self._clip_thread.is_alive():
            return  # the press already aborted it; this release is its tail
        self._clip_abort = threading.Event()

        def work():
            try:
                type_clipboard(self._clip_abort, self.key_delay)
            except Exception as e:
                print(f"holler: clipboard type: {e}", file=sys.stderr)
        self._clip_thread = threading.Thread(target=work, daemon=True)
        self._clip_thread.start()


def _transcribe_and_deliver(audio: np.ndarray, asr, mode: str, key_delay: float) -> None:
    global _last_paste_ts
    t0 = time.time()
    try:
        text = asr.transcribe(audio)
    except Exception as e:
        print(f"holler: transcription error: {e}", file=sys.stderr)
        return
    infer_ms = (time.time() - t0) * 1000
    dur_ms = len(audio) / TARGET_RATE * 1000
    if not text:
        print(f"holler: [{infer_ms:.0f} ms / {dur_ms:.0f} ms audio] (no speech)")
        return
    cleaned = cleanup(text)
    if cleaned != text:
        if not cleaned:
            print(f"holler: [{infer_ms:.0f} ms / {dur_ms:.0f} ms audio] "
                  f"dropped {text!r} (all hallucination)")
            return
        print(f"holler: cleaned {text!r} -> {cleaned!r}")
        text = cleaned
    print(f"holler: [{infer_ms:.0f} ms / {dur_ms:.0f} ms audio] {text}")
    if needs_continuation_space(text, time.monotonic(), _last_paste_ts):
        text = " " + text
    d0 = time.time()
    if mode == "type":
        type_text(text, key_delay)
    else:
        paste_text(text)
    print(f"holler: {mode} {((time.time() - d0) * 1000):.0f} ms")


# --- Startup registration (HKCU Run key) -----------------------------------

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUES = {"holler": "holler.py", "holler-tray": "holler_tray.py"}


def _pythonw() -> str:
    """pythonw.exe beside the current interpreter (no console window)."""
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


def startup_enabled() -> bool:
    if not WINDOWS:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
            winreg.QueryValueEx(k, "holler")
        return True
    except OSError:
        return False


def cmd_startup(args: list[str]) -> int:
    if not WINDOWS:
        print("holler: startup registration is Windows-only", file=sys.stderr)
        return 2
    import winreg
    sub = args[0].lower() if args else "status"
    if sub == "status" or sub not in ("on", "off"):
        if sub not in ("status", "on", "off"):
            print(f"holler: unknown startup subcommand {sub!r} (use: on|off|status)",
                  file=sys.stderr)
            return 2
        print("enabled" if startup_enabled() else "disabled")
        return 0
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if sub == "on":
            for name, script in _RUN_VALUES.items():
                cmd = f'"{_pythonw()}" "{REPO_ROOT / script}"'
                winreg.SetValueEx(k, name, 0, winreg.REG_SZ, cmd)
            print("holler: daemon + tray will start at sign-in")
        else:
            for name in _RUN_VALUES:
                try:
                    winreg.DeleteValue(k, name)
                except OSError:
                    pass
            print("holler: startup entries removed")
    return 0


# --- CLI subcommands -------------------------------------------------------


def cmd_devices() -> int:
    devices = sd.query_devices()
    default_in = sd.default.device[0]
    print(f"{'idx':>4} {'rate':>7}  name")
    for i, d in enumerate(devices):
        if d["max_input_channels"] <= 0:
            continue
        tag = "  *" if i == default_in else "   "
        print(f"{i:>4} {int(d['default_samplerate']):>7}{tag}{d['name']}")
    print("\n* = current system default input.")
    return 0


def cmd_mic(args: list[str]) -> int:
    cfg = load_config()
    if not args:
        print(cfg["microphone"] if cfg["microphone"] is not None else "(default)")
        return 0
    name = " ".join(args).strip()
    cfg["microphone"] = name or None
    save_config(cfg)
    idx, rate, matched = find_mic(cfg["microphone"])
    print(f"holler: microphone = {cfg['microphone']!r}  (matches [{idx}] {matched} @ {rate} Hz)")
    _hint_reload()
    return 0


def cmd_hotkey(args: list[str]) -> int:
    cfg = load_config()
    if not args:
        for action in HOTKEY_ACTIONS:
            print(f"{action:<15} {cfg['hotkeys'][action] or '(unbound)'}")
        return 0
    if args[0] in HOTKEY_ACTIONS:
        action, key_arg = args[0], args[1] if len(args) > 1 else None
    else:
        action, key_arg = "dictate", args[0]
    if key_arg is None:
        print(cfg["hotkeys"][action] or "(unbound)")
        return 0
    if key_arg.lower() in ("none", "null", "off"):
        if action == "dictate":
            print("holler: the dictate hotkey cannot be unbound", file=sys.stderr)
            return 2
        cfg["hotkeys"][action] = None
        save_config(cfg)
        print(f"holler: {action} hotkey unbound")
        _hint_reload()
        return 0
    key = key_arg.lower()
    if key not in _HOTKEY_ATTRS:
        print(f"holler: unknown hotkey {key!r}", file=sys.stderr)
        print("holler: choices: " + ", ".join(attr for _, attr in HOTKEY_CHOICES),
              file=sys.stderr)
        return 2
    for other in HOTKEY_ACTIONS:
        if other != action and cfg["hotkeys"].get(other) == key:
            print(f"holler: {key!r} is already bound to {other!r} — unbind it first",
                  file=sys.stderr)
            return 2
    cfg["hotkeys"][action] = key
    save_config(cfg)
    print(f"holler: {action} hotkey = {key}")
    _hint_reload()
    return 0


def cmd_meeting(args: list[str]) -> int:
    sub = args[0] if args else "toggle"
    if sub not in ("toggle", "status"):
        print(f"holler: unknown meeting subcommand {sub!r} (use: holler meeting [toggle|status])",
              file=sys.stderr)
        return 2
    st = read_status()
    if st is None:
        print("holler: no running daemon — start it with `holler` (or `holler startup on`)",
              file=sys.stderr)
        return 1

    if sub == "status":
        if st.get("meeting_active"):
            print(f"meeting recording for {_fmt_hms(time.time() - st['meeting_started_at'])}")
            print(f"  wav: {st['wav_path']}")
            print(f"  txt: {st['txt_path']}")
        else:
            print("no meeting recording")
        return 0

    was_active = bool(st.get("meeting_active"))
    if was_active:
        print("stopping meeting…")
    send_control("meeting")
    # The daemon's loop notices within ~CONTROL_POLL_SEC; stopping also
    # transcribes the final partial chunk, so give it a few seconds.
    deadline = time.time() + 10.0
    while time.time() < deadline:
        st = read_status()
        if st is not None and bool(st.get("meeting_active")) != was_active:
            if st["meeting_active"]:
                print("started recording ->")
                print(f"  wav: {st['wav_path']}")
                print(f"  txt: {st['txt_path']}")
            else:
                print("stopped — transcript should open shortly")
            return 0
        time.sleep(0.1)
    print("holler: command sent but daemon state didn't change — check `holler logs`",
          file=sys.stderr)
    return 1


def cmd_channel(args: list[str]) -> int:
    cfg = load_config()
    if not args:
        print(cfg["update_channel"])
        return 0
    ch = args[0].lower()
    if ch not in UPDATE_CHANNELS:
        print(f"holler: unknown channel {ch!r} (use: {' | '.join(UPDATE_CHANNELS)})",
              file=sys.stderr)
        return 2
    cfg["update_channel"] = ch
    save_config(cfg)
    print(f"holler: update channel = {ch} — see what it offers with `holler update --check`")
    return 0


def cmd_update(args: list[str]) -> int:
    check_only = "--check" in args
    auto = "--auto" in args
    channel = load_config()["update_channel"]
    chk = check_for_updates(channel)
    if chk["status"] == "up_to_date":
        print(f"holler: up to date ({chk['version']} · {channel})")
        return 0
    if chk["status"] != "update_available":
        print(f"holler: update check: {chk['status']} — {chk.get('error', '')}", file=sys.stderr)
        return 0 if auto else 1
    print(f"holler: {chk['version']} available on {channel} ({chk['behind']} behind)"
          if chk["behind"] else
          f"holler: switch to {chk['version']} available on {channel}")
    if check_only:
        return 0
    st = read_status()
    if st is not None and st.get("meeting_active"):
        print("holler: meeting recording in progress — not updating; try again after it stops",
              file=sys.stderr)
        return 0 if auto else 1
    print(f"holler: updating to {chk['version']}…")
    res = apply_update(chk)
    if res["status"] != "applied":
        print(f"holler: {res['status']}: {res['error']}", file=sys.stderr)
        return 1
    print(f"holler: now on {res['version']}")
    if read_status() is not None:
        print("holler: restarting daemon…")
        return cmd_restart([])
    print("holler: daemon not running — it'll pick up the new code next start")
    return 0


def cmd_restart(_args: list[str]) -> int:
    """Stop the running daemon (politely, then firmly) and start a fresh one
    detached from this console."""
    st = read_status()
    if st is not None:
        send_control("quit")
        deadline = time.time() + 8.0
        while time.time() < deadline and read_status() is not None:
            time.sleep(0.2)
        st2 = read_status()
        if st2 is not None:
            print("holler: daemon didn't exit; terminating it", file=sys.stderr)
            subprocess.run(["taskkill", "/PID", str(st2["pid"]), "/F"],
                           capture_output=True)
            clear_status()
    flags = 0
    if WINDOWS:
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([_pythonw(), str(REPO_ROOT / "holler.py")],
                     creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
    print("holler: daemon starting — check `holler status` in a few seconds")
    return 0


def cmd_status(_args: list[str]) -> int:
    st = read_status()
    if st is None:
        print("holler: no running daemon — start it with `holler` (or `holler restart`)",
              file=sys.stderr)
        return 1
    channel = f" · {st['channel']}" if st.get("channel") else ""
    print(f"daemon     pid {st['pid']} · {st.get('version', '?')}{channel}")
    print(f"mic        {st.get('mic') or 'default'}")
    hotkeys = st.get("hotkeys") or {}
    print("hotkeys    " + "  ".join(
        f"{a}={hotkeys.get(a) or '(unbound)'}" for a in HOTKEY_ACTIONS))
    print(f"dictating  {'yes' if st.get('dictating') else 'no'}")
    if st.get("meeting_active"):
        print(f"meeting    recording for {_fmt_hms(time.time() - st['meeting_started_at'])}")
        print(f"           txt: {st['txt_path']}")
    else:
        print("meeting    not recording")
    print(f"startup    {'at sign-in' if startup_enabled() else 'manual'}")
    upd = st.get("update")
    if upd and upd.get("status") == "update_available":
        print(f"update     {upd.get('version', '?')} available "
              f"({upd.get('behind', '?')} behind) — run `holler update`")
    return 0


def cmd_version(_args: list[str]) -> int:
    print(f"holler {current_version()} · {load_config()['update_channel']}")
    return 0


def cmd_logs(args: list[str]) -> int:
    """Tail-follow the daemon log (no journald here)."""
    if not LOG_PATH.exists():
        print(f"holler: no log yet at {LOG_PATH}", file=sys.stderr)
        return 1
    follow = "-f" in args or not args
    with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
        for line in lines[-200:]:
            print(line, end="")
        if not follow:
            return 0
        try:
            while True:
                line = f.readline()
                if line:
                    print(line, end="", flush=True)
                else:
                    time.sleep(0.5)
        except KeyboardInterrupt:
            return 0


def cmd_help(_args: list[str]) -> int:
    print(
        "holler — push-to-talk dictation daemon for Windows\n"
        "\n"
        "Usage:\n"
        "  holler                   run the daemon (foreground; `holler restart` detaches)\n"
        "  holler devices           list input-capable audio devices\n"
        "  holler mic [SUBSTRING]   show or set microphone (empty = system default)\n"
        "  holler hotkey [ACTION] [KEY|none]   show/set per-action hotkeys\n"
        "                           actions: dictate (hold->paste), dictate_type\n"
        "                           (hold->keystrokes), type_clipboard (hold; types\n"
        "                           clipboard on release), meeting (tap toggle)\n"
        "                           `holler hotkey f13` sets dictate (shorthand)\n"
        "  holler status            daemon/mic/hotkeys/dictating/meeting at a glance\n"
        "  holler version           running version + update channel (yell/whisper)\n"
        "  holler update [--check]  update to the channel's release (--check: report only)\n"
        "  holler channel [yell|whisper]   show or switch the update channel\n"
        "  holler meeting [toggle]  start/stop a meeting recording on the running daemon\n"
        "  holler meeting status    is a meeting recording? elapsed time + file paths\n"
        "  holler startup [on|off]  start daemon + tray automatically at sign-in\n"
        "  holler restart           restart the daemon detached from this console\n"
        "  holler logs [-f]         tail the daemon log\n"
        "  holler help              this message\n"
        "\n"
        f"Config: {CONFIG_PATH}\n"
        "Config changes apply live: the daemon reloads mic + hotkeys when the CLI\n"
        "asks it to (`holler restart` only needed for a model change).\n"
    )
    return 0


def _hint_reload() -> None:
    if read_status() is not None:
        send_control("reload")
        print("holler: daemon reloading to apply (model stays warm).")
    else:
        print("holler: will apply next time the daemon starts.")


# --- Daemon ----------------------------------------------------------------


def _redirect_output_to_log() -> None:
    """Under pythonw.exe there is no console; stdout/stderr are None. Send
    everything to the log file so `holler logs` has something to say."""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 5_000_000:
            LOG_PATH.replace(LOG_PATH.with_suffix(".log.1"))  # crude, sufficient
        log = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
        sys.stdout = log
        sys.stderr = log
        print(f"\n--- holler daemon starting {datetime.now().isoformat(timespec='seconds')} ---")
    except OSError:
        pass


def run_daemon(_args: list[str]) -> int:
    if sys.stdout is None or sys.stderr is None or not hasattr(sys.stdout, "isatty") \
            or not sys.stdout.isatty():
        _redirect_output_to_log()
    if not _HAVE_SD:
        print("holler: sounddevice unavailable — reinstall with install.ps1", file=sys.stderr)
        return 2
    if not _HAVE_PYNPUT:
        print("holler: pynput unavailable — reinstall with install.ps1", file=sys.stderr)
        return 2

    existing = read_status()
    if existing is not None and existing.get("pid") != os.getpid():
        print(f"holler: a daemon is already running (pid {existing['pid']}) — "
              f"use `holler restart`", file=sys.stderr)
        return 1

    print(f"holler: config={CONFIG_PATH}", flush=True)

    # Model is loaded exactly once; reload re-reads mic + hotkeys only.
    # Changing `model` in the config requires a full restart.
    cfg = load_config()
    asr = OnnxParakeetASR(cfg["model"])

    stop_flag = threading.Event()
    reload_flag = threading.Event()
    meeting_toggle = threading.Event()

    # One MeetingRecorder for the daemon's lifetime: an active meeting keeps
    # running across reloads (it owns its own InputStream); only dictation
    # state is rebuilt per reload cycle.
    meeting = MeetingRecorder(asr)
    STATUS["meeting"] = meeting
    write_status()
    threading.Thread(target=_startup_update_check,
                     args=(cfg["update_channel"],), daemon=True).start()

    try:
        while not stop_flag.is_set():
            cfg = load_config()
            mic_idx, mic_rate, mic_name = find_mic(cfg["microphone"])
            print(f"holler: mic [{mic_idx}] {mic_name} @ {mic_rate} Hz", flush=True)
            recorder = Recorder(mic_idx, mic_rate)
            cues = SoundCues(cfg)
            STATUS["mic"], STATUS["hotkeys"] = mic_name, cfg["hotkeys"]
            STATUS["channel"] = cfg["update_channel"]
            try:
                STATUS["devices"] = [d["name"] for d in sd.query_devices()
                                     if d["max_input_channels"] > 0]
            except Exception:
                STATUS["devices"] = None
            write_status()

            key_map = hotkey_key_map(cfg["hotkeys"])
            controller = HotkeyController(key_map, recorder, asr, cfg, cues,
                                          meeting, meeting_toggle)
            controller.start()
            bindings = ", ".join(f"{cfg['hotkeys'][a]}={a}"
                                 for a in HOTKEY_ACTIONS if cfg["hotkeys"].get(a))
            print(f"holler: listening; {bindings}. Ctrl+C to quit.", flush=True)

            try:
                while not stop_flag.is_set() and not reload_flag.is_set():
                    if meeting_toggle.is_set():
                        meeting_toggle.clear()
                        if meeting.is_active():
                            meeting.stop()
                            cues.play_stop()
                        elif controller.held is not None:
                            print("holler: can't start meeting while dictating", flush=True)
                        elif meeting.start(recorder.mic_idx, recorder.mic_rate,
                                           float(cfg["silence_rms"])):
                            cues.play_start()
                        write_status()
                    cmd = poll_control()
                    if cmd == "meeting":
                        meeting_toggle.set()
                        continue
                    if cmd == "reload":
                        reload_flag.set()
                        continue
                    if cmd == "quit":
                        stop_flag.set()
                        continue
                    time.sleep(CONTROL_POLL_SEC)
            finally:
                controller.stop()
            if reload_flag.is_set():
                reload_flag.clear()
                print("holler: reloading config", flush=True)
    except KeyboardInterrupt:
        pass
    if meeting.is_active():
        # Quit mid-meeting: finalize the transcript rather than orphaning it.
        meeting.stop()
    clear_status()
    print("holler: bye", flush=True)
    return 0


# --- Entry point -----------------------------------------------------------

_COMMANDS = {
    "devices": lambda a: cmd_devices(),
    "mic": cmd_mic,
    "hotkey": cmd_hotkey,
    "meeting": cmd_meeting,
    "status": cmd_status,
    "version": cmd_version,
    "--version": cmd_version,
    "update": cmd_update,
    "channel": cmd_channel,
    "startup": cmd_startup,
    "restart": cmd_restart,
    "logs": cmd_logs,
    "run": run_daemon,
    "help": cmd_help,
    "-h": cmd_help,
    "--help": cmd_help,
}


def main() -> None:
    argv = sys.argv[1:]
    if not argv:
        sys.exit(run_daemon(argv))
    cmd, rest = argv[0], argv[1:]
    handler = _COMMANDS.get(cmd)
    if handler is None:
        print(f"holler: unknown command {cmd!r}. Try `holler help`.", file=sys.stderr)
        sys.exit(2)
    sys.exit(handler(rest) or 0)


if __name__ == "__main__":
    main()
