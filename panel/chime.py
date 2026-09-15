"""Alert sounds for the panel - six appliances to choose from.

Each sound has two variants:

    green   "done"      - the finished sound, left to ring
    orange  "needs you" - shorter, usually doubled, so the two never blur

Everything is synthesised from partial sets rather than shipped as audio
files. What separates a ding from a beep is mostly inharmonicity: a struck
metal bar rings at ~1 : 2.76 : 5.40 : 8.93 times its fundamental and the
upper modes die away first, whereas a digital beep is a plain harmonic stack
with a flat envelope. Wood (marimba) is different again - 1 : 3.9 : 9.2.

Playback is delegated to :mod:`desktop`, which knows how to get a WAV heard on
each platform - notably that winsound refuses SND_MEMORY together with
SND_ASYNC, so the buffer has to reach a file first.
"""

import io
import math
import random
import struct
import wave

SAMPLE_RATE = 44100

# (frequency ratio, amplitude, decay multiplier)
VOICES = {
    # A struck metal bar - an oven timer, a chime bar.
    "bar": (
        (1.000, 1.00, 1.00),
        (2.756, 0.52, 0.55),
        (5.404, 0.24, 0.33),
        (8.933, 0.11, 0.21),
        (13.34, 0.05, 0.14),
    ),
    # A small dome bell: brighter, tighter partials, quick sparkle on top.
    "bell": (
        (1.00, 1.00, 1.00),
        (2.40, 0.62, 0.60),
        (4.50, 0.30, 0.38),
        (6.62, 0.16, 0.24),
        (9.40, 0.07, 0.15),
    ),
    # Odd harmonics only, all decaying together: a cheap piezo buzzer.
    "beep": (
        (1.0, 1.00, 1.0),
        (3.0, 0.30, 1.0),
        (5.0, 0.13, 1.0),
        (7.0, 0.06, 1.0),
    ),
    # A marimba bar is tuned to 1 : 4 : 10, which is why it sounds warm and
    # wooden rather than metallic.
    "wood": (
        (1.00, 1.00, 1.00),
        (3.93, 0.36, 0.45),
        (9.22, 0.12, 0.25),
    ),
    # Thin, high, and gone almost immediately.
    "glass": (
        (1.00, 1.00, 1.00),
        (2.71, 0.45, 0.42),
        (5.18, 0.20, 0.26),
        (8.80, 0.08, 0.16),
    ),
}

ATTACK = 0.0022
STRIKE_NOISE_LEN = 0.004

