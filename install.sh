#!/bin/sh
# Install wake-watcher on this machine.
#
#   ./install.sh --dry-run     print every change, make none
#   ./install.sh               do it
#   ./install.sh --uninstall   put the machine back
#
# This only installs the `wake-watcherctl` command. It does not start
# anything and it does not register any persistent service — wake-watcher
# is opt-in per project. After this script finishes:
#
#   cd /path/to/the/project/you/want/watched
#   wake-watcherctl dry              # observe for a while, wakes nothing
#   wake-watcherctl init              # once you trust it: install a launchd/systemd
#                                      # service scoped to *this* directory
#
# ## Why `init` is a separate step, run from inside the project
#
# wake-watcher watches one project at a time. The project it watches is
# WAKE_WATCHER_PROJECT_ROOT, and that value has no safe default: the code's
# own fallback derives it from *where wake-watcher itself is installed*,
# which is unrelated to what you want it to monitor once wake-watcher lives
# in a shared location instead of being copied into every project's
# `.claude/`. So `wake-watcherctl init` bakes the current directory in as
# WAKE_WATCHER_PROJECT_ROOT unconditionally, every time. This script never
# touches that value — it only puts the tool on PATH.
#
# ## Two rules, both borrowed from a sibling install script that already paid
#    for them
#
# 1. **Preflight before touching anything.** A half-installed watcher is
#    worse than no watcher — better to fail loudly before writing a single
#    file than to leave a symlink pointing at a missing interpreter.
# 2. **Never replace a file this script did not write.** A name collision at
#    `~/.local/bin/wake-watcherctl` is an error, not a silent takeover.
set -eu

PREFIX=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
BIN_DIR="$HOME/.local/bin"

DRY=0
UNINSTALL=0
for arg in "$@"; do
  case "$arg" in --dry-run) DRY=1 ;; --uninstall) UNINSTALL=1 ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) printf 'install.sh: unknown argument: %s\n' "$arg" >&2; exit 2 ;;
  esac
done

BOLD=$(printf '\033[1m'); DIM=$(printf '\033[2m')
RED=$(printf '\033[31m'); GREEN=$(printf '\033[32m'); YELLOW=$(printf '\033[33m')
RESET=$(printf '\033[0m')

say() { printf '%s\n' "$*"; }
step() { printf '\n%s%s%s\n' "$BOLD" "$*" "$RESET"; }
ok() { printf '  %s✓%s %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '  %s!%s %s\n' "$YELLOW" "$RESET" "$*"; }
die() { printf '\n%s%s%s\n' "$RED" "$*" "$RESET" >&2; exit 1; }
would() { printf '  %swould run:%s %s\n' "$DIM" "$RESET" "$*"; }

# A marker so a second install can tell its own symlink from something a
# person put there by hand, and so uninstall only ever removes what this
# script created.
MARKER="wake-watcher-install"

# ---------------------------------------------------------------------------
# preflight — everything that must be true before one byte is written
# ---------------------------------------------------------------------------
preflight() {
  step "1) preflight"
  missing=0

  if command -v python3 >/dev/null 2>&1; then
    pyver=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
    case "$pyver" in
      3.9|3.1[0-9]) ok "python3 $pyver" ;;
      *) warn "python3 $pyver — wake-watcher needs 3.9+"; missing=1 ;;
    esac
  else
    warn "python3 not found"; missing=1
  fi

  if [ -f "$PREFIX/src/wake_watcher/wake_watcher.py" ]; then
    ok "src/wake_watcher/wake_watcher.py in place"
  else
    warn "missing: $PREFIX/src/wake_watcher/wake_watcher.py"; missing=1
  fi

  if [ -x "$PREFIX/bin/wake-watcherctl" ]; then
    ok "bin/wake-watcherctl in place"
  else
    warn "missing or not executable: $PREFIX/bin/wake-watcherctl"; missing=1
  fi

  case "$(uname -s)" in
    Darwin) ok "platform macOS (launchd)" ;;
    Linux)  ok "platform Linux (systemd)" ;;
    *) warn "unsupported platform: $(uname -s) — 'wake-watcherctl init' will refuse to run, but start/stop/dry/once still work directly"; ;;
  esac

  [ "$missing" -eq 0 ] || die "preflight failed — fix every line above first.
A half-installed watcher is worse than none: a symlink can end up in place
while the code it points at is missing or unrunnable."
}

