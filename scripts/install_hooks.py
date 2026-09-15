"""Install / remove the Claude Traffic Light hooks in ~/.claude/settings.json.

Idempotent: re-running replaces our entries and leaves any other hooks alone.

    python install_hooks.py            # install (or refresh) globally
    python install_hooks.py --remove   # remove
"""

import argparse
import json
import os
import shutil
import sys
import time

MARKER = "claude_light_hook.py"

# A command is ours if it mentions any of these. Two installers write these
# entries - the host one invokes the .py directly, the container one goes
# through a generated launcher - and each must recognise the other's work, or
# installing both would double every hook and --remove would orphan half.
MARKERS = (
    MARKER,
    "claude-trafficlight/hook.",
    "claude-trafficlight\\hook.",
)


def is_ours(command):
    text = str(command or "")
    return any(m in text for m in MARKERS)

# Which hook events drive the light. PostToolUse/SubagentStop are deliberately
# left out: the session is already red and every hook costs a process spawn.
EVENTS = [
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "Notification",
    "Stop",
    "SessionEnd",
]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE_DIR = os.path.join(ROOT, "hooks")
CLAUDE_HOME = os.path.join(os.path.expanduser("~"), ".claude")
SETTINGS = os.path.join(CLAUDE_HOME, "settings.json")

# The hook payload is *copied* next to settings.json rather than referenced in
# the repo. Hook commands are absolute paths, so pointing them at a clone would
# break the lights the moment the repo is moved, renamed or deleted - and the
# container installs the same payload without knowing where the host repo is.
PAYLOAD_DIRNAME = "claude-trafficlight"
PAYLOAD_FILES = ("claude_light_hook.py", "transcript.py")
HOOK_NAME = "claude_light_hook.py"

PAYLOAD_DIR = os.path.join(CLAUDE_HOME, PAYLOAD_DIRNAME)
HOOK = os.path.join(PAYLOAD_DIR, HOOK_NAME)

LAUNCHER_CMD = """@echo off
REM Claude Traffic Light hook launcher. Generated - do not edit.
REM
REM The interpreter is resolved once and cached: a `where` lookup costs ~50ms
REM and this runs on every hook, which is latency the user sees as the light
REM lagging behind Claude. Falls back to curl so the lights still work on a
REM machine with no Python at all (the server parses the payload instead).
setlocal
set "DIR=%~dp0"
set "SCRIPT=%DIR%claude_light_hook.py"
set "CACHE=%DIR%interpreter.txt"
if "%CLAUDE_LIGHT_URL%"=="" set "CLAUDE_LIGHT_URL=http://127.0.0.1:8787"

set "PY="
if exist "%CACHE%" set /p PY=<"%CACHE%"
if defined PY if exist "%PY%" goto run

REM One-time resolution. python.exe first: going through the py launcher costs
REM an extra process spawn. The findstr filter drops the Microsoft Store alias
REM stub, which opens the Store instead of running anything.
for /f "delims=" %%P in ('where python.exe 2^>nul ^| findstr /v /i "WindowsApps"') do if not defined PY set "PY=%%P"
if not defined PY for /f "delims=" %%P in ('where python3.exe 2^>nul ^| findstr /v /i "WindowsApps"') do if not defined PY set "PY=%%P"
if not defined PY for /f "delims=" %%P in ('where py.exe 2^>nul') do if not defined PY set "PY=%%P"
if not defined PY goto curlfallback
>"%CACHE%" echo %PY%

:run
"%PY%" "%SCRIPT%" %*
exit /b 0

:curlfallback
where curl >nul 2>&1 && curl -s -m 2 -X POST -H "Content-Type: application/json" --data-binary @- "%CLAUDE_LIGHT_URL%/hook/%~1" >nul 2>&1
exit /b 0
"""

LAUNCHER_SH = """#!/bin/sh
# Claude Traffic Light hook launcher. Generated - do not edit.
# Tries any Python on PATH, then falls back to curl so the lights still work
# on a machine with no Python at all (the server parses the payload instead).
dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
: "${CLAUDE_LIGHT_URL:=http://127.0.0.1:8787}"
for py in python3 python; do
    if command -v "$py" >/dev/null 2>&1; then
        exec "$py" "$dir/claude_light_hook.py" "$@"
    fi
done
if command -v curl >/dev/null 2>&1; then
    curl -s -m 2 -X POST -H "Content-Type: application/json" \
         --data-binary @- "$CLAUDE_LIGHT_URL/hook/$1" >/dev/null 2>&1
fi
exit 0
"""


