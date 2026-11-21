"""The checks: download, checksum, unpack, signature, notarization, chip.

The order matters and is the security property of the whole tool: the
checksum is checked on the *download*, before it is unpacked; the
signature is checked on the *unpacked app*, before it is moved; the
Info.plist is read only after the signature checks pass, so only a
verified file is ever parsed.

Nothing here writes outside the directory it is given.
"""
import hashlib
import http.client
import os
import plistlib
import socket
import subprocess
import tarfile
import urllib.error
import urllib.request

from . import config
from .errors import KiriMcpError

# The biggest download accepted: the app is about 65 MB packed.
MAX_DOWNLOAD = 2 << 30

# The Mach-O CPU types we know.
ARM64 = 0x0100000C
X86_64 = 0x01000007


def download(url, dest, expected_sha256):
    """Download an artifact and check its SHA-256 against the feed's.

    The digest is computed while the bytes stream to disk, so the file
    is never read twice and a partial download is never accepted. A
    failed download leaves no file behind.
    """
    from . import releases

    if not url.startswith("https://") and not releases.insecure_url_allowed(url):
        raise KiriMcpError("feed_insecure", "the artifact URL is not https: {0}".format(url))
    request = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT})
    digest = hashlib.sha256()
    written = 0
    try:
        with urllib.request.urlopen(request, timeout=config.timeout()) as response:
            with open(dest, "wb") as out:
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_DOWNLOAD:
                        raise KiriMcpError(
                            "download_failed",
                            "the artifact is larger than the {0} bytes this tool accepts".format(MAX_DOWNLOAD),
                        )
                    digest.update(chunk)
                    out.write(chunk)
    except urllib.error.HTTPError as e:
        _discard(dest)
        raise KiriMcpError("download_failed", "{0} answered HTTP {1}".format(url, e.code))
    except socket.timeout:
        _discard(dest)
        raise KiriMcpError("timeout", "the download did not finish in time ({0} bytes so far)".format(written))
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        _discard(dest)
        if isinstance(reason, socket.timeout):
            raise KiriMcpError("timeout", "the download did not finish in time ({0} bytes so far)".format(written))
        raise KiriMcpError("download_failed", "could not download {0}: {1}".format(url, reason))
    except http.client.HTTPException as e:
        # A connection dropped mid-transfer: neither a URLError nor an
        # OSError, so it would otherwise escape as an unknown failure.
        _discard(dest)
        raise KiriMcpError("download_failed", "the connection to {0} broke: {1}".format(url, e))
    except OSError as e:
        _discard(dest)
        raise KiriMcpError("download_failed", "could not download {0}: {1}".format(url, e))

    got = digest.hexdigest()
    if got.lower() != expected_sha256.lower():
        _discard(dest)
        raise KiriMcpError(
            "checksum_mismatch",
            "the download does not match the feed's checksum (expected {0}, got {1}); nothing was installed".format(
                expected_sha256.lower(), got
            ),
        )
    return got


def _discard(path):
    """Delete a file that must not be left behind."""
    try:
        os.unlink(path)
    except OSError:
        pass


def is_appledouble(name):
    """Whether a member is an AppleDouble sidecar.

    macOS puts a `._name` file next to every real file to carry the
    resource fork and the extended attributes. `bsdtar` leaves those
    alone, but Python's tarfile writes them out as ordinary files, and a
    bundle with stray files in it no longer matches its signature: the
    code signature covers exactly the files that were signed, so
    `codesign --verify` fails with "a sealed resource is missing or
    invalid". The sidecars carry nothing the app needs, so they are
    dropped, the way `tar -xzf` drops them.
    """
    return os.path.basename(name).startswith("._")


def safe_members(archive):
    """The archive's members, checked, or a refusal.

    A tarball is untrusted input: it can name a path outside the
    directory, or a symlink that points outside it. On this machine's
    bsdtar, `tar -xzf` really does write a `../` member outside the
    target, so the members are checked here before anything is
    extracted. Python 3.9's tarfile has no extraction filter either.

    Returns the members' names, in the order the archive lists them.
    AppleDouble sidecars are left out: they are not part of the signed
    bundle, and writing them would break the signature.
    """
    try:
        with tarfile.open(archive, "r:*") as handle:
            members = handle.getmembers()
    except (tarfile.TarError, OSError) as e:
        raise KiriMcpError("unsafe_archive", "the download is not a readable archive: {0}".format(e))
    names = []
    for member in members:
        name = member.name
        if not name:
            raise KiriMcpError("unsafe_archive", "the archive has a member with no name")
        if name.startswith("/") or name.startswith("\\") or os.path.isabs(name):
            raise KiriMcpError("unsafe_archive", "the archive has an absolute path: {0}".format(name))
        parts = name.replace("\\", "/").split("/")
        if ".." in parts:
            raise KiriMcpError("unsafe_archive", "the archive escapes the target directory: {0}".format(name))
        if member.issym() or member.islnk():
            raise KiriMcpError("unsafe_archive", "the archive has a link member: {0}".format(name))
        if not (member.isfile() or member.isdir()):
            raise KiriMcpError("unsafe_archive", "the archive has a member of an unexpected kind: {0}".format(name))
        if is_appledouble(name):
            continue
        names.append(name)
    return names


