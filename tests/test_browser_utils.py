from pathlib import Path

from browser_utils import PROJECT_ROOT, get_chrome_user_data_dir


def test_chrome_profile_defaults_to_user_desktop_directory(monkeypatch):
    monkeypatch.delenv("XHS_CHROME_PROFILE", raising=False)

    assert get_chrome_user_data_dir() == Path.home() / "Desktop" / "xhs_chrome_profile"


def test_chrome_profile_can_be_overridden_by_environment(monkeypatch, tmp_path):
    profile_dir = tmp_path / "browser-profile"
    monkeypatch.setenv("XHS_CHROME_PROFILE", str(profile_dir))

    assert get_chrome_user_data_dir() == profile_dir


def test_relative_chrome_profile_override_is_project_relative(monkeypatch):
    monkeypatch.setenv("XHS_CHROME_PROFILE", "local-profile")

    assert get_chrome_user_data_dir() == PROJECT_ROOT / "local-profile"
