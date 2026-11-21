"""Kiri on Linux: the .deb and .rpm packages, installed through the system's package manager.

Linux has no release feed of its own (`latest.json` is the Mac arm64 feed's
old name), and `kiri update` on Linux refuses to update itself. What a
release does publish for Linux is `SHA256SUMS-linux` next to its packages on
download.runkiri.com, and `SHA256SUMS` on the GitHub release. The packages
are not signed, so the SHA-256 is the anchor: the digest must match in both
lists, which live on two different hosts, before the download is accepted.

The order holds the same property as on the Mac: the checksum is checked on
the download, the package's own metadata (name, version, architecture) is
read only after that, and nothing is installed until every check passes.
"""
import getpass
import os
import re
import shutil
import subprocess
import tempfile

from . import config, releases, verify
from .errors import KiriMcpError

SUMS_URL = "{0}/SHA256SUMS-linux".format(config.SITE)
GITHUB_SUMS_URL = "https://github.com/libracoder/kiri/releases/download/v{version}/SHA256SUMS"
NOTES_FEED_URL = "{0}/latest.json".format(config.SITE)
SUMS_LIMIT = 64 << 10

FLAVORS = ("kiri", "kiri-lite")
ENGINE_SOCKET = "/run/kiri/docker.sock"
INSTALL_TIMEOUT = 900

DEB_RE = re.compile(r"^(kiri|kiri-lite)_([0-9][0-9A-Za-z.+~]*)_(amd64|arm64)\.deb$")
RPM_RE = re.compile(r"^(kiri|kiri-lite)-([0-9][0-9A-Za-z.+~]*)-([0-9A-Za-z.]+)\.(x86_64|aarch64)\.rpm$")

FRESH_SIDE_EFFECTS = [
    "The `kiri` system group, which owns /run/kiri/docker.sock (members can run containers as root).",
    "kiri.service, enabled and started by `kiri host-access`.",
    "Your user added to the `kiri` group (takes effect in new login sessions).",
]


def is_linux():
    return hasattr(os, "uname") and os.uname().sysname == "Linux"


def os_release():
    """/etc/os-release as a dict."""
    data = {}
    for path in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            with open(path) as f:
                for line in f:
                    key, sep, value = line.strip().partition("=")
                    if sep:
                        data[key] = value.strip().strip('"')
            return data
        except OSError:
            continue
    return data


def package_format():
    """`deb` or `rpm`, from the distro and the tools it has."""
    info = os_release()
    family = " ".join([info.get("ID", ""), info.get("ID_LIKE", "")]).lower().split()
    has_dpkg = shutil.which("dpkg-query") and shutil.which("apt-get")
    has_rpm = shutil.which("rpm")
    if has_dpkg and (set(family) & {"debian", "ubuntu"} or not has_rpm):
        return "deb"
    if has_rpm:
        return "rpm"
    raise KiriMcpError("unsupported_os", "Kiri ships .deb and .rpm packages; this Linux has neither apt nor rpm")


def architecture():
    """`x86_64` or `arm64`."""
    machine = os.uname().machine.lower()
    if machine in ("x86_64", "amd64"):
        return "x86_64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    raise KiriMcpError("unsupported_platform", "Kiri ships for x86_64 and arm64 Linux, not {0}".format(machine))


def package_arch(fmt, arch):
    if fmt == "deb":
        return "amd64" if arch == "x86_64" else "arm64"
    return "x86_64" if arch == "x86_64" else "aarch64"


def glibc_version():
    try:
        value = os.confstr("CS_GNU_LIBC_VERSION") or ""
    except (ValueError, OSError):
        return None
    parts = value.split()
    return parts[1] if len(parts) == 2 and parts[0] == "glibc" else None


def summary():
    info = os_release()
    return "{0} ({1})".format(info.get("PRETTY_NAME") or "Linux", os.uname().machine)


def _run(command, timeout, env=None):
    try:
        return subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise KiriMcpError("timeout", "{0} did not finish in time".format(command[0]))
    except OSError as e:
        raise KiriMcpError("install_failed", "cannot run {0}: {1}".format(command[0], e))


def _text(out):
    return (out.stdout + out.stderr).decode("utf-8", "replace").strip()


def installed(fmt=None):
    """The installed Kiri package: {flavor, version}, or None."""
    fmt = fmt or package_format()
    for flavor in FLAVORS:
        if fmt == "deb":
            out = _run(["dpkg-query", "-W", "-f", "${Status}\t${Version}", flavor], 30)
            if out.returncode == 0:
                status, _, version = out.stdout.decode("utf-8", "replace").partition("\t")
                if status.strip().endswith(" installed") and version.strip():
                    return {"flavor": flavor, "version": version.strip()}
        else:
            out = _run(["rpm", "-q", "--qf", "%{VERSION}", flavor], 30)
            if out.returncode == 0 and out.stdout.strip():
                return {"flavor": flavor, "version": out.stdout.decode("utf-8", "replace").strip()}
    return None


