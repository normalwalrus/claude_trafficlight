#!/usr/bin/env python3
"""Cross-platform installer for Claude Traffic Light.

    python install.py            # server + hooks + autostart + launch
    python install.py --check    # report what is installed and healthy
    python install.py --remove   # undo everything

Everything is derived from this file's location, so the repo can be cloned
anywhere on Windows, macOS or Linux and this will wire it up correctly.
"""

import argparse
import os
import platform
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import run as runner  # noqa: E402
import install_hooks  # noqa: E402

APP_NAME = "Claude Traffic Light"
SLUG = "claude-trafficlight"


# --- autostart ---------------------------------------------------------------


def autostart_path():
    """Where this platform expects a login item to live."""
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        folder = os.path.join(
            os.environ.get("APPDATA", os.path.join(home, "AppData", "Roaming")),
            "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
        )
        return os.path.join(folder, SLUG + ".vbs")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "LaunchAgents", "com." + SLUG + ".plist")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    return os.path.join(base, "autostart", SLUG + ".desktop")


def autostart_body(python):
    launch = os.path.join(ROOT, "run.py")
    if sys.platform == "win32":
        # A .vbs in Startup runs with no console flash, unlike a .cmd.
        pyw = os.path.join(os.path.dirname(python), "pythonw.exe")
        exe = pyw if os.path.exists(pyw) else python
        return (
            "' " + APP_NAME + " - login item\n"
            'CreateObject("WScript.Shell").Run '
            '"""' + exe + '"" ""' + launch + '"" --quiet", 0, False\n'
        )
    if sys.platform == "darwin":
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n'
            "<dict>\n"
            "  <key>Label</key><string>com." + SLUG + "</string>\n"
            "  <key>ProgramArguments</key>\n"
            "  <array>\n"
            "    <string>" + python + "</string>\n"
            "    <string>" + launch + "</string>\n"
            "    <string>--quiet</string>\n"
            "  </array>\n"
            "  <key>RunAtLoad</key><true/>\n"
            "  <key>WorkingDirectory</key><string>" + ROOT + "</string>\n"
            "</dict>\n"
            "</plist>\n"
        )
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=" + APP_NAME + "\n"
        "Exec=" + python + " " + launch + " --quiet\n"
        "Path=" + ROOT + "\n"
        "X-GNOME-Autostart-enabled=true\n"
        "Terminal=false\n"
    )


def install_autostart(python):
    path = autostart_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(autostart_body(python))
        if sys.platform == "darwin":
            # Best effort: without this it still starts at the next login.
            subprocess.run(
                ["launchctl", "load", "-w", path],
                capture_output=True, timeout=30,
            )
        elif sys.platform.startswith("linux"):
            os.chmod(path, 0o755)
        return path
    except Exception as exc:
        print("  could not write autostart entry: " + str(exc))
        return None


def remove_autostart():
    path = autostart_path()
    if not os.path.exists(path):
        return False
    try:
        if sys.platform == "darwin":
            subprocess.run(
                ["launchctl", "unload", "-w", path], capture_output=True, timeout=30
            )
        os.remove(path)
        return True
    except Exception:
        return False


# --- hooks -------------------------------------------------------------------


def hooks_installed():
    """Return (count, stale) - how many of our hook entries exist, and whether
    any of them point at a path that no longer exists (repo moved/renamed)."""
    try:
        settings = install_hooks.load_settings()
    except Exception:
        return 0, False
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return 0, False
    count, stale = 0, False
    for groups in hooks.values():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            for h in group.get("hooks") or []:
                if not isinstance(h, dict):
                    continue
                cmd = str(h.get("command", ""))
                if install_hooks.is_ours(cmd):
                    count += 1
                    # Both installers stage into the payload directory, so a
                    # command pointing anywhere else is a leftover from an
                    # older install that referenced the repo directly.
                    if install_hooks.PAYLOAD_DIR not in cmd:
                        stale = True
    return count, stale


# --- commands ----------------------------------------------------------------


def cmd_check():
    python = install_hooks.python_exe()
    print(APP_NAME + " - status")
    print("  repo:        " + ROOT)
    print("  platform:    " + platform.platform())
    print("  python:      " + python)
    print("  tkinter:     " + ("yes" if runner.have_tk() else
                               "NO  (" + runner.tk_install_hint() + ")"))

    compose = runner.compose_cmd()
    print("  docker:      " + (" ".join(compose) if compose else "NOT FOUND"))
    healthy = runner.server_healthy()
    print("  server:      " + ("healthy at " + runner.server_url()
                               if healthy else "not responding"))

    count, stale = hooks_installed()
    if count == 0:
        print("  hooks:       not installed")
    elif stale:
        print("  hooks:       " + str(count) + " installed but pointing at a "
              "DIFFERENT copy of the repo")
        print("               re-run 'python install.py' to repoint them here")
    else:
        print("  hooks:       " + str(count) + " installed for this repo")

    auto = autostart_path()
    print("  autostart:   " + (auto if os.path.exists(auto) else "not installed"))
    print("  panel:       " + ("running" if runner.panel_running() else "not running"))

    ok = healthy and count > 0 and not stale
    print("")
    print("Ready." if ok else "Not fully set up - run: python install.py")
    return 0 if ok else 1


def cmd_install(args):
    python = install_hooks.python_exe()
    print(APP_NAME + " - install")
    print("  repo:   " + ROOT)
    print("  python: " + python)
    print("")

    if not args.no_server:
        if not runner.ensure_server(build=True):
            return 1
        print("Server healthy at " + runner.server_url())

    if not args.no_hooks:
        print("")
        rc = install_hooks.main([])
        if rc != 0:
            return rc

    if not args.no_autostart:
        print("")
        path = install_autostart(python)
        if path:
            print("Autostart entry: " + path)

    if not args.no_panel:
        print("")
        if args.web:
            runner.open_web()
            print("Opened the browser panel.")
        elif runner.panel_running():
            print("Panel already running.")
        elif runner.launch_panel():
            print("Panel started.")
        else:
            print("tkinter is unavailable (" + runner.tk_install_hint() + ").")
            print("Falling back to the browser panel at " + runner.server_url() + "/")
            runner.open_web()

    print("")
    print("Done. Restart any running Claude Code sessions so the hooks apply.")
    return 0


def cmd_remove(args):
    print(APP_NAME + " - remove")
    rc = install_hooks.main(["--remove"])
    if remove_autostart():
        print("Removed autostart entry.")
    compose = runner.compose_cmd()
    if compose and not args.keep_server:
        subprocess.run(compose + ["down"], cwd=ROOT, timeout=300)
    print("")
    print("Removed. The panel, if running, can be closed from its own menu.")
    return rc


def main(argv=None):
    ap = argparse.ArgumentParser(description="Install Claude Traffic Light")
    ap.add_argument("--check", action="store_true", help="report status and exit")
    ap.add_argument("--remove", action="store_true", help="undo the installation")
    ap.add_argument("--keep-server", action="store_true",
                    help="with --remove, leave the container running")
    ap.add_argument("--web", action="store_true",
                    help="use the browser panel instead of the native one")
    ap.add_argument("--no-server", action="store_true")
    ap.add_argument("--no-hooks", action="store_true")
    ap.add_argument("--no-autostart", action="store_true")
    ap.add_argument("--no-panel", action="store_true")
    args = ap.parse_args(argv)

    if args.check:
        return cmd_check()
    if args.remove:
        return cmd_remove(args)
    return cmd_install(args)


if __name__ == "__main__":
    sys.exit(main())
