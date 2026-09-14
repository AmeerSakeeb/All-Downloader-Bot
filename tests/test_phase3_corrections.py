import json

import pytest

from core.config import Settings
from services.cookie_profiles import CookieProfiles


@pytest.mark.parametrize(
    ("cookie_domains", "expected"),
    [
        ("", []),
        ("youtube.com", ["youtube.com"]),
        ("youtube.com,youtu.be", ["youtube.com", "youtu.be"]),
        ('["youtube.com", "youtu.be"]', ["youtube.com", "youtu.be"]),
    ],
)
def test_settings_parses_environment_list_strings(monkeypatch, cookie_domains, expected):
    monkeypatch.setenv("BOT_TOKEN", "123456:example-token")
    monkeypatch.setenv("ADMIN_USER_IDS", "123456789,987654321")
    monkeypatch.setenv("YTDLP_COOKIES_FILE", "")
    monkeypatch.setenv("YTDLP_COOKIE_DOMAINS", cookie_domains)

    settings = Settings(_env_file=None)

    assert settings.admin_user_ids == [123456789, 987654321]
    assert settings.ytdlp_cookie_domains == expected


def test_named_profile_cookie_domains_default_to_source_domains(tmp_path):
    profiles_path = tmp_path / "profiles.json"
    profiles_path.write_text(
        json.dumps(
            [
                {
                    "name": "youtube-owner",
                    "source_domains": ["youtube.com", "youtu.be"],
                    "path": str((tmp_path / "cookies.txt").resolve()),
                    "enabled": True,
                }
            ]
        ),
        encoding="utf-8",
    )
    settings = Settings(
        bot_token="123456:example-token",
        jobs_dir=tmp_path / "jobs",
        ytdlp_profiles_file=profiles_path,
        _env_file=None,
    )

    profile = CookieProfiles(settings)._read_configuration()["youtube-owner"]

    assert profile.source_domains == ("youtube.com", "youtu.be")
    assert profile.cookie_domains == profile.source_domains
