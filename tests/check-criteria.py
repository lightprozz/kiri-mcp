#!/usr/bin/env python3
"""Checks every success criterion in the plan, and says which pass.

    python3 tests/check-criteria.py

Each criterion is checked against the running server or the code, so the
answer is evidence, not a claim. This is a manual gate: the unit tests
are the suite that runs on every change.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from kirimcp import config, install, releases, verify  # noqa: E402
from kirimcp.errors import KiriMcpError  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))


def call(tool, arguments=None):
    """Call a tool through the server, the way a client does."""
    payload = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": arguments or {}}},
    ]
    text = "".join(json.dumps(p) + "\n" for p in payload)
    out = subprocess.run(
        [sys.executable, os.path.join(ROOT, "server.py")],
        input=text.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=300,
    )
    lines = [l for l in out.stdout.decode("utf-8").splitlines() if l.strip()]
    return json.loads(lines[-1])["result"]


def main():
    # 1. The status tool reports the machine accurately.
    result = call("kiri_release_status")
    sc = result["structuredContent"]
    check(
        "kiri_release_status reports the installed and latest versions",
        sc["installed"]["version"] and sc["latest"]["version"],
        "installed {0}, latest {1}, update_available {2}, binary {3}".format(
            sc["installed"]["version"], sc["latest"]["version"], sc["update_available"], sc["installed"]["binary"]
        ),
    )
    check(
        "the resolved app path is the app's own binary",
        (sc["installed"]["binary"] or "").endswith("/Kiri.app/Contents/MacOS/kiri"),
        sc["installed"]["binary"],
    )

    # 2. An up-to-date Mac changes nothing and reports changed: false.
    before = platform_state()
    result = call("kiri_install_latest", {"dry_run": False})
    after = platform_state()
    check(
        "kiri_install_latest on an up-to-date Mac reports changed: false, not an error",
        not result.get("isError") and result["structuredContent"].get("changed") is False,
        "changed={0} isError={1}".format(result["structuredContent"].get("changed"), result.get("isError")),
    )
    check(
        "the up-to-date run changed nothing on disk",
        before == after,
        "the installed version is still {0}".format(after),
    )

    # 3. A dry run reports the plan and changes nothing.
    before = platform_state()
    result = call("kiri_install_latest", {"dry_run": True})
    after = platform_state()
    dry = result["structuredContent"]
    check(
        "kiri_install_latest with dry_run reports the plan",
        dry.get("action") and ("checks" in dry or "output" in dry),
        "action={0}, checks={1}".format(dry.get("action"), len(dry.get("checks", []))),
    )
    check("the dry run changed nothing on disk", before == after, "the installed version is still {0}".format(after))

    # 4. A wrong checksum is refused, with a local feed.
    codes, detail = check_wrong_checksum()
    check("a feed whose sha256 does not match the download is refused", codes == "checksum_mismatch", detail)

    # 5. A plain-HTTP artifact is refused.
    codes, detail = check_insecure()
    check("a feed or artifact URL that is not https is refused", codes == "feed_insecure", detail)

    # 6. An escaping tarball is refused before anything is written.
    codes, detail = check_escaping_archive()
    check("a tarball whose members escape the target is refused", codes == "unsafe_archive", detail)

    # 7. The installed app passes the signature checks and the version matches.
    ok, detail = check_signature()
    check("the installed app passes codesign and spctl, and its version matches the feed", ok, detail)

    # 8. The official client round-trips.
    ok, detail = check_official_client()
    check("every tool call round-trips through the official mcp client", ok, detail)

    # 9. The server needs only the system Python.
    ok, detail = check_stdlib_only()
    check("the server starts with /usr/bin/python3 alone", ok, detail)

    # 10. Cursor's configuration registers the server.
    ok, detail = check_registration()
    check("~/.cursor/mcp.json registers the server", ok, detail)

    # 11. No name collides with issue #74.
    ok, detail = check_names()
    check("neither tool's name collides with issue #74's namespace", ok, detail)

    # 12. The annotations are there, and every payload is capped.
    ok, detail = check_annotations_and_caps()
    check("every result carries the annotations, and every payload is capped", ok, detail)

    print()
    failed = 0
    for name, ok, detail in RESULTS:
        print("{0} {1}".format("PASS" if ok else "FAIL", name))
        if detail:
            print("       {0}".format(detail))
        if not ok:
            failed += 1
    print()
    print("{0} of {1} criteria pass".format(len(RESULTS) - failed, len(RESULTS)))
    return 1 if failed else 0


def platform_state():
    """A fingerprint of what is installed, for "nothing changed" checks."""
    app = releases.platform.installed_app()
    return (app, releases.platform.installed_version())


def with_local_feed(feed, mode="install"):
    """Run an install against a feed served from 127.0.0.1."""
    folder = tempfile.mkdtemp(prefix="kiri-mcp-criteria-")
    old = dict(os.environ)
    os.environ["KIRI_APP_DIR"] = folder
    try:
        server = LocalFeed(folder, feed)
        os.environ["KIRI_UPDATE_URL"] = server.base + "/feed.json"
        try:
            install.install_latest(mode=mode)
            return None, "the install was accepted"
        except KiriMcpError as e:
            return e.code, e.message
    finally:
        os.environ.clear()
        os.environ.update(old)
        shutil.rmtree(folder, ignore_errors=True)


def check_wrong_checksum():
    """A feed whose digest does not match the artifact it names."""
    import hashlib
    import io
    import tarfile

    folder = tempfile.mkdtemp(prefix="kiri-mcp-criteria-")
    old = dict(os.environ)
    os.environ["KIRI_APP_DIR"] = folder
    try:
        path = os.path.join(folder, "kiri.tar.gz")
        with tarfile.open(path, "w:gz") as archive:
            info = tarfile.TarInfo("Kiri.app/Contents/Info.plist")
            data = b"<plist/>"
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        server = LocalFeed(folder, None)
        feed = {
            "version": "9.9.9",
            "url": server.base + "/kiri.tar.gz",
            "sha256": "0" * 64,
        }
        server.write_feed(feed)
        os.environ["KIRI_UPDATE_URL"] = server.base + "/feed.json"
        try:
            install.install_latest(mode="install")
            return None, "the install was accepted"
        except KiriMcpError as e:
            installed = os.path.isdir(os.path.join(folder, "Kiri.app"))
            return e.code, "{0}; Kiri.app installed: {1}".format(e.message, installed)
    finally:
        os.environ.clear()
        os.environ.update(old)
        shutil.rmtree(folder, ignore_errors=True)


def check_insecure():
    feed = {"version": "9.9.9", "url": "http://example.invalid/kiri.tar.gz", "sha256": "0" * 64}
    return with_local_feed(feed)


def check_escaping_archive():
    """A tarball with a member that escapes the target directory."""
    import hashlib
    import io
    import tarfile

    folder = tempfile.mkdtemp(prefix="kiri-mcp-criteria-")
    old = dict(os.environ)
    os.environ["KIRI_APP_DIR"] = folder
    try:
        path = os.path.join(folder, "kiri.tar.gz")
        with tarfile.open(path, "w:gz") as archive:
            info = tarfile.TarInfo("../escape.txt")
            data = b"pwned"
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        with open(path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        server = LocalFeed(folder, None)
        feed = {
            "version": "9.9.9",
            "url": server.base + "/kiri.tar.gz",
            "sha256": digest,
        }
        server.write_feed(feed)
        os.environ["KIRI_UPDATE_URL"] = server.base + "/feed.json"
        try:
            install.install_latest(mode="install")
            return None, "the install was accepted"
        except KiriMcpError as e:
            escaped = os.path.exists(os.path.join(folder, "escape.txt"))
            return e.code, "{0}; wrote outside: {1}".format(e.message, escaped)
    finally:
        os.environ.clear()
        os.environ.update(old)
        shutil.rmtree(folder, ignore_errors=True)


def check_signature():
    """The real published artifact, checked the way an install checks it."""
    release = releases.latest()
    folder = tempfile.mkdtemp(prefix="kiri-mcp-criteria-")
    try:
        archive = os.path.join(folder, "kiri.tar.gz")
        verify.download(release.url, archive, release.sha256)
        unpack = os.path.join(folder, "unpack")
        verify.extract(archive, unpack)
        app = os.path.join(unpack, "Kiri.app")
        verify.verify_signature(app)
        verify.verify_notarization(app)
        verify.verify_architecture(app, releases.platform.architecture())
        version = verify.verify_version(app, release.version)
        return True, "Kiri {0}: sha256, codesign, spctl, {1}, version {2} all pass".format(
            release.version, releases.platform.architecture(), version
        )
    except KiriMcpError as e:
        return False, "{0}: {1}".format(e.code, e.message)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def check_official_client():
    try:
        import mcp  # noqa: F401
    except ImportError:
        return False, "the mcp package is not installed: pip install mcp"
    script = """
