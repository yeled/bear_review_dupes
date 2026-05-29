#!/usr/bin/env python3
"""
Interactive side-by-side duplicate reviewer for Bear notes.

The two panels are aligned line-by-line as a diff (matched on a
whitespace-normalized key so reflowed/whitespace lines stay aligned):
  green  — text added in the COPY (right panel)
  red    — text removed from the BASE (left panel)
  dim    — line is identical except for whitespace (minor change)
  plain  — unchanged text
Lines that only changed by a few words highlight just the differing
words rather than the whole line.

Controls:
  Space   — skip (keep both, move to next pair)
  D       — trash the COPY  (right panel, the " 2" note)
  d       — trash the BASE  (left panel, the original)
  s       — sync tags COPY → BASE (adds the copy's missing tags to the base)
  ↑ / ↓  — scroll both panels together
  q       — quit

A metadata bar at the top of each panel shows the note's tags as stored in
the Bear database vs. the tags parsed from its markdown (mismatches are
highlighted), plus its creation and modification dates.

Deletion calls: bearcli trash <uuid>
Tag sync calls: bearcli tags add <uuid> <tag>

Usage:
    python3 bear_review_dupes.py
"""

import curses
import difflib
import subprocess
import sqlite3
import re
import sys
from datetime import datetime
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


# Core Data stores timestamps as seconds since 2001-01-01; this offset converts
# them to the Unix epoch.
CORE_DATA_EPOCH = 978307200

# Markdown tags: "#tag" / "#nested/tag". Requires a word char right after the
# "#" (so markdown headings like "# Heading" are not matched) and a word
# boundary before it (so "url#frag" is not matched). Multi-word "#a b#" tags
# are not captured.
_MD_TAG_RE = re.compile(r'(?:(?<=\s)|^)#([\w/][\w/\-]*)')


def md_tags(text: str) -> set:
    """Tags as written inline in the note's markdown."""
    return set(_MD_TAG_RE.findall(text or ""))


def fmt_date(ts) -> str:
    """Format a Core Data timestamp as a local date/time string."""
    if ts is None:
        return "—"
    try:
        return datetime.fromtimestamp(ts + CORE_DATA_EPOCH).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError, OverflowError):
        return "—"


def load_db_tags(conn) -> dict:
    """
    Return {note_pk: set(tag_title)} by discovering Bear's note↔tag join table
    (its name, e.g. Z_5TAGS, varies by Bear version).
    """
    cur = conn.cursor()
    cur.execute(
        r"SELECT name FROM sqlite_master "
        r"WHERE type='table' AND name LIKE 'Z\_%TAGS' ESCAPE '\'"
    )
    join_tables = [r[0] for r in cur.fetchall()]

    result = {}
    for jt in join_tables:
        cols = [r[1] for r in cur.execute(f'PRAGMA table_info("{jt}")').fetchall()]
        note_col = next((c for c in cols if c.endswith("NOTES")), None)
        tag_col  = next((c for c in cols if c.endswith("TAGS")), None)
        if not note_col or not tag_col:
            continue
        try:
            q = (f'SELECT j."{note_col}" AS note_pk, t.ZTITLE AS tag '
                 f'FROM "{jt}" j JOIN ZSFNOTETAG t ON t.Z_PK = j."{tag_col}"')
            for row in cur.execute(q):
                if row["tag"]:
                    result.setdefault(row["note_pk"], set()).add(row["tag"])
        except sqlite3.Error:
            continue
    return result


# Per-note columns, shared by the initial load and single-note reloads.
_NOTE_COLUMNS = (
    "Z_PK as pk, ZUNIQUEIDENTIFIER as id, ZTITLE as title, ZTEXT as content, "
    "ZCREATIONDATE as ctime, ZMODIFICATIONDATE as mtime"
)


def _build_note(row, db_tags_for_pk: set) -> dict:
    """Assemble the in-memory note record from a DB row and its DB tag set."""
    content = row["content"] or ""
    return {
        "pk":       row["pk"],
        "id":       row["id"],
        "title":    (row["title"] or "").strip(),
        "content":  content,
        "norm":     normalize(content),
        "ctime":    row["ctime"],
        "mtime":    row["mtime"],
        "db_tags":  db_tags_for_pk,
        "md_tags":  md_tags(content),
    }


