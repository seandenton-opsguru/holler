import holler_tray


def test_importable_without_pystray():
    # The module must import even without pystray/Pillow so its pure helpers
    # stay testable. main() is what requires them.
    assert holler_tray.HAVE_TRAY in (True, False)


def test_pick_icon_states():
    assert holler_tray.pick_icon(None) == "down"
    assert holler_tray.pick_icon({"meeting_active": True, "dictating": False}) == "meeting"
    assert holler_tray.pick_icon({"meeting_active": False, "dictating": True}) == "dictating"
    # meeting wins over dictating (dictation is disabled during meetings anyway)
    assert holler_tray.pick_icon({"meeting_active": True, "dictating": True}) == "meeting"
    assert holler_tray.pick_icon({"meeting_active": False, "dictating": False}) == "idle"


def test_meeting_label():
    assert holler_tray.meeting_label(None) == "Meeting: Start Recording"
    st = {"meeting_active": True, "meeting_started_at": 1000.0}
    assert holler_tray.meeting_label(st, now=1754.0) == "Meeting: Stop Recording (00:12:34)"


def test_version_label():
    assert holler_tray.version_label(None) == "Daemon: not running"
    assert holler_tray.version_label({"version": "v0.1.0", "channel": "yell"}) \
        == "Version: v0.1.0 · yell"
    assert holler_tray.version_label({"version": "abc1234"}) == "Version: abc1234"


def test_update_label_state_machine():
    assert holler_tray.update_label(None) == "Check for Updates"
    assert holler_tray.update_label({"update": {"status": "up_to_date"}}) == "Check for Updates"
    assert holler_tray.update_label(
        {"update": {"status": "update_available", "version": "v0.2.0", "behind": 2}}) \
        == "Update to v0.2.0 (2 behind)"
    assert holler_tray.update_label(
        {"update": {"status": "update_available", "version": "v0.2.0", "behind": 0}}) \
        == "Switch to v0.2.0"


def test_tray_never_shells_out_for_devices():
    # Family regression guard: a `holler devices` subprocess costs seconds of
    # venv-import CPU; the daemon publishes the device list in the status file.
    import inspect
    src = inspect.getsource(holler_tray)
    assert '"devices"]' not in src  # no CLI devices invocation
    assert '"devices"' in src       # read from status instead


def test_tray_never_uses_os_kill_for_liveness():
    # os.kill(pid, 0) TERMINATES the target on Windows. The probe must go
    # through OpenProcess there; only the POSIX branch may call os.kill.
    import inspect
    src = inspect.getsource(holler_tray._pid_alive)
    assert "OpenProcess" in src
