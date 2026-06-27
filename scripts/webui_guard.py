#!/usr/bin/env python3
"""
Web UI hard-enforcement loop.

The native desktop GUI cannot render on a headless box, so the Web UI is the
ONLY usable interface. If Nighty — or the user via the interface — disables the
Web UI, this guard re-asserts it within a few seconds:

  • nighty.config  -> web = true
  • web_config.json -> the credentials/host/port from .env

It also keeps the Rich-Presence / status-rotator profile from running: on a
headless box that presence machinery has no purpose, and running an RPC preset
makes Nighty fetch external assets through the bundled Go tls-client, which
intermittently segfaults under emulation (Box64) and crashes the whole backend.
If a profile is ever started (e.g. from the Web UI), the guard stops it again
the same way the UI does — Nighty's own toggle — within one interval.

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
                ec.enforce_rpc_off(appdata)        # keep profile.json off on disk
            # And stop it in memory if it is running (the on-disk flag alone does
            # not halt an already-running rotator). Cheap no-op when nothing runs.
            ec.enforce_rpc_off_runtime()
        except Exception as e:
            print("[guard] error:", repr(e))
        time.sleep(interval)


if __name__ == "__main__":
    main()
