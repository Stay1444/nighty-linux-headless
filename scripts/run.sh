#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# nighty-linux-headless — orchestrator
#
#  This single script brings up the WHOLE stack and keeps it alive:
#    • a virtual display (Xvfb)
#    • config enforcement (notifications off, Web UI on) — pre-launch + continuous
#    • the LAN Web UI bridge (port 8088)
#    • the Nighty backend (Wine, headless) with auto-relaunch
#
#  Run it with no arguments and it asks whether to start once or to install
#  itself as a systemd service (autostart on every boot) — and does the setup
#  for you. With --run it just starts the stack (this is what the service uses).
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ── load .env ────────────────────────────────────────────────────────────────
set -a; [ -f "$HERE/.env" ] && . "$HERE/.env"; set +a

# ── defaults ─────────────────────────────────────────────────────────────────
: "${NIGHTY_HOME:=$HOME/.local/share/nighty}"
: "${WINEPREFIX:=$NIGHTY_HOME/prefix}"
: "${NIGHTY_STUB:=$HERE/Nighty_stub.exe}"
: "${WINE_BIN:=wine64}"
: "${DISPLAY_NUM:=99}"
: "${STUB_PORT:=8765}"
: "${BRIDGE_PORT:=8088}"
: "${ENFORCE_WEBUI:=1}"
# Max seconds to wait for Nighty's stub control server to answer after
# launching Wine before assuming the boot hung and retrying. Some Wine builds
# can stall during first-prefix init well before Nighty's own code ever runs.
: "${BOOT_TIMEOUT:=300}"
mkdir -p "$NIGHTY_HOME"

export WINEPREFIX
export WINEARCH=win64
export WINEDEBUG=-all
export DISPLAY=":$DISPLAY_NUM"
export NIGHTY_STUB_PORT="$STUB_PORT"
export NIGHTY_STUB_LOG="${NIGHTY_STUB_LOG:-Z:$NIGHTY_HOME/stub_webview.log}"
# Headless DLL overrides. Nighty's GUI is stubbed out and the backend is pure
# Python, so we disable the Windows components that only crash or hang on a
# headless box: .NET (mscoree), Internet Explorer (mshtml — its first-run calls
# the unimplemented advpack.RegInstall and aborts the process), and desktop
# integration (winemenubuilder). Override via WINEDLLOVERRIDES in .env if needed.
export WINEDLLOVERRIDES="${WINEDLLOVERRIDES:-mscoree=d;mshtml=d;winemenubuilder.exe=d}"

# Architecture-aware tuning: Box64 knobs only matter when emulating x86-64.
case "$(uname -m)" in
  x86_64|amd64) ARCH_DESC="x86-64 (native Wine)" ;;
  *) ARCH_DESC="$(uname -m) (Wine over Box64)"
     export BOX64_NOBANNER=1 BOX64_LOG=0
     # Dynarec settings for the emulated x86-64 backend. These default to the
     # SAFE/conservative values: Nighty bundles a Go-based tls-client whose runtime
     # crashes on start ("panic on system stack" in schedinit) under more
     # aggressive dynarec (BIGBLOCK/CALLRET on, relaxed SAFEFLAGS, weaker
     # STRONGMEM). They are exposed here so you can experiment from .env at your own
     # risk, but the defaults below are the ones proven stable on this build.
     : "${BOX64_DYNAREC_BIGBLOCK:=0}"; : "${BOX64_DYNAREC_STRONGMEM:=3}"
     : "${BOX64_DYNAREC_SAFEFLAGS:=2}"; : "${BOX64_DYNAREC_CALLRET:=0}"
     export BOX64_DYNAREC_BIGBLOCK BOX64_DYNAREC_STRONGMEM \
            BOX64_DYNAREC_SAFEFLAGS BOX64_DYNAREC_CALLRET ;;
esac
ulimit -s 8192 2>/dev/null || true

log() { echo "[run] $(date '+%H:%M:%S') $*"; }

