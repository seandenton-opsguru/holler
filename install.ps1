# holler installer: venv + deps (idempotent), default config, CLI shim on PATH.
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads BOM-less .ps1 files as
# ANSI, so a UTF-8 em dash or curly quote (byte 0x94 = right double quote) breaks parsing.
# Safe to re-run - it is also `holler update`'s dependency-sync step.
# Nothing is auto-started; prints next-steps at the end.
#
#   powershell -ExecutionPolicy Bypass -File install.ps1

$ErrorActionPreference = "Stop"

$RepoDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$HollerDir = Join-Path $env:LOCALAPPDATA "holler"
$VenvDir   = Join-Path $HollerDir "venv"
$BinDir    = Join-Path $HollerDir "bin"
$ConfigDir = Join-Path $env:APPDATA "holler"

function Say($msg)  { Write-Host "holler: $msg" }
function Warn($msg) { Write-Host "holler: $msg" -ForegroundColor Yellow }

# 1. Find a Python 3.10-3.13 (the py launcher ships with python.org installers;
# `python` covers Microsoft Store and manual installs). 3.14+ is rejected: pre-release
# Pythons have no onnxruntime wheels yet.
$Probe = "import sys; print('%d.%d' % sys.version_info[:2])"
$PyExe = $null; $PyArgs = @()
$Cands = @(
    @{Exe = "py"; Args = @("-3.13")}, @{Exe = "py"; Args = @("-3.12")},
    @{Exe = "py"; Args = @("-3.11")}, @{Exe = "py"; Args = @("-3.10")},
    @{Exe = "python"; Args = @()}
)
foreach ($cand in $Cands) {
    if (-not (Get-Command $cand.Exe -ErrorAction SilentlyContinue)) { continue }
    $prevEap = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"   # PS 5.1: native stderr + 2>$null + Stop = terminating error
        $v = & $cand.Exe @($cand.Args + @("-c", $Probe)) 2>$null
        if ($LASTEXITCODE -eq 0 -and $v -and ([version]$v -ge [version]"3.10") -and ([version]$v -lt [version]"3.14")) {
            $PyExe = $cand.Exe; $PyArgs = $cand.Args; break
        }
    } catch {
    } finally {
        $ErrorActionPreference = $prevEap
    }
}
if (-not $PyExe) {
    Warn "need Python 3.10-3.13 (3.12 recommended) - install from https://www.python.org/downloads/ (check 'Add to PATH'), then re-run"
    exit 1
}
Say "using Python $v"

# 2. git is required for the release-channel updater.
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Warn "git not found - install Git for Windows (https://git-scm.com/download/win); `holler update` needs it"
}

# 3. Venv + deps. onnx-asr[cpu,hub] pulls onnxruntime + huggingface_hub;
# pystray/pillow are for the tray; pynput for global hotkeys + typing.
if (-not (Test-Path (Join-Path $VenvDir "Scripts\python.exe"))) {
    Say "creating venv at $VenvDir"
    & $PyExe @($PyArgs + @("-m", "venv", $VenvDir))
    & (Join-Path $VenvDir "Scripts\python.exe") -m pip install --quiet --upgrade pip wheel
}
$VenvPy = Join-Path $VenvDir "Scripts\python.exe"
# PS 5.1 turns native stderr + 2>$null into a terminating error under "Stop"; a failed import is expected on a fresh venv.
$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $VenvPy -c "import onnx_asr, sounddevice, pynput, pystray" 2>$null
$ProbeExit = $LASTEXITCODE
$ErrorActionPreference = $prevEap
if ($ProbeExit -ne 0) {
    Say "installing deps (~250 MB, a minute or two)"
    & $VenvPy -m pip install --prefer-binary "onnx-asr[cpu,hub]" sounddevice numpy pynput pystray pillow
    if ($LASTEXITCODE -ne 0) { Warn "pip install failed"; exit 1 }
} else {
    Say "venv already has the deps - skipping pip install"
}

# 4. Default config.
New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
$ConfigPath = Join-Path $ConfigDir "config.json"
if (-not (Test-Path $ConfigPath)) {
    @'
{
  "microphone": null,
  "hotkeys": {"dictate": "ctrl_r", "dictate_type": null, "type_clipboard": null, "meeting": null},
  "model": "nvidia/parakeet-tdt-0.6b-v2",
  "silence_rms": 0.01,
  "update_channel": "yell"
}
'@ | Set-Content -Path $ConfigPath -Encoding UTF8
    Say "wrote default config to $ConfigPath"
}

# 5. CLI shim on PATH: holler.cmd -> venv python holler.py
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
@"
@echo off
"$VenvPy" "$RepoDir\holler.py" %*
"@ | Set-Content -Path (Join-Path $BinDir "holler.cmd") -Encoding ASCII

$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($UserPath -notlike "*$BinDir*") {
    [Environment]::SetEnvironmentVariable("Path", "$UserPath;$BinDir", "User")
    Say "added $BinDir to your PATH - open a NEW terminal for `holler` to resolve"
}

# 6. Summary.
Write-Host ""
Say "install complete."
Say "    - CLI:      $BinDir\holler.cmd"
Say "    - Config:   $ConfigPath"
Say "    - Start:    holler restart        (detached; first start downloads the ~630 MB model)"
Say "    - Autostart: holler startup on    (daemon + tray at sign-in)"
Say "    - Tray now:  start /b pythonw `"$RepoDir\holler_tray.py`""
Say "    - Logs:     holler logs"
