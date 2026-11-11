"""
Presets for pianokb.

CHROMATIC is the free-play layout: two octaves, piano-style (black keys on the
row above the white keys).

SONGS is a list of song presets. Each one can carry a melody, a chord loop, or
both:

    {
        "name":  "My song",
        "root":  "C4",          # where the scale starts
        "scale": "major",       # see SCALES below
        "tempo": 120,           # optional, only printed
        "melody": [             # (note, beats), single notes
            ("E4", 1), ("E4", 1), ("F4", 1), ("G4", 1),
        ],
        "chords": [             # (notes, beats), press them together
            (["C4", "E4", "G4"], 4),
        ],
    }

pianokb lays the scale out left-to-right across the two letter rows, one key
per scale degree, starting on the tonic at or below the lowest note used. Every
key is therefore in the song's palette and no wrong note is possible while the
preset is selected. The melody / chord loop is printed when you switch to it,
so you always have the keys to press in front of you.

A "scale" here does not have to be a textbook seven-note scale: both songs
below borrow notes, so their palettes are eight notes wide to keep every chord
tone reachable from a key.
"""

CHROMATIC = {
    "name": "Piano (2 octaves)",
    "base": "C4",
    "layout": [
        ("z", 0), ("s", 1), ("x", 2), ("d", 3), ("c", 4), ("v", 5),
        ("g", 6), ("b", 7), ("h", 8), ("n", 9), ("j", 10), ("m", 11),
        ("q", 12), ("2", 13), ("w", 14), ("3", 15), ("e", 16), ("r", 17),
        ("5", 18), ("t", 19), ("6", 20), ("y", 21), ("7", 22), ("u", 23),
    ],
    "row_lengths": [12, 12],
}

SCALES = {
    "major": [0, 2, 4, 5, 7, 9, 11],
    "minor": [0, 2, 3, 5, 7, 8, 10],
    "dorian": [0, 2, 3, 5, 7, 9, 10],
    "mixolydian": [0, 2, 4, 5, 7, 9, 10],
    "major-pentatonic": [0, 2, 4, 7, 9],
    "minor-pentatonic": [0, 3, 5, 7, 10],
    # F# major plus the borrowed D, D# and E naturals its chords need
    "major-palette": [0, 2, 4, 5, 7, 8, 9, 10, 11],
    # natural minor plus the raised 7th, so a major V chord is reachable
    "minor-palette": [0, 2, 3, 5, 7, 8, 10, 11],
}

# The two key rows used for song presets, in pitch order (left to right).
DIATONIC_ROWS = ["zxcvbnm,./", "qwertyuiop"]

SONGS = [
    # Kanye West - "Never See Me Again" (Yandhi era).
    # Key F# major, 89 bpm (Hooktheory). Chord loop from the published chord
    # charts (Ultimate Guitar intro: F# F#7 B Bm A#m7 D#m7 G#m7 C#7).
    # One bar per chord.
    {
        "name": "Never See Me Again",
        "root": "F#3",
        "scale": "major-palette",
        "tempo": 89,
        "beat": "never-see-me-again",   # see beats.py: drums + F# loop as a kit
        # Hook line, one beat per note, sitting on the chord of the bar, so it
        # stays consonant with the loop whatever the player's timing.
        "melody": [
            ("F#4", 1), ("A#4", 1), ("C#5", 1), ("A#4", 1),      # F#
            ("F#4", 1), ("E4", 1), ("C#4", 2),                    # F#7
            ("B4", 1), ("D#5", 1), ("C#5", 1), ("B4", 1),          # B
            ("B4", 2), ("D4", 2),                                 # Bm
            ("A#4", 1), ("C#5", 1), ("F4", 1), ("G#4", 1),         # A#m7
            ("D#5", 1), ("F#5", 1), ("C#5", 1), ("A#4", 1),        # D#m7
            ("G#4", 1), ("B4", 1), ("D#5", 1), ("F#5", 1),         # G#m7
            ("C#5", 1), ("F4", 1), ("G#4", 1), ("B4", 1),          # C#7
        ],
        "chords": [
            (["F#3", "A#3", "C#4"], 4),
            (["F#3", "A#3", "C#4", "E4"], 4),
            (["B3", "D#4", "F#4"], 4),
            (["B3", "D4", "F#4"], 4),
            (["A#3", "C#4", "F4", "G#4"], 4),
            (["D#4", "F#4", "A#4", "C#5"], 4),
            (["G#3", "B3", "D#4", "F#4"], 4),
            (["C#4", "F4", "G#4", "B4"], 4),
        ],
    },
    # Big Pun - "Still Not a Player" (feat. Joe).
    # 94 bpm. The loop is F# minor: Bm - A - C# - F#m (chord set as published by
    # ChordU and the capo-2 chart on GuitarTuna; charts disagree slightly on bar
    # order, the four chords are the same). One bar per chord.
    {
        "name": "Still Not a Player",
        "root": "F#3",
        "scale": "minor-palette",
        "tempo": 94,
        # Hook line over Bm - A - C# - F#m, twice, with a longer tag at the end.
        "melody": [
            ("B4", 1), ("C#5", 1), ("D5", 1), ("C#5", 1),          # Bm
            ("A4", 1), ("C#5", 1), ("E5", 1), ("C#5", 1),          # A
            ("C#5", 1), ("F5", 1), ("G#5", 1), ("F5", 1),          # C#
            ("F#5", 2), ("C#5", 1), ("A4", 1),                     # F#m
            ("B4", 0.5), ("B4", 0.5), ("D5", 1), ("F#5", 1), ("D5", 1),   # Bm
            ("E5", 1), ("C#5", 1), ("A4", 2),                      # A
            ("F5", 1), ("G#5", 1), ("F5", 1), ("C#5", 1),          # C#
            ("F#5", 3), ("A4", 1),                                 # F#m
        ],
        "chords": [
            (["B3", "D4", "F#4"], 4),
            (["A3", "C#4", "E4"], 4),
            (["C#4", "F4", "G#4"], 4),
            (["F#3", "A3", "C#4"], 4),
        ],
    },
]
