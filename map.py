#!/usr/bin/env python3
"""The stem key menu: the layout on screen, movable with the mouse.

    python3 pianokb/map.py            # curses menu (mouse and keyboard)
    python3 pianokb/map.py --show     # plain text layout
    python3 pianokb/map.py --reset    # back to the default order

Click a key that has a stem, then click the key you want it on; if that key is
taken the two swap. Arrow keys move the cursor, Enter picks and places, `s`
saves, `r` resets, `q` quits. Nothing is grabbed, so the spare board keeps
working while this is open. The layout is saved to pianokb/keymap.json and
pianokb is restarted with it when you quit.
"""

from __future__ import annotations

import curses
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import stems

CELL = 4            # screen columns per key
MARGIN = 2
TOP = 3             # first key row, after the title
SONG_COLOURS = (
    curses.COLOR_CYAN, curses.COLOR_MAGENTA, curses.COLOR_YELLOW,
    curses.COLOR_GREEN, curses.COLOR_BLUE, curses.COLOR_RED,
)

has_colour = False


def songs() -> list[tuple[str, list[str]]]:
    """Every song folder and its stems, in the order the deck will play them."""
    out: list[tuple[str, list[str]]] = []
    root = stems.STEMS_DIR
    if not os.path.isdir(root):
        return out
    for entry in sorted(os.listdir(root)):
        path = os.path.join(root, entry)
        if entry.startswith(".") or not os.path.isdir(path):
            continue
        names = [name for name, _paths in stems._stem_parts(path)]
        if names:
            out.append((entry, names))
    return out


def cell_at(x: int, y: int) -> tuple[int, int] | None:
    """Which key of the drawn board is under screen point (x, y)."""
    row = (y - TOP) // 3
    if not 0 <= row < len(stems.KEY_ROWS) or (y - TOP) % 3 not in (0, 1):
        return None
    col = (x - MARGIN) // CELL
    if not 0 <= col < len(stems.KEY_ROWS[row]):
        return None
    return row, col


def mouse_on() -> None:
    """Ask the terminal for SGR mouse reports (button presses only)."""
    try:
        os.write(1, b"\033[?1000h\033[?1006h")
    except OSError:
        pass


def mouse_off() -> None:
    try:
        os.write(1, b"\033[?1006l\033[?1000l")
    except OSError:
        pass


def read_more(screen) -> str:
    """Whatever followed a bare ESC, so mouse reports are not lost."""
    screen.timeout(40)
    out = ""
    try:
        while len(out) < 24:
            ch = screen.getch()
            if not 0 <= ch < 256:
                break
            out += chr(ch)
            if out.startswith("[<") and out[-1] in "Mm":
                break
            if out.startswith("[M") and len(out) >= 5:
                break
            if len(out) == 2 and out[0] in "[O" and out[1] in "ABCDHF":
                break
            if out[0] not in "[O":
                break
    finally:
        screen.timeout(-1)
    return out


def mouse_cell(seq: str):
    """A left-button press gives a board cell, a release gives False, anything
    else (a bare ESC, an arrow key) gives None."""
    if seq.startswith("[<"):                      # SGR: ESC [ < b ; x ; y M
        body = seq[2:]
        if not body or body[-1] not in "Mm":
            return None
        try:
            button, x, y = (int(part) for part in body[:-1].split(";"))
        except ValueError:
            return None
        if body[-1] == "m" or button & 3:         # release, or not the left button
            return False
        return cell_at(x - 1, y - 1)
    if seq.startswith("[M") and len(seq) >= 5:     # X10: ESC [ M cb cx cy
        cb = ord(seq[2]) - 32
        if cb & 3:                                # release or right button
            return False
        return cell_at(ord(seq[3]) - 33, ord(seq[4]) - 33)
    return None


def pair_for(d: int) -> int:
    return curses.color_pair(d % len(SONG_COLOURS) + 1) if has_colour else 0


def put(screen, y: int, x: int, text: str, attr: int = 0) -> None:
    try:
        screen.addstr(y, x, text, attr)
    except curses.error:
        pass


def show_text(rows: list[tuple[str, list[str]]], keymap: dict[str, str]) -> None:
    chosen, by_char = stems.assign_keys(rows, keymap)
    print()
    print("  the board")
    for row in stems.KEY_ROWS:
        print("  " + " ".join(f" {ch.upper()} " for ch in row))
        tags = []
        for ch in row:
            owner = by_char.get(ch)
            tags.append("   " if owner is None
                        else rows[owner[0]][1][owner[1]][:3].ljust(3))
        print("  " + " ".join(tags))
        print()
    print("  stems")
    n = 0
    for d, (song, names) in enumerate(rows):
        for s, name in enumerate(names):
            n += 1
            print(f"  {n:>2}  {chosen[d][s].upper() or '-':<2}  {song} · {name}")
    print()
    print(f"  layout file: {stems.KEYMAP_FILE}")


