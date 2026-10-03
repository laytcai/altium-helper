"""Read board lists, comments and PCB data from Altium's GraphQL APIs (queries only).

Nexar (your own sign-in) and the Altium 365 API (a workspace token from an admin) take
the same queries. Neither has schematic connectivity or past revisions: those come from
git (see boards.py). The PCB snapshot here is the fallback for boards git can't reach.
The commit list is the same as git's history, and needs no download.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from . import api, config, nexar, unformat
from .boards import Board, slug

WORKSPACES = "query { desWorkspaceInfos { name url isDefault } }"

PROJECTS = """
query ($url: String, $after: String) {
  desProjects(workspaceUrl: $url, first: 100, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { id name repositoryUrl updatedAt }
  }
}"""

COMMENTS = """
query ($id: ID!) {
  desCommentThreads(projectId: $id) {
    threadNumber status createdAt
    context { documentId }
    comments { text createdAt createdBy { firstName lastName userName } }
  }
}"""

REVISIONS = """
query ($id: ID!, $n: Int, $after: String) {
  desProjectById(id: $id) {
    revisions(first: $n, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes { revisionId message author createdAt files { path kind } }
    }
  }
}"""

REVISION_TIMES = """
query ($id: ID!, $n: Int, $after: String) {
  desProjectById(id: $id) {
    revisions(first: $n, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes { revisionId createdAt }
    }
  }
}"""
REVISIONS_PER_PAGE = 1000

VARIANTS = (
    "query ($id: ID!) { desProjectById(id: $id) { design { variants { name } } } }"
)

PCB_ITEMS = """
query ($id: ID!, $variant: String!, $after: String) {
  desWipVariantByVariantName(projectId: $id, variantName: $variant) {
    pcb {
      designItems(first: 200, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          designator
          component { id comment description manufacturerParts { partNumber companyName } }
          pads { designator net { name } }
        }
      }
    }
  }
}"""

PIN_NAMES = """
query ($ids: [ID!]!) {
  desComponentsByIds(ids: $ids) {
    ... on DesComponent { id details { symbols { pins { name designator } } } }
    ... on DesErrorPayload { message }
  }
}"""

BOARD_LIST_MAX_AGE = 6 * 3600  # seconds before the board list is fetched again
THREAD_RESOLVED = 0  # DesCommentThread.status: "0 = Resolved, 1 = Active"
FILE_STATUS = {"ADDED": "A", "MODIFIED": "M", "DELETED": "D"}  # as git log shows them
GUID = re.compile(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


def endpoint_and_token() -> tuple[str, str]:
    """Where to send queries, and the token to send, for the configured API."""
    settings = config.Settings.load()
    if settings.api == "altium365":
        token = config.load_credentials().get("altium365", {}).get("token")
        if not token or not settings.workspace_url:
            raise api.ApiError(
                "No Altium 365 token saved. Run: altium-helper login --token"
            )
        return settings.workspace_url.rstrip("/") + "/api/graphql/", token
    return api.NEXAR_API, nexar.access_token()


def query(document: str, variables: dict | None = None) -> dict:
    endpoint, token = endpoint_and_token()
    return api.graphql(endpoint, token, document, variables)


def workspaces() -> list[dict]:
    return query(WORKSPACES).get("desWorkspaceInfos") or []


def _boards_file() -> Path:
    return config.designs_dir() / "boards.json"


def discover_boards() -> list[dict]:
    """List the workspace's projects and save them as boards. Returns the saved entries."""
    settings = config.Settings.load()
    entries, taken, after = [], set(), None
    while True:
        page = query(PROJECTS, {"url": settings.workspace_url or None, "after": after})[
            "desProjects"
        ]
        for project in page.get("nodes") or []:
            key = base = slug(project["name"])
            counter = 2
            while key in taken:
                key, counter = f"{base}-{counter}", counter + 1
            taken.add(key)
            entries.append(
                {
                    "key": key,
                    "name": project["name"],
                    "git_url": project.get("repositoryUrl") or "",
                    "project_id": project["id"],
                }
            )
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    # In one step: other tool calls read the file while this writes it.
    config.write_file(_boards_file(), json.dumps(entries, indent=2) + "\n")
    return entries


def refresh_boards_if_stale() -> bool:
    """Fetch the board list again if it's older than BOARD_LIST_MAX_AGE. Returns True if it did."""
    path = _boards_file()
    configured = (
        config.Settings.load().api == "altium365"
        or config.load_credentials().get("nexar")
    )
    if not configured:
        return False
    if path.exists() and time.time() - path.stat().st_mtime < BOARD_LIST_MAX_AGE:
        return False
    discover_boards()
    return True


def _person(user: dict | None) -> str:
    user = user or {}
    full = " ".join(p for p in (user.get("firstName"), user.get("lastName")) if p)
    return full or user.get("userName") or "unknown"


def comments(board: Board) -> list[dict]:
    """The board's Altium 365 comment threads, oldest first."""
    if not board.project_id:
        raise api.ApiError(
            f"{board.name} wasn't found through an API, so it has no comments to read"
        )
    threads = query(COMMENTS, {"id": board.project_id}).get("desCommentThreads") or []
    result = []
    for thread in sorted(threads, key=lambda t: t["createdAt"]):
        result.append(
            {
                "thread": thread["threadNumber"],
                "open": thread["status"] != THREAD_RESOLVED,
                "document": (thread.get("context") or {}).get("documentId"),
                "comments": [
                    {
                        "by": _person(c.get("createdBy")),
                        "at": c["createdAt"],
                        "text": c["text"],
                    }
                    for c in thread.get("comments") or []
                ],
            }
        )
    return result


