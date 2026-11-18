# How Stem Deck works

Three processes and two files carry everything. The engine owns the keyboard
and the audio; the UI server never touches either, it only reads a status file
and appends to a command file.

## The pieces

```mermaid
flowchart LR
    subgraph host["Stem Deck host (Linux, PipeWire)"]
        kb["BOYI 60% keyboard<br/>/dev/input/event*"]
        subgraph engine["piano.py — pianokb.service"]
            input["input loop<br/>EVIOCGRAB"]
            mixer["mixer thread<br/>128-sample blocks"]
            writer["status writer<br/>every 250 ms"]
            reader["control reader<br/>every 50 ms"]
        end
        pw["pw-cat → PipeWire<br/>48 kHz f32 stereo"]
        now[("now_playing.json")]
        ctl[("control.jsonl")]
        cache[("stems/.cache/*.f32<br/>memory-mapped")]
        ui["ui/server.py — pianokb-ui.service<br/>:8765"]
        demucs["Demucs + yt-dlp<br/>(.venv)"]
    end
    browser["local web UI<br/>127.0.0.1:8765"]
    console["Command Center<br/>Stem Deck tab"]

    kb -- "key / dial events" --> input
    input --> mixer
    cache --> mixer
    mixer --> pw
    writer --> now
    now --> ui
    ui -- "append commands" --> ctl
    ctl --> reader
    reader --> mixer
    ui --> demucs --> cache
    browser <--> ui
    console <-- "tailnet only" --> ui
```

* **The keyboard is grabbed** (`EVIOCGRAB`), so its keys never reach the
  desktop. The engine waits for every key to be up before grabbing, because a
  key held during the grab would stay stuck for every keyboard on the system.
* **Audio is one raw stream** into `pw-cat` with a 10 ms PipeWire buffer and
  2.7 ms blocks, so a key press is heard after about 13 ms.
* **Stems are memory-mapped**: each file is converted once to raw 48 kHz stereo
  float32 in `stems/.cache/`, so a big library costs no RAM and no load time.
* **Processes talk through files.** The engine writes `now_playing.json`
  (atomically, with `os.replace`) and tails `control.jsonl`. The UI server
  never needs a socket into the engine, and a UI restart never interrupts
  playback.

## One key press, end to end

```mermaid
sequenceDiagram
    participant K as Keyboard
    participant I as Input loop
    participant B as stems.Bank
    participant M as Mixer thread
    participant P as PipeWire
    K->>I: KEY_P down
    I->>B: press("p") → Ski · drums target = 1.0
    loop every 2.7 ms
        M->>B: mix(buf)
        B->>B: gain ramps 0 → 1 over 6 ms (no click)
        B->>B: read drums at the shared playhead
        M->>P: 128 frames
    end
    K->>I: KEY_P up
    I->>B: release("p") → target = 0.0
    Note over B: the playhead keeps running, so pressing again<br/>drops back in where the song has got to
```

Every stem of a song reads from one shared playhead, so the parts stay
sample-locked no matter how they are gated.

## Reverse: hold, latch, release

The apostrophe key is momentary and Space latches it. The effective
direction is `held OR latched`.

```mermaid
stateDiagram-v2
    [*] --> Forward
    Forward --> Held: ' down
    Held --> Forward: ' up
    Held --> HeldLatched: Space
    HeldLatched --> Latched: ' up
    HeldLatched --> Held: Space
    Latched --> Forward: Space
    Latched --> HeldLatched: ' down

    note right of Forward: Space here plays or mutes the whole song
    note right of Latched: stays backwards with no key held
```

Reversing does not jump anywhere: the playhead simply starts walking the other
way from where it is, so the reversed audio begins with what you just heard.
Piano notes started while reversed play tail first.

## The dial: tape speed

The BOYI knob is not a wheel. It sends **Volume Up / Volume Down** key presses
when turned and **Mute** when pressed, all on the keyboard interface the
engine has grabbed, so while Stem Deck runs the knob no longer changes the
system volume.

| dial | stems preset | piano / song presets |
| --- | --- | --- |
| turn | tape speed, 5% a notch, 0.50x – 2.00x | octave up / down |
| press | back to 1.00x if the speed moved, otherwise play / mute the whole song | same |

Speed is varispeed, like a turntable: pitch and tempo move together. At 1.00x
the mixer copies straight slices of the memmap; at any other speed it reads
fractional positions with linear interpolation:

```mermaid
flowchart LR
    pos["playhead p (float)"] --> idx["idx = p ± (0…127) · speed"]
    idx --> split["i0 = floor(idx), frac = idx − i0"]
    split --> lerp["out = s[i0]·(1−frac) + s[i0+1]·frac"]
    lerp --> adv["p ← p ± 128 · speed (mod length)"]
```

Reverse and speed combine freely, so you can drag a song backwards at
half speed.

## Status and control files

`now_playing.json` (written every 250 ms):

```json
{
  "active": true, "preset": "Stems (7 songs)",
  "reverse": false, "reverse_held": false, "reverse_latched": false,
  "speed": 0.9, "last": 6,
  "song": "Ski", "playing": true, "position": 51.2, "duration": 152.0,
  "stems": [{"name": "vocals", "on": true}, "..."],
  "decks": [{"name": "20 Min", "playing": true, "position": 25.1, "duration": 220.6,
             "stems": [{"name": "vocals", "on": false, "key": "1"}, "..."]}, "..."]
}
```

`control.jsonl`: one JSON command per line, appended by the UI server. The
engine reads from its startup offset (old commands are never replayed), drops
anything older than 10 s, and rewinds when the UI truncates the file past
256 KB. See [`integrations/command-center`](../integrations/command-center/)
for the command list.

## Network exposure

The UI server binds `127.0.0.1` unless `PIANOKB_UI_HOST` says otherwise. To
reach it from another machine on a tailnet, add a drop-in:

```ini
# ~/.config/systemd/user/pianokb-ui.service.d/tailnet.conf
[Service]
Environment=PIANOKB_UI_HOST=0.0.0.0
```

Even then it answers only loopback and Tailscale addresses (`100.64.0.0/10`,
`fd7a:115c:a1e0::/48`) and returns 403 to everything else. There is no other
authentication, so do not port-forward it to the internet.
