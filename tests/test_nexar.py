"""The Nexar browser sign-in, with a fake browser and a fake token endpoint."""

import threading
import time
import urllib.parse
import urllib.request

import pytest

from altium_helper import config, nexar
from altium_helper.api import ApiError


@pytest.fixture
def config_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("ALTIUM_HELPER_CONFIG", str(tmp_path / "config"))


def fake_browser(monkeypatch, answers):
    """Instead of opening a browser, send the redirect Nexar would send back."""
    seen = []

    def open_page(url):
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        seen.append(query)
        reply = answers.pop(0)
        reply.setdefault("state", query["state"])

        def redirect():
            time.sleep(0.2)
            urllib.request.urlopen(
                f"{nexar.REDIRECT_URI}?{urllib.parse.urlencode(reply)}", timeout=10
            ).read()

        threading.Thread(target=redirect, daemon=True).start()

    monkeypatch.setattr(nexar.webbrowser, "open", open_page)
    return seen


def fake_token_endpoint(monkeypatch):
    forms = []

    def post(form):
        forms.append(form)
        return {
            "access_token": f"token-{len(forms)}",
            "refresh_token": "refresh",
            "expires_at": time.time() + 3600,
        }

    monkeypatch.setattr(nexar, "_post_token", post)
    return forms


def test_sign_in_stores_tokens(config_folder, monkeypatch):
    seen = fake_browser(monkeypatch, [{"code": "abc"}])
    forms = fake_token_endpoint(monkeypatch)
    nexar.login("client", "secret", timeout=20)
    assert "offline_access" in seen[0]["scope"].split()
    assert seen[0]["code_challenge_method"] == "S256"
    assert forms[0]["code"] == "abc" and forms[0]["client_secret"] == "secret"
    assert len(forms[0]["code_verifier"]) >= 43
    assert nexar.access_token() == "token-1"


def test_retries_without_offline_access_when_the_app_refuses_it(
    config_folder, monkeypatch
):
    seen = fake_browser(monkeypatch, [{"error": "invalid_scope"}, {"code": "xyz"}])
    fake_token_endpoint(monkeypatch)
    nexar.login("client", "secret", timeout=20)
    assert "offline_access" not in seen[1]["scope"].split()


def test_a_forged_redirect_is_rejected(config_folder, monkeypatch):
    fake_browser(monkeypatch, [{"code": "abc", "state": "not-ours"}])
    fake_token_endpoint(monkeypatch)
    with pytest.raises(ApiError, match="didn't match"):
        nexar.login("client", "secret", timeout=20)


def test_expired_token_is_renewed_with_the_refresh_token(config_folder, monkeypatch):
    config.save_credentials(
        {
            "nexar": {
                "client_id": "client",
                "client_secret": "secret",
                "access_token": "old",
                "expires_at": time.time() - 1,
                "refresh_token": "refresh",
            }
        }
    )
    forms = fake_token_endpoint(monkeypatch)
    assert nexar.access_token() == "token-1"
    assert forms[0]["grant_type"] == "refresh_token"


def test_no_sign_in_says_how_to_sign_in(config_folder):
    with pytest.raises(ApiError, match="altium-helper login"):
        nexar.access_token()
