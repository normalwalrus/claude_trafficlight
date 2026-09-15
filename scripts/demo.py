"""Drive the panel with fake sessions so you can see it without running Claude.

    python scripts/demo.py

Ctrl+C removes the fake sessions.
"""

import json
import os
import sys
import time
import urllib.request

SERVER = os.environ.get("CLAUDE_LIGHT_URL", "http://127.0.0.1:8787").rstrip("/")

SESSIONS = [
    ("demo-1", "my-api"),
    ("demo-2", "claude_trafficlight"),
    ("demo-3", "docs-site"),
]

SCRIPT = [
    ("demo-1", "SessionStart", ""),
    ("demo-2", "SessionStart", ""),
    ("demo-3", "SessionStart", ""),
    ("demo-1", "UserPromptSubmit", ""),
    ("demo-2", "UserPromptSubmit", ""),
    ("demo-1", "PreToolUse", "Bash"),
    ("demo-3", "UserPromptSubmit", ""),
    ("demo-2", "Notification", "needs permission to edit"),
    ("demo-1", "Stop", ""),
    ("demo-3", "Notification", "waiting for your input"),
    ("demo-2", "Stop", ""),
    ("demo-3", "Stop", ""),
]


def send(session_id, event, detail):
    project = dict(SESSIONS)[session_id]
    body = {
        "session_id": session_id,
        "event": event,
        "project": project,
        "cwd": "C:\\demo\\" + project,
        "detail": detail,
    }
    req = urllib.request.Request(
        SERVER + "/event",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=3).read()
    print(session_id + "  " + event + ("  " + detail if detail else ""))


def cleanup():
    for sid, _ in SESSIONS:
        try:
            urllib.request.urlopen(
                urllib.request.Request(SERVER + "/sessions/" + sid, method="DELETE"),
                timeout=3,
            ).read()
        except Exception:
            pass
    print("removed demo sessions")


def main():
    try:
        urllib.request.urlopen(SERVER + "/healthz", timeout=3).read()
    except Exception:
        print("Server not reachable at " + SERVER + " - run: docker compose up -d")
        return 1

    try:
        for sid, event, detail in SCRIPT:
            send(sid, event, detail)
            time.sleep(2.5)
        print("\nDemo finished - sessions left on screen. Ctrl+C to clear.")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
