"""Checks on the files and directories this tool creates or reuses.

Found by independent testing (F-15, F-16, F-20): an existing directory, lock file
or action log owned by another user was accepted, a failed write could leave a
single report file behind, and parent directories were created world-readable.
"""

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from vast_troubleshooter.fixes.storage import ActionLog, ProcessLock
from vast_troubleshooter.report import ReportError, write_report
from vast_troubleshooter.securefs import (
    create_private_file,
    ensure_private_dir,
    open_private,
)

NOW = datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC)
posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
root_only = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() != 0,  # type: ignore[attr-defined,unused-ignore]
    reason="needs root to hand a file to another user",
)
OTHER_USER = 12345


@posix_only
def test_missing_parent_directories_are_created_owner_only(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "c"
    ensure_private_dir(target)
    for directory in (tmp_path / "a", tmp_path / "a" / "b", target):
        assert directory.stat().st_mode & 0o777 == 0o700


@posix_only
def test_a_group_or_world_writable_directory_is_refused(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    with pytest.raises(OSError, match="writable by other users"):
        ensure_private_dir(shared)


@root_only
def test_a_directory_owned_by_another_user_is_refused(tmp_path: Path) -> None:
    directory = tmp_path / "theirs"
    directory.mkdir(mode=0o755)
    os.chown(directory, OTHER_USER, OTHER_USER)
    with pytest.raises(OSError, match="another user"):
        ensure_private_dir(directory)


@posix_only
@pytest.mark.parametrize("mode", [0o666, 0o640, 0o604])
def test_a_lock_or_log_file_others_can_access_is_refused(
    tmp_path: Path, mode: int
) -> None:
    lock, log = tmp_path / "fix.lock", tmp_path / "actions.jsonl"
    for path in (lock, log):
        path.write_text("")
        path.chmod(mode)
    with pytest.raises(OSError, match="accessible by other users"):
        ProcessLock(lock).acquire()
    with pytest.raises(OSError, match="accessible by other users"):
        ActionLog(log).write({"run_id": "x"})
    assert log.read_text() == ""  # nothing was appended to it


@root_only
def test_a_lock_or_log_file_owned_by_another_user_is_refused(tmp_path: Path) -> None:
    for name in ("fix.lock", "actions.jsonl"):
        path = tmp_path / name
        path.write_text("")
        path.chmod(0o600)
        os.chown(path, OTHER_USER, OTHER_USER)
    with pytest.raises(OSError, match="another user"):
        ProcessLock(tmp_path / "fix.lock").acquire()
    with pytest.raises(OSError, match="another user"):
        ActionLog(tmp_path / "actions.jsonl").write({"run_id": "x"})


def test_our_own_private_files_are_reused(tmp_path: Path) -> None:
    log = ActionLog(tmp_path / "state" / "actions.jsonl")
    log.write({"run_id": "one"})
    log.write({"run_id": "two"})  # the second write reuses the file we made
    assert len((tmp_path / "state" / "actions.jsonl").read_text().splitlines()) == 2
    lock = ProcessLock(tmp_path / "state" / "fix.lock")
    lock.acquire()
    lock.release()


@posix_only
def test_open_private_refuses_a_directory_and_a_symlink(tmp_path: Path) -> None:
    (tmp_path / "real").write_text("")
    (tmp_path / "real").chmod(0o600)
    (tmp_path / "link").symlink_to(tmp_path / "real")
    with pytest.raises(OSError, match="symlink"):
        open_private(tmp_path / "link", os.O_RDWR)
    with pytest.raises(OSError):
        open_private(tmp_path, os.O_RDONLY)


# -- a failed write never leaves a file behind (F-16) -------------------------------


def _fail_nth_fsync(monkeypatch: pytest.MonkeyPatch, n: int) -> None:
    real, calls = os.fsync, {"n": 0}

    def flaky(fd: int) -> None:
        calls["n"] += 1
        if calls["n"] == n:
            raise OSError(5, "I/O error")
        real(fd)

    monkeypatch.setattr(os, "fsync", flaky)


def test_a_failed_write_removes_the_file_it_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fail_nth_fsync(monkeypatch, 1)
    with pytest.raises(OSError):
        create_private_file(tmp_path / "x.json", b"data")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("failing_file", [1, 2])
def test_a_report_write_failure_leaves_no_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing_file: int
) -> None:
    _fail_nth_fsync(monkeypatch, failing_file)
    with pytest.raises(ReportError):
        write_report(tmp_path / "reports", "{}", "# md", NOW)
    assert list((tmp_path / "reports").iterdir()) == []


def test_a_short_write_is_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: real(fd, bytes(data)[:3]))
    create_private_file(tmp_path / "x.json", b"0123456789")
    assert (tmp_path / "x.json").read_bytes() == b"0123456789"
    ActionLog(tmp_path / "log.jsonl").write({"run_id": "a-long-enough-value"})
    assert "a-long-enough-value" in (tmp_path / "log.jsonl").read_text()
