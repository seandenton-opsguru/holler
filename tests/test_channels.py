import holler


def test_default_channel_is_yell(tmp_path, monkeypatch):
    monkeypatch.setattr(holler, "CONFIG_PATH", tmp_path / "config.json")
    assert holler.load_config()["update_channel"] == "yell"


def test_whisper_accepted(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text('{"update_channel": "whisper"}')
    monkeypatch.setattr(holler, "CONFIG_PATH", cfg_path)
    assert holler.load_config()["update_channel"] == "whisper"


def test_unknown_channel_falls_back_with_warning(tmp_path, monkeypatch, capsys):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text('{"update_channel": "scream"}')
    monkeypatch.setattr(holler, "CONFIG_PATH", cfg_path)
    assert holler.load_config()["update_channel"] == "yell"
    assert "scream" in capsys.readouterr().err
