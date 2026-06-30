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
make the panel's "save profile" fail with HTTP 500.

For Rich Presence it is type-aware: only RPC presets crash the backend under
Box64 (image-asset fetch), so it disables auto-start / stops in memory ONLY for
RPC-type active profiles. Custom-status rotators are safe and left under the
user's control, so "Run last active profile on startup" works for them.

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
                ec.enforce_safe_presence(appdata)     # heal encoding; disable only RPC auto-start
            # Stop an RPC preset already running in memory (no-op for safe profiles).
            ec.stop_unsafe_running_profile()
        except Exception as e:
            print("[guard] error:", repr(e))
        time.sleep(interval)


if __name__ == "__main__":
    main()
