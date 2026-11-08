"""The Files panel plays videos and audio: _file_kind says 'video' or
'audio', and the raw bytes
come back seekable (a Range request gets 206) with a video type.

    python tests/test_files_video.py
"""
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "Flask"))
import main  # noqa: E402
from flask import send_from_directory  # noqa: E402


class FilesVideo(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_kinds(self):
        clip = b"\x00\x00\x00\x18ftypmp42" + b"x" * 4000
        for name in ("a.mp4", "b.MOV", "c.webm", "d.m4v", "e.ogv"):
            self.assertEqual(main._file_kind(self.write(name, clip)), "video", name)
        for name in ("a.mp3", "b.WAV", "c.m4a", "d.ogg", "e.opus", "f.flac", "g.aac", "h.oga"):
            self.assertEqual(main._file_kind(self.write(name, b"ID3\x00" + b"x" * 400)), "audio", name)
        self.assertEqual(main._file_kind(self.write("p.png", b"\x89PNG")), "image")
        self.assertEqual(main._file_kind(self.write("t.txt", b"hello")), "text")
        self.assertEqual(main._file_kind(self.write("z.zip", b"PK\x00\x00")), "binary")

    def test_seekable(self):
        self.write("a.mp4", b"\x00\x00\x00\x18ftypmp42" + b"x" * 4000)
        with main.app.test_request_context(headers={"Range": "bytes=0-99"}):
            r = send_from_directory(self.dir, "a.mp4")
            self.assertEqual(r.status_code, 206)
            self.assertEqual(r.headers["Content-Type"], "video/mp4")


if __name__ == "__main__":
    unittest.main()
