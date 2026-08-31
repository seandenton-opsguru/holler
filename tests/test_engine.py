import holler


def test_model_ids_alias_to_onnx_names():
    # Family configs carry the nvidia/... HF id; onnx-asr wants its registry name.
    assert holler.onnx_model_name("nvidia/parakeet-tdt-0.6b-v2") == "nemo-parakeet-tdt-0.6b-v2"
    assert holler.onnx_model_name("nvidia/parakeet-tdt-0.6b-v3") == "nemo-parakeet-tdt-0.6b-v3"
    # Unknown ids pass through untouched (onnx-asr accepts registry names or HF repos).
    assert holler.onnx_model_name("some/custom-model") == "some/custom-model"


def test_default_model(tmp_path, monkeypatch):
    monkeypatch.setattr(holler, "CONFIG_PATH", tmp_path / "config.json")
    assert holler.load_config()["model"] == "nvidia/parakeet-tdt-0.6b-v2"
