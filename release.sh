#!/usr/bin/env bash
# holler release tool — staged releases via floating channel tags.
# Runs on the dev machine (mac/linux/git-bash); gh must be authenticated.
#
#   ./release.sh [--minor|--major]   cut a beta: tag vX.Y.Z, GitHub prerelease,
#                                    move the whisper (beta) channel tag to it
#   ./release.sh promote             move yell (stable) to what whisper points at,
#                                    flip the GitHub release to latest
#   ./release.sh status              show both channels + unreleased work
#
# Channels: yell = stable (default for updaters), whisper = beta.
# Every release is an immutable v* tag; promotion never rebuilds — it re-points
# yell at the exact commit whisper already soaked on.

set -euo pipefail
cd "$(dirname "$0")"

STABLE=yell
BETA=whisper

say() { printf 'release: %s\n' "$*"; }
die() { printf 'release: %s\n' "$*" >&2; exit 1; }

need_clean_main() {
  command -v gh >/dev/null 2>&1 || die "gh is required (and must be authenticated)"
  git fetch origin --tags --force --quiet
  [ "$(git symbolic-ref --short HEAD)" = "main" ] || die "must be on main"
  [ -z "$(git status --porcelain)" ] || die "working tree is dirty"
  [ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] \
    || die "main != origin/main — push or pull first (never tag unpushed work)"
}

# Highest v* tag pointing at the given commit-ish.
version_at() {
  git tag --points-at "$1" --sort=-v:refname --list 'v*' | head -1
}

channel_commit() {
  git rev-parse --verify --quiet "refs/tags/$1^{commit}" || true
}

next_version() {
  local bump="$1" latest major minor patch
  latest=$(git tag --list 'v*' --sort=-v:refname | head -1)
  if [ -z "$latest" ]; then
    echo "v0.1.0"
    return
  fi
  IFS=. read -r major minor patch <<<"${latest#v}"
  case "$bump" in
    major) echo "v$((major + 1)).0.0" ;;
    minor) echo "v${major}.$((minor + 1)).0" ;;
    patch) echo "v${major}.${minor}.$((patch + 1))" ;;
  esac
}

cmd_cut() {
  local bump=patch
  case "${1:-}" in
    --minor) bump=minor ;;
    --major) bump=major ;;
    "") ;;
    *) die "unknown flag ${1} (use --minor or --major)" ;;
  esac
  need_clean_main
  local v
  v=$(next_version "$bump")
  git tag -a "$v" -m "holler $v"
  git push --quiet origin "$v"
  gh release create "$v" --title "holler $v" --generate-notes --prerelease
  git tag -f "$BETA" "$v^{commit}" >/dev/null
  git push --quiet --force origin "$BETA"
  say "🤫 $BETA -> $v (beta cut; promote with ./release.sh promote)"
}

cmd_promote() {
  need_clean_main
  local beta_commit stable_commit v
  beta_commit=$(channel_commit "$BETA")
  [ -n "$beta_commit" ] || die "no $BETA tag — cut a release first"
  stable_commit=$(channel_commit "$STABLE")
  v=$(version_at "$beta_commit")
  [ "$beta_commit" != "$stable_commit" ] || die "$STABLE is already at $v"
  git tag -f "$STABLE" "$beta_commit" >/dev/null
  git push --quiet --force origin "$STABLE"
  gh release edit "$v" --prerelease=false --latest >/dev/null
  say "📢 $STABLE -> $v"
}

cmd_status() {
  git fetch origin --tags --force --quiet 2>/dev/null || true
  local ch commit v
  for ch in "$STABLE" "$BETA"; do
    commit=$(channel_commit "$ch")
    if [ -n "$commit" ]; then
      v=$(version_at "$commit")
      printf '%-8s %s (%s)\n' "$ch" "${v:-?}" "$(git rev-parse --short "$commit")"
    else
      printf '%-8s (no tag yet)\n' "$ch"
    fi
  done
  if [ -n "$(channel_commit "$BETA")" ]; then
    say "unreleased on main: $(git rev-list --count "$BETA"..origin/main) commit(s)"
  fi
}

case "${1:-cut}" in
  cut) shift 2>/dev/null || true; cmd_cut "${1:-}" ;;
  --minor|--major) cmd_cut "$1" ;;
  promote) cmd_promote ;;
  status) cmd_status ;;
  *) die "usage: ./release.sh [--minor|--major] | promote | status" ;;
esac
