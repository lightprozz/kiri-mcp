"""The install itself: upgrade through Kiri's own updater, or install fresh.

The upgrade path does not re-implement anything: `kiri update` already
downloads, checks and installs a release, pinned to the installed app's
own signing identity. Calling it is both less code and a stronger
guarantee than a second implementation could be.

The fresh-install path is the only place this tool verifies an artifact
itself, and it mirrors the official installer.
"""
import os
import re
import shutil
import subprocess
import tempfile

from . import config, platform, releases, verify
from .errors import KiriMcpError

# How long `kiri update` may take: the artifact is about 65 MB.
UPDATE_TIMEOUT = 900

# The staging and the old app live beside the app, on the same volume, so
# the final step is a rename rather than a copy.
STAGE_NAME = ".kiri-mcp-stage"
OLD_NAME = ".kiri-mcp-old"

# What `kiri setup --no-domains` changes. The result says so, so a caller
# can tell what the install touched.
SETUP_SIDE_EFFECTS = [
    "The LaunchAgent that starts the engine at login.",
    "The `kiri` and `docker` commands on your shell PATH.",
    "The `kiri` docker context.",
    "~/.testcontainers.properties, for Testcontainers.",
]


def _run(command, timeout):
    """Run a command, capturing both streams. Never through a shell."""
    try:
        return subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise KiriMcpError("timeout", "{0} did not finish in time".format(command[0]))
    except OSError as e:
        raise KiriMcpError("install_failed", "cannot run {0}: {1}".format(command[0], e))


def upgrade(dry_run=False):
    """Update the installed Kiri, through `kiri update`.

    "Already up to date" is a result, not an error: it is the normal
    state of a current machine.
    """
    binary = config.kiri_bin()
    if not binary:
        raise KiriMcpError("kiri_not_found", "no `kiri` binary on this Mac to update with")
    platform.check_os()
    before = platform.installed_version()

    if dry_run:
        # `kiri update --check` prints what is available and changes
        # nothing, so the plan can report the target version without
        # touching the machine.
        command = [binary, "update", "--check"]
    else:
        command = [binary, "update"]

    out = _run(command, UPDATE_TIMEOUT)
    stdout = out.stdout.decode("utf-8", "replace")
    stderr = out.stderr.decode("utf-8", "replace")

    if out.returncode != 0:
        detail = stderr.strip() or stdout.strip() or "kiri update failed"
        raise KiriMcpError("install_failed", detail)

    after = platform.installed_version() or before

    # The updater's own lines, matched exactly. `kiri update` prints the
    # release notes on the same line as the version
    # (`Kiri 1.2.3 is available (this is 1.2.2): <notes>`), and those
    # notes come from the network — so a note that says "updated to Kiri
    # 9.9.9" must not be able to forge the version or claim an install
    # that did not happen. The shapes below are anchored to the whole
    # line, which the notes cannot be.
    available = re.search(r"^Kiri ([0-9][^\s]*) is available \(this is [0-9][^\s]*\):", stdout, re.M)
    updated = re.search(r"^updated to Kiri ([0-9][^\s,]*), and the engine runs it\s*$", stdout, re.M)
    up_to_date = re.search(r"^Kiri ([0-9][^\s]*) is up to date\s*$", stdout, re.M)

    if updated:
        target = updated.group(1)
        changed = True
    elif available:
        target = available.group(1)
        changed = False
    elif up_to_date:
        target = up_to_date.group(1)
        changed = False
    else:
        target = after
        changed = bool(before and after and after != before)

    # The updater's output is capped like every other field that can
    # carry text from the network.
    output, cut = releases.truncate_notes(stdout.strip())
    notes = []
    if cut:
        notes.append("The updater's output was cut at {0} characters.".format(releases.NOTES_LIMIT))

    result = {
        "action": "upgrade",
        "dry_run": bool(dry_run),
        "changed": changed,
        "before": before,
        "after": after,
        "target_version": target,
        "engine_restarted": bool(changed and not dry_run),
        "side_effects": (
            ["The engine restarted; running containers stopped."] if changed and not dry_run else []
        ),
        "notes": notes,
        "output": output,
    }
    return result