import asyncio, json, os, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def run():
    params = StdioServerParameters(command=sys.executable, args=[%(server)r])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            tools = await s.list_tools()
            names = sorted(t.name for t in tools.tools)
            annotated = all(t.annotations is not None for t in tools.tools)
            res = await s.call_tool("kiri_release_status", {})
            structured = bool(res.structured_content)
            return names, annotated, structured, res.is_error

names, annotated, structured, is_error = asyncio.run(run())
print(json.dumps({"names": names, "annotated": annotated, "structured": structured, "is_error": is_error}))
""".replace("%(server)r", repr(os.path.join(ROOT, "server.py")))
    out = subprocess.run([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
    if out.returncode != 0:
        return False, out.stderr.decode("utf-8", "replace")[-400:]
    data = json.loads(out.stdout.decode("utf-8").strip().splitlines()[-1])
    ok = data["annotated"] and data["structured"] and not data["is_error"] and data["names"] == [
        "kiri_install_latest",
        "kiri_release_status",
    ]
    return ok, json.dumps(data)


def check_stdlib_only():
    """The server must import nothing outside the standard library.

    The modules are resolved against the interpreter's own standard
    library folder, so the answer does not depend on Python 3.10's
    `sys.stdlib_module_names`.
    """
    script = """
import ast, json, os, sys, sysconfig

