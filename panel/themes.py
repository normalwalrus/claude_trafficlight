"""Colour themes for both panels.

One definition per theme, used by the desktop panel directly and exported to
`server/static/themes.json` for the browser panel, so the two never drift.

Every theme must define every token: a missing one would leave a widget drawn
in whatever the previous theme used. `validate()` enforces that, and the test
suite calls it.

Token groups:

    panel     bg, bg_hover, border, fg, fg_dim, fg_header
    surfaces  detail_bg, settings_bg, menu_bg, menu_active
    signal    case_bg, case_edge, bezel, lamp_off, lens
    lamps     red/orange/green, each (lit colour, halo colour)
    widgets   bar_bg, bar_ok, bar_warn, bar_full, btn_bg, btn_hover,
              slider_track, slider_knob, badge_bg, badge_fg

The lamp halo is what the lit lamp's glow is drawn in, and it doubles as the
banner background - it has to be dark enough (or light enough) that the banner
text still reads against it.
"""

TOKENS = (
    "bg", "bg_hover", "border", "fg", "fg_dim", "fg_header",
    "detail_bg", "settings_bg", "menu_bg", "menu_active",
    "case_bg", "case_edge", "bezel", "lamp_off", "lens",
    "bar_bg", "bar_ok", "bar_warn", "bar_full",
    "btn_bg", "btn_hover", "slider_track", "slider_knob",
    "badge_bg", "badge_fg",
)

THEMES = {
    "midnight": {
        "label": "Midnight",
        "dark": True,
        "bg": "#15171c", "bg_hover": "#1d212a", "border": "#2b3038",
        "fg": "#e6e9ef", "fg_dim": "#7f8798", "fg_header": "#69707f",
        "detail_bg": "#111318", "settings_bg": "#101318",
        "menu_bg": "#1c2029", "menu_active": "#2b3140",
        "case_bg": "#0c0e12", "case_edge": "#353d4a", "bezel": "#2b323d",
        "lamp_off": "#191d24", "lens": "#ffffff",
        "lights": {
            "red": ["#ff5964", "#59222a"],
            "orange": ["#ffb03a", "#5a3f15"],
            "green": ["#3ddc84", "#175130"],
        },
        "bar_bg": "#22262f", "bar_ok": "#3ddc84", "bar_warn": "#ffb03a",
        "bar_full": "#ff5964", "btn_bg": "#232833", "btn_hover": "#2e3543",
        "slider_track": "#2a3039", "slider_knob": "#e6e9ef",
        "badge_bg": "#3a2f14", "badge_fg": "#ffb03a",
    },

    "daylight": {
        "label": "Daylight",
        "dark": False,
        "bg": "#f4f5f7", "bg_hover": "#e8eaee", "border": "#d0d4dc",
        "fg": "#1d2430", "fg_dim": "#5c6675", "fg_header": "#7a8494",
        "detail_bg": "#eceef2", "settings_bg": "#e9ebf0",
        "menu_bg": "#ffffff", "menu_active": "#dde1e8",
        # The housing stays dark even in a light theme: a lit lamp has to be
        # clearly brighter than an unlit one, and red and green are not very
        # bright colours to begin with.
        "case_bg": "#2b313d", "case_edge": "#1d222b", "bezel": "#3d4553",
        "lamp_off": "#232935", "lens": "#ffffff",
        "lights": {
            "red": ["#e5384a", "#f3b8bf"],
            "orange": ["#e08600", "#f7ddb3"],
            "green": ["#17914f", "#b6e3c9"],
        },
        "bar_bg": "#d6dae1", "bar_ok": "#17914f", "bar_warn": "#e08600",
        "bar_full": "#e5384a", "btn_bg": "#ffffff", "btn_hover": "#dde1e8",
        "slider_track": "#cdd2db", "slider_knob": "#2d3542",
        "badge_bg": "#f7ddb3", "badge_fg": "#8a5200",
    },

    "neon": {
        "label": "Neon",
        "dark": True,
        "bg": "#14101f", "bg_hover": "#1f1833", "border": "#3a2a5c",
        "fg": "#f2e9ff", "fg_dim": "#a294c4", "fg_header": "#8478a8",
        "detail_bg": "#0f0c18", "settings_bg": "#120e1d",
        "menu_bg": "#1c1630", "menu_active": "#33265c",
        "case_bg": "#0a0712", "case_edge": "#4a2f7a", "bezel": "#3a2a5c",
        "lamp_off": "#1b1430", "lens": "#ffffff",
        "lights": {
            "red": ["#ff2d78", "#5c0f33"],
            "orange": ["#ffb703", "#5a3d05"],
            "green": ["#25f4c3", "#0a5348"],
        },
        "bar_bg": "#241a3d", "bar_ok": "#25f4c3", "bar_warn": "#ffb703",
        "bar_full": "#ff2d78", "btn_bg": "#241a3d", "btn_hover": "#33265c",
        "slider_track": "#2c2049", "slider_knob": "#25f4c3",
        "badge_bg": "#4a3208", "badge_fg": "#ffb703",
    },

    "terminal": {
        "label": "Terminal",
        "dark": True,
        "bg": "#080a08", "bg_hover": "#101610", "border": "#1e2a1e",
        "fg": "#c8f7c0", "fg_dim": "#6f9c69", "fg_header": "#547a50",
        "detail_bg": "#050705", "settings_bg": "#070b07",
        "menu_bg": "#0d120d", "menu_active": "#1c2a1c",
        "case_bg": "#000000", "case_edge": "#254025", "bezel": "#1a2a1a",
        "lamp_off": "#0e160e", "lens": "#e8ffe4",
        "lights": {
            "red": ["#ff5f45", "#4a1a12"],
            "orange": ["#ffc24a", "#4a3610"],
            "green": ["#5dff8b", "#124a25"],
        },
        "bar_bg": "#142014", "bar_ok": "#5dff8b", "bar_warn": "#ffc24a",
        "bar_full": "#ff5f45", "btn_bg": "#0f1a0f", "btn_hover": "#1c2e1c",
        "slider_track": "#182618", "slider_knob": "#5dff8b",
        "badge_bg": "#3a2c0c", "badge_fg": "#ffc24a",
    },

    "colourblind": {
        "label": "Colour-safe",
        "dark": True,
        # Red/green is the worst possible pairing for the most common forms of
        # colour blindness. Position already disambiguates the lamps, but this
        # theme also separates them by hue AND brightness: deep blue, amber,
        # pale cyan - distinguishable under deuteranopia and protanopia.
        "bg": "#14181d", "bg_hover": "#1c222a", "border": "#2c343e",
        "fg": "#eef2f6", "fg_dim": "#8a95a3", "fg_header": "#6f7a88",
        "detail_bg": "#101418", "settings_bg": "#121720",
        "menu_bg": "#1a1f27", "menu_active": "#2a3340",
        "case_bg": "#0b0e12", "case_edge": "#39434f", "bezel": "#2c343e",
        "lamp_off": "#181d24", "lens": "#ffffff",
        "lights": {
            "red": ["#1f6bff", "#122d5c"],      # working
            "orange": ["#ffb000", "#5a3e00"],   # needs you
            "green": ["#7ce8ff", "#11454f"],    # done
        },
        "bar_bg": "#222a33", "bar_ok": "#7ce8ff", "bar_warn": "#ffb000",
        "bar_full": "#1f6bff", "btn_bg": "#222a33", "btn_hover": "#2f3a46",
        "slider_track": "#28313b", "slider_knob": "#eef2f6",
        "badge_bg": "#4a3300", "badge_fg": "#ffb000",
    },

    "sepia": {
        "label": "Sepia",
        "dark": False,
        "bg": "#f2ebdd", "bg_hover": "#e8dfcb", "border": "#d3c6ac",
        "fg": "#3b3228", "fg_dim": "#7a6c58", "fg_header": "#94856d",
        "detail_bg": "#ebe2d0", "settings_bg": "#e8dfcb",
        "menu_bg": "#fbf6ec", "menu_active": "#ded3bb",
        "case_bg": "#352d21", "case_edge": "#221c14", "bezel": "#4a4030",
        "lamp_off": "#2f2820", "lens": "#fff7e8",
        "lights": {
            "red": ["#c14330", "#eec3ba"],
            "orange": ["#c98a12", "#f0dcaf"],
            "green": ["#4f7a3a", "#cadfb9"],
        },
        "bar_bg": "#dbd0b8", "bar_ok": "#4f7a3a", "bar_warn": "#c98a12",
        "bar_full": "#c14330", "btn_bg": "#fbf6ec", "btn_hover": "#ded3bb",
        "slider_track": "#d3c6ac", "slider_knob": "#4a3f2f",
        "badge_bg": "#f0dcaf", "badge_fg": "#7a5406",
    },
}

