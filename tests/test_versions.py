"""The version comparison, the feed, the mode and the notes cap.

    python3 -m unittest discover -s tests -t .
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kirimcp import config, releases  # noqa: E402
from kirimcp import platform as kiri_platform  # noqa: E402
from kirimcp.errors import KiriMcpError  # noqa: E402

MAC_ONLY = unittest.skipUnless(kiri_platform.is_macos(), "the macOS install path")


class TestVersions(unittest.TestCase):
    """The comparison `kiri update` makes, with the same cases."""

    def test_newer(self):
        self.assertTrue(releases.newer("0.2.0", "0.1.9"))
        self.assertTrue(releases.newer("1.2.10", "1.2.9"))
        self.assertTrue(releases.newer("v1.0.1", "1.0"))
        self.assertFalse(releases.newer("0.1.0", "0.1.0"))
        self.assertFalse(releases.newer("0.1.0", "0.2.0"))

    def test_a_missing_part_counts_as_zero(self):
        self.assertTrue(releases.newer("1.1", "1.0.9"))
        self.assertFalse(releases.newer("1", "1.0.0"))

    def test_a_dash_separates_parts(self):
        self.assertTrue(releases.newer("1.2.0-2", "1.2.0-1"))


class TestFeed(unittest.TestCase):
    """What the feed must say, and what it must not."""

    def parse(self, data):
        return releases.parse_feed(json.dumps(data).encode("utf-8"), "https://example.invalid/feed.json")

    def test_a_good_feed(self):
        release = self.parse({"version": "1.2.3", "url": "https://example.invalid/kiri.tar.gz", "sha256": "abc123"})
        self.assertEqual(release.version, "1.2.3")
        self.assertEqual(release.sha256, "abc123")
        self.assertFalse(release.notes_truncated)

    def test_a_feed_without_a_version(self):
        with self.assertRaises(KiriMcpError) as caught:
            self.parse({"url": "https://example.invalid/kiri.tar.gz", "sha256": "abc123"})
        self.assertEqual(caught.exception.code, "feed_invalid")

    def test_a_feed_without_a_url(self):
        with self.assertRaises(KiriMcpError) as caught:
            self.parse({"version": "1.2.3", "sha256": "abc123"})
        self.assertEqual(caught.exception.code, "feed_invalid")

    def test_a_feed_without_a_checksum(self):
        with self.assertRaises(KiriMcpError) as caught:
            self.parse({"version": "1.2.3", "url": "https://example.invalid/kiri.tar.gz"})
        self.assertEqual(caught.exception.code, "feed_invalid")

    def test_an_http_artifact_is_refused(self):
        with self.assertRaises(KiriMcpError) as caught:
            self.parse({"version": "1.2.3", "url": "http://example.invalid/kiri.tar.gz", "sha256": "abc123"})
        self.assertEqual(caught.exception.code, "feed_insecure")

    def test_not_json_is_refused(self):
        with self.assertRaises(KiriMcpError) as caught:
            releases.parse_feed(b"not json", "https://example.invalid/feed.json")
        self.assertEqual(caught.exception.code, "feed_invalid")


class TestNotesCap(unittest.TestCase):
    """The release notes are cut, and the cut is visible."""

    def test_short_notes_are_whole(self):
        notes, truncated = releases.truncate_notes("short")
        self.assertEqual(notes, "short")
        self.assertFalse(truncated)

    def test_long_notes_are_cut_with_a_marker(self):
        notes, truncated = releases.truncate_notes("x" * (releases.NOTES_LIMIT + 100))
        self.assertTrue(truncated)
        self.assertTrue(notes.endswith("… (truncated)"))
        self.assertEqual(len(notes), releases.NOTES_LIMIT + len("… (truncated)"))

    def test_the_cut_holds_in_the_parsed_feed(self):
        body = json.dumps({
            "version": "1.2.3",
            "url": "https://example.invalid/kiri.tar.gz",
            "sha256": "abc123",
            "notes": "y" * 8000,
        }).encode("utf-8")
        release = releases.parse_feed(body, "https://example.invalid/feed.json")
        self.assertTrue(release.notes_truncated)
        self.assertLess(len(release.notes), 8000)


class TestFeedUrls(unittest.TestCase):
    """Each kind of Mac has its own feed."""

    def test_arm64(self):
        self.assertEqual(config.feed_url("arm64"), "https://download.runkiri.com/latest-mac-arm64.json")

    def test_x86_64(self):
        self.assertEqual(config.feed_url("x86_64"), "https://download.runkiri.com/latest-mac-x86_64.json")

    def test_an_override_wins(self):
        os.environ["KIRI_UPDATE_URL"] = "https://example.invalid/feed.json"
        try:
            self.assertEqual(config.feed_override(), "https://example.invalid/feed.json")
        finally:
            del os.environ["KIRI_UPDATE_URL"]
        self.assertIsNone(config.feed_override())


@MAC_ONLY
class TestModes(unittest.TestCase):
    """The mode a caller asks for must match the machine."""

    def test_upgrade_without_an_app_is_a_conflict(self):
        from kirimcp import install

        original = install.platform.installed_app
        install.platform.installed_app = lambda: None
        try:
            with self.assertRaises(KiriMcpError) as caught:
                install.install_latest(mode="upgrade")
            self.assertEqual(caught.exception.code, "mode_conflict")
        finally:
            install.platform.installed_app = original

    def test_install_with_an_app_is_a_conflict(self):
        from kirimcp import install

        original = install.platform.installed_app
        install.platform.installed_app = lambda: "/Applications/Kiri.app"
        try:
            with self.assertRaises(KiriMcpError) as caught:
                install.install_latest(mode="install")
            self.assertEqual(caught.exception.code, "mode_conflict")
        finally:
            install.platform.installed_app = original

    def test_an_unknown_mode_is_a_conflict(self):
        from kirimcp import install

        with self.assertRaises(KiriMcpError) as caught:
            install.install_latest(mode="whatever")
        self.assertEqual(caught.exception.code, "mode_conflict")


if __name__ == "__main__":
    unittest.main()
