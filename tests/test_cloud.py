"""The API layer against a fake GraphQL endpoint: read-only guard, discovery, PCB snapshots."""

import io
import json
import time
import urllib.error

import pytest

from altium_helper import api, boards, cloud, config, unformat


@pytest.mark.parametrize(
    "document",
    [
        "mutation { desCreateComment(input: {}) { id } }",
        "query { a } mutation { b }",
        "subscription { changes }",
        "  MUTATION { x }",
    ],
)
def test_writes_are_refused_before_sending(document, monkeypatch):
    monkeypatch.setattr(api.urllib.request, "urlopen", pytest.fail)
    with pytest.raises(api.ApiError, match="read-only"):
        api.graphql("https://example.com/graphql", "t", document)


def test_the_word_mutation_inside_a_string_or_comment_is_fine():
    assert api.is_query_only(
        'query { search(text: "mutation") { id } } # no mutation here'
    )


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_expired_sign_in_says_how_to_fix_it(monkeypatch):
    def refuse(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 401, "Unauthorized", {}, io.BytesIO(b"")
        )

    monkeypatch.setattr(api.urllib.request, "urlopen", refuse)
    with pytest.raises(api.ApiError, match="altium-helper login"):
        api.graphql("https://example.com/graphql", "t", "query { a }")


@pytest.fixture
def signed_in(tmp_path, monkeypatch):
    monkeypatch.setenv("ALTIUM_HELPER_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("ALTIUM_HELPER_DESIGNS", str(tmp_path / "designs"))
    config.save_credentials(
        {
            "nexar": {
                "client_id": "id",
                "client_secret": "secret",
                "access_token": "token",
                "expires_at": time.time() + 3600,
            }
        }
    )
    settings = config.Settings.load()
    settings.workspace_url = "https://per.365.altium.com"
    settings.save()


def fake_api(monkeypatch, answer):
    """Route every query to answer(query, variables) -> data."""
    sent = []

    def urlopen(request, timeout):
        body = json.loads(request.data)
        assert request.get_header("Authorization") == "Bearer token"
        sent.append(body)
        return FakeResponse(
            json.dumps({"data": answer(body["query"], body["variables"])}).encode()
        )

    monkeypatch.setattr(api.urllib.request, "urlopen", urlopen)
    return sent


def test_discovery_pages_through_projects_and_dedupes_names(signed_in, monkeypatch):
    pages = {
        None: {
            "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
            "nodes": [
                {
                    "id": "P1",
                    "name": "DAQ Board",
                    "repositoryUrl": "https://a.365.altium.com/git/1.git",
                }
            ],
        },
        "c1": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [
                {"id": "P2", "name": "DAQ board", "repositoryUrl": None},
                {
                    "id": "P3",
                    "name": "PDU",
                    "repositoryUrl": "https://a.365.altium.com/git/3.git",
                },
            ],
        },
    }
    sent = fake_api(monkeypatch, lambda q, v: {"desProjects": pages[v["after"]]})
    entries = cloud.discover_boards()
    assert [e["key"] for e in entries] == ["daq-board", "daq-board-2", "pdu"]
    assert entries[1]["git_url"] == ""
    assert sent[0]["variables"]["url"] == "https://per.365.altium.com"
    found = boards.find("pdu")
    assert (found.project_id, found.git_url) == (
        "P3",
        "https://a.365.altium.com/git/3.git",
    )


def test_comment_threads_say_whether_they_are_still_open(signed_in, monkeypatch):
    # Nexar's schema: "Comment thread status. 0 = Resolved, 1 = Active."
    threads = [
        {
            "threadNumber": 2,
            "status": 1,
            "createdAt": "2026-09-30T10:00:00Z",
            "context": {"documentId": "D1"},
            "comments": [
                {
                    "text": "Is the fuse footprint right?",
                    "createdAt": "2026-09-30T10:00:00Z",
                    "createdBy": {"firstName": "Ada", "lastName": "Lovelace"},
                }
            ],
        },
        {
            "threadNumber": 1,
            "status": 0,
            "createdAt": "2026-09-29T10:00:00Z",
            "context": None,
            "comments": [],
        },
    ]
    fake_api(monkeypatch, lambda q, v: {"desCommentThreads": threads})
    board = boards.Board(key="pdu", name="PDU", git_url="", project_id="P9")
    result = cloud.comments(board)
    assert [(t["thread"], t["open"]) for t in result] == [(1, False), (2, True)]
    assert result[1]["comments"][0]["by"] == "Ada Lovelace"


