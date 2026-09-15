"""Cross-platform desktop integration for the panel.

Two things the panel needs from the OS, neither of which the standard library
offers portably:

    focus_session(pid)  - raise the window hosting a Claude session
    play_wav(data)      - play a WAV held in memory

Windows goes through :mod:`winutil` (ctypes, always available). macOS uses
``osascript`` and ``afplay``; Linux uses ``wmctrl``/``xdotool`` and whichever
of ``paplay``/``aplay``/``ffplay`` is installed. Everything degrades to a
no-op that reports failure rather than raising, so the panel still runs on a
box with none of these present.
"""

import os
import shutil
import subprocess
import sys
import tempfile

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

_MAX_DEPTH = 12

if IS_WIN:
    import winutil


def _run(args, timeout=4):
    """Run a helper command, swallowing every failure mode."""
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except Exception:
        return None


# --- process ancestry (POSIX) ------------------------------------------------


def _posix_parents():
    """{pid: ppid} for every process, via ps."""
    r = _run(["ps", "-eo", "pid=,ppid="])
    if not r or r.returncode != 0:
        return {}
    parents = {}
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            try:
                parents[int(parts[0])] = int(parts[1])
            except ValueError:
                continue
    return parents


def _ancestors(pid):
    """The pid itself followed by its ancestors, nearest first."""
    chain = [int(pid)]
    parents = _posix_parents()
    current = int(pid)
    for _ in range(_MAX_DEPTH):
        nxt = parents.get(current)
        if not nxt or nxt <= 1 or nxt == current:
            break
        chain.append(nxt)
        current = nxt
    return chain


# --- focus -------------------------------------------------------------------


def _focus_mac(pid):
    """Activate the GUI application that owns this process.

    A shell running inside Terminal or VS Code is not itself an application
    process, so try each ancestor until System Events recognises one.
    """
    for candidate in _ancestors(pid):
        script = (
            'tell application "System Events" to set frontmost of '
            "(first process whose unix id is " + str(candidate) + ") to true"
        )
        r = _run(["osascript", "-e", script])
        if r and r.returncode == 0:
            return True, candidate
    return False, 0


def _focus_linux(pid):
    wmctrl = shutil.which("wmctrl")
    xdotool = shutil.which("xdotool")
    if not wmctrl and not xdotool:
        return False, 0

    chain = _ancestors(pid)

    if wmctrl:
        r = _run([wmctrl, "-lp"])
        if r and r.returncode == 0:
            owners = {}
            for line in r.stdout.splitlines():
                parts = line.split(None, 3)
                if len(parts) >= 3:
                    try:
                        owners.setdefault(int(parts[2]), parts[0])
                    except ValueError:
                        continue
            for candidate in chain:
                win = owners.get(candidate)
                if win:
                    got = _run([wmctrl, "-i", "-a", win])
                    if got and got.returncode == 0:
                        return True, candidate

    if xdotool:
        for candidate in chain:
            r = _run([xdotool, "search", "--pid", str(candidate)])
            if r and r.returncode == 0 and r.stdout.strip():
                win = r.stdout.split()[-1]
                got = _run([xdotool, "windowactivate", win])
                if got and got.returncode == 0:
                    return True, candidate
    return False, 0


def focus_session(pid, hwnd_hint=0, cwd=""):
    """Raise the window hosting a Claude session.

    `cwd` is what distinguishes two sessions hosted by the same process - an
    editor with several windows open names each one after the folder it holds.

    Returns (ok, handle). `handle` is an HWND on Windows and the pid of the
    window-owning ancestor elsewhere; it is only used for the status message.
    """
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        pid = 0

    if IS_WIN:
        return winutil.focus_session(pid, hwnd_hint, cwd)
    if not pid:
        return False, 0
    if IS_MAC:
        return _focus_mac(pid)
    if IS_LINUX:
        return _focus_linux(pid)
    return False, 0


def foreground_window():
    """The window currently in front, or 0 where we cannot tell.

    Used to retire a "needs you" banner once you have actually opened the
    session, however you got there.
    """
    if IS_WIN:
        return winutil.foreground_window()
    return 0


def window_title(handle):
    """A human-readable name for whatever focus_session returned."""
    if IS_WIN:
        return winutil.window_title(handle)
    if not handle:
        return ""
    r = _run(["ps", "-p", str(handle), "-o", "comm="])
    if r and r.returncode == 0:
        return os.path.basename(r.stdout.strip())
    return ""


def can_focus():
    """Whether click-to-focus is expected to work on this machine."""
    if IS_WIN:
        return True
    if IS_MAC:
        return shutil.which("osascript") is not None
    if IS_LINUX:
        return bool(shutil.which("wmctrl") or shutil.which("xdotool"))
    return False


def focus_hint():
    """What the user could install to make click-to-focus work."""
    if IS_LINUX and not can_focus():
        return "install wmctrl or xdotool for click-to-focus"
    if IS_MAC:
        return "grant Accessibility permission to your terminal for click-to-focus"
    return ""


# --- audio -------------------------------------------------------------------

_PLAYERS = ("paplay", "aplay", "ffplay", "play")
_tmp_files = {}


def _player():
    if IS_MAC:
        return [shutil.which("afplay")] if shutil.which("afplay") else None
    for name in _PLAYERS:
        path = shutil.which(name)
        if path:
            if name == "ffplay":
                return [path, "-nodisp", "-autoexit", "-loglevel", "quiet"]
            return [path]
    return None


def _cached_wav(key, data):
    """Write each distinct chime to a temp file once and reuse it.

    Needed on every platform: POSIX players take a path, and winsound refuses
    SND_MEMORY together with SND_ASYNC ("Cannot play asynchronously from
    memory"), so playing from a buffer would block the Tk main loop for the
    length of the chime.
    """
    path = _tmp_files.get(key)
    if path and os.path.exists(path):
        return path
    fd, path = tempfile.mkstemp(prefix="claude-light-", suffix=".wav")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    except Exception:
        return None
    _tmp_files[key] = path
    return path


def play_wav(data, key="chime"):
    """Play WAV bytes without blocking. Returns True if playback started."""
    path = _cached_wav(key, data)
    if not path:
        return False

    if IS_WIN:
        try:
            import winsound

            winsound.PlaySound(
                path,
                winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
            )
            return True
        except Exception:
            return False

    player = _player()
    if not player:
        return False
    try:
        subprocess.Popen(
            player + [path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except Exception:
        return False


def can_play():
    return True if IS_WIN else _player() is not None


def capabilities():
    """A short report used by install.py --check and the panel's tooltip."""
    return {
        "platform": sys.platform,
        "focus": can_focus(),
        "focus_hint": focus_hint(),
        "audio": can_play(),
    }


if __name__ == "__main__":
    for k, v in capabilities().items():
        print("%-12s %s" % (k, v))
