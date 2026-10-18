from __future__ import annotations

import pytest
from pydantic import ValidationError

from iq_connect.settings import Settings


def make(**values) -> Settings:
    return Settings(_env_file=None, **values)


def test_trailing_slash_is_removed():
    settings = make(public_base_url="https://iq.example.com/")
    assert settings.public_base_url == "https://iq.example.com"
    assert settings.mcp_url == "https://iq.example.com/mcp"


def test_dev_allows_localhost():
    assert make(app_env="dev", public_base_url="http://localhost:8000").is_dev


@pytest.mark.parametrize(
    "url",
    ["http://iq.example.com", "https://localhost:8000", "https://127.0.0.1"],
)
def test_production_rejects_urls_claude_cannot_reach(url):
    with pytest.raises(ValidationError):
        make(app_env="prod", public_base_url=url)


@pytest.mark.parametrize(
    "url",
    ["iq.example.com", "ftp://iq.example.com", "https://iq.example.com/?x=1", "https://iq.example.com/#a"],
)
def test_malformed_public_url_is_rejected(url):
    with pytest.raises(ValidationError):
        make(public_base_url=url)


def test_log_level_is_case_insensitive():
    assert make(log_level="DEBUG").log_level == "debug"
