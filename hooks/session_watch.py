"""Tells the traffic light which Claude sessions are still actually running.

Claude Code writes `~/.claude/sessions/<pid>.json` when a session starts and
removes it when the session ends - but only when it gets the chance. A session
that is killed rather than quit (you close the editor window, the terminal goes
away, the machine sleeps) never runs its shutdown, and the file is left behind.
Nothing rewrites it afterwards: `updatedAt` is not a heartbeat, it is only
touched when the session's status changes, so a file three hours old is exactly
what a session you have had open all morning looks like too.

From inside the container the two are indistinguishable, which is why a closed
session could sit on the panel for ever. Only the host can tell them apart, so
this does: it reads the registry, checks each pid is still running *and* is
still the same process that registered it (pids get reused), and posts the
surviving session ids to the server, which drops every row they do not cover.

It is started by the hook, never by you, and it is deliberately short-lived:
one copy at a time, a poll every few seconds, and it exits once the sessions
are gone or the server stops answering. Closing Claude leaves nothing running.
"""

import json
import os
import sys
import tempfile
import time

POLL_SECONDS = 5.0

# A session has to be missing from two consecutive sweeps before we stop
# reporting it. Claude Code rewrites these files in place, and a sweep that
# catches one mid-write must not take the row away and put it back.
GRACE_SECONDS = POLL_SECONDS * 2 + 1

# Nothing to watch, so stop watching - but not before the session that started
# us has had time to register itself.
EMPTY_GRACE = 120.0

# Nobody is listening: the container is down, or the panel was never running.
OFFLINE_LIMIT = 180.0

# A backstop. Every path out of the loop should fire long before this.
MAX_LIFETIME = 12 * 3600.0

LOCK_STALE = 30.0

SERVER = os.environ.get("CLAUDE_LIGHT_URL", "http://127.0.0.1:8787").rstrip("/")
STATE_DIR = os.path.join(tempfile.gettempdir(), "claude-trafficlight")
LOCK = os.path.join(STATE_DIR, "session-watch.lock")

STILL_ACTIVE = 259


def registry_dir():
    return os.path.join(os.path.expanduser("~"), ".claude", "sessions")


def registry():
    """Every session Claude Code currently has a file for: sid -> entry."""
    out = {}
    try:
        names = os.listdir(registry_dir())
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(registry_dir(), name), "r", encoding="utf-8") as fh:
                entry = json.load(fh)
        except Exception:
            # Being rewritten, or not ours to read. The grace window covers it.
            continue
        if not isinstance(entry, dict):
            continue
        sid = entry.get("sessionId")
        if sid:
            out[str(sid)] = entry
    return out


# --- is that process still there? --------------------------------------------


def _windows_alive(pid, proc_start):
    """Alive on Windows, and still the process that wrote the registry entry.

    The start time is what makes it safe: Windows hands pids out again quickly,
    and a stale entry whose pid now belongs to something else would otherwise
    read as a live Claude session for as long as that program runs.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    ERROR_ACCESS_DENIED = 5

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # Access denied means it is running, we just cannot look at it.
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:
        code = wintypes.DWORD()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            if code.value != STILL_ACTIVE:
                return False
        if not proc_start:
            return True
        created = wintypes.FILETIME()
        rest = (wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME())
        got = kernel32.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(rest[0]),
            ctypes.byref(rest[1]), ctypes.byref(rest[2]),
        )
        if not got:
            return True
        actual = (created.dwHighDateTime << 32) | created.dwLowDateTime
        try:
            want = int(str(proc_start).strip())
        except (TypeError, ValueError):
            return True
        return actual == want
    finally:
        kernel32.CloseHandle(handle)


def pid_alive(pid, proc_start=""):
    """True unless we can show the process is gone.

    Every uncertain case answers True on purpose. Dropping a row for a session
    that is open is much worse than carrying a dead one for a few more seconds:
    the panel exists to be trusted about what is running.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return True
    if pid <= 0:
        return True
    if sys.platform == "win32":
        try:
            return _windows_alive(pid, proc_start)
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True           # someone else's process, but a process
    except Exception:
        return True
    return True


