"""The round-trip with the official MCP client, and the tool names.

The client is the `mcp` package. When it is not installed the round-trip
skips, and says how to install it. The name check never skips: it reads
the server's own tool list, so a rename that collides with issue #74's
reserved names fails the suite.
"""
import asyncio
import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from kirimcp import server as mcp_server  # noqa: E402

# The names Kiri's issue #74 reserves for its built-in MCP server. This
# server's tools must stay clear of them: two servers in one client, one
# name, two meanings.
ISSUE_74_TOOLS = {
    "kiri_status",
    "kiri_list_containers",
    "kiri_inspect_container",
    "kiri_container_logs",
    "kiri_list_images",
    "kiri_list_volumes",
    "kiri_list_networks",
    "kiri_start_container",
    "kiri_stop_container",
    "kiri_restart_container",
}


def tool_names():
    """The names this server offers."""
    return {tool["name"] for tool in mcp_server.TOOLS}


class TestToolNames(unittest.TestCase):
    """The names, and what they must not collide with."""

    def test_the_server_offers_two_tools(self):
        self.assertEqual(len(mcp_server.TOOLS), 2)
        self.assertEqual(tool_names(), {"kiri_release_status", "kiri_install_latest"})

    def test_no_name_collides_with_issue_74(self):
        collision = tool_names() & ISSUE_74_TOOLS
        self.assertEqual(collision, set(), "these names belong to issue #74's built-in server: {0}".format(collision))

    def test_every_tool_carries_the_annotations(self):
        for tool in mcp_server.TOOLS:
            annotations = tool.get("annotations") or {}
            for key in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"):
                self.assertIn(key, annotations, "{0} has no {1}".format(tool["name"], key))

    def test_the_mutating_tool_is_marked_destructive(self):
        by_name = {tool["name"]: tool for tool in mcp_server.TOOLS}
        self.assertTrue(by_name["kiri_install_latest"]["annotations"]["destructiveHint"])
        self.assertFalse(by_name["kiri_release_status"]["annotations"]["destructiveHint"])

    def test_every_tool_has_a_schema_and_a_description(self):
        for tool in mcp_server.TOOLS:
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertTrue(tool["description"].strip())


class TestOutputCaps(unittest.TestCase):
    """Long release notes must not flood the agent's context."""

    def test_long_notes_are_cut_in_the_structured_result(self):
        from kirimcp import releases

        notes, truncated = releases.truncate_notes("y" * 8000)
        self.assertTrue(truncated)
        self.assertTrue(notes.endswith("… (truncated)"))
        self.assertLess(len(notes), 8000)

    def test_long_notes_are_cut_in_the_content_text(self):
        from kirimcp import releases

        long_notes = "y" * 8000
        release = releases.Release("1.2.3", "https://example.invalid/k.tar.gz", "ab", long_notes, "", False)
        text = mcp_server._text({"latest": release.as_dict()})
        self.assertLess(len(text), len(long_notes))
        self.assertNotIn(long_notes, text)

    def test_the_structured_result_carries_the_flag(self):
        from kirimcp import releases

        release = releases.Release("1.2.3", "https://example.invalid/k.tar.gz", "ab", "y" * 8000, "", True)
        payload = release.as_dict()
        self.assertTrue(payload["notes_truncated"])
        self.assertLess(len(payload["notes"]), 8000)


class TestProtocol(unittest.TestCase):
    """The JSON-RPC loop, driven by hand over a pipe."""

    def run_server(self, requests):
        """Send the requests to the server, read the answers."""
        payload = "".join(json.dumps(r) + "\n" for r in requests)
        out = subprocess.run(
            [sys.executable, os.path.join(ROOT, "server.py")],
            input=payload.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )
        lines = [l for l in out.stdout.decode("utf-8").splitlines() if l.strip()]
        return [json.loads(l) for l in lines], out.stderr.decode("utf-8")

    def test_initialize_and_tools_list(self):
        answers, stderr = self.run_server([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ])
        self.assertEqual(len(answers), 2, "a notification must get no answer")
        self.assertEqual(answers[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(answers[0]["result"]["serverInfo"]["name"], "kiri-mcp")
        self.assertEqual({t["name"] for t in answers[1]["result"]["tools"]}, tool_names())

    def test_a_notification_gets_no_answer(self):
        answers, _ = self.run_server([{"jsonrpc": "2.0", "method": "notifications/initialized"}])
        self.assertEqual(answers, [])

    def test_a_tools_call_without_an_id_gets_no_answer(self):
        answers, _ = self.run_server([
            {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "kiri_release_status", "arguments": {}}}
        ])
        self.assertEqual(answers, [], "a notification must get no answer")

    def test_a_tools_list_without_an_id_gets_no_answer(self):
        answers, _ = self.run_server([{"jsonrpc": "2.0", "method": "tools/list"}])
        self.assertEqual(answers, [])

    def test_an_unknown_method_is_an_error(self):
        answers, _ = self.run_server([{"jsonrpc": "2.0", "id": 1, "method": "nope"}])
        self.assertEqual(answers[0]["error"]["code"], -32601)

    def test_an_unknown_tool_is_an_error(self):
        answers, _ = self.run_server([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "kiri_nope", "arguments": {}}}
        ])
        self.assertEqual(answers[0]["error"]["code"], -32602)

    def test_a_known_protocol_version_is_echoed(self):
        answers, _ = self.run_server([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
        ])
        self.assertEqual(answers[0]["result"]["protocolVersion"], "2024-11-05")

    def test_an_unknown_protocol_version_gets_the_newest_known(self):
        answers, _ = self.run_server([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}},
        ])
        self.assertEqual(answers[0]["result"]["protocolVersion"], mcp_server.PROTOCOL_VERSION)

    def test_stdout_carries_only_the_protocol(self):
        answers, stderr = self.run_server([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        ])
        for answer in answers:
            self.assertIn("jsonrpc", answer)
        self.assertIn("kiri-mcp", stderr)

    def test_a_line_that_is_not_json_is_skipped(self):
        payload = "not json\n" + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n"
        out = subprocess.run(
            [sys.executable, os.path.join(ROOT, "server.py")],
            input=payload.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )
        lines = [l for l in out.stdout.decode("utf-8").splitlines() if l.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["result"], {})


class TestOfficialClient(unittest.TestCase):
    """The round-trip with the official `mcp` client, when it is there."""

    def setUp(self):
        try:
            import mcp  # noqa: F401
        except ImportError:
            self.skipTest("the mcp package is not installed: pip install mcp")

    def test_the_client_sees_the_tools(self):
        async def run():
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            params = StdioServerParameters(command=sys.executable, args=[os.path.join(ROOT, "server.py")])
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    return tools.tools

        tools = asyncio.new_event_loop().run_until_complete(run())
        names = {tool.name for tool in tools}
        self.assertEqual(names, tool_names())
        self.assertEqual(names & ISSUE_74_TOOLS, set())
        by_name = {tool.name: tool for tool in tools}
        self.assertTrue(by_name["kiri_install_latest"].annotations)
        self.assertTrue(by_name["kiri_release_status"].annotations)


if __name__ == "__main__":
    unittest.main()
