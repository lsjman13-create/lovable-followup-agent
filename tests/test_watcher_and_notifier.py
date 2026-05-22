"""Watcher + Notifier 단위 테스트 — 실제 데스크톱 토스트는 비검증."""

from __future__ import annotations

import pytest

from lovable_agent.ingest.txt_watcher import (
    TxtInboxWatcher,
    file_hash,
    read_text_with_fallback_encoding,
)
from lovable_agent.output.notifier import (
    NotificationLevel,
    NotificationMessage,
    Notifier,
)
from lovable_agent.storage.sqlite_repo import SqliteRepository


# ──────────────────────────────────────────────────────────────
# Watcher
# ──────────────────────────────────────────────────────────────
@pytest.fixture()
def sqlite():
    r = SqliteRepository(":memory:")
    yield r
    r.close()


def test_file_hash_deterministic(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hello", encoding="utf-8")
    h1 = file_hash(f)
    h2 = file_hash(f)
    assert h1 == h2
    assert len(h1) == 64  # SHA-256


def test_file_hash_differs_for_different_content(tmp_path):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("hello", encoding="utf-8")
    b.write_text("world", encoding="utf-8")
    assert file_hash(a) != file_hash(b)


def test_read_text_falls_back_to_cp949(tmp_path):
    """cp949 로 저장된 한글 파일도 읽혀야 함."""
    f = tmp_path / "k.txt"
    f.write_bytes("안녕하세요".encode("cp949"))
    text = read_text_with_fallback_encoding(f)
    assert "안녕하세요" in text


def test_process_existing_calls_callback_once_per_file(tmp_path, sqlite):
    """폴더의 기존 .txt 파일들을 한 번만 처리."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.txt").write_text("aaa", encoding="utf-8")
    (inbox / "b.txt").write_text("bbb", encoding="utf-8")

    captured: list[tuple[str, str]] = []

    def on_new(text: str, name: str) -> None:
        captured.append((text, name))

    watcher = TxtInboxWatcher(inbox_folder=inbox, sqlite=sqlite, on_new_text=on_new)
    count = watcher.process_existing()
    assert count == 2
    assert {name for (_, name) in captured} == {"a.txt", "b.txt"}


def test_process_existing_skips_already_processed(tmp_path, sqlite):
    """두 번째 호출 시 같은 파일은 중복 처리 안 됨."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.txt").write_text("aaa", encoding="utf-8")

    captured: list[str] = []
    watcher = TxtInboxWatcher(
        inbox_folder=inbox,
        sqlite=sqlite,
        on_new_text=lambda text, name: captured.append(name),
    )

    assert watcher.process_existing() == 1
    assert watcher.process_existing() == 0  # 같은 해시
    assert captured == ["a.txt"]


def test_process_existing_skips_when_content_unchanged(tmp_path, sqlite):
    """파일을 지웠다가 같은 내용으로 다시 만들어도 (해시 같음) 스킵."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    f = inbox / "a.txt"
    f.write_text("aaa", encoding="utf-8")

    watcher = TxtInboxWatcher(
        inbox_folder=inbox,
        sqlite=sqlite,
        on_new_text=lambda text, name: None,
    )
    watcher.process_existing()

    f.unlink()
    f.write_text("aaa", encoding="utf-8")  # 같은 내용
    assert watcher.process_existing() == 0


def test_process_existing_handles_callback_failure(tmp_path, sqlite):
    """콜백이 예외를 던져도 watcher 가 죽지 않고 다음 파일로 진행."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.txt").write_text("aaa", encoding="utf-8")
    (inbox / "b.txt").write_text("bbb", encoding="utf-8")

    seen: list[str] = []

    def flaky(text: str, name: str) -> None:
        seen.append(name)
        if name == "a.txt":
            raise RuntimeError("simulated failure")

    watcher = TxtInboxWatcher(inbox_folder=inbox, sqlite=sqlite, on_new_text=flaky)
    count = watcher.process_existing()
    # 'a.txt' 는 콜백 실패로 mark 되지 않고 returncount 에서 빠짐.
    # 'b.txt' 는 정상 처리.
    assert count == 1
    assert set(seen) == {"a.txt", "b.txt"}
    # 다음 사이클에서 'a.txt' 는 여전히 처리 대상
    assert sqlite.is_file_processed(file_hash(inbox / "a.txt")) is False
    assert sqlite.is_file_processed(file_hash(inbox / "b.txt")) is True


# ──────────────────────────────────────────────────────────────
# Notifier
# ──────────────────────────────────────────────────────────────
def test_notifier_records_last_notification():
    n = Notifier()
    n.info("제목", "본문")
    assert n.last_notification is not None
    assert n.last_notification.title == "제목"
    assert n.last_notification.level == NotificationLevel.INFO


def test_notifier_levels():
    n = Notifier()
    n.warning("W", "x")
    assert n.last_notification.level == NotificationLevel.WARNING
    n.error("E", "x")
    assert n.last_notification.level == NotificationLevel.ERROR


def test_notify_with_dataclass():
    n = Notifier()
    msg = NotificationMessage(title="T", body="B", level=NotificationLevel.WARNING)
    n.notify(msg)
    assert n.last_notification is msg
