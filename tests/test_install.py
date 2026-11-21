"""The install path, end to end, against a local server.

Nothing here touches /Applications or the network: the feed and the
artifact are served from 127.0.0.1, and `KIRI_APP_DIR` points the
install at a temporary directory. The tamper fixtures are built here, so
no hostile artifact is ever fetched from anywhere.
"""
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import threading
import unittest
from http.server import HTTPServer, SimpleHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kirimcp import install, releases  # noqa: E402
from kirimcp import platform as kiri_platform  # noqa: E402
from kirimcp.errors import KiriMcpError  # noqa: E402

MAC_ONLY = unittest.skipUnless(kiri_platform.is_macos(), "the macOS install path")


def make_app_tarball(path, version, extra=()):
    """A tarball holding a Kiri.app with an Info.plist."""
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo("Kiri.app/")
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
        archive.addfile(info)
        payload = json.dumps({"CFBundleShortVersionString": version}).encode("utf-8")
        info = tarfile.TarInfo("Kiri.app/Contents/Info.plist")
        info.size = len(payload)
        info.mode = 0o644
        archive.addfile(info, io.BytesIO(payload))
        for name, text in extra:
            info = tarfile.TarInfo(name)
            data = text.encode("utf-8")
            info.size = len(data)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
    return path


class QuietHandler(SimpleHTTPRequestHandler):
    """A handler that says nothing, so the test output stays readable.

    Python 3.9's SimpleHTTPRequestHandler has no `directory` argument, so
    the folder is passed in the URL path instead.
    """

    def __init__(self, *args, **kwargs):
        self.root = kwargs.pop("root", None)
        SimpleHTTPRequestHandler.__init__(self, *args, directory=self.root, **kwargs)

    def log_message(self, *args):
        pass


