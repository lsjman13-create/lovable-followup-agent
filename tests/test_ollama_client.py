"""OllamaClient 단위 테스트 — fake httpx Client 주입."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from lovable_agent.domain import TaskSummary
from lovable_agent.process.ollama_client import OllamaClient


# ──────────────────────────────────────────────────────────────
# Fake httpx Client
# ──────────────────────────────────────────────────────────────
class _FakeResponse:
    def __init__(self, json_data: dict, status_code: int = 200) -> None:
        self._json = json_data
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._json


class _FakeHttp:
    """httpx.Client 의 .post() 만 mock — generate API 호출 시뮬레이션."""

    def __init__(self, ollama_response_text: str = '{"tasks": []}', status_code: int = 200) -> None:
        self._response_text = ollama_response_text
        self.status_code = status_code
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, json: dict, timeout: float = 0) -> _FakeResponse:
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        # Ollama /api/generate 형식 응답
        return _FakeResponse(
            {"model": json.get("model"), "response": self._response_text, "done": True},
            status_code=self.status_code,
        )


def _make_client(
    response_text: str = '{"tasks": []}', use_json_format: bool = True
) -> tuple[OllamaClient, _FakeHttp]:
    fake = _FakeHttp(ollama_response_text=response_text)
    client = OllamaClient(
        model="test-model",
        base_url="http://test-ollama:11434",
        timeout_sec=10,
        use_json_format=use_json_format,
        http_client=fake,
    )
    return client, fake


# ──────────────────────────────────────────────────────────────
# 정상 응답
# ──────────────────────────────────────────────────────────────
def test_empty_text_skips_invocation():
    fake = _FakeHttp()
    client = OllamaClient(http_client=fake)
    result = client.extract_tasks("", [])
    assert result.tasks == []
    assert fake.calls == []


def test_parses_single_task_json():
    response = (
        '{"tasks": [{"title": "MOP 보고서 작성", "what": "8월 운영 결과 정리", '
        '"context": "김매니저 요청", "due_date": "2026-06-01T15:00:00", '
        '"assignee": "박팀장", "is_duplicate_of": null}]}'
    )
    client, fake = _make_client(response_text=response)
    result = client.extract_tasks("아무 텍스트", [])
    assert len(result.tasks) == 1
    t = result.tasks[0]
    assert t.title == "MOP 보고서 작성"
    assert t.assignee == "박팀장"
    assert t.due_date == datetime(2026, 6, 1, 15, 0)


def test_parses_empty_tasks_array():
    client, _ = _make_client(response_text='{"tasks": []}')
    result = client.extract_tasks("아무 텍스트", [])
    assert result.tasks == []


def test_handles_duplicate_task():
    response = (
        '{"tasks": [{"title": "중복", "what": "이미", "context": "추가", '
        '"due_date": null, "assignee": "X", "is_duplicate_of": "abc"}]}'
    )
    client, _ = _make_client(response_text=response)
    result = client.extract_tasks("아무 텍스트", [])
    assert result.tasks[0].is_duplicate_of == "abc"


# ──────────────────────────────────────────────────────────────
# 견고성 — 망가진 응답
# ──────────────────────────────────────────────────────────────
def test_extracts_json_block_from_noisy_response():
    """format=json 미지원 모델 — 응답 앞뒤에 노이즈가 있어도 JSON 추출."""
    response = (
        "응답 시작합니다.\n"
        '여기 결과: {"tasks": [{"title": "T", "what": "W", "context": "C", '
        '"due_date": null, "assignee": "A", "is_duplicate_of": null}]}\n'
        "이상입니다."
    )
    client, _ = _make_client(response_text=response, use_json_format=False)
    result = client.extract_tasks("아무 텍스트", [])
    assert len(result.tasks) == 1
    assert result.tasks[0].title == "T"


def test_no_json_returns_empty():
    client, _ = _make_client(response_text="죄송합니다 분석할 수 없습니다")
    result = client.extract_tasks("아무 텍스트", [])
    assert result.tasks == []


def test_invalid_json_returns_empty():
    client, _ = _make_client(response_text="{tasks: [malformed")
    result = client.extract_tasks("아무 텍스트", [])
    assert result.tasks == []


def test_missing_tasks_key_returns_empty():
    client, _ = _make_client(response_text='{"foo": "bar"}')
    result = client.extract_tasks("아무 텍스트", [])
    assert result.tasks == []


def test_partial_task_uses_defaults():
    response = '{"tasks": [{"title": "T", "what": "W"}]}'
    client, _ = _make_client(response_text=response)
    result = client.extract_tasks("아무 텍스트", [])
    assert len(result.tasks) == 1
    assert result.tasks[0].assignee == "미정"


# ──────────────────────────────────────────────────────────────
# HTTP 호출 검증
# ──────────────────────────────────────────────────────────────
def test_http_call_targets_generate_endpoint():
    client, fake = _make_client()
    client.extract_tasks("test", [])
    assert len(fake.calls) == 1
    assert fake.calls[0]["url"] == "http://test-ollama:11434/api/generate"


def test_http_body_includes_model_and_prompt():
    client, fake = _make_client()
    client.extract_tasks("test 입력", [])
    body = fake.calls[0]["json"]
    assert body["model"] == "test-model"
    assert "test 입력" in body["prompt"]
    assert body["stream"] is False


def test_format_json_flag_added_when_enabled():
    client, fake = _make_client(use_json_format=True)
    client.extract_tasks("test", [])
    assert fake.calls[0]["json"].get("format") == "json"


def test_format_json_flag_omitted_when_disabled():
    client, fake = _make_client(use_json_format=False)
    client.extract_tasks("test", [])
    assert "format" not in fake.calls[0]["json"]


def test_prompt_includes_existing_tasks():
    existing = [
        TaskSummary(
            task_id="abc123",
            title="기존 업무",
            assignee="김매니저",
            due_date=datetime(2026, 6, 1),
            one_line_summary="x",
        )
    ]
    client, fake = _make_client()
    client.extract_tasks("아무 텍스트", existing)
    prompt = fake.calls[0]["json"]["prompt"]
    assert "abc123" in prompt
    assert "기존 업무" in prompt
    assert "김매니저" in prompt


# ──────────────────────────────────────────────────────────────
# 실패 처리 — 서버 연결 불가
# ──────────────────────────────────────────────────────────────
def test_os_error_wrapped_as_runtime_error():
    """connection refused → 친절한 에러 메시지로 변환."""

    class _BrokenHttp:
        def post(self, url: str, json: dict, timeout: float = 0) -> Any:  # noqa: ARG002
            raise OSError("Connection refused")

    client = OllamaClient(http_client=_BrokenHttp())
    with pytest.raises(RuntimeError, match="Ollama 서버 연결 실패"):
        client.extract_tasks("test", [])
