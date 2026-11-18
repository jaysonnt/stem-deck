#!/usr/bin/env python3
"""
pianokb - one spare keyboard, always a piano.

The chosen keyboard is grabbed at the kernel level (EVIOCGRAB), so its keys
never reach the desktop: no typing, no shortcuts, no interference with the
other keyboard. Key events are read straight from /dev/input and rendered by
a small additive synth, streamed to PipeWire through pw-cat.

Presets (chromatic piano, songs, beat kits, stem decks) are switched with the
arrow keys on the piano keyboard itself.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import queue
import re
import selectors
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

import numpy as np

try:
    import evdev
    from evdev import ecodes
except ImportError:  # pragma: no cover
    sys.exit("pianokb needs python-evdev (e.g. `sudo pacman -S python-evdev`)")

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

SR = 48_000
BLOCK = 128              # ~2.7 ms per audio block: fast repeated notes stay tight
RELEASE_S = 0.30         # key-up damping
SPEED_STEP = 0.05        # one notch of the dial: tape speed, 5% a notch
SPEED_MIN, SPEED_MAX = 0.5, 2.0
NOW_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "now_playing.json")


# --------------------------------------------------------------------------
# key names <-> evdev keycodes
# --------------------------------------------------------------------------

KEYS: dict[str, int] = {c: getattr(ecodes, "KEY_" + c.upper()) for c in "abcdefghijklmnopqrstuvwxyz"}
KEYS.update({c: getattr(ecodes, "KEY_" + c) for c in "0123456789"})
KEYS.update({
    ",": ecodes.KEY_COMMA,
    ".": ecodes.KEY_DOT,
    "/": ecodes.KEY_SLASH,
    ";": ecodes.KEY_SEMICOLON,
    "'": ecodes.KEY_APOSTROPHE,
    "-": ecodes.KEY_MINUS,
    "=": ecodes.KEY_EQUAL,
    "[": ecodes.KEY_LEFTBRACE,
    "]": ecodes.KEY_RIGHTBRACE,
    "\\": ecodes.KEY_BACKSLASH,
    "`": ecodes.KEY_GRAVE,
    "\u21e5": ecodes.KEY_TAB,
    "\u23ce": ecodes.KEY_ENTER,
    "\u232b": ecodes.KEY_BACKSPACE,
})
CHAR_OF = {code: ch for ch, code in KEYS.items()}

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
SEMITONE_OF = {name: i for i, name in enumerate(NOTE_NAMES)}
SEMITONE_OF.update({"Db": 1, "Eb": 3, "Gb": 6, "Ab": 8, "Bb": 10})


def midi_of(name: str) -> int:
    """'C4' -> 60, 'F#3' -> 54, 'Bb5' -> 82."""
    m = re.fullmatch(r"\s*([A-Ga-g])([#b]?)(-?\d+)\s*", str(name))
    if not m:
        raise ValueError(f"not a note name: {name!r}")
    letter = m.group(1).upper()
    acc = m.group(2)
    octave = int(m.group(3))
    semitone = SEMITONE_OF[letter + acc]
    return (octave + 1) * 12 + semitone


def note_name(midi: int) -> str:
    return f"{NOTE_NAMES[midi % 12]}{midi // 12 - 1}"


CHORD_QUALITIES = [
    (frozenset({0, 4, 7, 11}), "maj7"),
    (frozenset({0, 4, 7, 10}), "7"),
    (frozenset({0, 3, 7, 10}), "m7"),
    (frozenset({0, 3, 6, 10}), "m7b5"),
    (frozenset({0, 5, 7, 10}), "7sus4"),
    (frozenset({0, 4, 7}), ""),
    (frozenset({0, 3, 7}), "m"),
    (frozenset({0, 3, 6}), "dim"),
    (frozenset({0, 5, 7}), "sus4"),
    (frozenset({0, 2, 7}), "sus2"),
]


def chord_name(notes: list[int]) -> str:
    """'F#', 'Bm', 'A#m7' … for a voicing whose lowest note is the root."""
    if not notes:
        return ""
    root = notes[0]
    if len(notes) == 1:
        return note_name(root)
    steps = frozenset((n - root) % 12 for n in notes)
    for want, suffix in CHORD_QUALITIES:
        if steps == want:
            return NOTE_NAMES[root % 12] + suffix
    return NOTE_NAMES[root % 12] + f"({len(notes)})"


# --------------------------------------------------------------------------
# presets
# --------------------------------------------------------------------------

@dataclass
class Preset:
    name: str
    layout: list[tuple[str, int]]      # (key char, midi note), row by row
    row_lengths: list[int]
    melody: list[tuple[int, float]] | None = None
    chords: list[tuple[list[int], float]] | None = None
    tempo: int | None = None
    kit: "beats.Kit | None" = None       # drum-machine preset: keys fire one-shots
    stems: "stems.Bank | None" = None    # stem decks: keys gate song parts

    def keymap(self) -> dict[int, int]:
        return {KEYS[k]: m for k, m in self.layout if k in KEYS}

    def display_rows(self) -> list[list[tuple[str, int]]]:
        rows, i = [], 0
        for length in self.row_lengths:
            rows.append(self.layout[i:i + length])
            i += length
        return rows


def build_presets() -> list[Preset]:
    import beats
    import songs
    import stems as stem_decks

    presets: list[Preset] = []

    chrom = songs.CHROMATIC
    base = midi_of(chrom.get("base", "C4"))
    presets.append(Preset(
        name=chrom["name"],
        layout=[(k, base + off) for k, off in chrom["layout"]],
        row_lengths=chrom["row_lengths"],
    ))

    for song in songs.SONGS:
        melody = [(midi_of(n), float(beats)) for n, beats in song.get("melody", [])]
        chords = [
            ([midi_of(n) for n in notes], float(beats))
            for notes, beats in song.get("chords", [])
        ]
        pitches = sorted(
            {m for m, _ in melody} | {m for notes, _ in chords for m in notes}
        )
        if not pitches:
            raise ValueError(f"{song['name']}: give it a melody or a chord loop")
        scale = songs.SCALES[song.get("scale", "major")]
        root = midi_of(song["root"])

        # Tonic at or below the lowest melody note, then walk up the scale so
        # every melody note lands on a key. One key per scale degree: no
        # wrong notes are possible while the preset is selected.
        start = root
        while start > pitches[0]:
            start -= 12
        while start + 12 <= pitches[0]:
            start += 12

        rows = list(songs.DIATONIC_ROWS)
        width = len("".join(rows))

        notes: list[int] = []
        octave = 0
        while len(notes) < 64:
            notes.extend(start + 12 * octave + step for step in scale)
            octave += 1
            if len(notes) >= width and notes[-1] >= pitches[-1]:
                break
        notes = [m for m in notes if m >= start][:max(width, len(notes))]
        if pitches[-1] > notes[min(width, len(notes)) - 1]:
            raise ValueError(
                f"{song['name']}: melody spans more than {width} scale degrees "
                f"({note_name(pitches[0])}..{note_name(pitches[-1])}); "
                "shorten the range or lower 'root'."
            )

        presets.append(Preset(
            name=song["name"],
            layout=[(k, notes[i]) for i, k in enumerate("".join(rows))],
            row_lengths=[len(r) for r in rows],
            melody=melody or None,
            chords=chords or None,
            tempo=song.get("tempo"),
        ))

        beat = song.get("beat")
        if beat:
            kit = beats.build(beat)
            presets.append(Preset(
                name=kit.name,
                layout=[],
                row_lengths=[len(row) for row in kit.rows],
                tempo=kit.tempo,
                kit=kit,
            ))

    bank = stem_decks.discover()
    if bank.decks:
        presets.append(Preset(
            name=f"Stems ({len(bank.decks)} song{'s' if len(bank.decks) != 1 else ''})",
            layout=[],
            row_lengths=[],
            stems=bank,
        ))

    return presets


# --------------------------------------------------------------------------
# synthesis
# --------------------------------------------------------------------------

def make_note(midi: int, dur: float = 4.0) -> np.ndarray:
    """One stereo piano-ish note, peak-normalised."""
    f0 = 440.0 * 2.0 ** ((midi - 69) / 12.0)
    n = int(dur * SR)
    t = np.arange(n, dtype=np.float32) / SR
    out = np.zeros(n, dtype=np.float32)

    rng = np.random.default_rng(midi)
    brightness = float(np.clip((midi - 36) / 60.0, 0.0, 1.0))
    harmonics = int(6 + 6 * (1.0 - brightness))

    for k in range(1, harmonics + 1):
        fk = f0 * k * math.sqrt(1.0 + 0.00035 * k * k)   # slight inharmonicity
        if fk > SR * 0.45:
            break
        amp = 1.0 / k ** 1.9
        tau = max(3.4 / (1.0 + 0.7 * (k - 1)) * (1.0 - 0.5 * brightness), 0.18)
        phase = rng.uniform(0.0, 2.0 * math.pi)
        out += (amp * np.exp(-t / tau) * np.sin(2.0 * math.pi * fk * t + phase)).astype(np.float32)

    # felt hammer / key noise, short and soft
    noise = rng.normal(0.0, 1.0, n).astype(np.float32)
    window = 24
    csum = np.cumsum(noise, dtype=np.float32)
    smooth = np.empty(n, dtype=np.float32)
    smooth[:window] = csum[:window] / np.arange(1, window + 1, dtype=np.float32)
    smooth[window:] = (csum[window:] - csum[:-window]) / window
    out += 0.055 * smooth * np.exp(-t / 0.02).astype(np.float32)

    out *= (1.0 - np.exp(-t / 0.0022)).astype(np.float32)     # click-free attack
    peak = float(np.max(np.abs(out))) or 1.0
    out *= 0.28 / peak

    # a hair of width: right channel delayed ~0.35 ms, tiny pan by pitch
    delay = max(1, int(SR * 0.00035))
    left = out
    right = np.concatenate([np.zeros(delay, dtype=np.float32), out[:-delay]])
    pan = float(np.clip((midi - 60) / 60.0, -0.35, 0.35))
    stereo = np.stack([left * (1.0 - pan), right * (1.0 + pan)], axis=1).astype(np.float32)
    return stereo


class Voice:
    __slots__ = ("sample", "pos", "release", "gain", "sustained", "serial", "dir")

    def __init__(self, sample: np.ndarray, gain: float = 1.0, serial: int = 0,
                 direction: int = 1):
        self.sample = sample
        self.dir = direction
        self.pos = 0 if direction > 0 else len(sample)
        self.release: int | None = None
        self.gain = gain
        self.sustained = False
        self.serial = serial


class Reverb:
    """Small, cheap room.

    Comb delays are whole multiples of BLOCK, so each one is a simple ring
    buffer read/write per block: no per-sample Python loop, and the damped
    feedback path keeps it stable.
    """

    LEFT = (1536, 1792, 2048, 2304)      # 32, 37, 43, 48 ms at 48 kHz
    RIGHT = (1792, 2048, 2304, 2560)
    WET = 0.22

    def __init__(self) -> None:
        self.banks = []
        for delays in (self.LEFT, self.RIGHT):
            combs = []
            for i, delay in enumerate(delays):
                combs.append({
                    "buf": np.zeros(delay, dtype=np.float32),
                    "pos": 0,
                    "fb": 0.74 - 0.02 * i,
                    "prev": 0.0,
                })
            self.banks.append(combs)

    def process(self, buf: np.ndarray) -> np.ndarray:
        n = len(buf)
        out = np.zeros((n, 2), dtype=np.float32)
        for ch, combs in enumerate(self.banks):
            x = buf[:, ch]
            acc = np.zeros(n, dtype=np.float32)
            for comb in combs:
                ring = comb["buf"]
                delay = len(ring)
                pos = comb["pos"]
                if pos + n <= delay:
                    delayed = ring[pos:pos + n].copy()
                else:
                    k = delay - pos
                    delayed = np.concatenate((ring[pos:], ring[:n - k]))

                damped = 0.62 * delayed + 0.38 * np.concatenate(
                    (np.array([comb["prev"]], dtype=np.float32), delayed[:-1])
                )
                comb["prev"] = float(delayed[-1])
                level = x + comb["fb"] * damped
                if pos + n <= delay:
                    ring[pos:pos + n] = level
                else:
                    k = delay - pos
                    ring[pos:] = level[:k]
                    ring[:n - k] = level[k:]
                comb["pos"] = (pos + n) % delay
                acc += delayed
            out[:, ch] = acc * (self.WET / len(combs))
        return out


class Piano:
    def __init__(self, volume: float = 1.0):
        self.volume = volume
        self.samples: dict[int, np.ndarray] = {}
        self.voices: dict[int, Voice] = {}
        self.oneshots: list[Voice] = []      # kit hits: no release, no sustain
        self.pending: queue.Queue = queue.Queue()
        self._gen = threading.Lock()
        self.sustain = False
        self.reverse = False        # new notes play their tail first
        self.reverb = Reverb()
        self._rng = np.random.default_rng()
        self._serial = 0

    def prepare(self, midi: int) -> None:
        if midi not in self.samples:
            with self._gen:
                if midi not in self.samples:
                    self.samples[midi] = make_note(midi)

    def note_on(self, midi: int) -> int:
        """Start a note and return its token, so a late key-up cannot cut a
        newer press of the same key (fast trills and repeated notes)."""
        self._serial += 1
        token = self._serial
        self.pending.put(("on", (midi, token)))
        return token

    def note_off(self, midi: int, token: int) -> None:
        self.pending.put(("off", (midi, token)))

    def all_off(self) -> None:
        self.pending.put(("all", 0))

    def set_sustain(self, on: bool) -> None:
        self.pending.put(("sustain", 1 if on else 0))

    def set_reverse(self, on: bool) -> None:
        self.pending.put(("reverse", 1 if on else 0))

    def hit(self, sample: np.ndarray, gain: float = 1.0) -> None:
        self.pending.put(("hit", (sample, gain)))

    def render(self, n: int) -> np.ndarray:
        while True:
            try:
                kind, payload = self.pending.get_nowait()
            except queue.Empty:
                break
            if kind == "on":
                midi, token = payload
                if midi not in self.samples:
                    self.samples[midi] = make_note(midi)
                touch = 0.92 + 0.08 * float(self._rng.random())
                self.voices[midi] = Voice(self.samples[midi], gain=touch, serial=token,
                                          direction=-1 if self.reverse else 1)
            elif kind == "off":
                midi, token = payload
                voice = self.voices.get(midi)
                if voice is not None and voice.serial == token and voice.release is None:
                    if self.sustain:
                        voice.sustained = True
                    else:
                        voice.release = 0
            elif kind == "sustain":
                self.sustain = bool(payload)
                if not self.sustain:
                    for voice in self.voices.values():
                        if voice.sustained and voice.release is None:
                            voice.release = 0
                            voice.sustained = False
            elif kind == "reverse":
                self.reverse = bool(payload)
            elif kind == "hit":
                sample, gain = payload
                self.oneshots.append(Voice(sample, gain=gain))
            else:
                self.voices.clear()
                self.oneshots.clear()

        buf = np.zeros((n, 2), dtype=np.float32)
        rel_len = RELEASE_S * SR

        for midi, voice in list(self.voices.items()):
            sample = voice.sample
            done = voice.pos <= 0 if voice.dir < 0 else voice.pos >= len(sample)
            if done or (voice.release is not None and voice.release >= rel_len):
                del self.voices[midi]
                continue
            if voice.dir > 0:
                end = min(voice.pos + n, len(sample))
                chunk = sample[voice.pos:end] * voice.gain
                voice.pos = end
            else:
                start = max(0, voice.pos - n)
                chunk = sample[start:voice.pos][::-1] * voice.gain
                voice.pos = start
            count = len(chunk)
            if voice.release is None:
                buf[:count] += chunk
            else:
                idx = voice.release + np.arange(count, dtype=np.float32)
                env = np.clip(1.0 - idx / rel_len, 0.0, 1.0) ** 2
                buf[:count] += chunk * env[:, None]
                voice.release += count

        for voice in list(self.oneshots):
            chunk = voice.sample[voice.pos:voice.pos + n]
            buf[:len(chunk)] += chunk * voice.gain
            voice.pos += len(chunk)
            if voice.pos >= len(voice.sample):
                self.oneshots.remove(voice)

        buf += self.reverb.process(buf)
        buf *= self.volume
        np.tanh(buf, out=buf)          # soft ceiling, keeps dense chords clean
        return buf


class AudioOut:
    """Keeps a raw f32 stereo stream open on PipeWire, restarting if it dies."""

    CMD = [
        "pw-cat", "--playback", "--raw",
        "--format", "f32", "--rate", str(SR), "--channels", "2",
        "--latency", "10ms", "-",
    ]

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self._start()

    def _start(self) -> None:
        try:
            self.proc = subprocess.Popen(
                self.CMD,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            sys.exit("pianokb needs pw-cat (PipeWire). Install pipewire-tools or use pacat.")

    def write(self, data: bytes) -> None:
        proc = self.proc
        if proc is None or proc.poll() is not None:
            time.sleep(0.4)
            self._start()
            return
        try:
            proc.stdin.write(data)          # type: ignore[union-attr]
        except (BrokenPipeError, ValueError, OSError):
            try:
                proc.kill()
            except OSError:
                pass
            time.sleep(0.4)
            self._start()

    def close(self) -> None:
        if self.proc is None:
            return
        try:
            self.proc.stdin.close()         # type: ignore[union-attr]
        except (BrokenPipeError, ValueError, OSError):
            pass
        try:
            self.proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            self.proc.kill()


# --------------------------------------------------------------------------
# device discovery / grabbing
# --------------------------------------------------------------------------

def _phys_group(dev: evdev.InputDevice) -> str:
    phys = dev.phys or dev.name or ""
    return phys.split("/input")[0]


def _is_keyboard(dev: evdev.InputDevice) -> bool:
    caps = dev.capabilities()
    keys = caps.get(ecodes.EV_KEY, [])
    rel = caps.get(ecodes.EV_REL, [])
    return ecodes.KEY_A in keys and ecodes.REL_X not in rel


def keyboard_groups() -> dict[str, list[evdev.InputDevice]]:
    """One entry per physical USB device, all of its key-carrying interfaces."""
    groups: dict[str, list[evdev.InputDevice]] = {}
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
        except OSError:
            continue
        if not _is_keyboard(dev):
            dev.close()
            continue
        groups.setdefault(_phys_group(dev), []).append(dev)
    return {key: devs for key, devs in groups.items() if grab_targets(devs)}


def _close_others(groups: dict[str, list[evdev.InputDevice]], chosen: list[evdev.InputDevice]) -> None:
    for devs in groups.values():
        if devs is chosen:
            continue
        for dev in devs:
            try:
                dev.close()
            except OSError:
                pass


def group_label(devs: list[evdev.InputDevice]) -> str:
    names = [d.name for d in devs if d.phys and "/input0" in d.phys]
    if not names:
        names = [d.name for d in devs]
    return min(names, key=len)


def grab_targets(devs: list[evdev.InputDevice]) -> list[evdev.InputDevice]:
    return [d for d in devs if _is_keyboard(d)]


# The BOYI knob turns as Volume Up / Volume Down key presses and pushes as Mute,
# all on the grabbed keyboard interface. Other boards report a wheel and a
# mouse button, so both are accepted.
WHEEL_CODES = (ecodes.REL_WHEEL, ecodes.REL_HWHEEL)
DIAL_UP = (ecodes.KEY_VOLUMEUP,)
DIAL_DOWN = (ecodes.KEY_VOLUMEDOWN,)
DIAL_BUTTONS = (ecodes.KEY_MUTE, ecodes.BTN_LEFT, ecodes.BTN_MIDDLE, ecodes.BTN_SIDE, ecodes.BTN_EXTRA)


def grab(devs: list[evdev.InputDevice]) -> list[evdev.InputDevice]:
    grabbed = []
    for dev in devs:
        try:
            dev.grab()
            grabbed.append(dev)
        except OSError as exc:
            print(f"  ! could not grab {dev.name} ({dev.path}): {exc}")
    return grabbed


def wait_idle(devs: list[evdev.InputDevice], timeout: float | None = None) -> None:
    """Wait until no key is held, so its release is seen before the grab.

    Grabbing while a key is down leaves that key stuck for the whole desktop:
    the compositor never sees the release and keeps treating the key as down
    for *every* keyboard. There is no safe timeout — grabbing anyway recreates
    the stuck key — so this waits as long as it takes.
    """
    deadline = None if timeout is None else time.time() + timeout
    warned = False
    while deadline is None or time.time() < deadline:
        try:
            if not any(dev.active_keys() for dev in devs):
                return
        except OSError:
            return
        if not warned:
            print(colour("  waiting for held keys to be released before grabbing…", DIM))
            warned = True
        time.sleep(0.02)


def clear_stale_keys(codes: list[int]) -> bool:
    """Send a key-up for every code through a throwaway uinput device.

    A key held when the grab starts never delivers its release to the desktop,
    which leaves it stuck down for *every* keyboard until it is pressed and
    released again. This clears that state without touching the grabbed board.
    """
    import fcntl
    import struct

    try:
        fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return False

    try:
        fcntl.ioctl(fd, 0x40045564, ecodes.EV_KEY)       # UI_SET_EVBIT
        for code in codes:
            fcntl.ioctl(fd, 0x40045565, code)            # UI_SET_KEYBIT
        setup = struct.pack("=HHHH80sI", 3, 1, 1, 1, b"pianokb-clear", 0)
        fcntl.ioctl(fd, 0x405C5503, setup)               # UI_DEV_SETUP
        fcntl.ioctl(fd, 0x5501)                          # UI_DEV_CREATE
        time.sleep(0.02)
        now = time.time()
        stamp = struct.pack("=qqHHi", int(now), int(now * 1e6) % 1_000_000, 0, 0, 0)
        for code in codes:
            os.write(fd, stamp[:16] + struct.pack("=HHi", ecodes.EV_KEY, code, 0))
        os.write(fd, stamp[:16] + struct.pack("=HHi", ecodes.EV_SYN, ecodes.SYN_REPORT, 0))
        time.sleep(0.02)
        return True
    except OSError:
        return False
    finally:
        try:
            fcntl.ioctl(fd, 0x5502)                      # UI_DEV_DESTROY
        except OSError:
            pass
        os.close(fd)


def ungrab(devs: list[evdev.InputDevice]) -> None:
    for dev in devs:
        try:
            dev.ungrab()
        except (OSError, ValueError):
            pass


def release(devs: list[evdev.InputDevice]) -> None:
    for dev in devs:
        try:
            dev.ungrab()
        except (OSError, ValueError):
            pass
        try:
            dev.close()
        except (OSError, ValueError):
            pass


def grab_when_idle(devs: list[evdev.InputDevice]) -> list[evdev.InputDevice]:
    """Grab only once every key is up, and re-check after the grab.

    A key can go down between wait_idle() returning and the grab taking
    effect; the grab would then hide its release from the desktop. If keys
    are down after grabbing, let go and wait for idle again.
    """
    while True:
        wait_idle(devs)
        grabbed = grab(devs)
        if not grabbed:
            return []
        try:
            busy = any(dev.active_keys() for dev in grabbed)
        except OSError:
            busy = False
        if not busy:
            return grabbed
        ungrab(grabbed)
        time.sleep(0.02)


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
            fh.write("\n")
    except OSError as exc:
        print(f"  (could not save {CONFIG_PATH}: {exc})")


def list_devices() -> None:
    groups = keyboard_groups()
    if not groups:
        print("No keyboards found under /dev/input.")
        return
    print("\nKeyboards found:\n")
    for label_devs in groups.values():
        label = group_label(label_devs)
        targets = grab_targets(label_devs)
        print(f"  {label}")
        for dev in targets:
            print(f"      {dev.path:<22} {dev.name}")
        if not targets:
            print("      (no keyboard interface to grab)")
    print("\nPick one with --device \"<part of the name>\" or let pianokb ask once.\n")


def choose_group(args, cfg: dict) -> tuple[str, list[evdev.InputDevice]]:
    wanted = args.device or cfg.get("device") or ""
    while True:
        groups = keyboard_groups()
        if not groups:
            print("No keyboards found. Plug one in; waiting…", flush=True)
            time.sleep(2.0)
            continue

        if wanted:
            matches = [(label, devs) for devs in groups.values()
                       for label in [group_label(devs)]
                       if wanted.lower() in label.lower()
                       or any(wanted.lower() in d.name.lower() for d in devs)]
            if matches:
                _close_others(groups, matches[0][1])
                return matches[0]
            print(f"Waiting for keyboard matching “{wanted}”…", flush=True)
            for devs in groups.values():
                release(devs)
            time.sleep(2.0)
            continue

        labels = [(group_label(devs), devs) for devs in groups.values()]
        if len(labels) == 1:
            _close_others(groups, labels[0][1])
            return labels[0]

        print("\nWhich keyboard should become the piano?\n")
        for i, (label, _) in enumerate(labels, 1):
            print(f"  {i}. {label}")
        print()
        if not sys.stdin.isatty():
            sys.exit("Several keyboards found; run with --device \"<name>\".")
        try:
            choice = input("  number: ").strip() or "1"
            idx = int(choice) - 1
            if not 0 <= idx < len(labels):
                raise ValueError
        except (ValueError, EOFError):
            print("  not a valid choice, try again.")
            for devs in groups.values():
                release(devs)
            continue

        label = labels[idx][0]
        cfg["device"] = label
        save_config(cfg)
        print(f"  saved to {CONFIG_PATH}\n")
        _close_others(groups, labels[idx][1])
        return labels[idx]


# --------------------------------------------------------------------------
# terminal output
# --------------------------------------------------------------------------

DIM = "\033[2m"
BOLD = "\033[1m"
CYAN = "\033[36m"
GREEN = "\033[32m"
RESET = "\033[0m"


def colour(text: str, code: str) -> str:
    return f"{code}{text}{RESET}" if sys.stdout.isatty() else text


def show_preset(presets: list[Preset], index: int) -> None:
    preset = presets[index]
    print()
    print(colour(f"  {index + 1}/{len(presets)}  {preset.name}", BOLD + CYAN))
    if preset.tempo:
        print(colour(f"  {preset.tempo} bpm", DIM))

    if preset.melody:
        key_of = {midi: key for key, midi in preset.layout}
        notes = [note for note, _ in preset.melody]
        keys = [key_of.get(note, "?") for note in notes]
        for start in range(0, len(notes), 8):
            pairs = list(zip(keys[start:start + 8], notes[start:start + 8]))
            line_keys = "  ".join(key.upper() for key, _ in pairs)
            line_notes = " ".join(note_name(note).rjust(2) for _, note in pairs)
            bar = "| " if start else ""
            print(f"  {colour('keys', DIM)}  {bar}{colour(line_keys, GREEN)}")
            print(f"  {colour('note', DIM)}  {bar}{colour(line_notes, DIM)}")
        print()

    if preset.chords:
        key_of = {midi: key for key, midi in preset.layout}
        for start in range(0, len(preset.chords), 4):
            block = preset.chords[start:start + 4]
            names = "  ".join(chord_name(notes).rjust(6) for notes, _ in block)
            keys = "  ".join(
                "+".join(key_of.get(m, "-").upper() for m in notes).rjust(6)
                for notes, _ in block
            )
            print(f"  {colour('chord', DIM)}  {colour(names, GREEN)}")
            print(f"  {colour('key', DIM)}    {colour(keys, GREEN)}")
        print()

    if preset.kit is not None:
        for row in preset.kit.rows:
            width = max(2, max(len(label) for _, label in row))
            print("   " + " ".join(colour(k.upper().center(width), GREEN) for k, _ in row))
            print("   " + " ".join(label.center(width) for _, label in row))
            print()

    for row in preset.display_rows():
        print("   " + " ".join(colour(k.upper().rjust(2), GREEN) for k, _ in row))
        print("   " + " ".join(note_name(m).rjust(2) for _, m in row))
        print()

    if preset.stems is not None:
        bank = preset.stems
        width = min(max([len(d.name) for d in bank.decks] + [8]), 26)
        print(colour(f"  {'song'.ljust(width)}  stems", DIM))
        for deck, keys in zip(bank.decks, bank.keys):
            if not keys:
                print(f"  {deck.name[:width].ljust(width)}  "
                      + colour("wider than the keyboard", DIM))
                continue
            cells = "  ".join(f"{ch.upper()}={deck.stems[s][0]}" for ch, s in keys)
            print(f"  {deck.name[:width].ljust(width)}  {colour(cells, GREEN)}")
        print()
        print(colour("  press a key to start that stem · release mutes it", DIM))
        print(colour("  press again and the song carries on from where it is", DIM))
        print(colour("  hold ' = play backwards · space while reversed = stay reversed", DIM))
        print(colour("  dial turn = tape speed · dial press = 1.00x, or whole song", DIM))
        print(colour("  space = whole song   ← → or - = scrub 5 s   [ ] preset   esc quit", DIM))
        return

    print(colour("  [ ] song   - = octave (or dial)   space sustain   hold ' reverse   esc quit", DIM))


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Turn a spare keyboard into a piano.")
    parser.add_argument("--device", help="part of the keyboard name to use")
    parser.add_argument("--list", action="store_true", help="list keyboards and exit")
    parser.add_argument("--check", action="store_true", help="print presets and keymaps, then exit")
    args = parser.parse_args()

    try:                      # keep logs/autostart output live
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass

    presets = build_presets()
    stem_bank = next((p.stems for p in presets if p.stems is not None), None)

    if args.check:
        for i in range(len(presets)):
            show_preset(presets, i)
        return
    if args.list:
        list_devices()
        return

    cfg = load_config()
    label, devs = choose_group(args, cfg)
    targets = grab_targets(devs)
    if not targets:
        release(devs)
        sys.exit(f"{label} has no keyboard interface to grab.")
    for dev in devs:
        if dev not in targets:
            dev.close()

    print(colour(f"\n  piano keyboard: {label}", BOLD))
    print(colour("  grabbing it now — it will not type anywhere while pianokb runs.", DIM))
    print(colour("  dial: turn = tape speed, press = 1.00x / whole song · hold ' = reverse.", DIM))
    grabbed = grab_when_idle(targets)
    if not grabbed:
        sys.exit("Could not grab the keyboard (is another program holding it?).")
    if clear_stale_keys(sorted(set(KEYS.values()))):
        print(colour("  cleared keys a previous grab may have left down", DIM))

    piano = Piano()
    used = sorted({midi for preset in presets for _, midi in preset.layout})
    print(colour(f"  tuning {len(used)} notes…", DIM), end="", flush=True)
    for midi in used:
        piano.prepare(midi)
    print("\r" + " " * 40 + "\r", end="")

    audio = AudioOut()
    stop = threading.Event()

    def mixer() -> None:
        while not stop.is_set():
            buf = piano.render(BLOCK)
            if stem_bank is not None:
                stem_bank.mix(buf)
                peak = float(np.abs(buf).max())
                if peak > 0.985:            # keep the sum clean, no hard clipping
                    buf *= np.float32(0.985 / peak)
            audio.write(buf.tobytes())

    thread = threading.Thread(target=mixer, daemon=True)
    thread.start()

    # With stems loaded the board is a stem player, so start there rather than
    # on the piano: hold a key and the song plays.
    preset_index = next((i for i, p in enumerate(presets) if p.stems is not None), 0)
    octave_shift = 0
    show_preset(presets, preset_index)

    def now_snapshot() -> dict:
        preset = presets[preset_index]
        base = {
            "active": False, "preset": preset.name, "updated": time.time(),
            "reverse": reverse_held or reverse_latched,
            "reverse_held": reverse_held, "reverse_latched": reverse_latched,
            "speed": round(stem_bank.speed, 2) if stem_bank is not None else 1.0,
        }
        if stem_bank is not None:
            base["last"] = stem_bank.last
            base["decks"] = [
                {
                    "name": d.name,
                    "playing": bool(d.playing),
                    "position": round(d.pos / SR, 3),
                    "duration": round(d.length / SR, 3),
                    "stems": [
                        {"name": name, "on": d.targets[s] > 0.5,
                         "key": next((ch for ch, k in keys if k == s), "")}
                        for s, (name, _sample) in enumerate(d.stems)
                    ],
                }
                for d, keys in zip(stem_bank.decks, stem_bank.keys)
            ]
        if preset.stems is None or stem_bank is None or not stem_bank.decks:
            return base
        deck = stem_bank.decks[min(stem_bank.last, len(stem_bank.decks) - 1)]
        if not (deck.playing or any(deck.targets)):
            return base
        return {
            **base,
            "active": True,
            "preset": preset.name,
            "song": deck.name,
            "playing": bool(deck.playing),
            "position": round(deck.pos / SR, 3),
            "duration": round(deck.length / SR, 3),
            "stems": [
                {"name": name, "on": deck.targets[s] > 0.5}
                for s, (name, _sample) in enumerate(deck.stems)
            ],
            "updated": time.time(),
        }

    def now_writer() -> None:
        while not stop.is_set():
            try:
                tmp = NOW_FILE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(now_snapshot(), fh)
                os.replace(tmp, NOW_FILE)
            except OSError:
                pass
            stop.wait(0.25)

    # Reverse is a momentary button: hold ' and whatever is playing runs
    # backwards, let go and it runs forwards again. Space while reversed latches
    # it, so it stays backwards after ' is released; Space again unlatches.
    reverse_held = False
    reverse_latched = False
    reverse = False

    def apply_reverse() -> None:
        nonlocal reverse
        want = reverse_held or reverse_latched
        if want == reverse:
            return
        reverse = want
        piano.set_reverse(reverse)
        if stem_bank is not None:
            stem_bank.reverse = -1 if reverse else 1

    def hold_reverse(down: bool) -> None:
        nonlocal reverse_held
        reverse_held = down
        apply_reverse()
        if down:
            print(colour("  ◀◀ reverse (hold) — space keeps it", DIM))
        elif reverse_latched:
            print(colour("  ◀◀ reverse stays on (latched) — space releases it", DIM))
        else:
            print(colour("  ▶ forward", DIM))

    def toggle_latch() -> None:
        nonlocal reverse_latched
        reverse_latched = not reverse_latched
        apply_reverse()
        print(colour("  ◀◀ reverse latched" if reverse_latched
                     else ("  reverse unlatched — still held" if reverse_held
                           else "  ▶ forward (unlatched)"), DIM))

    def nudge_speed(notches: int) -> None:
        if stem_bank is None:
            return
        speed = round(stem_bank.speed + notches * SPEED_STEP, 2)
        stem_bank.speed = max(SPEED_MIN, min(SPEED_MAX, speed))
        print(colour(f"  speed {stem_bank.speed:.2f}x", DIM))

    def dial_press() -> None:
        """Back to normal speed if the dial moved it, otherwise the whole song."""
        if stem_bank is not None and abs(stem_bank.speed - 1.0) > 1e-6:
            stem_bank.speed = 1.0
            print(colour("  speed 1.00x", DIM))
            return
        toggle_song()

    space_latched = False     # this Space press went to the reverse latch
    unknown_seen: set[tuple[int, int]] = set()

    threading.Thread(target=now_writer, daemon=True).start()

    def keymap() -> dict[int, int]:
        return {code: midi + 12 * octave_shift for code, midi in presets[preset_index].keymap().items()}

    def kit_keys() -> dict[int, str]:
        kit = presets[preset_index].kit
        if kit is None:
            return {}
        return {KEYS[ch]: ch for ch in kit.samples if ch in KEYS}

    def warm(notes) -> None:
        """Render transposed notes off the input thread, so the first press of a
        shifted octave is as instant as the rest."""
        todo = [note for note in notes if note not in piano.samples]
        if not todo:
            return

        def run() -> None:
            for note in todo:
                piano.prepare(note)

        threading.Thread(target=run, daemon=True).start()

    held: set[int] = set()
    tokens: dict[int, int] = {}
    current = keymap()
    current_kit = kit_keys()

    def switch(delta: int) -> None:
        nonlocal preset_index, octave_shift, held, current, current_kit
        preset_index = (preset_index + delta) % len(presets)
        octave_shift = 0
        held.clear()
        tokens.clear()
        piano.all_off()
        if stem_bank is not None:
            stem_bank.stop()
        current = keymap()
        current_kit = kit_keys()
        warm(current.values())
        show_preset(presets, preset_index)

    def shift_octave(delta: int) -> None:
        nonlocal octave_shift, held, current
        if presets[preset_index].kit is not None:
            print(colour("  beat kit: no octave shift", DIM))
            return
        if presets[preset_index].stems is not None:
            print(colour("  stems: - = scrub, ← → scrub, no octave shift", DIM))
            return
        octave_shift = max(-2, min(2, octave_shift + delta))
        held.clear()
        tokens.clear()
        piano.all_off()
        current = keymap()
        warm(current.values())
        print(colour(f"  octave {octave_shift:+d}", DIM))

    def clock(samples: int) -> str:
        total = int(samples / SR)
        return f"{total // 60}:{total % 60:02d}"

    def scrub(seconds: float) -> None:
        if stem_bank is None or not stem_bank.decks:
            return
        stem_bank.scrub(seconds)
        deck = stem_bank.decks[min(stem_bank.last, len(stem_bank.decks) - 1)]
        print(colour(f"  {seconds:+.0f}s  {deck.name}  "
                     f"{clock(deck.pos)} / {clock(deck.length)}", DIM))

    def toggle_song() -> None:
        """Dial press: play (or mute) every stem of the song last touched."""
        if stem_bank is None or not stem_bank.decks:
            return
        deck = stem_bank.decks[min(stem_bank.last, len(stem_bank.decks) - 1)]
        on = not any(deck.targets)
        deck.playing = True
        for s in range(len(deck.targets)):
            deck.targets[s] = 1.0 if on else 0.0
        print(colour(f"  {'all stems playing' if on else 'stems muted'} — {deck.name}", DIM))

    def deck_named(name: str) -> int | None:
        if stem_bank is None:
            return None
        return next((i for i, d in enumerate(stem_bank.decks) if d.name == name), None)

    def apply_control(msg: dict) -> None:
        """One command from the web dashboard (ui/server.py → control.jsonl)."""
        nonlocal reverse_latched
        cmd = msg.get("cmd")
        if cmd == "latch":
            reverse_latched = bool(msg.get("on"))
            apply_reverse()
            print(colour(f"  dashboard: reverse {'latched' if reverse_latched else 'off'}", DIM))
            return
        if stem_bank is None or not stem_bank.decks:
            return
        if cmd == "speed":
            stem_bank.speed = max(SPEED_MIN, min(SPEED_MAX, float(msg.get("value", 1.0))))
            print(colour(f"  dashboard: speed {stem_bank.speed:.2f}x", DIM))
        elif cmd == "scrub":
            scrub(float(msg.get("seconds", 0.0)))
        elif cmd == "seek":
            target = int(float(msg.get("seconds", 0.0)) * SR)
            for d in stem_bank.decks:
                if d.length:
                    d.pos = target % d.length
        elif cmd == "song":
            d = deck_named(str(msg.get("song", "")))
            if d is None:
                return
            stem_bank.last = d
            if "on" in msg:
                deck = stem_bank.decks[d]
                deck.playing = True
                for s in range(len(deck.targets)):
                    deck.targets[s] = 1.0 if msg["on"] else 0.0
            else:
                toggle_song()
        elif cmd == "stem":
            d = deck_named(str(msg.get("song", "")))
            if d is None:
                return
            deck = stem_bank.decks[d]
            s = next((i for i, (n, _x) in enumerate(deck.stems) if n == msg.get("stem")), None)
            if s is None:
                return
            deck.targets[s] = 1.0 if msg.get("on") else 0.0
            deck.playing = True
            stem_bank.last = d

    def control_reader() -> None:
        path = os.path.join(HERE, "control.jsonl")
        try:
            offset = os.path.getsize(path)      # ignore commands from before startup
        except OSError:
            offset = 0
        while not stop.is_set():
            try:
                size = os.path.getsize(path)
                if size < offset:               # the UI truncated it
                    offset = 0
                if size > offset:
                    with open(path, "r", encoding="utf-8") as fh:
                        fh.seek(offset)
                        chunk = fh.read()
                    # only consume whole lines
                    end = chunk.rfind("\n") + 1
                    offset += len(chunk[:end].encode("utf-8"))
                    for line in chunk[:end].splitlines():
                        try:
                            msg = json.loads(line)
                            if isinstance(msg, dict) and time.time() - float(msg.get("at", 0)) < 10:
                                apply_control(msg)
                        except (ValueError, TypeError):
                            continue
            except OSError:
                pass
            stop.wait(0.05)

    threading.Thread(target=control_reader, daemon=True).start()

    selector = selectors.DefaultSelector()
    running = True

    def on_signal(_signum, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    try:
        while running:
            # (Re)attach if the keyboard was unplugged or is new.
            if not selector.get_map():
                for dev in targets:
                    if os.path.exists(dev.path):
                        try:
                            selector.register(dev.fd, selectors.EVENT_READ, dev)
                        except (OSError, ValueError):
                            pass
                if not selector.get_map():
                    print(colour("  keyboard unplugged — waiting…", DIM))
                    time.sleep(1.5)
                    continue

            events = selector.select(timeout=0.2)
            if not events:
                continue

            for key, _mask in events:
                dev = key.data
                try:
                    batch = dev.read()
                except BlockingIOError:
                    continue
                except OSError:
                    # device vanished: drop everything, re-grab when it returns
                    piano.all_off()
                    held.clear()
                    for sel_key in list(selector.get_map().values()):
                        try:
                            selector.unregister(sel_key.fd)
                        except (KeyError, ValueError):
                            pass
                    release(grabbed)
                    grabbed = []
                    print(colour("  keyboard gone — waiting for it to come back…", DIM))
                    time.sleep(1.0)
                    while running:
                        label2, devs2 = choose_group(args, cfg)
                        new_targets = grab_targets(devs2)
                        new_grab = grab_when_idle(new_targets)
                        if new_grab:
                            targets = new_targets
                            grabbed = new_grab
                            for d in devs2:
                                if d not in new_targets:
                                    d.close()
                            print(colour(f"  {label2} reconnected.", DIM))
                            break
                        for d in devs2:
                            d.close()
                        time.sleep(1.0)
                    break

                for event in batch:
                    def dial_turn(notches: int) -> None:
                        if presets[preset_index].stems is not None:
                            nudge_speed(notches)
                        else:
                            shift_octave(1 if notches > 0 else -1)

                    if event.type == ecodes.EV_REL:
                        # wheel-style knobs: one notch per event
                        if event.code in WHEEL_CODES and event.value:
                            dial_turn(1 if event.value > 0 else -1)
                        continue
                    if event.type != ecodes.EV_KEY:
                        continue
                    code = event.code

                    # The BOYI dial is Volume Up / Down keys: a fast spin
                    # auto-repeats, so repeats count as notches too.
                    if code in DIAL_UP or code in DIAL_DOWN:
                        if event.value in (1, 2):
                            dial_turn(1 if code in DIAL_UP else -1)
                        continue
                    if event.value == 2:
                        continue

                    if code in DIAL_BUTTONS:
                        if event.value == 1:
                            dial_press()
                        continue

                    if code == ecodes.KEY_ESC and event.value == 1:
                        running = False
                        break
                    if code == ecodes.KEY_APOSTROPHE:
                        hold_reverse(event.value == 1)
                        continue
                    if code == ecodes.KEY_SPACE:
                        # Space while reversed (held or latched) is the latch.
                        if event.value == 1 and (reverse_held or reverse_latched):
                            space_latched = True
                            toggle_latch()
                            continue
                        if event.value == 0 and space_latched:
                            space_latched = False
                            continue
                        # On the stems preset Space is the big button beside the
                        # dial: one press plays or mutes the whole song.
                        if presets[preset_index].stems is not None:
                            if event.value == 1:
                                toggle_song()
                            continue
                        piano.set_sustain(event.value == 1)
                        continue
                    if code == ecodes.KEY_CAPSLOCK and event.value == 1:
                        # Caps Lock does not type; it tells you where you are.
                        print(colour(
                            f"  preset {preset_index + 1}/{len(presets)}  "
                            f"{presets[preset_index].name}", DIM))
                        continue

                    if (code not in CHAR_OF and code not in (
                            ecodes.KEY_LEFT, ecodes.KEY_RIGHT, ecodes.KEY_UP, ecodes.KEY_DOWN)
                            and (event.type, code) not in unknown_seen):
                        unknown_seen.add((event.type, code))
                        print(colour(f"  (unmapped key {ecodes.KEY.get(code, code)})", DIM))

                    if presets[preset_index].stems is not None and stem_bank is not None:
                        if event.value == 0:
                            stem_bank.release(CHAR_OF.get(code, ""))
                            continue
                        if code == ecodes.KEY_LEFT:
                            scrub(-5.0)
                            continue
                        if code == ecodes.KEY_RIGHT:
                            scrub(5.0)
                            continue
                        if code in (ecodes.KEY_LEFTBRACE, ecodes.KEY_RIGHTBRACE):
                            switch(-1 if code == ecodes.KEY_LEFTBRACE else 1)
                            continue
                        if code in (ecodes.KEY_RIGHT, ecodes.KEY_EQUAL, ecodes.KEY_UP):
                            scrub(5.0)
                            continue
                        if code in (ecodes.KEY_LEFT, ecodes.KEY_MINUS, ecodes.KEY_DOWN):
                            scrub(-5.0)
                            continue
                        target = stem_bank.press(CHAR_OF.get(code, ""))
                        if target is not None:
                            d, s = target
                            deck = stem_bank.decks[d]
                            print(colour(f"  {deck.name} · {deck.stems[s][0]}  "
                                         f"{clock(deck.pos)} / {clock(deck.length)}", DIM))
                        continue
                    if event.value == 1:
                        # [ ] work on a 60% board; arrows work where they exist.
                        if code in (ecodes.KEY_LEFT, ecodes.KEY_LEFTBRACE):
                            switch(-1)
                            continue
                        if code in (ecodes.KEY_RIGHT, ecodes.KEY_RIGHTBRACE):
                            switch(1)
                            continue
                        if code in (ecodes.KEY_UP, ecodes.KEY_EQUAL):
                            shift_octave(1)
                            continue
                        if code in (ecodes.KEY_DOWN, ecodes.KEY_MINUS):
                            shift_octave(-1)
                            continue

                    if current_kit and event.value == 1:
                        ch = current_kit.get(code)
                        if ch is not None:
                            piano.hit(presets[preset_index].kit.samples[ch])
                            continue

                    midi = current.get(code)
                    if midi is None:
                        continue
                    if event.value == 1 and code not in held:
                        held.add(code)
                        piano.prepare(midi)
                        tokens[code] = piano.note_on(midi)
                    elif event.value == 0 and code in held:
                        held.discard(code)
                        token = tokens.pop(code, None)
                        if token is not None:
                            piano.note_off(midi, token)

                if not running:
                    break
    finally:
        stop.set()
        try:
            with open(NOW_FILE, "w", encoding="utf-8") as fh:
                json.dump({"active": False, "updated": time.time()}, fh)
        except OSError:
            pass
        audio.close()
        release(grabbed)
        for dev in targets:
            if dev not in grabbed:
                try:
                    dev.close()
                except OSError:
                    pass
        print(colour("\n  keyboard released. bye.\n", DIM))


if __name__ == "__main__":
    main()