def swap(staged, destination):
    """Put the verified app in place, keeping the old one until it is there.

    The staging area is inside the destination's folder, so this is a
    rename, not a copy. The old app is moved aside first and put back
    if the new one cannot go in, so the machine is never left without
    Kiri.

    The old app is kept in `.kiri-mcp-old` afterwards, not deleted: the
    caller checks the new app's signature on the installed path, and
    `rollback` puts the old one back if that fails.
    """
    folder = os.path.dirname(destination)
    old_folder = os.path.join(folder, OLD_NAME)
    shutil.rmtree(old_folder, ignore_errors=True)
    old = None
    if os.path.exists(destination):
        os.makedirs(old_folder, exist_ok=True)
        old = os.path.join(old_folder, os.path.basename(destination))
        os.rename(destination, old)
    try:
        os.rename(staged, destination)
    except OSError:
        if old is not None:
            os.rename(old, destination)
            shutil.rmtree(old_folder, ignore_errors=True)
        raise
    return old


def rollback(destination):
    """Put the previous app back, after the new one failed its check."""
    folder = os.path.dirname(destination)
    old_folder = os.path.join(folder, OLD_NAME)
    old = os.path.join(old_folder, os.path.basename(destination))
    if not os.path.isdir(old):
        return False
    shutil.rmtree(destination, ignore_errors=True)
    try:
        os.rename(old, destination)
    except OSError:
        return False
    shutil.rmtree(old_folder, ignore_errors=True)
    return True


def run_setup(app):
    """Run `kiri setup --no-domains` from the app just installed.

    The app's own binary, so the setup belongs to that app. A non-zero
    exit is reported, not raised: the official installer tolerates it
    too.

    `kiri setup` links the app's `kiri` and `docker` into `~/.kiri/bin`.
    Those links point at the bundle the *running binary* lives in, not at
    `KIRI_APP_DIR`. An install that does not land in the usual place
    would therefore repoint the user's live commands at an app that is
    not the one they use. The links are saved before the run and put
    back when this install did not land in the usual place.
    """
    binary = config.app_binary(app)
    command = [binary, "setup", "--no-domains"]
    saved_links = _save_bin_links()
    canonical = _is_canonical_app_dir(os.path.dirname(app))
    try:
        out = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
    except subprocess.TimeoutExpired:
        _restore_bin_links(saved_links, force=not canonical)
        return {"ok": False, "output": "kiri setup did not finish in time"}
    except OSError as e:
        _restore_bin_links(saved_links, force=not canonical)
        return {"ok": False, "output": "cannot run {0}: {1}".format(binary, e)}
    _restore_bin_links(saved_links, force=not canonical)
    text = (out.stdout.decode("utf-8", "replace") + out.stderr.decode("utf-8", "replace")).strip()
    return {"ok": out.returncode == 0, "output": text}


def _is_canonical_app_dir(folder):
    """Whether this is the place a Kiri app normally lives."""
    home = os.path.expanduser("~")
    return folder in ("/Applications", os.path.join(home, "Applications"))


def _bin_link_paths():
    """The links `kiri setup` makes in the state folder's bin directory."""
    folder = os.path.join(config.state_dir(), "bin")
    return [os.path.join(folder, name) for name in ("kiri", "docker")]


def _save_bin_links():
    """Remember what the bin links pointed at, so they can be restored."""
    saved = []
    for path in _bin_link_paths():
        try:
            target = os.readlink(path)
        except OSError:
            # Not a link, or not there: nothing to restore.
            target = None
        saved.append((path, target))
    return saved


def _restore_bin_links(saved, force=False):
    """Put the bin links back the way they were.

    With `force`, the links are put back whether or not they resolve:
    that is the case where the setup pointed them at an app outside the
    usual place. Without it, a link that still resolves is left alone —
    the setup pointed it at the app that was just installed, which is
    the right thing.
    """
    for path, target in saved:
        try:
            resolves = os.path.exists(path)
        except OSError:
            resolves = False
        if resolves and not force:
            continue
        try:
            os.remove(path)
        except OSError:
            pass
        if target is None:
            continue
        try:
            os.symlink(target, path)
        except OSError:
            pass



