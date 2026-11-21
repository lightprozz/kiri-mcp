"""The MCP server: JSON-RPC 2.0 over stdio, and two tools.

MCP over stdio is JSON-RPC 2.0, one message per line. A request without
an `id` is a notification and gets no answer. A tool that fails returns
`isError: true` with the message in `content`, so an agent sees "the
checksum did not match" instead of a dead server.

stdout carries the protocol and nothing else. Every diagnostic goes to
stderr: a stray print on stdout would corrupt the stream.

The tool names sit outside the `kiri_*` engine namespace that Kiri's
issue #74 reserves for its built-in server, so a client holding both
servers sees four distinct tools.
"""
import json
import sys

from . import __version__, install, releases
from .errors import KiriMcpError

# The protocol version observed to negotiate with the official client.
PROTOCOL_VERSION = "2025-06-18"

# The versions this server answers with. A client that asks for one of
# these gets it; one that asks for anything else gets the newest this
# server knows.
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")

SERVER_INFO = {"name": "kiri-mcp", "version": __version__}

# The longest text a tool returns. The release notes are the only
# unbounded field, and they are cut when they are read; this is the
# backstop for anything added later.
TEXT_LIMIT = 16384


def _tool(name, title, description, schema, annotations):
    return {
        "name": name,
        "title": title,
        "description": description,
        "inputSchema": schema,
        "annotations": annotations,
    }


TOOLS = [
    _tool(
        "kiri_release_status",
        "Kiri release status",
        "Report the Kiri installed on this machine (macOS or Linux) and the latest release published for it. "
        "Read-only: it changes nothing on the machine. The answer names the installed "
        "version and path, the latest version with its release notes, artifact URL and "
        "SHA-256, and whether an update is available.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    ),
    _tool(
        "kiri_install_latest",
        "Install or update Kiri",
        "Install the latest Kiri on this machine, or update the one that is installed. "
        "On macOS this REPLACES the app in /Applications; on Linux it installs the .deb or "
        ".rpm through apt/dnf/zypper with root (sudo -n). The upgrade restarts the engine and "
        "stops the containers that were running. Use dry_run first to see the plan "
        "without changing anything. The download is checked before anything is written: on macOS "
        "against the release feed's SHA-256 and Apple's signature and notarization, on Linux "
        "against SHA256SUMS-linux and the GitHub release's SHA256SUMS, then the package's own "
        "name, version and architecture.",
        {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["auto", "upgrade", "install"],
                    "default": "auto",
                    "description": "auto updates an installed Kiri and installs one otherwise.",
                },
                "dry_run": {
                    "type": "boolean",
                    "default": False,
                    "description": "Report what would happen and change nothing.",
                },
            },
            "additionalProperties": False,
        },
        {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": True,
        },
    ),
]


# The tools' names, so a call for one that does not exist is refused
# before any work is done.
TOOL_NAMES = {tool["name"] for tool in TOOLS}


def send(message):
    """Write one JSON-RPC message to stdout and flush it."""
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def negotiate(requested):
    """The protocol version to answer with.

    The client's version when this server knows it, so a client that
    speaks an older one still works; the newest this server knows
    otherwise, which is what the protocol asks for.
    """
    if requested in SUPPORTED_PROTOCOLS:
        return requested
    return PROTOCOL_VERSION


def log(message):
    """A diagnostic, on stderr, where it cannot corrupt the protocol."""
    sys.stderr.write("kiri-mcp: {0}\n".format(message))
    sys.stderr.flush()


def _text(payload):
    """A readable rendering of a result, cut to TEXT_LIMIT."""
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if len(text) <= TEXT_LIMIT:
        return text
    return text[:TEXT_LIMIT] + "\n… (truncated)"


