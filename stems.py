#!/usr/bin/env python3
"""Stem decks: play a song's parts from the keyboard, like a live remixer.

Drop one folder per song into <project>/stems, with one audio file per stem:

    stems/Never See Me Again/vocals.wav
    stems/Never See Me Again/drums.wav
    stems/Never See Me Again/bass.wav
    stems/Never See Me Again/other.wav

A stem may also be split into halves, and the parts are stitched back together
in order (raw float32 concatenation, so the join is sample-exact):

    stems/Never See Me Again/drums-1.wav      # or drums (1).wav
    stems/Never See Me Again/drums-2.wav

or one subfolder per half, each holding the same stem names:

    stems/Never See Me Again/first half/drums.wav
    stems/Never See Me Again/second half/drums.wav

Each file is converted once to 48 kHz stereo float32 in stems/.cache and then
memory-mapped, so a library of songs costs almost no RAM and no load time.

All stems of a song read from one shared playhead, so they are sample-locked.
The first press starts that song's transport and it keeps running: letting go
mutes the stem but the playhead carries on, so pressing it again drops you back
in where the song got to, not where you left it. Hold to the end and it loops,
forever.

Keys are handed out in order, but pianokb/keymap.json overrides that: run
pianokb/map.py to move a stem to another key and see the whole board.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STEMS_DIR = os.environ.get("PIANOKB_STEMS") or os.path.join(ROOT, "stems")
CACHE_DIR = os.path.join(STEMS_DIR, ".cache")
KEYMAP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "keymap.json")

SR = 48_000
RAMP = 0.006 * SR                    # ~6 ms gate fade: no clicks on press/release
AUDIO_EXT = {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".aiff", ".aif", ".webm"}

# 45 playable keys: the four 60% rows plus the spare keys on the 65% board
# (` \ Tab Enter Backspace). Six stems per song still get one block each.
KEY_ROWS = ["1234567890", "qwertyuiop", "asdfghjkl;", "zxcvbnm,./",
            "\u0060\\\u21e5\u23ce\u232b"]

# Preferred left-to-right order for a song's stems.
STEM_ORDER = (
    "vocals", "vocal", "acapella", "drums", "drum", "bass", "other",
    "piano", "keys", "synth", "guitar", "instrumental",
)

# Default arrangement: six stems per song as one 3x2 block, blocks side by side,
# so every song sits together under one pair of hands instead of running off the
# end of a row.
SONG_BLOCKS = (
    ("1", "2", "3", "q", "w", "e"),
    ("4", "5", "6", "r", "t", "y"),
    ("7", "8", "9", "u", "i", "o"),
    ("a", "s", "d", "z", "x", "c"),
    ("f", "g", "h", "v", "b", "n"),
    ("j", "k", "l", "m", ",", "."),
)


def clock(samples: int) -> str:
    total = int(samples / SR)
    return f"{total // 60}:{total % 60:02d}"


# A trailing part marker: "drums-2", "drums 02", "drums (2)", "drums_part2".
# A bare "drums2" is left alone, so two unrelated stems are never glued together.
PART_NUM = re.compile(
    r"^(?P<head>.+?)(?:\s*[\(\[\s.]\s*(?:part\s*)?(?P<bracket>\d{1,2})\s*[\)\]]"
    r"|[\s._\-]+(?:part[\s._\-]*)?(?P<plain>\d{1,2}))$",
    re.I,
)


def _rank(name: str) -> tuple[int, str]:
    stem = name.lower()
    try:
        return (STEM_ORDER.index(stem), stem)
    except ValueError:
        return (len(STEM_ORDER), stem)


def _natural(name: str):
    """Sort key so "part2" comes before "part10"."""
    return [int(bit) if bit.isdigit() else bit.lower() for bit in re.split(r"(\d+)", name)]


def _stem_of(filename: str) -> tuple[str, int]:
    """Split a file name into its stem name and its part number (0 if none)."""
    base = os.path.splitext(os.path.basename(filename))[0]
    match = PART_NUM.match(base)
    if match:
        head = match.group("head").strip(" ._-()[]")
        if head and not head.isdigit():
            return head, int(match.group("bracket") or match.group("plain"))
    return base.strip(), 0


def _convert(src: str, dst: str) -> None:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", src,
         "-ac", "2", "-ar", str(SR), "-f", "f32le", dst],
        check=True,
    )


def _load(folder: str, stem: str, paths: list[str]) -> np.ndarray:
    """Convert (once) to 48 kHz stereo f32 and return a read-only memmap.

    Several parts of one stem are appended in the given order; each is decoded to
    raw float32 first, so the join adds no gap, no overlap and no resampling.
    """
    stats = [(p, os.stat(p)) for p in paths]
    if len(paths) == 1:
        signature = f"{stats[0][1].st_size}_{int(stats[0][1].st_mtime)}"
    else:
        join = "|".join(f"{os.path.basename(p)}:{s.st_size}:{int(s.st_mtime)}" for p, s in stats)
        signature = f"{len(paths)}parts_{hashlib.md5(join.encode()).hexdigest()[:10]}"
    tag = re.sub(
        r"[^A-Za-z0-9._-]+", "-",
        f"{os.path.basename(folder)}__{stem}__{signature}",
    )

    out = os.path.join(CACHE_DIR, tag + ".f32")
    if not os.path.exists(out):
        os.makedirs(CACHE_DIR, exist_ok=True)
        part = out + ".part"
        one = part + ".one"
        try:
            if len(paths) == 1:
                _convert(paths[0], part)
            else:
                with open(part, "wb") as dst:
                    for src in paths:
                        _convert(src, one)
                        with open(one, "rb") as chunk:
                            shutil.copyfileobj(chunk, dst, 4 << 20)
        except BaseException:
            for leftover in (part, one):
                if os.path.exists(leftover):
                    os.remove(leftover)
            raise
        finally:
            if os.path.exists(one):
                os.remove(one)
        os.replace(part, out)

    data = np.memmap(out, dtype=np.float32, mode="r")
    data = data[: (len(data) // 2) * 2]          # whole stereo frames only
    return data.reshape(-1, 2)


def load_keymap(path: str = KEYMAP_FILE) -> dict[str, str]:
    """Read "Song|stem" -> key. A missing or broken file simply means defaults."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        print(f"  ! keymap: {path} is not a mapping — using the default layout")
        return {}
    return {
        k: v for k, v in data.items()
        if isinstance(k, str) and isinstance(v, str) and len(v) == 1
    }


