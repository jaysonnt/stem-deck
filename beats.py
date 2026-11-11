"""
Beat kits for pianokb.

A kit turns the piano keyboard into a drum machine: one row of keys is the
drums, the other is the tuned parts of the loop (bass notes, chord stabs) in the
song's key. Everything is synthesised here in numpy — no sample files, no
downloads — so a kit weighs nothing and stays in tune with its song.

    KITS["never-see-me-again"]  ->  Kit(name, tempo, rows, samples)

`rows` is what the terminal prints (key char, label); `samples` is the matching
one-shot per key char, stereo float32 at SR, peak-normalised and short enough to
retrigger fast.
"""

import math
from dataclasses import dataclass, field

import numpy as np

SR = 48000          # must match piano.py


# --------------------------------------------------------------------------
# little DSP helpers
# --------------------------------------------------------------------------

def _t(dur: float) -> np.ndarray:
    return np.arange(int(dur * SR), dtype=np.float32) / SR


def _noise(n: int, seed: int) -> np.ndarray:
    return np.random.default_rng(seed).normal(0.0, 1.0, n).astype(np.float32)


def _highpass(x: np.ndarray, width: int = 8) -> np.ndarray:
    """x minus its short moving average: enough to thin hats and claps out."""
    kernel = np.ones(width, dtype=np.float32) / width
    smooth = np.convolve(x, kernel, mode="same").astype(np.float32)
    return x - smooth


def _stereo(mono: np.ndarray, peak: float, pan: float = 0.0) -> np.ndarray:
    mono = np.asarray(mono, dtype=np.float32)
    top = float(np.max(np.abs(mono))) or 1.0
    mono = mono * (peak / top)
    left = mono * (1.0 - pan)
    right = mono * (1.0 + pan)
    return np.stack([left, right], axis=1).astype(np.float32)


def _freq(midi: int) -> float:
    return 440.0 * 2.0 ** ((midi - 69) / 12.0)


def _sweep(f0: float, f1: float, drop: float, t: np.ndarray) -> np.ndarray:
    """Sine whose pitch falls from f0+f1 to f1 — the body of a kick or tom."""
    f = f1 + f0 * np.exp(-t / drop)
    phase = 2.0 * math.pi * np.cumsum(f) / SR
    return np.sin(phase).astype(np.float32)


# --------------------------------------------------------------------------
# drums
# --------------------------------------------------------------------------

def kick(decay: float = 0.16, top: float = 120.0, base: float = 48.0,
         drop: float = 0.035, seed: int = 1) -> np.ndarray:
    t = _t(decay * 3.2)
    env = (1.0 - np.exp(-t / 0.0012)) * np.exp(-t / decay)
    body = _sweep(top, base, drop, t) * env
    click = _highpass(_noise(len(t), seed)) * np.exp(-t / 0.004) * 0.35
    return _stereo(body + click, 0.60)


def eight08(midi: int = 30, decay: float = 0.45) -> np.ndarray:
    """Long sub kick tuned to a note, 808-style."""
    f = _freq(midi)
    t = _t(decay * 2.4)
    env = (1.0 - np.exp(-t / 0.0015)) * np.exp(-t / decay)
    body = np.sin(2.0 * math.pi * np.cumsum(
        f * (1.0 + 0.9 * np.exp(-t / 0.03))) / SR).astype(np.float32) * env
    return _stereo(np.tanh(body * 1.6), 0.62)


def snare(seed: int = 2) -> np.ndarray:
    t = _t(0.26)
    tone = np.sin(2.0 * math.pi * 188.0 * t) * np.exp(-t / 0.05) * 0.55
    noise = _highpass(_noise(len(t), seed)) * np.exp(-t / 0.10)
    return _stereo(tone + noise, 0.52)


def clap(seed: int = 3) -> np.ndarray:
    t = _t(0.32)
    n = _highpass(_noise(len(t), seed), 5)
    env = np.zeros_like(t)
    for off in (0.0, 0.009, 0.019):
        env += np.where(t >= off, np.exp(-(t - off) / 0.012), 0.0).astype(np.float32)
    env += np.where(t >= 0.028, np.exp(-(t - 0.028) / 0.09) * 0.5, 0.0).astype(np.float32)
    return _stereo(n * env, 0.48)


def hat(open_hat: bool = False, seed: int = 4) -> np.ndarray:
    t = _t(0.42 if open_hat else 0.12)
    n = _highpass(_noise(len(t), seed), 4)
    env = np.exp(-t / (0.28 if open_hat else 0.028))
    return _stereo(n * env, 0.30)


def rim(seed: int = 5) -> np.ndarray:
    t = _t(0.10)
    ping = np.sin(2.0 * math.pi * 420.0 * t) * np.exp(-t / 0.02)
    click = _highpass(_noise(len(t), seed), 4) * np.exp(-t / 0.004) * 0.5
    return _stereo(ping + click, 0.40)