# strikes are (offset seconds, frequency scale, gain)
SOUNDS = {
    "oven": {
        "label": "Oven ding",
        "voice": "bar", "noise": 0.05,
        "green": {"freq": 1244.51, "decay": 0.62, "ring": 2.0,
                  "strikes": ((0.0, 1.0, 1.0),)},
        "orange": {"freq": 932.33, "decay": 0.30, "ring": 0.85,
                   "strikes": ((0.0, 1.0, 1.0), (0.17, 1.0, 0.85))},
    },
    "microwave": {
        "label": "Microwave",
        "voice": "beep", "noise": 0.0,
        "green": {"freq": 1046.50, "decay": 0.02, "ring": 0.14, "gate": 0.085,
                  "strikes": ((0.0, 1.0, 1.0), (0.17, 1.0, 1.0), (0.34, 1.0, 1.0))},
        "orange": {"freq": 880.00, "decay": 0.02, "ring": 0.14, "gate": 0.075,
                   "strikes": ((0.0, 1.0, 1.0), (0.17, 1.0, 0.9))},
    },
    "deskbell": {
        "label": "Desk bell",
        "voice": "bell", "noise": 0.08,
        "green": {"freq": 2093.00, "decay": 0.45, "ring": 1.5,
                  "strikes": ((0.0, 1.0, 1.0),)},
        "orange": {"freq": 1760.00, "decay": 0.26, "ring": 0.8,
                   "strikes": ((0.0, 1.0, 1.0), (0.15, 1.0, 0.8))},
    },
    "timer": {
        "label": "Timer bell",
        "voice": "bell", "noise": 0.10,
        # A wind-up timer is a clapper bouncing off the dome: rapid strikes,
        # each slightly detuned, which is what makes it rattle rather than ring.
        "green": {"freq": 1568.00, "decay": 0.22, "ring": 0.9,
                  "strikes": ((0.00, 1.000, 1.00), (0.075, 1.006, 0.92),
                              (0.150, 0.994, 0.84), (0.225, 1.004, 0.74),
                              (0.300, 0.997, 0.62))},
        "orange": {"freq": 1396.91, "decay": 0.18, "ring": 0.6,
                   "strikes": ((0.00, 1.000, 1.00), (0.075, 1.005, 0.85))},
    },
    "marimba": {
        "label": "Marimba",
        "voice": "wood", "noise": 0.02,
        "green": {"freq": 523.25, "decay": 0.34, "ring": 1.1,
                  "strikes": ((0.0, 1.0, 1.0), (0.13, 1.4983, 0.92))},
        "orange": {"freq": 587.33, "decay": 0.28, "ring": 0.9,
                   "strikes": ((0.0, 1.3348, 1.0), (0.13, 1.0, 0.92))},
    },
    "glass": {
        "label": "Glass tink",
        "voice": "glass", "noise": 0.03,
        "green": {"freq": 3135.96, "decay": 0.20, "ring": 0.7,
                  "strikes": ((0.0, 1.0, 1.0),)},
        "orange": {"freq": 2637.02, "decay": 0.14, "ring": 0.5,
                   "strikes": ((0.0, 1.0, 1.0), (0.11, 1.0, 0.85))},
    },
}

ORDER = ("oven", "microwave", "deskbell", "timer", "marimba", "glass")
DEFAULT = "oven"

# Kept for the oven-specific tests and as the reference voice.
BAR_MODES = VOICES["bar"]
DINGS = {"green": SOUNDS["oven"]["green"], "orange": SOUNDS["oven"]["orange"]}


def label(sound):
    return SOUNDS.get(sound, SOUNDS[DEFAULT])["label"]


def spec_for(kind, sound=DEFAULT):
    entry = SOUNDS.get(sound, SOUNDS[DEFAULT])
    spec = entry["green"] if kind != "orange" else entry["orange"]
    return entry, spec


def duration(kind, sound=DEFAULT):
    _entry, spec = spec_for(kind, sound)
    return max(o for o, _f, _g in spec["strikes"]) + spec["ring"]


def _render_tone(spec, voice, noise):
    """Synthesise one sound, normalised to a peak of 1.0.

    Amplitude is deliberately NOT applied here: synthesis is the expensive part
    (~70ms of sin() per sound) and a 0-100 slider would otherwise mean
    re-rendering the whole thing for every nudge.
    """
    ring = spec["ring"]
    decay = spec["decay"]
    gate = spec.get("gate")
    mode_sum = sum(a for _r, a, _d in voice)

    total = max(o for o, _f, _g in spec["strikes"]) + ring
    count = int(SAMPLE_RATE * total)
    buf = [0.0] * count
    length = int(SAMPLE_RATE * ring)
    noise_len = int(SAMPLE_RATE * STRIKE_NOISE_LEN)

    for offset_s, freq_scale, gain in spec["strikes"]:
        offset = int(SAMPLE_RATE * offset_s)
        freq = spec["freq"] * freq_scale
        # Seeded per strike so rendering is deterministic: buffers are cached
        # and compared by tests.
        rng = random.Random(0x0DDBA11)
        for i in range(length):
            idx = offset + i
            if idx >= count:
                break
            t = i / SAMPLE_RATE
            value = 0.0
            for ratio, amp, decay_mult in voice:
                if gate is not None:
                    # A beep holds flat, then releases fast.
                    env = 1.0 if t < gate else math.exp(-(t - gate) / decay)
                else:
                    env = math.exp(-t / (decay * decay_mult))
                if env < 1e-4:
                    continue
                value += amp * env * math.sin(2.0 * math.pi * freq * ratio * t)
            value /= mode_sum
            if noise and i < noise_len:
                value += rng.uniform(-1.0, 1.0) * noise * (1.0 - i / noise_len)
            if t < ATTACK:
                value *= t / ATTACK
            buf[idx] += value * gain

    peak = max(abs(v) for v in buf) or 1.0
    return [v / peak for v in buf]


