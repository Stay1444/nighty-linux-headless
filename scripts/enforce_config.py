#!/usr/bin/env python3
"""
Enforce Nighty's on-disk configuration for a headless deployment.

Run once before each launch (by run.sh) and continuously (by webui_guard.py):

  • notifications.json — disable EVERY boolean under the `toast` and `sound`
    groups, so a headless box never tries to raise desktop popups or play sounds.
  • web_config.json    — set the Web UI credentials / host / port from .env.
  • nighty.config      — force  web = true  (Web UI must always be available;
    it is the only usable interface on a machine without a desktop GUI).

All locations come from the environment (see .env.example). Nothing is hardcoded.

Settings are read from the project's `.env` FILE first, and only then from the
process environment. Parsing `.env` directly is deliberate: the Web UI credentials
(and the runtime paths) must come from the user's file even when this runs with a
stale or empty environment — e.g. after a configuration reset, or detached from
run.sh — so we never silently fall back to a hardcoded default while a `.env`
exists.
"""
import os, sys, json, glob
import urllib.request


def _find_env_file():
    """Locate the project's .env. It lives at the repo root, one level above this
    scripts/ directory; allow an override via NIGHTY_ENV for unusual layouts."""
    override = os.environ.get("NIGHTY_ENV")
    if override and os.path.isfile(override):
        return override
    here = os.path.dirname(os.path.abspath(__file__))
    for c in (os.path.join(os.path.dirname(here), ".env"), os.path.join(here, ".env")):
        if os.path.isfile(c):
            return c
    return None


# Parsed .env, cached and refreshed when the file changes (so live edits to the
# credentials are picked up by the continuous guard without a restart).
_ENV_CACHE = {"path": None, "mtime": None, "vals": {}}


def _env_file_vals():
    path = _find_env_file()
    if not path:
        return {}
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return _ENV_CACHE["vals"]
    if _ENV_CACHE["path"] == path and _ENV_CACHE["mtime"] == mtime:
        return _ENV_CACHE["vals"]
    vals = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                    v = v[1:-1]   # strip matching surrounding quotes
                vals[k] = v
    except Exception:
        return _ENV_CACHE["vals"]
    _ENV_CACHE.update(path=path, mtime=mtime, vals=vals)
    return vals


def env(k, d=None):
    """Resolve a setting, preferring the project's .env FILE over the process
    environment, and only then a hardcoded default. As long as .env exists and
    defines the key, that value wins — never the default."""
    fv = _env_file_vals()
    if k in fv:
        return fv[k]
    v = os.environ.get(k)
    return v if v is not None else d


def find_appdata():
    """Locate '.../Nighty Selfbot' inside the wine prefix."""
    prefix = env("WINEPREFIX") or os.path.join(env("NIGHTY_HOME", "/opt/nighty"), "prefix")
    user = env("WINEUSER") or ""
    candidates = []
    if user:
        candidates.append(os.path.join(prefix, "drive_c", "users", user,
                                       "AppData", "Roaming", "Nighty Selfbot"))
    candidates += glob.glob(os.path.join(prefix, "drive_c", "users", "*",
                                         "AppData", "Roaming", "Nighty Selfbot"))
    for c in candidates:
        if os.path.isdir(c):
            return c
    return None


def _load(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=4, ensure_ascii=False)
    os.replace(tmp, path)


def _disable_bools(node):
    """Recursively set every boolean leaf to False. Returns True if anything changed."""
    changed = False
    if isinstance(node, dict):
        for k, v in list(node.items()):
            if isinstance(v, bool):
                if v:
                    node[k] = False
                    changed = True
            elif isinstance(v, (dict, list)):
                changed = _disable_bools(v) or changed
    elif isinstance(node, list):
        for item in node:
            changed = _disable_bools(item) or changed
    return changed


def enforce_notifications(appdata):
    path = os.path.join(appdata, "data", "notifications.json")
    d = _load(path)
    if d is None:
        return "skip (missing)"
    changed = False
    for group in ("toast", "sound"):
        val = d.get(group)
        if isinstance(val, (dict, list)):
            changed = _disable_bools(val) or changed
        elif isinstance(val, bool) and val:
            d[group] = False
            changed = True
    if changed:
        _save(path, d)
        return "updated (toast+sound disabled)"
    return "ok (already disabled)"


