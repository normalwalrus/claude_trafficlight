"""Chime tests: the synthesised WAV buffers must be valid and in range."""

import io
import struct
import time
import wave

from harness import eq, ok

import chime

KINDS = ("orange", "green")
# Volume is a 0-100 slider; the old names still resolve, so both are covered.
LEVELS = (35, 60, 100)
LEGACY_NAMES = ("quiet", "normal", "loud")


def samples(kind, level):
    data = chime._wav(kind, level)
    with wave.open(io.BytesIO(data), "rb") as w:
        meta = (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes())
        raw = w.readframes(meta[3])
    return data, meta, struct.unpack("<%dh" % meta[3], raw)


def test_every_kind_and_level_is_a_valid_riff_wave():
    for kind in KINDS:
        for level in LEVELS:
            data, (ch, sw, rate, n), _ = samples(kind, level)
            eq(data[:4], b"RIFF", "%s/%s magic" % (kind, level))
            eq(data[8:12], b"WAVE", "%s/%s format" % (kind, level))
            eq(ch, 1, "channels")
            eq(sw, 2, "sample width")
            eq(rate, chime.SAMPLE_RATE, "sample rate")
            ok(n > 0, "no frames")


def test_amplitude_matches_the_requested_level_without_clipping():
    for kind in KINDS:
        for level in LEVELS:
            _, _, s = samples(kind, level)
            peak = max(abs(v) for v in s)
            want = chime.amplitude_for(level) * 32767
            ok(abs(peak - want) <= 2,
               "%s/%s peak %d, want ~%d" % (kind, level, peak, want))
            ok(not any(v <= -32768 or v >= 32767 for v in s), "clipped samples")
            ok(all(v == v for v in s), "NaN leaked into the buffer")


def test_louder_levels_really_are_louder():
    for kind in KINDS:
        peaks = [max(abs(v) for v in samples(kind, lvl)[2]) for lvl in LEVELS]
        ok(peaks[0] < peaks[1] < peaks[2], "%s levels not monotonic: %r" % (kind, peaks))


def test_the_onset_is_a_clean_strike():
    """A ding is supposed to hit hard, so the old "stay quiet for 1ms" rule no
    longer applies - that suited the two-note bell it replaced. What still has
    to hold is starting from silence: a waveform that begins at a non-zero
    value steps the speaker cone and pops."""
    for kind in KINDS:
        _, (_, _, rate, n), s = samples(kind, "loud")
        peak = max(abs(v) for v in s)
        eq(s[0], 0, "%s must start at silence" % kind)

        quarter = max(1, int(rate * chime.ATTACK / 4))
        ok(max(abs(v) for v in s[:quarter]) < peak * 0.5,
           "%s: the attack ramp is not being applied" % kind)

        peak_at = max(range(n), key=lambda i: abs(s[i])) / float(rate)
        ok(peak_at < 0.05,
           "%s peaks at %.3fs - a strike should be immediate" % (kind, peak_at))


def test_orange_and_green_are_different_sounds():
    ok(chime._wav("orange", "normal") != chime._wav("green", "normal"))


def test_notes_have_the_expected_duration():
    for kind in KINDS:
        _, (_, _, rate, n), _ = samples(kind, "normal")
        want = chime.duration(kind)
        ok(abs(n / rate - want) < 0.01,
           "%s duration %.3fs, want %.3f" % (kind, n / rate, want))


def test_green_rings_longer_than_orange():
    """Done is one strike left to ring; needs-you is two short taps."""
    ok(chime.duration("green") > chime.duration("orange"),
       "the done ding should ring on")
    eq(len(chime.DINGS["green"]["strikes"]), 1, "one strike for done")
    eq(len(chime.DINGS["orange"]["strikes"]), 2, "two taps for needs-you")