def extract(archive, dest):
    """Unpack an archive, but only the members `safe_members` approved."""
    names = safe_members(archive)
    os.makedirs(dest, exist_ok=True)
    root = os.path.realpath(dest)
    try:
        with tarfile.open(archive, "r:*") as handle:
            for name in names:
                member = handle.getmember(name)
                target = os.path.realpath(os.path.join(root, name))
                if target != root and not target.startswith(root + os.sep):
                    raise KiriMcpError("unsafe_archive", "the archive escapes the target directory: {0}".format(name))
                handle.extract(member, dest)
    except tarfile.TarError as e:
        raise KiriMcpError("unsafe_archive", "could not unpack the download: {0}".format(e))


def verify_signature(app):
    """The app's signature must verify, deeply and strictly."""
    try:
        out = subprocess.run(
            ["/usr/bin/codesign", "--verify", "--deep", "--strict", app],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        raise KiriMcpError("timeout", "codesign did not finish in time")
    except OSError as e:
        raise KiriMcpError("install_failed", "cannot run codesign: {0}".format(e))
    if out.returncode != 0:
        detail = out.stderr.decode("utf-8", "replace").strip() or "the signature does not verify"
        raise KiriMcpError("signature_invalid", "Kiri.app's signature does not verify: {0}".format(detail))


def verify_notarization(app):
    """macOS must accept the app as a notarized, executable."""
    try:
        out = subprocess.run(
            ["/usr/sbin/spctl", "-a", "-t", "exec", app],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        raise KiriMcpError("timeout", "spctl did not finish in time")
    except OSError as e:
        raise KiriMcpError("install_failed", "cannot run spctl: {0}".format(e))
    if out.returncode != 0:
        detail = out.stderr.decode("utf-8", "replace").strip() or "macOS does not accept it"
        raise KiriMcpError("not_notarized", "macOS does not accept Kiri.app (notarization): {0}".format(detail))


def _word(head, at, big_endian):
    """Four bytes at `at`, as an int."""
    part = head[at:at + 4]
    if len(part) != 4:
        return None
    if big_endian:
        return int.from_bytes(part, "big")
    return int.from_bytes(part, "little")


def has_cpu(head, want):
    """Whether a Mach-O header carries code for this CPU.

    A thin binary, or a universal one with a slice for it. This is the
    same check `kiri update` makes, so the tool accepts what Kiri
    accepts.
    """
    magic = _word(head, 0, True)
    if magic == 0xCFFAEDFE:
        # MH_MAGIC_64, little-endian on disk: the cputype follows.
        return _word(head, 4, False) == want
    if magic in (0xCAFEBABE, 0xCAFEBABF):
        # FAT_MAGIC / FAT_MAGIC_64: big-endian nfat_arch, then the
        # fat_arch entries, each starting with the cputype.
        step = 20 if magic == 0xCAFEBABE else 32
        count = _word(head, 4, True) or 0
        count = min(count, 64)
        for i in range(count):
            if _word(head, 8 + i * step, True) == want:
                return True
    return False


def verify_architecture(app, arch):
    """The app must carry code for this Mac's chip."""
    binary = config.app_binary(app)
    try:
        with open(binary, "rb") as f:
            head = f.read(4096)
    except OSError as e:
        raise KiriMcpError("wrong_architecture", "cannot read the app's binary: {0}".format(e))
    want = ARM64 if arch == "arm64" else X86_64
    if not has_cpu(head, want):
        raise KiriMcpError(
            "wrong_architecture",
            "the download is not built for this Mac ({0})".format(arch),
        )


def verify_version(app, promised):
    """The app must be the version the feed promised."""
    plist = os.path.join(app, "Contents", "Info.plist")
    try:
        with open(plist, "rb") as f:
            data = plistlib.load(f)
    except (OSError, ValueError, plistlib.InvalidFileException) as e:
        raise KiriMcpError("version_mismatch", "the app's Info.plist cannot be read: {0}".format(e))
    version = data.get("CFBundleShortVersionString")
    if not isinstance(version, str) or not version:
        raise KiriMcpError("version_mismatch", "the app's Info.plist has no CFBundleShortVersionString")
    expected = promised.strip()
    if expected.startswith("v"):
        expected = expected[1:]
    if version != expected:
        raise KiriMcpError(
            "version_mismatch",
            "the download is Kiri {0}, but the feed promised {1}".format(version, expected),
        )
    return version
