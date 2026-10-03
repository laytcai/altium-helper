"""The API layer against a fake GraphQL endpoint: read-only guard, discovery, PCB snapshots."""

import io
import json
import time
import urllib.error
from datetime import datetime, timedelta, timezone

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


GUID_PREFIX = "\\0065366B-FCCB-47C8-905B-7F119A516C3D\\"


def _answer_revisions(query, variables):
    nodes = [
        {
            "revisionId": "a" * 40,
            "createdAt": "2026-10-01T12:00:00.000Z",
            "message": "First layout",
            "author": "Alice",
            "files": [{"path": GUID_PREFIX + "PDU.PrjPcb", "kind": "ADDED"}],
        },
        {
            "revisionId": "b" * 40,
            "createdAt": "2026-10-02T12:00:00.000Z",
            "message": "",
            "author": "Bob",
            "files": [
                {"path": GUID_PREFIX + "sheets\\Power.SchDoc", "kind": "MODIFIED"},
                {"path": GUID_PREFIX + "PDU.BomDoc", "kind": "DELETED"},
            ],
        },
    ]
    return {"desProjectById": {"revisions": {"nodes": nodes}}}


def test_history_of_a_board_not_fetched_comes_from_the_api(
    unreachable_board, monkeypatch
):
    """Altium 365's commit list is the board's git history, without a download."""
    from altium_helper import git, mcp_server

    fake_api(monkeypatch, _answer_revisions)

    def no_git(*args, **kwargs):
        raise AssertionError("git ran")

    monkeypatch.setattr(git, "run", no_git)
    result = mcp_server.board_history("pdu", since="2026-10-02")
    assert result["source"] == "Altium 365 API" and "note" not in result
    (commit,) = result["commits"]
    assert commit["author"] == "Bob" and commit["rev"] == "b" * 10
    assert commit["files"] == [
        {"status": "M", "path": "sheets/Power.SchDoc"},
        {"status": "D", "path": "PDU.BomDoc"},
    ]
    assert commit["design_files_changed"] == ["sheets/Power.SchDoc"]


def test_an_excluded_boards_history_isnt_read_either(unreachable_board, monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    from altium_helper import mcp_server

    settings = config.Settings.load()
    settings.exclude = ["PDU"]
    settings.save()
    monkeypatch.setattr(api.urllib.request, "urlopen", pytest.fail)
    with pytest.raises(ToolError, match="exclude"):
        mcp_server.board_history("pdu")


def test_api_paths_read_like_git_paths():
    assert cloud._repository_path(GUID_PREFIX + "a\\b.SchDoc") == "a/b.SchDoc"
    assert cloud._repository_path("\\folder\\b.SchDoc") == "folder/b.SchDoc"


def test_an_excluded_board_gets_no_fallback(unreachable_board, monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    from altium_helper import mcp_server

    settings = config.Settings.load()
    settings.exclude = ["PDU"]
    settings.save()
    monkeypatch.setattr(api.urllib.request, "urlopen", pytest.fail)
    with pytest.raises(ToolError, match="exclude"):
        mcp_server.get_board("pdu")


def _many_revisions(count: int):
    """Altium's list of `count` commits, one an hour apart, newest (c0) first, in pages."""
    newest = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    nodes = [
        {
            "revisionId": f"{i:040x}",
            "createdAt": (newest - timedelta(hours=i)).strftime(
                "%Y-%m-%dT%H:%M:%S.000Z"
            ),
            "message": f"c{i}",
            "author": "Alice",
            "files": [],
        }
        for i in range(count)
    ]

    def answer(query, variables):
        start = int(variables.get("after") or 0)
        page = nodes[start : start + variables["n"]]
        end = start + len(page)
        info = {"hasNextPage": end < len(nodes), "endCursor": str(end)}
        return {"desProjectById": {"revisions": {"pageInfo": info, "nodes": page}}}

    return answer


def test_api_history_reads_back_past_the_newest_hundred(unreachable_board, monkeypatch):
    """A window 1,200 commits back: the list is read in pages until it reaches it."""
    from altium_helper import mcp_server

    sent = fake_api(monkeypatch, _many_revisions(1500))
    until = datetime(2026, 10, 1, 12, tzinfo=timezone.utc) - timedelta(hours=1200)
    result = mcp_server.board_history("pdu", until=until.isoformat(), limit=3)
    assert [c["message"] for c in result["commits"]] == ["c1200", "c1201", "c1202"]
    assert "note" not in result
    assert any(body["variables"]["after"] for body in sent)  # it went past one page


def test_api_history_that_cant_reach_back_says_so(unreachable_board, monkeypatch):
    """With git unreachable too, the answer says how far the list was read."""
    from altium_helper import mcp_server

    monkeypatch.setattr(mcp_server, "API_HISTORY_LIMIT", 100)
    fake_api(monkeypatch, _many_revisions(150))
    result = mcp_server.board_history("pdu", until="2026-09-26T12:00:00Z", limit=3)
    assert result["commits"] == []
    assert "Only its newest 100 commits were read" in result["note"]


def test_a_missing_revision_list_is_an_error_not_an_empty_history(
    unreachable_board, monkeypatch
):
    """GraphQL answers a failed resolver with null data; that isn't 'no commits'."""
    from mcp.server.mcpserver.exceptions import ToolError

    from altium_helper import mcp_server

    fake_api(monkeypatch, lambda query, variables: {"desProjectById": None})
    with pytest.raises(ToolError, match="no revision list"):
        mcp_server.board_history("pdu")


@pytest.mark.parametrize(
    "failure", [TimeoutError("timed out"), ConnectionResetError("reset")]
)
def test_a_dropped_connection_is_an_api_error(failure, monkeypatch):
    def urlopen(request, timeout):
        raise failure

    monkeypatch.setattr(api.urllib.request, "urlopen", urlopen)
    with pytest.raises(api.ApiError, match="No usable answer"):
        api.graphql("https://example.com/graphql", "t", "query { a }")


def test_a_reply_that_isnt_json_is_an_api_error(monkeypatch):
    def urlopen(request, timeout):
        return FakeResponse(b"<html>Service Unavailable</html>")

    monkeypatch.setattr(api.urllib.request, "urlopen", urlopen)
    with pytest.raises(api.ApiError, match="No usable answer"):
        api.graphql("https://example.com/graphql", "t", "query { a }")


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


def test_a_null_list_of_revisions_is_an_error_too(unreachable_board, monkeypatch):
    """One revision's field failing nulls the whole list; that isn't 'no commits'."""
    from mcp.server.mcpserver.exceptions import ToolError

    from altium_helper import mcp_server

    page = {"pageInfo": {"hasNextPage": False}, "nodes": None}
    fake_api(monkeypatch, lambda q, v: {"desProjectById": {"revisions": page}})
    with pytest.raises(ToolError, match="no revision list"):
        mcp_server.board_history("pdu")
