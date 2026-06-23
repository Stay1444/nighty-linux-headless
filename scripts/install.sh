#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# nighty-linux-headless — installer
#
#  Beginner-friendly: it CHECKS what is already present and installs ONLY what
#  is missing, then repacks your Nighty.exe into a headless stub.
#
#  What it sets up (only if missing):
#    • base tools (curl, tar, xz, gnupg)         • Python 3
#    • Xvfb (virtual display)                     • uv (fetches Python 3.8)
#    • x86-64:  Wine (native, from your distro)
#    • non-x86: Box64  +  a static x86-64 Wine build (runs under Box64)
#
#  You only need to bring your own licensed Nighty.exe (it is never shipped).
#
#  Re-running is safe: anything already installed is detected and skipped.
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

# ── pretty output ────────────────────────────────────────────────────────────
if [ -t 1 ]; then B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; C=$'\033[36m'; R=$'\033[31m'; N=$'\033[0m'; else B=; G=; Y=; C=; R=; N=; fi
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
add()  { printf '  %s+%s %s\n' "$Y" "$N" "$*"; }
info() { printf '%s==>%s %s\n' "$C" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; }
die()  { printf '%sERROR:%s %s\n' "$R" "$N" "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1; }

printf '\n%snighty-linux-headless installer%s\n\n' "$B" "$N"

# ── architecture ─────────────────────────────────────────────────────────────
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64) IS_X86=1; info "Architecture: $ARCH — Nighty runs natively under Wine." ;;
  *)            IS_X86=0; info "Architecture: $ARCH — Nighty runs under Wine on an x86-64 emulator (Box64)." ;;
esac

# ── sudo ─────────────────────────────────────────────────────────────────────
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  if need sudo; then SUDO="sudo"
  else die "Not running as root and 'sudo' is not installed. Re-run as root, or install sudo first."; fi
fi

# ── package manager ──────────────────────────────────────────────────────────
PM=""; PM_INSTALL=""; PM_UPDATE=""
if   need apt-get; then PM=apt;    PM_UPDATE="$SUDO apt-get update";                    PM_INSTALL="$SUDO apt-get install -y"
elif need dnf;     then PM=dnf;    PM_UPDATE="$SUDO dnf -y makecache";                  PM_INSTALL="$SUDO dnf install -y"
elif need pacman;  then PM=pacman; PM_UPDATE="$SUDO pacman -Sy --noconfirm";            PM_INSTALL="$SUDO pacman -S --noconfirm --needed"
elif need zypper;  then PM=zypper; PM_UPDATE="$SUDO zypper --non-interactive refresh";  PM_INSTALL="$SUDO zypper --non-interactive install"
else warn "No supported package manager found (apt/dnf/pacman/zypper)."; warn "Missing packages can't be auto-installed — see README and install them manually."; fi

_PM_UPDATED=0
pm_refresh() { [ "$_PM_UPDATED" = 1 ] && return 0; [ -n "$PM_UPDATE" ] && { info "Refreshing package lists…"; $PM_UPDATE >/dev/null 2>&1 || true; }; _PM_UPDATED=1; }

# map a generic dependency to the distro package name(s)
pkg_name() {
  case "$PM:$1" in
    apt:xvfb) echo xvfb ;;     dnf:xvfb) echo xorg-x11-server-Xvfb ;;
    pacman:xvfb) echo xorg-server-xvfb ;;  zypper:xvfb) echo xorg-x11-server-Xvfb ;;
    apt:xz) echo xz-utils ;;   *:xz) echo xz ;;
    apt:gnupg) echo gnupg ;;   *:gnupg) echo gnupg2 ;;
    apt:wine) echo "wine64 wine" ;;        *:wine) echo wine ;;
    *) echo "$1" ;;
  esac
}

