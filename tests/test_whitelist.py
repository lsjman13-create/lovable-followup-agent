"""WhitelistChecker 단위 테스트 — 캐시·노션 불일치 케이스 포함."""

from __future__ import annotations

import pytest

from lovable_agent.domain import WindowSpec
from lovable_agent.safety.whitelist import WhitelistChecker
from lovable_agent.storage.mock_notion_repo import MockNotionRepository
from lovable_agent.storage.sqlite_repo import SqliteRepository


@pytest.fixture()
def sqlite():
    r = SqliteRepository(":memory:")
    yield r
    r.close()


@pytest.fixture()
def notion():
    return MockNotionRepository()


@pytest.fixture()
def checker(sqlite, notion):
    return WhitelistChecker(sqlite=sqlite, notion=notion, cache_ttl_seconds=300)


# ──────────────────────────────────────────────────────────────
# 정상 흐름
# ──────────────────────────────────────────────────────────────
def test_allows_chatroom_present_in_both_cache_and_notion(checker):
    """MockNotionRepository 시드에 'MOP 운영방' 이 등록되어 있음."""
    result = checker.check("MOP 운영방")
    assert result.allowed is True
    assert "일치" in result.reason


def test_blocks_chatroom_not_in_notion(checker):
    result = checker.check("존재하지 않는 톡방")
    assert result.allowed is False
    assert "미등록" in result.reason or "차단" in result.reason


def test_blocks_empty_chatroom_name(checker):
    result = checker.check("")
    assert result.allowed is False
    result2 = checker.check("   ")
    assert result2.allowed is False


# ──────────────────────────────────────────────────────────────
# 캐시 동작
# ──────────────────────────────────────────────────────────────
def test_first_check_refreshes_cache(sqlite, notion):
    """초기에 캐시는 비어있고, 첫 check 호출이 자동으로 캐시를 채워야 함."""
    assert sqlite.is_chatroom_in_cache("MOP 운영방") is False  # 초기 캐시 비어있음
    checker = WhitelistChecker(sqlite=sqlite, notion=notion)
    checker.check("MOP 운영방")
    assert sqlite.is_chatroom_in_cache("MOP 운영방") is True  # 갱신됨


def test_cache_used_when_fresh(sqlite, notion):
    """캐시가 신선하면 노션 호출 횟수가 줄어듦 — 호출 카운트로 검증."""
    checker = WhitelistChecker(sqlite=sqlite, notion=notion, cache_ttl_seconds=300)

    # 노션 호출을 카운트하기 위해 monkey patch
    call_count = {"list": 0, "is": 0}
    orig_list = notion.list_whitelisted_chatrooms
    orig_is = notion.is_chatroom_whitelisted

    def counting_list():
        call_count["list"] += 1
        return orig_list()

    def counting_is(t):
        call_count["is"] += 1
        return orig_is(t)

    notion.list_whitelisted_chatrooms = counting_list  # type: ignore[method-assign]
    notion.is_chatroom_whitelisted = counting_is  # type: ignore[method-assign]

    checker.check("MOP 운영방")  # 첫 호출: list 1회 (캐시 갱신) + is 1회
    checker.check("MOP 운영방")  # 두 번째: 캐시 신선하니 list 호출 안함, is 만
    assert call_count["list"] == 1
    assert call_count["is"] == 2  # 두 번째 캐시 사용에도 노션 원본은 매번 확인


# ──────────────────────────────────────────────────────────────
# 캐시-노션 불일치
# ──────────────────────────────────────────────────────────────
def test_blocks_when_cache_has_but_notion_doesnt(sqlite, notion):
    """캐시엔 있지만 노션에 없는 (캐시 stale) 경우 — 안전 측에서 차단."""
    # 캐시에만 임의로 넣고 노션엔 없게
    sqlite.cache_whitelist([WindowSpec(title_exact="phantom 톡방")])
    assert sqlite.is_chatroom_in_cache("phantom 톡방") is True
    assert notion.is_chatroom_whitelisted("phantom 톡방") is False

    checker = WhitelistChecker(sqlite=sqlite, notion=notion, cache_ttl_seconds=999)
    result = checker.check("phantom 톡방")
    assert result.allowed is False
    assert "불일치" in result.reason


# ──────────────────────────────────────────────────────────────
# 사고 시나리오 — PRD R7 (동명 톡방 오발송)
# ──────────────────────────────────────────────────────────────
def test_blocks_unknown_chatroom_even_if_similar_name(checker):
    """'MOP 운영방' 만 등록 — 비슷한 'MOP 운영' 은 차단."""
    assert checker.check("MOP 운영").allowed is False
    assert checker.check("MOP 운영방 ").allowed is False  # 끝 공백
    assert checker.check(" MOP 운영방").allowed is False  # 앞 공백
    # 완전일치만 통과
    assert checker.check("MOP 운영방").allowed is True
