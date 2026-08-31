import json
import os
import time

import holler


def _point_runtime_at(tmp_path, monkeypatch):
    monkeypatch.setattr(holler, "RUNTIME_DIR", tmp_path / "run")
    return tmp_path / "run" / "status.json"


def test_write_read_roundtrip(tmp_path, monkeypatch):
    path = _point_runtime_at(tmp_path, monkeypatch)
    monkeypatch.setitem(holler.STATUS, "mic", "Yeti X")
    monkeypatch.setitem(holler.STATUS, "hotkeys",
                        {"dictate": "ctrl_r", "dictate_type": None,
                         "type_clipboard": None, "meeting": None})
    monkeypatch.setitem(holler.STATUS, "dictating", True)
    holler.write_status()
    st = holler.read_status()  # our own live pid passes the liveness probe
    assert st is not None
    assert st["pid"] == os.getpid()
    assert st["mic"] == "Yeti X"
    assert st["dictating"] is True
    assert st["meeting_active"] is False
    assert st["version"]  # non-empty: tag or short SHA or "unknown"
    assert path.exists()


def test_read_status_rejects_dead_pid(tmp_path, monkeypatch):
    path = _point_runtime_at(tmp_path, monkeypatch)
    path.parent.mkdir(parents=True)
    # PIDs are recycled from low numbers; 2**22 exceeds default pid_max.
    path.write_text(json.dumps({"pid": 2 ** 22 + 1, "meeting_active": False}))
    assert holler.read_status() is None


def test_read_status_rejects_corrupt_file(tmp_path, monkeypatch):
    path = _point_runtime_at(tmp_path, monkeypatch)
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert holler.read_status() is None


def test_clear_status(tmp_path, monkeypatch):
    path = _point_runtime_at(tmp_path, monkeypatch)
    holler.write_status()
    assert path.exists()
    holler.clear_status()
    assert not path.exists()


def test_pid_alive_rejects_nonsense():
    assert not holler.pid_alive(0)
    assert not holler.pid_alive(-1)
    assert not holler.pid_alive("42")  # type: ignore[arg-type]
    assert holler.pid_alive(os.getpid())


def test_current_version_returns_something():
    v = holler.current_version()
    assert isinstance(v, str) and v


# --- control file (the Windows stand-in for SIGUSR1/SIGHUP) -----------------


def test_control_roundtrip(tmp_path, monkeypatch):
    _point_runtime_at(tmp_path, monkeypatch)
    assert holler.send_control("meeting")
    assert holler.poll_control() == "meeting"
    assert holler.poll_control() is None  # consumed


def test_control_rejects_unknown_command(tmp_path, monkeypatch):
    _point_runtime_at(tmp_path, monkeypatch)
    assert not holler.send_control("self_destruct")
    assert holler.poll_control() is None


def test_control_discards_stale_command(tmp_path, monkeypatch):
    _point_runtime_at(tmp_path, monkeypatch)
    p = holler.control_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"cmd": "quit", "ts": time.time() - 120}))
    assert holler.poll_control() is None


def test_control_ignores_corrupt_file(tmp_path, monkeypatch):
    _point_runtime_at(tmp_path, monkeypatch)
    p = holler.control_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json")
    assert holler.poll_control() is None
    assert not p.exists()  # consumed even when corrupt
