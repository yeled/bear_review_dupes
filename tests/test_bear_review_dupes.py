#!/usr/bin/env python3
"""
Unit tests for the pure logic in bear_review_dupes.

Dependency-free: run with the stdlib test runner from the repo root:

    python3 -m unittest discover -s tests -v

The curses rendering and the live `bearcli` calls need a real terminal and
Bear database and aren't covered here; everything below exercises the data
loading, tag parsing, diff alignment and tag-sync logic in isolation, using a
synthetic SQLite database that mirrors the parts of Bear's schema we read.
"""

import os
import sys
import time
import sqlite3
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bear_review_dupes as brd  # noqa: E402


# ── synthetic Bear database ────────────────────────────────────────────────────

def make_db(path, notes, tags=None):
    """
    Build a minimal Bear-like SQLite database at `path`.

    notes: list of dicts with at least pk/id/title/content; ctime/mtime and the
           trashed/archived/encrypted flags default sensibly.
    tags:  {note_pk: [tag_title, ...]} — wired through a Z_5TAGS join table and
           ZSFNOTETAG, exactly the shape load_db_tags discovers at runtime.
    """
    conn = sqlite3.connect(path)
    conn.executescript("""
        DROP TABLE IF EXISTS ZSFNOTE;
        DROP TABLE IF EXISTS ZSFNOTETAG;
        DROP TABLE IF EXISTS Z_5TAGS;
        CREATE TABLE ZSFNOTE (
            Z_PK INTEGER PRIMARY KEY, ZUNIQUEIDENTIFIER TEXT, ZTITLE TEXT,
            ZTEXT TEXT, ZCREATIONDATE REAL, ZMODIFICATIONDATE REAL,
            ZTRASHED INT DEFAULT 0, ZARCHIVED INT DEFAULT 0, ZENCRYPTED INT DEFAULT 0
        );
        CREATE TABLE ZSFNOTETAG (Z_PK INTEGER PRIMARY KEY, ZTITLE TEXT);
        CREATE TABLE Z_5TAGS (Z_5NOTES INTEGER, Z_13TAGS INTEGER);
    """)
    for nt in notes:
        conn.execute(
            "INSERT INTO ZSFNOTE "
            "(Z_PK, ZUNIQUEIDENTIFIER, ZTITLE, ZTEXT, ZCREATIONDATE, "
            " ZMODIFICATIONDATE, ZTRASHED, ZARCHIVED, ZENCRYPTED) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                nt["pk"], nt.get("id", f"U{nt['pk']}"), nt["title"],
                nt.get("content", ""), nt.get("ctime", 1.0), nt.get("mtime", 2.0),
                nt.get("trashed", 0), nt.get("archived", 0), nt.get("encrypted", 0),
            ),
        )

    if tags:
        tag_pk = {}
        for note_pk, titles in tags.items():
            for title in titles:
                if title not in tag_pk:
                    pk = len(tag_pk) + 1
                    tag_pk[title] = pk
                    conn.execute(
                        "INSERT INTO ZSFNOTETAG (Z_PK, ZTITLE) VALUES (?,?)",
                        (pk, title),
                    )
                conn.execute(
                    "INSERT INTO Z_5TAGS (Z_5NOTES, Z_13TAGS) VALUES (?,?)",
                    (note_pk, tag_pk[title]),
                )
    conn.commit()
    conn.close()


