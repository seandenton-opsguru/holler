# holler 🎙️

**Talk to your Windows machine. It types.**

Hold a key. Say what you're thinking. Release. Your words appear at your cursor — in your editor, your terminal, your browser, your AI chat — transcribed in a couple hundred milliseconds by a state-of-the-art speech model running **entirely on your own machine**.

No cloud. No account. No subscription. No audio ever leaving your computer.

```powershell
irm https://raw.githubusercontent.com/sometimescreekventures/holler/main/bootstrap.ps1 | iex
```

## Why this exists

You speak at 150 words a minute and type at maybe 60. That gap used to not matter much — until talking to AI became half of how we work. Prompts, notes, replies, thinking out loud: it's all long-form text where your typing speed is the bottleneck, and where a slightly rambling sentence costs nothing because the model on the other end understands you anyway.

Windows has dictation built in — and it phones home, wants an account, and gives up in half the apps you live in. holler is built keystroke-first rather than app-integration-first, so it works in the places dictation tools traditionally surrender: terminals, Electron apps, and above all **remote desktops and VDIs** — the natural habitat of the Windows work machine.

And it's fast. The model stays loaded and warmed in a daemon, so there's no spin-up: release the key and a 3-second utterance is text in a few hundred milliseconds on an ordinary CPU. No GPU required. The whole install is ~250 MB of Python and a ~630 MB model.

## What you get

🎤 **Push-to-talk dictation** — hold Right Ctrl (configurable, per-action hotkeys), speak, release. Soft chirps confirm the mic opened and closed. The transcript arrives via the clipboard with a synthesized Ctrl+V — and whatever you had on the clipboard before is quietly **restored**. Smart touches you'll stop noticing: back-to-back dictations get joined with a space, filler artifacts like a hallucinated trailing "Thank you." are scrubbed, and silence is detected *before* the model can invent words for it.

⌨️ **The VDI escape hatch** — three more bindable hotkeys, each its own superpower:

| Bind a key to… | Get… |
| -------------- | ---- |
| `dictate` | Dictate → paste. The fast path for normal apps. **Right Ctrl** out of the box. |
| `dictate_type` | Dictate → *typed keystrokes*. Sails straight through remote clients that block or garble Ctrl+V. |
| `type_clipboard` | **Type your clipboard** as keystrokes — no mic involved. Prepared text goes into paste-hostile environments one keystroke at a time. Press again mid-flight to abort. |
| `meeting` | Tap to start/stop a meeting recording. |

📼 **A meeting recorder** — `holler meeting` (or a hotkey, or the tray) records long-form audio to a WAV while a timestamped transcript grows in real time, 30 seconds at a time. Stop, and the transcript opens — ready to paste into an LLM for a summary. Crash-safe by construction: pull the plug mid-meeting and you still have a playable WAV and every chunk transcribed so far.

🖱️ **A tray icon** that tells the truth — idle mic, red while dictating, amber during meetings, slashed if the daemon's down. Mic picker, hotkey pickers, updates, autostart toggle, all in the menu.

🚀 **Real releases** — installs pin to the stable channel (**yell** 📢); the adventurous ride the beta channel (**whisper** 🤫). `holler update` moves you forward — or deliberately *backward*, so a bad beta is never a trap. Betas soak on whisper, then get promoted to yell **bit-for-bit** — what you tested is exactly what ships.

## Can I trust this?

Fair question for a tool that listens to your mic and synthesizes keystrokes. The answers, verifiable in the source:

- **Audio never leaves your machine.** Transcription is local ONNX inference. The only network calls are the model download from Hugging Face (once) and `git fetch` when *you* check for updates.
- **The mic is only open while you hold the key** (or during a meeting you explicitly started, with a visible tray indicator). No wake words, no always-on listening.
- **The installer is ~100 commented lines** you can read before running it, and it pins you to a tagged, immutable release — not whatever the main branch woke up as.
- **Nothing updates itself behind your back.** `holler update` runs when you run it.
- **It's ~1,900 lines of readable Python** (one daemon file, one tray file), MIT-licensed. No admin rights required — everything lives under your user profile.

## Using it

|                                        |                                                               |
| -------------------------------------- | ------------------------------------------------------------- |
| `holler status`                        | daemon, mic, hotkeys, meeting, pending update — one screen    |
| `holler mic [NAME]`                    | pick a microphone (substring match)                           |
| `holler hotkey [ACTION] [KEY\|none]`   | bind `dictate`, `dictate_type`, `type_clipboard`, `meeting`   |
| `holler meeting`                       | start/stop a recording; `holler meeting status` for elapsed   |
| `holler update` / `holler channel`     | ride the release train; hop between yell and whisper          |
| `holler startup on\|off`               | daemon + tray at sign-in (HKCU Run key — no admin, no service) |
| `holler logs`                          | watch the daemon think                                        |

Config lives at `%APPDATA%\holler\config.json` — mic, hotkeys, VAD sensitivity (`silence_rms`), sound cues, typing speed for cranky VDIs (`type_key_delay`) — and applies live: the CLI nudges the running daemon to reload without dropping the warmed model.

## Requirements & honest scope

- **Windows 10/11**, any microphone, ~1 GB disk. **Python 3.10-3.13** (3.12 recommended) and Git for Windows (the bootstrap tells you exactly where to get them if they're missing). No GPU needed — CPU inference is many times realtime.
- Python 3.14+ and pre-release versions are not supported yet (no onnxruntime wheels). The Python install manager can install pre-release versions, so install 3.12 explicitly rather than taking its default.
- English-only for now (that's the Parakeet model's scope, not a design choice).
- Apps running **as Administrator** won't receive synthetic input from a non-elevated holler — that's Windows integrity levels, not a bug. Run holler elevated too if you must dictate into elevated windows.
- Hotkey misbehaves in some app? Avoid Alt-family keys (a lone Alt tap opens menu bars; Right Alt is AltGr on many layouts — holler doesn't offer them). `F13`–`F19` are gold if your keyboard has them; `Scroll Lock` and `Pause` are excellent squatters otherwise.
- Quiet speech dropped / silence hallucinating → nudge `silence_rms` down / up.

## Uninstall

```powershell
holler startup off
holler restart   # then close the daemon: taskkill /IM pythonw.exe (or just sign out)
Remove-Item -Recurse -Force "$env:LOCALAPPDATA\holler", "$env:APPDATA\holler"
```

Your recordings (`Documents\holler-meetings\`) stay — they're yours. Remove the PATH entry in Settings → Environment Variables if you're feeling thorough.

## Credits & license

Built on NVIDIA's [Parakeet-TDT-0.6B-v2](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v2) via [onnx-asr](https://github.com/istupakov/onnx-asr). Youngest sibling of [blurt](https://github.com/sometimescreekventures/blurt) (the macOS original) and [yap](https://github.com/sometimescreekventures/yap) (Linux) — the three share design DNA and trade bug fixes.

MIT — see [LICENSE](LICENSE). Use it, fork it, put it on every Windows box you're issued.
