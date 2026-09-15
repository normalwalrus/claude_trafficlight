"""Server tests.

These need FastAPI/pydantic, which only exist inside the container, so they
skip cleanly on a bare host. They drive `store` directly plus one real uvicorn
instance on an ephemeral port - never the live server on 8787.
"""

import json
import socket
import threading
import time
import urllib.error
import urllib.request

from harness import Skip, eq, ok

try:
    import app as srv
except Exception as exc:  # pragma: no cover - depends on the host
    srv = None
    _why = "needs fastapi/pydantic: %s" % exc


def _srv():
    if srv is None:
        raise Skip(_why)
    return srv


def _ev(**kw):
    kw.setdefault("event", "Stop")
    kw.setdefault("session_id", "s")
    return _srv().Event(**kw)


def fresh():
    s = _srv().Store()
    return s


# --- state machine ----------------------------------------------------------


def test_event_to_light_mapping():
    st = fresh()
    for event, want in [
        ("SessionStart", "green"),
        ("UserPromptSubmit", "red"),
        ("PreToolUse", "red"),
        ("PostToolUse", "red"),
        ("SubagentStop", "red"),
        ("Notification", "orange"),
        ("Stop", "green"),
    ]:
        st.apply(_ev(session_id="a", event=event))
        eq(st.sessions["a"]["state"], want, event)


def test_session_end_removes_the_row():
    st = fresh()
    st.apply(_ev(session_id="a", event="SessionStart"))
    st.apply(_ev(session_id="a", event="SessionEnd"))
    eq(st.sessions, {})


def test_session_end_for_an_unknown_session_is_harmless():
    st = fresh()
    st.apply(_ev(session_id="ghost", event="SessionEnd"))
    eq(st.sessions, {})


def test_unknown_event_names_fall_back_to_red():
    st = fresh()
    st.apply(_ev(session_id="a", event="SomethingNew"))
    eq(st.sessions["a"]["state"], "red")


def test_out_of_order_events_create_the_session():
    st = fresh()
    st.apply(_ev(session_id="a", event="Stop"))  # Stop before SessionStart
    eq(st.sessions["a"]["state"], "green")
    st.apply(_ev(session_id="a", event="SessionEnd"))
    st.apply(_ev(session_id="a", event="PreToolUse"))  # event after removal
    eq(st.sessions["a"]["state"], "red")


def test_since_only_moves_when_the_state_changes():
    st = fresh()
    st.apply(_ev(session_id="a", event="UserPromptSubmit"))
    since = st.sessions["a"]["since"]
    updated = st.sessions["a"]["updated"]
    for i in range(5):
        time.sleep(0.01)
        st.apply(_ev(session_id="a", event="PreToolUse", detail="tool%d" % i))
    eq(st.sessions["a"]["since"], since, "since must not move (red -> red)")
    ok(st.sessions["a"]["updated"] > updated, "updated must advance")
    st.apply(_ev(session_id="a", event="Stop"))
    ok(st.sessions["a"]["since"] > since, "since must move on red -> green")


def test_late_identity_details_win_over_placeholders():
    st = fresh()
    st.apply(_ev(session_id="a", event="SessionStart"))
    eq(st.sessions["a"]["project"], "claude")
    st.apply(_ev(session_id="a", event="Stop", project="real", pid=42, hwnd=7))
    eq(st.sessions["a"]["project"], "real")
    eq(st.sessions["a"]["pid"], 42)
    # ...and a later event without them does not wipe them out
    st.apply(_ev(session_id="a", event="Stop"))
    eq(st.sessions["a"]["project"], "real")
    eq(st.sessions["a"]["hwnd"], 7)


def test_snapshot_is_ordered_by_start_time():
    st = fresh()
    for sid in ("c", "a", "b"):
        st.apply(_ev(session_id=sid, event="SessionStart"))
        time.sleep(0.005)
    eq([s["session_id"] for s in st.snapshot()["sessions"]], ["c", "a", "b"])


def test_reaper_drops_stale_sessions_and_publishes():
    s = _srv()
    st = fresh()
    st.apply(_ev(session_id="old", event="Stop"))
    st.apply(_ev(session_id="new", event="Stop"))
    st.sessions["old"]["updated"] = time.time() - (s.STALE_SECONDS + 60)
    before = st.revision
    st.reap()
    eq(sorted(st.sessions), ["new"])
    ok(st.revision > before, "reap must publish a new snapshot")
    # nothing stale -> no spurious publish
    rev = st.revision
    st.reap()
    eq(st.revision, rev)


