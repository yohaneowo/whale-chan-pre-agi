import os
import shutil
import tempfile
import unittest
from unittest import mock

import build_meme_library as lib

JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 20
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 20
GIF = b"GIF89a" + b"\0" * 20
WEBP = b"RIFF\0\0\0\0WEBPVP8 " + b"\0" * 20


def meme(n="001", original="https://img/meme/001.png", preview="https://img/0_preview/meme/001.webp"):
    return {"n": n, "quote": "得加钱", "caption": "", "story": "", "original": original, "preview": preview}


class SniffTest(unittest.TestCase):
    def test_reads_type_from_bytes(self):
        self.assertEqual(lib.sniff(JPEG), "jpg")
        self.assertEqual(lib.sniff(PNG), "png")
        self.assertEqual(lib.sniff(GIF), "gif")
        self.assertEqual(lib.sniff(WEBP), "webp")
        self.assertIsNone(lib.sniff(b"<html>"))


class BuildOneTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.root, lib.ORIGINAL_DIR))
        os.makedirs(os.path.join(self.root, lib.PREVIEW_DIR))

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_original_saved_with_real_extension(self):
        # 网站写 .png，其实是 JPEG
        result = lib.build_one(self.root, meme(), lambda url: JPEG)
        self.assertEqual(result, ("img-original/001.jpg", "original", None))
        with open(os.path.join(self.root, result[0]), "rb") as file:
            self.assertEqual(file.read(), JPEG)

    def test_rerun_reuses_existing_file(self):
        lib.build_one(self.root, meme(), lambda url: GIF)
        fetcher = mock.Mock(side_effect=AssertionError("should not download"))
        self.assertEqual(lib.build_one(self.root, meme(), fetcher), ("img-original/001.gif", "original", None))

    def test_large_original_kept_full_size_with_preview_fallback(self):
        big = JPEG + b"\0" * 64
        with mock.patch.object(lib, "DISCORD_LIMIT", 40):
            self.assertEqual(lib.build_one(self.root, meme(), lambda url: WEBP if "preview" in url else big),
                             ("img-original/001.jpg", "original", "img/001.webp"))
        with open(os.path.join(self.root, "img-original/001.jpg"), "rb") as file:
            self.assertEqual(file.read(), big)

    def test_old_compressed_copy_is_fetched_again(self):
        with open(os.path.join(self.root, "img-original/001.webp"), "wb") as file:
            file.write(WEBP)
        open(os.path.join(self.root, "img-original/001.compressed"), "w").close()
        self.assertEqual(lib.build_one(self.root, meme(), lambda url: PNG), ("img-original/001.png", "original", None))
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, "img-original"))), ["001.png"])

    def test_falls_back_to_preview_when_original_fails(self):
        def fetcher(url):
            if "preview" in url:
                return WEBP
            raise OSError("404")
        self.assertEqual(lib.build_one(self.root, meme(), fetcher), ("img/001.webp", "preview", None))

    def test_unknown_format_falls_back_to_preview(self):
        self.assertEqual(
            lib.build_one(self.root, meme(), lambda url: WEBP if "preview" in url else b"<html>"),
            ("img/001.webp", "preview", None))

    def test_nothing_usable_is_skipped(self):
        def fetcher(url):
            raise OSError("down")
        self.assertEqual(lib.build_one(self.root, meme(), fetcher), (None, None, None))


if __name__ == "__main__":
    unittest.main()
