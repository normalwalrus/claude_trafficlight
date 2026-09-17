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
try:
    # Two layouts: staged, where the panel sits in <payload>/panel/ and this
    # lives one level up, and the repo, where it is in hooks/. Both matter -
    # a panel started from the repo has to claim the same lock, or the hook
    # would start a second one beside it.
    _here = os.path.dirname(os.path.abspath(__file__))
    _parent = os.path.dirname(_here)
    for _candidate in (_parent, os.path.join(_parent, "hooks")):
        if _candidate not in sys.path:
            sys.path.append(_candidate)
    import desktop_panel
except Exception:
    desktop_panel = None
import themes  # noqa: E402
import desktop  # noqa: E402
from desktop import foreground_window  # noqa: E402
from winutil import focus_session, window_title, resolve_window  # noqa: E402

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

FADE_SECS = 0.25     # the old lamp dims over this long
RISE_SECS = 0.09     # ...but the new one lights in this long
PULSE_SECS = 0.60    # one halo swell on the lamp that just lit
FRAME_MS = 40        # ~25fps, and only while something is actually moving
POLL_MS = 16         # how often the UI thread drains the SSE queue

BANNER_SECS = 4.0
BANNER_FADE = 0.35

# A state has to hold this long before it is worth a sound. Anything shorter
# was never visible on screen, so chiming for it is just noise.
CHIME_DELAY_MS = 700

# A session you have left waiting this long stops being just another amber row:
# it grows, and its background breathes, so a glance at the panel lands on it
# first. Silent on purpose - the chime already had its turn when it went amber.
URGENT_AFTER = 120
URGENT_GROW = 8           # px the row gains
URGENT_PERIOD = 1.8       # seconds per breath
URGENT_DEPTH = 0.40       # how far towards the amber halo it breathes

# If nobody has touched the machine for this long when a session finishes, you
# were not there to see it: the "finished" banner then stays up until you come
# back and deal with that row, instead of fading after four seconds.
AWAY_AFTER = 300

# Characters that fit on one line of the detail panel, at any scale: the width
# and the font size both scale together. Measured, not guessed - 292px of room
# at an average 5.6px per character is a little over 50, and 44 leaves room for
# a line of unusually wide ones. It matches the identity line below it.
DETAIL_CHARS = 44
PROMPT_LINES = 2

# How often the panel says it is alive, for the hook that may have started it.
HEARTBEAT_SECS = 5

# Settings panel, at 100% scale.
SETTINGS_H = 134
MIN_SCALE = 0.75
MAX_SCALE = 1.50

BAR_BG = "#22262f"
BAR_OK = "#3ddc84"
BAR_WARN = "#ffb03a"
BAR_FULL = "#ff5964"
BTN_BG = "#232833"
BTN_HOVER = "#2e3543"

# Fonts, in order of preference. Tk has no CSS-style fallback list: it takes a
# single family, and a name it does not have drops to a bitmap default (X11
# "fixed"/"gothic") rather than the nearest match - which is why hard-coding
# Segoe UI / Consolas looked right only on Windows. It also does no per-glyph
# fallback, so the family we pick has to carry every glyph the panel draws: the
# gear (⚙), the triangle arrows (▸ ▾ ◂ ▷), ×, ·, … and the curly quotes.
#
# The list is walked once at startup and the first family the machine actually
# has wins, so one ordered list covers every platform: Segoe UI on Windows, the
# system faces on macOS, and DejaVu on Linux. DejaVu leads the Linux choices on
# purpose - it is the fontconfig default and the one common Linux sans that has
# all of those glyphs. Liberation Sans, though it looks most like Windows Arial,
# is missing the gear and every triangle, so it is only a late fallback.
UI_FONT_CANDIDATES = (
    "Segoe UI",                          # Windows
    "SF Pro Text", "Helvetica Neue",     # macOS
    "DejaVu Sans", "Noto Sans", "FreeSans", "Cantarell", "Ubuntu",  # Linux
    "Liberation Sans", "Arial", "Helvetica",
)
MONO_FONT_CANDIDATES = (
    "Consolas",                          # Windows
    "SF Mono", "Menlo", "Monaco",        # macOS
    "DejaVu Sans Mono", "Noto Sans Mono", "Ubuntu Mono", "FreeMono",  # Linux
    "Liberation Mono", "Courier New",
)


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


