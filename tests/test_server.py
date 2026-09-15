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
