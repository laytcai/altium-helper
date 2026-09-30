"""A GraphQL client that only ever sends queries.

Nexar and Altium 365 tokens can also write (comments, library parts, users, permissions),
so every document is checked before it's sent: anything that isn't a plain query is refused.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

NEXAR_API = "https://api.nexar.com/graphql"

_STRINGS = re.compile(r'"""[\s\S]*?"""|"(?:\\.|[^"\\])*"')
_COMMENTS = re.compile(r"#[^\n]*")
_WRITES = re.compile(r"\b(mutation|subscription)\b", re.IGNORECASE)


class ApiError(RuntimeError):
    """The API refused a request or couldn't be reached."""


def is_query_only(document: str) -> bool:
    """True when the document holds queries only: no mutation or subscription anywhere."""
    code = _COMMENTS.sub("", _STRINGS.sub('""', document))
    return _WRITES.search(code) is None


def graphql(
    endpoint: str,
    token: str,
    query: str,
    variables: dict | None = None,
    timeout: int = 60,
) -> dict:
    """Send a query and return its ``data``. Refuses anything that could write."""
    if not is_query_only(query):
        raise ApiError("Refusing to send a mutation: altium-helper is read-only.")
    body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "altium-helper",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise ApiError(
                f"{endpoint} refused the sign-in (it expired or isn't allowed). "
                "Run: altium-helper login"
            ) from e
        detail = e.read()[:300].decode("utf-8", errors="replace")
        raise ApiError(f"HTTP {e.code} from {endpoint}: {detail}") from e
    except urllib.error.URLError as e:
        raise ApiError(f"Can't reach {endpoint}: {e.reason}") from e
    errors = payload.get("errors") or []
    if errors and not payload.get("data"):
        raise ApiError(
            "; ".join(error.get("message", "unknown error") for error in errors)
        )
    return payload.get("data") or {}
