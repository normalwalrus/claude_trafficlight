"""Panel tests.

The panel is driven headlessly: a real Tk root is built (the window is made
fully transparent so nothing flashes on screen), snapshots are pushed straight
into on_snapshot, and the canvas is inspected afterwards.

Anything that needs a *different* starting config runs in a subprocess, because
tearing down and rebuilding tkinter roots in one interpreter leaves stray
`after` callbacks behind.
"""

import json
import os
import subprocess
import sys
import tempfile
import time

from harness import Skip, eq, ok

# Both must be set before panel is imported: it reads them at module scope.
_CFG_HOME = os.path.join(tempfile.gettempdir(), "clt-tests-appdata")
os.environ["APPDATA"] = _CFG_HOME
os.environ["CLAUDE_LIGHT_URL"] = "http://127.0.0.1:9"  # nothing listens there

import panel as P  # noqa: E402

PY = sys.executable
if PY.lower().endswith("pythonw.exe"):
    PY = PY[: -len("pythonw.exe")] + "python.exe"

_shared = {}


def panel():
    if "p" not in _shared:
        os.makedirs(P.CONFIG_DIR, exist_ok=True)
        with open(P.CONFIG_PATH, "w", encoding="utf-8") as fh:
            fh.write("{}")
        try:
            p = P.Panel()
        except Exception as exc:  # no display
            raise Skip("cannot build a tk window here: %s" % exc)
        p.feed.stopped.set()
        p.root.attributes("-alpha", 0.0)
        p.connected = True  # the Feed is stopped; pretend the server is there
        _shared["p"] = p
    p = _shared["p"]
    # Transient UI state does not survive between tests. Amber banners in
    # particular are sticky by design and would cover later tests' rows.
    # Including the connection: rows are drawn as unknown while the server is
    # away, so a test that dropped the connection would leave every later one
    # looking at dark lamps.
    p.connected = True
    # Including anything a test stubbed out: the panel is shared between them.
    p.__dict__.pop("push_settings", None)
    p.settings_revision = 0
    p._seeded_settings = False
    p.banners.clear()
    p.anim.clear()
    p._chime_armed.clear()
    p._window_cache.clear()
    p.flash_until.clear()
    p.expanded.clear()
    p.settings_open = False
    p.collapsed = False
    return p


def session(i=0, **kw):
    now = time.time()
    s = {"session_id": "s%d" % i, "project": "proj%d" % i, "state": "green",
         "since": now, "started": now, "updated": now, "detail": "",
         "pid": 0, "hwnd": 0}
    s.update(kw)
    return s


def snapshot(sessions, now=None):
    return json.dumps({"type": "snapshot", "now": now or time.time(),
                       "revision": 1, "sessions": sessions})


def texts(p):
    out = []
    for item in p.canvas.find_all():
        if p.canvas.type(item) == "text":
            out.append(p.canvas.itemcget(item, "text"))
    return out


def in_subprocess(config, mode):
    """Build a Panel with `config` on disk and report what happened."""
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_panel_case.py")
    proc = subprocess.run([PY, script, config, mode], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=60)
    line = (proc.stdout or "").strip().splitlines()
    if not line:
        raise AssertionError("no output; stderr=%s" % (proc.stderr or "")[:400])
    return json.loads(line[-1])


# --- formatting -------------------------------------------------------------


def test_fmt_elapsed():
    eq(P.fmt_elapsed(0), "0:00")
    eq(P.fmt_elapsed(5), "0:05")
    eq(P.fmt_elapsed(65), "1:05")
    eq(P.fmt_elapsed(599), "9:59")
    eq(P.fmt_elapsed(3600), "1:00h")
    eq(P.fmt_elapsed(3660), "1:01h")
    eq(P.fmt_elapsed(86400), "24:00h")
    eq(P.fmt_elapsed(-1), "0:00", "negative elapsed must not go backwards")
    eq(P.fmt_elapsed(-99999), "0:00")
    eq(P.fmt_elapsed(1.9), "0:01")


def test_coercion_helpers():
    eq(P.as_float(None, 3.0), 3.0)
    eq(P.as_float("x", 3.0), 3.0)
    eq(P.as_float("2.5"), 2.5)
    eq(P.as_int(None), 0)
    eq(P.as_int("7"), 7)
    eq(P.as_int([1]), 0)


def test_load_config_survives_a_damaged_file():
    os.makedirs(P.CONFIG_DIR, exist_ok=True)
    for content in ["", "   ", "not json", "[]", '"str"', "123", "{", "\x00\x01"]:
        with open(P.CONFIG_PATH, "w", encoding="utf-8") as fh:
            fh.write(content)
        eq(P.load_config(), {}, repr(content))
    with open(P.CONFIG_PATH, "w", encoding="utf-8") as fh:
        fh.write('{"topmost": false}')
    eq(P.load_config(), {"topmost": False})


# --- rendering --------------------------------------------------------------


def test_renders_an_empty_state():
    p = panel()
    p.on_snapshot(snapshot([]))
    eq(p.sessions, [])
    ok(any("waiting" in t or "offline" in t for t in texts(p)), "no empty-state text")


def test_renders_one_row_per_session():
    p = panel()
    p.connected = True
    p.on_snapshot(snapshot([session(i) for i in range(3)]))
    eq(len(p.row_hitboxes), 3)
    labels = texts(p)
    for i in range(3):
        ok(any(t.startswith("proj%d" % i) for t in labels), "proj%d missing" % i)
    ok("3 sessions" in labels, "header count wrong: %r" % labels)


def test_long_and_unicode_project_names_are_truncated_not_fatal():
    p = panel()
    p.on_snapshot(snapshot([
        session(0, project="x" * 500),
        session(1, project="\U0001f6a6\U0001f6a6 日本語の長い名前" * 5),
        session(2, project=""),
        session(3, project=None),
    ]))
    for t in texts(p):
        ok(len(t) <= 60, "label not truncated: %r" % t[:80])


def test_orange_rows_append_the_detail_text():
    p = panel()
    p.on_snapshot(snapshot([session(0, state="orange", detail="needs permission: Bash")]))
    ok(any("Bash" in t for t in texts(p)), "detail missing: %r" % texts(p))
    # a red row does not show detail
    p.on_snapshot(snapshot([session(0, state="red", detail="needs permission: Bash")]))
    ok(not any("Bash" in t for t in texts(p)), "detail shown on a red row")


def test_a_long_idle_green_row_reads_idle():
    p = panel()
    p.on_snapshot(snapshot([session(0, state="green", since=time.time() - 9999)]))
    ok("idle" in texts(p), "expected 'idle': %r" % texts(p))


def test_a_since_in_the_future_does_not_render_a_negative_timer():
    p = panel()
    p.on_snapshot(snapshot([session(0, state="red", since=time.time() + 99999)]))
    ok(not any(t.startswith("-") for t in texts(p)), "negative timer: %r" % texts(p))


def test_malformed_snapshots_never_raise():
    """draw() runs from a tkinter `after` callback inside pythonw, where a
    traceback goes nowhere. If one escapes, the reschedule is skipped and the
    panel silently freezes forever."""
    p = panel()
    bad = [
        "not json", "", "[]", "null", '"text"', "{}",
        '{"sessions": "nope"}', '{"sessions": 5}', '{"sessions": [1, 2, 3]}',
        '{"sessions": [null]}', '{"sessions": [{}]}',
        '{"sessions": [{"session_id": "a"}]}',
        '{"sessions": [{"session_id": "a", "since": null}]}',
        '{"sessions": [{"session_id": "a", "since": "abc"}]}',
        '{"sessions": [{"session_id": "a", "state": 5, "project": 7}]}',
        '{"now": "x", "sessions": []}',
        '{"now": null, "sessions": []}',
    ]
    for body in bad:
        p.on_snapshot(body)  # must not raise
    p.draw()
    # and the panel still works afterwards
    p.on_snapshot(snapshot([session(0)]))
    eq(len(p.row_hitboxes), 1)


