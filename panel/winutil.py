"""Windows helpers for the panel (ctypes only, no third-party deps).

The panel resolves a session's window *at click time* from the live Claude
process id, rather than trusting the handle captured when the session started:
handles go stale when VS Code or a terminal is restarted, and the window may
not even exist yet when SessionStart fires.
"""

import sys

# Walking up past the shell would land on the desktop itself.
STOP_AT = {
    "explorer.exe",
    "services.exe",
    "wininit.exe",
    "winlogon.exe",
    "svchost.exe",
    "system",
    "",
}

_MAX_DEPTH = 12


def _win32():
    import ctypes
    from ctypes import wintypes

    return ctypes, wintypes


def process_parents():
    """Return {pid: (parent_pid, exe_name_lower)} for every running process."""
    if sys.platform != "win32":
        return {}
    try:
        ctypes, wintypes = _win32()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

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
            return {}
        out = {}
        try:
            entry = PROCESSENTRY32()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
            ok = kernel32.Process32First(snap, ctypes.byref(entry))
            while ok:
                name = entry.szExeFile.decode("mbcs", "replace").lower()
                out[int(entry.th32ProcessID)] = (int(entry.th32ParentProcessID), name)
                ok = kernel32.Process32Next(snap, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snap)
        return out
    except Exception:
        return {}


