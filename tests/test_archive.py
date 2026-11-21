"""The archive's members, the chip check, and the version check.

The traversal case is the one that matters: on this machine's bsdtar,
`tar -xzf` writes a `../` member outside the target directory. These
tests build such a tarball and prove the tool refuses it.
"""
import io
import os
import plistlib
import shutil
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kirimcp import verify  # noqa: E402
from kirimcp.errors import KiriMcpError  # noqa: E402


def make_tarball(path, members):
    """A tarball with the given members: (name, kind, payload)."""
    with tarfile.open(path, "w:gz") as archive:
        for name, kind, payload in members:
            info = tarfile.TarInfo(name)
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                archive.addfile(info)
            elif kind == "link":
                info.type = tarfile.SYMTYPE
                info.linkname = payload
                archive.addfile(info)
            else:
                data = payload.encode("utf-8")
                info.size = len(data)
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(data))
    return path


class TestArchiveMembers(unittest.TestCase):
    """What the tool accepts and what it refuses, before extracting."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="kiri-mcp-archive-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_well_formed_archive_passes(self):
        path = make_tarball(
            os.path.join(self.dir, "good.tar.gz"),
            [("Kiri.app/", "dir", None), ("Kiri.app/Info.plist", "file", "<plist/>")],
        )
        names = verify.safe_members(path)
        self.assertEqual(len(names), 2)
        self.assertTrue(names[0].startswith("Kiri.app"))
        self.assertEqual(names[1], "Kiri.app/Info.plist")

    def test_a_parent_member_is_refused(self):
        path = make_tarball(
            os.path.join(self.dir, "escape.tar.gz"),
            [("Kiri.app/", "dir", None), ("../escape.txt", "file", "pwned")],
        )
        with self.assertRaises(KiriMcpError) as caught:
            verify.safe_members(path)
        self.assertEqual(caught.exception.code, "unsafe_archive")

    def test_an_absolute_member_is_refused(self):
        path = make_tarball(
            os.path.join(self.dir, "absolute.tar.gz"),
            [("/tmp/escape.txt", "file", "pwned")],
        )
        with self.assertRaises(KiriMcpError) as caught:
            verify.safe_members(path)
        self.assertEqual(caught.exception.code, "unsafe_archive")

    def test_a_symlink_member_is_refused(self):
        path = make_tarball(
            os.path.join(self.dir, "link.tar.gz"),
            [("Kiri.app/", "dir", None), ("Kiri.app/link", "link", "/etc/passwd")],
        )
        with self.assertRaises(KiriMcpError) as caught:
            verify.safe_members(path)
        self.assertEqual(caught.exception.code, "unsafe_archive")

    def test_extracting_an_escaping_archive_writes_nothing_outside(self):
        path = make_tarball(
            os.path.join(self.dir, "escape.tar.gz"),
            [("../escape.txt", "file", "pwned")],
        )
        target = os.path.join(self.dir, "unpack")
        with self.assertRaises(KiriMcpError):
            verify.extract(path, target)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "escape.txt")))

    def test_appledouble_sidecars_are_left_out(self):
        """A `._name` file in the bundle would break the code signature.

        macOS puts a sidecar next to every file to carry the resource
        fork. bsdtar skips them; Python's tarfile does not, and a bundle
        with stray files in it no longer matches its signature.
        """
        path = make_tarball(
            os.path.join(self.dir, "sidecar.tar.gz"),
            [
                ("Kiri.app/", "dir", None),
                ("._Kiri.app", "file", "sidecar"),
                ("Kiri.app/._Info.plist", "file", "sidecar"),
                ("Kiri.app/Info.plist", "file", "<plist/>"),
            ],
        )
        names = verify.safe_members(path)
        self.assertNotIn("._Kiri.app", names)
        self.assertNotIn("Kiri.app/._Info.plist", names)
        self.assertIn("Kiri.app/Info.plist", names)

        target = os.path.join(self.dir, "unpack")
        verify.extract(path, target)
        self.assertFalse(os.path.exists(os.path.join(target, "._Kiri.app")))
        self.assertFalse(os.path.exists(os.path.join(target, "Kiri.app", "._Info.plist")))
        self.assertTrue(os.path.isfile(os.path.join(target, "Kiri.app", "Info.plist")))

    def test_a_good_archive_extracts(self):
        path = make_tarball(
            os.path.join(self.dir, "good.tar.gz"),
            [("Kiri.app/", "dir", None), ("Kiri.app/Info.plist", "file", "<plist/>")],
        )
        target = os.path.join(self.dir, "unpack")
        verify.extract(path, target)
        self.assertTrue(os.path.isfile(os.path.join(target, "Kiri.app", "Info.plist")))


class TestArchitecture(unittest.TestCase):
    """The Mach-O check, on headers built here."""

    def thin(self, cputype):
        # MH_MAGIC_64, little-endian on disk: the cputype follows.
        return b"\xcf\xfa\xed\xfe" + cputype.to_bytes(4, "little") + b"\x00" * 64

    def fat(self, cputypes):
        header = b"\xca\xfe\xba\xbe" + len(cputypes).to_bytes(4, "big")
        for cputype in cputypes:
            header += cputype.to_bytes(4, "big") + b"\x00" * 16
        return header + b"\x00" * 64

    def test_a_thin_arm64_binary(self):
        self.assertTrue(verify.has_cpu(self.thin(verify.ARM64), verify.ARM64))
        self.assertFalse(verify.has_cpu(self.thin(verify.ARM64), verify.X86_64))

    def test_a_thin_x86_64_binary(self):
        self.assertTrue(verify.has_cpu(self.thin(verify.X86_64), verify.X86_64))
        self.assertFalse(verify.has_cpu(self.thin(verify.X86_64), verify.ARM64))

    def test_a_universal_binary_with_the_slice(self):
        head = self.fat([verify.X86_64, verify.ARM64])
        self.assertTrue(verify.has_cpu(head, verify.ARM64))
        self.assertTrue(verify.has_cpu(head, verify.X86_64))

    def test_something_that_is_not_macho(self):
        self.assertFalse(verify.has_cpu(b"#!/bin/sh\n" + b"\x00" * 64, verify.ARM64))


class TestVersion(unittest.TestCase):
    """The app must be the version the feed promised."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="kiri-mcp-version-")
        self.app = os.path.join(self.dir, "Kiri.app")
        os.makedirs(os.path.join(self.app, "Contents"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write_plist(self, version):
        with open(os.path.join(self.app, "Contents", "Info.plist"), "wb") as f:
            plistlib.dump({"CFBundleShortVersionString": version}, f)

    def test_the_promised_version_passes(self):
        self.write_plist("1.2.3")
        self.assertEqual(verify.verify_version(self.app, "1.2.3"), "1.2.3")

    def test_a_leading_v_is_ignored(self):
        self.write_plist("1.2.3")
        self.assertEqual(verify.verify_version(self.app, "v1.2.3"), "1.2.3")

    def test_another_version_is_refused(self):
        self.write_plist("1.2.3")
        with self.assertRaises(KiriMcpError) as caught:
            verify.verify_version(self.app, "1.2.4")
        self.assertEqual(caught.exception.code, "version_mismatch")


if __name__ == "__main__":
    unittest.main()
