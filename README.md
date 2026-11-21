# kiri-mcp

A local MCP server that reports the latest [Kiri](https://github.com/libracoder/kiri)
release and installs it, on macOS and Linux.

It lets an agent ask the machine it runs on two questions — *what is the
latest Kiri, and is it installed?* — and then *install it*. It does the
install the way the maintainer does it: the release feed's SHA-256, then
Apple's signature and notarization, then the architecture and the version
the feed promised. It never installs anything it has not verified.

## What it is not

**It is not inside Kiri.** It is a separate tool, so updating Kiri does
not update it and the other way round. Update it with `git pull`.

**It is not issue #74's server.** Kiri's
[issue #74](https://github.com/libracoder/kiri/issues/74) asks for a
built-in `kiri mcp` server that agents use to manage containers, gated
by Settings → AI integrations. That is a different server: inside the
app, in Rust, driving the engine API. This one is a separate tool,
in Python, and manages the Kiri app itself.

Their tool names do not overlap. Issue #74 reserves the `kiri_*`
namespace for engine tools (`kiri_status`, `kiri_list_containers`,
`kiri_stop_container` and the rest); this server's tools are
`kiri_release_status` and `kiri_install_latest`. A client holding both
servers sees four distinct tools.

This server follows #74's conventions for how an MCP server should
behave: annotations on every tool, structured responses with capped
output, diagnostics on stderr, and a timeout on every call.

## Registering it

There is no virtualenv and no `pip install`: the server is Python 3's
standard library only. Clone it and point your MCP client at `server.py`
with an absolute path:

```bash
git clone https://github.com/lightprozz/kiri-mcp ~/kiri-mcp
```

**Cursor** (`~/.cursor/mcp.json`) or **Claude Desktop**:

```json
"kiri-mcp": {
  "command": "/usr/bin/python3",
  "args": ["/Users/you/kiri-mcp/server.py"]
}
```

**Claude Code:** `claude mcp add kiri-mcp -- python3 ~/kiri-mcp/server.py`

**Devin** (custom STDIO server; it clones the repo on first start):

```bash
/bin/bash -c 'd="$HOME/.kiri-mcp"; [ -f "$d/server.py" ] || git clone -q --depth 1 https://github.com/lightprozz/kiri-mcp "$d" >&2; exec python3 "$d/server.py"'
```

The key is `kiri-mcp`, not `kiri`, so the `kiri` name stays free for
issue #74's built-in server. Two servers cannot share one key.

## The tools

| Tool | What it does |
|---|---|
| `kiri_release_status` | Read-only. Reports the installed version and path, the latest release with its notes, artifact URL and SHA-256, and whether an update is available. Changes nothing. |
| `kiri_install_latest` | Installs the latest Kiri, or updates the one that is installed. Takes `mode` (`auto`, `upgrade`, `install`) and `dry_run`. |

`kiri_install_latest` **replaces the app** in `/Applications` (on Linux,
the installed package). The upgrade
restarts the engine, which stops the containers that were running. The
tool is marked `destructiveHint: true` for that reason, and `dry_run`
reports the whole plan without changing anything.

## What it verifies

**The upgrade path** delegates to `kiri update`, Kiri's own updater.
That path already does every check, and pins the update to the
installed app's own signing identity — so only a genuine Kiri release
is accepted:

- the SHA-256 from the release feed,
- the same designated requirement as the app already installed,
- Apple's notarization (`spctl -a -t exec`),
- the CPU slice the Mac needs,
- the version the feed promised.

**The fresh-install path** is the only place this tool verifies an
artifact itself, and it mirrors the official `install.sh`: the feed's
SHA-256, `codesign --verify --deep --strict`, `spctl -a -t exec`, the
CPU slice, and the version in `Info.plist`.

One difference from the upgrade path, stated plainly: **a fresh install
accepts any valid notarized signature**, not only Kiri's own team
identity — that is what the official installer does, and the anchor is
the SHA-256 in the HTTPS feed. The upgrade path is stricter, because
`kiri update` reads the requirement from the app already installed. Do
not read the fresh-install guarantee as stronger than it is.

**Nothing is written until every check has passed.** A failed check
leaves the machine exactly as it was, and the old app is kept until the
new one is in place.

## What a fresh install changes

Besides the app itself, `kiri setup --no-domains` writes:

- the LaunchAgent that starts the engine at login,
- the `kiri` and `docker` commands on your shell `PATH`,
- the `kiri` docker context,
- `~/.testcontainers.properties`, for Testcontainers.

The tool's result lists these, so a caller can see what the install
touched.

## On Linux

The same two tools work on Linux, through Kiri's `.deb` and `.rpm`
packages instead of `Kiri.app`:

- **Release:** Linux has no JSON feed, so the latest version comes from
  `https://download.runkiri.com/SHA256SUMS-linux`.
- **Package:** `apt` systems get the `.deb` (`amd64`/`arm64`); `rpm`
  systems get the `.rpm` (`x86_64`/`aarch64`) through `dnf`, `yum` or `zypper`.
- **Flavour:** an installed `kiri` or `kiri-lite` is kept. A fresh install
  picks `kiri`, or `kiri-lite` (the engine alone) when the system's glibc
  is older than the full package needs (`kiri` 0.2.2 needs glibc 2.39).
- **Checks:** the package's SHA-256 must match `SHA256SUMS-linux` *and* the
  GitHub release's `SHA256SUMS`, and the package's own name, version and
  architecture must match. Linux packages carry no signature, so the two
  checksum lists are the only anchors.
- **Root:** installing runs as root, or with passwordless `sudo -n`; the
  server never prompts for a password and fails with `needs_root` instead.
- **Setup:** a fresh install runs `kiri host-access $USER`, which enables
  `kiri.service` and adds the user to the `kiri` group. That group owns
  `/run/kiri/docker.sock`, so membership is root-equivalent, like `docker`.
- **Upgrade:** `kiri update` refuses on Linux, so an upgrade installs the
  new package over the old one; the engine restarts.

`KIRI_FLAVOR` (`kiri` or `kiri-lite`) forces a flavour. `KIRI_SUMS_URL` and
`KIRI_GITHUB_SUMS_URL` (`{version}` is substituted)
override the two checksum lists.

## Environment variables

| Variable | What it does |
|---|---|
| `KIRI_UPDATE_URL` | The feed to read. The same variable `kiri update` reads, so one override steers both paths. |
| `KIRI_APP_DIR` | Where a fresh install lands. Defaults to `/Applications`, or `~/Applications` when the first is not writable. |
| `KIRI_BIN` | The `kiri` binary to use. |
| `KIRI_SOCKET` | The engine's socket, when it is not at `~/.kiri/run/docker.sock`. |
| `KIRI_MCP_TIMEOUT` | Seconds for a network request. Default 30. |

A release must come over HTTPS. The one exception is a loopback
address, which is how the tests serve a feed from `127.0.0.1`; a
plain-HTTP URL to any other host is refused.

## The tests

```bash
python3 -m unittest discover -s tests -t .
```

The macOS tests skip on Linux and the Linux tests skip on macOS.
They never touch `/Applications`, never reach the network, and never
leave a file behind. The round-trip with the official `mcp` client
skips when that package is not installed (`pip install mcp`).

### The live release check

```bash
python3 tests/check-live-release.py
```

It reads Kiri's real release files (the Mac feeds, `SHA256SUMS-linux`, the
GitHub release's `SHA256SUMS`), downloads three Linux packages, and fails
when they no longer have the shape this server reads, or when the sources
disagree on the latest version. GitHub Actions runs it every day
(`.github/workflows/live-release.yml`), so a change on Kiri's side shows
up as a failed run before an install breaks.