# ── autostart (systemd) ──────────────────────────────────────────────────────
setup_autostart() {
  if ! command -v systemctl >/dev/null 2>&1; then
    log "systemd not found on this host — can't set up autostart automatically."
    log "You can still run it manually:  bash scripts/run.sh"
    return 1
  fi
  local SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
  local unit=/etc/systemd/system/nighty.service

  # Make sure no manual instance is holding the ports before the service starts.
  pkill -f "scripts/bridge.py" 2>/dev/null || true
  pkill -f "$NIGHTY_STUB" 2>/dev/null || true
  pkill -f "Xvfb :$DISPLAY_NUM" 2>/dev/null || true
  ( "${WINE_BIN%64}server" -k 2>/dev/null || wineserver -k 2>/dev/null ) || true
  sleep 2

  log "installing systemd service → $unit"
  $SUDO tee "$unit" >/dev/null <<EOF
[Unit]
Description=Nighty headless (backend + Web UI bridge)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(id -un)
WorkingDirectory=$HERE
ExecStart=/usr/bin/env bash $HERE/scripts/run.sh --run
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

  $SUDO systemctl daemon-reload || { log "daemon-reload failed"; return 1; }
  $SUDO systemctl enable --now nighty.service || { log "enabling service failed"; return 1; }
  echo
  log "Autostart is ON — Nighty now starts on every boot and is running already."
  echo "    status:   systemctl status nighty"
  echo "    live log: journalctl -u nighty -f"
  echo "    panel:    http://<this-host-ip>:$BRIDGE_PORT/"
  echo "    turn off: sudo systemctl disable --now nighty"
}

# ── the stack ────────────────────────────────────────────────────────────────
_CLEANED=0
cleanup() {
  [ "$_CLEANED" = 1 ] && return 0; _CLEANED=1
  log "shutting down…"
  pkill -f "scripts/bridge.py" 2>/dev/null || true
  pkill -f "$NIGHTY_STUB" 2>/dev/null || true
  ( "${WINE_BIN%64}server" -k 2>/dev/null || wineserver -k 2>/dev/null ) || true
  pkill -f "Xvfb :$DISPLAY_NUM" 2>/dev/null || true
}

