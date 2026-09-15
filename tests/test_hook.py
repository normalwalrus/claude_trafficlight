"""Hook tests.

The hook's one hard contract is: exit 0, fast, whatever happens. Most of these
run it as a real subprocess against a throwaway HTTP server so the timing and
the exit code are the real thing.
"""

import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

from harness import Skip, eq, ok

import claude_light_hook as hook

HOOK = hook.__file__
PY = sys.executable
if PY.lower().endswith("pythonw.exe"):
    PY = PY[: -len("pythonw.exe")] + "python.exe"


class _Collector(http.server.BaseHTTPRequestHandler):
    received = []
    status = 200

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            _Collector.received.append(json.loads(self.rfile.read(n)))
        except Exception:
            _Collector.received.append(None)
        self.send_response(_Collector.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *a):
        pass


_server = {}


def collector():
    if "url" not in _server:
        httpd = http.server.HTTPServer(("127.0.0.1", 0), _Collector)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        _server["url"] = "http://127.0.0.1:%d" % httpd.server_port
    _Collector.received = []
    _Collector.status = 200
    return _server["url"]


def run_hook(argv, stdin, url=None, timeout=20, watch=False):
    env = dict(os.environ)
    env["CLAUDE_LIGHT_URL"] = url if url is not None else collector()
    # The hook starts the liveness watcher, which outlives it on purpose. A
    # test run must not leave background processes behind, so it is off unless
    # the test is specifically about that.
    env["CLAUDE_LIGHT_WATCH"] = "1" if watch else "0"
    t0 = time.time()
    proc = subprocess.run(
        [PY, HOOK] + list(argv), input=stdin, capture_output=True,
        env=env, timeout=timeout,
    )
    return proc.returncode, time.time() - t0, proc.stderr


def payload(**kw):
    kw.setdefault("session_id", "test-session")
    kw.setdefault("cwd", os.path.join("C:", os.sep, "work", "my-project"))
    return json.dumps(kw).encode("utf-8")


# --- the never-break contract ----------------------------------------------


def test_always_exits_zero_on_hostile_stdin():
    cases = {
        "valid": payload(hook_event_name="Stop"),
        "empty": b"",
        "blank": b"   \n  ",
        "not json": b"not json at all",
        "json list": b"[]",
        "json string": b'"hello"',
        "json null": b"null",
        "truncated": b'{"session_id": "a"',
        "wrong types": json.dumps(
            {"session_id": {"a": 1}, "hook_event_name": 12, "cwd": [1, 2]}
        ).encode(),
        "unicode": payload(
            hook_event_name="Notification",
            session_id="\u00e9\U0001f6a6",
            message="Claude needs your permission to use Bash",
        ),
        "huge": payload(hook_event_name="Stop", cwd="C:/" + "z" * 20000),
        "nul bytes": payload(hook_event_name="Stop", session_id="a\u0000b"),
    }
    for name, data in cases.items():
        rc, dt, err = run_hook(["Stop"], data)
        eq(rc, 0, "%s exit code (stderr=%r)" % (name, err[:200]))
        ok(dt < 5, "%s took %.1fs" % (name, dt))


def test_exits_zero_with_no_argv_and_no_stdin():
    rc, _, err = run_hook([], b"")
    eq(rc, 0, repr(err[:200]))


def test_exits_zero_when_the_server_is_down():
    rc, dt, err = run_hook(["Stop"], payload(hook_event_name="Stop"),
                           url="http://127.0.0.1:9")
    eq(rc, 0, repr(err[:200]))
    ok(dt < 5, "connection refused should be fast, took %.1fs" % dt)


def test_exits_zero_when_the_server_returns_500():
    collector()
    _Collector.status = 500
    rc, dt, err = run_hook(["Stop"], payload(hook_event_name="Stop"))
    eq(rc, 0, repr(err[:200]))
    _Collector.status = 200


