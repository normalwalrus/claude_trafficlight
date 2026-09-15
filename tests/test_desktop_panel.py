"""Tests for the hook-started desktop panel.

A container cannot draw a window, so the hook is what puts the panel on your
screen. That makes three things worth being strict about: it must never start
a second panel, it must never start one nobody asked for, and Quit has to mean
quit - otherwise the next Claude event would undo it.

Nothing here launches a real panel.
"""

import json
import os
import subprocess
import sys
import tempfile
import time

from harness import eq, ok

import desktop_panel as D


class _Sandbox:
    """Point every path the module writes at a throwaway directory."""

    def __enter__(self):
        self.tmp = tempfile.mkdtemp(prefix="clt-panel-")
        self.saved = (D.LOCK, D.PAYLOAD_DIR, D.PANEL_DIR, D.PANEL,
                      D.STARTED_STAMP, D.QUIT_STAMP)
        D.LOCK = os.path.join(self.tmp, "panel.lock")
        D.PAYLOAD_DIR = self.tmp
        D.PANEL_DIR = os.path.join(self.tmp, "panel")
        D.PANEL = os.path.join(D.PANEL_DIR, "panel.py")
        D.STARTED_STAMP = os.path.join(self.tmp, "panel-started.json")
        D.QUIT_STAMP = os.path.join(self.tmp, "panel-quit.json")
        return self

    def __exit__(self, *exc):
        (D.LOCK, D.PAYLOAD_DIR, D.PANEL_DIR, D.PANEL,
         D.STARTED_STAMP, D.QUIT_STAMP) = self.saved

    def stage(self):
        os.makedirs(D.PANEL_DIR, exist_ok=True)
        with open(D.PANEL, "w", encoding="utf-8") as fh:
            fh.write("# not a real panel\n")

    def stamp(self, path, at):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"at": at}, fh)


def dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proc.wait()
    return proc.pid


# --- is one already there? ---------------------------------------------------


def test_no_lock_means_no_panel():
    with _Sandbox():
        ok(not D.running(), "nothing has claimed the lock")


def test_a_fresh_lock_from_a_live_process_means_one_is_running():
    with _Sandbox():
        D.touch_lock()
        ok(D.running(), "our own pid, just written")


def test_a_lock_nobody_has_touched_is_ignored():
    """The panel heartbeats every few seconds; a stale file is a panel that
    was killed, and a killed panel should be replaced."""
    with _Sandbox():
        D.touch_lock()
        old = time.time() - D.LOCK_STALE - 5
        os.utime(D.LOCK, (old, old))
        ok(not D.running())


def test_a_lock_held_by_a_dead_process_is_ignored():
    with _Sandbox():
        D.touch_lock(dead_pid())
        ok(not D.running(), "the holder is gone")


# --- should we start one at all? ---------------------------------------------


def test_nothing_starts_when_the_panel_was_never_staged():
    """Absence is the off switch: a machine that set TRAFFICLIGHT_DESKTOP_PANEL
    to 0 has no panel files, so the hook has nothing to start."""
    with _Sandbox():
        eq(D.ensure_running(), False)
        ok(not os.path.exists(D.LOCK), "and nothing is claimed")


def test_nothing_starts_while_one_is_already_running():
    with _Sandbox() as box:
        box.stage()
        D.touch_lock()
        eq(D.ensure_running(), False, "a second panel must never be started")


def test_nothing_starts_when_it_was_quit():
    with _Sandbox() as box:
        box.stage()
        box.stamp(D.STARTED_STAMP, time.time() - 100)
        box.stamp(D.QUIT_STAMP, time.time())
        ok(D.was_quit(), "the quit is the more recent decision")
        eq(D.ensure_running(), False, "quit has to mean quit")


def test_docker_compose_up_ends_a_quit():
    """The stamp the container writes on every startup is what brings it back -
    the same command that turned the panel on in the first place."""
    with _Sandbox() as box:
        box.stage()
        box.stamp(D.QUIT_STAMP, time.time() - 100)
        box.stamp(D.STARTED_STAMP, time.time())
        ok(not D.was_quit())


def test_quitting_writes_a_stamp_and_drops_the_lock():
    with _Sandbox():
        D.touch_lock()
        ok(D.write_quit_stamp())
        D.clear_lock()
        ok(D.was_quit())
        ok(not D.running(), "and it is no longer claiming to be there")


def test_the_environment_can_turn_it_off():
    with _Sandbox() as box:
        box.stage()
        os.environ["CLAUDE_LIGHT_PANEL"] = "0"
        try:
            eq(D.ensure_running(), False)
        finally:
            os.environ.pop("CLAUDE_LIGHT_PANEL", None)


# --- how it would launch -----------------------------------------------------


def test_it_prefers_a_windowless_interpreter():
    """A console flashing up behind the panel on every session start would be
    intolerable, so pythonw is used where there is one."""
    exe = D.interpreter()
    ok(exe, "some interpreter must be chosen")
    if sys.platform == "win32" and os.path.exists(
            os.path.join(os.path.dirname(sys.executable), "pythonw.exe")):
        ok(os.path.basename(exe).lower().startswith("pythonw"),
           "expected pythonw, got %r" % exe)


def test_tkinter_is_checked_before_launching():
    eq(D.have_tk(), True, "the test runner has tkinter, so this must say so")


def test_a_started_stamp_is_only_written_when_the_container_starts():
    """`docker compose up -d` does nothing when the container is already
    running, so the panel's menu names `docker compose restart` instead. This
    is the fact that makes that label the honest one."""
    with _Sandbox() as box:
        box.stage()
        first = time.time()
        box.stamp(D.STARTED_STAMP, first)     # what the container writes
        box.stamp(D.QUIT_STAMP, first + 1)
        ok(D.was_quit(), "a quit after that start stands")
        # ...and only a later start clears it.
        box.stamp(D.STARTED_STAMP, first + 2)
        ok(not D.was_quit())