def enforce_web(appdata):
    msgs = []

    # web_config.json — credentials, host, port
    wc_path = os.path.join(appdata, "web_config.json")
    wc = _load(wc_path)
    if wc is None:
        wc = {}
    desired = {
        "username": env("WEBUI_USERNAME", "admin"),
        "password": env("WEBUI_PASSWORD", ""),
        "host": env("WEBUI_HOST", "127.0.0.1"),
        "port": int(env("WEBUI_PORT", "8090")),
    }
    chg = False
    for k, v in desired.items():
        if k == "password" and v == "":
            continue  # never blank an existing password just because env is empty
        if wc.get(k) != v:
            wc[k] = v
            chg = True
    if chg:
        _save(wc_path, wc)
        msgs.append("web_config updated")
    else:
        msgs.append("web_config ok")

    # nighty.config — web must stay true (hard enforcement)
    nc_path = os.path.join(appdata, "nighty.config")
    nc = _load(nc_path)
    if nc is None:
        msgs.append("nighty.config missing")
    elif nc.get("web") is not True:
        nc["web"] = True
        _save(nc_path, nc)
        msgs.append("nighty.config web -> true")
    else:
        msgs.append("web already true")

    return "; ".join(msgs)


def enforce_rpc_off(appdata):
    """Headless hardening: keep Nighty's Rich Presence / status-rotator profile
    from running at startup. A headless selfbot has no reason to broadcast a
    rotating custom status or Rich Presence, and that presence machinery is part
    of what keeps the bot's event loop busy. (The biggest offender — the lyrics
    fetch the presence task makes — is neutralised at the network level; see the
    lrclib blackhole in install.sh.)"""
    path = os.path.join(appdata, "data", "profile.json")
    d = _load(path)
    if not isinstance(d, dict):
        return "skip (no profile.json)"
    changed = False
    for k in ("running", "run_at_startup"):
        if d.get(k) is not False:
            d[k] = False
            changed = True
    if changed:
        _save(path, d)
        return "Rich Presence / status-rotator disabled"
    return "ok (already off)"


def _stub_call(method, args=(), api=0, timeout=8):
    """Invoke a Nighty MainApi method through the stub control server
    (loopback only). Returns the parsed JSON reply, or None on any failure."""
    port = env("NIGHTY_STUB_PORT") or env("STUB_PORT", "8765")
    body = json.dumps({"api": api, "method": method, "args": list(args)}).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:%s/api/call" % port, data=body,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def enforce_rpc_off_runtime():
    """Stop a Rich-Presence / status-rotator profile that is running IN MEMORY.

    Setting profile.json running=false on disk does NOT stop an already-running
    rotator (Nighty drives it from in-memory state). On a headless box under
    emulation the presence path is also a crash hazard: running an RPC preset
    makes Nighty fetch its external assets through the bundled Go tls-client,
    whose JSON handling intermittently segfaults under Box64 and takes the whole
    backend down. So if a profile is reported running, we stop it the same way
    the UI does — Nighty's own toggleUserProfile — which cleanly ends the loop."""
    res = _stub_call("getUserProfiles")
    if not res or not res.get("ok"):
        return "skip (stub not ready)"
    prof = res.get("result") or {}
    if not prof.get("running"):
        return "ok (no profile running)"
    active = prof.get("active_profile")
    if not active:
        return "running but no active_profile — left as is"
    off = _stub_call("toggleUserProfile", [active])
    if off and off.get("ok"):
        return "stopped running profile %r" % active
    return "tried to stop %r (stub busy)" % active


def main():
    appdata = find_appdata()
    if not appdata:
        print("[enforce] Nighty appdata not found yet — it appears after the first launch.")
        return 0
    print("[enforce] appdata:", appdata)
    print("[enforce] notifications:", enforce_notifications(appdata))
    print("[enforce] web:", enforce_web(appdata))
    print("[enforce] presence:", enforce_rpc_off(appdata))
    return 0


if __name__ == "__main__":
    sys.exit(main())