def sums_url():
    return os.environ.get("KIRI_SUMS_URL", "").strip() or SUMS_URL


def github_sums_url(version):
    template = os.environ.get("KIRI_GITHUB_SUMS_URL", "").strip() or GITHUB_SUMS_URL
    return template.format(version=version)


def parse_sums(body, url):
    """`<sha256>  <file>` lines, as {file: sha256}."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise KiriMcpError("feed_invalid", "{0} is not text".format(url))
    sums = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        digest, name = parts[0].lower(), parts[1].lstrip("*")
        if re.match(r"^[0-9a-f]{64}$", digest) and "/" not in name:
            sums[name] = digest
    if not sums:
        raise KiriMcpError("feed_invalid", "{0} lists no checksums".format(url))
    return sums


def packages(sums):
    """The Linux packages a checksum list names: [(fmt, flavor, version, arch, file)]."""
    found = []
    for name in sums:
        m = DEB_RE.match(name)
        if m:
            found.append(("deb", m.group(1), m.group(2), m.group(3), name))
            continue
        m = RPM_RE.match(name)
        if m:
            found.append(("rpm", m.group(1), m.group(2), m.group(4), name))
    return found


def _base(url):
    return url.rsplit("/", 1)[0]


def _notes_for(version):
    """Release notes for `version`, from the shared feed when it names the same version."""
    try:
        release = releases.parse_feed(releases._fetch(NOTES_FEED_URL, config.FEED_LIMIT), NOTES_FEED_URL)
    except KiriMcpError:
        return "", "", False
    if release.version != version:
        return "", "", False
    return release.notes, release.notes_url, release.notes_truncated


def latest(fmt, arch, flavor):
    """The latest release of `flavor` for this machine, from SHA256SUMS-linux."""
    url = sums_url()
    sums = parse_sums(releases._fetch(url, SUMS_LIMIT), url)
    want_arch = package_arch(fmt, arch)
    matches = [p for p in packages(sums) if p[0] == fmt and p[1] == flavor and p[3] == want_arch]
    if not matches:
        raise KiriMcpError("feed_invalid", "{0} has no {1} .{2} for {3}".format(url, flavor, fmt, want_arch))
    newest = matches[0]
    for p in matches[1:]:
        if releases.newer(p[2], newest[2]):
            newest = p
    _, _, version, _, name = newest
    artifact = "{0}/{1}".format(_base(url), name)
    if not artifact.startswith("https://") and not releases.insecure_url_allowed(artifact):
        raise KiriMcpError("feed_insecure", "the package URL is not https: {0}".format(artifact))
    notes, notes_url, cut = _notes_for(version) if not os.environ.get("KIRI_SUMS_URL") else ("", "", False)
    release = releases.Release(version, artifact, sums[name], notes, notes_url, cut)
    return release, name


def cross_check(release, name):
    """The digest must be the same in the GitHub release's SHA256SUMS."""
    url = github_sums_url(release.version)
    sums = parse_sums(releases._fetch(url, SUMS_LIMIT), url)
    other = sums.get(name)
    if other is None:
        raise KiriMcpError("checksum_mismatch", "{0} does not list {1}; nothing was installed".format(url, name))
    if other != release.sha256:
        raise KiriMcpError(
            "checksum_mismatch",
            "download.runkiri.com and GitHub disagree on {0}'s SHA-256 ({1} vs {2}); nothing was installed".format(
                name, release.sha256, other
            ),
        )
    return url


def package_metadata(fmt, path):
    """(name, version, arch) read from the package file itself."""
    if fmt == "deb":
        out = _run(["dpkg-deb", "-f", path, "Package", "Version", "Architecture"], 60)
        fields = {}
        for line in out.stdout.decode("utf-8", "replace").splitlines():
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
        if out.returncode != 0:
            raise KiriMcpError("unsafe_archive", "the download is not a readable .deb: {0}".format(_text(out)))
        return fields.get("Package"), fields.get("Version"), fields.get("Architecture")
    out = _run(["rpm", "-qp", "--nosignature", "--qf", "%{NAME}\t%{VERSION}\t%{ARCH}", path], 60)
    if out.returncode != 0:
        raise KiriMcpError("unsafe_archive", "the download is not a readable .rpm: {0}".format(_text(out)))
    parts = out.stdout.decode("utf-8", "replace").split("\t")
    return tuple(parts) if len(parts) == 3 else (None, None, None)


