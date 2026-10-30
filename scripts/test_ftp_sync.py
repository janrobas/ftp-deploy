#!/usr/bin/env python3
"""Offline tests for the ftp_sync.py decision engine.

Runs entirely without a network: the plan computation, ignore matching and
execution order are tested against fake transports and temp directories.

Run:  python test_ftp_sync.py   (from this directory, any cwd works)
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ftp_sync import (  # noqa: E402
    IgnoreRules, StateStore, compute_plan, execute, local_walk, remote_walk,
    update_state,
)


def tree(root, files):
    """Write files: {relpath: bytes}. Returns the root Path."""
    root = Path(root)
    for rel, content in files.items():
        p = root / Path(*rel.split("/"))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    return root


def dirs_of(root, names):
    for rel in names:
        (root / Path(*rel.split("/"))).mkdir(parents=True, exist_ok=True)


class FakeTransport:
    """Implements the transport surface: mlsd, mkdir_safe, upload_atomic,
    delete, rmd. Entries: {relpath: ('file', size) | ('dir', None)},
    where relpaths are relative to `root`."""

    def __init__(self, entries=None, root=""):
        self.entries = dict(entries or {})
        self.root = root
        self.ops = []

    def rel_of(self, full):
        full = full.strip("/")
        root = self.root.strip("/")
        if root and full.startswith(root):
            rest = full[len(root):].lstrip("/")
            return rest
        return full

    def mlsd(self, path):
        base = self.rel_of(path)
        out = []
        prefix = (base + "/") if base else ""
        for rel, (t, size) in sorted(self.entries.items()):
            if not rel.startswith(prefix):
                continue
            rest = rel[len(prefix):]
            if "/" in rest:
                continue
            facts = {"type": "file" if t == "file" else "dir"}
            if t == "file":
                facts["size"] = str(size)
            out.append((rest, facts))
        return out

    def mkdir_safe(self, full):
        rel = self.rel_of(full)
        if rel not in self.entries:
            self.entries[rel] = ("dir", None)
            self.ops.append(("mkdir", rel))

    def upload_atomic(self, local_path, remote_dir, name):
        rel_parent = self.rel_of(remote_dir)
        rel = (rel_parent + "/" + name) if rel_parent else name
        data = Path(local_path).read_bytes()
        self.entries[rel] = ("file", len(data))
        self.ops.append(("upload", rel))

    def delete(self, full):
        rel = self.rel_of(full)
        self.entries.pop(rel, None)
        self.ops.append(("delete", rel))

    def rmd(self, full):
        rel = self.rel_of(full)
        self.entries.pop(rel, None)
        self.ops.append(("rmdir", rel))


def make_state(files):
    """files: {rel: (sha, size, remote_size)}"""
    return {rel: {"sha": sha, "size": size, "remote_size": rs}
            for rel, (sha, size, rs) in files.items()}


# ---------------------------------------------------------------------------
# Ignore rules
# ---------------------------------------------------------------------------

class TestIgnoreRules(unittest.TestCase):
    def match(self, patterns, rel):
        return IgnoreRules(patterns).is_ignored(rel)

    def test_basic_name(self):
        self.assertTrue(self.match(["*.log"], "a/b/c.log"))
        self.assertFalse(self.match(["*.log"], "a/b/c.txt"))

    def test_dir_suffix_covers_children(self):
        self.assertTrue(self.match(["data/"], "data"))
        self.assertTrue(self.match(["data/"], "data/users/x.md"))
        self.assertFalse(self.match(["data/"], "metadata"))

    def test_anchor(self):
        self.assertTrue(self.match(["/vendor/"], "vendor/autoload.php"))
        self.assertFalse(self.match(["/vendor/"], "app/vendor/autoload.php"))

    def test_double_star(self):
        self.assertTrue(self.match(["data/**"], "data"))
        self.assertTrue(self.match(["data/**"], "data/users/x.md"))
        self.assertFalse(self.match(["data/**"], "other/x.md"))

    def test_negation_last_match_wins(self):
        rules = ["data/", "!data/keep.md"]
        r = IgnoreRules(rules)
        self.assertTrue(r.has_negations)
        self.assertTrue(r.is_ignored("data/drop.md"))
        self.assertFalse(r.is_ignored("data/keep.md"))

    def test_comment_and_blank(self):
        self.assertFalse(self.match(["# comment", "", "*.tmp"], "x.php"))


# ---------------------------------------------------------------------------
# Local walk
# ---------------------------------------------------------------------------

class TestLocalWalk(unittest.TestCase):
    def test_walk_and_ignore(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = tree(tmp, {
                "index.php": b"<?php",
                "src/app.php": b"<?php x",
                "vendor/a.php": b"x",
                ".git/config": b"g",
                ".ftp-deploy/state.json": b"s",
                ".env": b"SECRET",
                "data/mem.md": b"mem",
                "logs/x.log": b"log",
            })
            rules = IgnoreRules(["data/", "*.log", ".env", ".ftpdeploy.json"])
            dirs, files = local_walk(root, rules)
            self.assertNotIn(".git", dirs)
            self.assertNotIn("data", files)
            self.assertNotIn("logs/x.log", files)
            self.assertIn("index.php", files)
            self.assertIn("src/app.php", files)
            self.assertIn("vendor/a.php", files)
            self.assertIn("src", dirs)
            size, sha = files["index.php"]
            self.assertEqual(size, 5)
            self.assertEqual(len(sha), 64)


# ---------------------------------------------------------------------------
# Remote walk (against the fake transport)
# ---------------------------------------------------------------------------

class TestRemoteWalk(unittest.TestCase):
    def test_listing(self):
        t = FakeTransport({
            "index.php": ("file", 100),
            "src": ("dir", None),
            "src/app.php": ("file", 50),
            "src/.hidden": ("file", 3),
            "src/app.php.ftpdeploy-tmp": ("file", 1),  # in-flight leftovers ignored
        }, root="public_html/site")
        remote = remote_walk(t, "/public_html/site", warn=lambda m: None)
        self.assertIn("index.php", remote)
        self.assertIn("src", remote)
        self.assertIn("src/app.php", remote)
        self.assertIn("src/.hidden", remote)
        self.assertNotIn("src/app.php.ftpdeploy-tmp", remote)
        self.assertEqual(remote["src"]["type"], "dir")


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

class TestPlan(unittest.TestCase):
    def plan(self, local_dirs, local_files, remote, state, adopt=True,
             delete=False, mirror=False, protected=None):
        return compute_plan(local_dirs, local_files, remote, state,
                            IgnoreRules(protected or []), adopt, delete, mirror)

    def test_fresh_empty_remote_all_add(self):
        p = self.plan({}, {"a.php": (10, "s1")}, {}, {}, adopt=False)
        self.assertEqual(p.add, ["a.php"])

    def test_unchanged_when_hash_matches_and_size_ok(self):
        remote = {"a.php": {"type": "file", "size": 10}}
        state = make_state({"a.php": ("s1", 10, 10)})
        p = self.plan({}, {"a.php": (10, "s1")}, remote, state, adopt=False)
        self.assertEqual(p.unchanged, ["a.php"])
        self.assertEqual(p.add, [])

    def test_changed_when_hash_differs(self):
        remote = {"a.php": {"type": "file", "size": 10}}
        state = make_state({"a.php": ("s1", 10, 10)})
        p = self.plan({}, {"a.php": (11, "s2")}, remote, state, adopt=False)
        self.assertEqual(p.add, ["a.php"])

    def test_remote_modified_when_server_size_moved(self):
        remote = {"a.php": {"type": "file", "size": 999}}
        state = make_state({"a.php": ("s1", 10, 10)})
        p = self.plan({}, {"a.php": (10, "s1")}, remote, state, adopt=False)
        self.assertEqual(p.remote_modified, ["a.php"])

    def test_missing_remote_reupload(self):
        state = make_state({"a.php": ("s1", 10, 10)})
        p = self.plan({}, {"a.php": (10, "s1")}, {}, state, adopt=False)
        self.assertEqual(p.add, ["a.php"])

    def test_adopt_same_size_when_fresh(self):
        remote = {"a.php": {"type": "file", "size": 10}}
        p = self.plan({}, {"a.php": (10, "s1")}, remote, {}, adopt=True)
        self.assertEqual(p.adopt, ["a.php"])
        self.assertEqual(p.add, [])

    def test_no_adopt_without_fresh_flag(self):
        remote = {"a.php": {"type": "file", "size": 10}}
        p = self.plan({}, {"a.php": (10, "s1")}, remote, {}, adopt=False)
        self.assertEqual(p.add, ["a.php"])

    def test_deletable_vs_delete(self):
        remote = {"old.php": {"type": "file", "size": 10}}
        state = make_state({"old.php": ("s1", 10, 10)})
        p_off = self.plan({}, {}, remote, state, delete=False)
        self.assertEqual(p_off.deletable, ["old.php"])
        self.assertEqual(p_off.delete, [])
        p_on = self.plan({}, {}, remote, state, delete=True)
        self.assertEqual(p_on.delete, ["old.php"])

    def test_foreign_kept(self):
        remote = {"cgi-bin/x.html": {"type": "file", "size": 10}}
        p = self.plan({}, {}, remote, {}, delete=True)
        self.assertEqual(p.foreign_kept, ["cgi-bin/x.html"])
        self.assertEqual(p.delete, [])

    def test_mirror_deletes_foreign_but_not_protected(self):
        remote = {
            "cgi-bin/x.html": {"type": "file", "size": 10},
            ".env": {"type": "file", "size": 5},
            "data/m.md": {"type": "file", "size": 5},
            "data": {"type": "dir"},
        }
        p = self.plan({}, {}, remote, {}, delete=True, mirror=True,
                      protected=[".env", "data/**"])
        self.assertEqual(p.mirror_delete, ["cgi-bin/x.html"])
        self.assertNotIn(".env", p.mirror_delete)
        self.assertNotIn("data/m.md", p.mirror_delete)

    def test_mkdir_chain(self):
        p = self.plan({}, {"src/a/b.php": (5, "s1")}, {}, {}, adopt=False)
        self.assertEqual(p.mkdir, ["src", "src/a"])

    def test_rmdir_only_empty(self):
        remote = {
            "a": {"type": "dir"},
            "b": {"type": "dir"},
            "b/x.php": {"type": "file", "size": 1},
        }
        p = self.plan({}, {}, remote, {}, delete=True)
        self.assertEqual(p.rmdir, ["a"])  # b is not empty
        # without delete approval, no rmdir is planned at all
        p2 = self.plan({}, {}, remote, {}, delete=False)
        self.assertEqual(p2.rmdir, [])

    def test_protected_dir_not_rmdir(self):
        remote = {
            "data": {"type": "dir"},
        }
        p = self.plan({}, {}, remote, {}, delete=True, protected=["data/**"])
        self.assertEqual(p.rmdir, [])


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

class TestExecute(unittest.TestCase):
    def test_order_and_atomicity(self):
        with tempfile.TemporaryDirectory() as tmp:
            local_root = tree(tmp, {"index.php": b"<?php echo 1;",
                                    "src/app.php": b"x"})
            remote = {"old.php": ("file", 10)}
            t = FakeTransport(remote, root="site")
            local_dirs, local_files = local_walk(local_root, IgnoreRules([]))
            r = remote_walk(t, "/site")
            st = make_state({"old.php": ("sha-old", 10, 10)})
            plan = compute_plan(local_dirs, local_files, r, st,
                                IgnoreRules([]), adopt_allowed=False,
                                delete_enabled=True, mirror=False)
            done = execute(plan, t, local_root, "/site", local_dirs, verbose=False)
            self.assertEqual(done["uploaded"], 2)
            self.assertEqual(done["deleted"], 1)
            self.assertEqual(done["mkdir"], 1)
            # mkdir before upload, deletes after uploads
            kinds = [op[0] for op in t.ops]
            self.assertEqual(kinds, ["mkdir", "upload", "upload", "delete"])
            self.assertIn(("upload", "index.php"), t.ops)
            self.assertIn(("upload", "src/app.php"), t.ops)
            self.assertIn(("delete", "old.php"), t.ops)
            # no leftover state in fake for deleted file
            self.assertNotIn("old.php", t.entries)
            self.assertEqual(t.entries["index.php"][1], 13)

    def test_rmdir_parents_after_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            local_root = tree(tmp, {})  # nothing local
            remote = {"old": ("dir", None), "old/x.php": ("file", 10)}
            t = FakeTransport(remote, root="site")
            local_dirs, local_files = local_walk(local_root, IgnoreRules([]))
            r = remote_walk(t, "/site")
            st = make_state({"old/x.php": ("sha-old", 10, 10)})
            plan = compute_plan(local_dirs, local_files, r, st, IgnoreRules([]),
                                adopt_allowed=False, delete_enabled=True, mirror=False)
            done = execute(plan, t, local_root, "/site", local_dirs, verbose=False)
            self.assertEqual(done["deleted"], 1)
            self.assertEqual(done["rmdir"], 1)
            self.assertNotIn("old", t.entries)

    def test_keep_remote_dir_when_local_empty_dir_remains(self):
        with tempfile.TemporaryDirectory() as tmp:
            local_root = tree(tmp, {})
            dirs_of(local_root, ["old"])  # empty local dir stays
            remote = {"old": ("dir", None), "old/x.php": ("file", 10)}
            t = FakeTransport(remote, root="site")
            local_dirs, local_files = local_walk(local_root, IgnoreRules([]))
            r = remote_walk(t, "/site")
            st = make_state({"old/x.php": ("sha-old", 10, 10)})
            plan = compute_plan(local_dirs, local_files, r, st, IgnoreRules([]),
                                adopt_allowed=False, delete_enabled=True, mirror=False)
            done = execute(plan, t, local_root, "/site", local_dirs, verbose=False)
            self.assertEqual(done["deleted"], 1)
            self.assertEqual(done["rmdir"], 0)
            self.assertIn("old", t.entries)


# ---------------------------------------------------------------------------
# State store
# ---------------------------------------------------------------------------

class TestState(unittest.TestCase):
    def test_roundtrip_and_remote_dir_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "state-x.json"
            st = StateStore(p, "/site")
            self.assertTrue(st.fresh)
            st.data = {"a": {"sha": "s", "size": 1, "remote_size": 1}}
            st.save()
            st2 = StateStore(p, "/site")
            self.assertFalse(st2.fresh)
            self.assertEqual(st2.data["a"]["sha"], "s")
            st3 = StateStore(p, "/other")
            self.assertTrue(st3.fresh)  # different remote_dir -> fresh
            self.assertEqual(st3.data, {})

    def test_update_state_keeps_tracked_but_not_deleted(self):
        remote = {"old.php": {"type": "file", "size": 10}}
        local_files = {"a.php": (20, "sha-a")}
        plan = compute_plan({}, local_files, remote,
                            make_state({"old.php": ("sha-old", 10, 10)}),
                            IgnoreRules([]), adopt_allowed=False,
                            delete_enabled=False, mirror=False)
        state = {"old.php": {"sha": "sha-old", "size": 10, "remote_size": 10}}
        update_state(state, plan, local_files, remote)
        self.assertIn("a.php", state)
        self.assertEqual(state["a.php"]["remote_size"], 20)
        self.assertIn("old.php", state)  # not deleted (no --delete), kept


if __name__ == "__main__":
    unittest.main(verbosity=2)