def draw(screen, rows, keymap, cursor, picked, message) -> None:
    screen.erase()
    height, width = screen.getmaxyx()
    if height < 19 or width < 46:
        put(screen, 0, 0, "resize the terminal to at least 46x19", curses.A_BOLD)
        screen.refresh()
        return

    _, by_char = stems.assign_keys(rows, keymap)

    put(screen, 0, 1, "pianokb · stem keys", curses.A_BOLD)
    hint = "click a key, then click where it goes   s save   r reset   q quit"
    if width > len(hint) + 28:
        put(screen, 0, width - len(hint) - 1, hint, curses.A_DIM)

    for r, row in enumerate(stems.KEY_ROWS):
        y = TOP + r * 3
        for c, ch in enumerate(row):
            x = MARGIN + c * CELL
            owner = by_char.get(ch)
            attr = 0
            tag = "   "
            if owner is not None:
                d, s = owner
                tag = rows[d][1][s][:3].ljust(3)
                attr = pair_for(d)
            if (r, c) == cursor:
                attr |= curses.A_UNDERLINE | curses.A_BOLD
            if picked == (r, c):
                attr |= curses.A_REVERSE
            put(screen, y, x, f" {ch.upper()} ", attr)
            put(screen, y + 1, x, tag, attr)

    legend_y = TOP + len(stems.KEY_ROWS) * 3
    x = MARGIN
    for d, (song, _names) in enumerate(rows):
        text = f" {song} "
        if x + len(text) >= width:
            break
        put(screen, legend_y, x, text, pair_for(d))
        x += len(text)

    put(screen, legend_y + 1, MARGIN, message[: max(1, width - MARGIN - 1)],
        curses.A_DIM)
    screen.refresh()


def run(screen, rows, keymap) -> bool:
    global has_colour
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    try:
        curses.start_color()
        curses.use_default_colors()
        for i, colour in enumerate(SONG_COLOURS):
            curses.init_pair(i + 1, colour, -1)
        has_colour = curses.has_colors()
    except curses.error:
        has_colour = False
    # Raw bytes, so xterm mouse reports arrive intact instead of being
    # swallowed as an unknown escape sequence; arrows are decoded below.
    screen.keypad(False)
    mouse_on()

    def occupant(cell):
        r, c = cell
        key = stems.KEY_ROWS[r][c]
        return key, stems.assign_keys(rows, keymap)[1].get(key)

    cursor = (0, 0)
    picked: tuple[int, int] | None = None
    dirty = False
    message = "click a key that has a stem to pick it up"

    def move_cursor(dc: int, dr: int) -> None:
        nonlocal cursor
        row = max(0, min(len(stems.KEY_ROWS) - 1, cursor[0] + dr))
        col = max(0, min(len(stems.KEY_ROWS[row]) - 1, cursor[1] + dc))
        cursor = (row, col)

    def do_move(cell) -> None:
        nonlocal picked, dirty, message
        key, owner = occupant(cell)

        if picked is None:
            if owner is None:
                message = f"{key.upper()} is empty — pick a key that has a stem"
                return
            picked = cell
            d, s = owner
            message = f"{rows[d][0]} · {rows[d][1][s]} picked — click its new key"
            return

        src_key, src_owner = occupant(picked)
        if src_owner is None or cell == picked:
            picked = None
            message = "nothing moved"
            return

        sd, ss = src_owner
        keymap[f"{rows[sd][0]}|{rows[sd][1][ss]}"] = key
        if owner is not None:
            od, os_ = owner
            keymap[f"{rows[od][0]}|{rows[od][1][os_]}"] = src_key
            message = (f"{rows[sd][1][ss]}: {src_key.upper()} → {key.upper()}  "
                       f"swapped with {rows[od][1][os_]}")
        else:
            message = f"{rows[sd][1][ss]}: {src_key.upper()} → {key.upper()}"
        stems.save_keymap(keymap)
        dirty = True
        picked = None

    while True:
        draw(screen, rows, keymap, cursor, picked, message)
        try:
            ch = screen.getch()
        except KeyboardInterrupt:
            break

        if ch == curses.KEY_RESIZE:
            continue
        if ch == 27:
            seq = read_more(screen)
            cell = mouse_cell(seq)
            if cell is False:                     # mouse release: nothing to do
                continue
            if cell is not None:
                cursor = cell
                do_move(cell)
            elif seq in ("[A", "OA"):
                move_cursor(0, -1)
            elif seq in ("[B", "OB"):
                move_cursor(0, 1)
            elif seq in ("[C", "OC"):
                move_cursor(1, 0)
            elif seq in ("[D", "OD"):
                move_cursor(-1, 0)
            elif picked is not None:
                picked = None
                message = "cancelled"
            else:
                break
            continue

        if ch in (ord("q"), ord("Q")):
            break
        if ch in (ord("s"), ord("S")):
            stems.save_keymap(keymap)
            message = f"saved to {stems.KEYMAP_FILE}"
            continue
        if ch in (ord("r"), ord("R")):
            keymap.clear()
            stems.save_keymap(keymap)
            dirty = True
            picked = None
            message = "back to the default order"
            continue
        if ch in (10, 13, ord(" ")):
            do_move(cursor)

    mouse_off()
    if dirty:
        stems.save_keymap(keymap)
    return dirty


def restart() -> None:
    try:
        done = subprocess.run(
            ["systemctl", "--user", "restart", "pianokb"],
            capture_output=True, text=True,
        )
    except (FileNotFoundError, OSError):
        return
    if done.returncode == 0:
        print("  pianokb restarted with the new layout")
    else:
        print("  could not restart pianokb — run: "
              "systemctl --user restart pianokb")


def main() -> None:
    rows = songs()
    if not rows:
        sys.exit(f"  no song folders in {stems.STEMS_DIR}")

    keymap = stems.load_keymap()
    reset = "--reset" in sys.argv
    if reset:
        keymap.clear()
        stems.save_keymap(keymap)

    plain = reset or "--show" in sys.argv or not sys.stdout.isatty() or not sys.stdin.isatty()
    if plain:
        show_text(rows, keymap)
        return

    try:
        dirty = curses.wrapper(run, rows, keymap)
    finally:
        mouse_off()
    if dirty:
        restart()


if __name__ == "__main__":
    main()