def test_the_partials_are_inharmonic():
    """A struck bar rings at ~1 : 2.76 : 5.40 : 8.93, not 1 : 2 : 3.

    Harmonic ratios here would mean the synthesis had drifted back to sounding
    like a tone generator rather than a piece of metal.
    """
    ratios = [r for r, _a, _d in chime.BAR_MODES]
    eq(ratios[0], 1.0, "fundamental first")

    # Against the harmonic series 1, 2, 3, 4, 5 that a tone generator would
    # produce. (Individual bar modes can still land near *some* integer -
    # 8.933 is genuinely close to 9 - so comparing each to its nearest integer
    # would be testing physics, not the code.)
    for i, ratio in enumerate(ratios[1:], start=2):
        ok(abs(ratio - i) > 0.5,
           "mode %d at %.3f is the harmonic series, not a struck bar" % (i, ratio))

    # The two loudest upper modes carry the character; they must be clearly
    # off-integer or it stops sounding like metal.
    for ratio in ratios[1:3]:
        ok(abs(ratio - round(ratio)) > 0.2,
           "ratio %.3f is too close to a harmonic" % ratio)


def test_upper_modes_die_away_first():
    """What makes it read as struck metal rather than a sustained chord."""
    decays = [d for _r, _a, d in chime.BAR_MODES]
    for i in range(1, len(decays)):
        ok(decays[i] < decays[i - 1],
           "mode %d should decay faster than mode %d" % (i, i - 1))


def test_rendering_is_deterministic():
    """The strike transient uses noise; an unseeded source would make the
    cached buffer differ from a freshly rendered one."""
    entry = chime.SOUNDS["oven"]
    voice = chime.VOICES[entry["voice"]]
    first = chime._render_tone(entry["green"], voice, entry["noise"])
    second = chime._render_tone(entry["green"], voice, entry["noise"])
    eq(first, second, "two renders of the same ding must be identical")


def test_rendering_is_cached_so_it_never_stalls_the_ui_twice():
    chime._cache.pop(("green", "loud"), None)
    t0 = time.time()
    chime._wav("green", "loud")
    cold = time.time() - t0
    t0 = time.time()
    chime._wav("green", "loud")
    warm = time.time() - t0
    ok(cold < 1.0, "a cold render blocks the tk main loop for %.2fs" % cold)
    ok(warm < cold / 10 + 0.001, "cache miss on the second call")


def test_unknown_kind_and_level_fall_back_instead_of_raising():
    data = chime._wav("nonsense", "nonsense")
    eq(data[:4], b"RIFF")
    chime.play("nonsense", "nonsense")  # must not raise
    chime.play("green", None)


# --- the volume slider -------------------------------------------------------


def test_volume_is_a_0_to_100_scale():
    eq(chime.as_volume(0), 0)
    eq(chime.as_volume(100), 100)
    eq(chime.as_volume(42), 42)
    eq(chime.as_volume("72"), 72, "numeric strings are accepted")
    eq(chime.as_volume(-50), 0, "clamped at the bottom")
    eq(chime.as_volume(500), 100, "clamped at the top")
    for junk in (None, "", "nonsense", [1], {}, object()):
        eq(chime.as_volume(junk), chime.DEFAULT_VOLUME,
           "junk %r should fall back" % (junk,))


def test_the_old_named_levels_still_resolve():
    """An existing panel.json has "normal" in it, not a number."""
    for name in LEGACY_NAMES:
        ok(0 < chime.as_volume(name) <= 100, name)
    ok(chime.as_volume("quiet") < chime.as_volume("normal")
       < chime.as_volume("loud"), "the old order is preserved")


def test_zero_is_silence_and_plays_nothing():
    eq(chime.amplitude_for(0), 0.0, "no amplitude at all")
    s = _samples(chime._wav("green", 0))
    eq(max(abs(v) for v in s), 0, "every sample is zero")
    eq(chime.play("green", 0), False, "muted must not touch the audio device")


def test_loudness_rises_monotonically_with_the_slider():
    peaks = []
    for v in range(0, 101, 10):
        s = _samples(chime._wav("green", v))
        peaks.append(max(abs(x) for x in s))
    for i in range(1, len(peaks)):
        ok(peaks[i] > peaks[i - 1],
           "volume %d is not louder than %d: %r" % (i * 10, (i - 1) * 10, peaks))
    eq(peaks[0], 0, "0 is silent")


def test_the_curve_is_tapered_not_linear():
    """A linear slider spends most of its travel sounding equally loud."""
    half = chime.amplitude_for(50)
    full = chime.amplitude_for(100)
    ok(half < full * 0.4,
       "50%% should be well below half the amplitude of 100%%: %.3f vs %.3f"
       % (half, full))


