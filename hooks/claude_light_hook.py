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

# Claude Code tags every Notification with its kind. Only some of them mean
# "Claude is blocked on you"; idle, authentication, quota and agent-finished
# notifications say nothing about what the session is doing, and repainting the
# lamp for those is what left sessions sitting amber with nothing running.
BLOCKING_NOTIFICATIONS = frozenset((
    "permission_prompt",
    "elicitation_dialog",
    "elicitation_url_dialog",
    "agent_needs_input",
))

# Older builds send no notification_type, so the message is all there is. Both
# tests are anchored on purpose: a bare "idle" also matches the tool call
# summary in "Bash wants to run: npm run idle-check", and reading a permission
# prompt as idle means never lighting up when Claude is genuinely waiting.
# The documented types that plainly do not want anything from you. Anything
# outside both sets is decided by the message.
IDLE_NOTIFICATIONS = frozenset((
    "idle_prompt",
    "auth_success",
    "agent_completed",
    "elicitation_complete",
    "elicitation_response",
    "quota_auto_resume_fired",
    "quota_auto_resume_stale",
    "quota_auto_resume_disabled",
    "quota_auto_resume_scheduled",
    "quota_auto_resume_started",
    "quota_auto_resume_failed",
))

BLOCKING_HINTS = ("needs your permission", "permission to use",
                  "wants to run", "wants to use")
IDLE_HINTS = ("is idle", "waiting for your input", "waiting for input")

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


def is_idle_notification(payload: dict) -> bool:
    """True when a Notification says nothing about the session being blocked.

    The server leaves the lamp exactly as it was for these, so a finished
    session stays green instead of turning amber a minute later. Claude Code
    fires Notification for twelve different things and only four of them are
    "waiting on you"; `notification_type` names which, so prefer it over
    reading the human-readable message.
    """
    if not isinstance(payload, dict):
        return False
    kind = str(payload.get("notification_type") or "").strip().lower()
    if kind in BLOCKING_NOTIFICATIONS:
        return False
    low = str(payload.get("message") or "").lower()
    if kind and kind in IDLE_NOTIFICATIONS:
        return True
    # An unrecognised type still gets read: failing to light up while Claude is
    # actually blocked is the one failure this whole thing exists to prevent,
    # and a real permission prompt always says so in its message.
    if any(hint in low for hint in BLOCKING_HINTS):
        return False
    if kind:
        # A type we do not know, saying nothing that sounds like a request.
        # Staying quiet beats an amber lamp with nothing behind it.
        return True
    # No type at all - an older build, which always sends a message.
    return any(hint in low for hint in IDLE_HINTS)


def notification_detail(payload: dict) -> str:
    """The short human-readable summary shown on the row and in the banner."""
    msg = str(payload.get("message") or "")
    # "Claude needs your permission to use Bash" -> "needs permission: Bash"
    if msg.startswith("Claude "):
        msg = msg[len("Claude "):]
    msg = msg.replace("needs your permission to use ", "needs permission: ")
    msg = msg.replace("is waiting for your input", "waiting for input")
    # Current builds phrase it the other way round, with the whole tool call
    # after it: "Bash wants to run: rm -rf /tmp" -> "needs permission: Bash".
    match = re.match(r"(.{1,40}?) wants to (?:run|use)\b", msg)
    if match:
        msg = "needs permission: " + match.group(1)
    return msg[:120]


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
    idle = False
    if event == "Notification":
        # Claude Code fires a Notification when it is blocked on you, but also
        # when it has merely been idle a minute, on a successful login, when a
        # background agent finishes... Only the first means "needs you".
        idle = is_idle_notification(payload)
        detail = notification_detail(payload)
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
        "idle": idle,
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
