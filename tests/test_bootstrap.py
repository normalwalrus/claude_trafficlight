"""Container-side install tests.

Covers the two pieces that let `docker compose up` do the whole job: staging
the hook payload into the Claude home (for a host whose OS the writer may not
share), and translating host transcript paths onto the bind mount.
"""

import os
import tempfile

from harness import Skip, eq, ok

import install_hooks as I


def _staging_dir(name):
    d = os.path.join(tempfile.mkdtemp(prefix="clt-stage-"), name)
    return d


def test_staging_writes_a_cmd_launcher_for_a_windows_host():
    dest = _staging_dir("win")
    launcher = I.stage_payload(
        source_dir=I.SOURCE_DIR,
        dest_dir=dest,
        host_dir="C:\\Users\\someone\\.claude\\claude-trafficlight",
        windows=True,
    )
    eq(launcher,
       "C:\\Users\\someone\\.claude\\claude-trafficlight\\hook.cmd",
       "host-style launcher path")
    ok(os.path.exists(os.path.join(dest, "hook.cmd")), "hook.cmd written")
    ok(not os.path.exists(os.path.join(dest, "hook.sh")),
       "a windows host must not also get hook.sh")


def test_staging_writes_an_sh_launcher_for_a_posix_host():
    dest = _staging_dir("posix")
    launcher = I.stage_payload(
        source_dir=I.SOURCE_DIR,
        dest_dir=dest,
        host_dir="/home/someone/.claude/claude-trafficlight",
        windows=False,
    )
    eq(launcher, "/home/someone/.claude/claude-trafficlight/hook.sh",
       "host-style launcher path")
    ok(os.path.exists(os.path.join(dest, "hook.sh")), "hook.sh written")
    ok(not os.path.exists(os.path.join(dest, "hook.cmd")),
       "a posix host must not also get hook.cmd")


def test_launchers_get_the_line_endings_their_shell_needs():
    """The container writes Windows files from Linux, so this cannot be left
    to the platform default: cmd.exe mis-parses LF-only batch files."""
    win = _staging_dir("crlf")
    I.stage_payload(I.SOURCE_DIR, win, "C:\\x", windows=True)
    with open(os.path.join(win, "hook.cmd"), "rb") as fh:
        data = fh.read()
    ok(b"\r\n" in data, "cmd launcher must use CRLF")

    posix = _staging_dir("lf")
    I.stage_payload(I.SOURCE_DIR, posix, "/x", windows=False)
    with open(os.path.join(posix, "hook.sh"), "rb") as fh:
        data = fh.read()
    ok(b"\r\n" not in data, "sh launcher must not contain CRLF")
    ok(data.startswith(b"#!/bin/sh"), "shebang first")


def test_staging_copies_the_whole_payload():
    dest = _staging_dir("payload")
    I.stage_payload(I.SOURCE_DIR, dest, dest, windows=False)
    for name in I.PAYLOAD_FILES:
        path = os.path.join(dest, name)
        ok(os.path.exists(path), "missing from payload: " + name)
        ok(os.path.getsize(path) > 0, "empty payload file: " + name)


def test_staging_is_repeatable():
    dest = _staging_dir("twice")
    first = I.stage_payload(I.SOURCE_DIR, dest, dest, windows=True)
    second = I.stage_payload(I.SOURCE_DIR, dest, dest, windows=True)
    eq(first, second, "same launcher path")
    eq(sorted(os.listdir(dest)),
       sorted(list(I.PAYLOAD_FILES) + ["hook.cmd"]),
       "no accumulating files")


def test_the_launcher_falls_back_to_curl():
    """A host with no Python at all must still light up."""
    ok("curl" in I.LAUNCHER_CMD, "cmd launcher needs a curl fallback")
    ok("curl" in I.LAUNCHER_SH, "sh launcher needs a curl fallback")
    for body in (I.LAUNCHER_CMD, I.LAUNCHER_SH):
        ok("/hook/" in body, "fallback must post to the server-side endpoint")
    ok("exec" in I.LAUNCHER_SH, "sh launcher should exec python, not fork it")


# --- host path translation ---------------------------------------------------


def _app():
    os.environ["HOST_HOME"] = "C:\\Users\\someone"
    os.environ["HOST_CLAUDE_DIR"] = os.environ.get("CLT_FAKE_MOUNT", "/nonexistent")
    try:
        import app
    except Exception as exc:
        raise Skip("fastapi not importable here: %s" % exc)
    app.HOST_HOME = "C:\\Users\\someone"
    return app


def test_paths_outside_the_mount_are_refused():
    app = _app()
    mount = tempfile.mkdtemp(prefix="clt-mount-")
    app.HOST_CLAUDE = mount
    for bad in (
        "C:\\Windows\\System32\\config\\SAM",
        "C:\\Users\\someone\\.ssh\\id_rsa",
        "C:\\Users\\someone\\.claude\\..\\..\\secret",
        "/etc/passwd",
        "",
        None,
    ):
        eq(app.host_path_to_container(bad), "", "must refuse %r" % bad)


