"""ClaudeCLIClient 단위 테스트 — subprocess 호출은 mock 으로."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest

from lovable_agent.domain import TaskSummary
from lovable_agent.process.claude_cli_client import ClaudeCLIClient


class _MockResult:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


@pytest.fixture()
def client():
    return ClaudeCLIClient(claude_executable="fake-claude")


def _patch_run(stdout: str = "", returncode: int = 0):
    return patch(
        "lovable_agent.process.claude_cli_client.subprocess.run",
        return_value=_MockResult(stdout=stdout, returncode=returncode),
    )


# ──────────────────────────────────────────────────────────────
# 정상 응답
# ──────────────────────────────────────────────────────────────
def test_empty_text_skips_invocation(client):
    """빈 텍스트는 LLM 호출 자체 안 함."""
    with _patch_run(stdout="{}") as mock_run:
        result = client.extract_tasks("", [])
        assert result.tasks == []
        assert mock_run.call_count == 0


def test_parses_single_task_json(client):
    response = """\
다음과 같이 분석했습니다:
{
  "tasks": [
    {
      "title": "MOP 보고서 작성",
      "what": "8월 운영 결과 정리",
      "context": "김매니저가 박팀장에게 요청",
      "due_date": "2026-06-01T15:00:00",
      "assignee": "박팀장",
      "is_duplicate_of": null
    }
  ]
}
이상입니다."""
    with _patch_run(stdout=response):
        result = client.extract_tasks("아무 텍스트", [])
        assert len(result.tasks) == 1
        t = result.tasks[0]
        assert t.title == "MOP 보고서 작성"
        assert t.assignee == "박팀장"
        assert t.due_date == datetime(2026, 6, 1, 15, 0)
        assert t.is_duplicate_of is None


def test_parses_empty_tasks_array(client):
    with _patch_run(stdout='{"tasks": []}'):
        result = client.extract_tasks("아무 텍스트", [])
        assert result.tasks == []


def test_handles_duplicate_task(client):
    response = '{"tasks": [{"title": "중복", "what": "이미 있음", "context": "추가 맥락", "due_date": null, "assignee": "X", "is_duplicate_of": "abc123"}]}'
    with _patch_run(stdout=response):
        result = client.extract_tasks("아무 텍스트", [])
        assert result.tasks[0].is_duplicate_of == "abc123"


def test_handles_null_due_date(client):
    response = '{"tasks": [{"title": "T", "what": "W", "context": "C", "due_date": null, "assignee": "A", "is_duplicate_of": null}]}'
    with _patch_run(stdout=response):
        result = client.extract_tasks("아무 텍스트", [])
        assert result.tasks[0].due_date is None


# ──────────────────────────────────────────────────────────────
# 견고성 — 망가진 응답 안전 처리
# ──────────────────────────────────────────────────────────────
def test_no_json_block_returns_empty(client):
    """응답에 JSON 이 전혀 없으면 빈 결과 반환 (예외 X)."""
    with _patch_run(stdout="죄송합니다 분석할 수 없습니다."):
        result = client.extract_tasks("아무 텍스트", [])
        assert result.tasks == []


def test_invalid_json_returns_empty(client):
    with _patch_run(stdout="{tasks: [malformed"):
        result = client.extract_tasks("아무 텍스트", [])
        assert result.tasks == []


def test_missing_tasks_key_returns_empty(client):
    """tasks 키 없는 JSON 도 안전하게 빈 결과."""
    with _patch_run(stdout='{"foo": "bar"}'):
        result = client.extract_tasks("아무 텍스트", [])
        assert result.tasks == []


def test_partial_task_item_uses_defaults(client):
    """일부 필드가 빠진 항목도 안전 변환."""
    response = '{"tasks": [{"title": "T", "what": "W"}]}'
    with _patch_run(stdout=response):
        result = client.extract_tasks("아무 텍스트", [])
        assert len(result.tasks) == 1
        assert result.tasks[0].assignee == "미정"
        assert result.tasks[0].due_date is None


# ──────────────────────────────────────────────────────────────
# 프롬프트 생성
# ──────────────────────────────────────────────────────────────
def test_prompt_includes_existing_tasks(client):
    """기존 진행중 업무가 프롬프트(stdin)에 포함되는지 — 중복 판별 정확성에 영향."""
    existing = [
        TaskSummary(
            task_id="abc123",
            title="기존 업무",
            assignee="김매니저",
            due_date=datetime(2026, 6, 1),
            one_line_summary="x",
        )
    ]
    captured_input: list[str] = []

    def fake_run(cmd, **kwargs):
        captured_input.append(kwargs.get("input", ""))
        return _MockResult(stdout='{"tasks": []}')

    with patch(
        "lovable_agent.process.claude_cli_client.subprocess.run",
        side_effect=fake_run,
    ):
        client.extract_tasks("아무 텍스트", existing)

    assert len(captured_input) == 1
    prompt = captured_input[0]
    assert "abc123" in prompt
    assert "기존 업무" in prompt
    assert "김매니저" in prompt


# ──────────────────────────────────────────────────────────────
# 실패 처리
# ──────────────────────────────────────────────────────────────
def test_nonzero_exit_raises(client):
    with _patch_run(returncode=1), pytest.raises(RuntimeError, match="claude CLI 실패"):
        client.extract_tasks("아무 텍스트", [])