def load_pairs():
    if not DB_PATH.exists():
        print(f"ERROR: Bear database not found at:\n  {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        f"SELECT {_NOTE_COLUMNS} FROM ZSFNOTE "
        f"WHERE ZTRASHED = 0 AND ZARCHIVED = 0 AND ZENCRYPTED = 0"
    )
    rows = cur.fetchall()
    db_tags = load_db_tags(conn)
    conn.close()

    by_title = {}
    for row in rows:
        note = _build_note(row, db_tags.get(row["pk"], set()))
        by_title[note["title"]] = note

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


def reload_note(note: dict) -> None:
    """
    Re-read a single note from Bear's DB and refresh it in place.

    Called after a mutation (e.g. a tag sync) so the metadata bar, body diff
    and IDENTICAL/DIVERGED badge — all recomputed each draw from these fields —
    reflect what's actually in the database rather than an optimistic patch.
    The note dict is updated in place so the references held in `pairs` stay
    valid.
    """
    if not DB_PATH.exists():
        return
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            f"SELECT {_NOTE_COLUMNS} FROM ZSFNOTE WHERE Z_PK = ?", (note["pk"],)
        ).fetchone()
        if row is not None:
            note.update(_build_note(row, load_db_tags(conn).get(row["pk"], set())))
    finally:
        conn.close()


# ── helpers ───────────────────────────────────────────────────────────────────

def trash(note_id: str) -> bool:
    result = subprocess.run(
        ["bearcli", "trash", note_id],
        capture_output=True, text=True
    )
    return result.returncode == 0


def add_tags(note_id: str, tags) -> bool:
    """Add each tag to a note via `bearcli tags add <uuid> <tag>` (won't touch mDate)."""
    ok = True
    for tag in tags:
        result = subprocess.run(
            ["bearcli", "tags", "add", note_id, tag],
            capture_output=True, text=True
        )
        ok = ok and result.returncode == 0
    return ok


# A "segment" is a (text, style) tuple. A "side" of a logical diff row is a
# (segments, fill_style) pair: `segments` are drawn left-to-right, then the rest
# of the panel width is padded using `fill_style` (so whole-line add/remove and
# whitespace rows show as a band even when blank).
#
# Styles:
#   "plain" / None — unchanged / context text (no color)
#   "del"          — removed text (red)            [token-level or whole line]
#   "add"          — added text (green)            [token-level or whole line]
#   "ws"           — whitespace-only difference (dim, minor change)

_TOKEN_RE = re.compile(r"\w+|\s+|[^\w\s]")


def _norm_key(line: str) -> str:
    """Whitespace-insensitive key: strip ends, collapse internal runs."""
    return re.sub(r"\s+", " ", line.strip())


def _tokenize(line: str):
    return _TOKEN_RE.findall(line)


def token_diff(left: str, right: str):
    """
    Intra-line word/character diff for a pair of changed lines.

    Returns (left_segments, right_segments); only the differing tokens are
    tagged "del"/"add", shared tokens stay "plain".
    """
    lt, rt = _tokenize(left), _tokenize(right)
    sm = difflib.SequenceMatcher(a=lt, b=rt, autojunk=False)

    lsegs, rsegs = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        lpart = "".join(lt[i1:i2])
        rpart = "".join(rt[j1:j2])
        if tag == "equal":
            if lpart:
                lsegs.append((lpart, "plain"))
            if rpart:
                rsegs.append((rpart, "plain"))
        elif tag == "replace":
            if lpart:
                lsegs.append((lpart, "del"))
            if rpart:
                rsegs.append((rpart, "add"))
        elif tag == "delete":
            if lpart:
                lsegs.append((lpart, "del"))
        elif tag == "insert":
            if rpart:
                rsegs.append((rpart, "add"))
    return lsegs or [("", "plain")], rsegs or [("", "plain")]


# Below this similarity ratio, two lines in a replace block are considered
# unrelated and shown as a full remove + full add rather than word-diffed.
_PAIR_THRESHOLD = 0.5
# Above this many line-pairs, skip the O(n*m) alignment and pair positionally
# (keeps very large diverged notes responsive).
_ALIGN_BUDGET = 10000


