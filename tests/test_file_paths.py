"""The agent's file tools take a file's path in the person's files, folders
and all — the same path Files, the Developer tools and the rest use.

    python tests/test_file_paths.py
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src import agent  # noqa: E402


class FilePaths(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.files = os.path.join(self.base, "files")
        self.old = agent._sess().static_dir
        agent.set_static_dir(self.files)
        self.addCleanup(shutil.rmtree, self.base, True)
        self.addCleanup(setattr, agent._sess(), "static_dir", self.old)

    def put(self, rel, text):
        path = os.path.join(self.files, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def tool(self, name, **args):
        return str(agent._run_tool(name, args))

    def test_read_a_file_in_a_folder(self):
        self.put("projects/drafts/notes.md", "the ledger\n")
        self.put("notes.md", "a different file at the top\n")
        self.assertIn("the ledger", self.tool("read_file", filename="projects/drafts/notes.md"))
        self.assertIn("the ledger", self.tool("read_file", filename="files/projects/drafts/notes.md"))

    def test_an_uploads_url_path_is_its_path(self):
        self.put("uploads/a.txt", "uploaded\n")
        self.assertIn("uploaded", self.tool("read_file", filename="static/bob/files/uploads/a.txt"))

    def test_a_top_level_file_named_with_a_folder_still_reads(self):
        # Where every file used to be: a read of a/b.txt that isn't there
        # finds b.txt at the top.
        self.put("b.txt", "top\n")
        self.assertIn("top", self.tool("read_file", filename="somewhere/b.txt"))

    def test_create_edit_delete_in_folders(self):
        self.tool("create_file", filename="projects/drafts/x/caption.txt", contents="one")
        path = os.path.join(self.files, "projects", "drafts", "x", "caption.txt")
        self.assertEqual(open(path).read(), "one")
        self.assertIn("edited", self.tool("edit_file", filename="projects/drafts/x/caption.txt",
                                          old_str="one", new_str="two"))
        self.assertEqual(open(path).read(), "two")
        self.assertEqual(self.tool("delete_file", filename="projects/drafts/x/caption.txt"), "File deleted.")
        self.assertFalse(os.path.exists(path))

    def test_list_files_shows_folders_and_lists_one(self):
        self.put("projects/drafts/a.md", "x")
        self.put("ping.md", "")
        top = self.tool("list_files")
        self.assertEqual(top, "In their files:\nping.md\nprojects/\n  projects/drafts/\n    a.md")
        self.assertIn("ping.md", top)
        self.assertEqual(self.tool("list_files", path="projects"), "In projects/:\nprojects/drafts/\n  a.md")
        self.assertEqual(self.tool("list_files", path="projects/drafts/"), "In projects/drafts/: a.md")
        self.assertIn("not found", self.tool("list_files", path="nope"))
        self.assertIn("not a folder", self.tool("list_files", path="ping.md"))

    def test_list_files_finds_a_file_folders_down_in_one_call(self):
        # 2026-11-12: one level a call, the third call (drafts/ → million_short/)
        # was the one the repeat guard held, and the agent said the video wasn't there.
        self.put("projects/drafts/million_short/what_if.mp4", "v")
        self.put("projects/drafts/million_short/frames/0001.jpg", "f")
        self.put("ping.md", "")
        top = self.tool("list_files")
        self.assertIn("projects/drafts/million_short/\n", top)
        self.assertIn("\n      what_if.mp4", top)
        self.assertIn("projects/drafts/million_short/frames/ (1 file)", top)  # 4 down: a count
        self.assertTrue(top.startswith("In their files:\nping.md\nprojects/\n"), top)

    def test_list_files_big_folder_is_a_count(self):
        for i in range(agent.LIST_NESTED_LINES + 1):
            self.put(f"out/frames/{i:04}.jpg", "f")
        self.put("out/video.mp4", "v")
        top = self.tool("list_files")
        self.assertIn(f"out/frames/ ({agent.LIST_NESTED_LINES + 1} files)", top)
        self.assertIn("  video.mp4", top)
        self.assertNotIn("0000.jpg", top)
        self.assertIn("0000.jpg", self.tool("list_files", path="out/frames"))  # listing it opens it

    def test_list_files_empty_and_flat(self):
        os.makedirs(os.path.join(self.files, "a", "empty"))
        self.put("a/x.txt", "x")
        self.assertEqual(self.tool("list_files"), "In their files:\na/\n  x.txt\n  a/empty/ (empty)")
        self.assertEqual(self.tool("list_files", path="a/empty"), "In a/empty/: nothing")

    def test_list_files_never_opens_a_link(self):
        outside = os.path.join(self.base, "elsewhere")
        os.makedirs(outside)
        with open(os.path.join(outside, "secret.txt"), "w") as f:
            f.write("no")
        self.put("p/a.txt", "x")
        os.symlink(outside, os.path.join(self.files, "p", "link"))
        top = self.tool("list_files")
        self.assertIn("p/link/", top)
        self.assertNotIn("secret.txt", top)

    def test_stays_inside(self):
        outside = os.path.join(self.base, "secret.txt")
        with open(outside, "w") as f:
            f.write("no")
        for bad in ("../secret.txt", "/etc/passwd", "projects/../../secret.txt", "C:/x.txt"):
            with self.subTest(bad=bad):
                self.assertIn("Invalid filename", self.tool("read_file", filename=bad))
        os.symlink(self.base, os.path.join(self.files, "link"))
        self.assertIn("outside", self.tool("read_file", filename="link/secret.txt"))
        self.assertIn("outside", self.tool("create_file", filename="link/new.txt", contents="x"))
        self.assertFalse(os.path.exists(os.path.join(self.base, "new.txt")))

    def test_memory_stays_protected(self):
        self.put("context.md", "# memory\n")
        self.assertIn("isn't read this way", self.tool("read_file", filename="context.md"))
        self.assertIn("NOT deleted", self.tool("delete_file", filename="context.md"))
        # A file of that name in a folder is just a file.
        self.put("projects/context.md", "a doc\n")
        self.assertIn("a doc", self.tool("read_file", filename="projects/context.md"))


if __name__ == "__main__":
    unittest.main()