class DBTestCase(unittest.TestCase):
    """Base class giving each test a temp DB path wired into brd.DB_PATH."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        self._tmp.close()
        self.db_path = Path(self._tmp.name)
        self._orig_db_path = brd.DB_PATH
        brd.DB_PATH = self.db_path

    def tearDown(self):
        brd.DB_PATH = self._orig_db_path
        try:
            os.unlink(self.db_path)
        except FileNotFoundError:
            pass


# ── normalize() ────────────────────────────────────────────────────────────────

class TestNormalize(unittest.TestCase):
    def test_none_and_empty(self):
        self.assertEqual(brd.normalize(None), "")
        self.assertEqual(brd.normalize(""), "")

    def test_drops_title_line(self):
        # the first line (Bear's title) is always dropped
        self.assertEqual(brd.normalize("My Title\nbody"), "body")

    def test_strips_tag_only_lines(self):
        out = brd.normalize("Title\nbody\n#journal #journal/2024 #upnote")
        self.assertEqual(out, "body")

    def test_strips_apple_maps_link(self):
        out = brd.normalize("Title\nbody\n[Home](http://maps.apple.com/?ll=1,2)")
        self.assertEqual(out, "body")

    def test_strips_image_and_bare_link(self):
        self.assertEqual(brd.normalize("Title\n![](img.png)\nbody"), "body")
        self.assertEqual(brd.normalize("Title\n[label](file.pdf)\nbody"), "body")

    def test_collapses_blank_runs_and_trims(self):
        out = brd.normalize("Title\n\n\na\n\n\n\nb\n\n")
        self.assertEqual(out, "a\n\nb")

    def test_tag_only_difference_normalizes_equal(self):
        # two notes that differ only by title + trailing tag line are "identical"
        a = brd.normalize("2024-01-01\n\nbody text\n\n#journal #upnote")
        b = brd.normalize("2024-01-01 2\n\nbody text\n\n#journal #journal/2024/W01 #upnote")
        self.assertEqual(a, b)


# ── md_tags() ──────────────────────────────────────────────────────────────────

class TestMdTags(unittest.TestCase):
    def test_simple_and_nested(self):
        self.assertEqual(
            brd.md_tags("#journal #journal/2024 #journal/2024/W01"),
            {"journal", "journal/2024", "journal/2024/W01"},
        )

    def test_excludes_markdown_heading(self):
        # "# Heading" has a space after # → not a tag
        self.assertEqual(brd.md_tags("# 2024-01-01\nbody"), set())

    def test_excludes_url_fragment(self):
        # no word boundary before '#' → not a tag
        self.assertEqual(brd.md_tags("see http://x/y#frag here"), set())

    def test_hyphenated_tag(self):
        self.assertEqual(brd.md_tags("#home-finance"), {"home-finance"})

    def test_none_and_empty(self):
        self.assertEqual(brd.md_tags(None), set())
        self.assertEqual(brd.md_tags(""), set())

    def test_tag_at_start_of_line(self):
        self.assertEqual(brd.md_tags("#alpha mid #beta"), {"alpha", "beta"})


# ── fmt_date() ──────────────────────────────────────────────────────────────────

class TestFmtDate(unittest.TestCase):
    def setUp(self):
        # pin the timezone so the formatted output is deterministic
        self._orig_tz = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()

    def tearDown(self):
        if self._orig_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._orig_tz
        time.tzset()

    def test_none(self):
        self.assertEqual(brd.fmt_date(None), "—")

    def test_core_data_epoch_is_2001(self):
        # ts 0 = Core Data epoch = 2001-01-01T00:00:00Z
        self.assertEqual(brd.fmt_date(0), "2001-01-01 00:00")

    def test_known_value(self):
        # 2024-01-01 17:10 UTC in Core Data seconds
        ts = (
            __import__("datetime").datetime(2024, 1, 1, 17, 10,
                                            tzinfo=__import__("datetime").timezone.utc).timestamp()
            - brd.CORE_DATA_EPOCH
        )
        self.assertEqual(brd.fmt_date(ts), "2024-01-01 17:10")

    def test_overflow_returns_dash(self):
        self.assertEqual(brd.fmt_date(10 ** 20), "—")


# ── load_db_tags() ──────────────────────────────────────────────────────────────

class TestLoadDbTags(DBTestCase):
    def test_discovers_join_table_and_groups_tags(self):
        make_db(
            self.db_path,
            notes=[{"pk": 1, "title": "a"}, {"pk": 2, "title": "b"}, {"pk": 3, "title": "c"}],
            tags={1: ["journal", "upnote"], 2: ["work"]},  # note 3 has no tags
        )
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            result = brd.load_db_tags(conn)
        finally:
            conn.close()
        self.assertEqual(result.get(1), {"journal", "upnote"})
        self.assertEqual(result.get(2), {"work"})
        self.assertNotIn(3, result)  # untagged notes are absent


# ── _build_note() ───────────────────────────────────────────────────────────────

class TestBuildNote(DBTestCase):
    def test_builds_record_with_derived_fields(self):
        make_db(self.db_path, notes=[{
            "pk": 1, "id": "U1", "title": "  Title  ",
            "content": "Title\nbody\n#journal", "ctime": 10.0, "mtime": 20.0,
        }])
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                f"SELECT {brd._NOTE_COLUMNS} FROM ZSFNOTE WHERE Z_PK=1"
            ).fetchone()
        finally:
            conn.close()
        note = brd._build_note(row, {"journal"})
        self.assertEqual(note["pk"], 1)
        self.assertEqual(note["id"], "U1")
        self.assertEqual(note["title"], "Title")          # stripped
        self.assertEqual(note["db_tags"], {"journal"})
        self.assertEqual(note["md_tags"], {"journal"})     # parsed from content
        self.assertEqual(note["norm"], "body")             # title + tag line dropped
        self.assertEqual((note["ctime"], note["mtime"]), (10.0, 20.0))

    def test_null_content_defaults_to_empty(self):
        make_db(self.db_path, notes=[{"pk": 1, "title": "t", "content": None}])
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                f"SELECT {brd._NOTE_COLUMNS} FROM ZSFNOTE WHERE Z_PK=1"
            ).fetchone()
        finally:
            conn.close()
        note = brd._build_note(row, set())
        self.assertEqual(note["content"], "")
        self.assertEqual(note["md_tags"], set())


# ── load_pairs() ────────────────────────────────────────────────────────────────

class TestLoadPairs(DBTestCase):
    def test_pairs_only_valid_suffixed_dupes(self):
        make_db(self.db_path, notes=[
            {"pk": 1, "title": "note",   "content": "note\nbody"},
            {"pk": 2, "title": "note 2", "content": "note\nbody changed"},
            {"pk": 3, "title": "solo",   "content": "solo\nx"},          # no base/copy
            {"pk": 4, "title": "orphan 2", "content": "orphan\ny"},      # copy w/o base
            {"pk": 5, "title": "weekly 1", "content": "weekly\nz"},      # n<2 ignored
            {"pk": 6, "title": "weekly",   "content": "weekly\nz"},
            {"pk": 7, "title": "gone",     "content": "gone\n"},
            {"pk": 8, "title": "gone 2",   "content": "gone\n", "trashed": 1},  # excluded
        ])
        pairs = brd.load_pairs()
        titles = {(b["title"], s["title"], n) for b, s, n in pairs}
        self.assertEqual(titles, {("note", "note 2", 2)})

    def test_sort_identical_before_diverged(self):
        make_db(self.db_path, notes=[
            {"pk": 1, "title": "zeta",   "content": "zeta\nsame"},
            {"pk": 2, "title": "zeta 2", "content": "zeta copy\nsame"},   # identical (norm)
            {"pk": 3, "title": "alpha",   "content": "alpha\none"},
            {"pk": 4, "title": "alpha 2", "content": "alpha\ntwo"},       # diverged
        ])
        pairs = brd.load_pairs()
        order = [b["title"] for b, s, n in pairs]
        # identical pair (zeta) sorts before the diverged pair (alpha)
        self.assertEqual(order, ["zeta", "alpha"])


# ── reload_note() ───────────────────────────────────────────────────────────────

class TestReloadNote(DBTestCase):
    def test_refreshes_in_place(self):
        make_db(
            self.db_path,
            notes=[{"pk": 1, "title": "n", "content": "n\nbody\n#journal", "mtime": 2.0}],
            tags={1: ["journal"]},
        )
        note = {"pk": 1}
        brd.reload_note(note)
        self.assertEqual(note["db_tags"], {"journal"})
        self.assertEqual(note["md_tags"], {"journal"})
        self.assertEqual(note["mtime"], 2.0)

        # simulate a mutation landing in the DB, then reload again
        make_db(
            self.db_path,
            notes=[{"pk": 1, "title": "n",
                    "content": "n\nbody\n#journal #journal/2024/W01", "mtime": 99.0}],
            tags={1: ["journal", "journal/2024/W01"]},
        )
        same_obj = note
        brd.reload_note(note)
        self.assertIs(note, same_obj)                      # updated in place
        self.assertIn("journal/2024/W01", note["db_tags"])
        self.assertIn("journal/2024/W01", note["md_tags"])
        self.assertEqual(note["mtime"], 99.0)

    def test_missing_db_is_noop(self):
        brd.DB_PATH = Path("/nonexistent/definitely/not/here.sqlite")
        note = {"pk": 1, "db_tags": {"keep"}}
        brd.reload_note(note)
        self.assertEqual(note, {"pk": 1, "db_tags": {"keep"}})  # untouched


# ── tags_to_sync() ──────────────────────────────────────────────────────────────

class TestTagsToSync(unittest.TestCase):
    def test_markdown_only_tag_on_copy_is_synced(self):
        # the regression that motivated this: W01 lives only in the copy's MD
        base = {"db_tags": {"journal", "journal/2024", "upnote"},
                "md_tags": {"journal", "journal/2024", "upnote"}}
        copy = {"db_tags": {"journal", "journal/2024", "upnote"},
                "md_tags": {"journal", "journal/2024", "journal/2024/W01", "upnote"}}
        self.assertEqual(brd.tags_to_sync(base, copy), {"journal/2024/W01"})

    def test_nothing_missing(self):
        base = {"db_tags": {"a", "b"}, "md_tags": {"a"}}
        copy = {"db_tags": {"a"}, "md_tags": {"b"}}
        self.assertEqual(brd.tags_to_sync(base, copy), set())

    def test_union_of_both_sources(self):
        base = {"db_tags": set(), "md_tags": set()}
        copy = {"db_tags": {"x"}, "md_tags": {"y"}}
        self.assertEqual(brd.tags_to_sync(base, copy), {"x", "y"})


# ── token_diff() ────────────────────────────────────────────────────────────────

class TestTokenDiff(unittest.TestCase):
    @staticmethod
    def _styled(segs, style):
        """Joined text of all segments carrying `style`."""
        return "".join(t for t, s in segs if s == style)

    def test_replace_highlights_changed_tokens(self):
        lsegs, rsegs = brd.token_diff("hello world", "hello there")
        # shared prefix stays plain (the equal "hello" + " " run is one segment)
        self.assertIn("hello", self._styled(lsegs, "plain"))
        self.assertEqual(self._styled(lsegs, "del"), "world")
        self.assertEqual(self._styled(rsegs, "add"), "there")
        # the shared token never lands in a changed segment
        self.assertNotIn("hello", self._styled(lsegs, "del"))
        self.assertNotIn("hello", self._styled(rsegs, "add"))

    def test_pure_insert(self):
        lsegs, rsegs = brd.token_diff("abc", "abc def")
        self.assertEqual(self._styled(lsegs, "del"), "")
        self.assertIn("def", self._styled(rsegs, "add"))

    def test_equal(self):
        lsegs, rsegs = brd.token_diff("same", "same")
        self.assertEqual(self._styled(lsegs, "del"), "")
        self.assertEqual(self._styled(rsegs, "add"), "")


# ── align_block() ───────────────────────────────────────────────────────────────

class TestAlignBlock(unittest.TestCase):
    def test_inserted_line_does_not_misalign(self):
        # an added middle line should pair the matching lines, not shift them
        lblock = ["the quick brown fox", "jumps over the lazy dog"]
        rblock = ["the quick brown fox", "BRAND NEW LINE HERE", "jumps over the lazy dog"]
        ops = brd.align_block(lblock, rblock)
        kinds = [op[0] for op in ops]
        self.assertEqual(kinds.count("pair"), 2)
        self.assertEqual(kinds.count("add"), 1)
        self.assertEqual(kinds.count("del"), 0)

    def test_unrelated_lines_are_del_add_not_paired(self):
        ops = brd.align_block(["completely different alpha"], ["totally unrelated omega"])
        kinds = sorted(op[0] for op in ops)
        self.assertEqual(kinds, ["add", "del"])


# ── build_diff() ────────────────────────────────────────────────────────────────

class TestBuildDiff(unittest.TestCase):
    @staticmethod
    def _styles(rows, side):  # side 0 = left, 1 = right
        out = []
        for row in rows:
            segs, _fill = row[side]
            out.extend(s for _t, s in segs)
        return out

    def test_identical_all_plain(self):
        rows = brd.build_diff("a\nb\nc", "a\nb\nc")
        self.assertTrue(all(s in (None, "plain") for s in self._styles(rows, 0)))
        self.assertNotIn("del", self._styles(rows, 0))
        self.assertNotIn("add", self._styles(rows, 1))

    def test_whitespace_only_difference_is_ws(self):
        rows = brd.build_diff("hello world", "hello   world")
        self.assertIn("ws", self._styles(rows, 0))
        self.assertIn("ws", self._styles(rows, 1))

    def test_added_line(self):
        rows = brd.build_diff("a", "a\nb")
        self.assertIn("add", self._styles(rows, 1))

    def test_removed_line(self):
        rows = brd.build_diff("a\nb", "a")
        self.assertIn("del", self._styles(rows, 0))


# ── wrap_segments() / wrap_diff() ───────────────────────────────────────────────

class TestWrapping(unittest.TestCase):
    def test_wrap_segments_splits_at_width(self):
        rows = brd.wrap_segments([("abcdef", "add")], 3)
        self.assertEqual(rows, [[("abc", "add")], [("def", "add")]])

    def test_wrap_segments_preserves_style_across_pieces(self):
        rows = brd.wrap_segments([("hello", "del")], 2)
        self.assertTrue(all(seg[1] == "del" for row in rows for seg in row))

    def test_wrap_diff_keeps_panels_row_aligned(self):
        rows = [(([("abcdefghij", "del")], "del"), ([], None))]
        left, right = brd.wrap_diff(rows, lwidth=3, rwidth=20)
        # left wraps to multiple rows; right is padded to match
        self.assertEqual(len(left), len(right))


if __name__ == "__main__":
    unittest.main(verbosity=2)
