"""The Linux path: the checksum lists, the package choice and the refusals.

Nothing here installs a package or reaches the network: the lists and the
package are served from 127.0.0.1, and the package manager is never run.
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kirimcp import linux  # noqa: E402
from kirimcp.errors import KiriMcpError  # noqa: E402
from tests.test_install import LocalServer  # noqa: E402

SUMS = (
    "{a}  kiri_0.2.2_amd64.deb\n{b}  kiri-lite_0.2.2_amd64.deb\n{c}  kiri-lite_0.2.1_amd64.deb\n"
    "{d}  kiri-0.2.2-1.x86_64.rpm\n{e}  kiri-lite-0.2.2-1.aarch64.rpm\n{f}  Kiri-0.2.2-arm64.dmg\n"
).format(a="a" * 64, b="b" * 64, c="c" * 64, d="d" * 64, e="e" * 64, f="f" * 64)


class TestSums(unittest.TestCase):
    def test_lines_are_read(self):
        sums = linux.parse_sums(SUMS.encode(), "x")
        self.assertEqual(sums["kiri-lite_0.2.2_amd64.deb"], "b" * 64)

    def test_an_empty_list_is_refused(self):
        with self.assertRaises(KiriMcpError) as caught:
            linux.parse_sums(b"nothing here\n", "x")
        self.assertEqual(caught.exception.code, "feed_invalid")

    def test_only_linux_packages_are_named(self):
        found = linux.packages(linux.parse_sums(SUMS.encode(), "x"))
        self.assertIn(("deb", "kiri", "0.2.2", "amd64", "kiri_0.2.2_amd64.deb"), found)
        self.assertIn(("rpm", "kiri-lite", "0.2.2", "aarch64", "kiri-lite-0.2.2-1.aarch64.rpm"), found)
        self.assertFalse([p for p in found if p[4].endswith(".dmg")])


@unittest.skipUnless(linux.is_linux() and shutil.which("dpkg-deb"), "needs Linux with dpkg-deb")
class TestAgainstALocalServer(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="kiri-mcp-linux-")
        self.served = os.path.join(self.dir, "served")
        os.makedirs(self.served)
        self.server = LocalServer(self.served)
        self.old_env = dict(os.environ)
        os.environ["KIRI_SUMS_URL"] = self.server.base + "/SHA256SUMS-linux"
        os.environ["KIRI_GITHUB_SUMS_URL"] = self.server.base + "/gh-{version}"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old_env)
        self.server.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def make_deb(self, file_name, package, version, depends=""):
        root = os.path.join(self.dir, "pkg-" + package)
        os.makedirs(os.path.join(root, "DEBIAN"), exist_ok=True)
        control = "Package: {0}\nVersion: {1}\nArchitecture: amd64\nMaintainer: t <t@t>\nDescription: t\n".format(package, version)
        if depends:
            control += "Depends: {0}\n".format(depends)
        with open(os.path.join(root, "DEBIAN", "control"), "w") as f:
            f.write(control)
        path = os.path.join(self.served, file_name)
        subprocess.run(["dpkg-deb", "-b", root, path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    def publish(self, entries, github=None):
        text = "".join("{0}  {1}\n".format(d, n) for n, d in entries.items())
        with open(os.path.join(self.served, "SHA256SUMS-linux"), "w") as f:
            f.write(text)
        gh = github if github is not None else entries
        with open(os.path.join(self.served, "gh-0.2.2"), "w") as f:
            f.write("".join("{0}  {1}\n".format(d, n) for n, d in gh.items()))

    def fetch(self, flavor="kiri-lite", why="installed"):
        release, name = linux.latest("deb", "x86_64", flavor)
        plan = {"notes": []}
        checks = []
        staging = os.path.join(self.dir, "stage")
        os.makedirs(staging, exist_ok=True)
        result = linux._fetch_verified("deb", "x86_64", flavor, why, release, name, staging,
                                       lambda c, ok, d="": checks.append(c), plan)
        return result, plan

    def test_the_newest_version_is_chosen(self):
        self.publish({"kiri-lite_0.2.1_amd64.deb": "c" * 64, "kiri-lite_0.2.2_amd64.deb": "b" * 64})
        release, name = linux.latest("deb", "x86_64", "kiri-lite")
        self.assertEqual((release.version, name), ("0.2.2", "kiri-lite_0.2.2_amd64.deb"))
        self.assertEqual(release.url, self.server.base + "/kiri-lite_0.2.2_amd64.deb")

    def test_a_good_package_passes(self):
        digest = self.make_deb("kiri-lite_0.2.2_amd64.deb", "kiri-lite", "0.2.2")
        self.publish({"kiri-lite_0.2.2_amd64.deb": digest})
        (path, name, release, flavor), _ = self.fetch()
        self.assertEqual((name, flavor), ("kiri-lite_0.2.2_amd64.deb", "kiri-lite"))

    def test_github_disagreeing_is_refused(self):
        digest = self.make_deb("kiri-lite_0.2.2_amd64.deb", "kiri-lite", "0.2.2")
        self.publish({"kiri-lite_0.2.2_amd64.deb": digest}, github={"kiri-lite_0.2.2_amd64.deb": "0" * 64})
        with self.assertRaises(KiriMcpError) as caught:
            self.fetch()
        self.assertEqual(caught.exception.code, "checksum_mismatch")

    def test_a_wrong_checksum_is_refused(self):
        self.make_deb("kiri-lite_0.2.2_amd64.deb", "kiri-lite", "0.2.2")
        self.publish({"kiri-lite_0.2.2_amd64.deb": "1" * 64})
        with self.assertRaises(KiriMcpError) as caught:
            self.fetch()
        self.assertEqual(caught.exception.code, "checksum_mismatch")

    def test_a_package_that_is_not_what_its_name_says_is_refused(self):
        digest = self.make_deb("kiri-lite_0.2.2_amd64.deb", "kiri-lite", "0.2.1")
        self.publish({"kiri-lite_0.2.2_amd64.deb": digest})
        with self.assertRaises(KiriMcpError) as caught:
            self.fetch()
        self.assertEqual(caught.exception.code, "version_mismatch")

    def test_an_old_glibc_falls_back_to_kiri_lite(self):
        full = self.make_deb("kiri_0.2.2_amd64.deb", "kiri", "0.2.2", depends="libc6 (>= 99.0)")
        lite = self.make_deb("kiri-lite_0.2.2_amd64.deb", "kiri-lite", "0.2.2")
        self.publish({"kiri_0.2.2_amd64.deb": full, "kiri-lite_0.2.2_amd64.deb": lite})
        (path, name, release, flavor), plan = self.fetch(flavor="kiri", why="default")
        self.assertEqual(flavor, "kiri-lite")
        self.assertTrue(plan["notes"])

    def test_an_installed_full_kiri_is_not_switched_silently(self):
        full = self.make_deb("kiri_0.2.2_amd64.deb", "kiri", "0.2.2", depends="libc6 (>= 99.0)")
        self.publish({"kiri_0.2.2_amd64.deb": full})
        with self.assertRaises(KiriMcpError) as caught:
            self.fetch(flavor="kiri", why="installed")
        self.assertEqual(caught.exception.code, "unsupported_os")


if __name__ == "__main__":
    unittest.main()