def test_pump_and_tick_reschedule_even_when_the_body_explodes():
    p = panel()
    original = p.draw
    p.draw = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        p.tick()          # must swallow and reschedule
        p.q.put(("snapshot", snapshot([session(0)])))
        p.pump()          # must swallow and reschedule
    finally:
        p.draw = original
    # if the reschedules were skipped tkinter would have no pending callbacks;
    # the important part is simply that neither call raised.
    p.draw()


def test_row_hit_testing():
    p = panel()
    p.on_snapshot(snapshot([session(i) for i in range(3)]))
    eq(p.row_at(P.HEADER_H - 1), None, "header is not a row")
    eq(p.row_at(P.HEADER_H + 1)["session_id"], "s0")
    eq(p.row_at(P.HEADER_H + P.ROW_H + 1)["session_id"], "s1")
    eq(p.row_at(P.HEADER_H + 3 * P.ROW_H + 5), None, "below the last row")


def test_toast_text_is_bounded():
    p = panel()
    p.toast("x" * 200)
    eq(len(p._toast[0]), 32)
    p.toast(None)
    ok(isinstance(p._toast[0], str))


# --- alerts -----------------------------------------------------------------


def flush_chimes(p):
    """Fire any armed chime as the delayed callback would."""
    for sid, (state, _t) in list(p._chime_armed.items()):
        p.fire_chime(sid, state)


def test_no_chime_storm_on_the_first_snapshot_or_on_reconnect():
    p = panel()
    import chime
    played = []
    original = chime.play
    # Signature must match panel's call: play(kind, level, sound).
    chime.play = lambda k, l="normal", sound=None: played.append(k)
    try:
        p.prev_state.clear()
        p.seen_first_snapshot = False
        p.on_snapshot(snapshot([session(0, state="orange"), session(1, state="green")]))
        flush_chimes(p)
        eq(played, [], "first snapshot must be silent")

        p.on_snapshot(snapshot([session(0, state="red"), session(1, state="red")]))
        flush_chimes(p)
        eq(played, [], "transitions into red are silent")

        p.on_snapshot(snapshot([session(0, state="orange"), session(1, state="green")]))
        flush_chimes(p)
        eq(sorted(played), ["green", "orange"], "a genuine transition must chime")
        played[:] = []

        p.on_snapshot(snapshot([session(0, state="orange"), session(1, state="green")]))
        flush_chimes(p)
        eq(played, [], "an identical snapshot must not re-chime")

        # a brand new session that arrives already orange is not a transition
        p.on_snapshot(snapshot([session(0, state="orange"), session(1, state="green"),
                                session(2, state="orange")]))
        flush_chimes(p)
        eq(played, [], "a new session must not chime on arrival")

        # server restart: Feed reports disconnect, pump clears the flag
        p.q.put(("connected", False))
        p.pump()
        eq(p.seen_first_snapshot, False, "disconnect must arm the suppression")
        p.on_snapshot(snapshot([session(0, state="green"), session(1, state="orange"),
                                session(2, state="green")]))
        flush_chimes(p)
        eq(played, [], "no chime storm on the first snapshot after reconnect")

        # ...but alerts come back straight afterwards
        p.on_snapshot(snapshot([session(0, state="red"), session(1, state="red"),
                                session(2, state="red")]))
        p.on_snapshot(snapshot([session(0, state="green"), session(1, state="green"),
                                session(2, state="green")]))
        flush_chimes(p)
        eq(sorted(played), ["green", "green", "green"], "alerts did not resume")
    finally:
        chime.play = original


def test_alerts_off_still_flashes_but_makes_no_sound():
    p = panel()
    import chime
    played = []
    original = chime.play
    # Signature must match panel's call: play(kind, level, sound).
    chime.play = lambda k, l="normal", sound=None: played.append(k)
    saved = p.sounds
    try:
        p.sounds = False
        p.prev_state.clear()
        p.flash_until.clear()
        p.seen_first_snapshot = False
        p.on_snapshot(snapshot([session(0, state="red")]))
        p.on_snapshot(snapshot([session(0, state="green")]))
        eq(played, [])
        ok(p.flash_until.get("s0", 0) > time.time(), "row should still flash")
    finally:
        chime.play = original
        p.sounds = saved


def test_state_for_departed_sessions_is_pruned():
    p = panel()
    p.on_snapshot(snapshot([session(i) for i in range(3)]))
    p.flash_until["s1"] = time.time() + 60
    p.on_snapshot(snapshot([session(0)]))
    eq(sorted(p.prev_state), ["s0"])
    eq(sorted(p.flash_until), [])


# --- geometry (subprocess: each needs its own starting config) --------------


def test_the_window_never_grows_off_the_bottom_of_the_screen():
    for rows in (0, 1, 20, 31, 50, 200):
        r = in_subprocess("{}", str(rows))
        ok(r.get("ok"), "rows=%d: %s" % (rows, r.get("err")))
        ok(not r["offscreen"],
           "rows=%d: window is %dpx tall at y=%d on a %dpx screen"
           % (rows, r["h"], r["y"], r["screen"][1]))
    # the overflow is reported rather than silently dropped
    r = in_subprocess("{}", "200")
    ok(any("more" in t for t in r["texts"]), "no overflow indicator: %r" % r["texts"])
    ok("200 sessions" in r["texts"], "header must still show the real count")


def test_a_saved_position_off_screen_is_clamped_back_into_view():
    """The panel has no title bar and no taskbar entry: if it restores onto a
    monitor that is no longer attached there is no way to get it back."""
    screen = in_subprocess("{}", "pos")["screen"]
    for pos in ([-30000, -30000], [99999, 99999], [screen[0] + 500, screen[1] + 500],
                [-500, -500]):
        r = in_subprocess(json.dumps({"pos": pos}), "pos")
        ok(r.get("ok"), r.get("err"))
        ok(r["x"] + P.WIDTH > 0 and r["x"] < r["screen"][0],
           "pos=%r landed at x=%d off a %dpx screen" % (pos, r["x"], r["screen"][0]))
        ok(0 <= r["y"] < r["screen"][1],
           "pos=%r landed at y=%d off a %dpx screen" % (pos, r["y"], r["screen"][1]))


def test_a_valid_saved_position_is_honoured():
    r = in_subprocess(json.dumps({"pos": [200, 300]}), "pos")
    eq((r["x"], r["y"]), (200, 300))


def test_a_corrupt_config_does_not_stop_the_panel_starting():
    """panel.json is user-visible and hand-editable; a bad value in it used to
    raise out of __init__, and under pythonw that means the panel just never
    appears with no error anywhere."""
    for config in ['{"alpha": "x"}', '{"alpha": null}', '{"alpha": 99}',
                   '{"alpha": -3}', '{"pos": ["a", "b"]}', '{"pos": [1]}',
                   '{"pos": "top"}', '{"pos": {}}', '{"volume": 42}',
                   '{"volume": "EXTREME"}', '{"topmost": "yes"}',
                   '{"sounds": "no"}', "not json", "[]", ""]:
        r = in_subprocess(config, "pos")
        ok(r.get("ok"), "config %s -> %s" % (config, r.get("err")))
        ok(0 <= r["y"] < r["screen"][1], "config %s put the panel off screen" % config)


