"""Claude Code hook -> Claude Traffic Light.

Installed globally in ~/.claude/settings.json. Reads the hook payload on stdin,
forwards it to the status server, and always exits 0 quickly so it can never
block or break a Claude Code session.

Usage: python claude_light_hook.py <EventName>
"""

import hashlib
import json
import os
import re
import sys
import tempfile

TIMEOUT = 1.0
SERVER = os.environ.get("CLAUDE_LIGHT_URL", "http://127.0.0.1:8787").rstrip("/")
CACHE_DIR = os.path.join(tempfile.gettempdir(), "claude-trafficlight")

# Walking past the shell would land on the desktop itself; keep this in step
# with panel/winutil.py.
STOP_AT = {
    "explorer.exe",
    "services.exe",
    "wininit.exe",
    "winlogon.exe",
    "svchost.exe",
    "system",
    "",
}


def read_payload() -> dict:
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# --- terminal window discovery (Windows, best effort) ------------------------


def find_terminal() -> tuple:
    """Walk up the process tree to the first ancestor owning a visible
    top-level window (Windows Terminal, pwsh, cmd, VS Code...).

    Returns (hwnd, pid) for that ancestor, or (0, 0). The pid matters as much
    as the handle: os.getppid() here is the throwaway shell Claude spawned for
    this one hook invocation and is dead by the time the panel reads it, so the
    window owner is what the panel can still resolve later.
    """
    if sys.platform != "win32":
        return 0, 0
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32 = ctypes.WinDLL("user32", use_last_error=True)

        TH32CS_SNAPPROCESS = 0x00000002
        MAX_PATH = 260

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_char * MAX_PATH),
            ]

        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == -1:
            return 0, 0
        parents = {}
        try:
            entry = PROCESSENTRY32()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
            ok = kernel32.Process32First(snap, ctypes.byref(entry))
            while ok:
                name = entry.szExeFile.decode("mbcs", "replace").lower()
                parents[int(entry.th32ProcessID)] = (
                    int(entry.th32ParentProcessID),
                    name,
                )
                ok = kernel32.Process32Next(snap, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snap)

        # pid -> visible top-level window
        windows = {}
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def cb(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True
            if user32.GetWindow(hwnd, 4):  # GW_OWNER - skip tool/dialog windows
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            windows.setdefault(int(pid.value), int(hwnd))
            return True

        user32.EnumWindows(WNDENUMPROC(cb), 0)

        pid = os.getpid()
        for _ in range(12):  # hook -> claude -> shell -> terminal host
            if pid in windows:
                return windows[pid], pid
            nxt = parents.get(pid, (0, ""))[0]
            if not nxt or nxt == pid:
                break
            # Never climb into explorer.exe: its window is the desktop.
            if parents.get(nxt, (0, ""))[1] in STOP_AT:
                break
            pid = nxt
    except Exception:
        return 0, 0
    return 0, 0


def cache_key(session_id: str) -> str:
    """A filename-safe stand-in for the session id.

    Claude's session ids are uuids, but the id is untrusted input here and
    ``/``, ``:`` and ``*`` would either escape the cache directory or make the
    open() fail silently - which would re-run the whole process/window
    enumeration on every single hook invocation.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "", session_id)[:48]
    digest = hashlib.sha1(session_id.encode("utf-8", "replace")).hexdigest()[:10]
    return (safe + "-" + digest) if safe else digest


def cached_terminal(session_id: str, event: str) -> tuple:
    """Resolve the terminal window once per session, then reuse it.

    Returns (hwnd, owner_pid).
    """
    path = os.path.join(CACHE_DIR, f"hwnd-{cache_key(session_id)}.txt")
    if event == "SessionEnd":
        try:
            os.remove(path)
        except OSError:
            pass
        return 0, 0
    try:
        with open(path, "r", encoding="utf-8") as fh:
            hwnd_s, _, pid_s = fh.read().strip().partition(",")
        hwnd, owner = int(hwnd_s or 0), int(pid_s or 0)
        if 0 < hwnd < 2 ** 48 and 0 <= owner < 2 ** 32:
            return hwnd, owner
    except Exception:
        pass
    hwnd, owner = find_terminal()
    if not hwnd:
        # Nothing found - the window may simply not exist yet (SessionStart
        # fires early). Don't cache the failure or we'd never look again.
        return 0, 0
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(str(hwnd) + "," + str(owner))
    except OSError:
        pass
    return hwnd, owner


def read_usage(session_id: str, payload: dict, event: str):
    """Token accounting for this session, or None if it cannot be read.

    Returning None rather than zeroes matters: the server keeps the last good
    reading instead of blanking the panel when one scan fails.
    """
    if event == "SessionEnd":
        try:
            import transcript

            transcript.forget(session_id)
        except Exception:
            pass
        return None
    path = payload.get("transcript_path")
    if not isinstance(path, str) or not path:
        return None
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import transcript

        return transcript.scan(path, session_id)
    except Exception:
        return None


# --- main --------------------------------------------------------------------


def main() -> int:
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    payload = read_payload()
    # str() everything: the payload is whatever Claude handed us and a hook
    # that raises on a surprising type reports nothing at all.
    event = str(payload.get("hook_event_name") or event or "Unknown")

    session_id = str(payload.get("session_id") or f"pid-{os.getppid()}")
    cwd = str(payload.get("cwd") or os.getcwd())
    project = os.path.basename(os.path.normpath(cwd)) or cwd

    tool = ""
    detail = ""
    if event == "Notification":
        msg = str(payload.get("message") or "")
        # "Claude needs your permission to use Bash" -> "needs permission: Bash"
        if msg.startswith("Claude "):
            msg = msg[len("Claude "):]
        msg = msg.replace("needs your permission to use ", "needs permission: ")
        msg = msg.replace("is waiting for your input", "waiting for input")
        detail = msg[:120]
    elif event == "PreToolUse":
        tool = str(payload.get("tool_name") or "")[:40]
        detail = tool

    hwnd, owner_pid = cached_terminal(session_id, event)
    usage = read_usage(session_id, payload, event)
    body = {
        "session_id": session_id,
        "event": event,
        "cwd": cwd,
        "project": project,
        # The window owner, not os.getppid(): the hook's parent is a throwaway
        # shell that has already exited by the time the panel is clicked.
        "pid": owner_pid or os.getppid(),
        "hwnd": hwnd,
        "detail": detail,
        "tool": tool,
        "usage": usage,
    }

    try:
        import urllib.request

        req = urllib.request.Request(
            f"{SERVER}/event",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=TIMEOUT).read()
    except Exception:
        # The traffic light is decoration - never let it interfere with Claude.
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