class LocalServer(object):
    """A file server on 127.0.0.1, serving a temporary directory."""

    def __init__(self, folder):
        self.folder = folder
        handler = lambda *args, **kwargs: QuietHandler(*args, root=folder, **kwargs)  # noqa: E731
        self.server = HTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()

    @property
    def base(self):
        return "http://127.0.0.1:{0}".format(self.server.server_address[1])

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@MAC_ONLY
class TestInstallAgainstALocalServer(unittest.TestCase):
    """The feed, the download and the refusals."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="kiri-mcp-install-")
        self.served = os.path.join(self.dir, "served")
        self.target = os.path.join(self.dir, "Applications")
        os.makedirs(self.served)
        os.makedirs(self.target)
        self.server = LocalServer(self.served)
        self.old_env = dict(os.environ)
        os.environ["KIRI_APP_DIR"] = self.target
        os.environ["KIRI_UPDATE_URL"] = self.server.base + "/feed.json"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old_env)
        self.server.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def write_feed(self, version="1.2.3", url=None, sha256=None):
        payload = {
            "version": version,
            "url": url or (self.server.base + "/kiri.tar.gz"),
            "sha256": sha256 or "0" * 64,
            "notes": "## New\n- Something.",
        }
        with open(os.path.join(self.served, "feed.json"), "w") as f:
            json.dump(payload, f)
        return payload

    def write_artifact(self, version="1.2.3", extra=()):
        path = os.path.join(self.served, "kiri.tar.gz")
        make_app_tarball(path, version, extra)
        with open(path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        self.write_feed(version=version, sha256=digest)
        return path

    def test_a_dry_run_changes_nothing(self):
        self.write_artifact()
        result = install.install_latest(mode="install", dry_run=True)
        self.assertFalse(result["changed"])
        self.assertEqual(result["action"], "install")
        self.assertFalse(os.path.exists(os.path.join(self.target, "Kiri.app")))
        names = [c["check"] for c in result["checks"]]
        self.assertIn("sha256", names)
        self.assertIn("signature", names)
        self.assertIn("notarization", names)

    def test_a_dry_run_creates_no_folder_either(self):
        """Not even an empty app folder: a dry run writes nothing."""
        self.write_artifact()
        fresh = os.path.join(self.dir, "not-created-yet")
        os.environ["KIRI_APP_DIR"] = fresh
        install.install_latest(mode="install", dry_run=True)
        self.assertFalse(os.path.exists(fresh))

    def test_a_wrong_checksum_is_refused(self):
        path = os.path.join(self.served, "kiri.tar.gz")
        make_app_tarball(path, "1.2.3")
        self.write_feed(version="1.2.3", sha256="1" * 64)
        with self.assertRaises(KiriMcpError) as caught:
            install.install_latest(mode="install")
        self.assertEqual(caught.exception.code, "checksum_mismatch")
        self.assertFalse(os.path.exists(os.path.join(self.target, "Kiri.app")))

    def test_an_http_artifact_is_refused_before_any_request(self):
        self.write_feed(url="http://example.invalid/kiri.tar.gz")
        with self.assertRaises(KiriMcpError) as caught:
            install.install_latest(mode="install")
        self.assertEqual(caught.exception.code, "feed_insecure")
        self.assertFalse(os.path.exists(os.path.join(self.target, "Kiri.app")))

    def test_an_escaping_archive_is_refused_and_writes_nothing_outside(self):
        path = os.path.join(self.served, "kiri.tar.gz")
        with tarfile.open(path, "w:gz") as archive:
            info = tarfile.TarInfo("../escape.txt")
            data = b"pwned"
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        with open(path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        self.write_feed(version="1.2.3", sha256=digest)
        with self.assertRaises(KiriMcpError) as caught:
            install.install_latest(mode="install")
        self.assertEqual(caught.exception.code, "unsafe_archive")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "escape.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.target, "Kiri.app")))

    def test_a_missing_app_in_the_archive_is_refused(self):
        path = os.path.join(self.served, "kiri.tar.gz")
        with tarfile.open(path, "w:gz") as archive:
            info = tarfile.TarInfo("readme.txt")
            data = b"nothing here"
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        with open(path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        self.write_feed(version="1.2.3", sha256=digest)
        with self.assertRaises(KiriMcpError) as caught:
            install.install_latest(mode="install")
        self.assertEqual(caught.exception.code, "install_failed")

    def test_the_staging_area_is_removed_after_a_failure(self):
        self.write_artifact()
        original = install.verify.verify_signature

        def refuse(app):
            raise KiriMcpError("signature_invalid", "the signature does not verify")

        install.verify.verify_signature = refuse
        try:
            with self.assertRaises(KiriMcpError):
                install.install_latest(mode="install")
        finally:
            install.verify.verify_signature = original
        self.assertFalse(os.path.exists(os.path.join(self.target, ".kiri-mcp-stage")))


@MAC_ONLY
class TestSetupDoesNotRepointTheUsersCommands(unittest.TestCase):
    """`kiri setup` links `kiri` and `docker` into ~/.kiri/bin.

    Those links point at the bundle the *running binary* lives in, not
    at `KIRI_APP_DIR`. An install that does not land in the usual place
    would therefore repoint the user's live commands at an app they do
    not use. The links are saved and put back in that case.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="kiri-mcp-setup-")
        self.old_env = dict(os.environ)
        self.state = os.path.join(self.dir, "state")
        os.environ["KIRI_STATE_DIR"] = self.state
        self.bin = os.path.join(self.state, "bin")
        os.makedirs(self.bin)
        self.app = os.path.join(self.dir, "Kiri.app")
        os.makedirs(os.path.join(self.app, "Contents", "MacOS"))
        self.binary = os.path.join(self.app, "Contents", "MacOS", "kiri")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old_env)
        shutil.rmtree(self.dir, ignore_errors=True)

    def write_fake_kiri(self):
        """A `kiri` that links the bin links the way the real setup does."""

        def fake(command, stdout=None, stderr=None, timeout=None):
            for name in ("kiri", "docker"):
                path = os.path.join(self.bin, name)
                try:
                    os.remove(path)
                except OSError:
                    pass
                os.symlink(os.path.join(self.app, "Contents", "MacOS", name), path)
            return type("Out", (), {"returncode": 0, "stdout": b"", "stderr": b""})()

        return fake

    def make_links(self):
        """Links that point at the app the user actually has."""
        keep = os.path.join(self.dir, "kept-app")
        os.makedirs(os.path.join(keep, "Contents", "MacOS"))
        for name in ("kiri", "docker"):
            target = os.path.join(keep, "Contents", "MacOS", name)
            with open(target, "w") as f:
                f.write("#!/bin/sh\n")
            os.chmod(target, 0o755)
            os.symlink(target, os.path.join(self.bin, name))

    def test_an_install_outside_the_usual_place_leaves_the_links_alone(self):
        self.make_links()
        original = install.subprocess.run
        install.subprocess.run = self.write_fake_kiri()
        try:
            install.run_setup(self.app)
        finally:
            install.subprocess.run = original
        for name in ("kiri", "docker"):
            path = os.path.join(self.bin, name)
            self.assertTrue(os.path.exists(path), "{0} was left dangling".format(name))
            self.assertIn("kept-app", os.readlink(path))

    def test_the_links_are_restored_even_when_the_setup_fails(self):
        self.make_links()
        original = install.subprocess.run

        def failing(command, stdout=None, stderr=None, timeout=None):
            raise OSError("cannot run it")

        install.subprocess.run = failing
        try:
            install.run_setup(self.app)
        finally:
            install.subprocess.run = original
        for name in ("kiri", "docker"):
            path = os.path.join(self.bin, name)
            self.assertTrue(os.path.exists(path), "{0} was left dangling".format(name))
            self.assertIn("kept-app", os.readlink(path))

    def test_an_install_in_the_usual_place_may_move_the_links(self):
        """A real install into /Applications should point at the new app."""
        self.make_links()
        original = install.subprocess.run
        install.subprocess.run = self.write_fake_kiri()
        original_is_canonical = install._is_canonical_app_dir
        install._is_canonical_app_dir = lambda folder: True
        try:
            install.run_setup(self.app)
        finally:
            install.subprocess.run = original
            install._is_canonical_app_dir = original_is_canonical
        for name in ("kiri", "docker"):
            path = os.path.join(self.bin, name)
            self.assertTrue(os.path.exists(path), "{0} was left dangling".format(name))


