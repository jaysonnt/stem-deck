# pianokb

Turns one spare keyboard into a piano that is always ready — while your main
keyboard keeps typing normally.

The piano keyboard is **grabbed at the kernel level** (`EVIOCGRAB`). While
`pianokb` runs, that keyboard's key events are delivered only to `pianokb`:
no typing, no shortcuts, no window focus games, nothing leaks to your desktop.
The other keyboard is untouched, so you can keep working while you play.

This install is pinned to the **BOYI 60% hall-effect keyboard** (`config.json`).
Presets — a two-octave chromatic piano, your songs, their beat kits and your
stem decks — are switched from the 60% board itself.

Key-down to sound is about 13 ms: 2.7 ms audio blocks and a 10 ms PipeWire
buffer. Note tails are rendered off the input thread, and each press carries a
token so releasing a key never cuts off a newer press of the same note.

## Requirements

Numpy, python-evdev and PipeWire's `pw-cat`:

```bash
# Arch
sudo pacman -S python-numpy python-evdev pipewire pipewire-audio
```

Your user must be able to read `/dev/input` (usually the `input` group):

```bash
sudo usermod -aG input "$USER"   # log out/in once after this
```

## Run

```bash
python3 piano.py
```

It uses the keyboard in `config.json` and starts immediately. To change which
keyboard is the piano, delete `config.json` or pass one explicitly:

```bash
python3 piano.py --list
python3 piano.py --device "GMMK"
```