run_stack() {
  [ -f "$NIGHTY_STUB" ] || { echo "[run] FATAL: $NIGHTY_STUB not found. Run scripts/install.sh first." >&2; exit 1; }
  trap cleanup EXIT
  trap 'exit 0' INT TERM

  log "host architecture: $ARCH_DESC"

  # Pre-launch config enforcement (notifications off, Web UI creds + web:true).
  python3 "$HERE/scripts/enforce_config.py" || true

  # Virtual display. Xvfb refuses to auto-create /tmp/.X11-unix unless running
  # as root, so on a non-root install (or any host where /tmp is freshly
  # mounted/cleared) it silently fails to bind. create the socket dir
  # ourselves rather than relying on Xvfb to do it.
  mkdir -p /tmp/.X11-unix
  chmod 1777 /tmp/.X11-unix 2>/dev/null || true
  pkill -f "Xvfb :$DISPLAY_NUM" 2>/dev/null || true
  sleep 1
  Xvfb ":$DISPLAY_NUM" -screen 0 1366x768x24 -nolisten tcp >/dev/null 2>&1 &
  sleep 2

  # Continuous Web UI hard-enforcement.
  if [ "$ENFORCE_WEBUI" = "1" ]; then
    python3 "$HERE/scripts/webui_guard.py" >>"$NIGHTY_HOME/guard.log" 2>&1 &
  fi

  # Web UI bridge — kept alive in its own loop.
  ( while true; do
      python3 "$HERE/scripts/bridge.py" >>"$NIGHTY_HOME/bridge.log" 2>&1
      echo "[bridge] $(date '+%H:%M:%S') exited — restarting in 3s" >>"$NIGHTY_HOME/bridge.log"
      sleep 3
    done ) &
  log "Web UI bridge up — open  http://<this-host-ip>:$BRIDGE_PORT/"

  # Backend — relaunch forever (covers a UI-triggered restart/close). A
  # watchdog kills and retries if the stub control server never answers
  # within BOOT_TIMEOUT — some Wine builds stall during first-prefix init
  # with no error, well before Nighty's own code would ever hang or crash.
  ( while true; do
      python3 "$HERE/scripts/enforce_config.py" >/dev/null 2>&1 || true
      log "launching backend ($NIGHTY_STUB)…"
      "$WINE_BIN" "$NIGHTY_STUB" >>"$NIGHTY_HOME/backend.log" 2>&1 &
      BACKEND_PID=$!

      (
        waited=0
        while [ "$waited" -lt "$BOOT_TIMEOUT" ]; do
          kill -0 "$BACKEND_PID" 2>/dev/null || exit 0
          # Bash builtin TCP probe (no curl/wget dependency): the stub is up
          # once something accepts a connection on STUB_PORT.
          if (exec 3<>"/dev/tcp/127.0.0.1/${STUB_PORT}") 2>/dev/null; then
            exec 3<&- 3>&-
            exit 0
          fi
          sleep 5
          waited=$((waited + 5))
        done
        if kill -0 "$BACKEND_PID" 2>/dev/null; then
          log "backend boot timed out after ${BOOT_TIMEOUT}s (stub never answered on :${STUB_PORT}) — killing and retrying."
          kill -9 "$BACKEND_PID" 2>/dev/null || true
        fi
      ) &
      WATCHDOG_PID=$!

      wait "$BACKEND_PID" 2>/dev/null || true
      kill "$WATCHDOG_PID" 2>/dev/null || true
      log "backend exited — relaunching in 3s (persistence)."
      ( "${WINE_BIN%64}server" -k 2>/dev/null || wineserver -k 2>/dev/null ) || true
      sleep 3
    done ) &

  wait
}

# ── entry point ──────────────────────────────────────────────────────────────
usage() {
  cat <<EOF
Usage: bash scripts/run.sh [COMMAND]

Commands:
  once        Start the whole stack now, in this terminal (Ctrl+C to stop).
  autostart   Install + enable a systemd service so it starts on every boot.
  --run       Same as 'once' (this is what the systemd service uses).
  help        Show this help.

With no command it shows an interactive menu. Tip: when using the menu, do NOT
background it with '&' — a backgrounded prompt can't read the terminal. Use a
command instead, e.g.  bash scripts/run.sh autostart
EOF
}

case "${1:-}" in
  once|--run|run|--service) run_stack; exit $? ;;
  autostart|--autostart)    setup_autostart; exit $? ;;
  -h|--help|help)           usage; exit 0 ;;
  "")                       : ;;   # no command → interactive menu below
  *) echo "Unknown command: $1" >&2; usage >&2; exit 1 ;;
esac

# No command: if there's no real terminal (piped, or launched as a service),
# just run — never block on a prompt we can't show.
if [ ! -t 0 ] || [ ! -t 1 ]; then
  run_stack; exit $?
fi

# Interactive menu. Ignore SIGTTIN so that, if this was backgrounded with '&',
# the read fails cleanly (and we default to "run once") instead of suspending.
trap '' TTIN
echo
echo "  Nighty headless — how do you want to run it?"
echo "    1) Run now (one-off, in this terminal — Ctrl+C stops it)"
echo "    2) Set up autostart (systemd) — starts automatically on every boot"
echo
printf "  Choice [1/2] (Enter = 1): "
if ! read -r choice; then
  echo; echo "  (no terminal input — starting once. For autostart run:  bash scripts/run.sh autostart)"
  choice=1
fi
trap - TTIN

case "${choice:-1}" in
  2)      setup_autostart; exit $? ;;
  1|"")   run_stack ;;
  *)      echo "  Unrecognised choice '$choice' — starting once."; run_stack ;;
esac
