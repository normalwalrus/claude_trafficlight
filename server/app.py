"""Status server for Claude Traffic Light.

Claude Code hooks POST lifecycle events here; the desktop panel subscribes to
/stream and re-renders whenever the session table changes.
"""

import asyncio
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Set

from fastapi import FastAPI, Request
from fastapi.responses import (HTMLResponse, JSONResponse, Response,
                               StreamingResponse)
from pydantic import BaseModel

# A session that has not reported anything for this long is assumed dead
# (terminal killed, machine slept, claude crashed) and is dropped.
STALE_SECONDS = int(os.environ.get("STALE_SECONDS", "900"))
REAP_INTERVAL = 30
HEARTBEAT_INTERVAL = 15

# hook event -> traffic light state. None means "remove this session".
EVENT_STATE: Dict[str, Optional[str]] = {
    "SessionStart": "green",
    "UserPromptSubmit": "red",
    "PreToolUse": "red",
    "PostToolUse": "red",
    "SubagentStop": "red",  # a subagent finished, but the main turn is still running
    "Notification": "orange",
    "Stop": "green",
    "SessionEnd": None,
}

app = FastAPI(title="Claude Traffic Light")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# The host's ~/.claude, bind-mounted. Lets the server read transcripts itself
# when the host has no Python for the full hook to run under.
HOST_CLAUDE = os.environ.get("HOST_CLAUDE_DIR", "/host-claude")
HOST_HOME = os.environ.get("HOST_HOME", "")

# Claude Code keeps its own registry of running sessions in ~/.claude/sessions.
# We mount that, so liveness does not have to be guessed from hook traffic.
# An entry older than this is treated as a leftover from a killed session.
REGISTRY_TTL = int(os.environ.get("REGISTRY_TTL", "3600"))

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "hookpayload"))
try:
    import transcript as _transcript
except Exception:
    _transcript = None


def live_session_ids() -> Optional[Set[str]]:
    """Session ids Claude Code itself currently lists as running.

    Returns None when the registry cannot be read (no mount), so callers know
    to fall back to the inactivity timeout rather than reaping everything.
    """
    folder = os.path.join(HOST_CLAUDE, "sessions")
    try:
        names = os.listdir(folder)
    except OSError:
        return None

    live: Set[str] = set()
    cutoff = time.time() - REGISTRY_TTL
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(folder, name), "r", encoding="utf-8") as fh:
                entry = json.load(fh)
        except Exception:
            continue
        if not isinstance(entry, dict):
            continue
        sid = entry.get("sessionId")
        if not sid:
            continue
        stamp = entry.get("updatedAt")
        # updatedAt is milliseconds. A file with no stamp still counts as live:
        # better to keep a row too long than to drop a session that is open.
        if isinstance(stamp, (int, float)) and stamp / 1000.0 < cutoff:
            continue
        live.add(str(sid))
    return live


def host_path_to_container(path: str) -> str:
    r"""Translate a host transcript path onto the mount.

    Hook payloads carry host paths (C:\Users\me\.claude\projects\... or
    /home/me/.claude/projects/...). Only paths inside the mounted Claude home
    are accepted; anything else is refused rather than read.
    """
    if not path or not HOST_HOME:
        return ""
    norm = path.replace("\\", "/")
    root = HOST_HOME.replace("\\", "/").rstrip("/") + "/.claude/"
    if not norm.lower().startswith(root.lower()):
        return ""
    relative = norm[len(root):]
    if ".." in relative.split("/"):
        return ""
    candidate = os.path.join(HOST_CLAUDE, *relative.split("/"))
    return candidate if os.path.exists(candidate) else ""


def usage_from_transcript(session_id: str, host_transcript: str):
    """Token accounting done server-side, for the curl fallback hook."""
    if _transcript is None:
        return None
    local = host_path_to_container(host_transcript)
    if not local:
        return None
    try:
        return _transcript.scan(local, "srv-" + session_id)
    except Exception:
        return None


class Event(BaseModel):
    session_id: str
    event: str
    cwd: Optional[str] = None
    project: Optional[str] = None
    pid: Optional[int] = None
    hwnd: Optional[int] = None
    detail: Optional[str] = None
    tool: Optional[str] = None
    # Token accounting, measured host-side by the hook: the transcript lives on
    # the host filesystem and this server runs in a container.
    usage: Optional[Dict[str, Any]] = None