def align_block(lblock, rblock):
    """
    Align the lines of a replace block by similarity (not by position), so an
    inserted/removed line in the middle doesn't shift everything and cause
    unrelated lines to be word-diffed against each other.

    Returns a list of ops: ("pair", l, r) | ("del", l) | ("add", r).
    """
    n, m = len(lblock), len(rblock)
    if n * m > _ALIGN_BUDGET:
        # fall back to positional pairing for very large blocks
        ops = [("pair", lblock[k], rblock[k]) for k in range(min(n, m))]
        ops += [("del", l) for l in lblock[m:]]
        ops += [("add", r) for r in rblock[n:]]
        return ops

    NEG = float("-inf")

    def score(l, r):
        s = difflib.SequenceMatcher(None, l, r, autojunk=False).ratio()
        return s if s >= _PAIR_THRESHOLD else NEG

    # Needleman–Wunsch: a gap (unmatched line) scores 0, a match scores its ratio.
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            dp[i][j] = max(
                dp[i - 1][j - 1] + score(lblock[i - 1], rblock[j - 1]),  # pair
                dp[i - 1][j],   # left line unmatched (del)
                dp[i][j - 1],   # right line unmatched (add)
            )

    # backtrack, preferring a real pair over gaps on ties
    ops = []
    i, j = n, m
    while i > 0 and j > 0:
        diag = dp[i - 1][j - 1] + score(lblock[i - 1], rblock[j - 1])
        if diag >= dp[i - 1][j] and diag >= dp[i][j - 1] and diag != NEG:
            ops.append(("pair", lblock[i - 1], rblock[j - 1]))
            i, j = i - 1, j - 1
        elif dp[i - 1][j] >= dp[i][j - 1]:
            ops.append(("del", lblock[i - 1]))
            i -= 1
        else:
            ops.append(("add", rblock[j - 1]))
            j -= 1
    while i > 0:
        ops.append(("del", lblock[i - 1]))
        i -= 1
    while j > 0:
        ops.append(("add", rblock[j - 1]))
        j -= 1
    ops.reverse()
    return ops


def build_diff(left_text: str, right_text: str):
    """
    Align the two notes into logical diff rows, matching on a whitespace-
    normalized key so that whitespace-only or shifted lines line up instead of
    being reported as separate add/remove.

    Returns a list of (left_side, right_side) where each side is
    (segments, fill_style).
    """
    L = left_text.splitlines()
    R = right_text.splitlines()
    sm = difflib.SequenceMatcher(
        a=[_norm_key(x) for x in L],
        b=[_norm_key(x) for x in R],
        autojunk=False,
    )

    rows = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for a, b in zip(L[i1:i2], R[j1:j2]):
                if a == b:
                    rows.append((([(a, "plain")], None), ([(b, "plain")], None)))
                else:  # same content, whitespace differs → minor change
                    rows.append((([(a, "ws")], "ws"), ([(b, "ws")], "ws")))
        elif tag == "replace":
            for op in align_block(L[i1:i2], R[j1:j2]):
                if op[0] == "pair":           # similar lines → word-level highlight
                    lsegs, rsegs = token_diff(op[1], op[2])
                    rows.append(((lsegs, None), (rsegs, None)))
                elif op[0] == "del":          # unrelated removed line
                    rows.append((([(op[1], "del")], "del"), ([], None)))
                else:                         # unrelated added line
                    rows.append((([], None), ([(op[1], "add")], "add")))
        elif tag == "delete":
            for a in L[i1:i2]:
                rows.append((([(a, "del")], "del"), ([], None)))
        elif tag == "insert":
            for b in R[j1:j2]:
                rows.append((([], None), ([(b, "add")], "add")))
    return rows


def wrap_segments(segments, width: int):
    """Wrap (text, style) segments to `width`, returning a list of rows of segments."""
    rows = [[]]
    cur = 0
    for text, style in segments:
        i = 0
        while i < len(text):
            if cur >= width:
                rows.append([])
                cur = 0
            take = min(width - cur, len(text) - i)
            rows[-1].append((text[i:i + take], style))
            i += take
            cur += take
    return rows


def wrap_diff(rows, lwidth: int, rwidth: int):
    """
    Wrap each logical diff row, keeping the two panels row-aligned.

    Returns (left_disp, right_disp); each element is (segments, fill_style).
    """
    left_disp, right_disp = [], []
    for (lsegs, lfill), (rsegs, rfill) in rows:
        lrows = wrap_segments(lsegs, lwidth)
        rrows = wrap_segments(rsegs, rwidth)
        n = max(len(lrows), len(rrows))
        lrows += [[] for _ in range(n - len(lrows))]
        rrows += [[] for _ in range(n - len(rrows))]
        left_disp.extend((row, lfill) for row in lrows)
        right_disp.extend((row, rfill) for row in rrows)
    return left_disp, right_disp


# ── drawing ───────────────────────────────────────────────────────────────────

C_TITLE   = 1   # cyan  — panel header
C_SAME    = 2   # green — identical badge
C_DIFF    = 3   # red   — diverged badge
C_STATUS  = 4   # black on white — bottom bar
C_DIVIDER = 5   # dim   — centre divider
C_HILITE  = 6   # yellow — key hints
C_ADD     = 7   # muted dark-green band — added text
C_DEL     = 8   # muted dark-red band   — removed text