def test_a_hanging_server_is_bounded_by_the_timeout():
    """Point the hook at a socket that accepts and then never answers. It must
    give up after TIMEOUT rather than stalling the Claude session."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    port = listener.getsockname()[1]
    held = []

    def accept_forever():
        while True:
            try:
                held.append(listener.accept()[0])
            except Exception:
                return

    threading.Thread(target=accept_forever, daemon=True).start()
    try:
        rc, dt, err = run_hook(
            ["Stop"], payload(hook_event_name="Stop"),
            url="http://127.0.0.1:%d" % port, timeout=30,
        )
    finally:
        listener.close()
    eq(rc, 0, repr(err[:200]))
    # TIMEOUT + interpreter startup; anything near 5s means the bound is gone
    ok(dt < hook.TIMEOUT + 2.0, "hook took %.2fs, TIMEOUT is %.1fs" % (dt, hook.TIMEOUT))


# --- payload shaping --------------------------------------------------------


def test_posts_the_expected_body():
    collector()
    run_hook(["Stop"], payload(hook_event_name="Stop", session_id="body-1"))
    ok(_Collector.received, "nothing posted")
    body = _Collector.received[-1]
    eq(body["session_id"], "body-1")
    eq(body["event"], "Stop")
    eq(body["project"], "my-project")
    ok(isinstance(body["pid"], int) and body["pid"] > 0, "pid")
    ok(isinstance(body["hwnd"], int), "hwnd")


def test_hook_event_name_wins_over_argv():
    collector()
    run_hook(["Stop"], payload(hook_event_name="Notification", message="x"))
    eq(_Collector.received[-1]["event"], "Notification")


def test_argv_is_used_when_the_payload_has_no_event():
    collector()
    run_hook(["PreToolUse"], payload(tool_name="Bash"))
    eq(_Collector.received[-1]["event"], "PreToolUse")
    eq(_Collector.received[-1]["detail"], "Bash")


def test_notification_messages_are_rewritten_short():
    collector()
    run_hook(["Notification"], payload(
        hook_event_name="Notification",
        message="Claude needs your permission to use Bash"))
    eq(_Collector.received[-1]["detail"], "needs permission: Bash")

    run_hook(["Notification"], payload(
        hook_event_name="Notification", message="Claude is waiting for your input"))
    eq(_Collector.received[-1]["detail"], "waiting for input")

    run_hook(["Notification"], payload(
        hook_event_name="Notification", message="q" * 500))
    eq(len(_Collector.received[-1]["detail"]), 120, "detail must be truncated")


def test_notification_type_decides_whether_claude_is_blocked_on_you():
    """Claude Code fires Notification for twelve different things and only four
    of them mean "waiting on you". Treating the rest as amber is what left the
    panel orange with nothing running - so the kind, not the prose, decides."""
    blocked = ["permission_prompt", "elicitation_dialog",
               "elicitation_url_dialog", "agent_needs_input"]
    passive = ["idle_prompt", "auth_success", "agent_completed",
               "elicitation_complete", "elicitation_response",
               "quota_auto_resume_fired", "quota_auto_resume_stale",
               "quota_auto_resume_disabled"]
    for kind in blocked:
        ok(not hook.is_idle_notification({"notification_type": kind}),
           "%s means Claude is blocked on you" % kind)
    for kind in passive:
        ok(hook.is_idle_notification({"notification_type": kind}),
           "%s says nothing about the session; the lamp must not move" % kind)
    # A type we have never heard of, with nothing else to go on, is not a
    # reason to claim you are needed. (If its message looks like a permission
    # prompt the message wins - see the unrecognised-type test below.)
    ok(hook.is_idle_notification({"notification_type": "something_new"}))
    # ...and the type wins over whatever the message happens to say.
    ok(not hook.is_idle_notification(
        {"notification_type": "permission_prompt",
         "message": "Claude Code is idle. Idle message: x"}))


def test_the_message_fallback_never_mistakes_a_permission_prompt_for_idle():
    """Older builds send no notification_type. A bare "idle" substring also
    matches the tool call summary in "Bash wants to run: npm run idle-check",
    and reading that as idle means never lighting up when Claude really is
    waiting on you - the worse of the two failures."""
    for msg in [
        "Claude needs your permission to use Bash",
        "Claude needs your permission to use the Write tool",
        "Bash wants to run: rm -rf /tmp",                  # current phrasing
        "Bash wants to run: npm run idle-check",           # 'idle' in the command
        "Read wants to run: src/idle.ts",
        "Edit wants to use middleware/idle.js",
    ]:
        ok(not hook.is_idle_notification({"message": msg}),
           "must stay blocking: %r" % msg)
    for msg in [
        "Claude Code is idle. Idle message: still there?",
        "Claude is waiting for your input",
        "Claude Code is waiting for input",
    ]:
        ok(hook.is_idle_notification({"message": msg}),
           "must be treated as idle: %r" % msg)
    # Nothing to go on at all: stay amber rather than silently swallow a prompt.
    ok(not hook.is_idle_notification({}))
    ok(not hook.is_idle_notification({"message": ""}))
    ok(not hook.is_idle_notification("not a dict"))


def test_the_idle_flag_reaches_the_server():
    collector()
    run_hook(["Notification"], payload(
        hook_event_name="Notification", notification_type="idle_prompt",
        message="Claude Code is idle. Idle message: still there?"))
    eq(_Collector.received[-1]["idle"], True, "idle notification")

    run_hook(["Notification"], payload(
        hook_event_name="Notification", notification_type="permission_prompt",
        message="Bash wants to run: npm run idle-check"))
    eq(_Collector.received[-1]["idle"], False, "permission prompt")

    run_hook(["Stop"], payload(hook_event_name="Stop"))
    eq(_Collector.received[-1]["idle"], False, "non-notification events")


def test_the_current_permission_wording_is_shortened_too():
    """The row has space for the tool name, not the whole command line."""
    eq(hook.notification_detail({"message": "Bash wants to run: rm -rf /tmp"}),
       "needs permission: Bash")
    eq(hook.notification_detail({"message": "Edit wants to use foo.py"}),
       "needs permission: Edit")
    eq(hook.notification_detail(
        {"message": "Claude needs your permission to use Bash"}),
       "needs permission: Bash")
    eq(hook.notification_detail({"message": "Claude is waiting for your input"}),
       "waiting for input")
    # A sentence that merely contains the words is left alone, not mangled.
    eq(hook.notification_detail({"message": "Authentication successful for you"}),
       "Authentication successful for you")
    eq(len(hook.notification_detail({"message": "q" * 500})), 120)


def test_pre_tool_use_detail_is_bounded():
    collector()
    run_hook(["PreToolUse"], payload(hook_event_name="PreToolUse", tool_name="T" * 300))
    eq(len(_Collector.received[-1]["detail"]), 40)


def test_missing_session_id_falls_back_to_a_pid_key():
    collector()
    run_hook(["Stop"], payload(hook_event_name="Stop", session_id=None))
    ok(_Collector.received[-1]["session_id"].startswith("pid-"))


# --- the hwnd/pid cache -----------------------------------------------------


def test_cache_key_is_a_safe_filename():
    """Session ids are untrusted: '/' and '..' would escape the cache dir and
    ':' would create an NTFS alternate data stream, both of which silently
    break the cache so every hook re-enumerates every window."""
    bad = ["a/b", "c:d", "e*f", "../escape", 'q"w', "<>|", "x?y", "\\\\unc\\share"]
    for sid in bad:
        key = hook.cache_key(sid)
        for ch in '/\\:*?"<>|':
            ok(ch not in key, "%r leaked %r into %r" % (sid, ch, key))
        ok(key.strip(".") == key.lstrip("."), "%r produced %r" % (sid, key))
        path = os.path.join(hook.CACHE_DIR, "hwnd-%s.txt" % key)
        ok(os.path.normpath(path) == os.path.join(
            os.path.normpath(hook.CACHE_DIR), "hwnd-%s.txt" % key),
           "%r escaped the cache dir: %r" % (sid, path))
    eq(len(set(hook.cache_key(s) for s in bad)), len(bad), "keys must be distinct")
    eq(hook.cache_key("a/b"), hook.cache_key("a/b"), "must be stable")


def test_cache_round_trip_and_reuse():
    sid = "cache-roundtrip"
    path = os.path.join(hook.CACHE_DIR, "hwnd-%s.txt" % hook.cache_key(sid))
    if os.path.exists(path):
        os.remove(path)
    original = hook.find_terminal
    hook.find_terminal = lambda: (1234, 5678)
    try:
        eq(hook.cached_terminal(sid, "SessionStart"), (1234, 5678))
        # second call must come from the file, not from a fresh walk
        hook.find_terminal = lambda: (9999, 9999)
        eq(hook.cached_terminal(sid, "Stop"), (1234, 5678))
        eq(hook.cached_terminal(sid, "SessionEnd"), (0, 0))
        ok(not os.path.exists(path), "SessionEnd must delete the cache file")
    finally:
        hook.find_terminal = original


def test_a_failed_resolution_is_not_cached():
    """SessionStart fires before the terminal window necessarily exists.
    Caching the 0 would mean the session never gets a window handle at all."""
    sid = "cache-zero"
    path = os.path.join(hook.CACHE_DIR, "hwnd-%s.txt" % hook.cache_key(sid))
    if os.path.exists(path):
        os.remove(path)
    original = hook.find_terminal
    hook.find_terminal = lambda: (0, 0)
    try:
        eq(hook.cached_terminal(sid, "SessionStart"), (0, 0))
        ok(not os.path.exists(path), "a failed lookup must not be cached")
        hook.find_terminal = lambda: (77, 88)
        eq(hook.cached_terminal(sid, "Stop"), (77, 88), "must retry later")
    finally:
        hook.find_terminal = original
        if os.path.exists(path):
            os.remove(path)


def test_corrupt_cache_files_self_heal():
    sid = "cache-corrupt"
    path = os.path.join(hook.CACHE_DIR, "hwnd-%s.txt" % hook.cache_key(sid))
    os.makedirs(hook.CACHE_DIR, exist_ok=True)
    original = hook.find_terminal
    hook.find_terminal = lambda: (555, 666)
    try:
        for junk in ["", "not-a-number", "\x00\x01\x02", "0", ",", "1,2,3",
                     "-4,-5", "99999999999999999999,5", "   "]:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(junk)
            eq(hook.cached_terminal(sid, "Stop"), (555, 666), "junk=%r" % junk)
    finally:
        hook.find_terminal = original
        if os.path.exists(path):
            os.remove(path)


def test_an_unwritable_cache_dir_does_not_raise():
    blocker = os.path.join(tempfile.gettempdir(), "clt-cache-blocker")
    shutil.rmtree(blocker, ignore_errors=True)
    with open(blocker, "w") as fh:  # a file where a directory is expected
        fh.write("not a directory")
    saved_dir, saved_find = hook.CACHE_DIR, hook.find_terminal
    hook.CACHE_DIR = blocker
    hook.find_terminal = lambda: (11, 22)
    try:
        eq(hook.cached_terminal("x", "Stop"), (11, 22))
        eq(hook.cached_terminal("x", "SessionEnd"), (0, 0))
    finally:
        hook.CACHE_DIR, hook.find_terminal = saved_dir, saved_find
        os.remove(blocker)


def test_reported_pid_owns_a_window_and_outlives_the_hook():
    """os.getppid() inside a hook is the throwaway shell Claude spawned for
    this invocation; it is dead before the panel can ever click it. The pid we
    report has to be the window owner, which is still around."""
    if sys.platform != "win32":
        raise Skip("windows only")
    import winutil

    hwnd, pid = hook.find_terminal()
    if not hwnd:
        raise Skip("no windowed ancestor in this environment")
    procs = winutil.process_parents()
    ok(pid in procs, "reported pid %s is not a live process" % pid)
    eq(winutil.windows_by_pid().get(pid), hwnd, "reported pid must own the hwnd")


def test_find_terminal_never_returns_the_desktop():
    if sys.platform != "win32":
        raise Skip("windows only")
    import winutil

    hwnd, pid = hook.find_terminal()
    if not hwnd:
        raise Skip("no windowed ancestor in this environment")
    name = winutil.process_parents().get(pid, (0, ""))[1]
    ok(name not in hook.STOP_AT, "resolved to a shell/desktop process: %r" % name)


def test_an_unrecognised_notification_type_is_decided_by_the_message():
    """Failing to light up while Claude is genuinely blocked is the one failure
    this exists to prevent, so an unknown type must not be assumed harmless."""
    import claude_light_hook as H

    blocking = {"notification_type": "some_future_prompt",
                "message": "Bash wants to run: npm test"}
    ok(not H.is_idle_notification(blocking),
       "an unknown type with a permission message must still light amber")

    quiet = {"notification_type": "some_future_thing",
             "message": "Claude is idle"}
    ok(H.is_idle_notification(quiet),
       "an unknown type with an idle message stays quiet")

    known_block = {"notification_type": "permission_prompt", "message": "anything"}
    ok(not H.is_idle_notification(known_block), "documented blocking type")

    known_quiet = {"notification_type": "auth_success",
                   "message": "Bash wants to run: x"}
    ok(H.is_idle_notification(known_quiet),
       "a documented quiet type wins over a misleading message")

    ok(not H.is_idle_notification({"message": "Edit wants to use the file"}),
       "no type at all: the message decides")
    ok(H.is_idle_notification({"message": "Claude is waiting for your input"}),
       "no type at all: idle message")