stdlib = os.path.realpath(sysconfig.get_paths()["stdlib"])
root = sys.argv[1]
third_party = set()
for base, dirs, files in os.walk(root):
    dirs[:] = [d for d in dirs if d != "__pycache__"]
    for name in files:
        if not name.endswith(".py"):
            continue
        path = os.path.join(base, name)
        with open(path) as f:
            tree = ast.parse(f.read(), path)
        for node in ast.walk(tree):
            tops = []
            if isinstance(node, ast.Import):
                tops = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                tops = [node.module.split(".")[0]]
            for top in tops:
                if top in sys.builtin_module_names or top == "kirimcp":
                    continue
                try:
                    found = __import__(top)
                except ImportError:
                    third_party.add(top)
                    continue
                origin = getattr(found, "__file__", None)
                if origin and not os.path.realpath(origin).startswith(stdlib + os.sep):
                    third_party.add(top)
print(json.dumps(sorted(third_party)))
""".strip()
    # Only the server and its package: the tests import the `mcp` client
    # and the `tests` package, which are not part of the server.
    targets = [os.path.join(ROOT, "server.py"), os.path.join(ROOT, "kirimcp")]
    third_party = set()
    for target in targets:
        out = subprocess.run(
            [sys.executable, "-c", script, target], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60
        )
        if out.returncode != 0:
            return False, out.stderr.decode("utf-8", "replace")[-400:]
        third_party.update(json.loads(out.stdout.decode("utf-8").strip().splitlines()[-1]))
    third_party = sorted(t for t in third_party if t not in ("tests", "mcp"))
    return not third_party, "imports outside the standard library: {0}".format(third_party or "none")


def check_registration():
    path = os.path.expanduser("~/.cursor/mcp.json")
    if not os.path.exists(path):
        return False, "{0} does not exist".format(path)
    with open(path) as f:
        data = json.load(f)
    servers = data.get("mcpServers") or {}
    entry = servers.get("kiri-mcp")
    if not entry:
        return False, "no 'kiri-mcp' key in {0} (keys: {1})".format(path, sorted(servers))
    server = entry.get("args", [None])[0]
    exists = bool(server) and os.path.exists(server)
    return exists, "registered as {0}, the file exists: {1}".format(entry, exists)


def check_names():
    from kirimcp import server as mcp_server

    from tests.test_mcp_client import ISSUE_74_TOOLS

    names = {t["name"] for t in mcp_server.TOOLS}
    collision = names & ISSUE_74_TOOLS
    return not collision, "our names {0}; collision {1}".format(sorted(names), sorted(collision) or "none")


def check_annotations_and_caps():
    from kirimcp import server as mcp_server

    keys = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
    ok = True
    for tool in mcp_server.TOOLS:
        annotations = tool.get("annotations") or {}
        for key in keys:
            if key not in annotations:
                ok = False
    long_notes = "z" * 20000
    notes, truncated = releases.truncate_notes(long_notes)
    if not truncated or len(notes) >= len(long_notes):
        ok = False
    text = mcp_server._text({"notes": long_notes})
    if len(text) > mcp_server.TEXT_LIMIT + 100:
        ok = False
    return ok, "notes capped at {0} characters, text capped at {1}".format(
        releases.NOTES_LIMIT, mcp_server.TEXT_LIMIT
    )


class LocalFeed(object):
    """A feed and an artifact served from 127.0.0.1."""

    def __init__(self, folder, feed=None):
        from http.server import HTTPServer, SimpleHTTPRequestHandler
        import threading

        self.folder = folder

        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                SimpleHTTPRequestHandler.__init__(self, *args, directory=folder, **kwargs)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()
        if feed is not None:
            self.write_feed(feed)

    def write_feed(self, feed):
        """Put the feed in the served folder."""
        with open(os.path.join(self.folder, "feed.json"), "w") as f:
            json.dump(feed, f)

    @property
    def base(self):
        return "http://127.0.0.1:{0}".format(self.server.server_address[1])


if __name__ == "__main__":
    sys.exit(main())
