#!/usr/bin/env python3
"""
nighty-linux-headless — LAN bridge.

Exposes Nighty's native Web UI (the "legacy" interface) safely over the LAN, and
provides a clean first-run onboarding flow (paste account token, then bot/app
token) for when the panel is not up yet.

  • GET  /            -> the native Web UI (reverse-proxied from WEBUI_PORT),
                         or onboarding pages while it is still starting.
  • POST /rpc         -> forwarded to the stub control server (onboarding only).
  • GET  /state       -> onboarding state machine.
  • GET  /events      -> stub event stream (onboarding only).

All hosts/ports/credentials come from the environment (see .env.example).
Nothing is hardcoded; no secrets live in this file.
"""
import os, json, ssl, time, base64, select, socket, threading, urllib.request, urllib.error, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from enforce_config import find_appdata  # same directory
except Exception:
    def find_appdata():
        return None

STUB_PORT = int(os.environ.get("STUB_PORT", "8765"))
WEBUI_HOST = os.environ.get("WEBUI_HOST", "127.0.0.1")
WEBUI_PORT = int(os.environ.get("WEBUI_PORT", "8090"))
HOST = os.environ.get("BRIDGE_HOST", "0.0.0.0")
PORT = int(os.environ.get("BRIDGE_PORT", "8088"))

STUB = "http://127.0.0.1:%d" % STUB_PORT
WEBPANEL = "http://%s:%d" % (WEBUI_HOST, WEBUI_PORT)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
_ctx = ssl.create_default_context(); _ctx.check_hostname = False; _ctx.verify_mode = ssl.CERT_NONE


_web_up_cache = {"t": 0.0, "v": False}


def web_up():
    # Cache the probe for ~1s so we don't open a fresh TCP connection on every
    # asset/poll request (adds latency that hurts socket.io responsiveness).
    now = time.time()
    if now - _web_up_cache["t"] < 1.0:
        return _web_up_cache["v"]
    s = socket.socket(); s.settimeout(0.4)
    try:
        s.connect((WEBUI_HOST, WEBUI_PORT)); ok = True
    except Exception:
        ok = False
    finally:
        try: s.close()
        except Exception: pass
    _web_up_cache["v"] = ok; _web_up_cache["t"] = now
    return ok