def wrap_text(text, width, max_lines):
    """Break text onto at most `max_lines` lines of at most `width` characters.

    Wraps on spaces, hard-breaks a word too long to fit one (a path, a URL),
    and ends the last line with an ellipsis if anything had to be dropped.
    """
    words = str(text or "").split()
    if not words or width <= 0 or max_lines <= 0:
        return []
    lines, current = [], ""
    for word in words:
        while len(word) > width:
            if current:
                lines.append(current)
                current = ""
            lines.append(word[:width])
            word = word[width:]
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= width:
            current += " " + word
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    if len(lines) <= max_lines:
        return lines
    kept = lines[:max_lines]
    kept[-1] = kept[-1][:max(1, width - 1)].rstrip() + "…"
    return kept


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

    @staticmethod
    def snapshots(lines):
        """Snapshot bodies from a raw SSE stream, one yield per line read.

        Yields None for a line that is not a snapshot, so the caller still gets
        to check whether it should stop between lines. The `event:` field is
        what keeps the keep-alive out: it carries a data line of its own, and
        taking that for a snapshot would empty the panel every fifteen seconds.
        """
        kind = ""
        for raw in lines:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                kind = ""              # a blank line ends one event
                yield None
                continue
            if line.startswith("event:"):
                kind = line[6:].strip()
                yield None
                continue
            body = line[5:].strip() if line.startswith("data:") else ""
            yield body if (body and kind in ("", "message")) else None

    def run(self):
        while not self.stopped.is_set():
            try:
                req = urllib.request.Request(
                    SERVER + "/stream", headers={"Accept": "text/event-stream"}
                )
                with urllib.request.urlopen(req, timeout=40) as resp:
                    self.out.put(("connected", True))
                    for body in self.snapshots(resp):
                        if self.stopped.is_set():
                            return
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
    # --- theme --------------------------------------------------------------

    def apply_theme(self, name=None):
        """Load a theme's colours onto the instance.

        Colours live here rather than at module level for the same reason the
        geometry does: so they can change while the panel is running.
        """
        if name is not None:
            # resolve() also maps themes that have been renamed, so an older
            # config does not silently snap back to the default.
            self.theme = themes.resolve(name)
        t = themes.get(self.theme)
        self.BG = t["bg"]
        self.BG_HOVER = t["bg_hover"]
        self.BORDER = t["border"]
        self.FG = t["fg"]
        self.FG_DIM = t["fg_dim"]
        self.FG_HEADER = t["fg_header"]
        self.DETAIL_BG = t["detail_bg"]
        self.SETTINGS_BG = t["settings_bg"]
        self.MENU_BG = t["menu_bg"]
        self.MENU_ACTIVE = t["menu_active"]
        self.CASE_BG = t["case_bg"]
        self.CASE_EDGE = t["case_edge"]
        self.BEZEL = t["bezel"]
        self.LAMP_OFF = t["lamp_off"]
        self.LENS = t["lens"]
        self.LIGHTS = {k: tuple(v) for k, v in t["lights"].items()}
        self.BAR_BG = t["bar_bg"]
        self.BAR_OK = t["bar_ok"]
        self.BAR_WARN = t["bar_warn"]
        self.BAR_FULL = t["bar_full"]
        self.BTN_BG = t["btn_bg"]
        self.BTN_HOVER = t["btn_hover"]
        self.SLIDER_TRACK = t["slider_track"]
        self.SLIDER_KNOB = t["slider_knob"]
        self.BADGE_BG = t["badge_bg"]
        self.BADGE_FG = t["badge_fg"]

    def restyle(self):
        """Push the current theme into the widgets Tk owns."""
        try:
            self.root.configure(bg=self.BG)
            self.canvas.configure(bg=self.BG)
            self.menu.configure(bg=self.MENU_BG, fg=self.FG,
                                activebackground=self.MENU_ACTIVE,
                                activeforeground=self.FG)
        except Exception:
            pass

    # --- shared settings ----------------------------------------------------
    #
    # The alert sound, the volume and the theme live on the server, because
    # both panels chime at the same events: a choice kept in this panel alone
    # means picking a sound in the browser and still hearing this one's. The
    # local panel.json stays as the cache, so the panel looks right at startup
    # and while the server is away.

    def push_settings(self, patch):
        """Tell the server what was just chosen here. Never blocks the UI."""
        def send():
            try:
                req = urllib.request.Request(
                    SERVER + "/settings",
                    data=json.dumps(patch).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="PUT",
                )
                urllib.request.urlopen(req, timeout=2).read()
            except Exception:
                pass          # offline: the local value still applies here

        threading.Thread(target=send, daemon=True).start()

    def apply_settings(self, data):
        """Take on a setting chosen in the other panel."""
        if not isinstance(data, dict):
            return
        revision = as_int(data.get("revision"))
        if revision <= 0:
            # Nothing has ever been chosen. Offer what this panel remembers,
            # so moving to shared settings keeps the choice already made.
            if not self._seeded_settings:
                self._seeded_settings = True
                self.push_settings({"sound": self.sound, "volume": self.volume,
                                    "theme": self.theme})
            return
        if revision == self.settings_revision:
            return
        self.settings_revision = revision
        redraw = False

        sound = data.get("sound")
        if isinstance(sound, str) and sound in chime.ORDER and sound != self.sound:
            self.sound = self.cfg["sound"] = sound
            redraw = True

        volume = data.get("volume")
        if isinstance(volume, (int, float)) and not isinstance(volume, bool):
            volume = chime.as_volume(volume)
            if volume != self.volume:
                self.volume = self.cfg["volume"] = volume
                redraw = True

        theme = data.get("theme")
        if isinstance(theme, str) and theme in themes.ORDER and theme != self.theme:
            self.apply_theme(theme)
            self.cfg["theme"] = self.theme
            self.restyle()
            redraw = True

        if redraw:
            save_config(self.cfg)
            self.draw()

    def cycle_theme(self, step):
        idx = (themes.ORDER.index(self.theme) + step) % len(themes.ORDER)
        self.apply_theme(themes.ORDER[idx])
        self.cfg["theme"] = self.theme
        save_config(self.cfg)
        self.push_settings({"theme": self.theme})
        self.restyle()
        self.draw()

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

    def _pick_family(self, candidates, default_font):
        """The first of `candidates` this machine actually has, else Tk's own
        default for that role.

        Two ways a family can count as present: it is in Tk's family list, or -
        because some X builds under-report what Xft can still resolve - Tk says
        it is what it *would actually use* when asked for it. Falling back to the
        real family behind TkDefaultFont/TkFixedFont matters: a name Tk does not
        have drops to a bitmap font, so a bogus "Segoe UI" on Linux looks far
        worse than Tk's own default sans.
        """
        from tkinter import font as tkfont
        try:
            have = {name.lower() for name in tkfont.families(self.root)}
        except Exception:
            have = set()
        for name in candidates:
            low = name.lower()
            if low in have:
                return name
            try:
                actual = tkfont.Font(
                    root=self.root, family=name, size=10).actual("family")
            except Exception:
                continue
            if actual and actual.lower() == low:
                return name
        try:
            return tkfont.nametofont(default_font).actual("family")
        except Exception:
            return candidates[0]

    def resolve_fonts(self):
        """Choose the UI and monospace families once, after the root exists."""
        self.ui_family = self._pick_family(UI_FONT_CANDIDATES, "TkDefaultFont")
        self.mono_family = self._pick_family(MONO_FONT_CANDIDATES, "TkFixedFont")

    def f(self, size, style=None):
        """A scaled UI font. Tk needs a real point size, so this rounds."""
        pt = max(6, int(round(size * self.scale)))
        return (self.ui_family, pt) if style is None else (self.ui_family, pt, style)

    def fm(self, size):
        return (self.mono_family, max(6, int(round(size * self.scale))))

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
        self._chime_armed = {}  # sid -> (state, token) awaiting confirmation
        self._window_cache = {}  # sid -> hwnd, so the foreground check is cheap
        self._chime_token = 0   # so a superseded timer cannot fire early
        self.collapsed = bool(self.cfg.get("collapsed", False))
        self._last_heartbeat = 0.0
        self._frame_job = None
        self.settings_open = False
        self.slider_hitboxes = []
        self._drag_slider = None
        # Real families are chosen once the Tk root exists (resolve_fonts); these
        # are only a safe default in case f()/fm() run before that.
        self.ui_family = UI_FONT_CANDIDATES[0]
        self.mono_family = MONO_FONT_CANDIDATES[0]

        self.theme = themes.DEFAULT
        self.apply_theme(str(self.cfg.get("theme", themes.DEFAULT)))
        self.scale = min(MAX_SCALE, max(MIN_SCALE,
                                        as_float(self.cfg.get("scale", 1.0), 1.0)))
        self.sound = str(self.cfg.get("sound", chime.DEFAULT))
        if self.sound not in chime.ORDER:
            self.sound = chime.DEFAULT
        # The server holds the sound, volume and theme both panels use; these
        # track what we have taken on from it. 0 means it has told us nothing.
        self.settings_revision = 0
        self._seeded_settings = False
        self.rescale()

        self.root = tk.Tk()
        self.resolve_fonts()
        self.root.title("Claude Traffic Light")
        self.root.overrideredirect(True)
        self.root.configure(bg=self.BG)
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
            bg=self.BG,
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(fill="both", expand=True)

        self.menu = tk.Menu(
            self.root,
            tearoff=0,
            bg=self.MENU_BG,
            fg=self.FG,
            activebackground=self.MENU_ACTIVE,
            activeforeground=self.FG,
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
        self.menu.add_command(label="Minimise to a badge", command=self.collapse)
        self.menu.add_command(label="Reset position", command=self.reset_position)
        self.menu.add_separator()
        # Say what Quit costs at the moment you are choosing it. A panel the
        # hook starts does not come back on its own - that is the point of the
        # stamp - and `docker compose up -d` is a no-op while the container is
        # already running, so name the command that actually works.
        self.menu.add_command(
            label="Quit (back on: docker compose restart)" if self.managed()
            else "Quit",
            command=self.quit,
        )

        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<Double-Button-1>", self.on_double_click)
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

    # --- how tall is a row, and is it shouting -----------------------------

    def is_urgent(self, s):
        """True once a session has been waiting on you for a good while.

        Only ever true for amber: red is Claude's turn, and a finished session
        is not asking for anything.
        """
        if not self.connected or not isinstance(s, dict):
            return False
        if s.get("state") != "orange":
            return False
        since = as_float(s.get("since"), 0.0)
        if not since:
            return False
        return (time.time() + self.clock_offset) - since >= URGENT_AFTER

    def any_urgent(self):
        return any(self.is_urgent(s) for s in self.sessions)

    def row_height(self, s):
        """Rows are not all the same height: one that has been left waiting
        grows, so the panel reads differently from across the room."""
        return self.RH + (int(URGENT_GROW * self.scale) if self.is_urgent(s) else 0)

    def detail_lines(self, s):
        """What this session is about, as (kind, text) lines for the detail.

        Claude Code writes both itself: a title it generates for the
        conversation, and the last thing you typed. Neither is reliable alone -
        the title is generated early and goes stale, the last prompt is often a
        fragment like "carry on" - so both are shown when both exist.
        """
        if not isinstance(s, dict):
            return []
        out = []
        title = str(s.get("title") or "").strip()
        prompt = str(s.get("prompt") or "").strip()
        if title:
            for line in wrap_text(title, DETAIL_CHARS, 1):
                out.append(("title", line))
        if prompt:
            quoted = "“" + prompt + "”"
            for line in wrap_text(quoted, DETAIL_CHARS, PROMPT_LINES):
                out.append(("prompt", line))
        return out

    def detail_height(self, s):
        return self.DH + len(self.detail_lines(s)) * int(14 * self.scale)

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
            h = self.row_height(s) + (self.detail_height(s) if expanded else 0)
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
        # Width too: the collapsed badge shrinks it, and expanding again has to
        # put it back.
        self.canvas.config(width=self.W, height=h)
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
                        # The rows on screen are the last thing we were told,
                        # not what is happening now. Nothing may still be
                        # fading or announcing as though it were live.
                        self.anim.clear()
                        self.banners.clear()
                        self.draw()
                elif kind == "snapshot":
                    self.on_snapshot(payload)
        except queue.Empty:
            pass
        except Exception:
            pass
        finally:
            self.root.after(POLL_MS, self.pump)

    def on_snapshot(self, body):
        try:
            data = json.loads(body)
        except Exception:
            return
        if not isinstance(data, dict):
            return
        self.clock_offset = as_float(data.get("now"), time.time()) - time.time()
        self.apply_settings(data.get("settings"))
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
                # Amber always stays until you deal with it. Green normally
                # fades after a few seconds - but if you were not at the
                # machine when it finished, you never saw it, so it stays too.
                sticky = state == "orange" or (state == "green" and self.away())
                self.banners[sid] = (
                    self.banner_text(s, was, state), state, time.time(), sticky)
                self.flash_until[sid] = time.time() + BANNER_SECS
                self.arm_chime(sid, state)
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
                self._chime_armed.pop(sid, None)
                self._window_cache.pop(sid, None)
        self.expanded &= live

        self.sessions = sessions
        self.seen_first_snapshot = True
        self.draw()
        self.start_frames()

    def session_by_id(self, sid):
        for s in self.sessions:
            if s.get("session_id") == sid:
                return s
        return None

    def agents_running(self, s):
        usage = s.get("usage") if isinstance(s, dict) else None
        return as_int(usage.get("agents")) if isinstance(usage, dict) else 0

    def arm_chime(self, sid, state):
        """Chime only for a change the main session actually made, and only if
        it lasts long enough to see.

        Subagents run inside their parent's session, so their churn shows up as
        the parent's own state changing: an agent finishing makes the parent
        fire Stop (green) and then immediately pick the work back up (red). The
        sound fires for a green that was never on screen. Waiting a moment and
        re-checking removes those, and a session with agents still running is
        not finished at all, so it stays silent regardless.
        """
        if not self.sounds:
            return
        # The token is what makes re-arming exact. Comparing the state alone
        # lets the timer from a green that has already been and gone fire for a
        # *later* green, well before that one has lasted the delay - which is
        # the flicker this whole mechanism exists to swallow.
        self._chime_token += 1
        token = self._chime_token
        self._chime_armed[sid] = (state, token)
        self.root.after(CHIME_DELAY_MS, lambda: self.fire_chime(sid, state, token))
        return token

    def fire_chime(self, sid, state, token=None):
        armed = self._chime_armed.get(sid)
        if not armed or armed[0] != state:
            return                      # superseded by a newer change
        if token is not None and armed[1] != token:
            return                      # re-armed since; that timer will do it
        self._chime_armed.pop(sid, None)
        if not self.sounds:
            return
        current = self.session_by_id(sid)
        if current is None or current.get("state") != state:
            return                      # too brief to see, so too brief to hear
        if self.agents_running(current):
            return                      # an agent is still working; not done
        chime.play(state, self.volume, self.sound)

    def away(self):
        """Whether the machine has been untouched long enough that you cannot
        have seen what just happened.

        Unknown (anything but Windows) counts as being here: the panel then
        behaves exactly as it always did rather than leaving banners up that
        nobody asked for.
        """
        idle = desktop.idle_seconds()
        return idle is not None and idle >= AWAY_AFTER

    def banner_text(self, s, was, state):
        """What the row says about the change that just happened."""
        if state == "green":
            spent = time.time() + self.clock_offset - self.prev_since.get(
                s.get("session_id"), time.time() + self.clock_offset)
            if was == "red" and spent > 1:
                return "finished · " + fmt_elapsed(spent)
            return "finished"
        # This one stays put, so it has to carry the project name too -
        # otherwise the row it is covering becomes unidentifiable.
        detail = str(s.get("detail") or "").strip() or "waiting for you"
        project = str(s.get("project") or "").strip()
        return (project + "  ·  " + detail) if project else detail

    def busy(self):
        """True while a lamp is cross-fading, a banner is up, or a row is
        breathing because it has been left waiting."""
        return bool(self.anim or self.banners or self.any_urgent())

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
            self.retire_opened_banners()
            self.draw()
            self.start_frames()
            self.heartbeat()
        except Exception:
            pass
        finally:
            self.root.after(500, self.tick)

    def heartbeat(self):
        """Say that a panel is running, so the hook does not start a second.

        Every few seconds rather than every tick: it is a file write, and the
        hook only cares whether it is recent.
        """
        if desktop_panel is None:
            return
        now = time.time()
        if now - self._last_heartbeat < HEARTBEAT_SECS:
            return
        self._last_heartbeat = now
        desktop_panel.touch_lock()

    # --- drawing ------------------------------------------------------------

    def draw(self):
        c = self.canvas
        c.delete("all")
        self.button_hitboxes = []
        self.slider_hitboxes = []
        self.row_hitboxes = []
        if self.collapsed:
            self.draw_collapsed()
            return
        visible, hidden, body = self.visible_layout()
        if self.settings_open:
            body += self.SETTINGS_H
        h = self.resize(body)

        c.create_rectangle(0, 0, self.W - 1, h - 1, fill=self.BG, outline=self.BORDER)

        dot = self.BAR_OK if self.connected else self.BAR_FULL
        c.create_oval(10, self.HH // 2 - 3, 16, self.HH // 2 + 3, fill=dot, outline="")
        c.create_text(
            24,
            self.HH // 2,
            anchor="w",
            text="CLAUDE",
            fill=self.FG_HEADER,
            font=self.f(8, "bold"),
        )
        n = len(self.sessions)
        status = str(n) + " session" + ("" if n == 1 else "s")
        if not self.connected:
            status = "server offline"
        status_fill = self.FG_HEADER
        if self._toast:
            text, expiry = self._toast
            if time.time() < expiry:
                status = text
                status_fill = self.BAR_WARN
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
        self._icon_close(self.W - int(13 * self.scale), self.HH // 2,
                         max(4, int(round(4.5 * self.scale))), self.FG_HEADER)
        self._icon_gear(self.W - int(30 * self.scale), self.HH // 2,
                        max(5, int(round(6 * self.scale))),
                        self.BAR_OK if self.settings_open else self.FG_HEADER)
        c.create_line(0, self.HH, self.W, self.HH, fill=self.BORDER)

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
                fill=self.FG_DIM,
                font=self.f(8),
            )
            if self.settings_open:
                self.draw_settings(self.HH + self.RH)
            return

        now = time.time() + self.clock_offset
        y = self.HH
        for i, (s, expanded) in enumerate(visible):
            self.draw_row(i, y, s, now, expanded)
            y += self.row_height(s)
            if expanded:
                self.draw_detail(y, s)
                y += self.detail_height(s)
        if hidden:
            c.create_text(
                self.W // 2,
                y + self.RH // 2,
                text="+" + str(hidden) + " more (no room on screen)",
                fill=self.FG_DIM,
                font=self.f(8),
            )
            y += self.RH
        if self.settings_open:
            self.draw_settings(y)

    def draw_row(self, index, top, s, now, expanded=False):
        c = self.canvas
        sid = s.get("session_id", "")
        # With the feed down every lamp goes out: "unknown" lights none of the
        # three. A panel confidently showing green for a session that ended
        # while the server was away is worse than one that admits it cannot
        # see. Same for a row Claude Code lists but that has never reported.
        state = s.get("state", "green") if self.connected else "unknown"
        rh = self.row_height(s)
        cy = top + rh // 2
        self.row_hitboxes.append((top, top + rh, s))

        if self.is_urgent(s):
            # Breathing, not flashing: it has to be noticeable from the corner
            # of your eye without being the brightest thing on the desktop.
            phase = 0.5 - 0.5 * math.cos(2 * math.pi * time.time() / URGENT_PERIOD)
            wash = mix(self.BG, self.LIGHTS["orange"][1], URGENT_DEPTH * phase)
            c.create_rectangle(1, top + 1, self.W - 2, top + rh - 1,
                               fill=wash, outline="")
            edge = max(2, int(3 * self.scale))
            c.create_rectangle(1, top + 1, 1 + edge, top + rh - 1,
                               fill=self.LIGHTS["orange"][0], outline="")
        elif self.hover_index == index:
            c.create_rectangle(1, top + 1, self.W - 2, top + rh - 1,
                               fill=self.BG_HOVER, outline="")

        self.draw_signal(cy, sid, state)

        # The banner and the row's own text cross-fade. Drawing both at full
        # strength during the fade overlaps two strings into unreadable mush.
        banner = self.banner_for(sid)
        fade = banner[2] if banner else 0.0

        elapsed = now - as_float(s.get("since"), now)
        if not self.connected:
            stamp = "—"          # how long ago we stopped hearing, not a state
        elif state == "unknown":
            stamp = ""
        elif state == "green" and elapsed > 300:
            stamp = "idle"
        else:
            stamp = fmt_elapsed(elapsed)

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

        label_id = c.create_text(
            self.TX, cy, anchor="w", text=label,
            fill=mix(self.FG if self.connected else self.FG_DIM, self.BG, fade),
            font=self.f(9))

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
                    smooth=True, fill=self.BADGE_BG, outline="",
                )
                gr = max(2, int(round(3 * self.scale)))
                self._icon_gear(x0 + int(7 * self.scale), cy, gr,
                                self.BADGE_FG, bg=self.BADGE_BG)
                c.create_text(x0 + int(7 * self.scale) + gr + int(2 * self.scale),
                              cy, anchor="w", text=str(agents),
                              fill=self.BADGE_FG, font=self.f(7))
        c.create_text(
            self.W - int(26 * self.scale), cy, anchor="e", text=stamp,
            fill=mix(self.FG_DIM, self.BG, fade), font=self.fm(8)
        )
        self._icon_tri(
            self.W - int(13 * self.scale), cy, max(3, int(4 * self.scale)),
            mix(self.FG_HEADER, self.BG, fade),
            "down" if expanded else "right",
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
            levels[was] = 1.0 - age / FADE_SECS
            # The new lamp rises far faster than the old one falls. A symmetric
            # cross-fade means the light only reads as "on" halfway through,
            # which shows up as the panel lagging behind Claude.
            levels[now_state] = min(1.0, age / RISE_SECS)
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
            smooth=True, fill=self.CASE_BG, outline=self.CASE_EDGE,
        )

        for j, name in enumerate(ORDER):
            cx = self.LX0 + j * self.LG
            level = levels.get(name, 0.0)
            on, glow = self.LIGHTS[name]

            if level > 0.01:
                halo = self.LR + 3 + pulse * 3.0
                c.create_oval(cx - halo, cy - halo, cx + halo, cy + halo,
                              fill=mix(self.CASE_BG, glow, level), outline="")

            c.create_oval(cx - self.LR - 1, cy - self.LR - 1,
                          cx + self.LR + 1, cy + self.LR + 1,
                          fill=self.BEZEL, outline="")
            c.create_oval(cx - self.LR, cy - self.LR, cx + self.LR, cy + self.LR,
                          fill=mix(self.LAMP_OFF, on, level), outline="")
            if level > 0.5:
                # A small off-centre highlight reads as a glass lens.
                c.create_oval(cx - 2, cy - self.LR + 1, cx, cy - self.LR + 3,
                              fill=mix(on, self.LENS, 0.45 * level), outline="")

    # --- settings -----------------------------------------------------------

    def draw_settings(self, top):
        """Size, volume and alert sound, drawn inline under the rows."""
        c = self.canvas
        s = self.scale
        c.create_rectangle(1, top, self.W - 2, top + self.SETTINGS_H,
                           fill=self.SETTINGS_BG, outline="")
        c.create_line(8, top, self.W - 8, top, fill=self.BORDER)
        c.create_text(int(12 * s), top + int(13 * s), anchor="w", text="SETTINGS",
                      fill=self.FG_HEADER, font=self.f(7, "bold"))

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
        self.draw_picker(label_x, track_x0, track_x1, y, "Sound",
                         chime.label(self.sound), "sound")
        self.draw_button(value_x - int(44 * s), y - int(9 * s), int(44 * s),
                         "test", "sound_test", None, h=int(18 * s), icon="right")

        y += int(24 * s)
        self.draw_picker(label_x, track_x0, track_x1, y, "Theme",
                         themes.label(self.theme), "theme")

    def draw_picker(self, label_x, x0, x1, y, label, value, action):
        """A left/right chooser, shared by the sound and theme rows."""
        c = self.canvas
        s = self.scale
        arrow = int(14 * s)
        c.create_text(label_x, y, anchor="w", text=label, fill=self.FG_DIM,
                      font=self.f(8))
        self.draw_button(x0, y - int(9 * s), arrow, "",
                         action + "_prev", None, h=int(18 * s), icon="left")
        c.create_text((x0 + x1) // 2, y, anchor="center", text=value,
                      fill=self.FG, font=self.f(8))
        self.draw_button(x1 - arrow, y - int(9 * s), arrow, "",
                         action + "_next", None, h=int(18 * s), icon="right")

    def draw_slider(self, key, label_x, x0, x1, value_x, y, label, frac, text):
        c = self.canvas
        s = self.scale
        c.create_text(label_x, y, anchor="w", text=label, fill=self.FG_DIM,
                      font=self.f(8))
        c.create_line(x0, y, x1, y, fill=self.SLIDER_TRACK, width=max(2, int(3 * s)))
        hx = x0 + (x1 - x0) * max(0.0, min(1.0, frac))
        filled = mix(self.SLIDER_TRACK, self.BAR_OK, 0.85)
        c.create_line(x0, y, hx, y, fill=filled, width=max(2, int(3 * s)))
        r = max(4, int(5 * s))
        c.create_oval(hx - r, y - r, hx + r, y + r,
                      fill=self.SLIDER_KNOB, outline="")
        c.create_text(value_x, y, anchor="e", text=text, fill=self.FG,
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
        self.push_settings({"sound": self.sound})
        chime.play("green", self.volume, self.sound)
        self.draw()

    def collapse(self):
        """Shrink to a small always-on-top badge.

        The badge is the whole point: closing the panel used to end the
        process, and getting it back meant a terminal. Clicking the badge
        brings it straight back.
        """
        self.collapsed = True
        self.cfg["collapsed"] = True
        save_config(self.cfg)
        self.draw()

    def expand(self):
        self.collapsed = False
        self.cfg["collapsed"] = False
        save_config(self.cfg)
        self.draw()

    def worst_state(self):
        """The state the badge shows: whatever most wants your attention."""
        states = {s.get("state") for s in self.sessions if isinstance(s, dict)}
        for name in ("orange", "red", "green"):
            if name in states:
                return name
        # Only sessions we know nothing about: the badge stays dark rather
        # than claiming everything has finished.
        return "unknown" if states else "green"

    def draw_collapsed(self):
        c = self.canvas
        s = self.scale
        w, h = int(84 * s), int(26 * s)
        self.canvas.config(width=w, height=h)
        self.root.geometry(str(w) + "x" + str(h))
        c.create_polygon(round_rect(1, 1, w - 2, h - 2, max(4, int(6 * s))),
                         smooth=True, fill=self.CASE_BG, outline=self.CASE_EDGE)

        state = self.worst_state()
        urgent = self.any_urgent()
        r = max(3, int(4.5 * s))
        cy = h // 2
        for j, name in enumerate(ORDER):
            cx = int(13 * s) + j * int(13 * s)
            on, glow = self.LIGHTS[name]
            if name == state and self.sessions and self.connected:
                halo = r + 2
                if urgent and name == "orange":
                    # Breathing in step with the row it stands for.
                    phase = 0.5 - 0.5 * math.cos(
                        2 * math.pi * time.time() / URGENT_PERIOD)
                    halo = r + 2 + int(round(3 * s * phase))
                c.create_oval(cx - halo, cy - halo, cx + halo, cy + halo,
                              fill=glow, outline="")
                c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=on, outline="")
            else:
                c.create_oval(cx - r, cy - r, cx + r, cy + r,
                              fill=self.LAMP_OFF, outline="")

        c.create_text(w - int(10 * s), cy, anchor="e",
                      text=str(len(self.sessions)), fill=self.FG,
                      font=self.f(8, "bold"))

    def toggle_settings(self):
        self.settings_open = not self.settings_open
        self.draw()

    # --- change banners -----------------------------------------------------

    def banner_for(self, sid):
        """(text, state, alpha) for a session's banner, or None.

        A "needs you" banner has no expiry: it is the one message you must not
        miss, and four seconds is easy to miss. It stays until you open that
        session - by any route - or until the session moves on by itself.
        """
        entry = self.banners.get(sid)
        if not entry:
            return None
        text, state, started, sticky = entry
        age = time.time() - started
        if age < BANNER_FADE:
            return text, state, max(0.0, age / BANNER_FADE)
        if sticky:
            return text, state, 1.0
        if age >= BANNER_SECS:
            self.banners.pop(sid, None)
            return None
        if age > BANNER_SECS - BANNER_FADE:
            alpha = (BANNER_SECS - age) / BANNER_FADE
        else:
            alpha = 1.0
        return text, state, max(0.0, min(1.0, alpha))

    def retire_opened_banners(self):
        """Drop a sticky banner once its session's window is in front.

        Only runs while one is up, and only compares handles - the expensive
        process walk is cached per session.
        """
        sticky = [sid for sid, e in self.banners.items() if e[3]]
        if not sticky:
            return
        front = foreground_window()
        if not front:
            return
        for sid in sticky:
            if self.window_for(sid) == front:
                self.banners.pop(sid, None)
                self.flash_until.pop(sid, None)

    def window_for(self, sid):
        """The window hosting a session, resolved once and remembered."""
        cached = self._window_cache.get(sid)
        if cached:
            return cached
        s = self.session_by_id(sid)
        if not s:
            return 0
        hwnd = as_int(s.get("hwnd")) or resolve_window(as_int(s.get("pid")),
                                                       str(s.get("cwd") or ""))
        if hwnd:
            self._window_cache[sid] = hwnd
        return hwnd

    def draw_banner(self, top, banner):
        """Fades in over the row's own text - never changes the row height, so
        nothing shifts under the cursor while you are reading it."""
        c = self.canvas
        text, state, alpha = banner
        on, glow = self.LIGHTS.get(state, self.LIGHTS["green"])
        c.create_rectangle(
            self.TX - 6, top + 4, self.W - 6, top + self.RH - 4,
            fill=mix(self.BG, glow, 0.85 * alpha), outline="",
        )
        c.create_text(
            self.TX + 2, top + self.RH // 2, anchor="w",
            text=text, fill=mix(self.BG, on, alpha), font=self.f(8, "bold"),
        )

    # --- expanded detail ----------------------------------------------------

    def draw_detail(self, top, s):
        """The panel shown under an expanded row: what it is doing, and what
        it has spent."""
        c = self.canvas
        usage = s.get("usage")
        if not isinstance(usage, dict):
            usage = {}

        # Every offset in here is scaled. self.DH and the Focus button already
        # were, so leaving the text on fixed pixel steps ran it straight into
        # the button below 100%.
        sc = self.scale
        pad = int(14 * sc)
        height = self.detail_height(s)

        c.create_rectangle(
            1, top, self.W - 2, top + height, fill=self.DETAIL_BG, outline=""
        )
        c.create_line(int(12 * sc), top, self.W - int(12 * sc), top,
                      fill=self.BORDER)

        y = top + int(13 * sc)
        state = s.get("state", "green")

        # what it is doing right now
        tool = str(s.get("tool") or "")
        detail = str(s.get("detail") or "")
        if not self.connected:
            activity = "Not connected · last seen state"
        elif state == "unknown":
            activity = "Running · no events yet"
        elif state == "red":
            activity = "Working" + ("  ·  " + tool if tool else "")
        elif state == "orange":
            activity = detail or "Waiting for you"
        else:
            activity = "Idle"
        c.create_text(
            pad, y, anchor="w", text=activity[:34], fill=self.FG, font=self.f(8)
        )
        agents = as_int(usage.get("agents"))
        if agents:
            c.create_text(
                self.W - pad,
                y,
                anchor="e",
                text=str(agents) + " agent" + ("" if agents == 1 else "s"),
                fill=self.BAR_WARN,
                font=self.f(8),
            )

        # What the session is about, under what it is doing right now: Claude
        # Code's own title for the conversation, then the last thing you typed.
        for kind, line in self.detail_lines(s):
            y += int(14 * sc)
            c.create_text(
                pad, y, anchor="w", text=line,
                fill=self.FG_HEADER if kind == "title" else self.FG_DIM,
                font=self.f(8),
            )

        y += int(18 * sc)
        limit = as_int(usage.get("context_limit"))
        used = as_int(usage.get("context_tokens"))
        if limit > 0:
            frac = min(1.0, max(0.0, used / float(limit)))
            c.create_text(
                pad,
                y,
                anchor="w",
                text="Context  " + fmt_tokens(used) + " / " + fmt_tokens(limit),
                fill=self.FG_DIM,
                font=self.f(8),
            )
            c.create_text(
                self.W - pad,
                y,
                anchor="e",
                text="{:.0f}%".format(frac * 100),
                fill=self.FG_DIM,
                font=self.f(8),
            )
            y += int(14 * sc)
            bar_w = self.W - 2 * pad
            bar_h = max(3, int(5 * sc))
            colour = self.BAR_OK if frac < 0.75 else (self.BAR_WARN if frac < 0.9 else self.BAR_FULL)
            c.create_rectangle(
                pad, y, pad + bar_w, y + bar_h, fill=self.BAR_BG, outline=""
            )
            if frac > 0:
                c.create_rectangle(
                    pad, y, pad + max(2, int(bar_w * frac)), y + bar_h,
                    fill=colour, outline="",
                )
            y += int(16 * sc)

            c.create_text(
                pad,
                y,
                anchor="w",
                text="Out " + fmt_tokens(usage.get("tokens_out"))
                + "   In " + fmt_tokens(
                    as_int(usage.get("tokens_in"))
                    + as_int(usage.get("tokens_cache_write"))
                )
                + "   Cached " + fmt_tokens(usage.get("tokens_cache_read")),
                fill=self.FG_DIM,
                font=self.f(8),
            )
            y += int(16 * sc)
            bits = [str(usage.get("model_name") or usage.get("model") or "")]
            if as_int(s.get("pid")):
                bits.append("pid " + str(as_int(s.get("pid"))))
            turns = as_int(usage.get("turns"))
            if turns:
                bits.append(str(turns) + " turns")
            line = "  ·  ".join(b for b in bits if b)
            c.create_text(
                pad, y, anchor="w", text=line[:44], fill=self.FG_HEADER,
                font=self.f(8),
            )
        else:
            # No transcript reading yet: the session predates the hook upgrade.
            c.create_text(
                pad,
                y,
                anchor="w",
                text="No token data - restart this Claude session",
                fill=self.FG_HEADER,
                font=self.f(8),
            )
            y += int(16 * sc)
            cwd = str(s.get("cwd") or "")
            c.create_text(
                pad, y, anchor="w", text=cwd[-44:], fill=self.FG_HEADER,
                font=self.f(8),
            )

        width = int(120 * sc)
        self.draw_button(pad, top + height - int(26 * sc),
                         width, "Focus window", "focus", s)

    # --- vector icons -------------------------------------------------------
    #
    # The gear, the arrows and the close cross are drawn as canvas shapes, not
    # font glyphs. Tk does no per-glyph fallback, and the panel's Tk may have no
    # scalable font at all - conda's Tk is built without Xft, so on Linux it is
    # left with X11 bitmap fonts that carry none of ⚙ ▸ ▾ ◂ × and rendered them
    # blank or as a box. A shape always draws, and looks the same on every OS.

    def _icon_gear(self, cx, cy, r, colour, bg=None, teeth=8):
        """A settings cog: a toothed disc with a punched-out hub."""
        c = self.canvas
        r_out = float(r)
        r_in = r * 0.70
        hub = max(1.0, r * 0.34)
        tw = 2 * math.pi / teeth
        pts = []
        for i in range(teeth):
            a = i * tw
            for frac, rr in ((0.00, r_in), (0.12, r_out), (0.38, r_out), (0.50, r_in)):
                ang = a + frac * tw
                pts += [cx + rr * math.cos(ang), cy + rr * math.sin(ang)]
        c.create_polygon(pts, fill=colour, outline="", joinstyle="round")
        c.create_oval(cx - hub, cy - hub, cx + hub, cy + hub,
                      fill=bg or self.BG, outline="")

    def _icon_close(self, cx, cy, r, colour):
        c = self.canvas
        w = max(1, int(round(r * 0.34)))
        c.create_line(cx - r, cy - r, cx + r, cy + r,
                      fill=colour, width=w, capstyle="round")
        c.create_line(cx - r, cy + r, cx + r, cy - r,
                      fill=colour, width=w, capstyle="round")

    def _icon_tri(self, cx, cy, r, colour, direction="right", fill=True):
        """A little triangle: row disclosure, the sound/theme pickers, ▷ test."""
        if direction == "left":
            p = [cx + r * 0.7, cy - r, cx - r * 0.9, cy, cx + r * 0.7, cy + r]
        elif direction == "down":
            p = [cx - r, cy - r * 0.7, cx + r, cy - r * 0.7, cx, cy + r * 0.9]
        else:  # right
            p = [cx - r * 0.7, cy - r, cx + r * 0.9, cy, cx - r * 0.7, cy + r]
        if fill:
            self.canvas.create_polygon(p, fill=colour, outline="")
        else:
            self.canvas.create_polygon(p, fill="", outline=colour,
                                       width=max(1, int(round(r * 0.30))),
                                       joinstyle="round")

    def draw_button(self, x, y, w, label, action, s, h=None, icon=None):
        c = self.canvas
        h = h or max(14, int(18 * self.scale))
        sid = s.get("session_id") if isinstance(s, dict) else None
        hot = self._hover_button == (action, sid)
        c.create_rectangle(
            x, y, x + w, y + h,
            fill=self.BTN_HOVER if hot else self.BTN_BG,
            outline=self.BORDER,
        )
        colour = self.FG if hot else self.FG_DIM
        cy = y + h // 2
        if icon and not label:
            # An icon-only button: the sound/theme picker arrows.
            self._icon_tri(x + w // 2, cy, max(3, int(4 * self.scale)),
                           colour, icon)
        elif icon:
            # Icon then label, side by side: the "▷ test" button.
            r = max(3, int(4 * self.scale))
            self._icon_tri(x + int(11 * self.scale), cy, r, colour, icon)
            c.create_text(x + int(11 * self.scale) + r + int(4 * self.scale), cy,
                          anchor="w", text=label, fill=colour, font=self.f(8))
        else:
            c.create_text(x + w // 2, cy, text=label, fill=colour, font=self.f(8))
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
            if key == "volume":
                # One push when the drag ends, not one per pixel of travel.
                self.push_settings({"volume": self.volume})
                if self.volume > 0:
                    chime.play("green", self.volume, self.sound)
            return
        if self._dragging:
            self.cfg["pos"] = [self.root.winfo_x(), self.root.winfo_y()]
            save_config(self.cfg)
            self._press = None
            self._dragging = False
            return

        if self.collapsed:
            self._press = None
            self._dragging = False
            self.expand()
            return

        if ev.y < self.HH:
            if ev.x > self.W - int(24 * self.scale):
                # Minimise, not quit. Quitting from here left no way back
                # without a terminal; "Quit" is on the right-click menu.
                self._press = None
                self.collapse()
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
            elif action == "theme_prev":
                self.cycle_theme(-1)
            elif action == "theme_next":
                self.cycle_theme(1)
            elif action == "sound_test":
                chime.play("green", self.volume, self.sound)
            elif action == "focus":
                self.open_session(s)
            else:
                row = self.row_at(ev.y)
                if row:
                    self.toggle_expanded(row)
        self._press = None
        self._dragging = False

    def toggle_expanded(self, s):
        sid = s.get("session_id")
        entry = self.banners.get(sid)
        if entry and entry[1] == "green":
            # You have looked at it. A "needs you" banner is deliberately not
            # cleared here: that one waits until you open the session itself.
            self.banners.pop(sid, None)
            self.flash_until.pop(sid, None)
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
        sid = s.get("session_id")
        if hwnd:
            self._window_cache[sid] = hwnd
        if ok:
            self.banners.pop(sid, None)
            self.flash_until.pop(sid, None)
            title = window_title(hwnd)
            self.toast(title[:40] if title else "focused")
        elif hwnd:
            self.toast("raised window (could not steal focus)")
        else:
            self.toast("window not found - session may have moved")

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

    def managed(self):
        """True when this panel was staged onto the host by the container and
        is kept running by the hooks, rather than started by hand."""
        if desktop_panel is None:
            return False
        try:
            return os.path.exists(desktop_panel.PANEL)
        except Exception:
            return False

    def quit(self):
        """Quit means quit.

        The hook starts the panel when the container has staged it, so without
        this a deliberate quit would last until the next Claude event. The
        stamp stands until the next `docker compose up` writes a later one.
        Collapsing to the badge is the everyday gesture - that is remembered by
        itself, and an auto-started panel comes back exactly as you left it.
        """
        if desktop_panel is not None:
            try:
                desktop_panel.write_quit_stamp()
                desktop_panel.clear_lock()
            except Exception:
                pass
        self.feed.stopped.set()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    Panel().run()
