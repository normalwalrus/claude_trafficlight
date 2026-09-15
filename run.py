#!/usr/bin/env python3
"""Start Claude Traffic Light.

    python run.py            # start the server, then the native panel
    python run.py --web      # start the server, open the browser panel
    python run.py --no-panel # just the server

Works on Windows, macOS and Linux with nothing installed but Docker and
Python 3.8+. Everything is resolved relative to this file, so the repo can
live anywhere.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
PANEL = os.path.join(ROOT, "panel", "panel.py")
DEFAULT_URL = "http://127.0.0.1:8787"


def server_url():
    return os.environ.get("CLAUDE_LIGHT_URL", DEFAULT_URL).rstrip("/")


# --- docker ------------------------------------------------------------------


def compose_cmd():
    """Return the compose command available here, or None.

    Docker Compose v2 is a docker subcommand; v1 is a separate binary. Both are
    still in the wild, so support whichever is present.
    """
    if shutil.which("docker"):
        try:
            r = subprocess.run(
                ["docker", "compose", "version"],
                capture_output=True,
                timeout=30,
            )
            if r.returncode == 0:
                return ["docker", "compose"]
        except Exception:
            pass
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    return None


def server_healthy(url=None, timeout=1.5):
    url = url or server_url()
    try:
        with urllib.request.urlopen(url + "/healthz", timeout=timeout) as r:
            return bool(json.loads(r.read()).get("ok"))
    except Exception:
        return False


def ensure_server(build=False, quiet=False):
    """Bring the status server up. Returns True if it is healthy afterwards."""
    if server_healthy():
        return True

    compose = compose_cmd()
    if compose is None:
        say(quiet, "Docker not found. Install Docker Desktop, or run the server")
        say(quiet, "directly:  pip install -r server/requirements.txt")
        say(quiet, "           uvicorn app:app --port 8787 --app-dir server")
        return False

    args = compose + ["up", "-d"] + (["--build"] if build else [])
    say(quiet, "Starting server: " + " ".join(args))
    try:
        r = subprocess.run(args, cwd=ROOT, timeout=600)
    except Exception as exc:
        say(quiet, "Failed to run docker compose: " + str(exc))
        return False
    if r.returncode != 0:
        say(quiet, "docker compose failed. Is the Docker daemon running?")
        return False

    for _ in range(40):
        if server_healthy():
            return True
        time.sleep(0.5)
    say(quiet, "Server did not become healthy in time.")
    return False


# --- panel -------------------------------------------------------------------


def have_tk():
    try:
        import tkinter  # noqa: F401

        return True
    except Exception:
        return False


def tk_install_hint():
    if sys.platform == "darwin":
        return "brew install python-tk    (or use python.org's installer)"
    if sys.platform.startswith("linux"):
        return "sudo apt install python3-tk    (or your distro's equivalent)"
    return "reinstall Python with the 'tcl/tk' option enabled"


def panel_running():
    """True if a panel process for THIS repo is already up."""
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' OR "
                    "Name='python.exe'\" | Select-Object -ExpandProperty CommandLine",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout
        else:
            out = subprocess.run(
                ["ps", "-eo", "args"], capture_output=True, text=True, timeout=30
            ).stdout
        return PANEL in out or os.path.join("panel", "panel.py") in out
    except Exception:
        return False


def launch_panel(detach=True):
    """Start the native panel. Returns True if it was launched."""
    if not have_tk():
        return False

    exe = sys.executable or "python3"
    if detach and sys.platform == "win32":
        # pythonw keeps the console from flashing up behind the panel.
        cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(cand):
            exe = cand

    kwargs = {"cwd": ROOT}
    if detach:
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x00000008 | 0x08000000  # DETACHED | NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL
        subprocess.Popen([exe, PANEL], **kwargs)
    else:
        subprocess.call([exe, PANEL], cwd=ROOT)
    return True


def open_web():
    import webbrowser

    webbrowser.open(server_url() + "/")


def say(quiet, msg):
    if not quiet:
        print(msg)


# --- main --------------------------------------------------------------------


def main(argv=None):
    ap = argparse.ArgumentParser(description="Start Claude Traffic Light")
    ap.add_argument("--web", action="store_true", help="open the browser panel")
    ap.add_argument("--no-panel", action="store_true", help="server only")
    ap.add_argument("--no-server", action="store_true", help="assume the server is up")
    ap.add_argument("--build", action="store_true", help="rebuild the image first")
    ap.add_argument(
        "--foreground",
        action="store_true",
        help="run the panel in this terminal instead of detaching",
    )
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if not args.no_server:
        if not ensure_server(build=args.build, quiet=args.quiet):
            return 1
    elif not server_healthy():
        say(args.quiet, "Server is not reachable at " + server_url())
        return 1

    say(args.quiet, "Server ready at " + server_url())

    if args.no_panel:
        return 0

    if args.web:
        say(args.quiet, "Opening browser panel...")
        open_web()
        return 0

    if panel_running() and not args.foreground:
        say(args.quiet, "Panel is already running.")
        return 0

    if launch_panel(detach=not args.foreground):
        say(args.quiet, "Panel started.")
        return 0

    say(args.quiet, "")
    say(args.quiet, "tkinter is not available, so the native panel cannot start.")
    say(args.quiet, "  Fix:      " + tk_install_hint())
    say(args.quiet, "  Or use:   python run.py --web")
    say(args.quiet, "")
    say(args.quiet, "Opening the browser panel instead.")
    open_web()
    return 0


if __name__ == "__main__":
    sys.exit(main())