def tom(midi: int = 45, seed: int = 6) -> np.ndarray:
    t = _t(0.55)
    env = (1.0 - np.exp(-t / 0.002)) * np.exp(-t / 0.18)
    body = _sweep(_freq(midi) * 1.6, _freq(midi), 0.03, t) * env
    skin = _highpass(_noise(len(t), seed), 6) * np.exp(-t / 0.03) * 0.15
    return _stereo(body + skin, 0.50)


def crash(seed: int = 7) -> np.ndarray:
    t = _t(1.7)
    n = _highpass(_noise(len(t), seed), 3)
    env = (1.0 - np.exp(-t / 0.002)) * np.exp(-t / 0.55)
    return _stereo(n * env, 0.28)


# --------------------------------------------------------------------------
# tuned parts
# --------------------------------------------------------------------------

def bass(midi: int, decay: float = 0.5) -> np.ndarray:
    f = _freq(midi)
    t = _t(decay * 2.2)
    env = (1.0 - np.exp(-t / 0.004)) * np.exp(-t / decay)
    sig = (np.sin(2.0 * math.pi * f * t)
           + 0.45 * np.sin(4.0 * math.pi * f * t)
           + 0.18 * np.sin(6.0 * math.pi * f * t))
    return _stereo(np.tanh(sig * 1.2) * env, 0.52)


def stab(midis: list[int], decay: float = 0.30) -> np.ndarray:
    """Short detuned chord hit — the loop's harmony, playable on one key."""
    t = _t(decay * 2.5)
    env = (1.0 - np.exp(-t / 0.004)) * np.exp(-t / decay)
    sig = np.zeros_like(t)
    for i, midi in enumerate(midis):
        f = _freq(midi)
        for det in (-0.004, 0.0, 0.004):
            sig += np.sin(2.0 * math.pi * f * (1.0 + det) * t + 0.3 * i).astype(np.float32)
    sig /= max(1, 3 * len(midis))
    return _stereo(sig * env, 0.42)


# --------------------------------------------------------------------------
# kits
# --------------------------------------------------------------------------

@dataclass
class Kit:
    name: str
    tempo: int | None
    rows: list[list[tuple[str, str]]]
    samples: dict[str, np.ndarray] = field(default_factory=dict)


# MIDI numbers used by the Never See Me Again kit (F# major).
F_SHARP_1, B_1, D_SHARP_2, C_SHARP_2 = 30, 35, 39, 37
F_SHARP_MAJOR = [54, 58, 61]
F_SHARP_7 = [54, 58, 61, 64]
B_MAJOR = [59, 63, 66]
B_MINOR = [59, 62, 66]
A_SHARP_M7 = [58, 61, 65, 68]
C_SHARP_7 = [61, 65, 68, 71]


def never_see_me_again() -> Kit:
    """Kanye West, 'Never See Me Again' — F# major, 89 bpm.

    Lower row is the kit, upper row is the harmony of the loop in F#; the four
    bass keys are the roots of the progression (F#, B, D#m, C#7's C#).
    """
    rows = [
        [("z", "kick"), ("x", "808"), ("c", "snare"), ("v", "clap"),
         ("b", "hat"), ("n", "ohat"), ("m", "rim"),
         (",", "tom"), (".", "tom hi"), ("/", "crash")],
        [("q", "F#"), ("w", "B"), ("e", "D#"), ("r", "C#"),
         ("t", "F#maj"), ("y", "F#7"), ("u", "Bmaj"),
         ("i", "Bm"), ("o", "A#m7"), ("p", "C#7")],
    ]
    samples = {
        "z": kick(),
        "x": eight08(F_SHARP_1),
        "c": snare(),
        "v": clap(),
        "b": hat(),
        "n": hat(open_hat=True),
        "m": rim(),
        ",": tom(45),
        ".": tom(52),
        "/": crash(),
        "q": bass(F_SHARP_1),
        "w": bass(B_1),
        "e": bass(D_SHARP_2),
        "r": bass(C_SHARP_2),
        "t": stab(F_SHARP_MAJOR),
        "y": stab(F_SHARP_7),
        "u": stab(B_MAJOR),
        "i": stab(B_MINOR),
        "o": stab(A_SHARP_M7),
        "p": stab(C_SHARP_7),
    }
    return Kit("Never See Me Again — beat", 89, rows, samples)


KITS = {
    "never-see-me-again": never_see_me_again,
}


def build(slug: str) -> Kit:
    try:
        return KITS[slug]()
    except KeyError:
        raise KeyError(f"unknown beat kit {slug!r}; known: {', '.join(KITS)}") from None