def save_keymap(keymap: dict[str, str], path: str = KEYMAP_FILE) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(dict(sorted(keymap.items())), fh, indent=2)
        fh.write("\n")


def assign_keys(
    songs: list[tuple[str, list[str]]],
    keymap: dict[str, str] | None = None,
) -> tuple[list[list[str]], dict[str, tuple[int, int]]]:
    """Give every stem a key: saved keys first, then each song its own block."""
    flat = [k for row in KEY_ROWS for k in row]
    chosen = [[""] * len(names) for _song, names in songs]
    claimed: set[str] = set()
    keymap = keymap or {}

    for d, (song, names) in enumerate(songs):
        for s, name in enumerate(names):
            ch = keymap.get(f"{song}|{name}", "")
            if ch in flat and ch not in claimed:
                chosen[d][s] = ch
                claimed.add(ch)

    # Then hand out the compact blocks: the block a song already partly uses,
    # otherwise the first untouched one. Anything left over falls back to the
    # remaining keys in board order (more songs than blocks, or extra stems).
    blocks = [list(block) for block in SONG_BLOCKS]
    for chars in chosen:
        empty = [s for s, ch in enumerate(chars) if not ch]
        if not empty:
            continue
        own = {ch for ch in chars if ch}
        block = next((b for b in blocks if own.intersection(b)), None)
        if block is None:
            block = next((b for b in blocks if not claimed.intersection(b)), None)
        if block is None:
            continue
        blocks.remove(block)
        for s, ch in zip(empty, (c for c in block if c not in claimed)):
            chars[s] = ch
            claimed.add(ch)

    free = iter([c for c in flat if c not in claimed])
    for chars in chosen:
        for s, ch in enumerate(chars):
            if not ch:
                chars[s] = next(free, "")

    by_char: dict[str, tuple[int, int]] = {}
    for d, chars in enumerate(chosen):
        for s, ch in enumerate(chars):
            if ch:
                by_char[ch] = (d, s)
    return chosen, by_char