# ---------------------------------------------------------------------------
install_wrapper() {
  step "2) install wake-watcherctl -> $BIN_DIR"
  mkdir -p "$BIN_DIR" 2>/dev/null || true
  source="$PREFIX/bin/wake-watcherctl"
  target="$BIN_DIR/wake-watcherctl"
  if [ -e "$target" ] && [ ! -L "$target" ]; then
    # A real file here is either an older hand-made install of this same
    # tool, or somebody else's program with an unlucky name. Content decides,
    # not the name.
    if cmp -s "$source" "$target"; then
      if [ "$DRY" -eq 1 ]; then
        would "ln -sf $source $target   ${DIM}(identical content, replacing with a symlink)${RESET}"
      else
        ln -sf "$source" "$target"
        ok "linked (was an identical standalone copy)"
      fi
    else
      die "$target already exists and differs from the one in this checkout — refusing to overwrite.
It may be someone else's program, or a version you edited by hand. Diff first:
  diff $source $target"
    fi
  else
    if [ "$DRY" -eq 1 ]; then
      would "ln -s $source $target"
    else
      ln -sf "$source" "$target"
      ok "wake-watcherctl -> $target"
    fi
  fi
}

# ---------------------------------------------------------------------------
selfcheck() {
  step "3) next steps"
  if [ "$DRY" -eq 1 ]; then
    would "wake-watcherctl dry     ${DIM}(run from inside the project you want watched)${RESET}"
    return
  fi
  case ":$PATH:" in
    *":$BIN_DIR:"*) ok "$BIN_DIR is on PATH" ;;
    *) warn "$BIN_DIR is not on PATH — add it to your shell profile" ;;
  esac
  printf '\n%snothing is running yet — wake-watcher is opt-in per project:%s\n' "$YELLOW" "$RESET"
  say "  1) cd into the project you want watched"
  say "  2) wake-watcherctl dry        ${DIM}classify only, wakes nothing — read docs before turning this on for real${RESET}"
  say "  3) wake-watcherctl init       ${DIM}installs a launchd/systemd service scoped to *that* directory${RESET}"
  say ""
  say "  ${DIM}uninstall a running instance: wake-watcherctl uninstall <name>${RESET}"
}

# ---------------------------------------------------------------------------
uninstall() {
  step "uninstall"
  target="$BIN_DIR/wake-watcherctl"
  if [ -L "$target" ] && [ "$(readlink "$target")" = "$PREFIX/bin/wake-watcherctl" ]; then
    if [ "$DRY" -eq 1 ]; then would "rm $target"; else rm -f "$target"; ok "removed $target"; fi
  else
    warn "$target is not a symlink into this checkout — leaving it alone"
  fi

  # This only removes the command. Any per-project services created by
  # `wake-watcherctl init` are left running on purpose — killing a monitoring
  # daemon as a side effect of removing the CLI would be a surprise, and
  # each one needs its own clean bootout anyway.
  leftover=0
  if [ -d "$HOME/Library/LaunchAgents" ]; then
    for plist in "$HOME"/Library/LaunchAgents/com.wake-watcher.*.plist; do
      [ -e "$plist" ] || continue
      leftover=1
      name=$(basename "$plist" .plist); name=${name#com.wake-watcher.}
      warn "still installed: $name — run: wake-watcherctl uninstall $name"
    done
  fi
  if [ -d /etc/systemd/system ]; then
    for unit in /etc/systemd/system/wake-watcher-*.service; do
      [ -e "$unit" ] || continue
      leftover=1
      name=$(basename "$unit" .service); name=${name#wake-watcher-}
      warn "still installed: $name — run: wake-watcherctl uninstall $name"
    done
  fi
  [ "$leftover" -eq 1 ] || ok "no per-project services left registered"

  printf '\n%sstate and logs kept at:%s\n' "$BOLD" "$RESET"
  say "  \${XDG_STATE_HOME:-\$HOME/.local/state}/wake-watcher/"
  say "  ${DIM}deleting someone's ledger/watermark history is not something to do implicitly, so it's left for you.${RESET}"
}

# ---------------------------------------------------------------------------
if [ "$UNINSTALL" -eq 1 ]; then
  uninstall
  exit 0
fi

[ "$DRY" -eq 1 ] && printf '%sdry run — nothing will be written%s\n' "$YELLOW" "$RESET"
preflight
install_wrapper
selfcheck
[ "$DRY" -eq 1 ] && printf '\n%sdry run finished, nothing changed.%s\n' "$YELLOW" "$RESET"
exit 0
