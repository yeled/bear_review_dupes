#!/usr/bin/env python3
"""
Interactive side-by-side duplicate reviewer for Bear notes.

Controls:
  Space   — skip (keep both, move to next pair)
  D       — trash the COPY  (right panel, the " 2" note)
  d       — trash the BASE  (left panel, the original)
  ↑ / ↓  — scroll both panels together
  q       — quit

Deletion calls: bearcli trash <uuid>

Usage:
    python3 bear_review_dupes.py
"""

import curses
import subprocess
import sqlite3
import re
import sys
from pathlib import Path

DB_PATH = Path.home() / "Library/Group Containers/9K33E3U3T4.net.shinyfrog.bear/Application Data/database.sqlite"


# ── data loading ─────────────────────────────────────────────────────────────

def normalize(content: str) -> str:
    lines = (content or "").split("\n")
    out = []
    for i, line in enumerate(lines):
        s = line.strip()
        if i == 0:
            continue
        if re.match(r'^(#[\w/\-]+ *)+$', s):
            continue
        if re.match(r'^\[.+\]\(http://maps\.apple\.com', s):
            continue
        if re.match(r'^!\[\]\(.+\)$', s):
            continue
        if re.match(r'^\[[\w.\-| &\[\]]+\]\([\w.\-|%& \[\]]+\)(?:<!--.*-->)?$', s):
            continue
        out.append(s)
    while out and out[0] == "":
        out.pop(0)
    while out and out[-1] == "":
        out.pop()
    collapsed, prev_blank = [], False
    for line in out:
        if line == "":
            if not prev_blank:
                collapsed.append(line)
            prev_blank = True
        else:
            collapsed.append(line)
            prev_blank = False
    return "\n".join(collapsed)


