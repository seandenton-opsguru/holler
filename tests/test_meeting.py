import struct
import sys
import threading
import time
from datetime import datetime

import numpy as np

import holler


class FakeASR:
    """Counts calls and returns numbered chunk texts."""

    def __init__(self):
        self.calls = 0

    def transcribe(self, audio):
        self.calls += 1
        return f"chunk {self.calls}"


def make_recorder(tmp_path, asr, rate=16000):
    """A MeetingRecorder with outputs open but no audio stream."""
    rec = holler.MeetingRecorder(asr)
    rec._rate = rate
    rec._silence_rms = 0.01
    rec.txt_path = tmp_path / "m.txt"
    rec.txt_path.write_text("Meeting — test\n\n")
    rec.wav_path = tmp_path / "m.wav"
    rec._wav_file = open(rec.wav_path, "wb")
    rec._wav_file.write(holler._wav_header(rate, 0))
    return rec


def voiced(seconds, rate=16000):
    t = np.linspace(0, seconds, int(seconds * rate), dtype=np.float32)
    return (0.1 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def silent(seconds, rate=16000):
    rng = np.random.default_rng(0)
    return rng.normal(0, 0.001, int(seconds * rate)).astype(np.float32)


def transcript_lines(rec):
    return [l for l in rec.txt_path.read_text().splitlines() if l.startswith("[")]


def test_fmt_hms():
    assert holler._fmt_hms(0) == "00:00:00"
    assert holler._fmt_hms(59.9) == "00:00:59"
    assert holler._fmt_hms(61) == "00:01:01"
    assert holler._fmt_hms(3661) == "01:01:01"


def test_meeting_paths_and_disambiguation(tmp_path):
    dt = datetime(2026, 8, 31, 14, 30)
    txt, wav = holler.meeting_paths(dt, tmp_path)
    assert txt.name == "2026-08-31-14-30-meeting.txt"
    assert wav.name == "2026-08-31-14-30-meeting.wav"
    txt.touch()
    txt2, _ = holler.meeting_paths(dt, tmp_path)
    assert txt2.name == "2026-08-31-14-30-meeting-2.txt"
    # A lone .wav must also block the stem.
    (tmp_path / "2026-08-31-14-30-meeting-2.wav").touch()
    txt3, _ = holler.meeting_paths(dt, tmp_path)
    assert txt3.name == "2026-08-31-14-30-meeting-3.txt"


def test_wav_header_layout():
    h = holler._wav_header(48000, 96000)
    assert len(h) == 44
    assert h[:4] == b"RIFF" and h[8:12] == b"WAVE" and h[36:40] == b"data"
    assert struct.unpack("<I", h[24:28])[0] == 48000       # sample rate
    assert struct.unpack("<I", h[40:44])[0] == 96000       # data bytes
    assert struct.unpack("<I", h[4:8])[0] == 36 + 96000    # RIFF size


def test_silent_chunk_skipped_but_clock_advances(tmp_path):
    asr = FakeASR()
    rec = make_recorder(tmp_path, asr)
    rec._chunk = [silent(2)]
    rec._chunk_samples = len(rec._chunk[0])
    rec._flush_chunk()
    assert asr.calls == 0, "ASR must not run on silence"
    assert transcript_lines(rec) == []

    rec._chunk = [voiced(2)]
    rec._chunk_samples = len(rec._chunk[0])
    rec._flush_chunk()
    rec._close_outputs()
    # Stamped at 2 s: the silent chunk still advanced the timeline.
    assert transcript_lines(rec) == ["[00:00:02] chunk 1"]


def test_worker_chunk_boundaries_and_incremental_append(tmp_path, monkeypatch):
    """5 × 0.5 s blocks with 1 s chunks → two in-loop flushes + one tail flush,
    with lines visible in the file *before* the worker is stopped."""
    monkeypatch.setattr(holler, "MEETING_CHUNK_SEC", 1.0)
    asr = FakeASR()
    rec = make_recorder(tmp_path, asr)
    for _ in range(5):
        rec._queue.put(voiced(0.5))

    worker = threading.Thread(target=rec._run, daemon=True)
    worker.start()
    deadline = time.monotonic() + 10
    while len(transcript_lines(rec)) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(transcript_lines(rec)) == 2, "chunks must append while still recording"

    rec._stop_evt.set()
    worker.join(timeout=10)
    assert not worker.is_alive()
    rec._close_outputs()

    assert transcript_lines(rec) == [
        "[00:00:00] chunk 1",
        "[00:00:01] chunk 2",
        "[00:00:02] chunk 3",  # 0.5 s tail, transcribed on stop
    ]

    # WAV: all 2.5 s streamed as int16, header patched to match.
    data = rec.wav_path.read_bytes()
    expected_bytes = 5 * int(0.5 * 16000) * 2
    assert len(data) == 44 + expected_bytes
    assert struct.unpack("<I", data[40:44])[0] == expected_bytes


def test_transcribe_error_does_not_kill_worker(tmp_path):
    class ExplodingASR:
        def transcribe(self, audio):
            raise RuntimeError("boom")

    rec = make_recorder(tmp_path, ExplodingASR())
    rec._chunk = [voiced(2)]
    rec._chunk_samples = len(rec._chunk[0])
    rec._flush_chunk()  # must not raise
    rec._close_outputs()
    assert transcript_lines(rec) == []


def test_no_heavy_asr_imported():
    """The whole suite must run without the ASR stack."""
    assert "onnx_asr" not in sys.modules
    assert "onnxruntime" not in sys.modules