META_H = 3      # rows in the top metadata bar (DB tags / MD tags / dates)


def style_attr(style):
    """Map a diff style to a curses attribute."""
    if style == "add":
        return curses.color_pair(C_ADD)
    if style == "del":
        return curses.color_pair(C_DEL)
    if style == "ws":
        return curses.A_DIM
    return curses.A_NORMAL


def draw_panel(stdscr, x, y, w, h, title, lines, scroll, color_title):
    """
    Draw one panel: header + scrolled content.

    `lines` is a list of (segments, fill_style) tuples, where `segments` is a
    list of (text, style) pieces. Each piece is colored by its style (green for
    "add", red for "del", dim for "ws"); the row's remaining width is padded
    using `fill_style` so whole-line changes show as a band.
    """
    # header
    header = f" {title} "
    header = header[:w]
    stdscr.attron(curses.color_pair(color_title) | curses.A_BOLD)
    stdscr.addstr(y, x, header.ljust(w)[:w])
    stdscr.attroff(curses.color_pair(color_title) | curses.A_BOLD)

    # content
    visible = lines[scroll: scroll + h]
    for i, (segments, fill) in enumerate(visible):
        row = y + 1 + i
        col = 0
        for text, style in segments:
            if col >= w:
                break
            piece = text[: w - col]
            if piece:
                try:
                    stdscr.addstr(row, x + col, piece, style_attr(style))
                except curses.error:
                    pass
                col += len(piece)
        if col < w:  # pad the rest of the row (colored band for full-line changes)
            try:
                stdscr.addstr(row, x + col, " " * (w - col), style_attr(fill))
            except curses.error:
                pass

    # blank remaining rows
    for i in range(len(visible), h):
        try:
            stdscr.addstr(y + 1 + i, x, " " * w)
        except curses.error:
            pass


def _put_line(stdscr, x, y, w, segments):
    """Write (text, attr) segments on one row, clipped and padded to `w`."""
    col = 0
    for text, attr in segments:
        if col >= w:
            break
        piece = text[: w - col]
        if piece:
            try:
                stdscr.addstr(y, x + col, piece, attr)
            except curses.error:
                pass
            col += len(piece)
    if col < w:
        try:
            stdscr.addstr(y, x + col, " " * (w - col))
        except curses.error:
            pass


def draw_meta(stdscr, x, y, w, note, other, only_here_style):
    """
    Draw a note's metadata block (3 rows): DB tags, markdown tags, and dates.

    Tags are diff-colored against the other note (compared source-for-source:
    DB↔DB, MD↔MD). A tag this note has that the other lacks is drawn with
    `only_here_style` ("del" on the base, "add" on the copy); shared tags are
    plain.
    """
    def tag_segs(label, tags, other_tags):
        segs = [(f"{label} ", curses.color_pair(C_TITLE))]
        if tags:
            for t in sorted(tags):
                attr = style_attr(only_here_style) if t not in other_tags else curses.A_NORMAL
                segs.append((f"#{t} ", attr))
        else:
            segs.append(("—", curses.A_DIM))
        return segs

    def date_segs(label, field):
        mine, theirs = fmt_date(note[field]), fmt_date(other[field])
        # compare the displayed values so sub-minute differences don't color
        attr = style_attr(only_here_style) if mine != theirs else curses.A_DIM
        return [(label, curses.A_DIM), (mine, attr)]

    _put_line(stdscr, x, y,     w, tag_segs("DB", note["db_tags"], other["db_tags"]))
    _put_line(stdscr, x, y + 1, w, tag_segs("MD", note["md_tags"], other["md_tags"]))
    _put_line(stdscr, x, y + 2, w,
              date_segs("created ", "ctime") + [("   ", curses.A_DIM)]
              + date_segs("modified ", "mtime"))


