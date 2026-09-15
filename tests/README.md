# Tests

```powershell
python tests\run_all.py                 # everything
python tests\run_all.py hook panel      # just those modules
```

No dependencies, no setup. Modules: `server`, `hook`, `chime`, `winutil`,
`panel`, `install_hooks`.

Expect `0 failed`. Skips are normal — a test skips when it cannot apply here
(see below), never because it failed.

## The server tests need FastAPI

`tests/test_server.py` imports `server/app.py`, which needs `fastapi` and
`pydantic`. Those normally only exist inside the container, so on a bare host
all 17 server tests **skip**. To actually run them:

```powershell
python -m venv .venv
.venv\Scripts\pip install -r server\requirements.txt
.venv\Scripts\python tests\run_all.py
```

They start their own uvicorn on an ephemeral port — never the live one on 8787.

## What these tests will not do

* touch `~/.claude/settings.json` — `install_hooks.py` is always run as a
  subprocess with `HOME`/`USERPROFILE` redirected into `%TEMP%`, and the last
  test in that module hashes the real file to prove it is unchanged
* touch the live server on port 8787, or the running panel process
* focus, move or close any window that the tests did not create

`test_panel.py` does build real tkinter windows, but they are created fully
transparent and destroyed immediately.

## Layout

| file | what it covers |
| --- | --- |
| `run_all.py` | entry point |
| `harness.py` | ~50-line runner: collects `test_*`, prints a pass/fail summary |
| `test_server.py` | state machine, `since` semantics, reaper, SSE fan-out and cleanup, backed-up subscribers, hostile payloads, concurrency |
| `test_hook.py` | the exit-0 contract (bad stdin, no server, 500s, a server that hangs), payload shape, the hwnd/pid cache |
| `test_chime.py` | WAV validity, amplitude per level, clipping, onset, caching |
| `test_winutil.py` | process/window enumeration, the explorer.exe stop rule, junk handles |
| `test_panel.py` | rendering, malformed snapshots, alert suppression, geometry, config |
| `test_install_hooks.py` | fresh/empty/corrupt settings, preserving other hooks, idempotency, `--remove` |
| `_panel_case.py` | helper: builds one Panel with a given `panel.json` in a subprocess |

Panel cases that need a different starting config run in a subprocess —
rebuilding tkinter roots in one interpreter leaves stray `after` callbacks
behind and the failures are misleading.
