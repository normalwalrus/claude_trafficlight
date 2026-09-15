"""Theme tests.

A theme with a missing token leaves widgets drawn in whatever the previous
theme used, which is the sort of bug that only shows up after switching - so
completeness is checked mechanically rather than by eye.
"""

import json
import os

from harness import eq, ok

import themes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_every_theme_defines_every_token():
    problems = themes.validate()
    ok(not problems, "incomplete themes:\n  " + "\n  ".join(problems))


def test_there_are_six_themes_and_a_default():
    eq(len(themes.ORDER), 6, "six themes")
    ok(themes.DEFAULT in themes.ORDER, "the default must be selectable")
    labels = [themes.label(n) for n in themes.ORDER]
    eq(len(set(labels)), len(labels), "labels must be distinct: %r" % labels)


def test_an_unknown_theme_falls_back():
    eq(themes.get("no-such-theme"), themes.get(themes.DEFAULT))
    eq(themes.label("no-such-theme"), themes.label(themes.DEFAULT))


def test_themes_are_actually_different_from_each_other():
    seen = {}
    for name in themes.ORDER:
        t = themes.get(name)
        fingerprint = (t["bg"], t["fg"],
                       tuple(t["lights"]["red"]), tuple(t["lights"]["green"]))
        for other, prev in seen.items():
            ok(prev != fingerprint, "%s and %s are the same theme" % (other, name))
        seen[name] = fingerprint


def _luminance(colour):
    """Rough perceptual luminance, 0..1, for contrast checks."""
    r = int(colour[1:3], 16) / 255.0
    g = int(colour[3:5], 16) / 255.0
    b = int(colour[5:7], 16) / 255.0
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def test_text_is_readable_against_its_background():
    """The whole point of a theme is legibility; a dark-on-dark combination
    would make a row unreadable rather than merely ugly."""
    for name in themes.ORDER:
        t = themes.get(name)
        for fg_token in ("fg", "fg_dim"):
            gap = abs(_luminance(t[fg_token]) - _luminance(t["bg"]))
            ok(gap > 0.25,
               "%s: %s on bg has only %.2f luminance separation"
               % (name, fg_token, gap))


def test_the_dark_flag_matches_the_background():
    for name in themes.ORDER:
        t = themes.get(name)
        lum = _luminance(t["bg"])
        if t["dark"]:
            ok(lum < 0.35, "%s claims dark but bg luminance is %.2f" % (name, lum))
        else:
            ok(lum > 0.6, "%s claims light but bg luminance is %.2f" % (name, lum))


def test_lit_lamps_stand_out_from_an_unlit_one():
    for name in themes.ORDER:
        t = themes.get(name)
        off = _luminance(t["lamp_off"])
        for lamp in ("red", "orange", "green"):
            lit = _luminance(t["lights"][lamp][0])
            ok(abs(lit - off) > 0.15,
               "%s: the %s lamp barely differs from unlit (%.2f vs %.2f)"
               % (name, lamp, lit, off))


def test_the_three_lamps_are_distinguishable_within_each_theme():
    for name in themes.ORDER:
        t = themes.get(name)
        lit = [t["lights"][k][0] for k in ("red", "orange", "green")]
        eq(len(set(lit)), 3, "%s reuses a lamp colour: %r" % (name, lit))


def _hue(colour):
    """Hue in degrees, 0-360."""
    import colorsys
    r = int(colour[1:3], 16) / 255.0
    g = int(colour[3:5], 16) / 255.0
    b = int(colour[5:7], 16) / 255.0
    return colorsys.rgb_to_hsv(r, g, b)[0] * 360.0


def _saturation(colour):
    import colorsys
    r = int(colour[1:3], 16) / 255.0
    g = int(colour[3:5], 16) / 255.0
    b = int(colour[5:7], 16) / 255.0
    return colorsys.rgb_to_hsv(r, g, b)[1]