def windows_by_pid_all():
    """Return {pid: [(hwnd, title), ...]} for visible, titled, top-level windows.

    Every window is kept, not just the first. Electron apps run one process per
    *application*, not per window, so VS Code's pid owns every editor window
    open on the machine - keeping only the first would pick whichever happens
    to be topmost.
    """
    if sys.platform != "win32":
        return {}
    try:
        ctypes, wintypes = _win32()
        user32 = ctypes.WinDLL("user32", use_last_error=True)

        found = {}
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def cb(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True
            if user32.GetWindow(hwnd, 4):  # GW_OWNER - skip tool/dialog windows
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            found.setdefault(int(pid.value), []).append((int(hwnd), buf.value))
            return True

        user32.EnumWindows(WNDENUMPROC(cb), 0)
        return found
    except Exception:
        return {}


def windows_by_pid():
    """Return {pid: hwnd}, keeping the first window found per process."""
    return {pid: ws[0][0] for pid, ws in windows_by_pid_all().items() if ws}


def cwd_terms(cwd):
    """Path segments of a session's cwd, most specific first.

    An editor window is titled after the folder it has open, so the deepest
    segments identify it best. Very short and very generic segments are
    dropped - matching on "src" would hit half the windows on screen.
    """
    if not cwd:
        return []
    parts = [p for p in str(cwd).replace("\\", "/").split("/") if p]
    generic = {"src", "app", "lib", "code", "repo", "repos", "projects",
               "documents", "desktop", "downloads", "users", "home", "dev",
               "work", "temp", "tmp", "c:", "d:"}
    terms = []
    for part in reversed(parts):
        low = part.lower().strip()
        if len(low) < 3 or low in generic:
            continue
        terms.append(low)
    return terms[:4]


def score_window(title, terms):
    """How well a window title identifies a session's folder.

    Earlier (deeper) terms score higher, so a window showing the project itself
    beats one merely showing its parent directory.
    """
    low = (title or "").lower()
    for i, term in enumerate(terms):
        if term in low:
            return len(terms) - i
    return 0


def pick_window(candidates, cwd):
    """Choose the window matching `cwd` from one process's windows.

    Returns (hwnd, confident). `confident` is False when the choice was a
    fallback rather than a real title match - the caller can then prefer a
    handle captured earlier instead.
    """
    if not candidates:
        return 0, False
    if len(candidates) == 1:
        return candidates[0][0], True

    terms = cwd_terms(cwd)
    if terms:
        best, best_score = None, 0
        for hwnd, title in candidates:
            score = score_window(title, terms)
            if score > best_score:
                best, best_score = hwnd, score
        if best:
            return best, True
    return candidates[0][0], False


def resolve_window(pid, cwd=""):
    """Find the window hosting `pid`, walking up its ancestry.

    `cwd` disambiguates when the owning process has several windows - which is
    the normal case under VS Code, where one Code.exe owns every editor window.
    Returns the hwnd, or 0.
    """
    return resolve_window_ex(pid, cwd)[0]


def resolve_window_ex(pid, cwd=""):
    """As resolve_window, but returns (hwnd, confident)."""
    if sys.platform != "win32" or not pid:
        return 0, False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return 0, False
    parents = process_parents()
    if pid not in parents:
        return 0, False  # process is gone
    # A recycled pid could land on explorer.exe itself; raising its window
    # would mean clicking a Claude row pops open a File Explorer.
    if parents[pid][1] in STOP_AT:
        return 0, False
    windows = windows_by_pid_all()

    current = pid
    for _ in range(_MAX_DEPTH):
        if current in windows:
            return pick_window(windows[current], cwd)
        nxt = parents.get(current, (0, ""))[0]
        if not nxt or nxt == current:
            return 0, False
        if parents.get(nxt, (0, ""))[1] in STOP_AT:
            return 0, False
        current = nxt
    return 0, False


def window_alive(hwnd):
    if sys.platform != "win32" or not hwnd:
        return False
    try:
        ctypes, _ = _win32()
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        return bool(user32.IsWindow(hwnd)) and bool(user32.IsWindowVisible(hwnd))
    except Exception:
        return False


def window_title(hwnd):
    if sys.platform != "win32" or not hwnd:
        return ""
    try:
        ctypes, wintypes = _win32()
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value
    except Exception:
        return ""


def foreground_window():
    """The window the user is currently looking at, or 0."""
    if sys.platform != "win32":
        return 0
    try:
        ctypes, _ = _win32()
        return int(ctypes.WinDLL("user32", use_last_error=True).GetForegroundWindow())
    except Exception:
        return 0


def idle_seconds():
    """Seconds since the last keyboard or mouse input anywhere, or None.

    System-wide on purpose: the question is whether you are at the machine at
    all, not whether you have touched this panel. Typing in your editor counts
    as being here; a session that finishes while this is large finished while
    you were away from the desk.
    """
    if sys.platform != "win32":
        return None
    try:
        ctypes, wintypes = _win32()

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        kernel32.GetTickCount64.restype = ctypes.c_ulonglong
        ticks = int(kernel32.GetTickCount64())
        # Both are milliseconds since boot. GetLastInputInfo is a 32-bit
        # counter that wraps every 49 days, so a negative answer means it has
        # wrapped and the honest reply is "no idea" rather than a huge number.
        idle = (ticks - int(info.dwTime)) / 1000.0
        return idle if idle >= 0 else None
    except Exception:
        return None


def window_pid(hwnd):
    """The pid owning a window, or 0."""
    if sys.platform != "win32" or not hwnd:
        return 0
    try:
        ctypes, wintypes = _win32()
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def focus_window(hwnd):
    """Bring a window to the foreground.

    Windows refuses SetForegroundWindow from a process that does not own the
    foreground, so we temporarily attach to the foreground thread's input
    state - the standard workaround.
    """
    if sys.platform != "win32" or not hwnd:
        return False
    try:
        ctypes, _ = _win32()
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        SW_RESTORE = 9

        if not user32.IsWindow(hwnd):
            return False
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)

        fg = user32.GetForegroundWindow()
        if fg == hwnd:
            return True

        target_thread = user32.GetWindowThreadProcessId(hwnd, None)
        current_thread = kernel32.GetCurrentThreadId()
        fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0

        attached = []
        for other in set([fg_thread, target_thread]):
            if other and other != current_thread:
                if user32.AttachThreadInput(current_thread, other, True):
                    attached.append(other)
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            for other in attached:
                user32.AttachThreadInput(current_thread, other, False)

        return int(user32.GetForegroundWindow()) == int(hwnd)
    except Exception:
        return False


def focus_session(pid, hwnd_hint=0, cwd=""):
    """Focus the window for a Claude session. Returns (ok, hwnd_used).

    Matching the session's folder against window titles comes first, because
    it is the only signal that actually identifies the right window when one
    process owns several. Under VS Code both of the alternatives are close to
    random: Code.exe owns every editor window, so resolving by pid alone
    returns whichever is topmost, and the handle the hook captured at
    SessionStart is whichever was topmost *then* - frequently a different
    project entirely.

    The captured handle is the fallback, used when no title matched (a plain
    terminal, an unusual title) but the handle is still live and still owned by
    the process we recorded.
    """
    hwnd, confident = resolve_window_ex(pid, cwd)
    if hwnd and confident and focus_window(hwnd):
        return True, hwnd

    if hwnd_hint and window_alive(hwnd_hint):
        # A recycled handle now owned by something else must not be focused.
        if not pid or window_pid(hwnd_hint) == as_pid(pid):
            if focus_window(hwnd_hint):
                return True, hwnd_hint

    if hwnd and focus_window(hwnd):
        return True, hwnd
    # The window may have been raised even if the foreground check failed.
    for candidate in (hwnd, hwnd_hint):
        if candidate and window_alive(candidate):
            return False, candidate
    return False, 0


def as_pid(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
