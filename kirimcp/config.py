"""Where the tool looks: the feed, the app folder, the `kiri` binary.

Everything here is a pure lookup, so it can be tested without a network
and without a Mac. The environment variables are the ones the rest of
Kiri already uses, so one override steers both paths.
"""
import os
import shutil

from . import __version__
from .errors import KiriMcpError

# The CDN answers 403 without a User-Agent, so every request carries one.
# The feed is public, so nothing secret is sent.
USER_AGENT = "kiri-mcp/{0}".format(__version__)

# Where the release feed and the artifacts live.
SITE = "https://download.runkiri.com"

# The biggest feed we read: 64 KiB, the same cap `kiri update` uses.
FEED_LIMIT = 64 << 10

# The app's name on disk.
APP_NAME = "Kiri.app"


def feed_urls(arch):
    """The feeds to try for this machine's chip, most specific first.

    Each kind of Mac has its own feed. The older names are kept as
    copies by the CDN and are read when the new one is missing, which is
    what the official installer does.
    """
    if arch == "arm64":
        return ["{0}/latest-mac-arm64.json".format(SITE), "{0}/latest.json".format(SITE)]
    return ["{0}/latest-mac-x86_64.json".format(SITE), "{0}/latest-x86_64.json".format(SITE)]


def feed_url(arch):
    """The feed this machine's release is announced in.

    `KIRI_UPDATE_URL` wins when it is set: it is the same variable
    `kiri update` reads, so one override steers both paths.
    """
    override = feed_override()
    if override:
        return override
    return feed_urls(arch)[0]


def feed_override():
    """The feed URL the environment names, or None.

    `KIRI_UPDATE_URL` is the same variable `kiri update` reads, so one
    override steers both the upgrade path and the fresh-install path.
    """
    value = os.environ.get("KIRI_UPDATE_URL", "").strip()
    return value or None


def app_dir():
    """Where a fresh install lands: `KIRI_APP_DIR`, else /Applications.

    Falls back to ~/Applications when /Applications is not writable, the
    way the official installer does.
    """
    override = os.environ.get("KIRI_APP_DIR", "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    if os.path.isdir("/Applications") and os.access("/Applications", os.W_OK):
        return "/Applications"
    home = os.path.expanduser("~")
    return os.path.join(home, "Applications")


def ensure_app_dir():
    """The app folder, created when it is not there yet."""
    folder = app_dir()
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as e:
        raise KiriMcpError("install_failed", "cannot use {0} for the install: {1}".format(folder, e))
    return folder


def home():
    """The user's home directory."""
    return os.path.expanduser("~")


def state_dir():
    """Kiri's folder in the home directory: ~/.kiri, or ~/.kiri-dev."""
    override = os.environ.get("KIRI_STATE_DIR", "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(home(), ".kiri")


def app_binary(app):
    """The `kiri` binary inside an app bundle."""
    return os.path.join(app, "Contents", "MacOS", "kiri")


def installed_app():
    """The installed Kiri.app, or None.

    Both /Applications and ~/Applications are checked, so the app is
    found wherever it was put.
    """
    candidates = []
    override = os.environ.get("KIRI_APP_DIR", "").strip()
    if override:
        candidates.append(os.path.abspath(os.path.expanduser(override)))
    else:
        candidates.append("/Applications")
        candidates.append(os.path.join(home(), "Applications"))
    for folder in candidates:
        path = os.path.join(folder, APP_NAME)
        if os.path.isdir(path):
            return path
    return None


def kiri_bin():
    """The `kiri` binary to run, or None.

    The resolution order matters: Cursor starts the server with whatever
    environment it has, so ~/.kiri/bin is often not on PATH. The
    installed app's own binary comes first, because it is the copy that
    matches the app.
    """
    override = os.environ.get("KIRI_BIN", "").strip()
    if override:
        return override if os.path.isfile(override) else None
    app = installed_app()
    if app:
        binary = app_binary(app)
        if os.path.isfile(binary):
            return binary
    linked = os.path.join(state_dir(), "bin", "kiri")
    if os.path.isfile(linked):
        return linked
    found = shutil.which("kiri")
    return found


def timeout():
    """Seconds for a network request."""
    value = os.environ.get("KIRI_MCP_TIMEOUT", "").strip()
    if not value:
        return 30
    try:
        seconds = int(value)
    except ValueError:
        return 30
    return seconds if seconds > 0 else 30


def engine_socket():
    """The engine's socket: ~/.kiri/run/docker.sock.

    `KIRI_SOCKET` moves it, the way `kiri daemon` does.
    """
    override = os.environ.get("KIRI_SOCKET", "").strip()
    if override:
        return override
    return os.path.join(state_dir(), "run", "docker.sock")
