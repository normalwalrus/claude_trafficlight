"""install_hooks.py tests.

CRITICAL: these never touch the real ~/.claude/settings.json. Every case runs
the script as a subprocess with HOME/USERPROFILE pointed at a throwaway
directory, and the last test asserts the real file was not modified.
"""

import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

from harness import eq, ok

import install_hooks

SCRIPT = install_hooks.__file__
PY = sys.executable
if PY.lower().endswith("pythonw.exe"):
    PY = PY[: -len("pythonw.exe")] + "python.exe"

SANDBOX = os.path.join(tempfile.gettempdir(), "clt-tests-homes")
MARKER = install_hooks.MARKER

_real = {}


def real_settings_digest():
    path = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
    if not os.path.exists(path):
        return None
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def home(name, content):
    """A throwaway HOME with (optionally) a settings.json in it."""
    _real.setdefault("digest", real_settings_digest())
    path = os.path.join(SANDBOX, name)
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(os.path.join(path, ".claude"))
    if content is not None:
        with open(os.path.join(path, ".claude", "settings.json"), "w",
                  encoding="utf-8") as fh:
            fh.write(content)
    return path


def run(path, *args):
    env = dict(os.environ)
    env["HOME"] = env["USERPROFILE"] = path
    env.pop("HOMEDRIVE", None)
    env.pop("HOMEPATH", None)
    proc = subprocess.run([PY, SCRIPT] + list(args), capture_output=True,
                          env=env, text=True, encoding="utf-8", errors="replace",
                          timeout=60)
    text = (proc.stdout or "") + (proc.stderr or "")
    ok("Traceback" not in text, "script crashed:\n" + text[-800:])
    return proc.returncode, text


def settings(path):
    f = os.path.join(path, ".claude", "settings.json")
    if not os.path.exists(f):
        return None
    with open(f, encoding="utf-8") as fh:
        return json.load(fh)


def our_hooks(data):
    found = []
    for event, groups in ((data or {}).get("hooks") or {}).items():
        for group in groups if isinstance(groups, list) else []:
            if not isinstance(group, dict):
                continue
            for entry in group.get("hooks") or []:
                if not isinstance(entry, dict):
                    continue
                if MARKER in str(entry.get("command", "")):
                    found.append(event)
    return sorted(found)


# --- installing -------------------------------------------------------------


def test_installs_into_a_machine_with_no_settings_file():
    path = home("fresh", None)
    rc, out = run(path)
    eq(rc, 0, out)
    eq(our_hooks(settings(path)), sorted(install_hooks.EVENTS))


def test_installs_into_an_empty_or_whitespace_file():
    for name, content in [("empty", ""), ("blank", "   \n\t  "), ("obj", "{}")]:
        path = home(name, content)
        rc, out = run(path)
        eq(rc, 0, out)
        eq(our_hooks(settings(path)), sorted(install_hooks.EVENTS), name)


def test_registers_a_matcher_for_pre_tool_use():
    path = home("matcher", "{}")
    run(path)
    groups = settings(path)["hooks"]["PreToolUse"]
    ok(any(g.get("matcher") == "*" for g in groups), "PreToolUse needs a matcher")
    ok("matcher" not in settings(path)["hooks"]["Stop"][0], "Stop needs no matcher")


def test_the_hook_command_is_runnable():
    path = home("cmd", "{}")
    run(path)
    command = settings(path)["hooks"]["Stop"][0]["hooks"][0]["command"]
    ok(command.startswith('"'), "interpreter path must be quoted: %s" % command)
    ok("pythonw" not in command.lower(), "pythonw has no stdin: %s" % command)
    exe, _, rest = command[1:].partition('"')
    ok(os.path.exists(exe), "interpreter %r does not exist" % exe)
    script = rest.strip().split('"')[1]
    ok(os.path.exists(script), "hook script %r does not exist" % script)


def test_unrelated_settings_and_hooks_are_preserved():
    original = {
        "model": "opus",
        "permissions": {"allow": ["Bash(ls:*)"]},
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}],
            "PreCompact": [{"hooks": [{"type": "command", "command": "my-thing"}]}],
        },
    }
    path = home("preserve", json.dumps(original))
    rc, out = run(path)
    eq(rc, 0, out)
    after = settings(path)
    eq(after["model"], "opus")
    eq(after["permissions"], original["permissions"])
    ok(any(e["command"] == "echo mine" for g in after["hooks"]["Stop"]
           for e in g["hooks"]), "the user's Stop hook was dropped")
    ok(any(e["command"] == "my-thing" for g in after["hooks"]["PreCompact"]
           for e in g["hooks"]), "the user's PreCompact hook was dropped")


