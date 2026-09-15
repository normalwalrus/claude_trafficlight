"""Desktop integration tests.

Only the inert paths are exercised: nothing here steals focus from a real
window or plays audio at the user.
"""

import os

from harness import Skip, eq, ok

import chime
import desktop as D


def test_capabilities_reports_the_expected_shape():
    caps = D.capabilities()
    for key in ("platform", "focus", "audio", "focus_hint"):
        ok(key in caps, "missing capability key: " + key)
    ok(isinstance(caps["focus"], bool), "focus is a bool")
    ok(isinstance(caps["audio"], bool), "audio is a bool")


def test_focus_session_is_inert_for_junk_arguments():
    for pid, hint in ((0, 0), (None, 0), ("abc", 0), (-1, 0), (999999999, 0),
                      (0, 999999999), (None, None)):
        got = D.focus_session(pid, hint)
        ok(isinstance(got, tuple) and len(got) == 2,
           "focus_session(%r, %r) should return a pair" % (pid, hint))
        eq(got[0], False, "focus_session(%r, %r) should not claim success" % (pid, hint))


def test_window_title_is_inert_for_bad_handles():
    for handle in (0, None, -1):
        eq(D.window_title(handle), "", "title for %r" % handle)


def test_a_chime_is_written_once_and_reused():
    """Playback needs a file on every platform; it must not be rewritten each
    time a chime sounds."""
    data = chime._wav("green", "quiet")
    first = D._cached_wav("test-key", data)
    if first is None:
        raise Skip("no writable temp dir")
    second = D._cached_wav("test-key", data)
    eq(first, second, "same path reused")
    ok(os.path.exists(first), "file exists")
    with open(first, "rb") as fh:
        head = fh.read(12)
    eq(head[:4], b"RIFF", "RIFF header")
    eq(head[8:12], b"WAVE", "WAVE header")


def test_distinct_chimes_get_distinct_files():
    a = D._cached_wav("k-orange", chime._wav("orange", "quiet"))
    b = D._cached_wav("k-green", chime._wav("green", "quiet"))
    if a is None or b is None:
        raise Skip("no writable temp dir")
    ok(a != b, "different keys must not share a file")


def test_chime_play_reports_whether_it_actually_played():
    """The whole point of the rewrite: a silent failure used to be invisible."""
    got = chime.play("green", "quiet")
    ok(isinstance(got, bool), "play() returns a bool")
    if D.can_play():
        eq(got, True, "play() should succeed where audio is available")