# A real traffic light is red, amber and green. A theme may change how bright
# or how saturated a lamp is, but not which colour it is.
HUE_BANDS = {
    "red": [(345, 360), (0, 20)],
    "orange": [(30, 55)],
    "green": [(90, 155)],
}


def test_every_lamp_is_a_true_red_amber_or_green():
    """No theme may recolour the lamps. Pink instead of red, or teal instead of
    green, stops it reading as a traffic light at a glance."""
    for name in themes.ORDER:
        lights = themes.get(name)["lights"]
        for lamp, bands in HUE_BANDS.items():
            colour = lights[lamp][0]
            hue = _hue(colour)
            ok(any(lo <= hue <= hi for lo, hi in bands),
               "%s: the %s lamp is %s (hue %.0f), which is not a %s"
               % (name, lamp, colour, hue, lamp))


def test_lamps_are_saturated_enough_to_read_as_a_colour():
    for name in themes.ORDER:
        lights = themes.get(name)["lights"]
        for lamp in ("red", "orange", "green"):
            colour = lights[lamp][0]
            ok(_saturation(colour) > 0.45,
               "%s: the %s lamp %s is too washed out to read as a colour"
               % (name, lamp, colour))


def test_the_high_contrast_theme_separates_the_lamps_by_brightness():
    """Since every theme now uses true red/amber/green, hue alone cannot help
    someone who cannot tell red from green. This theme spreads the lamps as far
    apart in brightness as those colours allow."""
    t = themes.get("contrast")
    lums = [_luminance(t["lights"][k][0]) for k in ("red", "orange", "green")]
    for i in range(len(lums)):
        for j in range(i + 1, len(lums)):
            ok(abs(lums[i] - lums[j]) > 0.12,
               "high-contrast lamps %d and %d are too close in brightness "
               "(%.2f vs %.2f)" % (i, j, lums[i], lums[j]))


def _mix(a, b, t):
    """The same blend panel.py draws fades with."""
    out = "#"
    for i in (1, 3, 5):
        va, vb = int(a[i:i + 2], 16), int(b[i:i + 2], 16)
        out += "%02x" % int(round(va + (vb - va) * t))
    return out


def test_a_banner_is_legible_against_its_own_halo():
    """The halo doubles as the change banner's background and the lit colour is
    the text drawn on it, so a theme can define a perfectly good lamp and still
    produce a banner nobody can read."""
    for name in themes.ORDER:
        t = themes.get(name)
        for lamp in ("red", "orange", "green"):
            lit, halo = t["lights"][lamp]
            # draw_banner fills with mix(bg, halo, 0.85) and writes in `lit`.
            background = _mix(t["bg"], halo, 0.85)
            gap = abs(_luminance(lit) - _luminance(background))
            ok(gap > 0.15,
               "%s: the %s banner writes %s on %s - only %.2f apart"
               % (name, lamp, lit, background, gap))


def test_a_renamed_theme_still_resolves():
    """An existing panel.json may still name a theme that has been renamed."""
    eq(themes.resolve("colourblind"), "contrast", "old name should map over")
    eq(themes.get("colourblind"), themes.get("contrast"))
    eq(themes.resolve("nonsense"), themes.DEFAULT, "junk falls back")
    eq(themes.resolve("neon"), "neon", "current names are untouched")


def test_the_exported_json_matches_the_python_definitions():
    """The browser panel reads themes.json; if it drifts from themes.py the two
    panels stop looking the same."""
    path = os.path.join(ROOT, "server", "static", "themes.json")
    ok(os.path.exists(path), "themes.json has not been exported")
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    eq(list(data["order"]), list(themes.ORDER), "order drifted")
    for name in themes.ORDER:
        exported = data["themes"][name]
        source = themes.get(name)
        for token in themes.TOKENS:
            eq(exported[token], source[token], "%s.%s drifted" % (name, token))
        for lamp in ("red", "orange", "green"):
            eq(list(exported["lights"][lamp]), list(source["lights"][lamp]),
               "%s.lights.%s drifted" % (name, lamp))