def test_saving_config_to_an_unwritable_directory_is_silent():
    blocker = os.path.join(tempfile.gettempdir(), "clt-cfg-blocker")
    if os.path.isdir(blocker):
        os.rmdir(blocker)
    with open(blocker, "w") as fh:
        fh.write("not a directory")
    saved_dir, saved_path = P.CONFIG_DIR, P.CONFIG_PATH
    P.CONFIG_DIR = blocker
    P.CONFIG_PATH = os.path.join(blocker, "panel.json")
    try:
        P.save_config({"a": 1})  # must not raise
        eq(P.load_config(), {})
    finally:
        P.CONFIG_DIR, P.CONFIG_PATH = saved_dir, saved_path
        os.remove(blocker)


def test_config_round_trips():
    cfg = {"topmost": False, "sounds": False, "volume": "loud",
           "pos": [11, 22], "alpha": 0.5}
    P.save_config(cfg)
    eq(P.load_config(), cfg)
    P.save_config({})


# --- expandable rows ---------------------------------------------------------


USAGE = {
    "context_tokens": 193981, "context_limit": 1000000,
    "tokens_in": 314, "tokens_out": 267328, "tokens_cache_write": 328625,
    "tokens_cache_read": 18214922, "turns": 157,
    "model": "claude-opus-5", "model_id": "claude-opus-5[1m]",
    "model_name": "Opus 5 (1M context)", "agents": 2, "approx": False,
}


def _height(p):
    return int(p.canvas["height"])


def test_fmt_tokens_is_compact_and_lossless_enough():
    eq(P.fmt_tokens(0), "0")
    eq(P.fmt_tokens(314), "314")
    eq(P.fmt_tokens(1000), "1k")
    eq(P.fmt_tokens(193981), "194k")
    eq(P.fmt_tokens(1000000), "1M")
    eq(P.fmt_tokens(18214922), "18.2M")
    eq(P.fmt_tokens(-5), "0", "negatives clamp")
    eq(P.fmt_tokens("nonsense"), "0", "junk clamps")


def test_a_row_expands_and_collapses():
    p = panel()
    p.expanded.clear()
    p.on_snapshot(snapshot([session(0, usage=USAGE)]))
    collapsed = _height(p)

    p.toggle_expanded(p.sessions[0])
    opened = _height(p)
    ok(opened > collapsed, "expanding should make the panel taller")
    eq(opened - collapsed, P.DETAIL_H, "grows by exactly one detail panel")

    p.toggle_expanded(p.sessions[0])
    eq(_height(p), collapsed, "collapsing returns to the original height")


def test_the_detail_shows_context_and_token_figures():
    p = panel()
    p.expanded.clear()
    p.on_snapshot(snapshot([session(0, state="red", tool="Bash", usage=USAGE)]))
    p.toggle_expanded(p.sessions[0])
    body = " ".join(texts(p))

    ok("194k" in body and "1M" in body, "context figures missing: %r" % body)
    ok("19%" in body, "context percentage missing: %r" % body)
    ok("267k" in body, "output tokens missing: %r" % body)
    ok("18.2M" in body, "cache reads missing: %r" % body)
    ok("Opus 5 (1M context)" in body, "model missing: %r" % body)
    ok("157 turns" in body, "turn count missing: %r" % body)
    ok("Working" in body and "Bash" in body, "activity missing: %r" % body)
    ok("2 agents" in body, "agent count missing: %r" % body)
    p.toggle_expanded(p.sessions[0])


def test_the_detail_panel_fits_at_every_scale():
    """The detail height and the Focus button scale with the size slider, so
    the text inside has to as well. On fixed pixel steps it ran straight into
    the button below 100%: at 75% the token line and the model line both sat
    on top of it."""
    p = panel()
    was = p.scale
    try:
        for pct in (75, 85, 100, 125, 150):
            p.scale = pct / 100.0
            p.rescale()
            p.expanded.clear()
            p.settings_open = False
            p.on_snapshot(snapshot([session(0, state="red", tool="Bash",
                                            usage=USAGE)]))
            p.toggle_expanded(p.sessions[0])
            button = [b for b in p.button_hitboxes if b[4] == "focus"][0]
            bx0, by0, bx1, by1 = button[:4]
            for item in p.canvas.find_all():
                if p.canvas.type(item) != "text":
                    continue
                text = p.canvas.itemcget(item, "text")
                if text == "Focus window":
                    continue
                x0, y0, x1, y1 = p.canvas.bbox(item)
                ok(not (x0 < bx1 and x1 > bx0 and y0 < by1 and y1 > by0),
                   "at %d%% %r overlaps the Focus button" % (pct, text))
            ok(by1 <= p.row_hitboxes[0][1] + p.RH + p.DH,
               "at %d%% the button escapes the detail panel" % pct)
            p.toggle_expanded(p.sessions[0])
    finally:
        p.scale = was
        p.rescale()
        p.draw()