class Store:
    def __init__(self) -> None:
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self.subscribers: Set[asyncio.Queue] = set()
        self.revision = 0

    def snapshot(self) -> Dict[str, Any]:
        now = time.time()
        rows = sorted(self.sessions.values(), key=lambda s: s["started"])
        return {
            "type": "snapshot",
            "now": now,
            "revision": self.revision,
            "sessions": rows,
        }

    def publish(self) -> None:
        self.revision += 1
        payload = json.dumps(self.snapshot())
        for q in list(self.subscribers):
            # Every message is a complete snapshot, so when a slow subscriber
            # has backed up the newest one is the only one worth keeping: drop
            # from the front rather than discarding the update we just made.
            while True:
                try:
                    q.put_nowait(payload)
                    break
                except asyncio.QueueFull:
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        break

    def apply(self, ev: Event) -> None:
        now = time.time()
        state = EVENT_STATE.get(ev.event, "red")

        if state is None:
            self.sessions.pop(ev.session_id, None)
            self.publish()
            return

        s = self.sessions.get(ev.session_id)
        if s is None:
            s = {
                "session_id": ev.session_id,
                "project": ev.project or "claude",
                "cwd": ev.cwd or "",
                "pid": ev.pid,
                "hwnd": ev.hwnd,
                "state": state,
                "since": now,
                "started": now,
                "updated": now,
                "detail": ev.detail or "",
                "tool": ev.tool or "",
                "usage": ev.usage or {},
                "last_event": ev.event,
            }
            self.sessions[ev.session_id] = s
        else:
            # Late-arriving identity details win over the placeholders.
            if ev.project:
                s["project"] = ev.project
            if ev.cwd:
                s["cwd"] = ev.cwd
            if ev.pid:
                s["pid"] = ev.pid
            if ev.hwnd:
                s["hwnd"] = ev.hwnd
            if s["state"] != state:
                s["state"] = state
                s["since"] = now
            s["updated"] = now
            s["detail"] = ev.detail or ""
            s["last_event"] = ev.event
            # A hook that could not read the transcript sends nothing rather
            # than zeroes; keep the last good reading instead of blanking it.
            if ev.tool is not None:
                s["tool"] = ev.tool
            if ev.usage:
                s["usage"] = ev.usage

        self.publish()

    def reap(self) -> None:
        """Drop sessions that are really gone.

        Hook traffic alone is not a liveness signal: a session you left open in
        your editor fires nothing at all between turns, so the inactivity
        timeout on its own would quietly delete a row that is plainly still
        running. Claude Code's own session registry is the authority; the
        timeout is only the fallback for when it cannot be read.
        """
        live = live_session_ids()
        cutoff = time.time() - STALE_SECONDS
        dead = []
        for sid, s in self.sessions.items():
            if s["updated"] >= cutoff:
                continue
            if live is not None and sid in live:
                continue  # open, just idle
            dead.append(sid)
        for sid in dead:
            self.sessions.pop(sid, None)
        if dead:
            self.publish()


store = Store()


@app.on_event("startup")
async def _start_reaper() -> None:
    async def loop() -> None:
        while True:
            await asyncio.sleep(REAP_INTERVAL)
            store.reap()

    asyncio.create_task(loop())


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """The browser panel - the one UI that needs nothing installed on the host."""
    path = os.path.join(STATIC_DIR, "index.html")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return HTMLResponse(fh.read())
    except OSError:
        return HTMLResponse(
            "<h1>Claude Traffic Light</h1><p>static/index.html is missing "
            "from the image.</p>",
            status_code=500,
        )


@app.get("/sounds.json")
async def sounds_json() -> Response:
    """The alert-sound definitions, exported from panel/chime.py at build time
    so the browser and the desktop panel play the same six sounds."""
    path = os.path.join(STATIC_DIR, "sounds.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return Response(fh.read(), media_type="application/json")
    except OSError:
        return Response('{"order": [], "sounds": {}, "voices": {}}',
                        media_type="application/json", status_code=404)


@app.get("/healthz")
async def healthz() -> Dict[str, Any]:
    return {"ok": True, "sessions": len(store.sessions), "stale_seconds": STALE_SECONDS}


@app.post("/event")
async def post_event(ev: Event) -> Dict[str, Any]:
    store.apply(ev)
    return {"ok": True, "state": store.sessions.get(ev.session_id, {}).get("state")}


@app.post("/hook/{event}")
async def post_hook(event: str, request: Request) -> Dict[str, Any]:
    """Raw Claude Code hook payload, posted by the curl fallback launcher.

    The full hook does this parsing host-side (and adds the window handle it
    can only get from there). This endpoint exists so a machine with no Python
    still lights up - the server has one, and can reach the transcripts through
    the same mount it installs the hooks into.
    """
    raw = await request.body()
    try:
        payload = json.loads(raw.decode("utf-8", "replace")) if raw else {}
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    event = str(payload.get("hook_event_name") or event or "Unknown")[:60]
    session_id = str(payload.get("session_id") or "")[:200]
    if not session_id:
        return {"ok": False, "error": "no session_id"}

    cwd = str(payload.get("cwd") or "")
    project = os.path.basename(cwd.replace("\\", "/").rstrip("/")) or "claude"

    tool = ""
    detail = ""
    if event == "Notification":
        msg = str(payload.get("message") or "")
        if msg.startswith("Claude "):
            msg = msg[len("Claude "):]
        msg = msg.replace("needs your permission to use ", "needs permission: ")
        msg = msg.replace("is waiting for your input", "waiting for input")
        detail = msg[:120]
    elif event == "PreToolUse":
        tool = str(payload.get("tool_name") or "")[:40]
        detail = tool

    usage = None
    if event != "SessionEnd":
        usage = usage_from_transcript(
            session_id, str(payload.get("transcript_path") or "")
        )

    store.apply(Event(
        session_id=session_id,
        event=event,
        cwd=cwd,
        project=project,
        detail=detail,
        tool=tool,
        usage=usage,
    ))
    return {"ok": True, "state": store.sessions.get(session_id, {}).get("state")}


@app.get("/sessions")
async def get_sessions() -> JSONResponse:
    return JSONResponse(store.snapshot())


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str) -> Dict[str, Any]:
    existed = store.sessions.pop(session_id, None) is not None
    if existed:
        store.publish()
    return {"ok": True, "removed": existed}


@app.delete("/sessions")
async def clear_sessions() -> Dict[str, Any]:
    n = len(store.sessions)
    store.sessions.clear()
    store.publish()
    return {"ok": True, "removed": n}


@app.get("/stream")
async def stream() -> StreamingResponse:
    q: asyncio.Queue = asyncio.Queue(maxsize=64)
    store.subscribers.add(q)

    async def gen():
        try:
            yield f"data: {json.dumps(store.snapshot())}\n\n"
            while True:
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=HEARTBEAT_INTERVAL)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield f"data: {payload}\n\n"
        finally:
            store.subscribers.discard(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
