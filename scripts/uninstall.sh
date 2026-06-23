#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# nighty-linux-headless - uninstaller / reset tool
#
#  Interactive menu:
#    [1] Full uninstall        remove EVERYTHING (service, $NIGHTY_HOME, Wine
#                              prefix, .env, both binaries) - back to pre-install.
#    [2] Reset configuration   delete ONLY the config/auth state (license, user
#                              token, bot token, lockdown marker) and restart so
#                              Nighty boots into a clean, fresh setup flow. Keeps
#                              the binary, the systemd service and everything else.
#    [3] Cancel                do nothing and exit.
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

if [ -t 1 ]; then B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; C=$'\033[36m'; R=$'\033[31m'; N=$'\033[0m'; else B=; G=; Y=; C=; R=; N=; fi
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
info() { printf '%s==>%s %s\n' "$C" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; }
need() { command -v "$1" >/dev/null 2>&1; }

# Learn the real runtime locations from .env (falling back to defaults).
if [ -f "$HERE/.env" ]; then set -a; . "$HERE/.env"; set +a; fi
: "${NIGHTY_HOME:=$HOME/.local/share/nighty}"
: "${WINEPREFIX:=$NIGHTY_HOME/prefix}"
: "${NIGHTY_STUB:=$HERE/Nighty_stub.exe}"
: "${NIGHTY_EXE:=$HERE/Nighty.exe}"
: "${WINE_BIN:=wine64}"
: "${DISPLAY_NUM:=99}"
case "$NIGHTY_STUB" in /*) : ;; ./*) NIGHTY_STUB="$HERE/${NIGHTY_STUB#./}" ;; esac
case "$NIGHTY_EXE"  in /*) : ;; ./*) NIGHTY_EXE="$HERE/${NIGHTY_EXE#./}" ;; esac

SUDO=""
if [ "$(id -u)" -ne 0 ] && need sudo; then SUDO="sudo"; fi

# Nighty's config dir lives inside the Wine prefix; the Windows user name varies.
find_appdata() {
  local d
  for d in "$WINEPREFIX"/drive_c/users/*/AppData/Roaming/"Nighty Selfbot"; do
    [ -d "$d" ] && { printf '%s' "$d"; return 0; }
  done
  return 1
}

stop_stack() {
  # Patterns are specific to our invocations so they can't match this script.
  pkill -f "scripts/bridge.py"      2>/dev/null || true
  pkill -f "scripts/webui_guard"    2>/dev/null || true
  pkill -f "scripts/enforce_config" 2>/dev/null || true
  pkill -f "Nighty_stub.exe"        2>/dev/null || true
  [ -n "${NIGHTY_STUB:-}" ] && pkill -f "$NIGHTY_STUB" 2>/dev/null || true
  pkill -f "Xvfb :$DISPLAY_NUM"     2>/dev/null || true
  ( WINEPREFIX="$WINEPREFIX" "${WINE_BIN%64}server" -k 2>/dev/null \
    || WINEPREFIX="$WINEPREFIX" wineserver -k 2>/dev/null ) || true
}