def stub_get(path):
    with urllib.request.urlopen(STUB + path, timeout=10) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def stub_call(api_idx, method, args=None, **opts):
    """POST a method call to the stub control server and return its JSON."""
    payload = {"api": api_idx, "method": method, "args": args or []}
    payload.update(opts)
    req = urllib.request.Request(STUB + "/api/call", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return {"ok": False, "error": "http %d" % e.code}
    except Exception as e:
        return {"ok": False, "error": repr(e)}


# Privileged gateway intents live in the Discord application's "flags" bitfield.
# Each intent has a "full" bit and an "unverified/limited" bit; either one being
# set means the toggle is on in the developer portal. Nighty's bot needs all
# three to work properly, so we require them before handing the token over.
INTENTS = [
    ("Presence Intent",        (1 << 12) | (1 << 13)),
    ("Server Members Intent",  (1 << 14) | (1 << 15)),
    ("Message Content Intent", (1 << 18) | (1 << 19)),
]


def _bot_token_app_id(token):
    """A bot token's first dot-segment is the base64 of its application id."""
    try:
        seg = token.split(".", 1)[0]
        seg += "=" * (-len(seg) % 4)
        return base64.urlsafe_b64decode(seg).decode("ascii")
    except Exception:
        return None


def _discord_bot_get(token, path):
    """GET discord.com/api/v10 authenticated as a bot. Returns (status, json|None)."""
    req = urllib.request.Request("https://discord.com/api/v10" + path, headers={
        "Authorization": "Bot " + token,
        "User-Agent": "DiscordBot (https://github.com/nighty-linux-headless 1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=20, context=_ctx) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return e.code, None
    except Exception:
        return -1, None


def check_bot_token(token):
    """Validate a bot token and report its privileged-intent state.

    We talk to Discord's own API as the bot, so we know — before Nighty ever
    sees the token — whether it is valid and whether the intents the bot needs
    are switched on. Returns a dict the onboarding page understands:

      valid        - the token authenticated as a bot
      app_id       - the application id (used for the intents settings link)
      bot_name     - the bot's username (shown back for confirmation)
      intents_ok   - every required privileged intent is enabled
      missing      - human names of the intents still turned off
      intents_url  - deep link to the Bot tab where the toggles live
      error        - a human message when valid is False
    """
    token = (token or "").strip()
    if not token:
        return {"valid": False, "error": "Paste your bot token first."}
    st, me = _discord_bot_get(token, "/users/@me")
    if st == 401 or not (isinstance(me, dict) and me.get("id")):
        return {"valid": False,
                "error": "Discord rejected this token. Make sure it is a *bot* token "
                         "(Developer Portal -> your app -> Bot -> Reset Token) and that "
                         "you copied all of it."}
    bot_name = me.get("username") or "your bot"
    _, app = _discord_bot_get(token, "/applications/@me")
    app_id = (app.get("id") if isinstance(app, dict) else None) or _bot_token_app_id(token)
    flags = app.get("flags", 0) if isinstance(app, dict) else 0
    missing = [name for name, bits in INTENTS if not (flags & bits)]
    return {"valid": True, "app_id": app_id, "bot_name": bot_name,
            "intents_ok": not missing, "missing": missing,
            "intents_url": "https://discord.com/developers/applications/%s/bot" % app_id}


def _write_app_id(app_id):
    """Reconcile the saved app context's id with the pasted token's application,
    so a later restart auto-logs in against the right application."""
    appdata = find_appdata()
    if not appdata or not app_id:
        return
    try:
        cfg = os.path.join(appdata, "nighty.config")
        d = json.load(open(cfg, encoding="utf-8"))
        changed = False
        for u, info in (d.get("logins") or {}).items():
            ap = (info or {}).get("app")
            if isinstance(ap, dict) and ap.get("token") and ap.get("id") != app_id:
                ap["id"] = app_id; changed = True
        if changed:
            tmp = cfg + ".tmp"
            json.dump(d, open(tmp, "w", encoding="utf-8"), indent=4, ensure_ascii=False)
            os.replace(tmp, cfg)
    except Exception:
        pass


def submit_bot_token(token):
    """Hand a validated bot token to Nighty (its MainApi.getAppTokenInput step).

    The page already ran check_bot_token, but we never trust the page — we
    re-validate the token and its intents here. Then we feed it to Nighty. If
    Nighty has no application context yet (it raises KeyError('app')), we set one
    up by pointing AppCreateApi at the token's OWN application's dev-portal URLs.
    Because we supply the bot token ourselves, Nighty never has to reset it —
    which is exactly why this avoids the developer-portal password and captcha.
    """
    token = (token or "").strip()
    chk = check_bot_token(token)
    if not chk.get("valid"):
        return {"ok": False, "error": chk.get("error", "Invalid bot token.")}
    if not chk.get("intents_ok"):
        return {"ok": False, "needs_intents": True, "missing": chk.get("missing", []),
                "intents_url": chk.get("intents_url"),
                "error": "Turn on the required intents, then re-check."}
    apis = stub_get("/api/methods").get("apis", [])
    main_idx = api_index(apis, "MainApi", 0)

    def feed():
        # getAppTokenInput is long-running: it saves the token and logs the bot
        # in. The stub normally answers {"running": true} at once, but under load
        # the HTTP call can time out while the work continues on the main thread —
        # that is "accepted", not "failed", so don't surface it as an error.
        r = stub_call(main_idx, "getAppTokenInput", [token])
        if not r.get("ok") and "timed out" in (r.get("error") or "").lower():
            return {"ok": True, "running": True}
        return r

    res = feed()
    if not res.get("ok") and "app" in (res.get("error") or "").lower():
        ac_idx = api_index(apis, "AppCreateApi", -1)
        if ac_idx >= 0 and chk.get("app_id"):
            base = "https://discord.com/developers/applications"
            for u in (base, "%s/%s/information" % (base, chk["app_id"]),
                      "%s/%s/bot" % (base, chk["app_id"])):
                stub_call(ac_idx, "url_callback_change", [u]); time.sleep(4)
        res = feed()
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or "Nighty did not accept the bot token."}
    _write_app_id(chk.get("app_id"))
    return {"ok": True, "app_id": chk.get("app_id"), "bot_name": chk.get("bot_name")}


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=15, context=_ctx) as r:
        return r.read(), r.headers.get("Content-Type", "text/html")


def api_index(apis, typ, default=0):
    return next((a["index"] for a in apis if a["type"] == typ), default)


def saved_app_id():
    appdata = find_appdata()
    if not appdata:
        return None
    try:
        cfg = os.path.join(appdata, "nighty.config")
        d = json.load(open(cfg, encoding="utf-8"))
        for u, info in (d.get("logins") or {}).items():
            ap = (info or {}).get("app") or {}
            if ap.get("id"):
                return ap["id"]
    except Exception:
        pass
    return None


def saved_account_token():
    """The Discord account token of the active login (if any)."""
    appdata = find_appdata()
    if not appdata:
        return None
    try:
        d = json.load(open(os.path.join(appdata, "nighty.config"), encoding="utf-8"))
        for u, info in (d.get("logins") or {}).items():
            info = info or {}
            if info.get("active") and info.get("token"):
                return info["token"]
    except Exception:
        pass
    return None


def saved_bot_token():
    """The bot/app token saved for the active account (if any)."""
    appdata = find_appdata()
    if not appdata:
        return None
    try:
        d = json.load(open(os.path.join(appdata, "nighty.config"), encoding="utf-8"))
        for u, info in (d.get("logins") or {}).items():
            info = info or {}; app = info.get("app") or {}
            if info.get("active") and app.get("token"):
                return app["token"]
    except Exception:
        pass
    return None


def _app_integration_types(app):
    """Discord integration types the app supports: 0 = guild install, 1 = user install."""
    try:
        cfg = (app or {}).get("integration_types_config") or {}
        return sorted(int(k) for k in cfg.keys())
    except Exception:
        return []


def bot_authorize_url(app_id, integration_types=None):
    """Build the Discord OAuth2 authorization URL the user opens to link the bot.

    Nighty's companion bot is added to the user's Discord through the standard
    OAuth2 authorize flow. A user-installable app (integration type 1) is added
    to the account itself; a classic app is invited to a server (type 0). We pick
    the right URL from the app's configured integration types so the link works
    on the first click without the user choosing options manually."""
    if not app_id:
        return None
    base = "https://discord.com/oauth2/authorize"
    types = integration_types or []
    if 1 in types and 0 not in types:
        return "%s?client_id=%s&integration_type=1&scope=applications.commands" % (base, app_id)
    # Guild install (covers apps that support both, or only guild install).
    return "%s?client_id=%s&scope=bot+applications.commands&permissions=8" % (base, app_id)


def authorization_status():
    """Report whether the bot is authorized on the account yet, plus the link to fix it.

    We ask Discord, as the bot, whether it is installed anywhere — added to at
    least one server (guild install) or installed on at least one account (user
    install). When it is installed nowhere, Nighty parks on its auth screen and
    the Web UI never starts; authorize_url is what the user opens to authorize it.
    The 'authorized' flag is a best-effort hint (Discord's install counts are
    approximate and cached) — the authoritative signal we gate the UI on is Nighty
    itself parking on auth.html."""
    tok = saved_bot_token()
    app_id = saved_app_id()
    out = {"authorized": None, "app_id": app_id, "authorize_url": bot_authorize_url(app_id),
           "bot_name": None, "integration": "guild"}
    if not tok:
        return out
    st, app = _discord_bot_get(tok, "/applications/@me")
    if isinstance(app, dict) and app.get("id"):
        types = _app_integration_types(app)
        out["app_id"] = app.get("id")
        out["bot_name"] = app.get("name")
        out["integration"] = "user" if (1 in types and 0 not in types) else "guild"
        out["authorize_url"] = bot_authorize_url(app.get("id"), types)
        _, gl = _discord_bot_get(tok, "/users/@me/guilds")
        guilds = len(gl) if isinstance(gl, list) else 0
        users = app.get("approximate_user_install_count") or 0
        out["authorized"] = bool(guilds) or bool(users)
    return out


def _auth_path():
    ad = find_appdata()
    return os.path.join(ad, "auth.json") if ad else None


def _lock_path():
    ad = find_appdata()
    return os.path.join(ad, ".setup_locked") if ad else None


def setup_locked():
    """True once the initial setup has fully completed (license verified, tokens
    saved, and a working/authorized bot observed).

    Lockdown is a security boundary: while locked, every setup and authorization
    screen is bypassed and the app boots straight into the working state, so a
    backend restart can never re-open the authorize window (which would otherwise
    let someone re-link the bot/account from the UI). The only supported way back
    to the setup flow is uninstall.sh's 'Reset Configuration Only', which deletes
    this lock together with the saved config."""
    p = _lock_path()
    return bool(p and os.path.exists(p))


def _set_lock():
    """Persist the lockdown marker (idempotent)."""
    p = _lock_path()
    if not p or os.path.exists(p):
        return
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write("locked %s\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
    except Exception:
        pass


def license_set():
    """True if a Nighty license key is already saved in auth.json."""
    p = _auth_path()
    if not p:
        return False
    try:
        return bool((json.load(open(p, encoding="utf-8")) or {}).get("license"))
    except Exception:
        return False


def save_license(key):
    """Save the user's Nighty license key (step 0 of onboarding).

    Without a license Nighty connects to its own server unlicensed; the bot's
    on_ready then aborts with KeyError('motd') *before* the slash-command tree is
    synced — so the bot shows up online but every command answers "not found".
    We let Nighty's own saver run (for any in-memory activation) and also write
    auth.json directly so it is in place for the next launch.
    """
    key = (key or "").strip()
    if not key:
        return {"ok": False, "error": "Enter your Nighty license key."}
    try:
        apis = stub_get("/api/methods").get("apis", [])
        stub_call(api_index(apis, "MainApi", 0), "saveKeyToAppdata", [key])
    except Exception:
        pass
    p = _auth_path()
    if p:
        try:
            d = {}
            if os.path.exists(p):
                try: d = json.load(open(p, encoding="utf-8")) or {}
                except Exception: d = {}
            d["license"] = key
            tmp = p + ".tmp"
            json.dump(d, open(tmp, "w", encoding="utf-8"), indent=4, ensure_ascii=False)
            os.replace(tmp, p)
        except Exception as e:
            return {"ok": False, "error": "Could not save the license: %r" % (e,)}
    if not license_set():
        return {"ok": False, "error": "License was not saved — check the key and try again."}
    # Nighty reads the license once, at startup, so a key saved into a running
    # backend is ignored by the in-memory client (the bot would still hit
    # KeyError('motd')). Restart the backend so it boots WITH the license on
    # disk. run.sh's keep-alive loop relaunches it. We do this now, before any
    # account is active, so the selfbot does not auto-start and race the rest of
    # onboarding — sign-in + bot then run on a freshly-licensed backend.
    try:
        os.system("pkill -f '[N]ighty_stub'")
    except Exception:
        pass
    return {"ok": True, "restarting": True}


def _saved_completion():
    """If the box is already onboarded (license + active account + saved bot
    token), return (account_token, bot_token); else None. Used to auto-resume
    after a reboot without making the user re-onboard."""
    if not license_set():
        return None
    appdata = find_appdata()
    if not appdata:
        return None
    try:
        d = json.load(open(os.path.join(appdata, "nighty.config"), encoding="utf-8"))
    except Exception:
        return None
    for u, info in (d.get("logins") or {}).items():
        info = info or {}; app = info.get("app") or {}
        if info.get("active") and info.get("token") and app.get("token"):
            return info["token"], app["token"]
    return None


def _auto_resume():
    """Bring an already-onboarded box back to life on each boot.

    Nighty does not relaunch the bot from saved config on its own (it expects the
    GUI onboarding to drive it), so after a reboot the panel stays down until the
    bot token is re-pasted. Here we detect a fully-onboarded box and replay the
    last two steps automatically — sign the account back in and re-feed the saved
    bot token — so systemd autostart restores the panel + commands with no human
    in the loop. Stays dormant until onboarding has been completed at least once."""
    time.sleep(25)
    for _ in range(45):
        try:
            if web_up():
                return
            saved = _saved_completion()
            if not saved:
                return   # not onboarded yet — let the user do it by hand
            st = get_state()
            if st.get("mode") == "main" or web_up():
                return
            if st.get("mode") in ("login", "bottoken"):
                acct, bot = saved
                try:
                    apis = stub_get("/api/methods").get("apis", [])
                    stub_call(api_index(apis, "MainApi", 0), "saveTokenToConfig", [acct])
                except Exception:
                    pass
                time.sleep(8)
                submit_bot_token(bot)
                for _ in range(40):
                    if web_up():
                        return
                    time.sleep(3)
                return
        except Exception:
            pass
        time.sleep(8)


def _backend_log_path():
    nh = os.environ.get("NIGHTY_HOME")
    return os.path.join(nh, "backend.log") if nh else None


_link_cache = {"t": 0.0, "v": None}


def bot_link_status():
    """Whether the companion bot is actually authorized/connected on the account.

    This is the authoritative 'is the bot linked?' check, and it deliberately does
    NOT trust Discord's application install counts: those are approximate and
    cached, so a disconnected user-install bot still reports a stale install (a
    false positive). Nighty also does not halt when the bot is disconnected — it
    boots, syncs global commands and serves the panel regardless — so 'panel is up'
    is not proof the bot works either.

    Instead we read Nighty's own backend log. When the bot is no longer authorized
    on the account, the companion bot's on_ready can't reach its command channel
    and raises NotFound(10003)/'Unknown Channel' from application_commands; the
    account selfbot's gateway is also rejected (HTTP 403). We scope the scan to the
    CURRENT backend process (everything after the last 'CTL server up' marker) so a
    stale failure from a previous boot does not linger after a fresh, re-authorized
    start. Cached ~5s so it is cheap to call per request."""
    now = time.time()
    if _link_cache["v"] is not None and now - _link_cache["t"] < 5.0:
        return _link_cache["v"]
    out = {"connected": True, "reason": None}
    p = _backend_log_path()
    if p and os.path.exists(p):
        try:
            lines = open(p, encoding="utf-8", errors="replace").read().splitlines()
        except Exception:
            lines = []
        start = 0
        for i, ln in enumerate(lines):
            if "CTL server up" in ln:        # one per backend launch
                start = i
        seg = "\n".join(lines[start:])
        on_ready_failed = ("Ignoring exception in on_ready" in seg and
                           ("application_commands" in seg) and
                           ("10003" in seg or "Unknown Channel" in seg or "NotFound" in seg))
        gateway_rejected = "server rejected WebSocket connection: HTTP 403" in seg
        if on_ready_failed or gateway_rejected:
            out = {"connected": False,
                   "reason": "bot_unauthorized" if on_ready_failed else "account_rejected"}
    _link_cache["v"] = out
    _link_cache["t"] = now
    return out


def _onboarded():
    """License + account token + bot token all saved (past the setup pages)."""
    return bool(license_set() and saved_account_token() and saved_bot_token())


def panel_blocked():
    """True when the box is onboarded but the bot is not actually linked.

    Used to stop the bridge from serving the native panel in the unauthorized
    state — Nighty itself would happily serve it, so the gate lives here. Once the
    setup is locked, the panel is never blocked: a completed install always boots
    straight through."""
    if setup_locked():
        return False
    if not (license_set() and saved_account_token() and saved_bot_token()):
        return False
    return not bot_link_status()["connected"]


def get_state():
    ev = stub_get("/bridge/events?since=0")
    methods = stub_get("/api/methods")
    apis = methods.get("apis", [])
    types = [a["type"] for a in apis]
    cur = ev.get("current_url") or ""
    main_idx = api_index(apis, "MainApi", 0)
    app_id = saved_app_id()
    # The popup ("Create app") window can leave a stale discord.com URL in
    # current_url; track the MAIN window's own last URL so we read Nighty's real
    # screen, not a leftover from a side window.
    master_url = ""
    for e in ev.get("events", []):
        if e.get("uid") == "master" and e.get("url"):
            master_url = e["url"]
    if not master_url:
        master_url = cur
    login_markers = ("loading.html", "discord_login.html", "auth.html")
    locked = setup_locked()
    if locked:
        # Lockdown: setup already completed once. Never re-enter any setup or
        # authorization screen — boot straight into the working state. (Reset is
        # only possible via uninstall.sh's 'Reset Configuration Only'.)
        mode = "main"
    elif not license_set():
        # The license is the very first thing Nighty needs — gate before sign-in.
        mode = "license"
    elif saved_account_token() and saved_bot_token():
        # Fully onboarded. Authorization is gated on whether the bot is actually
        # linked (bot_link_status), NOT on whether Nighty served the panel — Nighty
        # boots and serves it even when the bot is disconnected, so we must check
        # the link explicitly and present the authorize step when it is broken.
        if not bot_link_status()["connected"]:
            mode = "authorize"
        else:
            mode = "main"
    elif ("app_token.html" in cur) or ("discord.com" in master_url) or ("AppCreateApi" in types) or ("BotTokenApi" in types):
        mode = "bottoken"
    elif (not cur) or any(m in cur for m in login_markers):
        mode = "login"
    else:
        mode = "main"
    # Acquire the lock the first time a complete, working setup is observed: the
    # native panel is actually serving AND the bot link is healthy. From then on
    # the box is locked into the working state across restarts.
    if not locked and mode == "main" and web_up() and license_set() \
            and saved_account_token() and saved_bot_token() and bot_link_status()["connected"]:
        _set_lock()
        locked = True
    return {"mode": mode, "cur": cur, "main_idx": main_idx, "total": ev.get("total", 0),
            "app_id": app_id, "events": ev.get("events", []), "locked": locked,
            "ready": bool(web_up())}


CSS = """:root{--bg:#0b0f1a;--card:#121826;--card2:#0e1422;--line:#1e2942;--txt:#e7ecf6;--mut:#8a97b1;--ac:#3b82f6;--ac2:#60a5fa;--ok:#22c55e;--err:#ef4444}
*{box-sizing:border-box}body{margin:0;font-family:'Segoe UI',system-ui,Arial,sans-serif;color:var(--txt);min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;
background:radial-gradient(1200px 600px at 80% -10%,#16223f 0,transparent 60%),radial-gradient(900px 500px at -10% 110%,#0f1b33 0,transparent 55%),var(--bg)}
.card{width:100%;max-width:480px;background:linear-gradient(180deg,var(--card),var(--card2));border:1px solid var(--line);border-radius:18px;padding:34px 32px;box-shadow:0 30px 80px rgba(0,0,0,.55)}
.logo{display:flex;align-items:center;gap:12px;margin-bottom:6px}.logo .mark{width:40px;height:40px;border-radius:11px;background:linear-gradient(135deg,#2563eb,#22d3ee);display:flex;align-items:center;justify-content:center;font-weight:800;font-size:22px;color:#fff}
h1{font-size:20px;margin:0;font-weight:700}.sub{color:var(--mut);font-size:13px;margin:4px 0 20px}
label{display:block;font-size:12px;color:var(--mut);margin:14px 0 7px;text-transform:uppercase;letter-spacing:.6px}
input{width:100%;background:#0a0f1c;border:1px solid var(--line);border-radius:11px;color:var(--txt);padding:13px 14px;font-size:14px;outline:none}
input:focus{border-color:var(--ac);box-shadow:0 0 0 3px rgba(59,130,246,.18)}
ol{color:var(--mut);font-size:13px;line-height:1.7;padding-left:18px;margin:6px 0 0}ol b{color:var(--txt)}
a.btnlink{display:inline-block;margin-top:10px;color:var(--ac2);text-decoration:none;font-size:13px;border:1px solid var(--line);padding:8px 12px;border-radius:9px}
button.primary{width:100%;margin-top:18px;background:linear-gradient(135deg,var(--ac),#2563eb);border:0;color:#fff;font-size:15px;font-weight:600;padding:13px;border-radius:11px;cursor:pointer}
button.primary:disabled{opacity:.55;cursor:not-allowed}
.status{margin-top:16px;font-size:13px;min-height:20px;color:var(--mut);display:flex;align-items:center;gap:8px}
.status.ok{color:var(--ok)}.status.err{color:var(--err)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--mut)}.dot.ok{background:var(--ok)}.dot.err{background:var(--err)}.dot.busy{background:var(--ac2);animation:p 1s infinite}@keyframes p{0%,100%{opacity:.3}50%{opacity:1}}
.foot{margin-top:20px;border-top:1px solid var(--line);padding-top:14px;color:#5d6a86;font-size:11px;line-height:1.6}
.intents{display:none;margin-top:14px;background:#1c1407;border:1px solid #5a3a12;border-radius:11px;padding:12px 14px}
.intents .ih{color:#f4c47a;font-size:13px;margin-bottom:6px;font-weight:600}
.intents ul{margin:0 0 9px;padding-left:20px;color:var(--txt);font-size:13px;line-height:1.7}
.intents .hint{color:#9c8456;font-size:12px;margin-top:8px}"""

COMMON_JS = """
var msg=document.getElementById('msg'),st=document.getElementById('st'),dot=document.getElementById('dot');
function set(t,cls){msg.textContent=t;st.className='status'+(cls?' '+cls:'');dot.className='dot '+(cls==='ok'?'ok':cls==='err'?'err':'busy');}
// Robust JSON fetch. Returns parsed JSON, or null if the response was not JSON
// (e.g. an HTML page the bridge can briefly return while the backend is
// restarting / mid-proxy). Callers treat null as "not ready yet" and retry,
// instead of crashing on "Unexpected token '<'".
async function jtry(path,opts){try{var r=await fetch(path,Object.assign({cache:'no-store'},opts||{}));var t=await r.text();try{return JSON.parse(t);}catch(e){return null;}}catch(e){return null;}}
// POST JSON, auto-retrying while the response is non-JSON (backend still coming up).
async function jpost(path,body,onwait){for(var i=0;i<60;i++){var j=await jtry(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});if(j!==null)return j;if(onwait)onwait(i+1);await new Promise(function(r){setTimeout(r,2500);});}return {ok:false,error:'backend not responding (still starting?) — reload to retry'};}
async function rpc(idx,method,args){var j=await jpost('/rpc',{api:idx,method:method,args:args||[]});if(!j||!j.ok)throw new Error((j&&j.error)||'rpc');return j.result;}
"""


def license_page():
    return ("""<!DOCTYPE html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Nighty — Activate</title><style>%s</style></head><body><div class=card>
<div class=logo><div class=mark>N</div><h1>Activate Nighty</h1></div>
<div class=sub>Step 1/3 — enter your <b>Nighty license key</b>. Nighty needs it to start; without it the bot can sign in but <b>no commands will work</b>.</div>
<label>License key</label><input id=key type=password placeholder="Paste your Nighty license key…" autocomplete=off spellcheck=false>
<button class=primary id=go>Activate</button>
<div class=status id=st><span class="dot" id=dot></span><span id=msg>Paste your license key to begin.</span></div>
<div class=foot>This is the license key from your Nighty purchase (your Nighty dashboard or order email). It is saved locally to Nighty's own config only — the bridge does not store it.</div></div>
<script>%s%s
var key=document.getElementById('key'),go=document.getElementById('go');
go.onclick=async function(){var k=(key.value||'').trim();if(!k){set('Enter your license key.','err');return;}go.disabled=true;set('Activating…');
 var j=await jpost('/license',{key:k},function(){set('Backend is still starting — waiting…','busy');});
 if(!j||!j.ok){set('Error: '+((j&&j.error)||'activation failed'),'err');go.disabled=false;return;}
 set('License saved. Restarting Nighty to apply it (about 90 seconds)…','ok');
 var tries=0;(function chk(){jtry('/state').then(function(s){if(s&&s.mode&&s.mode!=='license'){set('Ready - continuing to sign-in…','ok');location.href='/';return;}if(++tries>80){set('Taking longer than usual - reloading…','err');location.href='/';return;}setTimeout(chk,3000);});})();};
key.addEventListener('keydown',function(e){if(e.key==='Enter'&&!go.disabled)go.click();});
</script></body></html>""" % (CSS, "", COMMON_JS)).encode("utf-8")


def login_page(main_idx):
    return ("""<!DOCTYPE html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Nighty — Sign in</title><style>%s</style></head><body><div class=card>
<div class=logo><div class=mark>N</div><h1>Nighty</h1></div>
<div class=sub>Step 2/3 — sign in with your Discord account token.</div>
<label>Discord token</label><input id=tok type=password placeholder="Paste your Discord account token…" autocomplete=off spellcheck=false>
<button class=primary id=go disabled>Sign in</button>
<div class=status id=st><span class="dot busy" id=dot></span><span id=msg>Connecting to backend…</span></div>
<div class=foot>Token path: browser → this bridge → Nighty backend. Nothing is stored by the bridge.</div></div>
<script>var MAIN=%d;%s
var tok=document.getElementById('tok'),go=document.getElementById('go');
rpc(MAIN,'getAvailableLoginOptions',[]).then(function(){set('Backend ready. Enter token and sign in.','ok');go.disabled=false;tok.focus();}).catch(function(e){set('Backend unavailable: '+e.message,'err');});
go.onclick=async function(){var t=(tok.value||'').trim();if(!t){set('Enter a token.','err');return;}go.disabled=true;set('Signing in…');
 try{await rpc(MAIN,'saveTokenToConfig',[t]);}catch(e){}
 var tries=0;(function chk(){jtry('/state').then(function(s){if(s&&s.mode&&s.mode!=='login'){set('Signed in. Continuing…','ok');location.href='/';return;}if(++tries>12){set('Token was not accepted. Check it and try again.','err');go.disabled=false;return;}setTimeout(chk,1200);});})();};
tok.addEventListener('keydown',function(e){if(e.key==='Enter'&&!go.disabled)go.click();});
</script></body></html>""" % (CSS, main_idx, COMMON_JS)).encode("utf-8")


def bottoken_page(main_idx, app_id=None):
    return ("""<!DOCTYPE html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Nighty — Connect your bot</title><style>%s</style></head><body><div class=card>
<div class=logo><div class=mark>N</div><h1>Connect your bot</h1></div>
<div class=sub>Step 3/3 — paste your Discord <b>bot token</b>. It is verified, and checked for the required intents, <b>before</b> it is handed to Nighty.</div>
<label>Bot token</label>
<input id=tok type=password placeholder="Paste your bot token…" autocomplete=off spellcheck=false>
<div class=intents id=intents>
  <div class=ih>These intents are still OFF — turn them on, then re-check:</div>
  <ul id=misslist></ul>
  <a class=btnlink id=portal target=_blank rel=noopener href="#">Open the Bot settings ↗</a>
  <div class=hint>Toggle them under <b>Privileged Gateway Intents</b>, Save Changes, then press <b>Validate &amp; connect</b> again.</div>
</div>
<button class=primary id=go>Validate &amp; connect</button>
<div class=status id=st><span class="dot" id=dot></span><span id=msg>Paste your bot token to begin.</span></div>
<div class=foot>Get a token at the <b>Developer Portal -> your app -> Bot -> Reset Token</b>, and enable the <b>Presence</b>, <b>Server Members</b> and <b>Message Content</b> intents on that same page. The token is sent only to your local Nighty backend — the bridge never stores it.</div></div>
<script>var MAIN=%d;%s
var tok=document.getElementById('tok'),go=document.getElementById('go');
var intents=document.getElementById('intents'),misslist=document.getElementById('misslist'),portal=document.getElementById('portal');
function showIntents(miss,url){misslist.innerHTML=(miss||[]).map(function(m){return '<li>'+m+'</li>';}).join('');if(url)portal.href=url;intents.style.display='block';}
function hideIntents(){intents.style.display='none';}
async function connect(t){set('Bot verified. Handing it to Nighty…','ok');
 var j=await jpost('/submit_token',{token:t},function(){set('Backend is still starting — waiting…','busy');});
 if(!j){set('Backend not ready — please try again in a moment.','err');go.disabled=false;return;}
 if(!j.ok){if(j.needs_intents){showIntents(j.missing,j.intents_url);}set('Error: '+(j.error||'submit failed'),'err');go.disabled=false;return;}
 set('Connected as '+j.bot_name+'. Starting Nighty…','ok');
 var tries=0;(function chk(){jtry('/state').then(function(s){if(s&&(s.mode==='main'||s.mode==='authorize')){set(s.mode==='authorize'?'Bot connected — authorization needed…':'Done! Loading panel…','ok');location.href='/';return;}if(++tries>70){set('Bot connected — continuing. Reload if this does not move on shortly.','err');go.disabled=false;return;}setTimeout(chk,2500);});})();}
go.onclick=async function(){var t=(tok.value||'').trim();if(!t){set('Paste your bot token first.','err');return;}
 go.disabled=true;hideIntents();set('Checking the bot token with Discord…');
 var j=await jpost('/check_token',{token:t},function(){set('Backend is still starting — waiting…','busy');});
 if(!j){set('Backend not ready — please try again in a moment.','err');go.disabled=false;return;}
 if(!j.valid){set('Error: '+(j.error||'invalid token'),'err');go.disabled=false;return;}
 if(!j.intents_ok){set('Bot ‘'+j.bot_name+'’ found, but some intents are OFF.','err');showIntents(j.missing,j.intents_url);go.disabled=false;return;}
 await connect(t);};
tok.addEventListener('keydown',function(e){if(e.key==='Enter'&&!go.disabled)go.click();});
</script></body></html>""" % (CSS, main_idx, COMMON_JS)).encode("utf-8")


def authorize_page(app_id=None):
    info = authorization_status()
    url = info.get("authorize_url") or ""
    bot_name = info.get("bot_name") or "your bot"
    integ = info.get("integration") or "guild"
    where = ("to your Discord account" if integ == "user"
             else "to a Discord server you are in")
    disabled = "" if url else "disabled"
    return ("""<!DOCTYPE html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Nighty — Authorize</title><style>%s</style></head><body><div class=card>
<div class=logo><div class=mark>N</div><h1>Authorize the bot</h1></div>
<div class=sub>Almost done. Nighty needs you to <b>authorize <span id=botn>%s</span></b> %s before it can start. This is a one-time Discord step — no password is handled here.</div>
<ol>
  <li>Click <b>Authorize on Discord</b> below and approve the prompt.</li>
  <li>Come back here and press <b>I&#39;ve authorized - continue</b>.</li>
</ol>
<a class=btnlink id=auth href="%s" target=_blank rel=noopener>Authorize on Discord ↗</a>
<button class=primary id=go %s>I&#39;ve authorized - continue</button>
<div class=status id=st><span class="dot" id=dot></span><span id=msg>%s</span></div>
<div class=foot>The link opens Discord&#39;s official authorization page for your own application (id <span id=appid>%s</span>). The bridge never sees your Discord password — you approve the bot directly on Discord.</div></div>
<script>%s%s
var go=document.getElementById('go');
go.onclick=async function(){go.disabled=true;set('Applying authorization - restarting Nighty (about 90 seconds)…','ok');
 await jpost('/recheck_auth',{});
 var tries=0;(function chk(){jtry('/state').then(function(s){if(s&&s.mode&&s.mode!=='authorize'&&s.mode!=='license'){set('Authorized - starting Nighty…','ok');location.href='/';return;}if(++tries>80){set('Still waiting - if you authorized, reload in a moment.','err');go.disabled=false;return;}setTimeout(chk,3000);});})();};
</script></body></html>""" % (
        CSS, bot_name, where, url, disabled,
        ("Authorize the bot, then continue." if url else
         "Could not build the authorization link - reload after the bot is ready."),
        info.get("app_id") or app_id or "?", "", COMMON_JS)).encode("utf-8")


def loading_page():
    """Polished 'waiting for backend' screen shown whenever the panel is not yet
    reachable (early boot, the stub/backend still starting). It polls /ready and
    swaps itself for the real panel the moment the backend answers — so the user
    never sees a bare white error page during startup."""
    return ("""<!DOCTYPE html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Nighty — Loading</title><style>%s
.lwrap{text-align:center}
.spin{width:46px;height:46px;border-radius:50%%;border:4px solid var(--line);border-top-color:var(--ac2);animation:rot 1s linear infinite;margin:8px auto 0}
@keyframes rot{to{transform:rotate(360deg)}}</style></head><body><div class=card>
<div class=logo style="justify-content:center"><div class=mark>N</div><h1>Nighty</h1></div>
<div class=lwrap><div class=spin></div>
<div class=sub id=msg style="margin-top:20px">Waiting for backend to load…</div>
<div class=foot style="border:0;text-align:center;margin-top:6px">First start can take a minute or two. This page continues on its own — no need to refresh.</div></div></div>
<script>
// Poll the bridge state and advance automatically as soon as a real screen is
// available: the panel is up (mode "main" + ready), OR Nighty needs the user on a
// setup / authorization screen. We never get stuck waiting on a panel that will
// not come up (e.g. the bot is unauthorized) — any non-loading state moves us on.
var n=0, msg=document.getElementById('msg');
function advance(s){
  if(!s||!s.mode) return false;
  if(s.mode==='loading') return false;          // backend still initialising
  if(s.mode==='main') return !!s.ready;          // panel must actually be up
  return true;                                   // license/login/bottoken/authorize
}
function poll(){
  fetch('/state',{cache:'no-store'}).then(function(r){return r.json();}).then(function(s){
    if(advance(s)){ msg.textContent='Ready — loading…'; location.replace('/'); return; }
    n++; msg.textContent='Waiting for backend to load… ('+(n*2)+'s)'; setTimeout(poll,2000);
  }).catch(function(){ n++; setTimeout(poll,2000); });
}
setTimeout(poll,1500);
</script></body></html>""" % (CSS,)).encode("utf-8")


def build_ui():
    try:
        s = get_state()
    except Exception:
        # Stub/backend not reachable yet (early boot) — show the loading screen
        # instead of letting the exception surface as a blank error page.
        return loading_page()
    if s["mode"] == "license":
        return license_page()
    if s["mode"] == "login":
        return login_page(s["main_idx"])
    if s["mode"] == "bottoken":
        return bottoken_page(s["main_idx"], s.get("app_id"))
    if s["mode"] == "authorize":
        return authorize_page(s.get("app_id"))
    # mode == main but the native panel is not up yet — graceful loading screen.
    return loading_page()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype="text/html; charset=utf-8", code=200):
        if isinstance(body, str):
            body = body.encode("utf-8", "replace")
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try: self.wfile.write(body)
        except Exception: pass

    def _proxy(self, method, body=None):
        url = WEBPANEL + self.path
        hdrs = {k: v for k, v in self.headers.items()
                if k.lower() not in ("host", "content-length", "connection", "accept-encoding", "keep-alive")}
        hdrs["Accept-Encoding"] = "identity"
        req = urllib.request.Request(url, data=(body if method == "POST" else None), method=method, headers=hdrs)
        try:
            r = urllib.request.urlopen(req, timeout=40)
            data = r.read(); status = r.status; rh = r.headers
        except urllib.error.HTTPError as e:
            data = e.read(); status = e.code; rh = e.headers
        except Exception as e:
            return self._send("panel proxy error: %r" % (e,), code=502)
        self.send_response(status)
        self.send_header("Content-Type", rh.get("Content-Type", "application/octet-stream"))
        for c in (rh.get_all("Set-Cookie") or []):
            self.send_header("Set-Cookie", c)
        loc = rh.get("Location")
        if loc:
            self.send_header("Location", loc)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data))); self.end_headers()
        try: self.wfile.write(data)
        except Exception: pass

    def _proxy_ws(self):
        """Low-latency, full-duplex tunnel for the native UI's socket.io WebSocket.

        Uses select() so neither direction can starve the other, disables Nagle
        (TCP_NODELAY) so the latency-sensitive engine.io upgrade probe and the
        tiny ping/pong frames are flushed immediately, and turns on TCP
        keepalive. The previous version copied each direction on its own thread
        with Nagle left on; under an emulated backend that delayed the upgrade
        handshake enough that socket.io kept dropping and reconnecting (the
        "Disconnected — Reconnecting…" loop in the Web UI)."""
        try:
            up = socket.create_connection((WEBUI_HOST, WEBUI_PORT), timeout=10)
        except Exception as e:
            return self._send("ws upstream error: %r" % (e,), code=502)
        client = self.connection
        self.close_connection = True
        for s in (up, client):
            try:
                # Clear any inherited recv timeout (create_connection leaves a
                # 10s one) — select() gates readiness, so a blocking recv must
                # never time out and tear down an otherwise-healthy socket.
                s.settimeout(None)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            except Exception:
                pass
        # Replay the client's upgrade request verbatim to the upstream.
        req = "%s %s %s\r\n" % (self.command, self.path, self.request_version)
        for k, v in self.headers.items():
            req += "%s: %s\r\n" % (k, v)
        req += "\r\n"
        try:
            up.sendall(req.encode("latin-1", "replace"))
        except Exception as e:
            up.close(); return self._send("ws send error: %r" % (e,), code=502)

        socks = [client, up]
        try:
            while True:
                # 90s idle ceiling: engine.io pings every 25s, so a fully silent
                # connection for 90s is dead — anything live keeps flowing.
                r, _, x = select.select(socks, [], socks, 90)
                if x or not r:
                    break
                stop = False
                for s in r:
                    try:
                        data = s.recv(65536)
                    except Exception:
                        stop = True; break
                    if not data:           # peer closed this direction → done
                        stop = True; break
                    dst = up if s is client else client
                    try:
                        dst.sendall(data)
                    except Exception:
                        stop = True; break
                if stop:
                    break
        finally:
            for s in (up, client):
                try: s.close()
                except Exception: pass

    def do_GET(self):
        try:
            api_path = self.path.startswith(("/state", "/events", "/ready"))
            # Authorization gate: if the box is onboarded but the bot is not
            # actually linked (and not locked), refuse to serve the native panel
            # and force the authorize screen — Nighty would otherwise serve a
            # non-working panel.
            if not api_path and panel_blocked():
                if self.headers.get("Upgrade", "").lower() == "websocket":
                    self.send_response(403); self.end_headers(); return
                return self._send(authorize_page())
            # Early setup (license / sign-in / bot token): the bridge owns the
            # onboarding UI. Never proxy the native panel here, even if a stale
            # backend still has 8090 open — show our setup pages instead.
            if not api_path and not setup_locked() and not _onboarded():
                if self.headers.get("Upgrade", "").lower() == "websocket":
                    self.send_response(403); self.end_headers(); return
                return self._send(build_ui())
            # Native Web UI ("legacy") is the primary interface.
            if web_up() and not api_path:
                if self.headers.get("Upgrade", "").lower() == "websocket":
                    return self._proxy_ws()
                return self._proxy("GET")
            if self.path.startswith("/ready"):
                # Stub-free readiness probe for the loading screen to poll.
                return self._send(json.dumps({"ready": bool(web_up() and not panel_blocked())}),
                                  "application/json")
            if self.path == "/" or self.path.startswith("/ui"):
                return self._send(build_ui())
            if self.path.startswith("/state"):
                try:
                    return self._send(json.dumps(get_state()), "application/json")
                except Exception:
                    return self._send(json.dumps({"mode": "loading", "locked": setup_locked(),
                                                  "ready": bool(web_up())}), "application/json")
            if self.path.startswith("/events"):
                q = urllib.parse.urlparse(self.path).query
                return self._send(urllib.request.urlopen(STUB + "/bridge/events?" + q, timeout=10).read(), "application/json")
            # Any other path while the panel is not up yet (e.g. an asset request)
            # — show the loading screen, never a bare 404/white page.
            return self._send(loading_page())
        except Exception:
            return self._send(loading_page())

    def do_POST(self):
        try:
            ln = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(ln) if ln else b""
            if self.path.startswith("/license"):
                # Save the Nighty license key (step 0). Required before the bot
                # connects, or its on_ready aborts at KeyError('motd').
                try: key = json.loads(body or b"{}").get("key", "")
                except Exception: key = ""
                return self._send(json.dumps(save_license(key)), "application/json")
            if self.path.startswith("/recheck_auth"):
                # The user confirms they authorized the bot ("I've authorized -
                # continue"). Trust that explicit assertion: lock the setup as
                # complete so the authorization screen never reappears (the only
                # way back is uninstall.sh's reset), and restart the backend so
                # Nighty reconnects with the now-authorized bot. This is the
                # authoritative completion signal — we do not depend on the
                # best-effort log-based link detection here, which can lag or
                # misread once the bot is freshly authorized.
                if _onboarded():
                    _set_lock()
                try:
                    os.system("pkill -f '[N]ighty_stub'")
                except Exception:
                    pass
                return self._send(json.dumps({"ok": True, "restarting": True}), "application/json")
            if self.path.startswith("/check_token"):
                # Validate the pasted bot token + its intents against Discord's
                # API, without handing anything to Nighty yet.
                try: tok = json.loads(body or b"{}").get("token", "")
                except Exception: tok = ""
                return self._send(json.dumps(check_bot_token(tok)), "application/json")
            if self.path.startswith("/submit_token"):
                # Re-validate, then feed the bot token to Nighty (getAppTokenInput).
                try: tok = json.loads(body or b"{}").get("token", "")
                except Exception: tok = ""
                return self._send(json.dumps(submit_bot_token(tok)), "application/json")
            if self.path.startswith("/rpc"):
                req = urllib.request.Request(STUB + "/api/call", data=body,
                                             headers={"Content-Type": "application/json"}, method="POST")
                try:
                    with urllib.request.urlopen(req, timeout=60) as r:
                        return self._send(r.read(), "application/json")
                except urllib.error.HTTPError as e:
                    # The stub returns a JSON body with the *real* error and
                    # traceback on 4xx/5xx — log it (so it lands in the bridge
                    # log) and forward it verbatim instead of masking it behind
                    # a generic "HTTPError 500".
                    err_body = e.read()
                    try:
                        import sys
                        sys.stderr.write("[rpc-error] %s\n" % err_body.decode("utf-8", "replace"))
                        sys.stderr.flush()
                    except Exception:
                        pass
                    return self._send(err_body, "application/json", e.code)
            if web_up() and not panel_blocked():
                return self._proxy("POST", body)
            return self._send("not found", code=404)
        except Exception as e:
            return self._send(json.dumps({"ok": False, "error": repr(e)}), "application/json", 500)


if __name__ == "__main__":
    print("bridge on %s:%d  ->  Web UI %s  /  stub %s" % (HOST, PORT, WEBPANEL, STUB), flush=True)
    # On an already-onboarded box, restore the bot from saved config on boot.
    threading.Thread(target=_auto_resume, daemon=True).start()
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