# --- fan-out ----------------------------------------------------------------


def test_publish_keeps_the_newest_snapshot_when_a_subscriber_backs_up():
    """A queue that has filled up must drop its OLDEST entry, not the update
    being published: every message is a full snapshot, so the newest one is
    the only one that matters. Dropping the new one instead leaves a laggy
    panel showing a state the server has already moved past."""
    import asyncio

    s = _srv()
    st = fresh()
    q = asyncio.Queue(maxsize=4)
    st.subscribers.add(q)
    for i in range(20):
        st.apply(_ev(session_id="a", event="PreToolUse" if i % 2 else "Stop"))
    eq(q.qsize(), 4)
    last = None
    while not q.empty():
        last = json.loads(q.get_nowait())
    eq(last["revision"], st.revision, "queue must end on the current revision")


def test_every_subscriber_gets_every_update():
    import asyncio

    st = fresh()
    qs = [asyncio.Queue(maxsize=64) for _ in range(5)]
    for q in qs:
        st.subscribers.add(q)
    for i in range(20):
        st.apply(_ev(session_id="a%d" % i, event="SessionStart"))
    for q in qs:
        eq(q.qsize(), 20)


# --- HTTP surface (one throwaway uvicorn on an ephemeral port) ---------------

_live = {}


def _live_server():
    if srv is None:
        raise Skip(_why)
    if "base" in _live:
        return _live["base"]
    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    cfg = uvicorn.Config(srv.app, host="127.0.0.1", port=port, log_level="error")
    threading.Thread(target=uvicorn.Server(cfg).run, daemon=True).start()
    base = "http://127.0.0.1:%d" % port
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/healthz", timeout=1).read()
            break
        except Exception:
            time.sleep(0.1)
    else:
        raise Skip("could not start a test server")
    _live["base"] = base
    return base