class Deck:
    """One song: several stems sharing a position."""

    __slots__ = ("name", "stems", "length", "pos", "gains", "targets", "playing")

    def __init__(self, name: str, stems: list[tuple[str, np.ndarray]]):
        self.name = name
        self.stems = stems
        self.length = min(len(sample) for _, sample in stems)
        self.pos = 0
        self.playing = False        # transport latches on at the first press
        self.gains = [0.0] * len(stems)
        self.targets = [0.0] * len(stems)


class Bank:
    """Every deck on the keyboard, mixed into the piano's audio stream."""

    def __init__(self, decks: list[Deck], keymap: dict[str, str] | None = None):
        self.decks = decks
        self.gain = 1.0
        self.reverse = 1        # -1 plays every deck backwards
        self.speed = 1.0        # tape speed: pitch and tempo together (the dial)
        self.last = 0
        self.unmapped = 0

        songs = [(deck.name, [name for name, _ in deck.stems]) for deck in decks]
        chosen, self.by_char = assign_keys(songs, keymap)
        self.keys: list[list[tuple[str, int]]] = []
        for deck, chars in zip(decks, chosen):
            keys = [(ch, s) for s, ch in enumerate(chars) if ch]
            if len(keys) < len(deck.stems):
                self.unmapped += 1
            self.keys.append(keys)

    # -- keyboard ---------------------------------------------------------

    def press(self, ch: str) -> tuple[int, int] | None:
        target = self.by_char.get(ch)
        if target is None:
            return None
        d, s = target
        self.decks[d].targets[s] = 1.0
        self.decks[d].playing = True
        self.last = d
        return target

    def release(self, ch: str) -> bool:
        target = self.by_char.get(ch)
        if target is None:
            return False
        d, s = target
        self.decks[d].targets[s] = 0.0
        return True

    def release_all(self) -> None:
        for deck in self.decks:
            for s in range(len(deck.targets)):
                deck.targets[s] = 0.0

    # -- transport --------------------------------------------------------

    def scrub(self, seconds: float) -> None:
        delta = int(seconds * SR)
        for deck in self.decks:
            if deck.length:
                deck.pos = (deck.pos + delta) % deck.length

    def stop(self) -> None:
        """Silence everything now, without a fade (used on preset changes)."""
        self.release_all()
        for deck in self.decks:
            for s in range(len(deck.gains)):
                deck.gains[s] = 0.0
            deck.playing = False
            deck.pos = 0

    # -- audio ------------------------------------------------------------

    def mix(self, buf: np.ndarray) -> None:
        """Add every audible stem into ``buf`` (in place, float32)."""
        if self.gain <= 0.0:
            return
        n = len(buf)
        fade = np.float32(n / RAMP)
        back = self.reverse < 0
        rate = float(self.speed)
        varispeed = abs(rate - 1.0) > 1e-6
        if varispeed:
            # Positions read this block, as a tape would: forwards from the
            # playhead, or backwards from just behind it.
            steps = (np.arange(n, dtype=np.float64) + (1.0 if back else 0.0)) * rate
            steps = -steps if back else steps

        for deck in self.decks:
            if not deck.length:
                continue
            targets = deck.targets
            gains = deck.gains
            if not deck.playing and not any(targets) and not any(gains):
                continue

            here = int(deck.pos)
            if varispeed:
                idx = deck.pos + steps
                i0 = np.floor(idx).astype(np.int64)
                frac = (idx - i0).astype(np.float32)[:, None]
                i0 %= deck.length
                i1 = (i0 + 1) % deck.length
            for s, (_name, sample) in enumerate(deck.stems):
                g = gains[s]
                target = targets[s]
                if target > g:
                    g = min(target, g + fade)
                elif target < g:
                    g = max(target, g - fade)
                gains[s] = g
                if g <= 1e-4:
                    continue
                if varispeed:
                    data = sample[i0] * (1.0 - frac) + sample[i1] * frac
                elif back:
                    before = here - n
                    if before >= 0:
                        data = sample[before:here][::-1]
                    else:                   # wrapped past the start of the song
                        data = sample[np.arange(before, here) % deck.length][::-1]
                else:
                    after = here + n
                    if after <= deck.length:
                        data = sample[here:after]
                    else:                   # wrapped past the end of the song
                        data = np.concatenate((sample[here:], sample[:after - deck.length]))
                if len(data) < n:
                    chunk = np.zeros((n, 2), dtype=np.float32)
                    chunk[:len(data)] = data
                    data = chunk
                buf += data * np.float32(g * self.gain)

            # Once a song has been pressed the playhead keeps running, even
            # while every stem is muted: releasing mutes, it does not pause.
            if deck.playing:
                if varispeed:
                    deck.pos = (deck.pos + (-n if back else n) * rate) % deck.length
                else:
                    deck.pos = (here - n) % deck.length if back else (here + n) % deck.length


