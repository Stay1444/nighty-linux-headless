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
    # Nighty reads these files with the process default encoding — cp1252 under
    # Wine, exactly as on Windows. Any raw non-ASCII byte we write here (e.g. an
    # emoji inside a Rich-Presence / Custom-Status profile) makes Nighty's own
    # read raise UnicodeDecodeError('charmap', …) and return HTTP 500 on save.
    # So we escape non-ASCII (ensure_ascii=True), matching how Nighty itself
    # writes the file and keeping it pure-ASCII and round-trippable.
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=4, ensure_ascii=True)
    os.replace(tmp, path)


def _has_non_ascii(path):
    """True if the file on disk contains any byte > 127 (i.e. it was written with
    raw UTF-8 and is unreadable by Nighty's cp1252 reader)."""
    try:
        with open(path, "rb") as f:
            return any(b > 127 for b in f.read())
    except OSError:
        return False


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


def normalize_profile_encoding(appdata):
    """Keep data/profile.json readable by Nighty under Wine.

    Nighty reads its config/state JSON with the process default encoding (cp1252
    under Wine, exactly as on Windows), so a file holding raw non-ASCII bytes
    (e.g. emoji in a Rich-Presence profile) makes Nighty's own read raise
    UnicodeDecodeError and the panel's "save profile" return HTTP 500. If the
    on-disk file has any non-ASCII byte, re-save it via _save (ensure_ascii=True)
    to normalise it back to ASCII.

    This deliberately does NOT touch `running` / `run_at_startup`: the Web UI's
    "Run last active profile on startup" option is left under the user's control.
    Earlier builds force-disabled the presence rotator here (and stopped it in
    memory) to dodge an intermittent Box64 segfault in Nighty's bundled Go
    tls-client while fetching RPC image assets — but that also silently reverted
    this user-facing option, so the suppression was removed. Presets that fetch
    external image assets may still occasionally crash the backend under
    emulation; the backend auto-relaunches (see scripts/run.sh)."""
    path = os.path.join(appdata, "data", "profile.json")
    if not _has_non_ascii(path):
        return "ok (ascii)"
    d = _load(path)
    if not isinstance(d, dict):
        return "skip (unreadable profile.json)"
    _save(path, d)
    return "normalised encoding"


# Notification sounds Nighty fetches on demand from its CDN. Nighty downloads
# them with urllib, whose default User-Agent ("Python-urllib/x.y") Cloudflare
# rejects with HTTP 403 — so the file never lands in data/sounds/ and Nighty
# retries on every matching event (the user-visible "Error downloading sound
# nicknames.mp3 (…/sounds/nickupdates.mp3): HTTP Error 403: Forbidden"). We
# fetch them once with a browser User-Agent (which the CDN serves with 200) so
# they sit on disk and Nighty's download-if-missing path never makes the
# blocked request. The mapping of notification category -> file name lives in
# Nighty's frozen code; this is the set confirmed to exist on the CDN. Add a
# name here if a new notification sound shows the same 403.
SOUND_BASE = "https://nighty.one/download/files/sounds"
SOUND_FILES = ("connected.mp3", "roleupdates.mp3", "nickupdates.mp3")
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def prefetch_sounds(appdata):
    """Pre-seed data/sounds/ with the CDN sounds Nighty would otherwise fail to
    download (Cloudflare 403s its urllib User-Agent). Idempotent and fail-soft:
    skips files already present and never raises into the launch path."""
    dest = os.path.join(appdata, "data", "sounds")
    try:
        os.makedirs(dest, exist_ok=True)
    except OSError:
        return "skip (no sounds dir)"
    have, fetched, failed = 0, [], []
    for name in SOUND_FILES:
        path = os.path.join(dest, name)
        try:
            if os.path.getsize(path) > 0:
                have += 1
                continue
        except OSError:
            pass
        try:
            req = urllib.request.Request("%s/%s" % (SOUND_BASE, name),
                                         headers={"User-Agent": _BROWSER_UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = r.read()
            if not data:
                raise ValueError("empty body")
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
            fetched.append(name)
        except Exception as e:
            failed.append("%s (%s)" % (name, e))
    msg = "%d already present" % have
    if fetched:
        msg += ", fetched %d" % len(fetched)
    if failed:
        msg += ", failed: %s" % ", ".join(failed)
    return msg


def main():
    appdata = find_appdata()
    if not appdata:
        print("[enforce] Nighty appdata not found yet — it appears after the first launch.")
        return 0
    print("[enforce] appdata:", appdata)
    print("[enforce] notifications:", enforce_notifications(appdata))
    print("[enforce] web:", enforce_web(appdata))
    print("[enforce] profile:", normalize_profile_encoding(appdata))
    print("[enforce] sounds:", prefetch_sounds(appdata))
    return 0


if __name__ == "__main__":
    sys.exit(main())