def draw_screen(stdscr, pairs, idx, scroll, trashed_count):
    stdscr.erase()
    h, w = stdscr.getmaxyx()

    half = (w - 1) // 2        # width of each panel
    right_w = w - half - 1
    sep_y = META_H              # row of the horizontal delimiter under the bar
    panel_y = META_H + 1        # row of the panel header (titles)
    # rows for content (minus metadata bar + delimiter + panel header + status bar)
    content_h = max(1, h - META_H - 3)

    base, sfx, n = pairs[idx]
    identical = base["norm"] == sfx["norm"]
    badge = "  IDENTICAL  " if identical else "  DIVERGED  "
    badge_color = C_SAME if identical else C_DIFF

    # metadata bar (per-note: base on the left, copy on the right; tags diff-colored)
    draw_meta(stdscr, 0, 0, half, base, sfx, "del")
    draw_meta(stdscr, half + 1, 0, right_w, sfx, base, "add")

    # horizontal delimiter marking the end of the metadata bar
    try:
        stdscr.attron(curses.color_pair(C_DIVIDER))
        stdscr.addstr(sep_y, 0, "─" * w)
        stdscr.attroff(curses.color_pair(C_DIVIDER))
    except curses.error:
        pass

    # build an aligned, color-coded diff for both panels
    rows = build_diff(base["content"], sfx["content"])
    left_lines, right_lines = wrap_diff(rows, half, right_w)

    # clamp scroll (panels are row-aligned, so lengths match)
    max_scroll = max(0, len(left_lines) - content_h)
    scroll = min(scroll, max_scroll)

    # left panel
    draw_panel(stdscr, 0, panel_y, half, content_h, base["title"], left_lines, scroll, C_TITLE)

    # divider (── crosses the delimiter row as ┼)
    for row in range(h - 1):
        try:
            stdscr.attron(curses.color_pair(C_DIVIDER))
            stdscr.addch(row, half, "┼" if row == sep_y else "│")
            stdscr.attroff(curses.color_pair(C_DIVIDER))
        except curses.error:
            pass

    # right panel
    draw_panel(stdscr, half + 1, panel_y, right_w, content_h, sfx["title"], right_lines, scroll, C_TITLE)

    # badge (centred on divider, on the delimiter row so it doesn't cover a title)
    badge_x = max(0, half - len(badge) // 2)
    try:
        stdscr.attron(curses.color_pair(badge_color) | curses.A_BOLD)
        stdscr.addstr(sep_y, badge_x, badge[: w - badge_x])
        stdscr.attroff(curses.color_pair(badge_color) | curses.A_BOLD)
    except curses.error:
        pass

    # status bar
    progress = f" {idx + 1}/{len(pairs)}  trashed:{trashed_count} "
    keys     = " [Space] skip  [D] trash COPY  [d] trash BASE  [s] sync tags→base  [↑↓] scroll  [q] quit "
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

def flash(stdscr, msg, color):
    """Show a one-line message on the status row and wait for a keypress."""
    h, w = stdscr.getmaxyx()
    try:
        stdscr.attron(curses.color_pair(color) | curses.A_BOLD)
        stdscr.addstr(h - 1, 0, msg[: w - 1].ljust(w - 1))
        stdscr.attroff(curses.color_pair(color) | curses.A_BOLD)
    except curses.error:
        pass
    stdscr.refresh()
    stdscr.getch()


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
    # muted, darker diff bands when the terminal has a 256-color palette;
    # otherwise fall back to the basic 8-color set.
    if curses.COLORS >= 256:
        curses.init_pair(C_ADD, 252, 22)   # light grey on dark green
        curses.init_pair(C_DEL, 252, 52)   # light grey on dark red
    else:
        curses.init_pair(C_ADD, curses.COLOR_BLACK, curses.COLOR_GREEN)
        curses.init_pair(C_DEL, curses.COLOR_WHITE, curses.COLOR_RED)

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
                flash(stdscr, " bearcli failed — is it installed? ", C_DIFF)

        elif key == ord('d'):
            # trash the base (left note)
            base, _, _ = pairs[idx]
            if trash(base["id"]):
                trashed += 1
                pairs.pop(idx)
                scroll = 0
            else:
                flash(stdscr, " bearcli failed — is it installed? ", C_DIFF)

        elif key == ord('s'):
            # sync tags from the copy into the base (union: add the ones it lacks).
            # Consider tags from both sources — a tag in the copy's markdown but
            # not yet indexed in the DB still needs to be transferred.
            base, sfx, _ = pairs[idx]
            missing = (sfx["db_tags"] | sfx["md_tags"]) - (base["db_tags"] | base["md_tags"])
            if not missing:
                flash(stdscr, " base already has all of the copy's tags ", C_HILITE)
            elif add_tags(base["id"], missing):
                # re-read from the DB so the tag rows, body diff and badge all
                # reflect ground truth (incl. where bearcli placed the tags)
                reload_note(base)
                flash(stdscr, f" synced {len(missing)} tag(s) → base ", C_SAME)
            else:
                flash(stdscr, " bearcli tags add failed ", C_DIFF)

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
