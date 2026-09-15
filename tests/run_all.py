"""Run the whole Claude Traffic Light test suite.

    python tests/run_all.py            # everything
    python tests/run_all.py hook panel # only the named modules

No third-party packages needed. The server tests skip themselves unless
fastapi/pydantic happen to be importable (they normally only live inside the
container); everything else runs on a bare Python 3.9+ install.

Nothing here touches ~/.claude/settings.json, the live server on port 8787, or
the running panel.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import harness  # noqa: E402

MODULES = ["server", "hook", "session_watch", "desktop_panel", "transcript",
           "chime", "winutil", "desktop", "themes", "panel", "focus",
           "install_hooks", "bootstrap"]


def main(argv):
    wanted = [a.lower() for a in argv[1:]] or MODULES
    unknown = [w for w in wanted if w not in MODULES]
    if unknown:
        print("unknown module(s): %s\navailable: %s"
              % (", ".join(unknown), ", ".join(MODULES)))
        return 2
    modules = [__import__("test_" + name) for name in wanted]
    return harness.run(modules)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
