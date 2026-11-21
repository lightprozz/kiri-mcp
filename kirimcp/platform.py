"""What machine this is: the chip, the macOS version, the app, the engine.

The chip is not `uname -m`: a shell running under Rosetta says x86_64 on
an Apple silicon Mac, so `sysctl -n hw.optional.arm64` is the source of
truth, exactly as the official installer uses it.
"""
import os
import plistlib
import platform as pyplatform
import socket
import subprocess

from . import config
from .errors import KiriMcpError

# macOS 14 is the oldest this tool supports, matching the installer.
MIN_MACOS_MAJOR = 14


def is_macos():
    """Whether this is a Mac."""
    return pyplatform.system() == "Darwin" or sys_platform() == "darwin"


def sys_platform():
    """The raw platform string, `darwin` or something else."""
    return os.uname().sysname.lower() if hasattr(os, "uname") else ""


def architecture():
    """This machine's chip: `arm64` or `x86_64`.

    `sysctl -n hw.optional.arm64` answers 1 on Apple silicon even under
    Rosetta, so it is the source of truth, exactly as the official
    installer uses it.

    When `sysctl` cannot answer, `uname -m` is used only when it says
    `arm64`: that is never wrong, on any Mac. When it says `x86_64`
    the answer is ambiguous — it is a real Intel Mac, or Apple silicon
    under Rosetta — and guessing there would install the wrong app, so
    it is an error instead.
    """
    if not is_macos():
        raise KiriMcpError("unsupported_platform", "this tool installs Kiri on macOS only")
    try:
        out = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "hw.optional.arm64"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return _architecture_without_sysctl()
    if out.returncode != 0:
        return _architecture_without_sysctl()
    answer = out.stdout.decode("utf-8", "replace").strip()
    if answer == "1":
        return "arm64"
    if answer == "0":
        return "x86_64"
    raise KiriMcpError("unsupported_platform", "cannot read this Mac's chip: sysctl answered {0!r}".format(answer))


def _architecture_without_sysctl():
    """The chip, when `sysctl` cannot answer it."""
    machine = pyplatform.machine().lower()
    if machine in ("arm64", "aarch64"):
        return "arm64"
    raise KiriMcpError(
        "unsupported_platform",
        "cannot read this Mac's chip: sysctl did not answer, and uname reports {0!r}, "
        "which is ambiguous on Apple silicon under Rosetta".format(machine),
    )


def macos_version():
    """This Mac's macOS version, as a string."""
    try:
        out = subprocess.run(
            ["/usr/bin/sw_vers", "-productVersion"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        raise KiriMcpError("unsupported_os", "cannot read this Mac's version: sw_vers did not answer")
    if out.returncode != 0:
        raise KiriMcpError("unsupported_os", "cannot read this Mac's version: sw_vers did not answer")
    return out.stdout.decode("utf-8", "replace").strip()


def macos_major():
    """This Mac's macOS major version, as an int."""
    version = macos_version()
    try:
        return int(version.split(".")[0])
    except (IndexError, ValueError):
        raise KiriMcpError("unsupported_os", "cannot read this Mac's version: sw_vers answered {0!r}".format(version))


def check_os():
    """Make sure this Mac can run Kiri, and say why if it cannot."""
    if not is_macos():
        raise KiriMcpError("unsupported_platform", "this tool installs Kiri on macOS only")
    major = macos_major()
    if major < MIN_MACOS_MAJOR:
        raise KiriMcpError(
            "unsupported_os",
            "Kiri needs macOS {0} or later; this Mac has {1}".format(MIN_MACOS_MAJOR, macos_version()),
        )
    return major


def installed_app():
    """The installed Kiri.app, or None."""
    return config.installed_app()


def bundle_version(app):
    """CFBundleShortVersionString from an app bundle's Info.plist."""
    plist = os.path.join(app, "Contents", "Info.plist")
    try:
        with open(plist, "rb") as f:
            data = plistlib.load(f)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    value = data.get("CFBundleShortVersionString")
    return value if isinstance(value, str) and value else None


def installed_version():
    """The installed Kiri's version, or None when there is no app."""
    app = installed_app()
    if not app:
        return None
    return bundle_version(app)


def signing_identity(app):
    """The app's signing requirement, as `codesign -d -r -` prints it.

    Reported, never trusted on its own here: the upgrade path reads the
    same requirement from the installed app and pins the update to it.
    """
    try:
        out = subprocess.run(
            ["/usr/bin/codesign", "-d", "-r", "-", app],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = out.stdout.decode("utf-8", "replace")
    for line in text.splitlines():
        if line.startswith("designated => "):
            requirement = line[len("designated => "):].strip()
            if requirement:
                return requirement
    return None


def engine_running():
    """Whether the engine's socket answers, without raising."""
    path = config.engine_socket()
    if not path or not os.path.exists(path):
        return False
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(2)
    try:
        sock.connect(path)
        return True
    except OSError:
        return False
    finally:
        sock.close()


def platform_summary():
    """One line describing the machine, for the tool's result."""
    if not is_macos():
        return "{0} ({1})".format(pyplatform.system(), pyplatform.machine())
    return "macOS {0} ({1})".format(macos_version(), architecture())