def required_glibc(fmt, path):
    """The newest glibc the package asks for, or None."""
    if fmt == "deb":
        out = _run(["dpkg-deb", "-f", path, "Depends"], 60)
        m = re.search(r"\blibc6 \(>= ([0-9.]+)\)", out.stdout.decode("utf-8", "replace"))
        return m.group(1) if m else None
    out = _run(["rpm", "-qp", "--nosignature", "--requires", path], 60)
    versions = re.findall(r"libc\.so\.6\(GLIBC_([0-9.]+)\)", out.stdout.decode("utf-8", "replace"))
    newest = None
    for v in versions:
        if newest is None or releases.newer(v, newest):
            newest = v
    return newest


def glibc_ok(fmt, path):
    need = required_glibc(fmt, path)
    have = glibc_version()
    if not need or not have:
        return True, need, have
    return not releases.newer(need, have), need, have


def root_prefix():
    """[] as root, ["sudo", "-n"] when sudo works without a password."""
    if os.geteuid() == 0:
        return []
    if shutil.which("sudo") and _run(["sudo", "-n", "true"], 30).returncode == 0:
        return ["sudo", "-n"]
    return None


def install_command(fmt, path):
    if fmt == "deb":
        return ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", "--no-install-recommends", path]
    for tool, args in (("dnf", ["install", "-y"]), ("yum", ["install", "-y"]),
                       ("zypper", ["--non-interactive", "install", "--allow-unsigned-rpm"])):
        if shutil.which(tool):
            return [tool] + args + [path]
    return ["rpm", "-U", "--replacepkgs", path]


def engine_socket():
    return os.environ.get("KIRI_SOCKET", "").strip() or ENGINE_SOCKET


def engine_running():
    import socket

    path = engine_socket()
    if not os.path.exists(path):
        return False
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(2)
    try:
        sock.connect(path)
        return True
    except OSError:
        # Not in the kiri group yet: the socket exists but refuses us. Ask systemd.
        out = _run(["systemctl", "is-active", "-q", "kiri.service"], 10) if shutil.which("systemctl") else None
        return bool(out and out.returncode == 0)
    finally:
        sock.close()


def _flavor(here):
    if here:
        return here["flavor"], "installed"
    chosen = os.environ.get("KIRI_FLAVOR", "").strip()
    if chosen:
        if chosen not in FLAVORS:
            raise KiriMcpError("mode_conflict", "KIRI_FLAVOR must be kiri or kiri-lite, not {0!r}".format(chosen))
        return chosen, "KIRI_FLAVOR"
    return "kiri", "default"


def status():
    fmt, arch = package_format(), architecture()
    here = installed(fmt)
    flavor, _ = _flavor(here)
    release, name = latest(fmt, arch, flavor)
    return {
        "platform": summary(),
        "architecture": arch,
        "package_format": fmt,
        "glibc": glibc_version(),
        "installed": {
            "version": here["version"] if here else None,
            "flavor": here["flavor"] if here else None,
            "path": shutil.which("kiri") if here else None,
        },
        "latest": dict(release.as_dict(), flavor=flavor, package=name),
        "update_available": bool(here) and releases.newer(release.version, here["version"]),
        "feed_url": sums_url(),
        "engine_running": engine_running(),
    }


