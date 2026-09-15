"""Claude Traffic Light - always-on-top desktop panel.

One row per live Claude Code session:
    red    = working        orange = waiting on you        green = done

Talks to the dockerised status server over SSE (/stream) and reconnects
automatically if the server restarts. Standard library only.
"""

import json
import math
import os
import queue
import sys
import threading
import time
import tkinter as tk
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chime  # noqa: E402
from winutil import focus_session, window_title  # noqa: E402

SERVER = os.environ.get("CLAUDE_LIGHT_URL", "http://127.0.0.1:8787").rstrip("/")

CONFIG_DIR = os.path.join(
    os.environ.get("APPDATA") or os.path.expanduser("~"), "claude-trafficlight"
)
CONFIG_PATH = os.path.join(CONFIG_DIR, "panel.json")

WIDTH = 320
HEADER_H = 24
ROW_H = 34
DETAIL_H = 112        # height added to a row while its detail is open
LABEL_CHARS = 30      # roughly what fits between the lights and the timer
PROJECT_CHARS = 20    # the project name is never sacrificed to the detail text
MIN_VISIBLE = 48      # px of the panel that must stay on screen
SCREEN_MARGIN = 80    # leave this much of the desktop free below the panel

BG = "#15171c"
BG_HOVER = "#1d212a"
BG_FLASH = "#2a3040"
BORDER = "#2b3038"
FG = "#e6e9ef"
FG_DIM = "#7f8798"
FG_HEADER = "#69707f"

OFF = "#282c35"
LIGHTS = {
    "red": ("#ff5964", "#59222a"),
    "orange": ("#ffb03a", "#5a3f15"),
    "green": ("#3ddc84", "#175130"),
}
ORDER = ("red", "orange", "green")

# The signal head: a dark housing with three bezelled lamps, laid out left to
# right so a row stays 34px tall.
CASE_X0, CASE_X1 = 8, 70
CASE_H = 24
CASE_BG = "#0c0e12"
CASE_EDGE = "#353d4a"
BEZEL = "#2b323d"
LAMP_OFF = "#191d24"
LAMP_R = 6
LAMP_X0 = 19
LAMP_GAP = 20
TEXT_X = 80

FADE_SECS = 0.25     # cross-fade from the old lamp to the new one
PULSE_SECS = 0.60    # one halo swell on the lamp that just lit
FRAME_MS = 40        # ~25fps, and only while something is actually moving

BANNER_SECS = 4.0
BANNER_FADE = 0.35

# Settings panel, at 100% scale.
SETTINGS_H = 108
MIN_SCALE = 0.75
MAX_SCALE = 1.50

BAR_BG = "#22262f"
BAR_OK = "#3ddc84"
BAR_WARN = "#ffb03a"
BAR_FULL = "#ff5964"
BTN_BG = "#232833"
BTN_HOVER = "#2e3543"


def mix(colour_a, colour_b, t):
    """Blend two #rrggbb colours. Tk canvas items have no alpha channel, so
    every fade here is done by mixing against the colour behind."""
    t = max(0.0, min(1.0, t))
    a = colour_a.lstrip("#")
    b = colour_b.lstrip("#")
    out = []
    for i in (0, 2, 4):
        va = int(a[i:i + 2], 16)
        vb = int(b[i:i + 2], 16)
        out.append(int(round(va + (vb - va) * t)))
    return "#%02x%02x%02x" % tuple(out)


def round_rect(x0, y0, x1, y1, r):
    """Point list for a rounded rectangle, drawn with create_polygon(smooth=True).

    Tk has no rounded-rectangle primitive; doubling the corner points and
    letting the spline smooth them is the standard way to fake one.
    """
    return [
        x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
        x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
        x0, y1, x0, y1 - r, x0, y0 + r, x0, y0,
    ]


def fmt_tokens(n):
    """Compact token counts: 284875 -> 285k, 21881535 -> 21.9M."""
    n = max(0, as_int(n))
    if n >= 1_000_000:
        text = "{:.1f}M".format(n / 1_000_000)
        return text.replace(".0M", "M")
    if n >= 1_000:
        return "{:.0f}k".format(n / 1_000)
    return str(n)


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_config(cfg):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
    except Exception:
        pass


