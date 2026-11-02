# holler bootstrap - zero-to-dictating on a fresh Windows machine.
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads BOM-less .ps1 files as
# ANSI, so a UTF-8 em dash or curly quote (byte 0x94 = right double quote) breaks parsing.
#
#   irm https://raw.githubusercontent.com/sometimescreekventures/holler/main/bootstrap.ps1 | iex
#
# What it does (idempotent - safe to re-run any time):
#   1. checks for git + Python 3.10-3.13 (tells you where to get them if missing)
#   2. clones the repo to %LOCALAPPDATA%\holler\app (override with $env:HOLLER_DIR)
#   3. pins a fresh clone to the latest stable release (the `yell` channel tag)
#   4. runs install.ps1 (venv + deps + config + CLI shim on PATH)
#   5. registers daemon + tray to start at sign-in, and starts them now

$ErrorActionPreference = "Stop"

$RepoUrl   = "https://github.com/sometimescreekventures/holler.git"
$HollerDir = if ($env:HOLLER_DIR) { $env:HOLLER_DIR } else { Join-Path $env:LOCALAPPDATA "holler\app" }

function Say($msg)  { Write-Host "holler-bootstrap: $msg" }
function Warn($msg) { Write-Host "holler-bootstrap: $msg" -ForegroundColor Yellow }

# 1. Prerequisites ------------------------------------------------------------
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Warn "git is required - install Git for Windows: https://git-scm.com/download/win"
    Warn "then re-run this bootstrap."
    return
}
$HavePython = $false
$Probe = "import sys; print('%d.%d' % sys.version_info[:2])"
# Only 3.10-3.13: newer (pre-release) Pythons have no onnxruntime wheels yet.
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
            $HavePython = $true; break
        }
    } catch {
    } finally {
        $ErrorActionPreference = $prevEap
    }
}
if (-not $HavePython) {
    Warn "Python 3.10-3.13 is required (3.12 recommended) - https://www.python.org/downloads/ (check 'Add python.exe to PATH')"
    Warn "Python 3.14+ / pre-release versions are not supported yet. Then re-run this bootstrap."
    return
}

# 2. Clone or refresh ---------------------------------------------------------
$Fresh = $false
if (Test-Path (Join-Path $HollerDir ".git")) {
    Say "existing checkout at $HollerDir"
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"   # detached HEAD: symbolic-ref writes to stderr (terminating under Stop on PS 5.1)
    $branch = git -C $HollerDir symbolic-ref --short HEAD 2>$null
    $ErrorActionPreference = $prevEap
    $dirty  = git -C $HollerDir status --porcelain
    if ($branch -eq "main" -and -not $dirty) {
        Say "refreshing it (git pull --ff-only)"
        git -C $HollerDir pull --ff-only --quiet
    } else {
        Warn "checkout is on branch '$branch' or has local changes - leaving it untouched"
    }
} else {
    Say "cloning to $HollerDir"
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $HollerDir) | Out-Null
    git clone --quiet $RepoUrl $HollerDir
    $Fresh = $true
}
Set-Location $HollerDir

# 3. Fresh installs start on the stable release, not main's tip ---------------
if ($Fresh) {
    $yell = git rev-parse --verify --quiet 'refs/tags/yell^{commit}'
    if ($yell) {
        git reset --hard --quiet $yell   # stays on main: exactly the state `holler update` maintains
        $v = git tag --points-at HEAD --sort=-v:refname --list 'v*' | Select-Object -First 1
        Say "pinned to the stable release: $v (yell channel)"
    } else {
        Warn "no release cut yet - running main's tip; 'holler update' takes over once one exists"
    }
}

# 4. Install ------------------------------------------------------------------
Say "running install.ps1 - first run downloads ~250 MB of Python deps"
powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $HollerDir "install.ps1")
if ($LASTEXITCODE -ne 0) { Warn "install failed"; return }

# 5. Autostart + start now ----------------------------------------------------
$VenvPy  = Join-Path $env:LOCALAPPDATA "holler\venv\Scripts\python.exe"
$VenvPyW = Join-Path $env:LOCALAPPDATA "holler\venv\Scripts\pythonw.exe"
Say "registering daemon + tray at sign-in"
& $VenvPy (Join-Path $HollerDir "holler.py") startup on
Say "starting the daemon + tray (first start downloads the Parakeet model, ~630 MB)"
Start-Process -FilePath $VenvPyW -ArgumentList "`"$(Join-Path $HollerDir 'holler.py')`"" -WindowStyle Hidden
Start-Process -FilePath $VenvPyW -ArgumentList "`"$(Join-Path $HollerDir 'holler_tray.py')`"" -WindowStyle Hidden

Write-Host ""
Say "done. Once the model finishes downloading: hold RIGHT CTRL, speak, release -"
Say "the text lands in the focused window. Watch progress with:  holler logs"
Say "    holler status      daemon overview       holler meeting   record + transcribe a meeting"
Say "    holler mic NAME    pick a microphone     holler update    move to the latest release"
Say "(open a NEW terminal first if `holler` isn't recognized - PATH just changed)"