def install_latest(mode="auto", dry_run=False):
    if mode not in ("auto", "upgrade", "install"):
        raise KiriMcpError("mode_conflict", "unknown mode {0!r}: use auto, upgrade or install".format(mode))
    fmt, arch = package_format(), architecture()
    here = installed(fmt)
    if mode == "auto":
        mode = "upgrade" if here else "install"
    elif mode == "upgrade" and not here:
        raise KiriMcpError("mode_conflict", "there is no Kiri on this machine to upgrade")
    elif mode == "install" and here:
        raise KiriMcpError("mode_conflict", "Kiri is already installed here; use the upgrade mode")
    flavor, why = _flavor(here)
    release, name = latest(fmt, arch, flavor)
    before = here["version"] if here else None
    engine_was_running = engine_running()
    plan = {
        "action": mode,
        "dry_run": bool(dry_run),
        "changed": False,
        "before": before,
        "after": before,
        "target_version": release.version,
        "flavor": flavor,
        "flavor_reason": why,
        "package": name,
        "url": release.url,
        "sha256": release.sha256,
        "engine_restarted": False,
        "checks": [],
        "side_effects": [],
        "notes": [],
    }

    def note(check, ok, detail=""):
        plan["checks"].append({"check": check, "ok": bool(ok), "detail": detail})

    if mode == "upgrade" and not releases.newer(release.version, before):
        plan["notes"].append("Kiri {0} is up to date".format(before))
        return plan

    prefix = root_prefix()
    if dry_run:
        note("checksum", True, "would check the download against SHA256SUMS-linux and the GitHub release's SHA256SUMS")
        note("package", True, "would check the package is {0} {1} for {2}".format(flavor, release.version, package_arch(fmt, arch)))
        note("root", prefix is not None, "would install with {0}".format(" ".join((prefix or ["sudo"]) + install_command(fmt, name))))
        if mode == "install":
            note("setup", True, "would run kiri host-access {0}".format(_user()))
            plan["side_effects"] = list(FRESH_SIDE_EFFECTS)
        elif engine_was_running:
            plan["side_effects"] = ["The engine would restart; running containers stop (those with a restart policy start again)."]
        if prefix is None:
            plan["notes"].append("Installing needs root: run as root or with passwordless sudo.")
        return plan

    if prefix is None:
        raise KiriMcpError(
            "needs_root",
            "installing {0} needs root, and sudo asks for a password; run it yourself: sudo {1}".format(
                name, " ".join(install_command(fmt, release.url.rsplit("/", 1)[-1]))
            ),
        )

    staging = tempfile.mkdtemp(prefix="kiri-mcp-")
    os.chmod(staging, 0o755)
    try:
        path, name, release, flavor = _fetch_verified(fmt, arch, flavor, why, release, name, staging, note, plan)
        out = _run(prefix + install_command(fmt, path), INSTALL_TIMEOUT)
        output, _ = releases.truncate_notes(_text(out))
        plan["output"] = output
        if out.returncode != 0:
            raise KiriMcpError("install_failed", "the package manager failed; Kiri is as it was:\n{0}".format(output))
        now = installed(fmt)
        if not now or now["flavor"] != flavor or now["version"] != release.version:
            raise KiriMcpError("install_failed", "after the install the system reports {0}, not {1} {2}".format(now, flavor, release.version))
        note("installed", True, "{0} {1}".format(flavor, release.version))
        plan["changed"] = True
        plan["after"] = release.version
        if mode == "upgrade":
            plan["engine_restarted"] = engine_was_running
            if engine_was_running:
                plan["side_effects"].append("The engine restarted; running containers stopped (those with a restart policy started again).")
        else:
            plan["setup"] = _setup(prefix)
            note("setup", plan["setup"]["ok"], plan["setup"]["output"])
            plan["side_effects"] = list(FRESH_SIDE_EFFECTS)
        return plan
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _fetch_verified(fmt, arch, flavor, why, release, name, staging, note, plan):
    """Download, check both checksums and the package's metadata. Falls back to kiri-lite when glibc is too old."""
    path = os.path.join(staging, name)
    verify.download(release.url, path, release.sha256)
    note("checksum", True, "SHA-256 matches SHA256SUMS-linux")
    note("checksum_github", True, "SHA-256 also matches {0}".format(cross_check(release, name)))
    expected = (flavor, release.version, package_arch(fmt, arch))
    got = package_metadata(fmt, path)
    if tuple(got) != expected:
        raise KiriMcpError("version_mismatch", "the package is {0}, not {1}; nothing was installed".format(got, expected))
    note("package", True, "{0} {1} {2}".format(*got))
    ok, need, have = glibc_ok(fmt, path)
    if ok:
        note("glibc", True, "needs {0}, have {1}".format(need, have) if need else "no glibc requirement")
        return path, name, release, flavor
    if flavor == "kiri" and why == "default":
        plan["notes"].append(
            "The full kiri package needs glibc {0}; this system has {1}, so kiri-lite (the engine alone) is installed instead.".format(need, have)
        )
        os.unlink(path)
        release, name = latest(fmt, arch, "kiri-lite")
        plan.update(flavor="kiri-lite", flavor_reason="glibc {0} < {1}".format(have, need), package=name,
                    url=release.url, sha256=release.sha256, target_version=release.version)
        return _fetch_verified(fmt, arch, "kiri-lite", "fallback", release, name, staging, note, plan)
    raise KiriMcpError("unsupported_os", "{0} {1} needs glibc {2}; this system has {3}".format(flavor, release.version, need, have))


def _user():
    return os.environ.get("SUDO_USER") or getpass.getuser()


def _setup(prefix):
    """`kiri host-access USER`: the kiri group, and the engine enabled and started."""
    user = _user()
    if user == "root":
        return {"ok": False, "output": "running as root: run `kiri host-access <your user>` to start the engine"}
    out = _run(prefix + ["kiri", "host-access", user], 120)
    text, _ = releases.truncate_notes(_text(out))
    return {"ok": out.returncode == 0, "output": text}