def _audio_files(folder: str) -> list[str]:
    """Audio files in one folder, in natural order."""
    return [
        os.path.join(folder, f)
        for f in sorted(os.listdir(folder), key=_natural)
        if not f.startswith(".") and os.path.splitext(f)[1].lower() in AUDIO_EXT
    ]


def _stem_parts(folder: str) -> list[tuple[str, list[str]]]:
    """Group one song folder into stems, with their parts in playback order.

    Files loose in the folder come first, then each subfolder in name order, so
    either ``drums-1.wav`` / ``drums-2.wav`` or ``first half/drums.wav`` /
    ``second half/drums.wav`` stitch into one stem.
    """
    parts: list[list[str]] = []
    loose = _audio_files(folder)
    if loose:
        parts.append(loose)
    for entry in sorted(os.listdir(folder), key=_natural):
        sub = os.path.join(folder, entry)
        if entry.startswith(".") or not os.path.isdir(sub):
            continue
        inside = _audio_files(sub)
        if inside:
            parts.append(inside)

    groups: dict[str, list[tuple[int, int, str]]] = {}
    display: dict[str, str] = {}
    for part, files in enumerate(parts):
        for path in files:
            stem, number = _stem_of(path)
            key = stem.lower()
            display.setdefault(key, stem)
            groups.setdefault(key, []).append((part, number, path))

    return [
        (display[key], [path for _part, _num, path in sorted(groups[key])])
        for key in sorted(groups, key=lambda k: _rank(display[k]))
    ]


def discover(root: str = STEMS_DIR) -> Bank:
    """Load every song folder under ``root``."""
    decks: list[Deck] = []
    keymap = load_keymap()
    if not os.path.isdir(root):
        return Bank(decks, keymap)

    loose: list[str] = []
    for entry in sorted(os.listdir(root)):
        if entry.startswith("."):
            continue
        path = os.path.join(root, entry)
        if not os.path.isdir(path):
            if os.path.splitext(entry)[1].lower() in AUDIO_EXT:
                loose.append(entry)
            continue
        stems: list[tuple[str, np.ndarray]] = []
        for stem, paths in _stem_parts(path):
            try:
                sample = _load(path, stem, paths)
            except (OSError, subprocess.CalledProcessError) as exc:
                names = ", ".join(os.path.basename(p) for p in paths)
                print(f"  ! stems: could not load {entry}/{names}: {exc}")
                continue
            stems.append((stem, sample))
            if len(paths) > 1:
                minutes = len(sample) / SR / 60.0
                print(f"  stems: {entry}/{stem}: stitched {len(paths)} parts "
                      f"-> {minutes:.1f} min")
        if stems:
            decks.append(Deck(entry, stems))

    if loose:
        print(f"  ! stems: {len(loose)} file(s) directly in {root} ignored — "
              f"put each song's stems inside their own folder.")
    return Bank(decks, keymap)
