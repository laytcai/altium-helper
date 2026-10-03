"""Sign in to Nexar with your Altium account (OAuth2 authorization code with PKCE).

Follows Nexar's own Python example: a Nexar app's client ID and secret, a browser sign-in,
and a redirect to a one-shot listener on http://localhost:3000/login. Access tokens last
24 hours; with ``offline_access`` a refresh token renews them without signing in again.
"""

from __future__ import annotations

import base64
import hashlib
import html
import http.client
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import config
from .api import ApiError
from .boards import _exclusive

AUTHORIZE_URL = "https://identity.nexar.com/connect/authorize"
TOKEN_URL = "https://identity.nexar.com/connect/token"
REDIRECT_PORT = 3000
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/login"
SCOPES = [
    "openid",
    "profile",
    "email",
    "user.access",
    "design.domain",
    "offline_access",
]
_REFRESHING = threading.Lock()
_FAILED: list = []  # [monotonic time, message] of the last renewal that failed


def _post_token(form: dict) -> dict:
    request = urllib.request.Request(
        TOKEN_URL,
        data=urllib.parse.urlencode(form).encode("ascii"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            token = json.load(response)
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode("utf-8", errors="replace")
        raise ApiError(f"Nexar refused the sign-in: {detail}") from e
    except urllib.error.URLError as e:
        raise ApiError(f"Can't reach Nexar: {e.reason}") from e
    except (OSError, http.client.HTTPException, ValueError) as e:
        raise ApiError(f"No usable answer from Nexar: {e!r}") from e
    token["expires_at"] = time.time() + int(token.get("expires_in", 0)) - 60
    return token


def _wait_for_redirect(url: str, state: str, timeout: int) -> dict:
    """Open the sign-in page and wait for the browser to come back to localhost."""
    result: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parts = urllib.parse.urlsplit(self.path)
            if parts.path != "/login":
                self.send_error(404)
                return
            result.update(
                {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
            )
            ok = "code" in result and result.get("state") == state
            message = (
                "Signed in. You can close this tab and go back to the terminal."
                if ok
                else "Sign-in failed: "
                + html.escape(result.get("error", "unknown error"))
            )
            body = f"<html><body><p>{message}</p></body></html>".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # keep the terminal quiet
            pass

    try:
        server = HTTPServer(("127.0.0.1", REDIRECT_PORT), Handler)
    except OSError as e:
        raise ApiError(
            f"Port {REDIRECT_PORT} is busy, so the sign-in can't finish: {e}"
        ) from e
    server.timeout = 1
    print(f"Opening the Nexar sign-in page. If no browser opens, visit:\n  {url}")
    webbrowser.open(url)
    deadline = time.time() + timeout
    with server:
        while not result and time.time() < deadline:
            server.handle_request()
    if not result:
        raise ApiError("Timed out waiting for the Nexar sign-in")
    if result.get("error"):
        raise ApiError(
            f"Nexar sign-in failed: {result['error']} {result.get('error_description', '')}".strip()
        )
    if result.get("state") != state:
        raise ApiError("Nexar sign-in failed: the response didn't match this request")
    return result


def login(client_id: str, client_secret: str, timeout: int = 300) -> dict:
    """Sign in through the browser and store the tokens. Returns the token response."""
    scopes = list(SCOPES)
    for attempt in range(2):
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        )
        state = secrets.token_urlsafe(24)
        url = (
            AUTHORIZE_URL
            + "?"
            + urllib.parse.urlencode(
                {
                    "client_id": client_id,
                    "response_type": "code",
                    "redirect_uri": REDIRECT_URI,
                    "scope": " ".join(scopes),
                    "code_challenge": challenge.rstrip(b"=").decode("ascii"),
                    "code_challenge_method": "S256",
                    "state": state,
                }
            )
        )
        try:
            redirect = _wait_for_redirect(url, state, timeout)
        except ApiError as e:
            if (
                attempt == 0
                and "invalid_scope" in str(e)
                and "offline_access" in scopes
            ):
                print(
                    "This Nexar app can't renew sign-ins by itself; signing in without that."
                )
                scopes.remove("offline_access")
                continue
            raise
        token = _post_token(
            {
                "grant_type": "authorization_code",
                "code": redirect["code"],
                "redirect_uri": REDIRECT_URI,
                "client_id": client_id,
                "client_secret": client_secret,
                "code_verifier": verifier,
            }
        )
        _store(client_id, client_secret, token)
        return token
    raise ApiError("Nexar sign-in failed")


def _store(client_id: str, client_secret: str, token: dict) -> None:
    credentials = config.load_credentials()
    previous = credentials.get("nexar", {})
    credentials["nexar"] = {
        "client_id": client_id,
        "client_secret": client_secret,
        "access_token": token["access_token"],
        "expires_at": token["expires_at"],
        "refresh_token": token.get("refresh_token") or previous.get("refresh_token"),
    }
    config.save_credentials(credentials)


def access_token() -> str:
    """A valid Nexar access token, renewed with the refresh token when possible."""
    stored = _signed_in()
    if time.time() < stored.get("expires_at", 0):
        return stored["access_token"]
    asked = time.monotonic()
    # One renewal at a time, across parallel tool calls and other sessions' servers,
    # and the rest use its token: a refresh token may only work once.
    with _REFRESHING, _exclusive(config.config_dir() / "nexar.lock"):
        if _FAILED and _FAILED[0] >= asked:  # it failed while this call waited
            raise ApiError(_FAILED[1])
        stored = _signed_in()
        if time.time() < stored.get("expires_at", 0):
            return stored["access_token"]
        try:
            return _renew(stored)
        except ApiError as e:
            _FAILED[:] = [time.monotonic(), str(e)]
            raise


def _signed_in() -> dict:
    stored = config.load_credentials().get("nexar")
    if not stored:
        raise ApiError("Not signed in to Nexar. Run: altium-helper login")
    return stored


def _renew(stored: dict) -> str:
    if stored.get("refresh_token"):
        token = _post_token(
            {
                "grant_type": "refresh_token",
                "refresh_token": stored["refresh_token"],
                "client_id": stored["client_id"],
                "client_secret": stored["client_secret"],
            }
        )
        try:
            _store(stored["client_id"], stored["client_secret"], token)
        except OSError as e:
            raise ApiError(f"Couldn't save the renewed Nexar sign-in: {e}") from e
        return token["access_token"]
    raise ApiError(
        "The Nexar sign-in expired (it lasts 24 hours). Run: altium-helper login"
    )