@MAC_ONLY
class TestUpgrade(unittest.TestCase):
    """The upgrade path, against a fake `kiri` that prints like the real one."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="kiri-mcp-upgrade-")
        self.old_env = dict(os.environ)
        self.fake = os.path.join(self.dir, "kiri")
        self.output = ""
        self.exit_code = 0

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old_env)
        shutil.rmtree(self.dir, ignore_errors=True)

    def write_fake(self, output, exit_code=0):
        """A `kiri` that prints `output` and exits with `exit_code`."""
        with open(self.fake, "w") as f:
            f.write(
                "#!/bin/sh\ncat <<'EOF'\n{0}\nEOF\nexit {1}\n".format(output, exit_code)
            )
        os.chmod(self.fake, 0o755)
        os.environ["KIRI_BIN"] = self.fake

    def test_already_up_to_date_is_a_result_not_an_error(self):
        self.write_fake("Kiri 0.2.1 is up to date")
        result = install.upgrade(dry_run=False)
        self.assertFalse(result["changed"])
        self.assertEqual(result["action"], "upgrade")
        self.assertEqual(result["target_version"], "0.2.1")

    def test_a_newer_version_is_reported(self):
        self.write_fake("Kiri 0.3.0 is available (this is 0.2.1): ## New\n- Something.")
        result = install.upgrade(dry_run=True)
        self.assertFalse(result["changed"], "a dry run changes nothing")
        self.assertEqual(result["target_version"], "0.3.0")

    def test_a_successful_update_reports_the_engine_restart(self):
        self.write_fake("updated to Kiri 0.3.0, and the engine runs it")
        result = install.upgrade(dry_run=False)
        self.assertTrue(result["changed"])
        self.assertTrue(result["engine_restarted"])
        self.assertTrue(any("engine restarted" in s for s in result["side_effects"]))

    def test_a_failure_surfaces_the_stderr(self):
        self.write_fake("kiri update: something went wrong", exit_code=1)
        with self.assertRaises(KiriMcpError) as caught:
            install.upgrade(dry_run=False)
        self.assertEqual(caught.exception.code, "install_failed")
        self.assertIn("something went wrong", caught.exception.message)

    def test_a_missing_binary_is_refused(self):
        os.environ["KIRI_BIN"] = os.path.join(self.dir, "not-here")
        with self.assertRaises(KiriMcpError) as caught:
            install.upgrade(dry_run=False)
        self.assertEqual(caught.exception.code, "kiri_not_found")


class TestFeedFetch(unittest.TestCase):
    """The feed is fetched with the User-Agent the CDN needs."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="kiri-mcp-feed-")
        self.server = LocalServer(self.dir)
        self.old_env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old_env)
        self.server.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_feed_is_read(self):
        with open(os.path.join(self.dir, "feed.json"), "w") as f:
            json.dump({"version": "9.9.9", "url": "https://example.invalid/k.tar.gz", "sha256": "ab"}, f)
        os.environ["KIRI_UPDATE_URL"] = self.server.base + "/feed.json"
        release = releases.latest()
        self.assertEqual(release.version, "9.9.9")

    def test_an_unreachable_feed_is_refused(self):
        os.environ["KIRI_UPDATE_URL"] = "http://127.0.0.1:1/feed.json"
        with self.assertRaises(KiriMcpError) as caught:
            releases.latest()
        self.assertIn(caught.exception.code, ("feed_unreachable", "timeout"))


if __name__ == "__main__":
    unittest.main()
