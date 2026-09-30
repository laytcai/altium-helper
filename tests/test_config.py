import sys

import pytest

from altium_helper import config


@pytest.fixture
def config_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("ALTIUM_HELPER_CONFIG", str(tmp_path / "config"))
    return tmp_path / "config"


def test_settings_round_trip(config_folder):
    settings = config.Settings.load()
    assert settings.api == "nexar" and settings.boards == {}
    settings.workspace_url = "https://example.365.altium.com"
    settings.exclude = ["Secret Board"]
    settings.boards["demo"] = config.BoardConfig(
        git_url="https://example.com/demo.git", name="Demo"
    )
    settings.save()
    loaded = config.Settings.load()
    assert loaded.workspace_url == "https://example.365.altium.com"
    assert loaded.exclude == ["Secret Board"]
    assert loaded.boards["demo"].git_url == "https://example.com/demo.git"


def test_credentials_are_private(config_folder):
    config.save_credentials({"nexar": {"client_secret": "s3cret"}})
    assert config.load_credentials() == {"nexar": {"client_secret": "s3cret"}}
    if sys.platform != "win32":
        assert (config_folder / "credentials.json").stat().st_mode & 0o777 == 0o600


def test_folders_follow_environment_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("ALTIUM_HELPER_DATA", str(tmp_path / "data"))
    assert config.designs_dir() == tmp_path / "data" / "designs"