def test_changing_volume_does_not_resynthesise():
    """Synthesis is ~70ms; a slider must only re-encode."""
    chime._tones.clear()
    chime._cache.clear()
    t0 = time.time()
    chime._wav("green", 50)
    cold = time.time() - t0
    t0 = time.time()
    chime._wav("green", 85)
    warm = time.time() - t0
    ok(warm < cold, "a new volume should reuse the synthesised tone "
                    "(cold %.3fs, new volume %.3fs)" % (cold, warm))
    eq(len(chime._tones), 1, "the tone is synthesised once")


# --- the six-sound set -------------------------------------------------------


def test_there_are_six_selectable_sounds():
    eq(len(chime.ORDER), 6, "six sounds")
    for name in chime.ORDER:
        ok(name in chime.SOUNDS, "missing sound: " + name)
        entry = chime.SOUNDS[name]
        ok(entry["label"], "every sound needs a label")
        ok(entry["voice"] in chime.VOICES, "unknown voice for " + name)
        for kind in ("green", "orange"):
            spec = entry[kind]
            ok(spec["strikes"], "%s/%s has no strikes" % (name, kind))
            ok(spec["ring"] > 0, "%s/%s has no ring" % (name, kind))


def test_every_sound_renders_valid_audio():
    for name in chime.ORDER:
        for kind in ("green", "orange"):
            data = chime._wav(kind, "normal", name)
            eq(data[:4], b"RIFF", "%s/%s not RIFF" % (name, kind))
            eq(data[8:12], b"WAVE", "%s/%s not WAVE" % (name, kind))
            s = _samples(data)
            eq(s[0], 0, "%s/%s must start at silence" % (name, kind))
            peak = max(abs(v) for v in s)
            ok(peak > 1000, "%s/%s is near-silent" % (name, kind))
            ok(not any(abs(v) >= 32767 for v in s),
               "%s/%s clips" % (name, kind))


def _samples(data):
    with wave.open(io.BytesIO(data)) as w:
        n = w.getnframes()
        return struct.unpack("<%dh" % n, w.readframes(n))


def test_all_twelve_variants_are_distinct():
    """Picking a different sound has to actually change what you hear."""
    seen = {}
    for name in chime.ORDER:
        for kind in ("green", "orange"):
            data = chime._wav(kind, "normal", name)
            key = (name, kind)
            for other, blob in seen.items():
                ok(blob != data, "%r and %r are identical audio" % (other, key))
            seen[key] = data
    eq(len(seen), 12, "twelve variants")


def test_done_and_needs_you_differ_within_every_sound():
    for name in chime.ORDER:
        ok(chime._wav("green", "normal", name) != chime._wav("orange", "normal", name),
           "%s: done and needs-you sound the same" % name)


def test_an_unknown_sound_falls_back_instead_of_raising():
    fallback = chime._wav("green", "normal", "no-such-sound")
    eq(fallback, chime._wav("green", "normal", chime.DEFAULT), "falls back to default")
    eq(chime.label("no-such-sound"), chime.label(chime.DEFAULT))
    ok(chime.duration("green", "no-such-sound") > 0)


def test_a_beep_is_harmonic_and_a_ding_is_not():
    """The voices have to actually differ, or all six would sound the same."""
    bar = [r for r, _a, _d in chime.VOICES["bar"]]
    beep = [r for r, _a, _d in chime.VOICES["beep"]]
    for ratio in beep[1:]:
        eq(ratio, round(ratio), "a beep is a plain harmonic stack")
    ok(any(abs(r - round(r)) > 0.2 for r in bar[1:]),
       "a struck bar must be inharmonic")
    wood = [r for r, _a, _d in chime.VOICES["wood"]]
    ok(abs(wood[1] - 4) < 0.2, "a marimba bar's second mode sits near 4x")


def test_the_gated_voices_hold_flat_before_releasing():
    """A microwave beep is flat-topped; an exponential decay would make it a
    ding instead."""
    spec = chime.SOUNDS["microwave"]["green"]
    ok("gate" in spec, "the microwave beep needs a gate")
    s = _samples(chime._wav("green", "loud", "microwave"))
    rate = chime.SAMPLE_RATE
    early = max(abs(v) for v in s[int(rate * 0.01):int(rate * 0.02)])
    late = max(abs(v) for v in s[int(rate * 0.06):int(rate * 0.075)])
    ok(late > early * 0.6, "the beep should still be near full at the gate end")
