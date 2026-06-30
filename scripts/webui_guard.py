#!/usr/bin/env python3
"""
Web UI hard-enforcement loop.

The native desktop GUI cannot render on a headless box, so the Web UI is the
ONLY usable interface. If Nighty — or the user via the interface — disables the
Web UI, this guard re-asserts it within a few seconds:

  • nighty.config  -> web = true
  • web_config.json -> the credentials/host/port from .env

It also keeps data/profile.json ASCII-encoded so Nighty (which reads it with the
cp1252 default under Wine) can always read it back; otherwise emoji in a profile
make the panel's "save profile" fail with HTTP 500. The Rich-Presence rotator and
its "Run last active profile on startup" option are left under the user's control
(an earlier build force-disabled them to dodge a Box64 crash, which also reverted
the user's startup choice).

Started in the background by run.sh (and supervised by systemd). It writes only
when something actually changed, so it never fights Nighty's own writes.
"""
import os
import time

import enforce_config as ec  # same directory


def main():
    if os.environ.get("ENFORCE_WEBUI", "1") != "1":
        print("[guard] ENFORCE_WEBUI disabled — guard not running.")
        return
    interval = float(os.environ.get("ENFORCE_INTERVAL", "5"))
    print(f"[guard] Web UI hard-enforcement active (every {interval}s)")
    while True:
        try:
            appdata = ec.find_appdata()
            if appdata:
                ec.enforce_web(appdata)
                ec.normalize_profile_encoding(appdata)   # keep profile.json cp1252-readable
        except Exception as e:
            print("[guard] error:", repr(e))
        time.sleep(interval)


if __name__ == "__main__":
    main()