class Watcher:
    def __init__(self, server=SERVER):
        self.server = server
        self.seen = {}            # sid -> when it was last confirmed alive
        self.started = time.time()
        self.last_ok = time.time()
        self.saw_any = False

    def sweep(self, now=None):
        """The session ids to report as live."""
        now = time.time() if now is None else now
        for sid, entry in registry().items():
            if pid_alive(entry.get("pid"), entry.get("procStart")):
                self.seen[sid] = now
        live = [sid for sid, at in self.seen.items() if now - at <= GRACE_SECONDS]
        for sid in [s for s, at in self.seen.items() if now - at > GRACE_SECONDS * 4]:
            self.seen.pop(sid, None)
        if live:
            self.saw_any = True
        return sorted(live)

    def post(self, live):
        import urllib.request

        body = json.dumps({"live": live, "interval": POLL_SECONDS})
        req = urllib.request.Request(
            self.server + "/live",
            data=body.encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=2).read()

    def done(self, live, now=None):
        """True when there is no longer any reason to be running."""
        now = time.time() if now is None else now
        if now - self.started > MAX_LIFETIME:
            return True
        if now - self.last_ok > OFFLINE_LIMIT:
            return True
        if not live and (self.saw_any or now - self.started > EMPTY_GRACE):
            return True
        return False

    def run(self):
        """Poll until there is nothing left to watch."""
        while True:
            touch_lock()
            live = self.sweep()
            try:
                self.post(live)
                self.last_ok = time.time()
            except Exception:
                pass
            if self.done(live):
                break
            time.sleep(POLL_SECONDS)
        try:
            os.remove(LOCK)
        except OSError:
            pass
        return 0


# --- one copy at a time -------------------------------------------------------


def lock_is_live(now=None):
    """True while another watcher is holding the lock and still touching it."""
    now = time.time() if now is None else now
    try:
        age = now - os.path.getmtime(LOCK)
    except OSError:
        return False
    if age > LOCK_STALE:
        return False
    try:
        with open(LOCK, "r", encoding="utf-8") as fh:
            pid = int((fh.read().strip() or "0").split(",")[0])
    except Exception:
        return True           # unreadable but fresh: assume someone is there
    return pid_alive(pid)


def touch_lock(pid=None):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(LOCK, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid() if pid is None else pid) + ","
                     + str(int(time.time())))
    except OSError:
        pass


def ensure_running():
    """Start a watcher if one is not already running. Called from the hook.

    The whole point is that nothing has to be installed or left running: the
    watcher exists only while Claude does. Costs one stat when it is already
    up, which is the case for every hook but the first.

    CLAUDE_LIGHT_WATCH=0 turns it off, at the price of closed sessions
    lingering on the panel until Claude Code next tidies its registry.
    """
    if os.environ.get("CLAUDE_LIGHT_WATCH", "1") in ("0", "false", "no"):
        return False
    if lock_is_live():
        return False
    if not sys.executable:
        return False
    import subprocess

    # Claim the lock before spawning, under this (still running) pid: two hooks
    # firing at the same instant must not start two watchers. The watcher takes
    # the lock over with its own pid on its first sweep, so it is never checked
    # against a hook process that has long since exited.
    touch_lock()
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
              "stderr": subprocess.DEVNULL, "close_fds": True}
    if sys.platform == "win32":
        # Detached and window-less: this outlives the hook, and a console
        # flashing up on every session start would be intolerable.
        kwargs["creationflags"] = 0x00000008 | 0x08000000
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(
            [sys.executable, "-S", os.path.abspath(__file__), "watch"], **kwargs
        )
    except Exception:
        return False
    return True


def main(argv):
    if len(argv) > 1 and argv[1] == "watch":
        # No lock check here: ensure_running() claimed it before spawning us,
        # and re-testing it would see that claim and exit immediately.
        return Watcher().run()
    # Run by hand: report once and say what it found.
    watcher = Watcher()
    live = watcher.sweep()
    print("live sessions: " + (", ".join(live) or "none"))
    try:
        watcher.post(live)
        print("reported to " + watcher.server)
    except Exception as exc:
        print("could not reach " + watcher.server + ": " + repr(exc))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception:
        sys.exit(0)
