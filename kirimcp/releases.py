"""The release feed: what is published, and what is installed here.

The feed is a small JSON document naming a version, an artifact URL and
its SHA-256. Nothing here is trusted: this module fetches and parses,
and the checks live in `verify`.
"""
import http.client
import json
import socket
import urllib.error
import urllib.request

from . import config, platform
from .errors import KiriMcpError

# The release notes are free text from the network. They are cut here, at
# the point they are read, so the cap holds whether they reach a model
# or a program.
NOTES_LIMIT = 4096


class Release(object):
    """What the feed says about the latest release."""

    __slots__ = ("version", "url", "sha256", "notes", "notes_url", "notes_truncated")

    def __init__(self, version, url, sha256, notes="", notes_url="", notes_truncated=False):
        self.version = version
        self.url = url
        self.sha256 = sha256
        # The cap is applied here, not at the point the notes are read,
        # so it holds for every Release whatever built it.
        self.notes, cut = truncate_notes(notes)
        self.notes_url = notes_url
        self.notes_truncated = bool(notes_truncated) or cut

    def as_dict(self):
        return {
            "version": self.version,
            "url": self.url,
            "sha256": self.sha256,
            "notes": self.notes,
            "notes_url": self.notes_url,
            "notes_truncated": self.notes_truncated,
        }


def newer(candidate, current):
    """Whether `candidate` is a newer version than `current`.

    Numeric, part by part: `1.2.10` is newer than `1.2.9`, which a
    string comparison gets backwards. A missing part counts as zero, and
    a leading `v` is ignored, the way `kiri update` compares.
    """
    def parts(version):
        cleaned = version.strip()
        if cleaned.startswith("v"):
            cleaned = cleaned[1:]
        out = []
        for piece in cleaned.replace("-", ".").split("."):
            try:
                out.append(int(piece))
            except ValueError:
                out.append(0)
        return out

    a, b = parts(candidate), parts(current)
    for i in range(max(len(a), len(b))):
        x = a[i] if i < len(a) else 0
        y = b[i] if i < len(b) else 0
        if x != y:
            return x > y
    return False


def truncate_notes(notes):
    """The release notes, cut to NOTES_LIMIT with a visible marker."""
    if notes is None:
        return "", False
    text = notes if isinstance(notes, str) else str(notes)
    if len(text) <= NOTES_LIMIT:
        return text, False
    return text[:NOTES_LIMIT] + "… (truncated)", True


def _fetch(url, limit):
    """GET a URL, with the User-Agent the CDN needs, capped at `limit`."""
    request = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=config.timeout()) as response:
            body = response.read(limit + 1)
            declared = response.headers.get("Content-Length")
    except urllib.error.HTTPError as e:
        raise KiriMcpError("feed_unreachable", "{0} answered HTTP {1}".format(url, e.code))
    except socket.timeout:
        raise KiriMcpError("timeout", "{0} did not answer in time".format(url))
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, socket.timeout):
            raise KiriMcpError("timeout", "{0} did not answer in time".format(url))
        raise KiriMcpError("feed_unreachable", "could not reach {0}: {1}".format(url, reason))
    except http.client.HTTPException as e:
        # A connection dropped mid-transfer: neither a URLError nor an
        # OSError, so it would otherwise escape as an unknown failure.
        raise KiriMcpError("feed_unreachable", "the connection to {0} broke: {1}".format(url, e))
    except OSError as e:
        raise KiriMcpError("feed_unreachable", "could not reach {0}: {1}".format(url, e))
    if len(body) > limit:
        raise KiriMcpError("feed_invalid", "{0} is larger than the {1} bytes a release feed may be".format(url, limit))
    if declared is not None:
        try:
            expected = int(declared)
        except ValueError:
            expected = None
        if expected is not None and len(body) < expected:
            raise KiriMcpError(
                "feed_invalid",
                "{0} sent {1} of the {2} bytes it said it would".format(url, len(body), expected),
            )
    return body


def insecure_url_allowed(url):
    """Whether this URL may be plain HTTP.

    A release must come over HTTPS. The one exception is a loopback
    address: the tests serve a feed from 127.0.0.1, and nothing on the
    network can reach a loopback address. A plain-HTTP URL to any other
    host is refused, whatever the feed says.
    """
    if not url.startswith("http://"):
        return False
    host = url[len("http://"):].split("/")[0].split("@")[-1]
    if host.startswith("["):
        host = host[1:].split("]")[0]
    else:
        host = host.split(":")[0]
    return host in ("127.0.0.1", "localhost", "::1")


def parse_feed(body, url):
    """The feed's JSON, read into a Release."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise KiriMcpError("feed_invalid", "{0} is not a valid release feed: {1}".format(url, e))
    if not isinstance(data, dict):
        raise KiriMcpError("feed_invalid", "{0} is not a release feed: it is not an object".format(url))
    version = data.get("version")
    artifact = data.get("url")
    digest = data.get("sha256")
    for name, value in (("version", version), ("url", artifact), ("sha256", digest)):
        if not isinstance(value, str) or not value.strip():
            raise KiriMcpError("feed_invalid", "{0} has no {1}".format(url, name))
    artifact = artifact.strip()
    if not artifact.startswith("https://") and not insecure_url_allowed(artifact):
        raise KiriMcpError("feed_insecure", "{0} names an artifact that is not https: {1}".format(url, artifact))
    notes, truncated = truncate_notes(data.get("notes", ""))
    notes_url = data.get("notes_url", "")
    if not isinstance(notes_url, str):
        notes_url = ""
    return Release(version.strip(), artifact, digest.strip().lower(), notes, notes_url.strip(), truncated)


def latest():
    """The latest release, from the feed for this machine."""
    override = config.feed_override()
    if override:
        urls = [override]
    else:
        urls = config.feed_urls(platform.architecture())
    last = None
    for url in urls:
        try:
            body = _fetch(url, config.FEED_LIMIT)
        except KiriMcpError as e:
            last = e
            continue
        return parse_feed(body, url)
    if last is not None:
        raise last
    raise KiriMcpError("feed_unreachable", "no release feed to read")


def installed():
    """What is installed here: the app, its version and its signing identity."""
    app = platform.installed_app()
    if not app:
        raise KiriMcpError("not_installed", "no Kiri is installed on this Mac")
    return {
        "version": platform.installed_version(),
        "path": app,
        "binary": config.app_binary(app),
        "signing_identity": platform.signing_identity(app),
    }


def status():
    """The whole read-only picture: here, and what is published."""
    from . import linux

    if linux.is_linux():
        return linux.status()
    arch = platform.architecture()
    release = latest()
    app = platform.installed_app()
    here = platform.installed_version()
    update_available = bool(here) and newer(release.version, here)
    return {
        "platform": platform.platform_summary(),
        "architecture": arch,
        "installed": {
            "version": here,
            "path": app,
            "binary": config.app_binary(app) if app else None,
            "signing_identity": platform.signing_identity(app) if app else None,
        },
        "latest": release.as_dict(),
        "update_available": update_available,
        "feed_url": config.feed_override() or config.feed_url(arch),
        "engine_running": platform.engine_running(),
    }