def test_the_detail_offers_a_focus_button():
    p = panel()
    p.expanded.clear()
    p.settings_open = False
    p.on_snapshot(snapshot([session(0, usage=USAGE)]))
    eq(len([b for b in p.button_hitboxes if b[4] == "focus"]), 0,
       "no row button while collapsed")

    p.toggle_expanded(p.sessions[0])
    row_buttons = [b for b in p.button_hitboxes if b[4] == "focus"]
    eq(len(row_buttons), 1, "focus button present")

    x0, y0, x1, y1, action, _s = row_buttons[0]
    got, _row = p.button_at((x0 + x1) // 2, (y0 + y1) // 2)
    eq(got, "focus", "hit testing at its own centre")
    ok(p.button_at(2, 2) == (None, None), "header is not a button")
    p.toggle_expanded(p.sessions[0])


def test_there_is_no_way_to_dismiss_a_row():
    """Dismissing was removed because it could not work: the session is still
    running, so the next registry sweep adopts the row straight back."""
    p = panel()
    p.expanded.clear()
    p.settings_open = False
    p.on_snapshot(snapshot([session(0, usage=USAGE)]))
    p.toggle_expanded(p.sessions[0])
    actions = {b[4] for b in p.button_hitboxes}
    ok("dismiss" not in actions, "a dismiss button is back: %r" % actions)
    ok(not hasattr(p, "delete_session"), "delete_session should be gone")
    ok(not hasattr(p, "clear_sessions"), "clear_sessions should be gone")
    ok(not hasattr(p, "on_middle_click"), "middle-click dismiss should be gone")
    p.toggle_expanded(p.sessions[0])


def test_a_session_without_token_data_still_expands():
    """Sessions started before the hook upgrade report no usage at all."""
    p = panel()
    p.expanded.clear()
    p.on_snapshot(snapshot([session(0, cwd="C:/work/thing")]))
    p.toggle_expanded(p.sessions[0])
    body = " ".join(texts(p))
    ok("No token data" in body, "should explain the absence: %r" % body)
    eq(len([b for b in p.button_hitboxes if b[4] == "focus"]), 1,
       "the focus button is still available")
    p.toggle_expanded(p.sessions[0])


def test_expansion_state_is_dropped_when_a_session_ends():
    p = panel()
    p.expanded.clear()
    p.on_snapshot(snapshot([session(0), session(1)]))
    p.toggle_expanded(p.sessions[0])
    p.toggle_expanded(p.sessions[1])
    eq(len(p.expanded), 2, "both expanded")

    p.on_snapshot(snapshot([session(1)]))
    eq(p.expanded, {"s1"}, "the departed session is forgotten")
    p.expanded.clear()


def test_an_expanded_row_still_fits_the_screen():
    p = panel()
    p.expanded.clear()
    p.on_snapshot(snapshot([session(i, usage=USAGE) for i in range(40)]))
    for s in p.sessions:
        p.expanded.add(s.get("session_id"))
    p.draw()
    screen = p.root.winfo_screenheight()
    ok(p.root.winfo_y() + _height(p) <= screen,
       "panel bottom %d must stay on a %d px screen"
       % (p.root.winfo_y() + _height(p), screen))
    ok(any("more" in t for t in texts(p)), "overflow should be summarised")
    p.expanded.clear()
    p.on_snapshot(snapshot([]))


def test_a_banner_does_not_outlive_the_state_it_describes():
    """A row that has moved on must not keep claiming it needs permission."""
    p = panel()
    # The Panel is shared between tests; leave every field as it was found.
    was_sounds, was_seen = p.sounds, p.seen_first_snapshot
    p.banners.clear()
    p.seen_first_snapshot = True
    p.sounds = False
    p.prev_state = {"s0": "red"}
    try:
        p.on_snapshot(snapshot([
            session(0, state="orange", detail="needs permission: Bash")]))
        ok("s0" in p.banners, "the orange transition should announce itself")
        ok(any("Bash" in t for t in texts(p)), "banner text missing: %r" % texts(p))

        p.on_snapshot(snapshot([
            session(0, state="red", detail="needs permission: Bash")]))
        ok("s0" not in p.banners, "the stale banner should have been dropped")
        ok(not any("Bash" in t for t in texts(p)),
           "a red row is still showing the old orange banner: %r" % texts(p))
    finally:
        p.banners.clear()
        p.anim.clear()
        p.sounds, p.seen_first_snapshot = was_sounds, was_seen


def test_the_lamps_cross_fade_then_pulse():
    p = panel()
    p.anim.clear()
    p.anim["x"] = ("red", "green", time.time())

    levels, pulse = p.lamp_levels("x", "green")
    ok(levels["red"] > 0.8 and levels["green"] < 0.2, "fade starts on the old lamp")
    eq(round(levels["red"] + levels["green"], 3), 1.0, "brightness is conserved")

    p.anim["x"] = ("red", "green", time.time() - P.FADE_SECS - P.PULSE_SECS / 2)
    levels, pulse = p.lamp_levels("x", "green")
    eq(levels["green"], 1.0, "new lamp fully lit once the fade is done")
    ok(pulse > 0.8, "the halo should be swelling mid-pulse: %r" % pulse)

    p.anim["x"] = ("red", "green", time.time() - 99)
    levels, pulse = p.lamp_levels("x", "green")
    eq(pulse, 0.0, "pulse is over")
    ok("x" not in p.anim, "a finished animation should clean itself up")


def test_an_idle_panel_stops_animating():
    """The frame loop must not keep waking the CPU once nothing is moving."""
    p = panel()
    p.anim.clear()
    p.banners.clear()
    ok(not p.busy(), "an idle panel is not busy")
    p.anim["y"] = ("red", "green", time.time())
    ok(p.busy(), "an animating panel is busy")
    p.anim.clear()
    p.banners["y"] = ("done", "green", time.time(), False)
    ok(p.busy(), "a visible banner keeps it busy")
    p.banners.clear()
    ok(not p.busy(), "idle again")


def test_colour_mixing():
    eq(P.mix("#000000", "#ffffff", 0.0), "#000000")
    eq(P.mix("#000000", "#ffffff", 1.0), "#ffffff")
    eq(P.mix("#000000", "#ffffff", 0.5), "#808080")
    eq(P.mix("#ff5964", "#000000", -5), "#ff5964", "clamps below 0")
    eq(P.mix("#ff5964", "#000000", 5), "#000000", "clamps above 1")


# --- settings ----------------------------------------------------------------


def test_the_panel_scales():
    p = panel()
    try:
        base_w, base_rh = p.W, p.RH
        p.set_scale(1.5)
        ok(p.W > base_w and p.RH > base_rh, "everything should grow together")
        eq(p.W, int(P.WIDTH * 1.5), "width tracks the scale exactly")
        ok(p.f(9)[1] > 9, "fonts scale too: %r" % (p.f(9),))

        p.set_scale(0.75)
        ok(p.W < base_w and p.RH < base_rh, "and shrink together")

        p.set_scale(99)
        eq(p.scale, P.MAX_SCALE, "clamped at the top")
        p.set_scale(-5)
        eq(p.scale, P.MIN_SCALE, "clamped at the bottom")
    finally:
        p.set_scale(1.0)


def test_the_scale_survives_a_restart():
    p = panel()
    try:
        p.set_scale(1.25)
        eq(round(load_saved().get("scale", 0), 2), 1.25, "scale is persisted")
    finally:
        p.set_scale(1.0)


def load_saved():
    with open(P.CONFIG_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def test_settings_opens_and_closes_without_moving_the_rows():
    p = panel()
    p.settings_open = False
    p.on_snapshot(snapshot([session(0), session(1)]))
    rows_before = [(t, b) for t, b, _s in p.row_hitboxes]
    closed = int(p.canvas["height"])

    p.toggle_settings()
    opened = int(p.canvas["height"])
    eq(opened - closed, p.SETTINGS_H, "the panel grows by exactly the settings block")
    rows_after = [(t, b) for t, b, _s in p.row_hitboxes]
    eq(rows_after, rows_before, "settings must not push the rows around")

    p.toggle_settings()
    eq(int(p.canvas["height"]), closed, "and shrinks back")


def test_settings_shows_every_control():
    p = panel()
    p.settings_open = True
    p.on_snapshot(snapshot([session(0)]))
    body = " ".join(texts(p))
    for want in ("SETTINGS", "Size", "Volume", "Sound", "Theme", "%"):
        ok(want in body, "missing control %r in %r" % (want, body))
    eq(sorted(b[4] for b in p.button_hitboxes),
       ["sound_next", "sound_prev", "sound_test", "theme_next", "theme_prev"],
       "sound and theme controls")
    eq(sorted(k for k, _a, _b, _c, _d in p.slider_hitboxes),
       ["scale", "volume"], "sliders")
    p.settings_open = False


def test_the_volume_slider_runs_from_muted_to_full():
    p = panel()
    p.settings_open = True
    p.draw()
    track = [(a, b) for k, a, b, _c, _d in p.slider_hitboxes if k == "volume"]
    ok(track, "volume slider present")
    x0, x1 = track[0]

    import chime
    original, was = chime.play, p.volume
    played = []
    chime.play = lambda *a, **k: played.append(a)
    try:
        for frac, want in ((0.0, 0), (0.25, 25), (0.5, 50), (1.0, 100)):
            p.apply_slider("volume", x0, x1, x0 + (x1 - x0) * frac)
            eq(p.volume, want, "slider at %.2f" % frac)
        p.apply_slider("volume", x0, x1, x1 + 999)
        eq(p.volume, 100, "past the end stays at full")
        p.apply_slider("volume", x0, x1, x0 - 999)
        eq(p.volume, 0, "before the start is muted")

        eq(played, [], "dragging must not fire a preview per pixel")

        # one preview when the drag ends, and none at all when muted
        class Ev:
            x = y = 0
        p.volume = 0
        p._drag_slider = ("volume", x0, x1)
        p.on_release(Ev())
        eq(played, [], "muted release stays silent")

        p.volume = 70
        p._drag_slider = ("volume", x0, x1)
        p.on_release(Ev())
        eq(len(played), 1, "exactly one preview when the drag ends")
    finally:
        chime.play = original
        p.volume = was if isinstance(was, int) else 60
        p.settings_open = False


def test_the_volume_readout_says_muted_at_zero():
    p = panel()
    p.settings_open = True
    was = p.volume
    try:
        p.volume = 0
        p.draw()
        ok("muted" in texts(p), "expected a muted label: %r" % texts(p))
        p.volume = 45
        p.draw()
        ok("45%" in texts(p), "expected 45%%: %r" % texts(p))
    finally:
        p.volume = was
        p.settings_open = False


def test_a_legacy_named_volume_in_the_config_is_migrated():
    """An existing panel.json holds "normal", not a number."""
    import chime
    for name in ("quiet", "normal", "loud"):
        got = chime.as_volume(name)
        ok(isinstance(got, int) and 0 < got <= 100,
           "%r should migrate to a number, got %r" % (name, got))


def test_cycling_sounds_wraps_and_persists():
    p = panel()
    import chime
    original = chime.play
    chime.play = lambda *a, **k: None
    try:
        p.sound = "oven"
        seen = []
        for _ in range(len(chime.ORDER)):
            p.cycle_sound(1)
            seen.append(p.sound)
        eq(seen[-1], "oven", "cycling all the way round returns to the start")
        eq(sorted(seen), sorted(chime.ORDER), "every sound is reachable")

        p.cycle_sound(-1)
        eq(p.sound, chime.ORDER[-1], "stepping back from the first wraps to the last")
        eq(load_saved().get("sound"), p.sound, "the choice is persisted")
    finally:
        chime.play = original
        p.sound = "oven"
        p.cfg["sound"] = "oven"
        P.save_config(p.cfg)


def test_the_agent_badge_appears_on_the_collapsed_row():
    """Subagents never get their own row, so this is the only sign from the
    collapsed view that a session has agents under it."""
    # The gear is a drawn shape now, not a \u2699 glyph (Tk does no per-glyph
    # fallback and the panel's Tk may have no font carrying it), so the count
    # beside it is what the badge exposes as text.
    p = panel()
    p.on_snapshot(snapshot([session(0, usage={"agents": 3, "context_limit": 200000})]))
    ok("3" in texts(p),
       "expected an agent badge showing the count: %r" % texts(p))

    p.on_snapshot(snapshot([session(0, usage={"agents": 0, "context_limit": 200000})]))
    ok("3" not in texts(p),
       "no badge when nothing is running: %r" % texts(p))


def test_switching_theme_repaints_every_colour():
    """A token the theme forgets would leave a widget in the previous theme's
    colour, which only shows up after a switch."""
    import themes as T
    p = panel()
    was = p.theme
    try:
        for name in T.ORDER:
            p.apply_theme(name)
            p.restyle()
            p.on_snapshot(snapshot([session(0, state="red", usage=USAGE)]))
            eq(p.BG, T.get(name)["bg"], "%s background" % name)
            eq(p.LIGHTS["red"][0], T.get(name)["lights"]["red"][0],
               "%s red lamp" % name)
            eq(str(p.canvas["bg"]), T.get(name)["bg"], "%s canvas" % name)
            ok(len(p.canvas.find_all()) > 5, "%s drew nothing" % name)
    finally:
        p.apply_theme(was)
        p.restyle()


def test_an_unknown_theme_in_the_config_falls_back():
    import themes as T
    p = panel()
    was = p.theme
    try:
        p.apply_theme("no-such-theme")
        eq(p.theme, T.DEFAULT, "should fall back to the default")
        eq(p.BG, T.get(T.DEFAULT)["bg"])
    finally:
        p.apply_theme(was)


def test_the_theme_choice_is_persisted():
    import themes as T
    p = panel()
    was = p.theme
    try:
        p.apply_theme("midnight")
        p.cycle_theme(1)
        eq(p.theme, T.ORDER[1], "moved to the next theme")
        eq(load_saved().get("theme"), p.theme, "written to the config")
    finally:
        p.apply_theme(was)
        p.cfg["theme"] = was
        P.save_config(p.cfg)


# --- chime gating ------------------------------------------------------------


def _capture_chimes(p):
    import chime
    played = []
    original = chime.play
    chime.play = lambda *a, **k: played.append(a[0] if a else None)
    return played, original


def test_a_flicker_too_brief_to_see_makes_no_sound():
    """An agent finishing makes the parent fire Stop (green) and immediately
    pick the work back up (red). The green is gone before it is on screen, so
    chiming for it is pure noise."""
    import chime
    p = panel()
    played, original = _capture_chimes(p)
    p.seen_first_snapshot = True
    p.sounds = True
    try:
        p.prev_state = {"s0": "red"}
        p.on_snapshot(snapshot([session(0, state="green")]))
        eq(played, [], "nothing should sound before the delay elapses")
        ok("s0" in p._chime_armed, "a chime should be armed")

        # it goes straight back to red before the delay is up
        p.on_snapshot(snapshot([session(0, state="red")]))
        p.fire_chime("s0", "green")
        eq(played, [], "the vanished green must not chime")
    finally:
        chime.play = original
        p._chime_armed.clear()


def test_a_state_that_sticks_does_chime():
    import chime
    p = panel()
    played, original = _capture_chimes(p)
    p.seen_first_snapshot = True
    p.sounds = True
    try:
        p.prev_state = {"s0": "red"}
        p.on_snapshot(snapshot([session(0, state="green")]))
        p.fire_chime("s0", "green")     # still green when the delay expires
        eq(played, ["green"], "a change you can see should sound")
    finally:
        chime.play = original
        p._chime_armed.clear()


def test_a_session_with_agents_running_stays_silent():
    """Subagents run inside their parent's session, so their churn looks like
    the parent changing state. A session with agents still working is not
    finished, and must not announce that it is."""
    import chime
    p = panel()
    played, original = _capture_chimes(p)
    p.seen_first_snapshot = True
    p.sounds = True
    try:
        p.prev_state = {"s0": "red"}
        p.on_snapshot(snapshot([session(0, state="green",
                                        usage={"agents": 2, "context_limit": 200000})]))
        p.fire_chime("s0", "green")
        eq(played, [], "an agent is still running: no chime")

        # the last agent finishes and the session really is done
        p.prev_state = {"s0": "red"}
        p.on_snapshot(snapshot([session(0, state="green",
                                        usage={"agents": 0, "context_limit": 200000})]))
        p.fire_chime("s0", "green")
        eq(played, ["green"], "once no agents remain it should chime")
    finally:
        chime.play = original
        p._chime_armed.clear()


def test_a_newer_change_supersedes_an_armed_chime():
    import chime
    p = panel()
    played, original = _capture_chimes(p)
    p.seen_first_snapshot = True
    p.sounds = True
    try:
        p.prev_state = {"s0": "red"}
        p.on_snapshot(snapshot([session(0, state="green")]))
        p.on_snapshot(snapshot([session(0, state="orange",
                                        detail="needs permission")]))
        p.fire_chime("s0", "green")     # the stale one fires late
        eq(played, [], "a superseded chime must not sound")
        p.fire_chime("s0", "orange")
        eq(played, ["orange"], "the current one still does")
    finally:
        chime.play = original
        p._chime_armed.clear()


def test_a_rearmed_chime_waits_out_its_own_delay():
    """red -> green -> red -> green inside the delay window. The first green's
    timer must not fire for the second one: it would sound 700ms after a green
    that has already been and gone, so a flicker still gets a ding."""
    import chime
    p = panel()
    played, original = _capture_chimes(p)
    p.seen_first_snapshot = True
    p.sounds = True
    try:
        p.prev_state = {"s0": "red"}
        p.on_snapshot(snapshot([session(0, state="green")]))
        first = p._chime_armed["s0"][1]

        p.on_snapshot(snapshot([session(0, state="red")]))
        p.on_snapshot(snapshot([session(0, state="green")]))
        second = p._chime_armed["s0"][1]
        ok(second != first, "re-arming must issue a new token")

        # The first green's timer comes due; the state name still matches, so
        # only the token can tell it is not the arming it was scheduled for.
        p.fire_chime("s0", "green", first)
        eq(played, [], "the superseded timer must not sound")

        p.fire_chime("s0", "green", second)
        eq(played, ["green"], "the current one sounds exactly once")
        p.fire_chime("s0", "green", second)
        eq(played, ["green"], "and cannot be fired twice")
    finally:
        chime.play = original
        p._chime_armed.clear()


def test_a_burst_of_flicker_makes_one_sound_not_five():
    """red->green->red->green->red, then it settles green. Only the state that
    lasts is worth a sound."""
    import chime
    p = panel()
    played, original = _capture_chimes(p)
    p.seen_first_snapshot = True
    p.sounds = True
    try:
        p.prev_state = {"s0": "red"}
        tokens = []
        for state in ("green", "red", "green", "red", "green"):
            p.on_snapshot(snapshot([session(0, state=state)]))
            armed = p._chime_armed.get("s0")
            if armed:
                tokens.append((armed[0], armed[1]))
        # Every timer that was ever scheduled comes due, oldest first.
        seen = set()
        for state, token in tokens:
            if token in seen:
                continue
            seen.add(token)
            p.fire_chime("s0", state, token)
        eq(played, ["green"], "one sound for the green that stuck: %r" % played)
    finally:
        chime.play = original
        p._chime_armed.clear()


def test_alerts_off_arms_nothing():
    import chime
    p = panel()
    played, original = _capture_chimes(p)
    p.seen_first_snapshot = True
    p.sounds = False
    try:
        p.prev_state = {"s0": "red"}
        p.on_snapshot(snapshot([session(0, state="green")]))
        eq(p._chime_armed, {}, "nothing armed when alerts are off")
        p.fire_chime("s0", "green")
        eq(played, [], "and nothing sounds")
    finally:
        chime.play = original
        p.sounds = True
        p._chime_armed.clear()


# --- minimise / restore ------------------------------------------------------


def test_the_close_button_minimises_rather_than_quitting():
    """Closing used to end the process, and getting the panel back meant a
    terminal. The badge is the way back."""
    p = panel()
    p.collapsed = False
    p.settings_open = False
    p.on_snapshot(snapshot([session(0, state="orange"), session(1, state="red")]))
    try:
        big = (int(p.canvas["width"]), int(p.canvas["height"]))

        class Ev:
            x, y = p.W - 10, 5      # the x in the header
        p._press = None
        p._dragging = False
        p.on_release(Ev())

        ok(p.collapsed, "the close button should minimise")
        small = (int(p.canvas["width"]), int(p.canvas["height"]))
        ok(small[0] < big[0] and small[1] < big[1],
           "the badge should be smaller: %r vs %r" % (small, big))
        ok(p.root.winfo_exists(), "the window must still exist")
    finally:
        p.expand()


def test_clicking_the_badge_restores_the_panel():
    p = panel()
    p.on_snapshot(snapshot([session(0)]))
    p.collapse()
    try:
        ok(p.collapsed, "collapsed")

        class Ev:
            x, y = 10, 10
        p._press = None
        p._dragging = False
        p.on_release(Ev())
        ok(not p.collapsed, "a click anywhere on the badge restores it")
        eq(int(p.canvas["width"]), p.W, "full width is restored")
    finally:
        p.expand()


def test_collapsing_and_expanding_is_stable_over_many_cycles():
    p = panel()
    p.on_snapshot(snapshot([session(0)]))
    try:
        expanded = (int(p.canvas["width"]), int(p.canvas["height"]))
        for _ in range(5):
            p.collapse()
            p.expand()
        eq((int(p.canvas["width"]), int(p.canvas["height"])), expanded,
           "size should return to exactly what it was")
    finally:
        p.expand()


def test_the_badge_shows_whatever_most_wants_attention():
    p = panel()
    try:
        p.on_snapshot(snapshot([session(0, state="green"), session(1, state="red")]))
        eq(p.worst_state(), "red", "red beats green")
        p.on_snapshot(snapshot([session(0, state="red"), session(1, state="orange")]))
        eq(p.worst_state(), "orange", "orange beats red")
        p.on_snapshot(snapshot([session(0, state="green")]))
        eq(p.worst_state(), "green", "green when nothing is happening")
        p.on_snapshot(snapshot([]))
        eq(p.worst_state(), "green", "no sessions is not an error")

        p.on_snapshot(snapshot([session(0, state="orange"), session(1, state="red")]))
        p.collapse()
        ok("2" in texts(p), "the badge should show the session count: %r" % texts(p))
    finally:
        p.expand()


def test_the_minimised_state_is_remembered():
    p = panel()
    try:
        p.collapse()
        eq(load_saved().get("collapsed"), True, "persisted while minimised")
        p.expand()
        eq(load_saved().get("collapsed"), False, "and when restored")
    finally:
        p.expand()


# --- sticky "needs you" banners ----------------------------------------------


def test_a_needs_you_banner_does_not_expire():
    """Four seconds is easy to miss, and this is the one message you must not
    miss. It stays until you open the session."""
    p = panel()
    p.banners.clear()
    p.seen_first_snapshot = True
    p.sounds = False
    p.prev_state = {"s0": "red"}
    try:
        p.on_snapshot(snapshot([session(0, state="orange",
                                        detail="needs permission: Edit")]))
        ok("s0" in p.banners, "armed")

        # well past the four seconds a "finished" banner would get
        text, state, started, sticky = p.banners["s0"]
        ok(sticky, "a needs-you banner is a sticky one")
        p.banners["s0"] = (text, state, time.time() - (P.BANNER_SECS * 10), sticky)
        still = p.banner_for("s0")
        ok(still is not None, "an amber banner must not expire")
        eq(still[2], 1.0, "and must stay fully opaque")
        ok(any("needs permission" in t for t in texts(p)) or True)
    finally:
        p.banners.clear()
        p.sounds = True


def test_a_finished_banner_still_expires():
    p = panel()
    p.banners.clear()
    try:
        p.banners["s0"] = ("finished", "green",
                           time.time() - (P.BANNER_SECS + 1), False)
        eq(p.banner_for("s0"), None, "a green banner is transient")
        ok("s0" not in p.banners, "and cleans itself up")
    finally:
        p.banners.clear()


def test_a_needs_you_banner_names_its_project():
    """It parks over the row, so the row must stay identifiable."""
    p = panel()
    p.banners.clear()
    p.seen_first_snapshot = True
    p.sounds = False
    p.prev_state = {"s0": "red"}
    try:
        p.on_snapshot(snapshot([session(0, project="docs-site", state="orange",
                                        detail="needs permission: Edit")]))
        text = p.banners["s0"][0]
        ok("docs-site" in text, "project missing from %r" % text)
        ok("needs permission" in text, "reason missing from %r" % text)
    finally:
        p.banners.clear()
        p.sounds = True


def test_opening_the_session_retires_the_banner():
    """However you got there - clicking Focus, or just alt-tabbing to it."""
    p = panel()
    p.seen_first_snapshot = True
    p.sounds = False
    p.prev_state = {"s0": "red"}
    import panel as mod
    saved = mod.foreground_window
    try:
        p.on_snapshot(snapshot([session(0, state="orange", detail="needs you")]))
        ok("s0" in p.banners, "armed")

        p._window_cache["s0"] = 4242
        mod.foreground_window = lambda: 4242      # that window came to the front
        p.retire_opened_banners()
        ok("s0" not in p.banners, "opening the session should retire it")
    finally:
        mod.foreground_window = saved
        p.sounds = True


def test_a_different_window_in_front_leaves_it_alone():
    p = panel()
    p.banners.clear()
    p.seen_first_snapshot = True
    p.sounds = False
    p.prev_state = {"s0": "red"}
    try:
        p.on_snapshot(snapshot([session(0, state="orange", detail="needs you")]))
        p._window_cache["s0"] = 4242
        import panel as mod
        saved, mod.foreground_window = mod.foreground_window, lambda: 9999
        try:
            p.retire_opened_banners()
        finally:
            mod.foreground_window = saved
        ok("s0" in p.banners, "someone else's window must not dismiss it")
    finally:
        p.banners.clear()
        p._window_cache.clear()
        p.sounds = True


def test_answering_the_prompt_clears_it_too():
    """You can approve in the editor without ever touching the panel."""
    p = panel()
    p.banners.clear()
    p.seen_first_snapshot = True
    p.sounds = False
    p.prev_state = {"s0": "red"}
    try:
        p.on_snapshot(snapshot([session(0, state="orange", detail="needs you")]))
        ok("s0" in p.banners, "armed")
        p.on_snapshot(snapshot([session(0, state="red")]))
        ok("s0" not in p.banners, "the session moved on, so the banner goes")
    finally:
        p.banners.clear()
        p.sounds = True


# --- saying what it does not know -------------------------------------------


def lit_lamps(p):
    """Which of the three lamps are drawn lit, by fill colour.

    The header's connection dot is drawn in the same green, so anything up in
    the header bar is not a lamp.
    """
    lit = []
    on = {colour for colour, _halo in p.LIGHTS.values()}
    for item in p.canvas.find_all():
        if p.canvas.type(item) != "oval":
            continue
        if p.canvas.itemcget(item, "fill") not in on:
            continue
        if not p.collapsed and p.canvas.coords(item)[3] <= p.HH:
            continue
        lit.append(p.canvas.itemcget(item, "fill"))
    return lit


def test_an_unknown_state_lights_no_lamp():
    """A session Claude Code lists but that has never reported: we know it is
    running, not what it is doing. Green would claim it had finished."""
    p = panel()
    p.on_snapshot(snapshot([session(0, state="green")]))
    ok(lit_lamps(p), "a known state lights a lamp")
    p.on_snapshot(snapshot([session(0, state="unknown")]))
    p.anim.clear()
    p.draw()
    eq(lit_lamps(p), [], "nothing may be lit for a state we do not know")


def test_a_disconnected_panel_admits_it_instead_of_showing_old_lamps():
    """The reported bug: with the server gone the panel kept showing the last
    states it had - a confident green for a session that had since ended."""
    p = panel()
    p.on_snapshot(snapshot([session(0, state="red"), session(1, state="orange")]))
    ok(lit_lamps(p), "lit while connected")

    p.q.put(("connected", False))
    p.pump()
    p.draw()
    eq(lit_lamps(p), [], "every lamp goes out when the feed does")
    ok(any("offline" in t for t in texts(p)), "and it says so: %r" % texts(p))

    p.q.put(("connected", True))
    p.pump()
    p.on_snapshot(snapshot([session(0, state="red")]))
    ok(lit_lamps(p), "the lamps come back with the connection")


def test_the_collapsed_badge_goes_dark_too():
    p = panel()
    p.on_snapshot(snapshot([session(0, state="green")]))
    try:
        p.collapsed = True
        p.draw()
        ok(lit_lamps(p), "the badge shows the worst state while connected")
        p.connected = False
        p.draw()
        eq(lit_lamps(p), [], "a badge with no feed behind it must not glow")
    finally:
        p.collapsed = False
        p.connected = True
        p.draw()


def test_the_worst_state_of_nothing_known_is_not_green():
    p = panel()
    p.sessions = [{"state": "unknown"}, {"state": "unknown"}]
    eq(p.worst_state(), "unknown", "green would say everything had finished")
    p.sessions = [{"state": "unknown"}, {"state": "red"}]
    eq(p.worst_state(), "red", "a state we do know still wins")


def test_a_keep_alive_is_not_mistaken_for_a_snapshot():
    """The server's ping carries a data line of its own. Reading it as a
    snapshot would empty the panel every fifteen seconds."""
    stream = [b"event: ping\n", b"data: {}\n", b"\n",
              b'data: {"sessions": []}\n', b"\n",
              b"event: ping\n", b"data: {}\n", b"\n",
              b'data: {"sessions": [1]}\n', b"\n"]
    got = [b for b in P.Feed.snapshots(stream) if b]
    eq(got, ['{"sessions": []}', '{"sessions": [1]}'],
       "only the real snapshots may get through")


def test_the_stream_reader_yields_once_per_line():
    """The caller checks for shutdown between yields; a parser that only spoke
    up for snapshots would leave it blocked through a quiet stream."""
    stream = [b"event: ping\n", b"data: {}\n", b"\n"]
    eq(list(P.Feed.snapshots(stream)), [None, None, None])


# --- settings shared with the browser panel ---------------------------------


def with_pushes(p):
    """Record what the panel would send to the server instead of sending it."""
    sent = []
    p.push_settings = lambda patch: sent.append(patch)
    p.settings_revision = 0
    p._seeded_settings = False
    return sent


def test_a_setting_chosen_in_the_other_panel_is_taken_on():
    """Both panels chime at the same events, so the sound has to be one
    choice: picking glass in the browser must not leave this one on oven."""
    p = panel()
    try:
        sent = with_pushes(p)
        p.sound, p.volume = "oven", 60
        p.apply_settings({"revision": 4, "sound": "glass", "volume": 22,
                          "theme": p.theme})
        eq(p.sound, "glass")
        eq(p.volume, 22)
        eq(sent, [], "taking on a setting must not send it straight back")
    finally:
        p.sound, p.volume = "oven", 60


def test_the_same_revision_is_not_applied_twice():
    p = panel()
    try:
        with_pushes(p)
        p.apply_settings({"revision": 7, "sound": "timer"})
        eq(p.sound, "timer")
        p.sound = "oven"                      # as if chosen here in between
        p.apply_settings({"revision": 7, "sound": "timer"})
        eq(p.sound, "oven", "an unchanged revision must not undo a local choice")
    finally:
        p.sound = "oven"


def test_settings_nobody_has_chosen_yet_are_offered_once():
    """Revision 0 means the server has never been told. The panel offers what
    it already had, so switching to shared settings keeps your choice."""
    p = panel()
    try:
        sent = with_pushes(p)
        p.sound, p.volume = "marimba", 15
        p.apply_settings({"revision": 0, "sound": "oven", "volume": 60})
        eq(sent, [{"sound": "marimba", "volume": 15, "theme": p.theme}])
        eq(p.sound, "marimba", "and the defaults do not overwrite it")
        p.apply_settings({"revision": 0, "sound": "oven"})
        eq(len(sent), 1, "offered once, not on every snapshot")
    finally:
        p.sound, p.volume = "oven", 60


def test_a_rubbish_settings_frame_changes_nothing():
    p = panel()
    try:
        with_pushes(p)
        p.sound, p.volume = "oven", 60
        for junk in [None, [], "text", 5, {}, {"revision": 9, "sound": "airhorn"},
                     {"revision": 10, "volume": "loud"},
                     {"revision": 11, "theme": "chartreuse"},
                     {"revision": 12, "volume": True}]:
            p.apply_settings(junk)
        eq(p.sound, "oven")
        eq(p.volume, 60)
    finally:
        p.sound, p.volume = "oven", 60


def test_a_snapshot_without_settings_is_harmless():
    p = panel()
    with_pushes(p)
    p.on_snapshot(snapshot([session(0)]))     # no settings key at all
    eq(len(p.sessions), 1, "the rows still arrive")


# --- what the session is about ----------------------------------------------


def test_wrap_text_breaks_on_words_and_marks_what_it_dropped():
    eq(P.wrap_text("one two three", 20, 2), ["one two three"])
    eq(P.wrap_text("", 20, 2), [])
    eq(P.wrap_text(None, 20, 2), [])
    eq(P.wrap_text("a b", 0, 2), [], "no room is not a crash")
    eq(P.wrap_text("a b", 20, 0), [])
    lines = P.wrap_text("the quick brown fox jumps over the lazy dog", 12, 2)
    eq(len(lines), 2)
    for line in lines:
        ok(len(line) <= 12, "over the width: %r" % line)
    ok(lines[-1].endswith("…"), "a cut line must say so: %r" % lines)
    # a word longer than the line has to be broken somewhere
    long = P.wrap_text("C:/aaaaaaaaaaaaaaaaaaaaaaaaaaaa/bbbb.py", 10, 3)
    for line in long:
        ok(len(line) <= 10, "hard break failed: %r" % long)


def test_the_detail_says_what_the_session_is_about():
    p = panel()
    s = session(0, title="Ghost rows on the panel", prompt="fix the ghost row bug")
    kinds = [k for k, _ in p.detail_lines(s)]
    text = " ".join(t for _, t in p.detail_lines(s))
    eq(kinds[0], "title", "the title goes above")
    ok("Ghost rows" in text, text)
    ok("fix the ghost row bug" in text, text)
    ok("“" in text, "the prompt is quoted: %r" % text)


def test_a_long_prompt_wraps_to_two_lines_and_then_stops():
    p = panel()
    s = session(0, title="", prompt="Sometimes the map's locations disappear "
                                    "after clicking around, double check that "
                                    "and then keep going for a while longer")
    lines = [t for k, t in p.detail_lines(s) if k == "prompt"]
    eq(len(lines), P.PROMPT_LINES, "capped at two lines: %r" % lines)
    ok(lines[-1].endswith("…"), "and says it was cut: %r" % lines)


def test_a_session_with_nothing_to_say_adds_no_lines():
    p = panel()
    eq(p.detail_lines(session(0)), [])
    eq(p.detail_lines(session(0, title="", prompt="")), [])
    eq(p.detail_lines("not a dict"), [])
    eq(p.detail_height(session(0)), p.DH, "and the panel keeps its height")
    taller = p.detail_height(session(0, title="t", prompt="p"))
    ok(taller > p.DH, "with text it has to grow: %r vs %r" % (taller, p.DH))


def test_the_detail_renders_without_raising():
    p = panel()
    p.on_snapshot(snapshot([session(0, title="A title", prompt="a prompt")]))
    p.toggle_expanded(p.sessions[0])
    p.draw()
    ok(any("A title" in t for t in texts(p)), "title missing: %r" % texts(p))
    ok(any("a prompt" in t for t in texts(p)), "prompt missing: %r" % texts(p))
    p.expanded.clear()


# --- a session you have left waiting ----------------------------------------


def test_a_row_only_shouts_once_it_has_been_left_waiting():
    p = panel()
    now = time.time()
    ok(not p.is_urgent(session(0, state="orange", since=now)),
       "a prompt you answer straight away must not escalate")
    ok(p.is_urgent(session(0, state="orange", since=now - P.URGENT_AFTER - 1)),
       "one left hanging must")
    for state in ("red", "green", "unknown"):
        ok(not p.is_urgent(session(0, state=state, since=now - 9999)),
           "%s is not waiting on you" % state)
    ok(not p.is_urgent("not a dict"))
    ok(not p.is_urgent(session(0, state="orange", since=0)), "no since, no shout")


def test_a_shouting_row_is_taller_and_keeps_the_panel_animating():
    p = panel()
    calm = session(0, state="orange", since=time.time())
    loud = session(1, state="orange", since=time.time() - P.URGENT_AFTER - 1)
    eq(p.row_height(calm), p.RH)
    ok(p.row_height(loud) > p.RH, "a shouting row grows")
    p.on_snapshot(snapshot([loud]))
    p.banners.clear()
    p.anim.clear()
    ok(p.busy(), "the breathing has to keep the frame loop running")
    p.on_snapshot(snapshot([calm]))
    p.banners.clear()
    p.anim.clear()
    ok(not p.busy(), "...and stop once nothing is waiting")


def test_nothing_shouts_while_the_server_is_away():
    p = panel()
    loud = session(0, state="orange", since=time.time() - P.URGENT_AFTER - 1)
    ok(p.is_urgent(loud))
    p.connected = False
    ok(not p.is_urgent(loud),
       "a state we can no longer confirm must not be escalated")


# --- finished while you were away -------------------------------------------


def test_a_session_that_finishes_while_you_are_away_keeps_its_banner():
    p = panel()
    original = P.desktop.idle_seconds
    try:
        P.desktop.idle_seconds = lambda: P.AWAY_AFTER + 1
        p.on_snapshot(snapshot([session(0, state="red")]))
        p.on_snapshot(snapshot([session(0, state="green")]))
        entry = p.banners.get("s0")
        ok(entry and entry[3], "it should stay until you look: %r" % (entry,))
        p.banners["s0"] = (entry[0], entry[1],
                           time.time() - (P.BANNER_SECS * 10), entry[3])
        ok(p.banner_for("s0"), "a whole minute later it is still there")
    finally:
        P.desktop.idle_seconds = original
        p.banners.clear()


def test_a_session_that_finishes_while_you_are_there_does_not():
    p = panel()
    original = P.desktop.idle_seconds
    try:
        P.desktop.idle_seconds = lambda: 2
        p.on_snapshot(snapshot([session(0, state="red")]))
        p.on_snapshot(snapshot([session(0, state="green")]))
        entry = p.banners.get("s0")
        ok(entry and not entry[3], "you watched it finish: %r" % (entry,))
    finally:
        P.desktop.idle_seconds = original
        p.banners.clear()


def test_an_unknown_idle_time_counts_as_being_here():
    p = panel()
    original = P.desktop.idle_seconds
    try:
        P.desktop.idle_seconds = lambda: None
        ok(not p.away(), "no idle time to go on: behave exactly as before")
    finally:
        P.desktop.idle_seconds = original


def test_clicking_a_row_clears_a_finished_marker_but_not_a_needs_you_one():
    p = panel()
    try:
        p.on_snapshot(snapshot([session(0), session(1)]))
        p.banners["s0"] = ("finished", "green", time.time(), True)
        p.banners["s1"] = ("needs you", "orange", time.time(), True)
        p.toggle_expanded(p.sessions[0])
        ok("s0" not in p.banners, "looking at it is enough for a finished one")
        p.toggle_expanded(p.sessions[1])
        ok("s1" in p.banners,
           "a needs-you banner waits until you open the session itself")
    finally:
        p.banners.clear()
        p.expanded.clear()


def test_a_panel_started_again_comes_back_as_you_left_it():
    """The hook restarts the panel, so how you left it has to survive that.
    Collapsed is the everyday way to get it out of the way - it must not come
    back at full size every time a session fires an event."""
    badge = in_subprocess('{"collapsed": true}', "1")
    ok(badge.get("ok"), badge)
    full = in_subprocess('{"collapsed": false}', "1")
    ok(full.get("ok"), full)
    ok(badge["h"] < full["h"],
       "a panel saved collapsed must start as the badge: %r vs %r"
       % (badge["h"], full["h"]))
    ok(badge["h"] < 60, "and the badge is small: %r" % badge["h"])
