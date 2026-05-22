"""SQLite 운영 상태 저장소.

ARCHITECTURE §5.2 의 4개 테이블 (processed_files, send_queue, send_history,
whitelist_cache) + schema_version 으로 마이그레이션 추적.

설계 결정:
- ORM 미사용 — 표준 sqlite3 + 직접 SQL. 의존성·복잡도 최소화 (DECISIONS 참조)
- 마이그레이션은 lovable_agent/storage/migrations/NNN_*.sql 파일 순서대로 적용
- 외래키 활성화 (`PRAGMA foreign_keys = ON`)
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from lovable_agent.domain import SendQueueItem, WindowSpec

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


# ──────────────────────────────────────────────────────────────
# datetime ↔ TEXT 어댑터 등록 (Python 3.12+ 의 deprecated 기본 어댑터 회피)
# ──────────────────────────────────────────────────────────────
def _adapt_datetime(dt: datetime) -> str:
    return dt.isoformat(sep=" ")


def _convert_timestamp(value: bytes) -> datetime:
    return datetime.fromisoformat(value.decode("utf-8"))


sqlite3.register_adapter(datetime, _adapt_datetime)
sqlite3.register_converter("timestamp", _convert_timestamp)


class SqliteRepository:
    """카톡 발송 큐·이력·중복 방지를 위한 SQLite 저장소."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        """
        Args:
            db_path: DB 파일 경로. None 또는 ':memory:' 면 인메모리 DB.
        """
        if db_path is None or str(db_path) == ":memory:":
            self._db_path = ":memory:"
        else:
            p = Path(db_path).expanduser()
            p.parent.mkdir(parents=True, exist_ok=True)
            self._db_path = str(p)

        # in-memory DB 는 연결이 닫히면 사라지므로 하나의 연결을 계속 들고 있어야 함
        self._persistent_conn: sqlite3.Connection | None = None
        if self._db_path == ":memory:":
            self._persistent_conn = self._connect_raw()

        self._apply_migrations()

    # ─── 내부 유틸 ───
    def _connect_raw(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self._db_path,
            detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def _conn(self):
        if self._persistent_conn is not None:
            # in-memory — 연결 재사용
            yield self._persistent_conn
            self._persistent_conn.commit()
        else:
            conn = self._connect_raw()
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    # ─── 마이그레이션 ───
    def _current_version(self, conn: sqlite3.Connection) -> int:
        try:
            row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
            return row["v"] or 0
        except sqlite3.OperationalError:
            # schema_version 테이블 자체가 없음 = 초기 상태
            return 0

    def _apply_migrations(self) -> None:
        with self._conn() as conn:
            current = self._current_version(conn)
            files = sorted(MIGRATIONS_DIR.glob("*.sql"))
            for f in files:
                version = int(f.name.split("_", 1)[0])
                if version <= current:
                    continue
                conn.executescript(f.read_text(encoding="utf-8"))
                conn.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (version, datetime.now()),
                )

    # ─── processed_files ───
    def is_file_processed(self, file_hash: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM processed_files WHERE file_hash = ?", (file_hash,)
            ).fetchone()
            return row is not None

    def mark_file_processed(self, file_hash: str, file_name: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO processed_files (file_hash, file_name, processed_at) "
                "VALUES (?, ?, ?)",
                (file_hash, file_name, datetime.now()),
            )

    # ─── send_queue ───
    def enqueue_send(self, item: SendQueueItem) -> int:
        with self._conn() as conn:
            cursor = conn.execute(
                "INSERT INTO send_queue (task_id, chatroom_title, message, scheduled_at, "
                "status, attempted_count) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    item.task_id,
                    item.chatroom.title_exact,
                    item.message,
                    item.scheduled_at,
                    item.status,
                    item.attempted_count,
                ),
            )
            return int(cursor.lastrowid)

    def list_pending(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM send_queue WHERE status = 'queued' ORDER BY scheduled_at LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def update_send_status(
        self,
        send_queue_id: int,
        status: str,
        increment_attempt: bool = False,
    ) -> None:
        with self._conn() as conn:
            if increment_attempt:
                conn.execute(
                    "UPDATE send_queue SET status = ?, last_attempted_at = ?, "
                    "attempted_count = attempted_count + 1 WHERE id = ?",
                    (status, datetime.now(), send_queue_id),
                )
            else:
                conn.execute(
                    "UPDATE send_queue SET status = ? WHERE id = ?",
                    (status, send_queue_id),
                )

    def is_already_queued(self, task_id: str, scheduled_at: datetime) -> bool:
        """같은 task_id + 같은 발송 시점이 이미 큐에 있는지 — 중복 enqueue 방지."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM send_queue WHERE task_id = ? AND scheduled_at = ?",
                (task_id, scheduled_at),
            ).fetchone()
            return row is not None

    # ─── send_history ───
    def record_send_attempt(
        self,
        send_queue_id: int,
        success: bool,
        error_detail: str | None = None,
        screenshot_path: str | None = None,
    ) -> int:
        with self._conn() as conn:
            cursor = conn.execute(
                "INSERT INTO send_history (send_queue_id, sent_at, success, error_detail, "
                "screenshot_path) VALUES (?, ?, ?, ?, ?)",
                (
                    send_queue_id,
                    datetime.now(),
                    success,
                    error_detail,
                    screenshot_path,
                ),
            )
            return int(cursor.lastrowid)

    def list_history(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM send_history ORDER BY sent_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    # ─── whitelist_cache ───
    def cache_whitelist(self, entries: list[WindowSpec]) -> None:
        """노션 화이트리스트를 캐시로 갱신 — 전체 교체."""
        with self._conn() as conn:
            conn.execute("DELETE FROM whitelist_cache")
            now = datetime.now()
            conn.executemany(
                "INSERT INTO whitelist_cache (chatroom_title, active, cached_at) VALUES (?, ?, ?)",
                [(spec.title_exact, True, now) for spec in entries],
            )

    def is_chatroom_in_cache(self, title_exact: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM whitelist_cache WHERE chatroom_title = ? AND active = 1",
                (title_exact,),
            ).fetchone()
            return row is not None

    def whitelist_cache_age_seconds(self) -> float | None:
        """가장 오래된 캐시 엔트리의 경과 시간(초). 캐시 비어있으면 None."""
        with self._conn() as conn:
            row = conn.execute("SELECT MIN(cached_at) AS oldest FROM whitelist_cache").fetchone()
            if not row or row["oldest"] is None:
                return None
            oldest = row["oldest"]
            if isinstance(oldest, str):
                oldest = datetime.fromisoformat(oldest)
            return (datetime.now() - oldest).total_seconds()

    def close(self) -> None:
        if self._persistent_conn is not None:
            self._persistent_conn.close()
            self._persistent_conn = None