pm_install() {
  [ -n "$PM" ] || die "Need to install '$1' but no package manager is available. Install it manually and re-run."
  pm_refresh
  local pkg; pkg="$(pkg_name "$1")"
  add "installing: $pkg"
  # shellcheck disable=SC2086
  $PM_INSTALL $pkg >/dev/null 2>&1 || $PM_INSTALL $pkg || die "Failed to install: $pkg"
}

# ensure a command exists; install its package if not
ensure() { # <command> <generic-pkg> <label>
  if need "$1"; then ok "$3 present"; else add "$3 missing"; pm_install "$2"; need "$1" && ok "$3 installed" || die "$3 still missing after install"; fi
}

# download helper: dl <url> <dest>
dl() {
  if   need curl; then curl -fSL --retry 3 --connect-timeout 20 -o "$2" "$1"
  elif need wget; then wget -q -O "$2" "$1"
  else return 1; fi
}

# set KEY=VALUE in a file (replace if present, append otherwise)
set_kv() { # <file> <key> <value>
  if grep -qE "^$2=" "$1"; then
    local esc; esc="$(printf '%s' "$3" | sed -e 's/[\/&|]/\\&/g')"
    sed -i "s|^$2=.*|$2=$esc|" "$1"
  else
    printf '%s=%s\n' "$2" "$3" >> "$1"
  fi
}

# ── 0) .env (create from example, then load so we honour existing settings) ──
if [ ! -f .env ]; then
  cp .env.example .env
  add "created .env — set your Web UI username/password in it before launching!"
else
  ok ".env already exists (keeping your settings)"
fi
set -a; . ./.env; set +a

# ── 1) base tooling ──────────────────────────────────────────────────────────
info "Checking base tools…"
if ! need curl && ! need wget; then pm_install curl; fi
need curl || need wget || die "Need curl or wget."
ensure tar  tar  "tar"
[ "$IS_X86" = 1 ] || ensure xz xz "xz (extractor)"   # only needed to unpack the Wine tarball

# ── 2) Python 3 + Xvfb ───────────────────────────────────────────────────────
ensure python3 python3 "Python 3"
ensure Xvfb    xvfb    "Xvfb (virtual display)"

# ── 3) uv (used to fetch Python 3.8 for the repack) ──────────────────────────
if need uv; then
  ok "uv present"
else
  add "uv missing — installing the official build"
  if need curl; then curl -LsSf https://astral.sh/uv/install.sh | sh
  else wget -qO- https://astral.sh/uv/install.sh | sh; fi
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  need uv && ok "uv installed" || die "uv installed but not on PATH — open a new shell (or 'source ~/.bashrc') and re-run."
fi

# ── 4) Wine (+ Box64 on non-x86) ─────────────────────────────────────────────
WINE_BIN_RESOLVED=""
NIGHTY_HOME="${NIGHTY_HOME:-$HOME/.local/share/nighty}"

# Make sure NIGHTY_HOME exists AND is writable by us. A stale .env can point it
# at a root-only path (e.g. /opt/nighty); plain mkdir would fail silently and
# later downloads/writes would break confusingly. Try as the user, then via
# sudo (+chown), and finally fall back to a guaranteed-writable home location.
ensure_runtime_home() {
  if mkdir -p "$NIGHTY_HOME" 2>/dev/null && [ -w "$NIGHTY_HOME" ]; then ok "Runtime dir: $NIGHTY_HOME"; return; fi
  if [ -n "$SUDO" ] && $SUDO mkdir -p "$NIGHTY_HOME" 2>/dev/null; then
    $SUDO chown -R "$(id -u):$(id -g)" "$NIGHTY_HOME" 2>/dev/null || true
    [ -w "$NIGHTY_HOME" ] && { ok "Runtime dir: $NIGHTY_HOME (created with sudo)"; return; }
  fi
  local fb="$HOME/.local/share/nighty"
  warn "NIGHTY_HOME '$NIGHTY_HOME' is not writable — using $fb instead."
  NIGHTY_HOME="$fb"
  mkdir -p "$NIGHTY_HOME" || die "Could not create a runtime directory at $NIGHTY_HOME."
  ok "Runtime dir: $NIGHTY_HOME"
}
ensure_runtime_home