ORDER = ("midnight", "daylight", "neon", "terminal", "colourblind", "sepia")
DEFAULT = "midnight"


def get(name):
    return THEMES.get(name, THEMES[DEFAULT])


def label(name):
    return get(name)["label"]


def validate():
    """Every theme defines every token. Returns a list of problems."""
    problems = []
    for name in ORDER:
        if name not in THEMES:
            problems.append("missing theme: " + name)
            continue
        theme = THEMES[name]
        for token in TOKENS:
            value = theme.get(token)
            if not isinstance(value, str) or not value.startswith("#") \
                    or len(value) != 7:
                problems.append("%s: bad or missing %r (%r)" % (name, token, value))
        lights = theme.get("lights")
        if not isinstance(lights, dict):
            problems.append(name + ": no lights")
            continue
        for lamp in ("red", "orange", "green"):
            pair = lights.get(lamp)
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                problems.append("%s: lights[%r] must be [lit, halo]" % (name, lamp))
                continue
            for colour in pair:
                if not isinstance(colour, str) or len(colour) != 7 \
                        or not colour.startswith("#"):
                    problems.append("%s: bad colour %r in %r" % (name, colour, lamp))
    extra = set(THEMES) - set(ORDER)
    if extra:
        problems.append("themes not listed in ORDER: %s" % sorted(extra))
    return problems


if __name__ == "__main__":
    issues = validate()
    print("\n".join(issues) if issues else "all %d themes valid" % len(ORDER))
