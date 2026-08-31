# CLAUDE.md

Guidance for Claude when working on this repo.

## What this is

Windows push-to-talk dictation daemon. Hold the configured hotkey, speak, release — Parakeet-TDT-0.6B-v2 transcribes locally (onnx-asr, int8, CPU) and the result lands at the cursor: clipboard + synthesized Ctrl+V (with prior clipboard restored) for the `dictate` action, raw keystrokes for `dictate_type` / `type_clipboard` (the VDI path).

Youngest sibling of **blurt** (macOS, `sometimescreekventures/blurt`) and **yap** (Linux, `sometimescreekventures/yap`). The three share design DNA — engine seam, peak-windowed VAD, transcript edge cleanup, continuation spacing, meeting recorder, status-file contract, floating-channel-tag releases — and trade bug fixes. A fix to `cleanup()` or the meeting recorder here almost certainly belongs in the siblings too (and vice versa).

Implementation is `holler.py` (daemon + CLI) plus `holler_tray.py` (pystray tray — deliberately a second process: it does NOT import holler.py; the status file is its read contract, the CLI its write contract, so a tray crash can never break dictation). Pure-logic tests in `tests/` (no CI); `tests/conftest.py` stubs `sounddevice`/`pynput` in `sys.modules` before `import holler`, so the suite runs on any OS in well under a second: `python3 -m pytest tests/`.

## Windows-specific landmines (learned the hard way — do not "simplify")

- **`os.kill(pid, 0)` TERMINATES the target process on Windows.** All liveness probes go through `pid_alive()` (OpenProcess + GetExitCodeProcess). `test_tray.py` has a regression guard.
- **No POSIX signals.** Where yap uses SIGHUP/SIGUSR1, the CLI writes a small JSON **control file** (`send_control` / `poll_control`) that the daemon's main loop polls every 0.2 s. Commands: `meeting`, `reload`, `quit`. Stale (>30 s) and unknown commands are discarded.
- **Windows auto-repeats key-down** while a key is held; `HotkeyController._on_press` guards with held-state.
- **The clipboard is a global mutex** another app may hold — every open retries (`_open_clipboard_retry`). `set_clipboard` hands ownership of the GlobalAlloc'd handle to the system on success; free it only on failure.
- **Elevated windows ignore synthetic input** from a non-elevated process (UIPI). Documented in the README; not fixable from here.
- **pythonw has no stdout** — `run_daemon` detects it and redirects to `%LOCALAPPDATA%\holler\holler.log` (what `holler logs` tails).
- **Alt keys are not offered as hotkeys** (menu-bar focus; AltGr layouts). Right Ctrl is the default; F13–F19 / Scroll Lock / Pause are the good citizens.

## Architecture notes (family-inherited)

- **Engine seam.** `OnnxParakeetASR`: 16 kHz float32 ndarray in, text out, all inference serialized on `_asr_lock` (dictation + meeting chunks must not run concurrently). Config keeps the `nvidia/...` HF id; `_ONNX_MODEL_ALIASES` maps to onnx-asr registry names. Warmup with 0.5 s silence at load is the biggest first-utterance latency win — don't remove it.
- **Audio at the mic's native rate**, resampled to 16 kHz in `resample_to_16k` (np.interp) after capture. Forcing 16 kHz at the stream breaks some mics (Yeti X, on Linux at least).
- **VAD is peak-windowed RMS** (`buffer_peak_rms`): loudest 100 ms window vs `silence_rms`, NOT whole-buffer mean — the press → think → speak pattern leaves buffers mostly silent and mean-RMS rejects real speech.
- **`cleanup()` scrubs edges only**, with `\b` anchors (without them "umbrella" → "brella"). Mid-utterance content is never touched.
- **Continuation spacing**: `" "` prepended when the last delivery was <15 s ago and the new transcript starts alphanumeric. `_last_paste_ts` is module-level on purpose (survives reload; last-writer-wins is fine).
- **Meeting recorder**: WAV header written by hand and re-patched per chunk so a hard kill leaves a playable file; one bad chunk never ends the meeting; transcript lines flush per write. One instance for the daemon's lifetime — meetings survive config reloads.
- **Status file** (`%LOCALAPPDATA%\holler\run\status.json`): the daemon's one outward-facing contract — `{pid, version, channel, mic, devices, hotkeys, dictating, update, meeting_active, meeting_started_at, wav_path, txt_path}`. Atomic tmp+rename writes; `devices` is published so the tray never shells out for them (regression-guarded).
- **CLI fast path**: `_COMMANDS` dispatch happens before any heavy import. No top-level `import onnx_asr` anywhere.
- **Releases**: floating channel tags **yell** (stable) / **whisper** (beta) via `release.sh` (runs on the dev machine, needs gh). `holler update` = fetch tags → resolve channel tag to SHA once → `reset --hard` → re-run `install.ps1` as dep sync → restart daemon. Fresh bootstraps pin to yell.

## Running / testing

- Dev machine (mac/linux): `python3 -m pytest tests/` — pure logic only, no audio/hooks/model.
- Windows box: `powershell -ExecutionPolicy Bypass -File install.ps1`, then `holler restart`; `holler logs -f` to watch. Venv at `%LOCALAPPDATA%\holler\venv`, checkout wherever it was cloned (bootstrap uses `%LOCALAPPDATA%\holler\app`).
- **Deferred — do not implement unprompted**: dictate_lock (hands-free toggle), sound volume control (winsound has none), Wayland-style key suppression, single-instance mutex beyond the status-file check.