def _cap(value):
    """A payload with every long string cut to TEXT_LIMIT.

    The cap has to hold on both channels: a client that reads
    `structuredContent` rather than the text must not get an unbounded
    payload either.
    """
    if isinstance(value, str):
        return value if len(value) <= TEXT_LIMIT else value[:TEXT_LIMIT] + "… (truncated)"
    if isinstance(value, dict):
        return {key: _cap(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_cap(item) for item in value]
    return value


def _result(payload, is_error=False):
    """A tool result: the text a model reads and the data a program reads."""
    payload = _cap(payload)
    result = {"content": [{"type": "text", "text": _text(payload)}], "structuredContent": payload}
    if is_error:
        result["isError"] = True
    return result


def _arguments(name, arguments):
    """The tool's arguments, checked against its own schema.

    The schema says `dry_run` is a boolean and `mode` is one of three
    strings. A client that sends `"false"` as a string, or `0` as an
    int, must not be able to talk its way into a real install when it
    meant a dry run — so the types are checked, not coerced.
    """
    if not isinstance(arguments, dict):
        raise KiriMcpError("mode_conflict", "the arguments must be an object")
    allowed = {
        "kiri_release_status": set(),
        "kiri_install_latest": {"mode", "dry_run"},
    }[name]
    unknown = set(arguments) - allowed
    if unknown:
        raise KiriMcpError(
            "mode_conflict",
            "{0} takes no argument named {1}".format(name, ", ".join(sorted(unknown))),
        )
    if name != "kiri_install_latest":
        return arguments

    mode = arguments.get("mode", "auto")
    if not isinstance(mode, str) or mode not in ("auto", "upgrade", "install"):
        raise KiriMcpError(
            "mode_conflict",
            "mode must be auto, upgrade or install, not {0!r}".format(mode),
        )
    dry_run = arguments.get("dry_run", False)
    if not isinstance(dry_run, bool):
        raise KiriMcpError(
            "mode_conflict",
            "dry_run must be true or false, not {0!r}".format(dry_run),
        )
    return {"mode": mode, "dry_run": dry_run}


def call_tool(name, arguments):
    """Run one tool and return its result. Never raises."""
    arguments = arguments if isinstance(arguments, dict) else {}
    try:
        if name == "kiri_release_status":
            return _result(releases.status())
        if name == "kiri_install_latest":
            checked = _arguments(name, arguments)
            return _result(install.install_latest(mode=checked["mode"], dry_run=checked["dry_run"]))
        raise KiriMcpError("mode_conflict", "no tool named {0!r}".format(name))
    except KiriMcpError as e:
        log("{0} failed: {1}".format(name, e.message))
        return _result({"error": e.as_dict()}, is_error=True)
    except Exception as e:  # a bug must not kill the server
        log("{0} failed unexpectedly: {1!r}".format(name, e))
        return _result(
            {"error": {"code": "install_failed", "message": "{0}: {1}".format(type(e).__name__, e)}},
            is_error=True,
        )


def handle(request):
    """Answer one JSON-RPC request, or None for a notification.

    A message without an `id` is a notification and gets no answer.
    That holds for every method, `initialize` included: JSON-RPC 2.0
    says a notification is never answered.
    """
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params") or {}

    if request_id is None:
        return None

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": negotiate(params.get("protocolVersion")),
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        }

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name not in TOOL_NAMES:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": "unknown tool: {0}".format(name)},
            }
        return {"jsonrpc": "2.0", "id": request_id, "result": call_tool(name, arguments)}

    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}

    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": "method not found: {0}".format(method)},
    }


def answer(request):
    """One request's answer, or None. Never raises."""
    try:
        return handle(request)
    except Exception as e:
        request_id = request.get("id")
        log("a request failed unexpectedly: {0!r}".format(e))
        if request_id is None:
            return None
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32603, "message": "{0}: {1}".format(type(e).__name__, e)},
        }


def main():
    """The loop: one request per line on stdin, one answer per line on stdout."""
    log("kiri-mcp {0} ready".format(__version__))
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            log("a line was not JSON; skipping it")
            continue
        if isinstance(request, list):
            # A JSON-RPC batch: each request is answered on its own, and
            # the answers go back as one array.
            answers = [a for a in (answer(item) for item in request if isinstance(item, dict)) if a is not None]
            if answers:
                send(answers)
            continue
        if not isinstance(request, dict):
            log("a message was not an object; skipping it")
            continue
        response = answer(request)
        if response is not None:
            send(response)
    log("kiri-mcp: stdin closed")