def as_float(value, default=0.0):
    """Config and snapshot values arrive from disk and from the network; a
    surprising type must never take the panel down."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def fmt_elapsed(seconds):
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return str(seconds // 3600) + ":" + format((seconds % 3600) // 60, "02d") + "h"
    return str(seconds // 60) + ":" + format(seconds % 60, "02d")


class Feed(threading.Thread):
    """Background SSE subscriber with auto-reconnect."""

    def __init__(self, out):
        super().__init__(daemon=True)
        self.out = out
        self.stopped = threading.Event()

    def run(self):
        while not self.stopped.is_set():
            try:
                req = urllib.request.Request(
                    SERVER + "/stream", headers={"Accept": "text/event-stream"}
                )
                with urllib.request.urlopen(req, timeout=40) as resp:
                    self.out.put(("connected", True))
                    for raw in resp:
                        if self.stopped.is_set():
                            return
                        line = raw.decode("utf-8", "replace").strip()
                        if line.startswith("data:"):
                            body = line[5:].strip()
                            if body:
                                self.out.put(("snapshot", body))
            except Exception:
                pass
            self.out.put(("connected", False))
            # Retry quickly so a restarted server is picked up without a wait.
            for _ in range(10):
                if self.stopped.is_set():
                    return
                time.sleep(0.2)


class Panel:
    # --- scaling ------------------------------------------------------------

    def rescale(self):
        """Recompute every dimension from self.scale.

        Geometry lives on the instance rather than at module level precisely so
        this can change at runtime without restarting the panel.
        """
        s = self.scale
        self.W = int(WIDTH * s)
        self.HH = max(18, int(HEADER_H * s))
        self.RH = max(22, int(ROW_H * s))
        self.DH = max(70, int(DETAIL_H * s))
        self.CX0 = int(CASE_X0 * s)
        self.CX1 = int(CASE_X1 * s)
        self.CH = max(14, int(CASE_H * s))
        self.LR = max(3, int(round(LAMP_R * s)))
        self.LX0 = int(LAMP_X0 * s)
        self.LG = int(LAMP_GAP * s)
        self.TX = int(TEXT_X * s)
        self.MV = int(MIN_VISIBLE * s)
        self.SETTINGS_H = int(SETTINGS_H * s)

    def f(self, size, style=None):
        """A scaled UI font. Tk needs a real point size, so this rounds."""
        pt = max(6, int(round(size * self.scale)))
        return ("Segoe UI", pt) if style is None else ("Segoe UI", pt, style)

    def fm(self, size):
        return ("Consolas", max(6, int(round(size * self.scale))))

    def set_scale(self, value):
        self.scale = min(MAX_SCALE, max(MIN_SCALE, value))
        self.rescale()
        self.cfg["scale"] = round(self.scale, 3)
        save_config(self.cfg)
        self.draw()

    def __init__(self):
        self.cfg = load_config()
        self.sessions = []
        self.prev_state = {}
        self.clock_offset = 0.0
        self.connected = False
        self.seen_first_snapshot = False
        self.flash_until = {}
        self.hover_index = -1
        self.row_hitboxes = []
        self._press = None
        self._dragging = False
        self._toast = None
        self.expanded = set()
        self.button_hitboxes = []
        self._hover_button = None
        self.anim = {}          # sid -> (from_state, to_state, started)
        self.banners = {}       # sid -> (text, state, started)
        self.prev_since = {}    # sid -> when the previous state began
        self._frame_job = None
        self.settings_open = False
        self.slider_hitboxes = []
        self._drag_slider = None

        self.scale = min(MAX_SCALE, max(MIN_SCALE,
                                        as_float(self.cfg.get("scale", 1.0), 1.0)))
        self.sound = str(self.cfg.get("sound", chime.DEFAULT))
        if self.sound not in chime.ORDER:
            self.sound = chime.DEFAULT
        self.rescale()

        self.root = tk.Tk()
        self.root.title("Claude Traffic Light")
        self.root.overrideredirect(True)
        self.root.configure(bg=BG)
        self.root.attributes("-topmost", bool(self.cfg.get("topmost", True)))
        self.alpha_idle = min(1.0, max(0.2, as_float(self.cfg.get("alpha", 0.93), 0.93)))
        self.root.attributes("-alpha", self.alpha_idle)
        self.sounds = bool(self.cfg.get("sounds", True))
        # Accepts the old "quiet"/"normal"/"loud" strings from an existing
        # config and converts them to the 0-100 scale.
        self.volume = chime.as_volume(self.cfg.get("volume", chime.DEFAULT_VOLUME))

        self.canvas = tk.Canvas(
            self.root,
            width=self.W,
            height=self.HH + self.RH,
            bg=BG,
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(fill="both", expand=True)

        self.menu = tk.Menu(
            self.root,
            tearoff=0,
            bg="#1c2029",
            fg=FG,
            activebackground="#2b3140",
            activeforeground=FG,
            bd=0,
            relief="flat",
        )
        self.var_top = tk.BooleanVar(value=bool(self.cfg.get("topmost", True)))
        self.var_snd = tk.BooleanVar(value=self.sounds)
        self.menu.add_checkbutton(
            label="Always on top", variable=self.var_top, command=self.toggle_topmost
        )
        self.menu.add_checkbutton(
            label="Alerts (sound + flash)", variable=self.var_snd, command=self.toggle_sounds
        )
        self.menu.add_command(label="Test sound", command=self.test_chime)
        self.menu.add_separator()
        self.menu.add_command(label="Settings…", command=self.toggle_settings)
        self.menu.add_command(label="Clear all sessions", command=self.clear_sessions)
        self.menu.add_command(label="Reset position", command=self.reset_position)
        self.menu.add_separator()
        self.menu.add_command(label="Quit", command=self.quit)

        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<Double-Button-1>", self.on_double_click)
        self.canvas.bind("<ButtonRelease-2>", self.on_middle_click)
        self.canvas.bind("<Button-3>", self.on_right_click)
        self.canvas.bind("<Motion>", self.on_motion)
        self.canvas.bind("<Leave>", self.on_leave)
        self.canvas.bind("<Enter>", self.on_enter)

        self.place_window()

        self.q = queue.Queue()
        self.feed = Feed(self.q)
        self.feed.start()

        self.pump()
        self.tick()

    # --- window placement ---------------------------------------------------

    def default_pos(self):
        return self.root.winfo_screenwidth() - self.W - 24, 48

    def clamp(self, x, y):
        """Keep the window reachable.

        The panel is overrideredirect: no title bar, no taskbar entry. A
        position saved on a monitor that has since been unplugged would leave
        it permanently invisible with no way to drag it back.
        """
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        x = min(max(x, self.MV - self.W), sw - self.MV)
        y = min(max(y, 0), max(0, sh - self.HH))
        return x, y

    def place_window(self):
        pos = self.cfg.get("pos")
        h = self.HH + self.RH
        x = y = None
        if isinstance(pos, (list, tuple)) and len(pos) == 2:
            try:
                x, y = int(pos[0]), int(pos[1])
            except (TypeError, ValueError):
                x = y = None
        if x is None:
            x, y = self.default_pos()
        x, y = self.clamp(x, y)
        self.root.geometry(str(self.W) + "x" + str(h) + "+" + str(x) + "+" + str(y))

    def reset_position(self):
        self.cfg.pop("pos", None)
        save_config(self.cfg)
        self.place_window()

    def visible_layout(self):
        """Which sessions fit on screen, and how tall the panel must be.

        Rows are no longer a fixed height - an expanded one carries its detail
        panel - so the cap is computed in pixels. Without it the window just
        keeps growing past the bottom of the screen, and there is no title bar
        or scrollbar to recover with.
        """
        room = self.root.winfo_screenheight() - self.root.winfo_y() - SCREEN_MARGIN
        avail = max(self.RH, room - self.HH - 4)
        items, used = [], 0
        for s in self.sessions:
            expanded = s.get("session_id") in self.expanded
            h = self.RH + (self.DH if expanded else 0)
            if items and used + h > avail:
                break
            items.append((s, expanded))
            used += h
        hidden = len(self.sessions) - len(items)
        if hidden:
            used += self.RH
        return items, hidden, used

    def resize(self, body_height):
        h = self.HH + max(self.RH, body_height) + 4
        self.canvas.config(height=h)
        self.root.geometry(str(self.W) + "x" + str(h))
        return h

    # --- server feed --------------------------------------------------------

    def pump(self):
        # The reschedule lives in `finally`: if anything in here ever raises,
        # tkinter would swallow the traceback into a pythonw process with no
        # console and the panel would silently freeze for good.
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "connected":
                    self.connected = bool(payload)
                    if not self.connected:
                        self.seen_first_snapshot = False
                elif kind == "snapshot":
                    self.on_snapshot(payload)
        except queue.Empty:
            pass
        except Exception:
            pass
        finally:
            self.root.after(80, self.pump)

    def on_snapshot(self, body):
        try:
            data = json.loads(body)
        except Exception:
            return
        if not isinstance(data, dict):
            return
        self.clock_offset = as_float(data.get("now"), time.time()) - time.time()
        sessions = data.get("sessions")
        if not isinstance(sessions, list):
            sessions = []
        sessions = [s for s in sessions if isinstance(s, dict)]

        for s in sessions:
            sid = s.get("session_id")
            state = s.get("state")
            was = self.prev_state.get(sid)
            changed = self.seen_first_snapshot and was and was != state
            if changed:
                # Every change cross-fades the lamps, including into red.
                self.anim[sid] = (was, state, time.time())
                # Whatever the last banner said is now out of date: a row that
                # has moved on must not keep claiming it needs permission.
                self.banners.pop(sid, None)
                self.flash_until.pop(sid, None)
            # Only announce a real transition, and never on the first snapshot
            # after (re)connecting - otherwise a restart pings for everything.
            if changed and state in ("orange", "green"):
                self.banners[sid] = (
                    self.banner_text(s, was, state), state, time.time())
                self.flash_until[sid] = time.time() + BANNER_SECS
                if self.sounds:
                    chime.play(state, self.volume, self.sound)
            self.prev_state[sid] = state
            self.prev_since[sid] = as_float(s.get("since"), time.time())

        live = set(s.get("session_id") for s in sessions)
        for sid in list(self.prev_state):
            if sid not in live:
                self.prev_state.pop(sid, None)
                self.flash_until.pop(sid, None)
                self.prev_since.pop(sid, None)
                self.anim.pop(sid, None)
                self.banners.pop(sid, None)
        self.expanded &= live

        self.sessions = sessions
        self.seen_first_snapshot = True
        self.draw()
        self.start_frames()

    def banner_text(self, s, was, state):
        """What the row says about the change that just happened."""
        if state == "green":
            spent = time.time() + self.clock_offset - self.prev_since.get(
                s.get("session_id"), time.time() + self.clock_offset)
            if was == "red" and spent > 1:
                return "finished · " + fmt_elapsed(spent)
            return "finished"
        detail = str(s.get("detail") or "").strip()
        return detail or "waiting for you"

    def busy(self):
        """True while a lamp is cross-fading or a banner is on screen."""
        return bool(self.anim or self.banners)

    def frame(self):
        """Animation loop. Runs only while something is moving, then stops -
        an idle panel should not be waking the CPU 25 times a second."""
        self._frame_job = None
        try:
            self.draw()
        except Exception:
            pass
        finally:
            if self.busy():
                self._frame_job = self.root.after(FRAME_MS, self.frame)

    def start_frames(self):
        if self._frame_job is None and self.busy():
            self._frame_job = self.root.after(FRAME_MS, self.frame)

    def tick(self):
        try:
            self.draw()
            self.start_frames()
        except Exception:
            pass
        finally:
            self.root.after(500, self.tick)

    # --- drawing ------------------------------------------------------------

    def draw(self):
        c = self.canvas
        c.delete("all")
        self.button_hitboxes = []
        self.slider_hitboxes = []
        visible, hidden, body = self.visible_layout()
        if self.settings_open:
            body += self.SETTINGS_H
        h = self.resize(body)

        c.create_rectangle(0, 0, self.W - 1, h - 1, fill=BG, outline=BORDER)

        dot = "#3ddc84" if self.connected else "#ff5964"
        c.create_oval(10, self.HH // 2 - 3, 16, self.HH // 2 + 3, fill=dot, outline="")
        c.create_text(
            24,
            self.HH // 2,
            anchor="w",
            text="CLAUDE",
            fill=FG_HEADER,
            font=self.f(8, "bold"),
        )
        n = len(self.sessions)
        status = str(n) + " session" + ("" if n == 1 else "s")
        if not self.connected:
            status = "server offline"
        status_fill = FG_HEADER
        if self._toast:
            text, expiry = self._toast
            if time.time() < expiry:
                status = text
                status_fill = "#ffb03a"
            else:
                self._toast = None
        c.create_text(
            self.W - int(44 * self.scale),
            self.HH // 2,
            anchor="e",
            text=status,
            fill=status_fill,
            font=self.f(8),
        )
        # Explicit quit affordance: an overrideredirect window has no title bar
        # and no taskbar entry, so never rely on the context menu alone.
        c.create_text(
            self.W - int(13 * self.scale),
            self.HH // 2,
            anchor="center",
            text="×",
            fill=FG_HEADER,
            font=self.f(11),
        )
        c.create_text(
            self.W - int(30 * self.scale),
            self.HH // 2,
            anchor="center",
            text="⚙",
            fill=BAR_OK if self.settings_open else FG_HEADER,
            font=self.f(9),
        )
        c.create_line(0, self.HH, self.W, self.HH, fill=BORDER)

        self.row_hitboxes = []

        if not self.sessions:
            msg = (
                "waiting for Claude sessions..."
                if self.connected
                else "server offline - docker compose up -d"
            )
            c.create_text(
                self.W // 2,
                self.HH + self.RH // 2,
                text=msg,
                fill=FG_DIM,
                font=self.f(8),
            )
            if self.settings_open:
                self.draw_settings(self.HH + self.RH)
            return

        now = time.time() + self.clock_offset
        y = self.HH
        for i, (s, expanded) in enumerate(visible):
            self.draw_row(i, y, s, now, expanded)
            y += self.RH
            if expanded:
                self.draw_detail(y, s)
                y += self.DH
        if hidden:
            c.create_text(
                self.W // 2,
                y + self.RH // 2,
                text="+" + str(hidden) + " more (no room on screen)",
                fill=FG_DIM,
                font=self.f(8),
            )
            y += self.RH
        if self.settings_open:
            self.draw_settings(y)

    def draw_row(self, index, top, s, now, expanded=False):
        c = self.canvas
        sid = s.get("session_id", "")
        state = s.get("state", "green")
        cy = top + self.RH // 2
        self.row_hitboxes.append((top, top + self.RH, s))

        if self.hover_index == index:
            c.create_rectangle(1, top + 1, self.W - 2, top + self.RH - 1,
                               fill=BG_HOVER, outline="")

        self.draw_signal(cy, sid, state)

        # The banner and the row's own text cross-fade. Drawing both at full
        # strength during the fade overlaps two strings into unreadable mush.
        banner = self.banner_for(sid)
        fade = banner[2] if banner else 0.0

        elapsed = now - as_float(s.get("since"), now)
        stamp = "idle" if (state == "green" and elapsed > 300) else fmt_elapsed(elapsed)

        label = str(s.get("project") or "claude")
        if len(label) > PROJECT_CHARS:
            label = label[: PROJECT_CHARS - 1] + "…"
        detail = str(s.get("detail") or "")
        if state == "orange" and detail:
            # The expanded panel spells it out in full; up here the tool name
            # is the part worth the pixels.
            detail = detail.replace("needs permission: ", "")
            room = LABEL_CHARS - len(label) - 3
            if room > 4:
                if len(detail) > room:
                    detail = detail[: room - 1] + "…"
                label = label + "  · " + detail

        label_id = c.create_text(self.TX, cy, anchor="w", text=label,
                                 fill=mix(FG, BG, fade), font=self.f(9))

        # Subagents run inside their parent session and never get a row of
        # their own, so without this there is no sign from the collapsed row
        # that a session has agents working under it.
        agents = as_int((s.get("usage") or {}).get("agents")
                        if isinstance(s.get("usage"), dict) else 0)
        if agents and fade < 0.5:
            x0 = c.bbox(label_id)[2] + int(6 * self.scale)
            w = int(20 * self.scale)
            h = int(13 * self.scale)
            if x0 + w < self.W - int(48 * self.scale):
                c.create_polygon(
                    round_rect(x0, cy - h // 2, x0 + w, cy + h // 2,
                               max(3, int(4 * self.scale))),
                    smooth=True, fill="#3a2f14", outline="",
                )
                c.create_text(x0 + w // 2, cy, anchor="center",
                              text="⚙" + str(agents),
                              fill=BAR_WARN, font=self.f(7))
        c.create_text(
            self.W - 26, cy, anchor="e", text=stamp,
            fill=mix(FG_DIM, BG, fade), font=self.fm(8)
        )
        c.create_text(
            self.W - 13,
            cy,
            anchor="center",
            text="▾" if expanded else "▸",
            fill=mix(FG_HEADER, BG, fade),
            font=self.f(7),
        )

        if banner:
            self.draw_banner(top, banner)

    # --- the signal head ----------------------------------------------------

    def lamp_levels(self, sid, state):
        """Brightness 0..1 per lamp, cross-fading between states.

        Returns (levels, pulse) where pulse 0..1 drives the one-off halo swell
        on the lamp that just lit.
        """
        levels = {name: 0.0 for name in ORDER}
        anim = self.anim.get(sid)
        if not anim:
            levels[state] = 1.0
            return levels, 0.0

        was, now_state, started = anim
        age = time.time() - started
        if age >= FADE_SECS + PULSE_SECS:
            self.anim.pop(sid, None)
            levels[state] = 1.0
            return levels, 0.0

        if age < FADE_SECS:
            p = age / FADE_SECS
            levels[was] = 1.0 - p
            levels[now_state] = p
            return levels, 0.0

        levels[now_state] = 1.0
        # A single swell, up and back down.
        q = (age - FADE_SECS) / PULSE_SECS
        return levels, math.sin(math.pi * q)

    def draw_signal(self, cy, sid, state):
        """A traffic light: dark housing, bezelled lamps, the live one glowing."""
        c = self.canvas
        levels, pulse = self.lamp_levels(sid, state)

        c.create_polygon(
            round_rect(self.CX0, cy - self.CH // 2, self.CX1, cy + self.CH // 2, 6),
            smooth=True, fill=CASE_BG, outline=CASE_EDGE,
        )

        for j, name in enumerate(ORDER):
            cx = self.LX0 + j * self.LG
            level = levels.get(name, 0.0)
            on, glow = LIGHTS[name]

            if level > 0.01:
                halo = self.LR + 3 + pulse * 3.0
                c.create_oval(cx - halo, cy - halo, cx + halo, cy + halo,
                              fill=mix(CASE_BG, glow, level), outline="")

            c.create_oval(cx - self.LR - 1, cy - self.LR - 1,
                          cx + self.LR + 1, cy + self.LR + 1,
                          fill=BEZEL, outline="")
            c.create_oval(cx - self.LR, cy - self.LR, cx + self.LR, cy + self.LR,
                          fill=mix(LAMP_OFF, on, level), outline="")
            if level > 0.5:
                # A small off-centre highlight reads as a glass lens.
                c.create_oval(cx - 2, cy - self.LR + 1, cx, cy - self.LR + 3,
                              fill=mix(on, "#ffffff", 0.45 * level), outline="")

    # --- settings -----------------------------------------------------------

    def draw_settings(self, top):
        """Size, volume and alert sound, drawn inline under the rows."""
        c = self.canvas
        s = self.scale
        c.create_rectangle(1, top, self.W - 2, top + self.SETTINGS_H,
                           fill="#101318", outline="")
        c.create_line(8, top, self.W - 8, top, fill=BORDER)
        c.create_text(int(12 * s), top + int(13 * s), anchor="w", text="SETTINGS",
                      fill=FG_HEADER, font=self.f(7, "bold"))

        label_x = int(12 * s)
        track_x0 = int(64 * s)
        track_x1 = self.W - int(66 * s)
        value_x = self.W - int(12 * s)

        y = top + int(34 * s)
        frac = (self.scale - MIN_SCALE) / (MAX_SCALE - MIN_SCALE)
        self.draw_slider("scale", label_x, track_x0, track_x1, value_x, y,
                         "Size", frac, "%d%%" % round(self.scale * 100))

        y += int(24 * s)
        self.draw_slider("volume", label_x, track_x0, track_x1, value_x, y,
                         "Volume", self.volume / 100.0,
                         "muted" if self.volume <= 0 else "%d%%" % self.volume)

        y += int(26 * s)
        c.create_text(label_x, y, anchor="w", text="Sound",
                      fill=FG_DIM, font=self.f(8))
        self.draw_button(track_x0, y - int(9 * s), int(14 * s), "◂",
                         "sound_prev", None, h=int(18 * s))
        c.create_text((track_x0 + track_x1) // 2, y, anchor="center",
                      text=chime.label(self.sound), fill=FG, font=self.f(8))
        self.draw_button(track_x1 - int(14 * s), y - int(9 * s), int(14 * s),
                         "▸", "sound_next", None, h=int(18 * s))
        self.draw_button(value_x - int(44 * s), y - int(9 * s), int(44 * s),
                         "▷ test", "sound_test", None, h=int(18 * s))

    def draw_slider(self, key, label_x, x0, x1, value_x, y, label, frac, text):
        c = self.canvas
        s = self.scale
        c.create_text(label_x, y, anchor="w", text=label, fill=FG_DIM,
                      font=self.f(8))
        c.create_line(x0, y, x1, y, fill="#2a3039", width=max(2, int(3 * s)))
        hx = x0 + (x1 - x0) * max(0.0, min(1.0, frac))
        filled = mix("#2a3039", BAR_OK, 0.85)
        c.create_line(x0, y, hx, y, fill=filled, width=max(2, int(3 * s)))
        r = max(4, int(5 * s))
        c.create_oval(hx - r, y - r, hx + r, y + r, fill="#e6e9ef", outline="")
        c.create_text(value_x, y, anchor="e", text=text, fill=FG,
                      font=self.f(8))
        # A generous vertical band: a 3px line is far too thin to hit.
        self.slider_hitboxes.append((key, x0, x1, y - int(10 * s), y + int(10 * s)))

    def slider_at(self, x, y):
        for key, x0, x1, y0, y1 in self.slider_hitboxes:
            if y0 <= y <= y1 and x0 - 8 <= x <= x1 + 8:
                return key, x0, x1
        return None, 0, 0

    def apply_slider(self, key, x0, x1, x):
        frac = 0.0 if x1 <= x0 else (x - x0) / float(x1 - x0)
        frac = max(0.0, min(1.0, frac))
        if key == "scale":
            self.set_scale(MIN_SCALE + frac * (MAX_SCALE - MIN_SCALE))
        elif key == "volume":
            value = int(round(frac * 100))
            if value != self.volume:
                self.volume = value
                self.cfg["volume"] = value
                save_config(self.cfg)
                # No preview here: a continuous slider would fire one per pixel
                # of travel. on_release plays a single sample instead.
            self.draw()

    def cycle_sound(self, step):
        idx = (chime.ORDER.index(self.sound) + step) % len(chime.ORDER)
        self.sound = chime.ORDER[idx]
        self.cfg["sound"] = self.sound
        save_config(self.cfg)
        chime.play("green", self.volume, self.sound)
        self.draw()

    def toggle_settings(self):
        self.settings_open = not self.settings_open
        self.draw()

    # --- change banners -----------------------------------------------------

    def banner_for(self, sid):
        """(text, state, alpha) for a session's banner, or None."""
        entry = self.banners.get(sid)
        if not entry:
            return None
        text, state, started = entry
        age = time.time() - started
        if age >= BANNER_SECS:
            self.banners.pop(sid, None)
            return None
        if age < BANNER_FADE:
            alpha = age / BANNER_FADE
        elif age > BANNER_SECS - BANNER_FADE:
            alpha = (BANNER_SECS - age) / BANNER_FADE
        else:
            alpha = 1.0
        return text, state, max(0.0, min(1.0, alpha))

    def draw_banner(self, top, banner):
        """Fades in over the row's own text - never changes the row height, so
        nothing shifts under the cursor while you are reading it."""
        c = self.canvas
        text, state, alpha = banner
        on, glow = LIGHTS.get(state, LIGHTS["green"])
        c.create_rectangle(
            self.TX - 6, top + 4, self.W - 6, top + self.RH - 4,
            fill=mix(BG, glow, 0.85 * alpha), outline="",
        )
        c.create_text(
            self.TX + 2, top + self.RH // 2, anchor="w",
            text=text, fill=mix(BG, on, alpha), font=self.f(8, "bold"),
        )

    # --- expanded detail ----------------------------------------------------

    def draw_detail(self, top, s):
        """The panel shown under an expanded row: what it is doing, and what
        it has spent."""
        c = self.canvas
        usage = s.get("usage")
        if not isinstance(usage, dict):
            usage = {}

        c.create_rectangle(
            1, top, self.W - 2, top + self.DH, fill="#111318", outline=""
        )
        c.create_line(12, top, self.W - 12, top, fill=BORDER)

        y = top + 13
        state = s.get("state", "green")

        # what it is doing right now
        tool = str(s.get("tool") or "")
        detail = str(s.get("detail") or "")
        if state == "red":
            activity = "Working" + ("  ·  " + tool if tool else "")
        elif state == "orange":
            activity = detail or "Waiting for you"
        else:
            activity = "Idle"
        c.create_text(
            14, y, anchor="w", text=activity[:34], fill=FG, font=self.f(8)
        )
        agents = as_int(usage.get("agents"))
        if agents:
            c.create_text(
                self.W - 14,
                y,
                anchor="e",
                text=str(agents) + " agent" + ("" if agents == 1 else "s"),
                fill=BAR_WARN,
                font=self.f(8),
            )

        y += 18
        limit = as_int(usage.get("context_limit"))
        used = as_int(usage.get("context_tokens"))
        if limit > 0:
            frac = min(1.0, max(0.0, used / float(limit)))
            c.create_text(
                14,
                y,
                anchor="w",
                text="Context  " + fmt_tokens(used) + " / " + fmt_tokens(limit),
                fill=FG_DIM,
                font=self.f(8),
            )
            c.create_text(
                self.W - 14,
                y,
                anchor="e",
                text="{:.0f}%".format(frac * 100),
                fill=FG_DIM,
                font=self.f(8),
            )
            y += 14
            bar_w = self.W - 28
            colour = BAR_OK if frac < 0.75 else (BAR_WARN if frac < 0.9 else BAR_FULL)
            c.create_rectangle(
                14, y, 14 + bar_w, y + 5, fill=BAR_BG, outline=""
            )
            if frac > 0:
                c.create_rectangle(
                    14, y, 14 + max(2, int(bar_w * frac)), y + 5,
                    fill=colour, outline="",
                )
            y += 16

            c.create_text(
                14,
                y,
                anchor="w",
                text="Out " + fmt_tokens(usage.get("tokens_out"))
                + "   In " + fmt_tokens(
                    as_int(usage.get("tokens_in"))
                    + as_int(usage.get("tokens_cache_write"))
                )
                + "   Cached " + fmt_tokens(usage.get("tokens_cache_read")),
                fill=FG_DIM,
                font=self.f(8),
            )
            y += 16
            bits = [str(usage.get("model_name") or usage.get("model") or "")]
            if as_int(s.get("pid")):
                bits.append("pid " + str(as_int(s.get("pid"))))
            turns = as_int(usage.get("turns"))
            if turns:
                bits.append(str(turns) + " turns")
            line = "  ·  ".join(b for b in bits if b)
            c.create_text(
                14, y, anchor="w", text=line[:44], fill=FG_HEADER,
                font=self.f(8),
            )
        else:
            # No transcript reading yet: the session predates the hook upgrade.
            c.create_text(
                14,
                y,
                anchor="w",
                text="No token data - restart this Claude session",
                fill=FG_HEADER,
                font=self.f(8),
            )
            y += 16
            cwd = str(s.get("cwd") or "")
            c.create_text(
                14, y, anchor="w", text=cwd[-44:], fill=FG_HEADER,
                font=self.f(8),
            )

        self.draw_button(14, top + self.DH - 26, 110, "Focus window", "focus", s)
        self.draw_button(
            self.W - 86, top + self.DH - 26, 72, "Dismiss", "dismiss", s
        )

    def draw_button(self, x, y, w, label, action, s, h=None):
        c = self.canvas
        h = h or max(14, int(18 * self.scale))
        sid = s.get("session_id") if isinstance(s, dict) else None
        hot = self._hover_button == (action, sid)
        c.create_rectangle(
            x, y, x + w, y + h,
            fill=BTN_HOVER if hot else BTN_BG,
            outline=BORDER,
        )
        c.create_text(
            x + w // 2, y + h // 2, text=label,
            fill=FG if hot else FG_DIM, font=self.f(8),
        )
        self.button_hitboxes.append((x, y, x + w, y + h, action, s))

    # --- interaction --------------------------------------------------------

    def row_at(self, y):
        for top, bottom, s in self.row_hitboxes:
            if top <= y < bottom:
                return s
        return None

    def on_press(self, ev):
        key, x0, x1 = self.slider_at(ev.x, ev.y)
        if key:
            # Grabbing a slider must not also drag the window out from under it.
            self._drag_slider = (key, x0, x1)
            self._press = None
            self.apply_slider(key, x0, x1, ev.x)
            return
        self._press = (ev.x_root, ev.y_root, self.root.winfo_x(), self.root.winfo_y())
        self._dragging = False

    def on_drag(self, ev):
        if self._drag_slider:
            key, x0, x1 = self._drag_slider
            self.apply_slider(key, x0, x1, ev.x)
            return
        if not self._press:
            return
        sx, sy, wx, wy = self._press
        dx, dy = ev.x_root - sx, ev.y_root - sy
        if not self._dragging and (abs(dx) > 4 or abs(dy) > 4):
            self._dragging = True
        if self._dragging:
            self.root.geometry("+" + str(wx + dx) + "+" + str(wy + dy))

    def button_at(self, x, y):
        for x0, y0, x1, y1, action, s in self.button_hitboxes:
            if x0 <= x <= x1 and y0 <= y <= y1:
                return action, s
        return None, None

    def on_release(self, ev):
        if self._drag_slider:
            key = self._drag_slider[0]
            self._drag_slider = None
            if key == "volume" and self.volume > 0:
                chime.play("green", self.volume, self.sound)
            return
        if self._dragging:
            self.cfg["pos"] = [self.root.winfo_x(), self.root.winfo_y()]
            save_config(self.cfg)
            self._press = None
            self._dragging = False
            return

        if ev.y < self.HH:
            if ev.x > self.W - int(24 * self.scale):
                self._press = None
                self.quit()
                return
            if ev.x > self.W - int(40 * self.scale):
                self._press = None
                self._dragging = False
                self.toggle_settings()
                return
        else:
            action, s = self.button_at(ev.x, ev.y)
            if action == "sound_prev":
                self.cycle_sound(-1)
            elif action == "sound_next":
                self.cycle_sound(1)
            elif action == "sound_test":
                chime.play("green", self.volume, self.sound)
            elif action == "focus":
                self.open_session(s)
            elif action == "dismiss":
                self.delete_session(s.get("session_id"))
            else:
                row = self.row_at(ev.y)
                if row:
                    self.toggle_expanded(row)
        self._press = None
        self._dragging = False

    def toggle_expanded(self, s):
        sid = s.get("session_id")
        if sid in self.expanded:
            self.expanded.discard(sid)
        else:
            self.expanded.add(sid)
        self.draw()

    def on_double_click(self, ev):
        """Focus the session's window.

        The two single clicks underneath have already toggled the row open and
        shut again, so the expansion state is left exactly as it was.
        """
        if ev.y < self.HH:
            return
        action, _s = self.button_at(ev.x, ev.y)
        if action:
            return
        s = self.row_at(ev.y)
        if s:
            self.open_session(s)

    def open_session(self, s):
        """Bring the window hosting this Claude session to the front.

        Resolved live from the session's pid, so it still works after the
        editor or terminal has been restarted since the session began.
        """
        pid = as_int(s.get("pid"))
        hint = as_int(s.get("hwnd"))
        ok, hwnd = focus_session(pid, hint, str(s.get("cwd") or ""))
        if ok:
            title = window_title(hwnd)
            self.toast(title[:40] if title else "focused")
        elif hwnd:
            self.toast("raised window (could not steal focus)")
        else:
            self.toast("window not found - session may have moved")

    def on_middle_click(self, ev):
        s = self.row_at(ev.y)
        if s:
            self.delete_session(s.get("session_id"))

    def on_right_click(self, ev):
        try:
            # A borderless window may not hold focus; the menu needs it to
            # dismiss properly on Windows.
            self.root.focus_force()
            self.menu.tk_popup(ev.x_root, ev.y_root)
        finally:
            self.menu.grab_release()

    def on_motion(self, ev):
        idx = -1
        for i, box in enumerate(self.row_hitboxes):
            if box[0] <= ev.y < box[1]:
                idx = i
                break
        action, s = self.button_at(ev.x, ev.y)
        hover_button = None
        if action:
            hover_button = (action,
                            s.get("session_id") if isinstance(s, dict) else None)
        if idx != self.hover_index or hover_button != self._hover_button:
            self.hover_index = idx
            self._hover_button = hover_button
            self.draw()

    def on_enter(self, _ev):
        self.root.attributes("-alpha", 1.0)

    def on_leave(self, _ev):
        self.hover_index = -1
        self._hover_button = None
        self.root.attributes("-alpha", self.alpha_idle)
        self.draw()

    def toast(self, text):
        # Held in state rather than drawn directly: the 500ms redraw would
        # otherwise wipe it almost immediately.
        text = str(text)
        if len(text) > 32:
            text = text[:31] + "…"
        self._toast = (text, time.time() + 2.0)
        self.draw()

    # --- server actions -----------------------------------------------------

    def request(self, path, method="GET"):
        def work():
            try:
                req = urllib.request.Request(SERVER + path, method=method)
                urllib.request.urlopen(req, timeout=2).read()
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    def delete_session(self, sid):
        if sid:
            self.request("/sessions/" + str(sid), "DELETE")

    def clear_sessions(self):
        self.request("/sessions", "DELETE")

    # --- menu ---------------------------------------------------------------

    def toggle_topmost(self):
        val = bool(self.var_top.get())
        self.root.attributes("-topmost", val)
        self.cfg["topmost"] = val
        save_config(self.cfg)

    def toggle_sounds(self):
        self.sounds = bool(self.var_snd.get())
        self.cfg["sounds"] = self.sounds
        save_config(self.cfg)

    def test_chime(self):
        chime.play("orange", self.volume, self.sound)
        self.root.after(
            int(chime.duration("orange", self.sound) * 1000) + 300,
            lambda: chime.play("green", self.volume, self.sound))

    def quit(self):
        self.feed.stopped.set()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    Panel().run()
