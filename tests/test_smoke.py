"""Phase 1 smoke 테스트 — 코드 골조가 모순 없이 import / 동작하는가."""

from __future__ import annotations

from lovable_agent.config import load_config
from lovable_agent.domain import ExtractedTask, TaskStatus, WindowSpec
from lovable_agent.main import _run_dry_cycle, main
from lovable_agent.process.mock_client import MockLLMClient
from lovable_agent.safety.prefix import enforce_prefix
from lovable_agent.storage.mock_notion_repo import MockNotionRepository


def test_config_loads_defaults_when_no_file(tmp_path):
    """config.toml 이 없어도 기본값으로 로드되어야 함."""
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.safety.message_prefix == "[AI 자동 팔로우업] "
    assert cfg.scheduling.late_reminder_threshold_hours == 6


def test_prefix_enforcement_idempotent():
    """접두어 강제는 멱등 — 두 번 적용해도 한 번만 붙음."""
    once = enforce_prefix("테스트 메시지")
    twice = enforce_prefix(once)
    assert once == twice
    assert once.startswith("[AI 자동 팔로우업] ")


def test_mock_llm_returns_at_least_one_task():
    """MockLLMClient 는 비어있지 않은 텍스트에 대해 1개 이상 반환."""
    llm = MockLLMClient()
    result = llm.extract_tasks("내일까지 보고서 공유 부탁드립니다", [])
    assert len(result.tasks) >= 1
    assert isinstance(result.tasks[0], ExtractedTask)


def test_mock_notion_repo_seeded_state():
    """MockNotionRepository 는 dry-run 흐름에 의미를 주는 시드 데이터를 가짐."""
    repo = MockNotionRepository()
    assert len(repo.list_active_tasks()) >= 1
    assert len(repo.list_whitelisted_chatrooms()) >= 1
    assert repo.is_chatroom_whitelisted("MOP 운영방") is True
    assert repo.is_chatroom_whitelisted("존재하지 않는 톡방") is False


def test_dry_run_full_cycle_returns_zero():
    """Phase 1 완료 기준 — --dry-run 한 사이클이 정상 종료 (반환 0)."""
    llm = MockLLMClient()
    repo = MockNotionRepository()
    assert _run_dry_cycle(llm, repo, message_prefix="[AI 자동 팔로우업] ") == 0


def test_main_dry_run_argv_returns_zero():
    """main(['--dry-run']) 가 정상 종료해야 함 — entrypoint 통합."""
    assert main(["--dry-run"]) == 0


def test_window_spec_carries_defaults():
    """WindowSpec 은 카톡 PC 기본 클래스 이름들을 포함."""
    spec = WindowSpec(title_exact="테스트 톡방")
    assert spec.process_name == "KakaoTalk.exe"
    assert spec.expected_input_class == "RICHEDIT50W"


def test_task_status_enum_has_korean_labels():
    """TaskStatus 의 값은 노션 Status 컬럼과 1:1 매핑되어야 함."""
    assert TaskStatus.REVIEW_PENDING.value == "검토 대기"
    assert TaskStatus.CONFIRMED.value == "확정"