def stage_payload(source_dir=None, dest_dir=None, host_dir=None, windows=None):
    """Copy the hook payload beside settings.json and write the launchers.

    `dest_dir` is where this process writes; `host_dir` is the same directory
    as the *host* sees it (they differ when the container does the install).
    Returns the launcher path in host terms.
    """
    source_dir = source_dir or SOURCE_DIR
    dest_dir = dest_dir or PAYLOAD_DIR
    host_dir = host_dir or dest_dir
    if windows is None:
        windows = os.name == "nt"

    os.makedirs(dest_dir, exist_ok=True)
    for name in PAYLOAD_FILES:
        src = os.path.join(source_dir, name)
        if os.path.exists(src):
            shutil.copyfile(src, os.path.join(dest_dir, name))

    name = "hook.cmd" if windows else "hook.sh"
    body = LAUNCHER_CMD if windows else LAUNCHER_SH
    launcher = os.path.join(dest_dir, name)
    # cmd.exe wants CRLF, sh wants LF. Pin both explicitly rather than
    # inheriting the writing platform's default: the container runs Linux
    # but may well be writing a .cmd file for a Windows host.
    newline = "\r\n" if windows else "\n"
    with open(launcher, "w", encoding="utf-8", newline=newline) as fh:
        fh.write(body)
    if not windows:
        os.chmod(launcher, 0o755)

    sep = "\\" if windows else "/"
    return host_dir.rstrip("/\\") + sep + name


def python_exe():
    exe = sys.executable or "python"
    # pythonw has no usable stdin; hooks need it.
    if exe.lower().endswith("pythonw.exe"):
        exe = exe[: -len("pythonw.exe")] + "python.exe"
    return exe


def load_settings():
    if not os.path.exists(SETTINGS):
        return {}
    with open(SETTINGS, "r", encoding="utf-8") as fh:
        text = fh.read().strip()
    if not text:
        return {}
    return json.loads(text)


def backup():
    if os.path.exists(SETTINGS):
        dest = SETTINGS + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(SETTINGS, dest)
        return dest
    return None


def strip_ours(settings):
    """Drop every hook entry we previously installed."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings
    for event in list(hooks.keys()):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        kept_groups = []
        for group in groups:
            if not isinstance(group, dict):
                kept_groups.append(group)
                continue
            inner = group.get("hooks")
            if not isinstance(inner, list):
                kept_groups.append(group)
                continue
            kept = [
                h
                for h in inner
                if not isinstance(h, dict) or not is_ours(h.get("command"))
            ]
            if kept:
                group = dict(group)
                group["hooks"] = kept
                kept_groups.append(group)
        if kept_groups:
            hooks[event] = kept_groups
        else:
            hooks.pop(event, None)
    if not hooks:
        settings.pop("hooks", None)
    return settings


def add_ours(settings, command_base=None):
    if command_base is None:
        # On the host we know the exact interpreter, which beats the launcher's
        # PATH search. The container has no such knowledge and passes its own.
        command_base = '"' + python_exe() + '" "' + HOOK + '"'
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        # An empty list or null is just noise we can replace; anything else
        # is content we refuse to silently throw away (checked in main()).
        hooks = {}
        settings["hooks"] = hooks
    for event in EVENTS:
        entry = {
            "hooks": [
                {
                    "type": "command",
                    "command": command_base + " " + event,
                    "timeout": 5,
                }
            ]
        }
        if event in ("PreToolUse",):
            entry["matcher"] = "*"
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            groups = []
            hooks[event] = groups
        groups.append(entry)
    return settings


def main(argv=None, command_base=None, source_dir=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--remove", action="store_true")
    args = ap.parse_args(argv)

    if not args.remove:
        src = source_dir or SOURCE_DIR
        if not os.path.exists(os.path.join(src, HOOK_NAME)):
            print("ERROR: hook payload not found in " + src)
            return 1

    try:
        settings = load_settings()
    except json.JSONDecodeError as exc:
        print("ERROR: " + SETTINGS + " is not valid JSON (" + str(exc) + ").")
        print("Fix or move that file, then re-run.")
        return 1

    if not isinstance(settings, dict):
        print("ERROR: unexpected settings.json structure.")
        return 1

    hooks = settings.get("hooks")
    if hooks and not isinstance(hooks, dict):
        print('ERROR: "hooks" in ' + SETTINGS + " is a " + type(hooks).__name__)
        print("       but Claude Code expects an object keyed by event name.")
        print("       Fix that by hand first - refusing to overwrite it.")
        return 1

    if not os.path.exists(SETTINGS) and args.remove:
        print("Nothing to remove: " + SETTINGS + " does not exist.")
        return 0

    b = backup()
    if b:
        print("Backed up settings to " + b)

    settings = strip_ours(settings)
    if not args.remove:
        if command_base is None:
            # Host-side install. A caller that supplies its own command has
            # already staged the payload (and knows which launcher the *host*
            # needs, which this process cannot infer from its own os.name).
            stage_payload(source_dir=source_dir)
        settings = add_ours(settings, command_base)

    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    with open(SETTINGS, "w", encoding="utf-8") as fh:
        json.dump(settings, fh, indent=2)
        fh.write("\n")

    action = "Removed" if args.remove else "Installed"
    print(action + " traffic light hooks in " + SETTINGS)
    if not args.remove:
        print("Payload: " + PAYLOAD_DIR)
        print("Events: " + ", ".join(EVENTS))
        print("Restart any running Claude Code sessions to pick them up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
