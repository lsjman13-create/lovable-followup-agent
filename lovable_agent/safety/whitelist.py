"""화이트리스트 더블체크 — ARCHITECTURE §4.6.3 의 사전 검증 단계.

발송 직전 두 곳을 모두 조회해서 **둘 다 일치할 때만** 통과:
1. SQLite 캐시 (`whitelist_cache` 테이블) — 빠른 일상 조회
2. 노션 원본 (`NotionRepository`) — 캐시 갱신 후 한 번 더 검증

캐시 TTL 을 넘긴 경우 자동으로 갱신 후 다시 검증.

PRD §NFR-1.1 (Sev-1) — 화이트리스트 미일치 발송 차단의 핵심 코드.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from lovable_agent.storage.repository import NotionRepository
from lovable_agent.storage.sqlite_repo import SqliteRepository

log = logging.getLogger(__name__)

DEFAULT_CACHE_TTL_SECONDS = 300  # 5분


@dataclass(frozen=True)
class WhitelistCheckResult:
    """체크 결과 — 발송 진행 / 차단 / 차단 사유."""

    allowed: bool
    chatroom: str
    reason: str  # 사람이 읽을 수 있는 사유 (로그·알림용)


class WhitelistChecker:
    """캐시 + 노션 원본 더블체크."""

    def __init__(
        self,
        sqlite: SqliteRepository,
        notion: NotionRepository,
        cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
    ) -> None:
        self._sqlite = sqlite
        self._notion = notion
        self._ttl = cache_ttl_seconds

    def _refresh_cache(self) -> None:
        log.info("화이트리스트 캐시 갱신 시작")
        entries = self._notion.list_whitelisted_chatrooms()
        self._sqlite.cache_whitelist(entries)
        log.info("화이트리스트 캐시 갱신 완료 — %d개 톡방", len(entries))

    def _is_cache_fresh(self) -> bool:
        age = self._sqlite.whitelist_cache_age_seconds()
        if age is None:
            return False
        return age <= self._ttl

    def check(self, chatroom_title: str) -> WhitelistCheckResult:
        """톡방 발송 가능 여부를 더블체크.

        절차:
        1. 캐시가 신선하면 캐시 조회. 미일치면 즉시 차단.
        2. 캐시가 stale 이거나 캐시에 있어도, **노션 원본을 한 번 더 조회**.
        3. 노션 원본에도 있으면 통과. 캐시-노션 불일치 시 차단 (안전 측 선택).
        """
        if not chatroom_title or not chatroom_title.strip():
            return WhitelistCheckResult(
                allowed=False,
                chatroom=chatroom_title,
                reason="톡방명이 비어있음 — 발송 차단",
            )

        # Step 1: 캐시
        if not self._is_cache_fresh():
            self._refresh_cache()

        in_cache = self._sqlite.is_chatroom_in_cache(chatroom_title)
        if not in_cache:
            return WhitelistCheckResult(
                allowed=False,
                chatroom=chatroom_title,
                reason=f"캐시에 톡방 '{chatroom_title}' 미등록 — 발송 차단",
            )

        # Step 2: 노션 원본 더블체크 (캐시가 stale 한 경우 대비)
        in_notion = self._notion.is_chatroom_whitelisted(chatroom_title)
        if not in_notion:
            log.warning(
                "캐시-노션 불일치 — 캐시엔 있지만 노션 원본엔 없음: %s. 안전 측에서 차단.",
                chatroom_title,
            )
            return WhitelistCheckResult(
                allowed=False,
                chatroom=chatroom_title,
                reason="캐시-노션 불일치 (캐시 ✓ / 노션 ✗) — 안전 측에서 차단",
            )

        return WhitelistCheckResult(
            allowed=True,
            chatroom=chatroom_title,
            reason="캐시·노션 모두 일치",
        )
