# nighty-linux-headless

Run **Nighty** headless on Linux and use its built-in **Web UI** over your LAN -
even though the native desktop GUI can't render there. It works on any Linux
host: on **x86-64** Nighty runs natively under Wine; on other architectures it
runs through an x86-64 emulator (Box64) under Wine. The startup script detects
your architecture automatically.

You bring your **own licensed `Nighty.exe`**; this project automates everything
else: it repacks the binary with a headless GUI stub, enforces a sane headless
configuration, exposes the Web UI safely over the LAN, and keeps the backend
alive.

> **Disclaimer.** This is an unofficial, community interop/automation tool. It is
> **not** affiliated with or endorsed by Nighty, and it does **not** include,
> redistribute, crack, or unlicense Nighty - you must supply your own legally
> obtained copy and a valid license. Nighty is a Discord **selfbot**; automating
> a user account can violate Discord's Terms of Service. Use it only with your
> own account, on your own hardware, at your own risk. For education and personal
> interoperability.

---

## How it works (short version)

```
browser ──▶ bridge :8088 ──proxy──▶ Nighty Web UI :8090 (loopback)
                                         ▲
                                   Nighty backend running headless,
                                   GUI replaced by a no-op webview stub,
                                   under Xvfb + Wine (+ Box64 on non-x86 hosts)
```

Full details in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Features

- **Bring-your-own binary** - drop in your own `Nighty.exe`; updates stay your
  choice (just re-run the installer with a newer exe).
- **Headless GUI stub** - repacks the exe so the backend + Web UI start without a
  renderable desktop GUI. Licensing and protected code are left untouched.
- **Configurable Web UI login** - you set the username/password in `.env`.
- **Web UI always-on (hard enforcement)** - if Nighty or the user disables the
  Web UI, it is forced back on within seconds (it's the only usable interface on
  a headless box).
- **Quiet by default** - every `toast` and `sound` option in `notifications.json`
  is disabled before each launch.
- **Persistence** - `run.sh` relaunches the backend on restart/close, and systemd
  supervises everything (survives crashes and reboots).

## Requirements

You only need three things - **the installer handles the rest**:

- Any **Linux** host with **`sudo`** access and an **internet connection**.
- Your own **`Nighty.exe`** and a valid Nighty license.
- A supported package manager for the auto-install (**apt**, **dnf**, **pacman**,
  or **zypper**). On other distros, install the dependencies manually (below).

`scripts/install.sh` **checks what is already present and installs only what is
missing**:

- base tools (curl/tar/xz/gnupg), **Python 3**, **Xvfb**, and
  **[`uv`](https://docs.astral.sh/uv/)** (used to fetch Python 3.8 for the repack);
- on **x86-64** - **Wine** from your distro when it is **version 10 or newer**
  (Nighty runs natively). Older Wine **hangs Nighty during early startup with no
  error** (Ubuntu 22.04/24.04 still ship Wine 6-9), so when the distro Wine is too
  old - or not installed - the installer transparently falls back to the same
  self-contained **static x86-64 Wine** build used on ARM. Your system Wine is
  left untouched;
- on **non-x86** (ARM, etc.) - **Box64** plus a **static x86-64 Wine** build that
  runs under it. Hardware is detected automatically (e.g. the best Box64 build),
  and `run.sh` applies emulator tuning only when needed.

Re-running the installer is safe: anything already set up is detected and skipped.

## Quick start

```bash
git clone <your-fork-url> nighty-linux-headless
cd nighty-linux-headless

# 1) Put YOUR binary here
cp /path/to/Nighty.exe .

# 2) Install everything that's missing + repack into Nighty_stub.exe
#    (auto-installs Wine/Xvfb/uv, and Box64 + static Wine on ARM - asks for sudo)
bash scripts/install.sh

# 3) Set your Web UI username/password (paths are filled in for you)
$EDITOR .env

# 4) Start everything
bash scripts/run.sh
```

`run.sh` brings up the **whole stack** (virtual display, config enforcement, the
LAN Web UI bridge, and the Nighty backend - each kept alive automatically). With
no arguments it shows a menu:

```
  1) Run now (one-off, in this terminal)
  2) Set up autostart (systemd) - starts automatically on every boot
```

Choose **2** and it installs and enables a systemd service for you (asks for
`sudo`), so Nighty starts on every boot - no manual unit editing needed. You can
also skip the menu with a command:

```bash
bash scripts/run.sh once        # run in this terminal
bash scripts/run.sh autostart   # install + enable the systemd service
```

> When using the menu, don't background it with `&` - a backgrounded prompt
> can't read your keypress. Use `run.sh autostart` (or run it in the foreground).

Then open `http://<host-ip>:8088/` in a browser. First run walks you through
these steps:

1. **Activate** - paste your **Nighty license key** (from your Nighty purchase /
   dashboard). Nighty needs it to run: without a license the bot can sign in but
   `on_ready` aborts before its commands are registered, so it looks online yet
   **no command works**. The bridge saves the key for you.
2. **Sign in** - paste your Discord account token.
3. **Connect your bot** - paste your **bot token** (Developer Portal → your app
   → **Bot** → *Reset Token*). The bridge verifies it with Discord *before*
   handing it to Nighty: it confirms the token is valid and that the required
   **privileged intents** (Presence, Server Members, Message Content) are
   enabled. If any are off, it shows you exactly which ones and links you
   straight to the right settings page - flip them on, hit **Save Changes**,
   then re-check. Pasting the token yourself avoids the developer-portal
   password and captcha entirely.
4. **Authorize** *(only if your bot isn't linked yet)* - if the bot has never
   been authorized on your Discord, Nighty won't finish starting and the bridge
   shows an **Authorize** page with a direct Discord OAuth link for your own
   application. Click it, approve the bot on Discord, then press *continue* - the
   bridge restarts Nighty so it picks up the authorization. You won't see this
   step if the bot is already authorized.

After that the native Web UI loads.

## Configuration

All settings live in `.env` (copy from `.env.example`). Key ones:

| Variable | Meaning |
|---|---|
| `NIGHTY_EXE` / `NIGHTY_STUB` | your original exe / the repacked stub |
| `NIGHTY_HOME`, `WINEPREFIX` | runtime + Wine prefix locations |
| `WEBUI_USERNAME`, `WEBUI_PASSWORD` | **your** Web UI login |
| `WEBUI_HOST`, `WEBUI_PORT` | native panel bind (keep loopback) |
| `BRIDGE_HOST`, `BRIDGE_PORT` | LAN bridge bind (what you open) |
| `STUB_PORT` | stub control server (keep loopback) |
| `DISPLAY_NUM` | Xvfb display number |
| `ENFORCE_WEBUI`, `ENFORCE_INTERVAL` | Web UI hard-enforcement |

## Real-time (WebSockets)

Nighty's native Web UI uses **socket.io (WebSockets)** for live updates. The
bundled bridge tunnels them with a `select()`-based full-duplex pump that
disables Nagle (`TCP_NODELAY`) and enables TCP keepalive, so the connection
stays up instead of dropping into a "Disconnected - Reconnecting…" loop. No
extra software is required for stable real-time over the LAN.

If you want TLS or are fronting a high-traffic deployment, you can still put a
dedicated reverse proxy ahead of the bridge - a ready-made
[`Caddyfile.example`](Caddyfile.example) is included (`nginx` works too). It is
optional, not required.

## Repository layout

```
nighty-linux-headless/
├── README.md
├── LICENSE                  MIT (wrapper only; Nighty is third-party)
├── .env.example             copy to .env
├── Caddyfile.example        optional production front (TLS/HTTP2; not required)
├── .gitignore
├── scripts/
│   ├── install.sh           auto-install missing deps + scaffold .env + repack
│   ├── uninstall.sh         stop + remove service, purge all data (clean reset)
│   ├── repack.py            Nighty.exe -> Nighty_stub.exe (headless stub)
│   ├── enforce_config.py    notifications off + Web UI creds + web:true
│   ├── webui_guard.py       continuous "Web UI always on" enforcement
│   ├── run.sh               orchestrator: starts the whole stack + autostart menu
│   └── bridge.py            LAN reverse-proxy to the native Web UI
├── systemd/
│   └── nighty.service       reference unit (run.sh installs this for you)
└── docs/
    └── ARCHITECTURE.md
```

## Where your data lives (and how to fully reset)

Nighty's state is stored **outside this repo**, so deleting the project folder or
the systemd unit does **not** reset Nighty - a reinstall finds the old data and
comes straight back up. Everything persists under **`$NIGHTY_HOME`** (default
`~/.local/share/nighty`):

- `…/prefix/` - the Wine prefix. Your **license** (`auth.json`), **account + bot
  tokens** and `web=true` (`nighty.config`), Web UI creds (`web_config.json`), and
  `data/` (themes, scripts, settings) all live inside it.
- `…/wine/` - the bundled x86-64 Wine (non-x86 hosts), plus the logs.

The only Nighty files elsewhere are the generated `.env` and the
`/etc/systemd/system/nighty.service` unit. See
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#data--storage-locations) for the
full map.

Once initial setup completes (license + tokens + an authorized bot), the box is
**locked**: a `.setup_locked` marker is written next to the config, and every
later restart boots straight into the working state - the setup and authorization
screens are never shown again. The only way back to setup is the reset option
below.

`scripts/uninstall.sh` opens an interactive menu:

```bash
bash scripts/uninstall.sh
#   [1] Full uninstall        remove everything (service, $NIGHTY_HOME, prefix,
#                             .env, both binaries) - back to a pre-install state.
#   [2] Reset configuration   delete only the license, tokens and the lockdown
#                             marker, then restart Nighty for a fresh setup flow.
#                             Keeps the binary, service and the rest installed.
#   [3] Cancel
```

Use **[2] Reset configuration** to redo onboarding (it is the only supported way
to unlock and re-run setup). Use **[1] Full uninstall** to remove Nighty entirely;
afterwards copy your `Nighty.exe` back in and run `scripts/install.sh`. (Shared
tools - `uv`, Box64, distro Wine - are always left in place.)

## Security notes

- Only the **bridge (8088)** is meant to be LAN-reachable. The native Web UI
  (8090) and stub control server (8765) stay on loopback.
- The bridge does not store tokens; it forwards them to the local backend.
- The LAN bridge has no transport encryption - run it only on a trusted LAN, or
  put it behind a reverse proxy / VPN if you need remote access.
- Your `.env` (credentials) and the Wine prefix (tokens) are git-ignored. Never
  commit them.

## Troubleshooting

- **Repack fails / "bad marshal data"** - the repack must run under Python 3.8.
  Let `install.sh` use the `uv`-provided 3.8 interpreter.
- **Bot is online but no commands work / "application command not found"** -
  this is a **missing Nighty license**. Unlicensed, Nighty's `on_ready` aborts
  before it registers its command tree. Complete step 1 (Activate) with your
  Nighty license key and reconnect.
- **Backend never opens 8090** - it only starts after a successful login. Open
  the bridge and complete the onboarding flow.
- **Authorization problems - "asks to authorize", bot disconnected, or stuck on
  the auth screen.** If your bot is not authorized on your Discord account (or you
  disconnected/removed it), Nighty cannot work and the bridge shows an
  **Authorize** page with a direct OAuth link for your app - open it and approve
  the bot. If the box is already locked (setup completed before), or you are still
  stuck after authorizing, **reset the configuration** to redo onboarding cleanly:

  ```bash
  bash scripts/uninstall.sh     # choose [2] Reset configuration
  ```

  Option [2] deletes only the license, tokens and the lockdown marker, then
  restarts Nighty so it re-runs Activate -> Sign in -> Connect bot -> Authorize
  from the start - the binary and service stay installed. (For a complete removal
  instead, choose [1] Full uninstall.) A user-installable app authorizes with an
  `integration_type=1&scope=applications.commands` link; a classic app is invited
  to a server.
- **"some intents are OFF" on the bot step** - your bot application doesn't have
  the privileged gateway intents enabled. Click the link the page gives you
  (Developer Portal → your app → **Bot**), turn on **Presence**, **Server
  Members** and **Message Content**, press **Save Changes**, then **Validate &
  connect** again.
- **On x86-64, the backend hangs at startup (no `[STUBWV]` logs, `:8765` and
  `:8090` never bind, no error)** - your distro Wine is too old. Wine 6-9 (shipped
  by Ubuntu 22.04/24.04) hangs Nighty before the webview stub even loads; Wine 10+
  fixes it. The installer detects this and falls back to a static Wine build
  automatically, so just re-run `bash scripts/install.sh`. To force the static
  build regardless, point `WINE_BIN` in `.env` at it (the installer downloads it
  to `~/.local/share/nighty/wine`), or pick a specific release with `WINE_VERSION`.
- **On non-x86 hosts, Wine crashes with illegal-instruction** - you need a
  recent **Box64** built for your CPU; older distro packages may be too old.
- **The menu prints but typing `1`/`2` does nothing (or "command not found")** -
  you backgrounded `run.sh` with `&`; a backgrounded prompt can't read input.
  Run it in the foreground, or use `bash scripts/run.sh once` / `autostart`.
- **Backend keeps exiting / "Disconnected - Reconnecting"** - make sure the
  headless DLL overrides are active (`run.sh` sets `WINEDLLOVERRIDES` to disable
  .NET/IE/desktop integration, which otherwise abort on a fresh Wine prefix).
- **Bot is very slow / commands return "the application did not respond"** - on
  emulated (non-x86) hosts this is caused by Nighty's Rich-Presence task fetching
  song lyrics from **lrclib.net** with a blocking call on the bot's event loop;
  under emulation it freezes the whole bot for tens of seconds (the gateway
  heartbeat times out and slash commands miss Discord's 3-second deadline).
  `install.sh` blackholes `lrclib.net` in `/etc/hosts` so that call fails
  instantly (`BLOCK_LRCLIB=1`), `run.sh` applies faster Box64 dynarec tuning, and
  config enforcement disables the Rich-Presence status rotator. If you still see
  it, confirm the `/etc/hosts` entry exists and that you restarted the stack.
- **The backend crashes when a Rich-Presence preset runs** - on emulated
  (non-x86) hosts, running an RPC preset makes Nighty fetch its presence assets
  through the bundled Go `tls-client`, whose JSON handling **intermittently
  segfaults under Box64** ("access violation" / Go `runtime.sigpanic`) and takes
  the whole backend down (it then auto-relaunches). Rich Presence has no purpose
  on a headless selfbot, so the config guard keeps the presence rotator disabled
  and, if a profile is ever started from the Web UI, stops it again within a few
  seconds (the same way the UI's toggle does). Leave the rotator off on these
  hosts; this is a Box64/Nighty emulation limitation, not a bug in this wrapper.
- **"Error downloading sound ... HTTP Error 403: Forbidden"** - Nighty fetches
  its notification sounds with `urllib`, whose default `User-Agent` the CDN
  (Cloudflare) blocks with 403, so the sound never lands and the error repeats on
  every matching event. The config enforcer pre-seeds `data/sounds/` with those
  files using a browser `User-Agent` at launch, so Nighty's download-if-missing
  path skips the blocked request. If a different sound shows the same 403, add its
  file name to `SOUND_FILES` in `scripts/enforce_config.py`.

## License

MIT for the wrapper code in this repo. See [`LICENSE`](LICENSE) - note the
NOTICE: Nighty itself is proprietary and not included.

## Support

If this project saved you some time, a tip is hugely appreciated - thank you! ☕

- **Ko-fi:** https://ko-fi.com/glowxx
- **Litecoin (LTC):** `ltc1qz76tezwulr25xmv8xzzu7wgs9rkjl20mlplgew`
