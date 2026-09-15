"""winutil tests - Windows only, and deliberately non-destructive.

Nothing here focuses or closes a window belonging to someone else; the focus
path is exercised only through arguments that cannot match a real window.
"""

import os
import sys

from harness import Skip, eq, ok

import winutil


def win_only():
    if sys.platform != "win32":
        raise Skip("windows only")


def test_process_parents_finds_this_process():
    win_only()
    procs = winutil.process_parents()
    ok(len(procs) > 10, "only %d processes enumerated" % len(procs))
    ok(os.getpid() in procs, "our own pid is missing")
    parent, name = procs[os.getpid()]
    ok(name.endswith(".exe"), "bad exe name %r" % name)
    eq(parent, os.getppid(), "parent pid mismatch")


def test_windows_by_pid_returns_plausible_handles():
    win_only()
    found = winutil.windows_by_pid()
    ok(found, "no visible windows found at all")
    for pid, hwnd in found.items():
        ok(pid > 0 and hwnd > 0, "bad entry %r -> %r" % (pid, hwnd))
        ok(winutil.window_alive(hwnd), "enumerated a dead window")


def test_resolve_window_rejects_junk_pids():
    win_only()
    for pid in (0, None, -1, "abc", 999999, 2 ** 40):
        eq(winutil.resolve_window(pid), 0, "pid=%r" % pid)


def test_resolve_window_finds_the_window_hosting_us():
    win_only()
    hwnd = winutil.resolve_window(os.getpid())
    if not hwnd:
        raise Skip("this process has no windowed ancestor (headless runner)")
    ok(winutil.window_alive(hwnd), "resolved a dead window")
    ok(winutil.window_title(hwnd), "resolved a window with no title")


def test_resolve_window_never_returns_the_desktop():
    """Walking all the way up lands on explorer.exe, whose window is the
    desktop - focusing that would minimise everything the user has open."""
    win_only()
    procs = winutil.process_parents()
    shells = [pid for pid, (_p, name) in procs.items() if name in winutil.STOP_AT]
    ok(shells, "no shell/system processes found to test against")
    for pid in shells:
        eq(winutil.resolve_window(pid), 0, "pid %d (%s)" % (pid, procs[pid][1]))
    # ...and children of those shells that own no window of their own
    orphans = [pid for pid, (par, _n) in procs.items()
               if par in shells and pid not in winutil.windows_by_pid()]
    for pid in orphans[:10]:
        eq(winutil.resolve_window(pid), 0, "pid %d (%s)" % (pid, procs[pid][1]))


def test_window_helpers_tolerate_bad_handles():
    win_only()
    for hwnd in (0, None, -1, 1, 2 ** 40, 99999999):
        eq(winutil.window_alive(hwnd), False, "alive(%r)" % hwnd)
        eq(winutil.window_title(hwnd), "", "title(%r)" % hwnd)
        eq(winutil.focus_window(hwnd), False, "focus(%r)" % hwnd)


def test_focus_session_returns_a_sane_tuple_in_every_branch():
    win_only()
    for args in [(0, 0), (None, None), (-5, -5), (999999, 0), (0, 99999999),
                 ("x", "y"), (999999, 2 ** 40)]:
        result = winutil.focus_session(*args)
        ok(isinstance(result, tuple) and len(result) == 2, "bad shape %r" % (result,))
        okflag, hwnd = result
        ok(isinstance(okflag, bool), "ok flag is %r" % type(okflag))
        ok(isinstance(hwnd, int), "hwnd is %r" % type(hwnd))
        eq(result, (False, 0), "args=%r" % (args,))


def test_non_windows_helpers_are_inert():
    if sys.platform == "win32":
        raise Skip("this asserts the posix no-op path")
    eq(winutil.process_parents(), {})
    eq(winutil.resolve_window(1), 0)
    eq(winutil.focus_session(1, 1), (False, 0))