def load_pairs():
    if not DB_PATH.exists():
        print(f"ERROR: Bear database not found at:\n  {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT ZUNIQUEIDENTIFIER as id, ZTITLE as title, ZTEXT as content
        FROM ZSFNOTE
        WHERE ZTRASHED = 0 AND ZARCHIVED = 0 AND ZENCRYPTED = 0
    """)
    rows = cur.fetchall()
    conn.close()

    by_title = {}
    for row in rows:
        title = (row["title"] or "").strip()
        by_title[title] = {
            "id":      row["id"],
            "title":   title,
            "content": row["content"] or "",
            "norm":    normalize(row["content"] or ""),
        }

    suffix_re = re.compile(r'^(.+?) (\d+)$')
    pairs = []
    for title, note in by_title.items():
        m = suffix_re.match(title)
        if not m:
            continue
        base_title = m.group(1)
        n = int(m.group(2))
        if n < 2:
            continue
        if base_title in by_title:
            pairs.append((by_title[base_title], note, n))

    # identical first, then diverged; within each group sort by title
    def sort_key(g):
        base, sfx, n = g
        return (0 if base["norm"] == sfx["norm"] else 1, base["title"].lower())

    pairs.sort(key=sort_key)
    return pairs


# ── helpers ───────────────────────────────────────────────────────────────────

def trash(note_id: str) -> bool:
    result = subprocess.run(
        ["bearcli", "trash", note_id],
        capture_output=True, text=True
    )
    return result.returncode == 0


def wrap_lines(text: str, width: int) -> list[str]:
    """Wrap text to fit in `width` columns."""
    out = []
    for raw_line in text.splitlines():
        if not raw_line:
            out.append("")
            continue
        while len(raw_line) > width:
            out.append(raw_line[:width])
            raw_line = raw_line[width:]
        out.append(raw_line)
    return out


# ── drawing ───────────────────────────────────────────────────────────────────

C_TITLE   = 1   # cyan  — panel header
C_SAME    = 2   # green — identical badge
C_DIFF    = 3   # red   — diverged badge
C_STATUS  = 4   # black on white — bottom bar
C_DIVIDER = 5   # dim   — centre divider
C_HILITE  = 6   # yellow — key hints


def draw_panel(stdscr, x, y, w, h, title, lines, scroll, color_title):
    """Draw one panel: header + scrolled content."""
    # header
    header = f" {title} "
    header = header[:w]
    stdscr.attron(curses.color_pair(color_title) | curses.A_BOLD)
    stdscr.addstr(y, x, header.ljust(w)[:w])
    stdscr.attroff(curses.color_pair(color_title) | curses.A_BOLD)

    # content
    visible = lines[scroll: scroll + h]
    for i, line in enumerate(visible):
        line = line[:w]
        try:
            stdscr.addstr(y + 1 + i, x, line.ljust(w)[:w])
        except curses.error:
            pass

    # blank remaining rows
    for i in range(len(visible), h):
        try:
            stdscr.addstr(y + 1 + i, x, " " * w)
        except curses.error:
            pass


def draw_screen(stdscr, pairs, idx, scroll, trashed_count):
    stdscr.erase()
    h, w = stdscr.getmaxyx()

    half = (w - 1) // 2        # width of each panel
    content_h = h - 3           # rows for content (minus header + status)

    base, sfx, n = pairs[idx]
    identical = base["norm"] == sfx["norm"]
    badge = "  IDENTICAL  " if identical else "  DIVERGED  "
    badge_color = C_SAME if identical else C_DIFF

    # wrap content for each panel
    left_lines  = wrap_lines(base["content"], half)
    right_lines = wrap_lines(sfx["content"],  half)

    # clamp scroll
    max_scroll = max(0, max(len(left_lines), len(right_lines)) - content_h)
    scroll = min(scroll, max_scroll)

    # left panel
    draw_panel(stdscr, 0, 0, half, content_h, base["title"], left_lines, scroll, C_TITLE)

    # divider
    for row in range(h - 1):
        try:
            stdscr.attron(curses.color_pair(C_DIVIDER))
            stdscr.addch(row, half, "│")
            stdscr.attroff(curses.color_pair(C_DIVIDER))
        except curses.error:
            pass

    # right panel
    draw_panel(stdscr, half + 1, 0, w - half - 1, content_h, sfx["title"], right_lines, scroll, C_TITLE)

    # badge (centred on divider)
    badge_x = max(0, half - len(badge) // 2)
    try:
        stdscr.attron(curses.color_pair(badge_color) | curses.A_BOLD)
        stdscr.addstr(0, badge_x, badge[: w - badge_x])
        stdscr.attroff(curses.color_pair(badge_color) | curses.A_BOLD)
    except curses.error:
        pass

    # status bar
    progress = f" {idx + 1}/{len(pairs)}  trashed:{trashed_count} "
    keys     = " [Space] skip   [D] trash COPY   [d] trash BASE   [↑↓] scroll   [q] quit "
    bar = (progress + keys).ljust(w)[:w]
    try:
        stdscr.attron(curses.color_pair(C_STATUS) | curses.A_BOLD)
        stdscr.addstr(h - 1, 0, bar[:w - 1])
        stdscr.attroff(curses.color_pair(C_STATUS) | curses.A_BOLD)
    except curses.error:
        pass

    stdscr.refresh()
    return scroll


# ── main loop ─────────────────────────────────────────────────────────────────

def run(stdscr, pairs):
    curses.curs_set(0)
    curses.use_default_colors()
    stdscr.keypad(True)

    curses.init_pair(C_TITLE,   curses.COLOR_CYAN,    -1)
    curses.init_pair(C_SAME,    curses.COLOR_GREEN,   -1)
    curses.init_pair(C_DIFF,    curses.COLOR_RED,     -1)
    curses.init_pair(C_STATUS,  curses.COLOR_BLACK,   curses.COLOR_WHITE)
    curses.init_pair(C_DIVIDER, curses.COLOR_WHITE,   -1)
    curses.init_pair(C_HILITE,  curses.COLOR_YELLOW,  -1)

    idx = 0
    scroll = 0
    trashed = 0

    while idx < len(pairs):
        scroll = draw_screen(stdscr, pairs, idx, scroll, trashed)

        key = stdscr.getch()

        if key == ord('q'):
            break

        elif key == ord(' '):
            idx += 1
            scroll = 0

        elif key == ord('D'):
            # trash the copy (right / suffixed note)
            _, sfx, _ = pairs[idx]
            if trash(sfx["id"]):
                trashed += 1
                pairs.pop(idx)
                scroll = 0
            else:
                # show error briefly
                h, w = stdscr.getmaxyx()
                msg = " bearcli failed — is it installed? "
                try:
                    stdscr.attron(curses.color_pair(C_DIFF) | curses.A_BOLD)
                    stdscr.addstr(h - 1, 0, msg[:w - 1])
                    stdscr.attroff(curses.color_pair(C_DIFF) | curses.A_BOLD)
                except curses.error:
                    pass
                stdscr.refresh()
                stdscr.getch()  # wait for keypress

        elif key == ord('d'):
            # trash the base (left note)
            base, _, _ = pairs[idx]
            if trash(base["id"]):
                trashed += 1
                pairs.pop(idx)
                scroll = 0
            else:
                h, w = stdscr.getmaxyx()
                msg = " bearcli failed — is it installed? "
                try:
                    stdscr.attron(curses.color_pair(C_DIFF) | curses.A_BOLD)
                    stdscr.addstr(h - 1, 0, msg[:w - 1])
                    stdscr.attroff(curses.color_pair(C_DIFF) | curses.A_BOLD)
                except curses.error:
                    pass
                stdscr.refresh()
                stdscr.getch()

        elif key in (curses.KEY_UP, ord('k')):
            scroll = max(0, scroll - 1)

        elif key in (curses.KEY_DOWN, ord('j')):
            scroll += 1

        elif key in (curses.KEY_PPAGE,):   # page up
            scroll = max(0, scroll - 20)

        elif key in (curses.KEY_NPAGE,):   # page down
            scroll += 20

    # summary screen
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    msg = f"Done. {trashed} note(s) trashed. {len(pairs)} pair(s) remaining. Press any key."
    try:
        stdscr.addstr(h // 2, max(0, (w - len(msg)) // 2), msg[:w])
    except curses.error:
        pass
    stdscr.refresh()
    stdscr.getch()


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    pairs = load_pairs()
    if not pairs:
        print("No ' 2' / ' N' suffixed duplicate pairs found in active notes.")
        return
    print(f"Found {len(pairs)} pairs. Launching reviewer…")
    curses.wrapper(run, pairs)


if __name__ == "__main__":
    main()