def _post(base, body, raw=None):
    data = raw if raw is not None else json.dumps(body).encode()
    req = urllib.request.Request(
        base + "/event", data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, None


def test_http_rejects_malformed_bodies_without_dying():
    base = _live_server()
    for raw in [b"not json", b"[]", b"", b"null", b'{"session_id": 1}',
                b'{"event": "Stop"}', b'{"session_id":"a","event":"Stop","pid":"x"}']:
        code, _ = _post(base, None, raw=raw)
        ok(code == 422, "want 422 for %r, got %s" % (raw[:20], code))
    # still alive and serving
    code, body = _post(base, {"session_id": "after", "event": "Stop"})
    eq(code, 200)
    urllib.request.urlopen(
        urllib.request.Request(base + "/sessions/after", method="DELETE"), timeout=5
    ).read()


def test_http_accepts_hostile_but_well_typed_payloads():
    base = _live_server()
    for body in [
        {"session_id": "nul\x00id", "event": "Stop"},
        {"session_id": "emoji", "event": "Stop", "project": "\U0001f6a6 日本語"},
        {"session_id": "../../x", "event": "Stop"},
        {"session_id": "", "event": "Stop"},
        {"session_id": "big", "event": "Stop", "project": "A" * 50000},
    ]:
        code, _ = _post(base, body)
        eq(code, 200, repr(body.get("session_id"))[:30])
    urllib.request.urlopen(
        urllib.request.Request(base + "/sessions", method="DELETE"), timeout=5
    ).read()


def test_delete_with_a_traversal_id_does_not_escape():
    base = _live_server()
    for sid in ["..%2F..%2Fetc%2Fpasswd", "*", "%2e%2e%2fx"]:
        req = urllib.request.Request(base + "/sessions/" + sid, method="DELETE")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                json.loads(r.read())  # a plain miss
        except urllib.error.HTTPError as exc:
            ok(exc.code == 404, "want 200/404, got %s" % exc.code)


def test_sse_delivers_the_snapshot_and_reflects_later_events():
    base = _live_server()
    sock = socket.create_connection(("127.0.0.1", int(base.rsplit(":", 1)[1])), 5)
    sock.settimeout(5)
    sock.sendall(b"GET /stream HTTP/1.1\r\nHost: x\r\nAccept: text/event-stream\r\n\r\n")
    time.sleep(0.4)
    _post(base, {"session_id": "sse-1", "event": "Notification"})
    buf = b""
    deadline = time.time() + 5
    while time.time() < deadline and b"sse-1" not in buf:
        buf += sock.recv(65536)
    sock.close()
    ok(b"data: " in buf, "no SSE data frame")
    ok(b"sse-1" in buf, "event never reached the subscriber")


def test_disconnected_sse_subscribers_are_cleaned_up():
    s = _srv()
    base = _live_server()
    port = int(base.rsplit(":", 1)[1])
    start = len(s.store.subscribers)
    socks = []
    for _ in range(8):
        so = socket.create_connection(("127.0.0.1", port), 5)
        so.settimeout(3)
        so.sendall(b"GET /stream HTTP/1.1\r\nHost: x\r\nAccept: text/event-stream\r\n\r\n")
        try:
            so.recv(4096)
        except Exception:
            pass
        socks.append(so)
    for _ in range(50):
        if len(s.store.subscribers) >= start + 8:
            break
        time.sleep(0.1)
    eq(len(s.store.subscribers), start + 8, "subscribers did not register")
    for so in socks:
        so.close()
    for _ in range(100):
        _post(base, {"session_id": "nudge", "event": "Stop"})
        if len(s.store.subscribers) <= start:
            break
        time.sleep(0.1)
    eq(len(s.store.subscribers), start, "subscriber set leaked after disconnect")
    urllib.request.urlopen(
        urllib.request.Request(base + "/sessions", method="DELETE"), timeout=5
    ).read()


def test_concurrent_posts_do_not_lose_or_corrupt_sessions():
    base = _live_server()
    urllib.request.urlopen(
        urllib.request.Request(base + "/sessions", method="DELETE"), timeout=5
    ).read()
    errors = []

    def worker(k):
        for i in range(20):
            try:
                _post(base, {"session_id": "c%d" % ((k * 20 + i) % 50),
                             "event": "PreToolUse", "project": "p", "pid": 1})
            except Exception as exc:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    eq(errors, [])
    with urllib.request.urlopen(base + "/sessions", timeout=5) as r:
        snap = json.loads(r.read())
    eq(len(snap["sessions"]), 50)
    urllib.request.urlopen(
        urllib.request.Request(base + "/sessions", method="DELETE"), timeout=5
    ).read()


# --- idle notifications ------------------------------------------------------


def test_an_idle_notification_leaves_the_lamp_alone():
    """Claude Code fires Notification when it has merely been sitting idle for
    a minute. Painting that amber left finished sessions claiming they needed
    you with nothing running."""
    st = fresh()
    st.apply(_ev(session_id="a", event="Stop"))
    since = st.sessions["a"]["since"]
    st.apply(_ev(session_id="a", event="Notification", idle=True,
                 detail="waiting for input"))
    eq(st.sessions["a"]["state"], "green", "an idle notification must not repaint")
    eq(st.sessions["a"]["since"], since, "nor restart the timer")

    st.apply(_ev(session_id="a", event="PreToolUse"))
    eq(st.sessions["a"]["state"], "red")
    red_since = st.sessions["a"]["since"]
    st.apply(_ev(session_id="a", event="Notification", idle=True))
    eq(st.sessions["a"]["state"], "red", "a working session stays working")
    eq(st.sessions["a"]["since"], red_since)

    # ...but a real one still does its job.
    st.apply(_ev(session_id="a", event="Notification", idle=False,
                 detail="needs permission: Bash"))
    eq(st.sessions["a"]["state"], "orange")

    # An idle notification for a session we have never seen has to land
    # somewhere; green is the honest default.
    st.apply(_ev(session_id="ghost", event="Notification", idle=True))
    eq(st.sessions["ghost"]["state"], "green")


def test_the_curl_fallback_classifies_notifications_too():
    """A host with Docker but no Python posts the raw payload to /hook, so that
    path has to reach the same verdict as the full host-side hook - otherwise
    exactly the machines the fallback exists for keep the bug."""
    base = _live_server()

    def hook(sid, **payload):
        payload.setdefault("session_id", sid)
        payload.setdefault("hook_event_name", payload.get("event", "Notification"))
        req = urllib.request.Request(
            base + "/hook/" + payload["hook_event_name"],
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())

    sid = "curl-idle"
    eq(hook(sid, hook_event_name="Stop")["state"], "green", "finished")
    eq(hook(sid, notification_type="idle_prompt",
            message="Claude Code is idle. Idle message: x")["state"], "green",
       "an idle notification must leave a finished session green")
    eq(hook(sid, notification_type="permission_prompt",
            message="Bash wants to run: rm -rf /tmp")["state"], "orange",
       "a permission prompt still lights it up")
    snap = _srv().store.sessions[sid]
    eq(snap["detail"], "needs permission: Bash", "detail shortened the same way")
    urllib.request.urlopen(
        urllib.request.Request(base + "/sessions/" + sid, method="DELETE"),
        timeout=5).read()


# --- liveness ----------------------------------------------------------------


def _registry(entries):
    """Build a fake ~/.claude mount holding Claude Code's session registry."""
    import json as _json
    import os
    import tempfile
    mount = tempfile.mkdtemp(prefix="clt-registry-")
    os.makedirs(os.path.join(mount, "sessions"))
    for sid, extra in entries.items():
        body = {"sessionId": sid, "status": "idle",
                "updatedAt": time.time() * 1000, "cwd": "/tmp/" + sid}
        body.update(extra or {})
        with open(os.path.join(mount, "sessions", sid + ".json"), "w",
                  encoding="utf-8") as fh:
            _json.dump(body, fh)
    return mount


def _seed(app, sid, age_seconds=0.0, started=None):
    app.store.apply(app.Event(session_id=sid, event="Stop", project=sid))
    row = app.store.sessions[sid]
    row["updated"] = time.time() - age_seconds
    row["started"] = time.time() - (started if started is not None else age_seconds)
    return row


def test_a_session_is_never_dropped_for_being_idle():
    """The reported behaviour: rows vanished after a while. A session left open
    in the editor fires no hooks at all between turns, so quietness must never
    be treated as death - however long it lasts."""
    app = _srv()
    mount = _registry({"open": None})
    saved = (app.HOST_CLAUDE, app.STALE_SECONDS)
    app.HOST_CLAUDE = mount
    app.STALE_SECONDS = 1
    try:
        app.store.sessions.clear()
        app.store.missing_since.clear()
        # a full day of silence
        _seed(app, "open", age_seconds=86400)
        app.store.reap()
        ok("open" in app.store.sessions,
           "an open session must survive any amount of idleness")
        # and again much later
        app.store.reap()
        ok("open" in app.store.sessions, "still there on a later sweep")
    finally:
        app.HOST_CLAUDE, app.STALE_SECONDS = saved
        app.store.sessions.clear()
        app.store.missing_since.clear()


def test_a_session_goes_when_claude_code_stops_listing_it():
    """Closing the editor tab removes it from the registry; the row follows."""
    app = _srv()
    mount = _registry({})          # nothing registered: the session is gone
    saved = (app.HOST_CLAUDE, app.MISSING_GRACE, app.NEW_SESSION_GRACE)
    app.HOST_CLAUDE = mount
    app.MISSING_GRACE = 0
    app.NEW_SESSION_GRACE = 0
    try:
        app.store.sessions.clear()
        app.store.missing_since.clear()
        _seed(app, "closed", age_seconds=5)
        app.store.reap()
        ok("closed" not in app.store.sessions,
           "a session no longer listed should be removed")
    finally:
        app.HOST_CLAUDE, app.MISSING_GRACE, app.NEW_SESSION_GRACE = saved
        app.store.sessions.clear()
        app.store.missing_since.clear()


def test_a_brand_new_session_is_not_dropped_before_it_registers():
    """A hook can beat Claude Code to writing the registry file."""
    app = _srv()
    mount = _registry({})
    saved = (app.HOST_CLAUDE, app.MISSING_GRACE, app.NEW_SESSION_GRACE)
    app.HOST_CLAUDE = mount
    app.MISSING_GRACE = 0
    app.NEW_SESSION_GRACE = 90
    try:
        app.store.sessions.clear()
        app.store.missing_since.clear()
        _seed(app, "fresh", age_seconds=0, started=1)
        app.store.reap()
        ok("fresh" in app.store.sessions,
           "a just-started session must be given time to register")
    finally:
        app.HOST_CLAUDE, app.MISSING_GRACE, app.NEW_SESSION_GRACE = saved
        app.store.sessions.clear()
        app.store.missing_since.clear()


def test_one_missed_scan_does_not_remove_a_row():
    """Registry files are rewritten in place; a scan can catch one mid-write."""
    app = _srv()
    mount = _registry({})
    saved = (app.HOST_CLAUDE, app.MISSING_GRACE, app.NEW_SESSION_GRACE)
    app.HOST_CLAUDE = mount
    app.MISSING_GRACE = 60
    app.NEW_SESSION_GRACE = 0
    try:
        app.store.sessions.clear()
        app.store.missing_since.clear()
        _seed(app, "blink", age_seconds=5)
        app.store.reap()
        ok("blink" in app.store.sessions,
           "one absent scan must not be enough to delete a row")
        ok("blink" in app.store.missing_since, "absence should be recorded")

        # it comes back before the grace expires
        import json as _json
        import os
        with open(os.path.join(mount, "sessions", "blink.json"), "w",
                  encoding="utf-8") as fh:
            _json.dump({"sessionId": "blink", "updatedAt": time.time() * 1000}, fh)
        app.store.reap()
        ok("blink" in app.store.sessions, "still alive")
        ok("blink" not in app.store.missing_since,
           "the absence record should be cleared once it reappears")
    finally:
        app.HOST_CLAUDE, app.MISSING_GRACE, app.NEW_SESSION_GRACE = saved
        app.store.sessions.clear()
        app.store.missing_since.clear()


def test_an_old_registry_entry_still_counts_as_live_by_default():
    """REGISTRY_TTL defaults to 0: presence is enough, nothing ages out."""
    app = _srv()
    eq(app.REGISTRY_TTL, 0, "the default must not age entries out")
    mount = _registry({"ancient": {"updatedAt": (time.time() - 99999) * 1000}})
    saved = (app.HOST_CLAUDE, app.MISSING_GRACE, app.NEW_SESSION_GRACE)
    app.HOST_CLAUDE = mount
    app.MISSING_GRACE = 0
    app.NEW_SESSION_GRACE = 0
    try:
        app.store.sessions.clear()
        app.store.missing_since.clear()
        _seed(app, "ancient", age_seconds=99999)
        app.store.reap()
        ok("ancient" in app.store.sessions,
           "an old registry entry is still a running session")
    finally:
        app.HOST_CLAUDE, app.MISSING_GRACE, app.NEW_SESSION_GRACE = saved
        app.store.sessions.clear()
        app.store.missing_since.clear()


def test_session_end_removes_the_row_immediately():
    """The clean path: quitting Claude fires SessionEnd, no waiting."""
    app = _srv()
    app.store.sessions.clear()
    app.store.apply(app.Event(session_id="bye", event="Stop", project="bye"))
    ok("bye" in app.store.sessions)
    app.store.apply(app.Event(session_id="bye", event="SessionEnd", project="bye"))
    ok("bye" not in app.store.sessions, "SessionEnd removes it at once")


def test_no_registry_falls_back_to_the_timeout():
    """Without the mount there is nothing to consult, so behave as before."""
    app = _srv()
    saved = (app.HOST_CLAUDE, app.STALE_SECONDS)
    app.HOST_CLAUDE = "/definitely/not/here"
    app.STALE_SECONDS = 1
    try:
        eq(app.live_session_ids(), None, "an unreadable registry reports None")
        app.store.sessions.clear()
        _seed(app, "x", age_seconds=9999)
        app.store.reap()
        ok("x" not in app.store.sessions, "the timeout still applies")
    finally:
        app.HOST_CLAUDE, app.STALE_SECONDS = saved
        app.store.sessions.clear()


def test_running_sessions_are_adopted_even_if_they_never_report():
    """Hooks only fire when something happens. A session sitting idle - or any
    session at all after the container restarts - would otherwise be invisible
    despite being open."""
    app = _srv()
    mount = _registry({"never-spoke": {"cwd": "/home/dev/projects/quiet-one",
                                       "pid": 4321}})
    saved = app.HOST_CLAUDE
    app.HOST_CLAUDE = mount
    try:
        app.store.sessions.clear()
        app.store.missing_since.clear()
        eq(len(app.store.sessions), 0, "starting empty")
        app.store.reap()
        row = app.store.sessions.get("never-spoke")
        ok(row, "a running session must be adopted: %r" % app.store.sessions)
        eq(row["project"], "quiet-one", "project comes from the registry cwd")
        eq(row["state"], "unknown",
           "we know it is running, not what it is doing - and green would "
           "claim it had finished while it may be mid-turn")
        eq(row["adopted"], True, "marked as adopted")
        eq(row["pid"], 4321, "pid carried over")
    finally:
        app.HOST_CLAUDE = saved
        app.store.sessions.clear()
        app.store.missing_since.clear()


def test_adoption_never_overwrites_a_session_we_heard_from():
    """A real hook knows the state; the registry only knows it exists."""
    app = _srv()
    mount = _registry({"known": {"cwd": "/home/dev/projects/known"}})
    saved = app.HOST_CLAUDE
    app.HOST_CLAUDE = mount
    try:
        app.store.sessions.clear()
        app.store.apply(app.Event(session_id="known", event="Notification",
                                  project="known", detail="needs permission"))
        eq(app.store.sessions["known"]["state"], "orange")
        app.store.reap()
        eq(app.store.sessions["known"]["state"], "orange",
           "adoption must not clobber a reported state")
        ok(not app.store.sessions["known"].get("adopted"),
           "a session we heard from is not an adopted one")
    finally:
        app.HOST_CLAUDE = saved
        app.store.sessions.clear()


def test_the_fallback_classifier_holds_if_the_hook_payload_is_missing():
    """The image always ships the hook payload, but if that import ever fails
    the degraded path must not go back to turning idle sessions amber."""
    app = _srv()
    saved = app._hook
    app._hook = None
    try:
        ok(app._fallback_is_idle({"message": "Claude is waiting for your input"}),
           "idle message")
        ok(app._fallback_is_idle({"notification_type": "auth_success"}),
           "a type that wants nothing")
        ok(not app._fallback_is_idle({"notification_type": "permission_prompt"}),
           "a documented blocking type")
        ok(not app._fallback_is_idle({"message": "Bash wants to run: npm test"}),
           "a permission message still blocks")

        app.store.sessions.clear()
        app.store.apply(app.Event(session_id="fb", event="Stop", project="fb"))
        eq(app.store.sessions["fb"]["state"], "green")
        app.store.apply(app.Event(session_id="fb", event="Notification",
                                  project="fb", idle=True))
        eq(app.store.sessions["fb"]["state"], "green",
           "an idle notification must not repaint a finished session")
    finally:
        app._hook = saved
        app.store.sessions.clear()


# --- the host's verdict on what is really running ----------------------------


def _reset(app):
    app.store.sessions.clear()
    app.store.missing_since.clear()
    app.store.ended.clear()
    app.store.host_live = None
    app.store.host_live_at = 0.0


def test_a_killed_session_goes_even_though_its_registry_file_remains():
    """The reported bug. Close the editor window and Claude Code never gets to
    delete its registry entry, and nothing ever rewrites it - so from in here
    the dead session looks exactly like one left open. The host says which
    pids are still running; that is what settles it."""
    app = _srv()
    mount = _registry({"killed": None, "open": None})
    saved = (app.HOST_CLAUDE, app.NEW_SESSION_GRACE)
    app.HOST_CLAUDE = mount
    app.NEW_SESSION_GRACE = 0
    try:
        _reset(app)
        _seed(app, "killed", age_seconds=30)
        _seed(app, "open", age_seconds=30)
        app.store.reap()
        ok("killed" in app.store.sessions, "the file alone keeps it there")

        app.store.set_host_live(["open"])     # the watcher checked the pids
        app.store.reap()
        ok("killed" not in app.store.sessions,
           "a session whose process is gone must not survive its own file")
        ok("open" in app.store.sessions, "the one still running stays")
    finally:
        app.HOST_CLAUDE, app.NEW_SESSION_GRACE = saved
        _reset(app)


def test_a_killed_session_is_not_adopted_back():
    """Removing the row is not enough: the file is still there, so the next
    sweep would adopt it straight back."""
    app = _srv()
    mount = _registry({"killed": None})
    saved = app.HOST_CLAUDE
    app.HOST_CLAUDE = mount
    try:
        _reset(app)
        app.store.set_host_live([])
        app.store.reap()
        eq(app.store.sessions, {}, "a dead entry must not become a row: %r"
           % app.store.sessions)
    finally:
        app.HOST_CLAUDE = saved
        _reset(app)


def test_a_host_report_goes_stale_rather_than_ruling_for_ever():
    """The watcher exits with the last session. Its final word must not keep
    reaping rows for the sessions you open afterwards."""
    app = _srv()
    mount = _registry({"later": None})
    saved = (app.HOST_CLAUDE, app.NEW_SESSION_GRACE)
    app.HOST_CLAUDE = mount
    app.NEW_SESSION_GRACE = 0
    try:
        _reset(app)
        app.store.set_host_live([])
        app.store.host_live_at = time.time() - app.HOST_REPORT_TTL - 1
        eq(app.store.host_live_ids(), None, "an old report is no report")
        app.store.reap()
        ok("later" in app.store.sessions,
           "with no current report the registry decides again")
    finally:
        app.HOST_CLAUDE, app.NEW_SESSION_GRACE = saved
        _reset(app)


def test_a_brand_new_session_survives_a_report_that_predates_it():
    app = _srv()
    mount = _registry({})
    saved = (app.HOST_CLAUDE, app.NEW_SESSION_GRACE)
    app.HOST_CLAUDE = mount
    app.NEW_SESSION_GRACE = 90
    try:
        _reset(app)
        _seed(app, "fresh", age_seconds=0, started=1)
        app.store.set_host_live([])
        app.store.reap()
        ok("fresh" in app.store.sessions,
           "a session that has only just started has not registered yet")
    finally:
        app.HOST_CLAUDE, app.NEW_SESSION_GRACE = saved
        _reset(app)


def test_the_host_report_works_without_the_registry_mount():
    """No mount means no adoption, but a row we heard from must still go when
    the host says its process has ended."""
    app = _srv()
    saved = (app.HOST_CLAUDE, app.NEW_SESSION_GRACE, app.STALE_SECONDS)
    app.HOST_CLAUDE = "/definitely/not/here"
    app.NEW_SESSION_GRACE = 0
    app.STALE_SECONDS = 99999
    try:
        _reset(app)
        _seed(app, "gone", age_seconds=5)
        _seed(app, "here", age_seconds=5)
        app.store.set_host_live(["here"])
        app.store.reap()
        ok("gone" not in app.store.sessions, "the host's verdict still applies")
        ok("here" in app.store.sessions)
    finally:
        app.HOST_CLAUDE, app.NEW_SESSION_GRACE, app.STALE_SECONDS = saved
        _reset(app)


def test_a_session_that_ended_is_not_resurrected_by_its_own_file():
    """SessionEnd removes the row; Claude Code deletes the file a moment
    later. In between, a sweep must not put the row back."""
    app = _srv()
    mount = _registry({"bye": None})
    saved = app.HOST_CLAUDE
    app.HOST_CLAUDE = mount
    try:
        _reset(app)
        app.store.apply(app.Event(session_id="bye", event="Stop", project="bye"))
        app.store.apply(app.Event(session_id="bye", event="SessionEnd",
                                  project="bye"))
        app.store.reap()
        ok("bye" not in app.store.sessions,
           "the row came back from the dead: %r" % app.store.sessions)
    finally:
        app.HOST_CLAUDE = saved
        _reset(app)


def test_a_resumed_session_can_be_adopted_again():
    """The tombstone must not outlive its purpose: --resume keeps the id."""
    app = _srv()
    mount = _registry({"again": None})
    saved = app.HOST_CLAUDE
    app.HOST_CLAUDE = mount
    try:
        _reset(app)
        app.store.apply(app.Event(session_id="again", event="SessionEnd",
                                  project="again"))
        app.store.apply(app.Event(session_id="again", event="SessionStart",
                                  project="again"))
        eq(app.store.sessions["again"]["state"], "green", "it is back")
        app.store.sessions.clear()
        app.store.reap()
        ok("again" in app.store.sessions,
           "a session that reported after ending is live again")
    finally:
        app.HOST_CLAUDE = saved
        _reset(app)


def test_the_live_endpoint_reports_what_it_is_watching():
    app = _srv()
    try:
        _reset(app)
        eq(app.store.host_live_ids(), None, "nothing reported yet")
        app.store.set_host_live(["a", "b"])
        eq(app.store.host_live_ids(), {"a", "b"}, "the last report stands")
    finally:
        _reset(app)
