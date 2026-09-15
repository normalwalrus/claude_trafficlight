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


def test_the_colour_safe_theme_avoids_the_red_green_pairing():
    """Red/green is the worst possible pairing for the commonest forms of
    colour blindness. This theme separates the lamps by hue *and* brightness."""
    t = themes.get("colourblind")
    lums = [_luminance(t["lights"][k][0]) for k in ("red", "orange", "green")]
    for i in range(len(lums)):
        for j in range(i + 1, len(lums)):
            ok(abs(lums[i] - lums[j]) > 0.12,
               "colour-safe lamps %d and %d are too close in brightness "
               "(%.2f vs %.2f)" % (i, j, lums[i], lums[j]))

    def channels(colour):
        return (int(colour[1:3], 16), int(colour[3:5], 16), int(colour[5:7], 16))

    working = channels(t["lights"]["red"][0])
    done = channels(t["lights"]["green"][0])
    ok(working[2] > working[0], "the working lamp should be blue-dominant")
    ok(done[2] >= done[0], "the done lamp must not be red-dominant")


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
