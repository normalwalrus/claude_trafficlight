"""Keeps the desktop panel running, started from the hook.

Nothing in a container can draw a window on your desktop, so the always-on-top
panel has to be a host process. The hooks are the only thing this project runs
on the host, which makes them the only place that can start one - the same
trick :mod:`session_watch` uses to check that sessions are still alive.

So `docker compose up -d` is the whole setup: the container stages the panel
beside the hook payload, and the next Claude session to fire a hook brings the
panel up. It appears when there is something to show, which is when it earns
its place on screen.

Three things stop it being a nuisance:

* It is only staged when TRAFFICLIGHT_DESKTOP_PANEL is on, so a machine that
  never asked for it has nothing to start.
* The panel heartbeats a lock file, so a second one is never started - however
  you started the first, `run.py` included.
* Quit means quit: the panel stamps the moment you chose it, and this refuses
  to start one again until the next `docker compose up` writes a later stamp.
  Collapsing to the badge is the everyday gesture, and the panel remembers
  that by itself - an auto-started panel comes back exactly as you left it.
"""

import json
import os
import sys
import tempfile
import time

STATE_DIR = os.path.join(tempfile.gettempdir(), "claude-trafficlight")

# The panel touches this every few seconds while it runs.
LOCK = os.path.join(STATE_DIR, "panel.lock")
LOCK_STALE = 30.0

PAYLOAD_DIR = os.path.dirname(os.path.abspath(__file__))
PANEL_DIR = os.path.join(PAYLOAD_DIR, "panel")
PANEL = os.path.join(PANEL_DIR, "panel.py")

# Written by the container on every startup, and by the panel when you quit.
STARTED_STAMP = os.path.join(PAYLOAD_DIR, "panel-started.json")
QUIT_STAMP = os.path.join(PAYLOAD_DIR, "panel-quit.json")


def _stamp(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            value = json.load(fh).get("at")
    except Exception:
        return 0.0
    return float(value) if isinstance(value, (int, float)) else 0.0


def write_quit_stamp():
    """Called by the panel when you quit it deliberately."""
    try:
        with open(QUIT_STAMP, "w", encoding="utf-8") as fh:
            json.dump({"at": time.time()}, fh)
        return True
    except OSError:
        return False


def was_quit():
    """True while a deliberate quit still stands.

    The container rewrites the started stamp on every `docker compose up`, so
    that is what brings the panel back - a deliberate act, and the same command
    that turned it on in the first place.
    """
    return _stamp(QUIT_STAMP) > _stamp(STARTED_STAMP)


def running():
    """True while a panel is alive, whoever started it."""
    try:
        age = time.time() - os.path.getmtime(LOCK)
    except OSError:
        return False
    if age > LOCK_STALE:
        return False
    try:
        with open(LOCK, "r", encoding="utf-8") as fh:
            pid = int((fh.read().strip() or "0").split(",")[0])
    except Exception:
        return True            # fresh but unreadable: assume it is there
    if pid <= 0:
        return True
    try:
        import session_watch

        return session_watch.pid_alive(pid)
    except Exception:
        return True


def touch_lock(pid=None):
    """The panel's heartbeat. Also claims the lock before a launch, so two
    hooks firing at the same instant cannot start two panels."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(LOCK, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid() if pid is None else pid) + ","
                     + str(int(time.time())))
        return True
    except OSError:
        return False


def clear_lock():
    try:
        os.remove(LOCK)
    except OSError:
        pass


def have_tk():
    """Whether this interpreter can draw a panel at all.

    The hook and the panel run under the same interpreter, so asking here is
    asking the right question.
    """
    try:
        import importlib.util

        return importlib.util.find_spec("tkinter") is not None
    except Exception:
        return False


def interpreter():
    """The interpreter to launch with - pythonw where there is one, so no
    console flashes up behind the panel."""
    exe = sys.executable
    if not exe:
        return ""
    if sys.platform == "win32":
        base = os.path.basename(exe).lower()
        if base.startswith("python"):
            windowed = os.path.join(os.path.dirname(exe),
                                    base.replace("python", "pythonw", 1))
            if os.path.exists(windowed):
                return windowed
    return exe


def ensure_running():
    """Start the panel if it is wanted, absent, and possible."""
    if os.environ.get("CLAUDE_LIGHT_PANEL", "1") in ("0", "false", "no"):
        return False
    if not os.path.exists(PANEL):
        return False           # not staged: this machine did not ask for it
    if was_quit() or running() or not have_tk():
        return False
    exe = interpreter()
    if not exe:
        return False
    import subprocess

    touch_lock()               # claimed under this pid until the panel takes over
    kwargs = {"cwd": PANEL_DIR, "stdin": subprocess.DEVNULL,
              "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
              "close_fds": True}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000008 | 0x08000000   # DETACHED | NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen([exe, PANEL], **kwargs)
    except Exception:
        clear_lock()
        return False
    return True


def main(argv):
    """Run by hand: say what it would do, and do it."""
    if len(argv) > 1 and argv[1] == "--status":
        print("staged:  " + ("yes" if os.path.exists(PANEL) else "no"))
        print("running: " + ("yes" if running() else "no"))
        print("quit:    " + ("yes, until the next docker compose up"
                             if was_quit() else "no"))
        print("tkinter: " + ("yes" if have_tk() else "no"))
        return 0
    print("started" if ensure_running() else "not started (see --status)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception:
        sys.exit(0)
