"""Build one Panel with a given panel.json and report what it did.

Run as a subprocess by tests/test_panel.py so every config case starts from a
clean tkinter interpreter. Prints a single JSON line.

    python _panel_case.py '<panel.json contents>' <rows|pos>
"""

import json
import os
import sys
import tempfile
import time

os.environ["APPDATA"] = os.path.join(tempfile.gettempdir(), "clt-tests-appdata-sub")
os.environ["CLAUDE_LIGHT_URL"] = "http://127.0.0.1:9"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "panel"))

import panel as P  # noqa: E402


def main():
    config, mode = sys.argv[1], sys.argv[2]
    os.makedirs(P.CONFIG_DIR, exist_ok=True)
    with open(P.CONFIG_PATH, "w", encoding="utf-8") as fh:
        fh.write(config)

    out = {}
    try:
        p = P.Panel()
    except Exception as exc:
        print(json.dumps({"ok": False, "err": "%s: %s" % (type(exc).__name__, exc)}))
        return
    p.feed.stopped.set()
    p.root.attributes("-alpha", 0.0)
    p.connected = True  # the Feed is stopped; pretend the server is there

    def check():
        try:
            if mode.isdigit():
                now = time.time()
                rows = [{"session_id": "s%d" % i, "project": "proj%d" % i,
                         "state": "green", "since": now, "started": now,
                         "updated": now, "detail": "", "pid": 0, "hwnd": 0}
                        for i in range(int(mode))]
                p.on_snapshot(json.dumps({"now": now, "sessions": rows}))
            p.root.update_idletasks()
            geom = p.root.geometry()
            height = int(geom.split("x")[1].split("+")[0])
            out.update(
                ok=True, geom=geom, h=height,
                x=p.root.winfo_x(), y=p.root.winfo_y(),
                screen=[p.root.winfo_screenwidth(), p.root.winfo_screenheight()],
                offscreen=p.root.winfo_y() + height > p.root.winfo_screenheight(),
                texts=[p.canvas.itemcget(i, "text") for i in p.canvas.find_all()
                       if p.canvas.type(i) == "text"],
            )
        except Exception as exc:
            out.update(ok=False, err="%s: %s" % (type(exc).__name__, exc))
        p.root.after(5, p.quit)

    p.root.after(120, check)
    p.run()
    print(json.dumps(out))


if __name__ == "__main__":
    main()