# ── [1] full uninstall ───────────────────────────────────────────────────────
full_uninstall() {
  printf '\n%sFull uninstall%s deletes EVERYTHING: the service, %s, .env and both binaries.\n' "$R" "$N" "$NIGHTY_HOME"
  printf 'There is no undo. Continue? [y/N] '
  read -r reply || reply=""
  case "$reply" in y|Y|yes|YES) : ;; *) echo "Cancelled."; return 1 ;; esac
  echo

  if need systemctl; then
    info "Removing systemd service…"
    $SUDO systemctl disable --now nighty.service >/dev/null 2>&1 || true
    $SUDO rm -f /etc/systemd/system/nighty.service
    $SUDO systemctl daemon-reload >/dev/null 2>&1 || true
    $SUDO systemctl reset-failed nighty.service >/dev/null 2>&1 || true
    ok "service stopped, disabled and removed"
  fi

  info "Stopping any running processes…"; stop_stack; sleep 2; ok "processes stopped"

  info "Deleting all data…"
  [ -d "$NIGHTY_HOME" ] && rm -rf "$NIGHTY_HOME" && ok "removed $NIGHTY_HOME"
  case "$WINEPREFIX" in
    "$NIGHTY_HOME"/*|"$NIGHTY_HOME") : ;;
    *) [ -d "$WINEPREFIX" ] && rm -rf "$WINEPREFIX" && ok "removed $WINEPREFIX" ;;
  esac
  [ -f "$HERE/.env" ]   && rm -f "$HERE/.env"   && ok "removed .env"
  [ -f "$NIGHTY_STUB" ] && rm -f "$NIGHTY_STUB" && ok "removed $(basename "$NIGHTY_STUB")"
  [ -f "$NIGHTY_EXE" ]  && rm -f "$NIGHTY_EXE"  && ok "removed $(basename "$NIGHTY_EXE")"

  echo
  printf '%sDone.%s Nighty is completely removed - the host is back to a pre-install state.\n' "$G" "$N"
  echo "To reinstall: copy your Nighty.exe into this folder and run scripts/install.sh"
}

# ── [2] reset configuration only ─────────────────────────────────────────────
reset_config() {
  local AD; AD="$(find_appdata || true)"
  printf '\n%sReset configuration%s deletes your license, account token, bot token and the\n' "$Y" "$N"
  printf 'authorization lock, then restarts Nighty for a fresh setup. The app stays installed.\n'
  printf 'Continue? [y/N] '
  read -r reply || reply=""
  case "$reply" in y|Y|yes|YES) : ;; *) echo "Cancelled."; return 1 ;; esac
  echo

  if [ -n "$AD" ] && [ -d "$AD" ]; then
    info "Clearing config + authorization state…"
    rm -f "$AD/auth.json" "$AD/nighty.config" "$AD/.setup_locked"
    rm -f "$AD"/auth.json.bak.* "$AD"/nighty.config.bak.* 2>/dev/null || true
    ok "removed license, tokens and the lockdown marker"
  else
    warn "could not find Nighty's config dir under $WINEPREFIX"
    warn "(nothing saved yet, or a different WINEPREFIX) - it will set up fresh on next start."
  fi

  info "Restarting Nighty so it boots into a fresh setup flow…"
  # Prefer a clean systemd restart when the service supervises Nighty. Use
  # non-interactive sudo so this never hangs on a password prompt; if that is not
  # possible we fall back to bouncing the backend directly (the supervisor — the
  # service's own run.sh loop, or a manual run.sh — relaunches it either way).
  restarted=0
  if need systemctl && systemctl is-active --quiet nighty.service; then
    if ${SUDO:+$SUDO -n} systemctl restart nighty.service 2>/dev/null; then
      ok "nighty.service restarted"; restarted=1
    fi
  fi
  if [ "$restarted" = 0 ]; then
    pkill -f "Nighty_stub.exe" 2>/dev/null || true
    ( WINEPREFIX="$WINEPREFIX" "${WINE_BIN%64}server" -k 2>/dev/null \
      || WINEPREFIX="$WINEPREFIX" wineserver -k 2>/dev/null ) || true
    if pgrep -f "scripts/run.sh --run" >/dev/null 2>&1 \
       || (need systemctl && systemctl is-active --quiet nighty.service); then
      ok "backend restarted (its supervisor will relaunch it into fresh setup)"
    else
      warn "no supervisor running - start Nighty again with:  bash scripts/run.sh"
    fi
  fi

  echo
  printf '%sDone.%s Open the Web UI ( http://<host-ip>:%s/ ) to set Nighty up again from step 1.\n' \
    "$G" "$N" "${BRIDGE_PORT:-8088}"
}

# ── menu ─────────────────────────────────────────────────────────────────────
printf '\n%snighty-linux-headless - uninstaller / reset%s\n\n' "$B" "$N"
echo "  What would you like to do?"
echo "    ${B}[1]${N} Full uninstall        remove everything (binary, service, data, prefix)"
echo "    ${B}[2]${N} Reset configuration   wipe tokens/license/auth only, then restart for fresh setup"
echo "    ${B}[3]${N} Cancel"
echo
printf "  Enter choice [1/2/3]: "
if ! read -r choice; then choice=3; fi

case "${choice:-3}" in
  1) full_uninstall ;;
  2) reset_config ;;
  3|"") echo "Cancelled - nothing was changed." ;;
  *) echo "Unrecognised choice '$choice' - nothing was changed."; exit 1 ;;
esac
