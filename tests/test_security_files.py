import ipaddress

import pytest

from core.exceptions import SecurityError
from security.ssrf import SSRFGuard, is_ip_allowed
from security.url_logging import sanitize_url_for_log
from storage.file_manager import FileManager, safe_extension


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.1", "169.254.169.254", "169.254.1.1", "::1", "fe80::1", "fd00::1"],
)
def test_ssrf_forbidden_ranges(address):
    assert is_ip_allowed(ipaddress.ip_address(address))[0] is False


def test_ssrf_private_hostname_blocked(monkeypatch):
    monkeypatch.setattr("security.ssrf.resolve_hostname", lambda host: [ipaddress.ip_address("10.1.2.3")])
    with pytest.raises(SecurityError):
        SSRFGuard.validate_url("https://evil.example/video")


def test_ssrf_requires_http_scheme():
    with pytest.raises(SecurityError):
        SSRFGuard.validate_url("file:///etc/passwd")


def test_url_logging_removes_secrets():
    result = sanitize_url_for_log("https://cdn.example/x.mp4?token=secret#fragment")
    assert "secret" not in result and "fragment" not in result


def test_path_traversal(tmp_path):
    manager = FileManager(tmp_path / "jobs")
    with pytest.raises(SecurityError):
        manager.get_job_dir("../escape")


def test_path_prefix_confusion(tmp_path):
    manager = FileManager(tmp_path / "jobs")
    with pytest.raises(SecurityError):
        manager.get_job_file_path("safe", "../../jobs-evil/file")


def test_malicious_format_id_cannot_be_extension():
    assert safe_extension("../../exe") == "bin"


def test_symlink_escape(tmp_path):
    manager = FileManager(tmp_path / "jobs")
    target = tmp_path / "outside"
    target.mkdir()
    link = manager.base_jobs_dir / "linked"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(SecurityError):
        manager.get_job_dir("linked")


def test_cleanup_does_not_follow_file_symlink(tmp_path):
    manager = FileManager(tmp_path / "jobs")
    job_dir = manager.get_job_dir("job")
    outside = tmp_path / "important"
    outside.write_text("keep")
    link = job_dir / "link"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert manager.cleanup_job("job")
    assert outside.read_text() == "keep"