ensure_wine_native() {
  if need wine64; then ok "Wine present"; WINE_BIN_RESOLVED="$(command -v wine64)"; return; fi
  if need wine;   then ok "Wine present"; WINE_BIN_RESOLVED="$(command -v wine)";   return; fi
  add "Wine missing"
  pm_install wine
  WINE_BIN_RESOLVED="$(command -v wine64 || command -v wine || true)"
  [ -n "$WINE_BIN_RESOLVED" ] && ok "Wine installed ($WINE_BIN_RESOLVED)" || die "Wine still missing after install."
}

box64_pkg_for_host() {
  local m=""
  [ -r /proc/device-tree/model ] && m="$(tr -d '\0' </proc/device-tree/model 2>/dev/null)"
  case "$m" in
    *"Raspberry Pi 5"*) echo box64-rpi5 ;;
    *"Raspberry Pi 4"*) echo box64-rpi4 ;;
    *"Raspberry Pi 3"*) echo box64-rpi3 ;;
    *)                  echo box64-generic-arm ;;
  esac
}

install_box64_apt() {
  info "Adding the Box64 APT repository…"
  ensure gpg gnupg "gnupg"
  pm_refresh
  local key=/etc/apt/trusted.gpg.d/box64-debian.gpg list=/etc/apt/sources.list.d/box64-debian.list
  if need curl; then
    curl -fsSL https://ryanfortner.github.io/box64-debian/KEY.gpg | $SUDO gpg --dearmor -o "$key" || return 1
    curl -fsSL https://ryanfortner.github.io/box64-debian/box64-debian.list | $SUDO tee "$list" >/dev/null || return 1
  else
    wget -qO- https://ryanfortner.github.io/box64-debian/KEY.gpg | $SUDO gpg --dearmor -o "$key" || return 1
    wget -qO- https://ryanfortner.github.io/box64-debian/box64-debian.list | $SUDO tee "$list" >/dev/null || return 1
  fi
  $SUDO apt-get update >/dev/null 2>&1 || true
  local pkg; pkg="$(box64_pkg_for_host)"
  add "installing: $pkg"
  $SUDO apt-get install -y "$pkg" || $SUDO apt-get install -y box64-generic-arm || return 1
}

install_box64_source() {
  warn "Falling back to building Box64 from source (this takes a few minutes)…"
  ensure git git "git"
  [ -n "$PM" ] && { pm_refresh; $PM_INSTALL build-essential cmake >/dev/null 2>&1 || $PM_INSTALL gcc make cmake >/dev/null 2>&1 || true; }
  need cmake || die "Box64 source build needs cmake — install it and re-run."
  local tmp; tmp="$(mktemp -d)"
  git clone --depth 1 https://github.com/ptitSeb/box64 "$tmp/box64" || { rm -rf "$tmp"; return 1; }
  ( cd "$tmp/box64" && mkdir -p build && cd build \
      && cmake .. -DARM_DYNAREC=ON -DCMAKE_BUILD_TYPE=RelWithDebInfo >/dev/null \
      && make -j"$(nproc)" >/dev/null \
      && $SUDO make install >/dev/null ) || { rm -rf "$tmp"; return 1; }
  $SUDO systemctl restart systemd-binfmt 2>/dev/null || true
  rm -rf "$tmp"
}

ensure_box64() {
  if need box64; then ok "Box64 present"; return; fi
  add "Box64 missing"
  if [ "$PM" = apt ]; then install_box64_apt || install_box64_source; else install_box64_source; fi
  need box64 && ok "Box64 installed" || die "Could not install Box64 automatically. See https://github.com/ptitSeb/box64 and install it, then re-run."
}