def fresh_install(dry_run=False):
    """Install the latest release on a Mac with no Kiri, verified."""
    platform.check_os()
    arch = platform.architecture()
    release = releases.latest()
    checks = []

    def note(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    # The destination is reported without creating the folder: a dry run
    # must change nothing on disk, and not even an empty folder.
    destination = os.path.join(config.app_dir(), config.APP_NAME)
    plan = {
        "action": "install",
        "dry_run": bool(dry_run),
        "changed": False,
        "before": platform.installed_version(),
        "after": None,
        "target_version": release.version,
        "destination": destination,
        "checks": checks,
        "side_effects": list(SETUP_SIDE_EFFECTS),
        "notes": [],
    }

    if dry_run:
        note("feed", True, "{0} names {1}".format(release.url, release.version))
        note("download", True, "would download {0}".format(release.url))
        note("sha256", True, "would check {0}".format(release.sha256))
        note("signature", True, "would run codesign --verify --deep --strict")
        note("notarization", True, "would run spctl -a -t exec")
        note("architecture", True, "would check the app is built for {0}".format(arch))
        note("version", True, "would check the app is {0}".format(release.version))
        note("setup", True, "would run kiri setup --no-domains")
        return plan

    folder = config.ensure_app_dir()
    staging = os.path.join(folder, STAGE_NAME)
    shutil.rmtree(staging, ignore_errors=True)
    # The staging area holds the artifact before it is checked, so only
    # the user may write in it. It is removed again at the end of the
    # install, whatever the outcome.
    os.makedirs(staging, mode=0o700, exist_ok=True)
    os.chmod(staging, 0o700)
    archive = os.path.join(staging, "kiri.tar.gz")
    unpacked = os.path.join(staging, "unpack")
    try:
        note("feed", True, "{0} names {1}".format(release.url, release.version))
        try:
            digest = verify.download(release.url, archive, release.sha256)
            note("sha256", True, digest)
        except KiriMcpError as e:
            note("sha256", False, e.message)
            raise

        try:
            verify.extract(archive, unpacked)
            note("unpack", True, "the archive's members stay inside the staging area")
        except KiriMcpError as e:
            note("unpack", False, e.message)
            raise

        app = os.path.join(unpacked, config.APP_NAME)
        if not os.path.isdir(app):
            note("unpack", False, "the download has no {0}".format(config.APP_NAME))
            raise KiriMcpError("install_failed", "the download has no {0}".format(config.APP_NAME))

        try:
            verify.verify_signature(app)
            note("signature", True, "codesign --verify --deep --strict passed")
        except KiriMcpError as e:
            note("signature", False, e.message)
            raise

        try:
            verify.verify_notarization(app)
            note("notarization", True, "spctl -a -t exec passed")
        except KiriMcpError as e:
            note("notarization", False, e.message)
            raise

        try:
            verify.verify_architecture(app, arch)
            note("architecture", True, "the app is built for {0}".format(arch))
        except KiriMcpError as e:
            note("architecture", False, e.message)
            raise

        try:
            version = verify.verify_version(app, release.version)
            note("version", True, "the app is {0}".format(version))
        except KiriMcpError as e:
            note("version", False, e.message)
            raise

        staged_app = os.path.join(staging, config.APP_NAME)
        shutil.move(app, staged_app)
        swap(staged_app, destination)
        note("install", True, "installed {0}".format(destination))

        # The signature is checked again on the installed path, before
        # anything is run from it. The app was checked in the staging
        # area, and between that check and this one it was renamed
        # twice; the staging area is inside the app folder, so anything
        # that can write there could have swapped the payload. This is
        # what `kiri update` does too: it re-checks the app after it is
        # in place.
        try:
            verify.verify_signature(destination)
            verify.verify_notarization(destination)
            note("signature", True, "the installed app's signature and notarization were checked again")
        except KiriMcpError as e:
            note("signature", False, e.message)
            if rollback(destination):
                plan["notes"].append("The previous Kiri was put back.")
            else:
                plan["notes"].append("The new Kiri did not pass its check and the previous one could not be put back.")
            raise

        # The old app has served its purpose.
        shutil.rmtree(os.path.join(os.path.dirname(destination), OLD_NAME), ignore_errors=True)

        setup = run_setup(destination)
        note("setup", setup["ok"], setup["output"])
        if not setup["ok"]:
            plan["notes"].append("kiri setup did not finish cleanly: {0}".format(setup["output"]))

        plan["changed"] = True
        plan["after"] = platform.bundle_version(destination)
        plan["installed_path"] = destination
        plan["setup"] = setup
        return plan
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def install_latest(mode="auto", dry_run=False):
    """Install or update Kiri, whichever the machine needs."""
    from . import linux

    if linux.is_linux():
        return linux.install_latest(mode=mode, dry_run=dry_run)
    if mode not in ("auto", "upgrade", "install"):
        raise KiriMcpError("mode_conflict", "unknown mode {0!r}: use auto, upgrade or install".format(mode))
    app = platform.installed_app()
    if mode == "auto":
        mode = "upgrade" if app else "install"
    elif mode == "upgrade" and not app:
        raise KiriMcpError("mode_conflict", "there is no Kiri on this Mac to upgrade")
    elif mode == "install" and app:
        raise KiriMcpError("mode_conflict", "Kiri is already installed here; use the upgrade mode")
    if mode == "upgrade":
        return upgrade(dry_run=dry_run)
    return fresh_install(dry_run=dry_run)