def test_pcb_snapshot_becomes_a_valid_netlist(signed_in, monkeypatch):
    def answer(query, variables):
        if "variants { name }" in query:
            return {"desProjectById": {"design": {"variants": [{"name": "Default"}]}}}
        if "desWipVariantByVariantName" in query:
            items = [
                {
                    "designator": "U3",
                    "component": {
                        "id": "C-MCU",
                        "manufacturerParts": [
                            {"partNumber": "STM32G474", "companyName": "ST"}
                        ],
                    },
                    "pads": [
                        {"designator": "12", "net": {"name": "CAN_TX"}},
                        {"designator": "13", "net": None},
                    ],
                },
                {
                    "designator": "U4",
                    "component": {"id": "C-XCVR", "manufacturerParts": []},
                    "pads": [{"designator": "1", "net": {"name": "CAN_TX"}}],
                },
            ]
            page = {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": items,
            }
            return {"desWipVariantByVariantName": {"pcb": {"designItems": page}}}
        if "desComponentsByIds" in query:
            return {
                "desComponentsByIds": [
                    {
                        "id": "C-MCU",
                        "details": {
                            "symbols": [
                                {"pins": [{"designator": "12", "name": "PB12"}]}
                            ]
                        },
                    },
                    {"message": "not a managed component"},
                ]
            }
        raise AssertionError(query)

    fake_api(monkeypatch, answer)
    netlist = cloud.pcb_netlist("P1")
    assert netlist["nets"]["CAN_TX"] == {"U3": ["12"], "U4": ["1"]}
    assert netlist["components"]["U3"]["pins"] == {
        "12": {"name": "PB12", "net": "CAN_TX"},
        "13": "NetU3_13",
    }
    assert netlist["components"]["U3"]["mpn"] == "STM32G474"
    assert netlist["metadata"]["netlistHash"] == unformat.netlist_hash(
        netlist["nets"], netlist["components"]
    )


def _answer_pcb(query, variables):
    if "variants { name }" in query:
        return {"desProjectById": {"design": {"variants": [{"name": "Default"}]}}}
    if "desWipVariantByVariantName" in query:
        item = {
            "designator": "R1",
            "component": None,
            "pads": [{"designator": "1", "net": {"name": "LED"}}],
        }
        page = {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [item]}
        return {"desWipVariantByVariantName": {"pcb": {"designItems": page}}}
    return {"desComponentsByIds": []}


@pytest.fixture
def unreachable_board(signed_in):
    """A board Altium 365 lists, whose git repository can't be reached."""
    entry = {
        "key": "pdu",
        "name": "PDU",
        "git_url": "http://127.0.0.1:9/pdu.git",
        "project_id": "P9",
    }
    path = config.designs_dir() / "boards.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([entry]), encoding="utf-8")


def test_get_board_falls_back_to_the_pcb_when_git_fails(unreachable_board, monkeypatch):
    from altium_helper import mcp_server

    fake_api(monkeypatch, _answer_pcb)
    result = mcp_server.get_board("pdu")
    assert result["source"] == "Altium 365 API, PCB only"
    assert "no schematic wiring" in result["note"]
    netlist = json.loads(open(result["design"], encoding="utf-8").read())
    assert netlist["nets"]["LED"] == {"R1": ["1"]}


def test_an_excluded_board_gets_no_fallback(unreachable_board, monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    from altium_helper import mcp_server

    settings = config.Settings.load()
    settings.exclude = ["PDU"]
    settings.save()
    monkeypatch.setattr(api.urllib.request, "urlopen", pytest.fail)
    with pytest.raises(ToolError, match="exclude"):
        mcp_server.get_board("pdu")


def test_a_board_can_be_excluded_by_its_project_id(unreachable_board, monkeypatch):
    """A project id never moves, unlike a key when two projects share a name."""
    from mcp.server.mcpserver.exceptions import ToolError

    from altium_helper import mcp_server

    settings = config.Settings.load()
    settings.exclude = ["P9"]
    settings.save()
    monkeypatch.setattr(api.urllib.request, "urlopen", pytest.fail)
    with pytest.raises(ToolError, match="exclude"):
        mcp_server.get_board("pdu")
