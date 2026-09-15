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
HOST_HOME = os.environ.get("HOST_HOME", "")
ENABLED = os.environ.get("TRAFFICLIGHT_INSTALL_HOOKS", "1") not in ("0", "false", "no")
PAYLOAD_SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hookpayload")

sys.path.insert(0, PAYLOAD_SOURCE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import install_hooks  # noqa: E402


def host_is_windows(home):
    """A host home of C:\\Users\\someone means we are writing for cmd.exe."""
    return bool(re.match(r"^[A-Za-z]:[\\/]", home or ""))


def host_join(*parts):
    sep = "\\" if host_is_windows(HOST_HOME) else "/"
    return sep.join(p.rstrip("/\\") for p in parts if p)


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

    rc = install_hooks.main(
        argv=[],
        command_base='"' + launcher + '"',
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
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # never stop the server from starting
        print("[bootstrap] hook install failed: " + repr(exc))
        sys.exit(0)