ensure_wine_static_x86() {
  local wdir="$NIGHTY_HOME/wine" ver="${WINE_VERSION:-10.0}"
  WINE_BIN_RESOLVED="$wdir/bin/wine64"
  if [ -x "$WINE_BIN_RESOLVED" ]; then ok "x86-64 Wine present ($wdir)"; return; fi
  add "x86-64 Wine (static, for Box64) missing — downloading Wine $ver"
  local url="https://github.com/Kron4ek/Wine-Builds/releases/download/${ver}/wine-${ver}-amd64.tar.xz"
  local tgz="$NIGHTY_HOME/.wine-dl.tar.xz"
  info "Fetching $url"
  dl "$url" "$tgz" || die "Could not download Wine. Check your connection, or set WINE_VERSION to another release."
  rm -rf "$wdir"; mkdir -p "$wdir"
  info "Extracting Wine…"
  tar -xf "$tgz" -C "$wdir" --strip-components=1 || die "Failed to extract the Wine tarball."
  rm -f "$tgz"
  [ -x "$WINE_BIN_RESOLVED" ] || die "Wine extracted but $WINE_BIN_RESOLVED is missing."
  ok "x86-64 Wine ready ($wdir)"
}

if [ "$IS_X86" = 1 ]; then
  ensure_wine_native
else
  ensure_box64
  ensure_wine_static_x86
fi

# ── 5) write resolved runtime paths back into .env ──────────────────────────
set_kv .env NIGHTY_HOME "$NIGHTY_HOME"
set_kv .env WINEPREFIX  "$NIGHTY_HOME/prefix"
[ -n "$WINE_BIN_RESOLVED" ] && set_kv .env WINE_BIN "$WINE_BIN_RESOLVED"

# ── 6) locate your Nighty.exe ────────────────────────────────────────────────
SRC="${NIGHTY_EXE:-$HERE/Nighty.exe}"
OUT="${NIGHTY_STUB:-$HERE/Nighty_stub.exe}"
case "$SRC" in /*) : ;; ./*) SRC="$HERE/${SRC#./}" ;; esac
case "$OUT" in /*) : ;; ./*) OUT="$HERE/${OUT#./}" ;; esac
if [ ! -f "$SRC" ]; then
  printf '\n%sAlmost there — one manual step:%s\n' "$B" "$N"
  printf '  Copy YOUR licensed Nighty.exe into this folder, then re-run this installer:\n\n'
  printf '    cp /path/to/Nighty.exe %q\n'  "$SRC"
  printf '    bash scripts/install.sh\n\n'
  printf '  (Nighty.exe is never bundled or redistributed — bring your own copy.)\n'
  die "Nighty.exe not found at: $SRC"
fi

# ── 7) repack (must run under Python 3.8 — marshal format is version-specific) ─
info "Ensuring Python 3.8 (via uv) for the repack…"
uv python install 3.8 >/dev/null 2>&1 || true
PY38="$(uv python find 3.8 2>/dev/null || true)"
[ -n "$PY38" ] || die "Could not obtain Python 3.8 via uv."
ok "Python 3.8: $PY38"

info "Repacking $(basename "$SRC") → $(basename "$OUT") (headless webview stub)…"
NIGHTY_EXE="$SRC" NIGHTY_STUB="$OUT" "$PY38" scripts/repack.py "$SRC" "$OUT" || die "Repack failed."
ok "Repack done: $OUT"

# ── done ─────────────────────────────────────────────────────────────────────
printf '\n%sSetup complete.%s Next:\n\n' "$G" "$N"
printf '  1) Set your Web UI login in .env:  %sWEBUI_USERNAME%s / %sWEBUI_PASSWORD%s\n' "$B" "$N" "$B" "$N"
printf '  2) Start everything with one command:\n'
printf '       %sbash scripts/run.sh%s\n' "$B" "$N"
printf '     It asks whether to run once or set up autostart (systemd). Or skip\n'
printf '     the menu:  bash scripts/run.sh once   |   bash scripts/run.sh autostart\n'
printf '  3) Open  http://<this-host-ip>:%s/  and follow the on-screen onboarding.\n\n' "${BRIDGE_PORT:-8088}"
