"""Liveness watcher tests.

The watcher answers one question the container cannot: is the process behind
this registry entry still running? Everything here is about getting that wrong
in the safe direction - an uncertain answer must mean "alive", because dropping
the row of a session you have open is worse than carrying a dead one a few
seconds longer.

Nothing here starts a real watcher.
"""

import json
import os
import subprocess
import sys
import tempfile
import time

from harness import eq, ok

import session_watch as W


def _registry(tmp, entries):
    """A throwaway ~/.claude/sessions holding the given entries."""
    folder = os.path.join(tmp, "sessions")
    os.makedirs(folder, exist_ok=True)
    for name in os.listdir(folder):
        os.remove(os.path.join(folder, name))
    for sid, entry in entries.items():
        body = {"sessionId": sid}
        body.update(entry or {})
        path = os.path.join(folder, str(body.get("pid", sid)) + ".json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(body, fh)
    return folder


class _Registry:
    """Point the watcher at a directory of our own for the duration."""

    def __init__(self, entries):
        self.tmp = tempfile.mkdtemp(prefix="clt-watch-")
        self.folder = _registry(self.tmp, entries)

    def __enter__(self):
        self._saved = W.registry_dir
        W.registry_dir = lambda: self.folder
        return self

    def __exit__(self, *exc):
        W.registry_dir = self._saved

    def write(self, entries):
        self.folder = _registry(self.tmp, entries)


def dead_pid():
    """A pid that has certainly exited."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proc.wait()
    return proc.pid


# --- is it alive? ------------------------------------------------------------


def test_this_process_is_alive():
    ok(W.pid_alive(os.getpid()), "our own pid must read as alive")


def test_an_exited_process_is_not_alive():
    ok(not W.pid_alive(dead_pid()), "a process that has exited must read dead")


def test_a_recycled_pid_is_not_mistaken_for_the_session():
    """Windows hands pids out again quickly. Without the start-time check a
    stale entry would read as live for as long as whatever inherited its pid
    keeps running - which could be days."""
    if sys.platform != "win32":
        ok(W.pid_alive(os.getpid(), "1"), "only Windows records a start time")
        return
    ok(not W.pid_alive(os.getpid(), "1"),
       "a start time that does not match means a different process")
    ok(W.pid_alive(os.getpid(), "not a number"),
       "an unreadable start time must not condemn a live process")


def test_nonsense_pids_are_treated_as_alive():
    for value in (None, "", "abc", 0, -5, {"pid": 1}):
        ok(W.pid_alive(value), "%r must not be reported dead" % (value,))


# --- what it reports ---------------------------------------------------------


def test_only_sessions_with_a_live_process_are_reported():
    gone = dead_pid()
    with _Registry({"mine": {"pid": os.getpid()}, "killed": {"pid": gone}}) as reg:
        eq(W.Watcher().sweep(), ["mine"],
           "a registry entry whose process is gone must not be reported")
        ok(reg.folder)


def test_one_unreadable_sweep_does_not_drop_a_session():
    """Claude Code rewrites these files in place; a sweep can catch one
    mid-write. Reporting it gone would take the row away and put it back."""
    with _Registry({"mine": {"pid": os.getpid()}}) as reg:
        w = W.Watcher()
        start = time.time()
        eq(w.sweep(start), ["mine"])
        reg.write({})                       # the file blinks out
        eq(w.sweep(start + 1), ["mine"], "one missed sweep is not a death")
        eq(w.sweep(start + W.GRACE_SECONDS + 1), [],
           "still missing a sweep later: now it is gone")


def test_a_closed_session_stops_being_reported():
    with _Registry({"mine": {"pid": os.getpid()}}) as reg:
        w = W.Watcher()
        start = time.time()
        eq(w.sweep(start), ["mine"])
        reg.write({})
        eq(w.sweep(start + W.GRACE_SECONDS + 1), [])


def test_entries_without_a_readable_pid_are_still_reported():
    with _Registry({"odd": {}, "text": {"pid": "nonsense"}}):
        eq(W.Watcher().sweep(), ["odd", "text"],
           "we only remove what we can prove is gone")


# --- knowing when to stop ----------------------------------------------------


def test_it_stops_once_the_sessions_are_gone():
    w = W.Watcher()
    w.saw_any = True
    ok(w.done([]), "nothing left to watch")
    ok(not w.done(["still-here"]), "a live session keeps it running")


def test_it_waits_before_deciding_there_is_nothing_to_watch():
    """The session that started us may not have registered itself yet."""
    w = W.Watcher()
    ok(not w.done([]), "must not give up the moment it starts")
    w.started = time.time() - W.EMPTY_GRACE - 1
    ok(w.done([]), "but it does not wait for ever")


def test_it_stops_when_nothing_is_listening():
    w = W.Watcher()
    w.saw_any = True
    w.last_ok = time.time() - W.OFFLINE_LIMIT - 1
    ok(w.done(["live-session"]),
       "with the server gone there is nobody to report to")


def test_it_stops_eventually_whatever_happens():
    w = W.Watcher()
    w.started = time.time() - W.MAX_LIFETIME - 1
    ok(w.done(["live-session"]), "the backstop must fire")


# --- one copy at a time ------------------------------------------------------


def _lock_to(path):
    saved = W.LOCK
    W.LOCK = path
    return saved


def test_a_fresh_lock_held_by_a_live_process_blocks_a_second_watcher():
    tmp = tempfile.mkdtemp(prefix="clt-lock-")
    saved = _lock_to(os.path.join(tmp, "watch.lock"))
    try:
        ok(not W.lock_is_live(), "no lock at all")
        W.touch_lock()
        ok(W.lock_is_live(), "our own pid, just written")
        eq(W.ensure_running(), False, "a running watcher must not be doubled")
    finally:
        W.LOCK = saved


def test_a_lock_left_by_a_crash_does_not_block_for_ever():
    tmp = tempfile.mkdtemp(prefix="clt-lock-")
    saved = _lock_to(os.path.join(tmp, "watch.lock"))
    try:
        W.touch_lock(dead_pid())
        ok(not W.lock_is_live(), "the holder is gone, so the lock is not live")
        old = time.time() - W.LOCK_STALE - 5
        W.touch_lock()
        os.utime(W.LOCK, (old, old))
        ok(not W.lock_is_live(), "a lock nobody has touched has been abandoned")
    finally:
        W.LOCK = saved


def test_it_can_be_turned_off():
    tmp = tempfile.mkdtemp(prefix="clt-lock-")
    saved = _lock_to(os.path.join(tmp, "watch.lock"))
    os.environ["CLAUDE_LIGHT_WATCH"] = "0"
    try:
        eq(W.ensure_running(), False, "CLAUDE_LIGHT_WATCH=0 starts nothing")
        ok(not os.path.exists(W.LOCK), "and claims nothing")
    finally:
        os.environ.pop("CLAUDE_LIGHT_WATCH", None)
        W.LOCK = saved
