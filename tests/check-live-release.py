#!/usr/bin/env python3
"""Checks Kiri's live release files still have the shape kiri-mcp reads.

    python3 tests/check-live-release.py

It reaches the network: download.runkiri.com and GitHub. It is run every
day by .github/workflows/live-release.yml, not by the unit tests. Every
check prints PASS, FAIL or SKIP; any FAIL exits 1.
"""
import os
import shutil
import sys
import tempfile
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kirimcp import config, linux, releases, verify  # noqa: E402
from kirimcp.errors import KiriMcpError  # noqa: E402

GITHUB_LATEST = "https://github.com/libracoder/kiri/releases/latest"
MAC_ARCHES = ("arm64", "x86_64")
LINUX_ARCHES = ("x86_64", "arm64")
FORMATS = ("deb", "rpm")
DOWNLOADS = [("deb", "kiri-lite"), ("deb", "kiri"), ("rpm", "kiri-lite")]

results = []
versions = {}


def check(name, fn):
    try:
        detail = fn()
    except KiriMcpError as e:
        results.append(("FAIL", name, "{0}: {1}".format(e.code, e.message)))
    except Exception as e:  # noqa: BLE001 — every failure is a finding here
        results.append(("FAIL", name, "{0}: {1}".format(type(e).__name__, e)))
    else:
        status, detail = detail if isinstance(detail, tuple) else ("PASS", detail)
        results.append((status, name, detail or ""))
    print("{0:4}  {1}  {2}".format(*results[-1]), flush=True)


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def head(url):
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": config.USER_AGENT})
    with urllib.request.urlopen(request, timeout=config.timeout()) as response:
        return response.status, int(response.headers.get("Content-Length") or 0)


def github_latest():
    request = urllib.request.Request(GITHUB_LATEST, method="HEAD", headers={"User-Agent": config.USER_AGENT})
    with urllib.request.urlopen(request, timeout=config.timeout()) as response:
        landed = response.geturl()
    tag = landed.rsplit("/tag/", 1)[1] if "/tag/" in landed else ""
    require(tag.startswith("v"), "{0} led to {1}, not a v-tag".format(GITHUB_LATEST, landed))
    versions["github"] = tag[1:]
    return tag


def mac_feed(arch):
    def run():
        errors = []
        for url in config.feed_urls(arch):
            try:
                release = releases.parse_feed(releases._fetch(url, config.FEED_LIMIT), url)
            except KiriMcpError as e:
                errors.append(e.message)
                continue
            require(len(release.sha256) == 64 and all(c in "0123456789abcdef" for c in release.sha256),
                    "{0} has a sha256 that is not 64 hex digits".format(url))
            status, size = head(release.url)
            require(status == 200 and size > 0, "{0} answered {1} with {2} bytes".format(release.url, status, size))
            versions["mac-" + arch] = release.version
            return "{0} -> {1} ({2})".format(url.rsplit("/", 1)[1], release.version, release.url.rsplit("/", 1)[1])
        raise AssertionError("no feed answered: " + "; ".join(errors))
    return run


def linux_sums():
    url = linux.sums_url()
    sums = linux.parse_sums(releases._fetch(url, linux.SUMS_LIMIT), url)
    found = linux.packages(sums)
    require(found, "{0} names no .deb or .rpm".format(url))
    newest = found[0][2]
    for p in found:
        if releases.newer(p[2], newest):
            newest = p[2]
    want = {(fmt, flavor, linux.package_arch(fmt, arch)) for fmt in FORMATS for flavor in linux.FLAVORS for arch in LINUX_ARCHES}
    have = {(p[0], p[1], p[3]) for p in found if p[2] == newest}
    require(have >= want, "{0} {1} is missing {2}".format(url, newest, sorted(want - have)))
    unknown = [n for n in sums if n not in {p[4] for p in found}]
    versions["linux"] = newest
    return "{0}: {1} packages{2}".format(newest, len(have), "; unrecognised: " + ", ".join(unknown) if unknown else "")


def linux_package(fmt, arch, flavor):
    def run():
        release, name = linux.latest(fmt, arch, flavor)
        github = linux.cross_check(release, name)
        status, size = head(release.url)
        require(status == 200 and size > 0, "{0} answered {1} with {2} bytes".format(release.url, status, size))
        return "{0} ({1} MB), matches {2}".format(name, size >> 20, github.split("/download/")[1])
    return run


def linux_download(fmt, flavor, staging):
    def run():
        tool = "dpkg-deb" if fmt == "deb" else "rpm"
        if not shutil.which(tool):
            return "SKIP", "no {0} here".format(tool)
        release, name = linux.latest(fmt, "x86_64", flavor)
        path = os.path.join(staging, name)
        verify.download(release.url, path, release.sha256)
        got = tuple(linux.package_metadata(fmt, path))
        want = (flavor, release.version, linux.package_arch(fmt, "x86_64"))
        require(got == want, "{0} says it is {1}, not {2}".format(name, got, want))
        glibc = linux.required_glibc(fmt, path)
        if flavor == "kiri":
            require(glibc, "{0} declares no glibc requirement, so the kiri-lite fallback cannot be decided".format(name))
        os.remove(path)
        return "{0} is {1}; glibc {2}".format(name, " ".join(got), glibc or "not required")
    return run


def versions_agree():
    seen = {k: v for k, v in versions.items() if v}
    require(len(set(seen.values())) == 1, "the sources disagree: {0}".format(seen))
    return next(iter(seen.values()))


def main():
    for var in ("KIRI_SUMS_URL", "KIRI_GITHUB_SUMS_URL", "KIRI_UPDATE_URL"):
        os.environ.pop(var, None)
    staging = tempfile.mkdtemp(prefix="kiri-live-")
    try:
        check("github release", github_latest)
        for arch in MAC_ARCHES:
            check("mac feed " + arch, mac_feed(arch))
        check("SHA256SUMS-linux", linux_sums)
        for fmt in FORMATS:
            for arch in LINUX_ARCHES:
                for flavor in linux.FLAVORS:
                    check("linux {0} {1} {2}".format(fmt, arch, flavor), linux_package(fmt, arch, flavor))
        for fmt, flavor in DOWNLOADS:
            check("package {0} {1}".format(fmt, flavor), linux_download(fmt, flavor, staging))
        check("versions agree", versions_agree)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    failed = [r for r in results if r[0] == "FAIL"]
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write("## Kiri live release check\n\n| | Check | Detail |\n|---|---|---|\n")
            for status, name, detail in results:
                f.write("| {0} | {1} | {2} |\n".format(status, name, detail.replace("|", "\\|").replace("\n", " ")))
    print("\n{0} checks, {1} failed".format(len(results), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