def _revision_nodes(board: Board, document: str, limit: int) -> list[dict]:
    """The newest ``limit`` revisions (fewer if that's all), newest first."""
    if not board.project_id:
        raise api.ApiError(
            f"{board.name} wasn't found through an API, so it has no revision list"
        )
    nodes: list[dict] = []
    after = None
    while len(nodes) < limit:
        variables = {
            "id": board.project_id,
            "n": min(limit - len(nodes), REVISIONS_PER_PAGE),
            "after": after,
        }
        project = query(document, variables).get("desProjectById")
        page = (project or {}).get("revisions")
        # A failed resolver: GraphQL sends null, with errors, for the field that failed,
        # or for the whole list when one revision failed.
        if page is None or page.get("nodes") is None:
            raise api.ApiError(f"Altium 365 sent no revision list for {board.name}")
        nodes += page["nodes"]
        info = page.get("pageInfo") or {}
        if not info.get("hasNextPage") or not info.get("endCursor"):
            break
        after = info["endCursor"]
    return sorted(nodes, key=lambda n: n.get("createdAt") or "", reverse=True)[:limit]


def _repository_path(path: str) -> str:
    """A path as git shows it. Altium 365 starts each one with the project's GUID: \\<GUID>\\..."""
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    if len(parts) > 1 and GUID.fullmatch(parts[0]):
        parts = parts[1:]
    return "/".join(parts)


def revisions(board: Board, limit: int = 30) -> list[dict]:
    """Altium 365's own commit list for a board, newest first: who, when, message, files.

    It matches the board's git history: the same commits, authors, dates, messages and
    changed files (renames aside) on all 1,341 commits checked.
    """
    return [
        {
            "rev": (node.get("revisionId") or "")[:10],
            # Altium gives UTC; local time reads like git's dates.
            "date": _time(node["createdAt"]).astimezone().isoformat(),
            "author": node.get("author"),
            "message": node.get("message") or "",
            "files": [
                {
                    "status": FILE_STATUS.get(f.get("kind") or "", "M"),
                    "path": _repository_path(f.get("path") or ""),
                }
                for f in node.get("files") or []
            ],
        }
        for node in _revision_nodes(board, REVISIONS, limit)
    ]


def revision_times(board: Board, limit: int = 100) -> list[tuple[str, datetime]]:
    """Commit ids and times, newest first: enough to size a history fetch."""
    return [
        (node["revisionId"].lower(), _time(node["createdAt"]))
        for node in _revision_nodes(board, REVISION_TIMES, limit)
        if node.get("revisionId") and node.get("createdAt")
    ]


def _time(text: str) -> datetime:
    text = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    return datetime.fromisoformat(text)


def _pin_names(component_ids: list[str]) -> dict[str, dict[str, str]]:
    """Symbol pin names by component id (workspace-library parts only have them)."""
    names: dict[str, dict[str, str]] = {}
    for start in range(0, len(component_ids), 50):
        chunk = component_ids[start : start + 50]
        for entry in query(PIN_NAMES, {"ids": chunk}).get("desComponentsByIds") or []:
            if not entry or "id" not in entry:
                continue
            pins = {}
            for symbol in (entry.get("details") or {}).get("symbols") or []:
                for pin in symbol.get("pins") or []:
                    if pin.get("designator") and pin.get("name"):
                        pins[pin["designator"]] = pin["name"]
            names[entry["id"]] = pins
    return names


def pcb_netlist(project_id: str) -> dict:
    """The latest PCB's connectivity as a Universal Netlist (pads, nets, symbol pin names)."""
    variants = query(VARIANTS, {"id": project_id})["desProjectById"]["design"][
        "variants"
    ]
    if not variants:
        raise api.ApiError("This project has no design data in Altium 365 yet")
    items, after = [], None
    while True:
        variant = query(
            PCB_ITEMS,
            {"id": project_id, "variant": variants[0]["name"], "after": after},
        )
        pcb = (variant.get("desWipVariantByVariantName") or {}).get("pcb")
        if not pcb:
            raise api.ApiError("This project has no PCB in Altium 365 yet")
        page = pcb["designItems"]
        items += page.get("nodes") or []
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    ids = sorted(
        {i["component"]["id"] for i in items if (i.get("component") or {}).get("id")}
    )
    names = _pin_names(ids)
    components = {}
    for item in items:
        ref, component = item["designator"], item.get("component") or {}
        symbol_pins = names.get(component.get("id"), {})
        pins = {}
        for pad in item.get("pads") or []:
            number = pad["designator"]
            net = (pad.get("net") or {}).get("name") or f"Net{ref}_{number}"
            pins[number] = (
                {"name": symbol_pins[number], "net": net}
                if symbol_pins.get(number)
                else net
            )
        parts = component.get("manufacturerParts") or [{}]
        components[ref] = {
            "pins": pins,
            "mpn": parts[0].get("partNumber"),
            "manufacturer": parts[0].get("companyName"),
            "description": component.get("description"),
            "comment": component.get("comment"),
        }
    return unformat.build(components)


def pcb_snapshot(board: Board) -> Path:
    """Save the board's current PCB from the API as a netlist file universal-netlist can read."""
    if not board.project_id:
        raise api.ApiError(
            f"{board.name} wasn't found through an API, so there's no PCB data to fetch"
        )
    netlist = pcb_netlist(board.project_id)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return unformat.write(board.root / "nexar" / f"pcb-{stamp}.netlist.json", netlist)