def test_a_real_transcript_path_resolves_onto_the_mount():
    app = _app()
    mount = tempfile.mkdtemp(prefix="clt-mount-")
    app.HOST_CLAUDE = mount
    inner = os.path.join(mount, "projects", "proj")
    os.makedirs(inner)
    target = os.path.join(inner, "s.jsonl")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write("{}\n")

    got = app.host_path_to_container(
        "C:\\Users\\someone\\.claude\\projects\\proj\\s.jsonl"
    )
    eq(os.path.normpath(got), os.path.normpath(target), "resolved onto the mount")


def test_a_path_that_looks_right_but_is_absent_is_refused():
    app = _app()
    app.HOST_CLAUDE = tempfile.mkdtemp(prefix="clt-mount-")
    eq(app.host_path_to_container(
        "C:\\Users\\someone\\.claude\\projects\\nope\\missing.jsonl"), "",
       "a non-existent file must not be returned")


# --- the two installers must recognise each other ----------------------------


def test_launcher_entries_are_recognised_as_ours():
    """Regression: the container registers hook.cmd / hook.sh, not the .py.

    While is_ours() only knew the .py name, every `docker compose up` appended
    another six entries instead of replacing them - hooks fired three times per
    event after three restarts - and --remove left the launcher entries behind.
    """
    ok(I.is_ours(r'"C:\Users\me\.claude\claude-trafficlight\hook.cmd" Stop'),
       "windows launcher must be recognised")
    ok(I.is_ours('"/home/me/.claude/claude-trafficlight/hook.sh" Stop'),
       "posix launcher must be recognised")
    ok(I.is_ours('"/usr/bin/python" "/home/me/.claude/claude-trafficlight/'
                 'claude_light_hook.py" Stop'),
       "direct python invocation must be recognised")

    for other in ("", None, "echo hello", "bash ~/.claude/statusline-command.sh",
                  r'"C:\tools\some-other-hook.cmd" Stop'):
        ok(not I.is_ours(other), "must not claim %r" % other)


def test_installing_twice_through_different_launchers_does_not_duplicate():
    import json

    home = tempfile.mkdtemp(prefix="clt-both-")
    settings = os.path.join(home, "settings.json")
    saved_settings, saved_payload, saved_hook = I.SETTINGS, I.PAYLOAD_DIR, I.HOOK
    I.SETTINGS = settings
    I.PAYLOAD_DIR = os.path.join(home, I.PAYLOAD_DIRNAME)
    I.HOOK = os.path.join(I.PAYLOAD_DIR, I.HOOK_NAME)
    try:
        def count():
            with open(settings, encoding="utf-8") as fh:
                data = json.load(fh)
            return sum(1 for e in data.get("hooks", {}).values() for g in e
                       for h in g.get("hooks", []) if I.is_ours(h.get("command")))

        # container-style: a generated launcher
        launcher = I.stage_payload(I.SOURCE_DIR, I.PAYLOAD_DIR, I.PAYLOAD_DIR,
                                   windows=True)
        I.main([], command_base='"' + launcher + '"')
        eq(count(), len(I.EVENTS), "one entry per event after the first install")

        # same again - a second `docker compose up`
        I.main([], command_base='"' + launcher + '"')
        eq(count(), len(I.EVENTS), "re-running the container install must replace")

        # host-style: direct python invocation
        I.main([])
        eq(count(), len(I.EVENTS), "a host install must replace, not duplicate")

        I.main(["--remove"])
        eq(count(), 0, "--remove must clear entries from either installer")
    finally:
        I.SETTINGS, I.PAYLOAD_DIR, I.HOOK = saved_settings, saved_payload, saved_hook


# --- host path shape ---------------------------------------------------------


def _bootstrap():
    import bootstrap
    return bootstrap


def test_a_posix_shell_on_windows_is_still_a_windows_host():
    """Git Bash and MSYS report HOME as /c/Users/name. Read literally that is a
    POSIX host, so we would generate a hook.sh for a machine running cmd.exe
    and write a path Claude Code cannot execute - the lights would just never
    come on, with nothing on screen to say why."""
    B = _bootstrap()
    eq(B.normalise_host_home("/c/Users/someone"), r"C:\Users\someone")
    eq(B.normalise_host_home("/d/work/dev"), r"D:\work\dev")
    ok(B.host_is_windows(B.normalise_host_home("/c/Users/someone")),
       "must be recognised as a Windows host")


def test_windows_paths_settle_on_one_separator():
    B = _bootstrap()
    eq(B.normalise_host_home(r"C:\Users\someone"), r"C:\Users\someone")
    eq(B.normalise_host_home("C:/Users/someone"), r"C:\Users\someone")
    eq(B.normalise_host_home("C:\\Users\\someone\\"), r"C:\Users\someone",
       "a trailing separator is dropped")


def test_real_posix_homes_are_left_alone():
    B = _bootstrap()
    for home in ("/home/dev", "/Users/dev", "/root", "/var/lib/someone"):
        eq(B.normalise_host_home(home), home, home)
        ok(not B.host_is_windows(B.normalise_host_home(home)),
           "%s is a posix host" % home)


def test_junk_does_not_raise():
    B = _bootstrap()
    for junk in ("", None, "   ", "/", "//", "relative/path"):
        got = B.normalise_host_home(junk)
        ok(isinstance(got, str), "normalise_host_home(%r) -> %r" % (junk, got))