def test_installing_repeatedly_does_not_duplicate_entries():
    path = home("idempotent", '{"hooks": {"Stop": [{"hooks": '
                              '[{"type": "command", "command": "echo mine"}]}]}}')
    for i in range(3):
        rc, out = run(path)
        eq(rc, 0, out)
        eq(our_hooks(settings(path)), sorted(install_hooks.EVENTS), "run %d" % (i + 1))
    text = json.dumps(settings(path))
    eq(text.count(MARKER), len(install_hooks.EVENTS))


def test_install_then_remove_restores_the_original():
    original = {"model": "opus", "hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}}
    path = home("roundtrip", json.dumps(original))
    run(path)
    rc, out = run(path, "--remove")
    eq(rc, 0, out)
    eq(settings(path), original, "remove did not restore the original")


def test_remove_cleans_up_events_it_created_entirely():
    path = home("rm_clean", "{}")
    run(path)
    rc, out = run(path, "--remove")
    eq(rc, 0, out)
    eq(settings(path), {}, "removal left debris: %r" % settings(path))


def test_remove_on_a_machine_with_no_settings_file_creates_nothing():
    path = home("rm_fresh", None)
    rc, out = run(path, "--remove")
    eq(rc, 0, out)
    eq(settings(path), None, "--remove must not conjure a settings.json")


def test_a_backup_is_written_before_any_change():
    path = home("backup", '{"model": "opus"}')
    run(path)
    backups = glob.glob(os.path.join(path, ".claude", "settings.json.bak-*"))
    eq(len(backups), 1, "expected exactly one backup, got %r" % backups)
    with open(backups[0], encoding="utf-8") as fh:
        eq(json.load(fh), {"model": "opus"}, "backup does not match the original")


# --- bad input --------------------------------------------------------------


def test_invalid_json_is_reported_and_the_file_is_left_alone():
    path = home("badjson", "{ this is not json")
    rc, out = run(path)
    eq(rc, 1)
    ok("not valid JSON" in out, out)
    with open(os.path.join(path, ".claude", "settings.json"), encoding="utf-8") as fh:
        eq(fh.read(), "{ this is not json", "the broken file was rewritten")


def test_a_non_object_settings_file_is_refused():
    for name, content in [("list", "[]"), ("string", '"hi"'), ("number", "5")]:
        path = home("top_" + name, content)
        rc, out = run(path)
        eq(rc, 1, name)
        ok("unexpected settings.json structure" in out, out)


def test_an_empty_or_null_hooks_value_is_replaced():
    for name, content in [("list", '{"hooks": []}'), ("null", '{"hooks": null}')]:
        path = home("hooks_empty_" + name, content)
        rc, out = run(path)
        eq(rc, 0, "%s: %s" % (name, out))
        eq(our_hooks(settings(path)), sorted(install_hooks.EVENTS), name)


def test_a_hooks_value_holding_real_data_is_refused_not_clobbered():
    """`hooks` must be an object; if it holds something else that isn't empty
    we cannot merge into it - and overwriting would destroy user data."""
    for name, content in [("list", '{"hooks": [{"x": 1}]}'),
                          ("string", '{"hooks": "nope"}')]:
        path = home("hooks_data_" + name, content)
        rc, out = run(path)
        eq(rc, 1, "%s: %s" % (name, out))
        ok("refusing to overwrite" in out, out)
        eq(settings(path), json.loads(content), "the file was modified anyway")


def test_oddly_shaped_hook_entries_do_not_crash_the_installer():
    weird = [
        '{"hooks": {"Stop": "oops"}}',
        '{"hooks": {"Stop": ["oops", 5]}}',
        '{"hooks": {"Stop": [{"hooks": "oops"}]}}',
        '{"hooks": {"Stop": [{"hooks": ["oops", null, 7]}]}}',
        '{"hooks": {"Stop": [null]}}',
        '{"hooks": {"Stop": []}}',
    ]
    for i, content in enumerate(weird):
        path = home("weird%d" % i, content)
        rc, out = run(path)
        eq(rc, 0, "%s -> %s" % (content, out))
        eq(our_hooks(settings(path)), sorted(install_hooks.EVENTS), content)
        rc, out = run(path, "--remove")
        eq(rc, 0, "%s (remove) -> %s" % (content, out))


def test_remove_only_strips_entries_that_are_ours():
    path = home("selective", json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "echo keep-me"},
        {"type": "command", "command": "python " + MARKER + " Stop"},
    ]}]}}))
    rc, out = run(path, "--remove")
    eq(rc, 0, out)
    commands = [e["command"] for g in settings(path)["hooks"]["Stop"]
                for e in g["hooks"]]
    eq(commands, ["echo keep-me"])


# --- the guard rail ---------------------------------------------------------


def test_zz_the_real_user_settings_file_was_never_touched():
    before = _real.get("digest", real_settings_digest())
    eq(real_settings_digest(), before,
       "~/.claude/settings.json CHANGED while the tests ran")
