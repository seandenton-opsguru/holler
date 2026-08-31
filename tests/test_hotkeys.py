from pynput.keyboard import Key

import holler


def test_defaults():
    hotkeys, warnings = holler.merge_hotkeys(None)
    assert hotkeys == {"dictate": "ctrl_r", "dictate_type": None,
                       "type_clipboard": None, "meeting": None}
    assert warnings == []


def test_user_map_merges():
    hotkeys, warnings = holler.merge_hotkeys({"dictate": "f13", "meeting": "f14"})
    assert hotkeys["dictate"] == "f13"
    assert hotkeys["meeting"] == "f14"
    assert warnings == []


def test_unknown_action_dropped_with_warning():
    hotkeys, warnings = holler.merge_hotkeys({"teleport": "f13"})
    assert "teleport" not in hotkeys
    assert any("teleport" in w for w in warnings)


def test_unknown_key_name_dropped_with_warning():
    hotkeys, warnings = holler.merge_hotkeys({"dictate": "hyper_key"})
    assert hotkeys["dictate"] == "ctrl_r"  # default survives
    assert any("hyper_key" in w for w in warnings)


def test_collision_unbinds_lower_priority_action():
    hotkeys, warnings = holler.merge_hotkeys(
        {"dictate": "f13", "meeting": "f13"})
    assert hotkeys["dictate"] == "f13"
    assert hotkeys["meeting"] is None
    assert any("collision" in w for w in warnings)


def test_collision_priority_order_is_action_tuple_order():
    # dictate_type outranks type_clipboard outranks meeting.
    hotkeys, warnings = holler.merge_hotkeys(
        {"dictate_type": "f14", "type_clipboard": "f14", "meeting": "f14"})
    assert hotkeys["dictate_type"] == "f14"
    assert hotkeys["type_clipboard"] is None
    assert hotkeys["meeting"] is None
    assert len([w for w in warnings if "collision" in w]) == 2


def test_key_map_resolution():
    key_map = holler.hotkey_key_map({"dictate": "ctrl_r", "meeting": "f13"})
    assert key_map == {Key.ctrl_r: "dictate", Key.f13: "meeting"}


def test_key_map_unknown_meeting_key_unbinds(capsys):
    key_map = holler.hotkey_key_map({"dictate": "ctrl_r", "meeting": "bogus"})
    assert key_map == {Key.ctrl_r: "dictate"}
    assert "unbound" in capsys.readouterr().err


def test_load_config_merges_hotkeys(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text('{"hotkeys": {"dictate": "f13"}, "silence_rms": 0.02}')
    monkeypatch.setattr(holler, "CONFIG_PATH", cfg_path)
    cfg = holler.load_config()
    assert cfg["hotkeys"]["dictate"] == "f13"
    assert cfg["hotkeys"]["meeting"] is None
    assert cfg["silence_rms"] == 0.02


def test_all_hotkey_choices_resolve_to_stub_keys():
    # Every choice offered in the tray/CLI must be a real pynput Key attribute
    # (the conftest stub mirrors the names holler relies on).
    for _label, attr in holler.HOTKEY_CHOICES:
        assert holler.key_from_name(attr) is not None, attr