def _encode(tone, amplitude):
    """Scale a normalised tone and wrap it as a 16-bit mono WAV."""
    scale = amplitude * 32767.0
    frames = bytearray()
    for v in tone:
        s = int(v * scale)
        frames += struct.pack("<h", max(-32768, min(32767, s)))

    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(bytes(frames))
    return out.getvalue()


# Volume is a 0-100 slider. 0 is silent; 100 is as loud as this will go.
MAX_AMPLITUDE = 0.55
VOLUME_STEP = 5      # buffers are cached per step, not per integer

# What the old named levels map to, so an existing config still makes sense.
LEGACY_LEVELS = {"quiet": 35, "normal": 60, "loud": 100}
_LEVELS = LEGACY_LEVELS  # kept under the old name for callers that used it
DEFAULT_VOLUME = 60


def as_volume(value):
    """Accept a 0-100 number, or one of the old names. Never raises."""
    if isinstance(value, str):
        key = value.strip().lower()
        if key in LEGACY_LEVELS:
            return LEGACY_LEVELS[key]
        try:
            value = float(key)
        except ValueError:
            return DEFAULT_VOLUME
    try:
        v = int(round(float(value)))
    except (TypeError, ValueError):
        return DEFAULT_VOLUME
    return max(0, min(100, v))


def amplitude_for(volume):
    """Slider position -> peak amplitude.

    Squared rather than linear: perceived loudness runs roughly with the
    square root of amplitude, so a linear slider spends most of its travel
    sounding equally loud and only goes quiet right at the bottom.
    """
    v = as_volume(volume)
    if v <= 0:
        return 0.0
    return MAX_AMPLITUDE * (v / 100.0) ** 2


_tones = {}
_cache = {}


def _tone(kind, sound=DEFAULT):
    key = (kind if kind == "orange" else "green", sound)
    if key not in _tones:
        entry, spec = spec_for(kind, sound)
        _tones[key] = _render_tone(
            spec,
            VOICES.get(entry["voice"], VOICES["bar"]),
            entry.get("noise", 0.0),
        )
    return _tones[key]


def _wav(kind, level=DEFAULT_VOLUME, sound=DEFAULT):
    v = as_volume(level)
    step = int(round(v / float(VOLUME_STEP))) * VOLUME_STEP
    key = (kind if kind == "orange" else "green", sound, step)
    if key not in _cache:
        _cache[key] = _encode(_tone(kind, sound), amplitude_for(step))
    return _cache[key]


def play(kind, level=DEFAULT_VOLUME, sound=DEFAULT):
    """Play the alert for 'orange' or 'green'. Never raises.

    Returns True if playback actually started. A volume of 0 is silence by
    request, so it returns False without touching the audio device.
    """
    if as_volume(level) <= 0:
        return False
    try:
        import desktop

        return desktop.play_wav(
            _wav(kind, level, sound),
            key=sound + "-" + kind + "-" + str(as_volume(level)),
        )
    except Exception:
        return False


if __name__ == "__main__":
    import time

    for name in ORDER:
        for kind in ("green", "orange"):
            print("%-10s %-7s %s" % (name, kind, label(name)))
            play(kind, DEFAULT_VOLUME, name)
            time.sleep(duration(kind, name) + 0.5)
        time.sleep(0.4)
