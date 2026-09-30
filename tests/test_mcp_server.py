"""Talk to `altium-helper mcp` over stdio the way Claude does."""

import json
import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.network  # the board fixture needs universal-netlist


class Client:
    def __init__(self):
        self.process = subprocess.Popen(
            [sys.executable, "-m", "altium_helper", "mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=dict(os.environ),
        )
        self.next_id = 0
        self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        )
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, message: dict) -> None:
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def request(self, method: str, params: dict) -> dict:
        self.next_id += 1
        self._send(
            {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params}
        )
        while True:
            line = self.process.stdout.readline()
            if not line:
                raise AssertionError("server exited: " + self.process.stderr.read())
            message = json.loads(line)
            if message.get("id") == self.next_id:
                assert "error" not in message, message
                return message["result"]

    def call(self, tool: str, **arguments) -> tuple[bool, dict | str]:
        result = self.request("tools/call", {"name": tool, "arguments": arguments})
        text = result["content"][0]["text"] if result.get("content") else ""
        if result.get("isError"):
            return False, text
        return True, result.get("structuredContent") or json.loads(text)

    def close(self) -> None:
        self.process.stdin.close()
        self.process.wait(timeout=20)


@pytest.fixture
def client(board):
    client = Client()
    yield client
    client.close()


def test_tools_are_listed_and_read_only(client):
    tools = {t["name"]: t for t in client.request("tools/list", {})["tools"]}
    assert set(tools) == {
        "list_boards",
        "get_board",
        "board_history",
        "board_changes",
        "check_board",
    }
    assert all(t["annotations"]["readOnlyHint"] for t in tools.values())


def test_what_changed_since_yesterday(client):
    ok, result = client.call("board_changes", board="daq", since="yesterday")
    assert ok, result
    fix = result["commits"][0]
    assert fix["author"] == "Alice" and fix["message"] == "Fix flipped CAN pins"
    assert fix["changes"][0].startswith("Swapped: U3.12 (PB12) and U3.13 (PB13)")


def test_get_board_returns_a_design_universal_netlist_can_read(client):
    ok, result = client.call("get_board", board="DAQ")
    assert ok, result
    assert result["design"].endswith("DAQ.netlist.json")
    assert os.path.exists(result["design"])
    assert result["revision"]["message"] == "Add CAN terminator"


def test_unknown_board_is_a_readable_error(client):
    ok, message = client.call("get_board", board="nonexistent")
    assert not ok
    assert "No board called" in message