Press **Esc** on the piano keyboard to quit (the terminal's Ctrl-C works too).
On exit the grab is released and the keyboard types normally again.

## Playing

Chromatic preset, two octaves:

```
z  s  x  d  c  v  g  b  h  n  j  m
C4 C#4 D4 D#4 E4 F4 F#4 G4 G#4 A4 A#4 B4

q  2  w  3  e  r  5  t  6  y  7  u
C5 C#5 D5 D#5 E5 F5 F#5 G5 G#5 A5 A#5 B5
```

Chords work — hold several keys. The engine always plays at full volume;
use your system volume to taste.

| key on the piano keyboard | does |
| --- | --- |
| letters / numbers above | play notes |
| `[` / `]` (or ← / →) | previous / next preset |
| `-` / `=` (or ↓ / ↑) | transpose the whole keyboard an octave |
| Space (hold, piano/songs) | sustain pedal — notes ring until you let go |
| Space (stems preset) | the same button as the dial press: play / mute the whole song |
| dial (turn) | scrub every stem two seconds a notch (stems preset only) |
| dial (press) | play / mute all six stems of the last song |
| kit preset (`z`…`/`) | drums: kick, 808, snare, clap, hat, open hat, rim, toms, crash |
| kit preset (`q`…`p`) | the loop in key: four bass roots, six chord stabs |
| stems preset (hold a key) | play that stem; release to stop it |
| stems preset `←` / `→` (or `-` / `=`) | scrub every stem five seconds |
| stems preset `[` / `]` | leave / change preset (silences the decks) |
| `'` (apostrophe) | reverse playback: stems run backwards, new piano notes play tail first |
| Caps Lock | show the current preset in the terminal |
| Esc | quit |

Everything works on a 60% board with no arrow keys or function keys.

Song presets lay the song's scale out left to right, one key per scale degree,
starting on the tonic below the lowest melody note. Every key is in the song's
key, so you cannot hit a wrong note — the printed sheet shows which key plays
which note:

```
  keys  Z  Z  X  C  |  C  X  Z  S
  note  E4 E4 F4 G4 | G4 F4 E4 D4
```

## Which preset am I on?

The preset name is printed when you switch and at startup. Tap Caps Lock to
print it again at any time — it never types anything (the keyboard is grabbed)
and does not toggle Caps Lock.

## Sound

- Additive piano with slightly inharmonic partials, per-partial decay, a soft
  hammer noise and a little stereo width.
- A small, block-aligned room reverb (four damped combs, stereo-decorrelated)
  for warmth without washing out fast passages.
- Velocity is varied a few percent per note so repeated notes do not sound
  mechanical.
- Sustain pedal on Space keeps notes ringing after key-up; releasing Space
  damps everything you are holding.

## Your songs

Two songs are loaded: **Never See Me Again** (Kanye West) and **Still Not a
Player** (Big Pun). Each is a preset: a hook melody above its chord loop, both on
the same keys. Never See Me Again also has a **beat kit** for playing the groove
by hand — drums on one row, bass roots and chord stabs on the other.

Every note of both melodies and chords is inside the song's palette, so no key
can sound wrong. The melody lines below the preset name are one beat per note,
and each note falls on the chord of its bar:

```
  keys  F#  A#  C#  A#  |  F#  E  C#
  note  F#4 A#4 C#5 A#4 | F#4 E4 C#4
```

The chord loop prints the same way — press those keys together, one bar per
chord:

```
  chord      Bm       A      C#     F#m
  key     V+N+.   C+B+M   B+,+/   Z+C+B
```

Open `songs.py`. A song is a name, a key, a scale, and either a `melody` of
`(note, beats)` or a `chords` list of `(notes, beats)`:

```python
{
    "name": "My song",
    "root": "C4",            # where the scale starts
    "scale": "major",        # see SCALES in songs.py
    "tempo": 120,            # optional, only printed
    "melody": [("E4", 1), ("E4", 1), ("F4", 1), ("G4", 1)],
    # or, for a loop: chords = [(["C4", "E4", "G4"], 4)],
},
```

Scales do not have to be seven notes: the two songs borrow notes (D natural in
an F# major song, the raised 7th over a minor key), so `major-palette` and
`minor-palette` are eight and nine notes wide. That keeps every chord tone on a
real key — `--check` is the quick way to confirm nothing prints as `-`.

`python3 piano.py --check` prints every preset and keymap without grabbing a
keyboard.

## Stems — play a song's parts

Drop one folder per song in `stems/`, with one audio file per stem:

```
stems/Never See Me Again/vocals.wav
stems/Never See Me Again/drums.wav
stems/Never See Me Again/bass.wav
stems/Never See Me Again/other.wav
```

A stem that comes in halves is stitched back together in order — number the parts
(`drums-1.wav`, `drums-2.wav`, or `drums (1).wav`, `drums (2).wav`), or put one
subfolder per half (`first half/drums.wav`, `second half/drums.wav`) with the same
stem names in each. The join is a raw float32 append, so it is sample-exact, but
the halves must line up: cut the song at one exact point and split the stems from
the same halves, otherwise the second half of one stem will drift against the
others.

Any format `ffmpeg` reads is fine, but **use WAV or FLAC** (what stem splitters
write). MP3 frames carry padding that can shift one stem a few milliseconds
against the others, which is exactly what breaks the lock between parts.

The **Stems** preset appears automatically once at least one song is there, and
pianokb starts on it (so keys play stems instead of piano notes) — `[` / `]`
go back to the piano and songs. The layout is yours to arrange though:

| key | stem |
| --- | --- |
| `1 2 3` / `Q W E` | song 1: vocals, drums, bass / other, piano, guitar |
| `4 5 6` / `R T Y` | song 2 |
| `7 8 9` / `U I O` | song 3 |
| `A S D` / `Z X C` | song 4 |
| `F G H` / `V B N` | song 5 |
| `J K L` / `M , .` | song 6 |

Every stem gets its own key, and each song's six stems sit together in one
3-wide, 2-tall block: top row vocals, drums, bass; the row below other, piano,
guitar. A whole song is under one hand instead of running off the end of a row.
Blocks fill left to right, top band first, in folder-name order, so six songs
fill 36 of the 45 keys (the four 60% rows, plus the spare grave, backslash,
Tab, Enter and Backspace keys on the 65% board). Hold the ones you want
and the song plays, past its end and back to the start, for as long as you keep
holding — it never stops on its own. Rearrange any of it with `keys` (below).

Press the dial — or Space, the big button by your thumb — to play (or mute
again) every stem of the song you last touched, so the whole track comes out
of one control. Turning the dial scrubs all stems two seconds a notch, the same
timeline as `←` / `→`; outside the stems preset a notch does nothing, because
there are no decks to move. Space only becomes that button on the stems preset:
elsewhere it is still the sustain pedal.

Press a key once and that song's transport starts; it keeps running after you
let go, and the stem just mutes. Press again and it unmutes wherever the song
has got to, so letting go for a second is a drop-out, not a pause. Every stem
of a song reads from one shared playhead, so they stay sample-locked, and all
decks share the same timeline. `←` / `→` — or `-` / `=` on a board without
arrows — scrub **all** stems five seconds at a time (wrapping at the ends) and
print the new position; `[` / `]` leave the preset, silence the decks and reset
the playheads. With reverse on (`'`) the decks run backwards and scrubbing
still moves along the timeline, so `→` stays "later in the song".

## Arranging the keys — the web UI

```
keys        # opens http://127.0.0.1:8765 in the browser
keys --tui  # the terminal menu (python3 pianokb/map.py)
```

`keys` is a small launcher (`pianokb/menu`) linked into `~/.local/bin`, so it
works from any directory. It starts the UI service if needed and opens it.

The page draws the four rows with every key tagged by the stem it plays,
coloured per song. Drag a key onto another to move it, or swap the two if the
target is taken; click one key and then another to do the same. Every move is
saved to `pianokb/keymap.json` and pianokb restarts with it a moment later.
`Reset layout` returns to the default blocks. The UI never grabs the keyboard,
so the spare board keeps working while it is open.

Every song card also has a **bench** button: it moves the song to
`stems/.bench`, off the board and out of the key budget, without deleting it.
Benched songs appear under **Not on the board** with an **Add to board**
button. The player at the top shows the song last touched, which of its stems
are on, and the shared playhead.

### Adding songs and splitting stems

**Add a song** takes a full mix — split locally into six stems with Demucs
(`htdemucs_6s`: vocals, drums, bass, other, piano, guitar) — stem files you
already have, or a link. For a link, yt-dlp downloads the audio first and the
same split runs on it, so a YouTube URL becomes six stems in one step. A
progress bar and the splitter's log show what is happening, and the song
appears on the board when it finishes. A ten-minute
mix takes a few minutes on CPU; the first split also downloads the ~80 MB
model. This is the local counterpart of the online BS-Roformer split: same six
stems, no upload, but RoFormer models usually separate a little cleaner.

The splitter and yt-dlp live in `pianokb/.venv` and are installed once:

```bash
./pianokb/ui/setup-venv.sh
```

The UI itself is the `pianokb-ui` user unit, so it stays up without a terminal:

```bash
systemctl --user status pianokb-ui
journalctl --user -u pianokb-ui -f
systemctl --user restart pianokb-ui
```

Forty-five keys are all the board has: seven six-stem songs fit in 42, and
past 45 stems the extra ones have no key — the UI says how many, and **bench**
frees a whole song's worth of keys without deleting it.

Files are converted once to 48 kHz stereo float32 into `stems/.cache/` and then
memory-mapped: a large library costs no RAM and no startup time. Delete
`stems/.cache/` to force re-conversion after replacing a file. If a song has
more than four stems, it still works — its stems simply take more keys, and
songs beyond the fortieth key are left off the map (the preset says so).

## Always on at login

Installed as a systemd user unit, `~/.config/systemd/user/pianokb.service`:

```ini
[Unit]
Description=pianokb — spare keyboard as piano
After=pipewire.service
Wants=pipewire.service

[Service]
ExecStart=/usr/bin/python3 /path/to/pianokb/piano.py
WorkingDirectory=/path/to/pianokb
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
```

It starts with your session and re-grabs the keyboard if it ever exits.
Manage it with:

```bash
systemctl --user status pianokb
systemctl --user stop pianokb       # give the keyboard back now
systemctl --user start pianokb
systemctl --user disable pianokb    # stop it starting at login
journalctl --user -u pianokb -f     # watch what it is doing
```

## Notes

- The grabbed keyboard is completely unavailable to the desktop while pianokb
  runs. Pick the spare one, not the keyboard you type on.
- Unplugging the piano keyboard is safe: it releases everything and re-grabs
  automatically when the keyboard comes back (the service waits for it).
- Before grabbing, pianokb waits until no key is held. Grabbing mid-hold would
  hide that key's release from the desktop and leave it stuck for every
  keyboard, so the wait has no timeout; the log says when it is waiting.
- If audio ever stops (PipeWire restart, sink change), the stream reopens on
  its own; there is no need to restart pianokb.
- Key events trigger only on press, so holding a key rings the note out; the
  note is damped when you release it (unless Space is held).
