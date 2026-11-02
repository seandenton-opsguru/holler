"""Windows PowerShell 5.1 reads BOM-less .ps1 files as ANSI, so any non-ASCII
byte (an em dash's 0x94, say) can become a quote character and break parsing.
Every .ps1 in the repo must stay pure ASCII."""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_ps1_files_are_ascii_only():
    scripts = sorted(REPO_ROOT.rglob("*.ps1"))
    assert scripts, "expected at least bootstrap.ps1 and install.ps1"
    bad = []
    for path in scripts:
        for lineno, line in enumerate(path.read_bytes().split(b"\n"), 1):
            if any(b > 0x7F for b in line):
                bad.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
    assert not bad, "non-ASCII bytes in .ps1 files: " + ", ".join(bad)
