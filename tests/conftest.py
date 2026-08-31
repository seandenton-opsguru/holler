"""Stub the audio/input hardware modules before importing holler.

holler.py guards its sounddevice/pynput imports, but the tests want the
*stubbed* modules present so pure logic that touches Key objects (hotkey
resolution) is exercisable on any machine — including Linux/macOS CI where
neither real module works headless, and where winsound/winreg don't exist
at all (holler only touches those behind `WINDOWS` checks).
"""
import sys
import types
from pathlib import Path

sd = types.ModuleType("sounddevice")


class _NoAudioInTests:
    def __init__(self, *args, **kwargs):
        raise AssertionError("tests must not open audio streams")


class _Default:
    device = (None, None)


sd.InputStream = _NoAudioInTests
sd.query_devices = lambda *a, **k: []
sd.default = _Default()
sys.modules["sounddevice"] = sd


class _StubKey:
    """Distinct hashable stand-ins for pynput's Key enum members."""

    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return f"Key.{self.name}"


class Key:
    pass


for _name in ("ctrl", "ctrl_r", "ctrl_l", "shift_r", "scroll_lock", "pause",
              "f13", "f14", "f15", "f16", "f17", "f18", "f19"):
    setattr(Key, _name, _StubKey(_name))


class _NoInputInTests:
    def __init__(self, *args, **kwargs):
        raise AssertionError("tests must not hook the keyboard")


pynput_keyboard = types.ModuleType("pynput.keyboard")
pynput_keyboard.Key = Key
pynput_keyboard.Controller = _NoInputInTests
pynput_keyboard.Listener = _NoInputInTests

pynput = types.ModuleType("pynput")
pynput.keyboard = pynput_keyboard
sys.modules["pynput"] = pynput
sys.modules["pynput.keyboard"] = pynput_keyboard

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import holler  # noqa: E402,F401  (imported for its side on sys.modules ordering)

assert holler._HAVE_PYNPUT, "conftest stubs must be visible to holler's import"
