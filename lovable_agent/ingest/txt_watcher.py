"""inbox 폴더 감시 — 새 .txt 파일이 떨어지면 처리 콜백 호출.

watchdog 라이브러리 기반. 같은 파일 해시 중복 처리를 막기 위해 SqliteRepository 의
processed_files 테이블과 연동.

사용법:
    from lovable_agent.ingest.txt_watcher import TxtInboxWatcher

    def on_new_text(text: str, file_name: str) -> None:
        ...  # extractor 호출 등

    watcher = TxtInboxWatcher(
        inbox_folder="~/lovable-agent/inbox/",
        sqlite=sqlite_repo,
        on_new_text=on_new_text,
    )
    watcher.start()  # 백그라운드 시작
    # ...
    watcher.stop()
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from lovable_agent.storage.sqlite_repo import SqliteRepository

log = logging.getLogger(__name__)


def file_hash(path: Path) -> str:
    """파일 내용의 SHA-256 해시 — 중복 처리 방지용 키."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def read_text_with_fallback_encoding(path: Path) -> str:
    """utf-8 → utf-8-sig → cp949 순으로 시도, 마지막은 replace."""
    for enc in ("utf-8", "utf-8-sig", "cp949"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


class TxtInboxWatcher:
    """inbox 폴더에서 새 .txt 를 발견하면 콜백 호출."""

    def __init__(
        self,
        inbox_folder: str | Path,
        sqlite: SqliteRepository,
        on_new_text: Callable[[str, str], None],
    ) -> None:
        self._folder = Path(inbox_folder).expanduser()
        self._folder.mkdir(parents=True, exist_ok=True)
        self._sqlite = sqlite
        self._on_new = on_new_text
        self._observer: Observer | None = None

    def process_existing(self) -> int:
        """폴더에 이미 있는 .txt 들을 한 번 훑어 처리. 시작 시 1회 호출 권장.

        Returns:
            새로 처리한 파일 수.
        """
        count = 0
        for path in sorted(self._folder.glob("*.txt")):
            if self._process_one(path):
                count += 1
        return count

    def _process_one(self, path: Path) -> bool:
        """단일 파일 처리. 이미 처리된 해시면 스킵.

        Returns:
            새로 처리한 경우 True, 중복으로 스킵한 경우 False.
        """
        try:
            digest = file_hash(path)
        except OSError as e:
            log.warning("파일 해시 실패: %s — %s", path, e)
            return False

        if self._sqlite.is_file_processed(digest):
            log.debug("이미 처리된 파일 스킵: %s", path.name)
            return False

        try:
            text = read_text_with_fallback_encoding(path)
        except OSError as e:
            log.warning("파일 읽기 실패: %s — %s", path, e)
            return False

        log.info("새 .txt 처리: %s (%d자)", path.name, len(text))
        try:
            self._on_new(text, path.name)
        except Exception:
            log.exception("on_new_text 콜백 실패: %s", path.name)
            return False

        self._sqlite.mark_file_processed(digest, path.name)
        return True

    def start(self) -> None:
        """백그라운드 감시 시작."""
        if self._observer is not None:
            return  # 이미 시작됨

        handler = _Handler(self._process_one)
        observer = Observer()
        observer.schedule(handler, str(self._folder), recursive=False)
        observer.start()
        self._observer = observer
        log.info("Watcher 시작: %s", self._folder)

    def stop(self) -> None:
        if self._observer is None:
            return
        self._observer.stop()
        self._observer.join(timeout=2.0)
        self._observer = None
        log.info("Watcher 정지")


class _Handler(FileSystemEventHandler):
    """watchdog 이벤트 → _process_one 위임."""

    def __init__(self, process_one: Callable[[Path], bool]) -> None:
        super().__init__()
        self._process_one = process_one

    def on_created(self, event) -> None:
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix.lower() == ".txt":
            self._process_one(path)

    def on_modified(self, event) -> None:
        # 일부 OS 에선 파일이 'modified' 로만 들어옴
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix.lower() == ".txt":
            self._process_one(path)
