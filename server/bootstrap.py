"""Install the host-side hooks from inside the container.

Runs once at container start, before uvicorn. `docker compose up` is then the
only command needed: the container copies the hook payload into the mounted
Claude home and registers it in settings.json, so the host needs nothing
installed and the repo can live anywhere (or be deleted afterwards).

Requires the bind mount of the host's ~/.claude. Without it this does nothing
but print why - the server still starts and the browser panel still works,
there just won't be any sessions to show.

Disable with TRAFFICLIGHT_INSTALL_HOOKS=0.
"""

import os
import re
import sys

HOST_CLAUDE = os.environ.get("HOST_CLAUDE_DIR", "/host-claude")
_RAW_HOST_HOME = os.environ.get("HOST_HOME", "")
ENABLED = os.environ.get("TRAFFICLIGHT_INSTALL_HOOKS", "1") not in ("0", "false", "no")
# Nothing in here can draw a window, so the always-on-top panel has to run on
# the host. The hooks are the only thing this project runs there, so they are
# what starts it - all the container does is put the panel where they can find
# it. Set TRAFFICLIGHT_DESKTOP_PANEL=0 to keep the browser panel only.
DESKTOP_PANEL = os.environ.get("TRAFFICLIGHT_DESKTOP_PANEL", "1") not in (
    "0", "false", "no")
PAYLOAD_SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hookpayload")
PANEL_SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "panelpayload")

sys.path.insert(0, PAYLOAD_SOURCE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import install_hooks  # noqa: E402


def normalise_host_home(home):
    """Put HOST_HOME into the form the host itself uses.

    A POSIX shell on Windows (Git Bash, MSYS, WSL-style paths) reports HOME as
    `/c/Users/name`. Taken literally that reads as a POSIX host, so we would
    write a `hook.sh` for a machine that runs cmd.exe, and put a path into
    settings.json that Claude Code cannot execute. Either mistake means the
    lights simply never come on, with nothing on screen to say why.
    """
    home = (home or "").strip().replace("\\", "/").rstrip("/")
    match = re.match(r"^/([A-Za-z])/(.+)$", home)
    if match:
        home = match.group(1).upper() + ":/" + match.group(2)
    if re.match(r"^[A-Za-z]:/", home):
        # Settle on one separator: host_join adds backslashes for a Windows
        # host, and mixing the two produces paths that are legal but horrible
        # to read in settings.json.
        return home.replace("/", "\\")
    return home


HOST_HOME = normalise_host_home(_RAW_HOST_HOME)


def host_is_windows(home):
    """A host home of C:\\Users\\someone means we are writing for cmd.exe."""
    return bool(re.match(r"^[A-Za-z]:[\\/]", home or ""))


def host_join(*parts):
    sep = "\\" if host_is_windows(HOST_HOME) else "/"
    return sep.join(p.rstrip("/\\") for p in parts if p)


def cached_interpreter(dest_dir):
    """The interpreter path the launcher resolved on its first run, if any."""
    try:
        with open(os.path.join(dest_dir, "interpreter.txt"), encoding="utf-8") as fh:
            value = fh.read().strip().strip('"')
    except OSError:
        return ""
    # It is a host path, so we cannot stat it; sanity-check the shape instead.
    if not value or "\n" in value or len(value) > 500:
        return ""
    low = value.lower()
    if not (low.endswith("python.exe") or low.endswith("python3")
            or low.endswith("python")):
        return ""
    return value


def match_owner(path, reference):
    """Give written files the same owner as the mounted directory.

    On Linux the container is root, so anything it creates in the user's home
    would end up root-owned and unwritable by them afterwards. No-op elsewhere.
    """
    if not hasattr(os, "chown"):
        return
    try:
        st = os.stat(reference)
        if not os.path.isdir(path):
            os.chown(path, st.st_uid, st.st_gid)
            return
        for root, dirs, files in os.walk(path):
            for name in dirs + files:
                try:
                    os.chown(os.path.join(root, name), st.st_uid, st.st_gid)
                except OSError:
                    pass
        os.chown(path, st.st_uid, st.st_gid)
    except OSError:
        pass


def main():
    if not ENABLED:
        print("[bootstrap] TRAFFICLIGHT_INSTALL_HOOKS=0, skipping hook install")
        return 0

    if not os.path.isdir(HOST_CLAUDE):
        print("[bootstrap] " + HOST_CLAUDE + " is not mounted - hooks NOT installed.")
        print("[bootstrap] The panel will have no sessions to show. Mount the")
        print("[bootstrap] host's ~/.claude (docker-compose.yml does this).")
        return 0

    if not HOST_HOME:
        print("[bootstrap] HOST_HOME is unset - cannot write host-absolute hook")
        print("[bootstrap] paths into settings.json. Hooks NOT installed.")
        return 0

    windows = host_is_windows(HOST_HOME)
    host_payload = host_join(HOST_HOME, ".claude", install_hooks.PAYLOAD_DIRNAME)
    dest = os.path.join(HOST_CLAUDE, install_hooks.PAYLOAD_DIRNAME)

    # Point the installer at the mount instead of the container's own home.
    install_hooks.SETTINGS = os.path.join(HOST_CLAUDE, "settings.json")
    install_hooks.PAYLOAD_DIR = dest
    install_hooks.HOOK = os.path.join(dest, install_hooks.HOOK_NAME)

    launcher = install_hooks.stage_payload(
        source_dir=PAYLOAD_SOURCE,
        dest_dir=dest,
        host_dir=host_payload,
        windows=windows,
    )
    staged_panel = install_hooks.stage_panel(PANEL_SOURCE, dest, DESKTOP_PANEL)

    # The launcher caches the interpreter it resolved on its first run, and we
    # can see that file through the mount. Once it exists we can invoke Python
    # directly and drop the shell from the chain - worth ~25ms on every hook,
    # which is latency the user sees as the light lagging behind Claude.
    command_base = '"' + launcher + '"'
    interpreter = cached_interpreter(dest)
    if interpreter:
        hook = host_join(host_payload, install_hooks.HOOK_NAME)
        command_base = '"' + interpreter + '" -S "' + hook + '"'
        print("[bootstrap] using cached interpreter: " + interpreter)

    rc = install_hooks.main(
        argv=[],
        command_base=command_base,
        source_dir=PAYLOAD_SOURCE,
    )
    match_owner(dest, HOST_CLAUDE)
    try:
        match_owner(install_hooks.SETTINGS, HOST_CLAUDE)
    except Exception:
        pass

    if rc == 0:
        print("[bootstrap] host: " + ("windows" if windows else "posix")
              + "  payload: " + host_payload)
        if staged_panel:
            print("[bootstrap] desktop panel staged - it will start with your "
                  "next Claude session (needs Python with tkinter on the host)")
        else:
            print("[bootstrap] desktop panel off (TRAFFICLIGHT_DESKTOP_PANEL=0)")
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # never stop the server from starting
        print("[bootstrap] hook install failed: " + repr(exc))
        sys.exit(0)
