"""Images go to the model as images: get_image_description and uploads.

    python tests/test_native_images.py
"""
import base64
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from PIL import Image  # noqa: E402
from src import agent  # noqa: E402


def png(path, size, mode="RGB"):
    Image.new(mode, size, (200, 30, 30) if mode == "RGB" else (200, 30, 30, 128)).save(path)


class NativeImages(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp() + os.sep
        png(self.dir + "small.png", (400, 300))
        Image.new("RGB", (4000, 3000), (10, 120, 200)).save(self.dir + "big.jpg", quality=95)
        png(self.dir + "clear.png", (3000, 1000), "RGBA")
        with open(self.dir + "notes.txt", "w") as f:
            f.write("hi")
        providers = [("Main", "https://x", "k", None, None, "responses")]
        patch = mock.patch.object(agent, "_active_providers", return_value=providers)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(shutil.rmtree, self.dir, True)
        agent._unsupported_extras.discard("Main")

    def decode(self, url):
        head, data = url.split(",", 1)
        return head, Image.open(io.BytesIO(base64.b64decode(data)))

    def test_small_image_as_it_is(self):
        url, size = agent._image_data_url(self.dir + "small.png")
        self.assertTrue(url.startswith("data:image/png;base64,"))
        self.assertEqual(size, (400, 300))

    def test_big_image_scaled_down(self):
        url, size = agent._image_data_url(self.dir + "big.jpg")
        head, im = self.decode(url)
        self.assertEqual(head, "data:image/jpeg;base64")
        self.assertEqual(im.size, (2048, 1536))
        self.assertEqual(size, (2048, 1536))

    def test_transparency_kept(self):
        url, _ = agent._image_data_url(self.dir + "clear.png")
        self.assertTrue(url.startswith("data:image/png"))

    def test_not_an_image(self):
        self.assertIsNone(agent._image_data_url(self.dir + "notes.txt"))

    def test_upload_attached_to_the_message(self):
        msg = {"role": "user", "content": '[File uploaded: "Photo.png" → static/u/files/small.png]\n\nWhat is this?'}
        agent._attach_upload(msg, self.dir)
        self.assertEqual(len(msg["images"]), 1)
        out = agent._with_tool_images([msg])
        self.assertEqual(out[1]["content"][0]["text"], "The image they sent:")
        self.assertEqual(out[1]["content"][1]["type"], "image_url")

    def test_no_image_for_other_uploads_or_plain_text(self):
        for content in ('[File uploaded: "n.txt" → static/u/files/notes.txt]\n\nRead it',
                        "just words", '[File uploaded: "x.png" → static/u/files/missing.png]'):
            msg = {"role": "user", "content": content}
            agent._attach_upload(msg, self.dir)
            self.assertNotIn("images", msg, content)

    def test_a_chain_that_refused_images_gets_none(self):
        agent._unsupported_extras.add("Main")
        try:
            msg = {"role": "user", "content": '[File uploaded: "p.png" → static/u/files/small.png]'}
            agent._attach_upload(msg, self.dir)
            self.assertNotIn("images", msg)
            self.assertFalse(agent._sees_images())
        finally:
            agent._unsupported_extras.discard("Main")


if __name__ == "__main__":
    unittest.main()
