"""로컬 LLM (Ollama) 기반 LLMClient 구현 — PII 외부 유출 0.

설계:
- Ollama HTTP API (http://localhost:11434) 호출
- `format=json` 옵션으로 JSON 강제 (모델이 지원할 때)
- 정규식 JSON 블록 추출 fallback (구식 모델용)
- DECISIONS §8.2 의 "Claude Code 환경 직접 운영" 옵션의 또 다른 변형

vs AnthropicAPIClient / ClaudeCLIClient:
- 비용: 무료 (전기료만)
- PII: 본인 PC 안에서 처리 — 외부 유출 0
- 속도: 모델 크기·GPU 유무에 따라 5초~수 분
- 품질: 모델 의존. 한국어 카톡: EXAONE 3.5 / Solar / Qwen 2.5 권장
- 사전 조건: Ollama 설치 + `ollama pull <모델>` 1회

사용자 셋업 (1회):
    winget install Ollama.Ollama
    ollama pull exaone3.5:7.8b   # 또는 다른 한국어 모델

사용 (`OllamaClient.extract_tasks` 호출):
    llm = OllamaClient(model="exaone3.5:7.8b")
    result = llm.extract_tasks(text, existing_tasks)
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any

from lovable_agent.domain import ExtractedTask, ExtractionResult, TaskSummary

log = logging.getLogger(__name__)


# Ollama HTTP API 의 generate endpoint
DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_MODEL = "exaone3.5:7.8b"  # 한국어 카톡에 적합한 LG 한국 모델 권장
DEFAULT_TIMEOUT_SEC = 300.0  # 로컬 추론은 느릴 수 있음


SYSTEM_PROMPT = """당신은 카카오톡 대화에서 업무(액션 아이템)를 추출하는 도우미입니다.

다음 카톡 대화를 읽고, 발생한 업무를 4요소로 추출하세요:
- title: 한 줄 요약 (50자 이내)
- what: 구체적인 업무 내용
- context: 발생 맥락 (누가 누구에게, 왜)
- due_date: ISO 8601 datetime (예: "2026-06-01T15:00:00"). 명시 없으면 null.
- assignee: 업무 담당자 이름. 명시 없으면 "미정".

추출 원칙:
1. 단순 잡담·인사·이미지·이모티콘은 무시. 실행 가능한 업무만.
2. 기존 진행 중인 업무 목록(EXISTING_TASKS)이 주어진다. 비슷한 업무가 있다면
   is_duplicate_of 필드에 그 task_id 를 넣고, context 에 새로 받은 추가 맥락만 적어라.
3. 중복이 아니면 is_duplicate_of: null.

응답 형식 — 반드시 다음 JSON 만 출력 (다른 텍스트·코드 블록 금지):

{
  "tasks": [
    {
      "title": "...",
      "what": "...",
      "context": "...",
      "due_date": "2026-06-01T15:00:00" | null,
      "assignee": "...",
      "is_duplicate_of": null | "<task_id>"
    }
  ]
}

업무가 0건이면 {"tasks": []} 만 출력하세요."""


_JSON_BLOCK = re.compile(r"\{[\s\S]*\}")


class OllamaClient:
    """LLMClient Protocol 구현 — Ollama HTTP API 호출."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_OLLAMA_URL,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        use_json_format: bool = True,
        http_client: Any = None,
    ) -> None:
        """
        Args:
            model: Ollama 모델 이름 (예: "exaone3.5:7.8b", "llama3.1:8b").
            base_url: Ollama 서버 URL.
            timeout_sec: 호출당 타임아웃.
            use_json_format: Ollama 의 format=json 옵션 사용 (모델이 지원할 때).
                구식·작은 모델은 무시할 수 있음 → 응답에서 정규식 JSON 추출 fallback.
            http_client: 테스트용 fake httpx Client 주입.
        """
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_sec
        self._use_json_format = use_json_format
        self._http = http_client

    # ─── 공개 API ───
    def extract_tasks(self, text: str, existing_tasks: list[TaskSummary]) -> ExtractionResult:
        if not text.strip():
            return ExtractionResult(tasks=[])

        prompt = self._build_prompt(text, existing_tasks)
        response_text = self._invoke_ollama(prompt)
        return self._parse_response(response_text)

    # ─── 내부 ───
    def _build_prompt(self, text: str, existing_tasks: list[TaskSummary]) -> str:
        existing_block = "(없음)"
        if existing_tasks:
            lines = []
            for t in existing_tasks:
                due_str = t.due_date.isoformat() if t.due_date else "미정"
                lines.append(
                    f'- task_id={t.task_id}, title="{t.title}", '
                    f'assignee="{t.assignee}", due={due_str}'
                )
            existing_block = "\n".join(lines)

        return (
            f"{SYSTEM_PROMPT}\n\n"
            f"=== EXISTING_TASKS (진행 중인 업무) ===\n{existing_block}\n\n"
            f"=== KAKAO_CONVERSATION ===\n{text}\n\n"
            f"=== 응답 (JSON 만) ===\n"
        )

    def _invoke_ollama(self, prompt: str) -> str:
        """Ollama /api/generate 호출. format=json 으로 JSON 강제 시도."""
        body = {
            "model": self._model,
            "prompt": prompt,
            "stream": False,
        }
        if self._use_json_format:
            body["format"] = "json"

        log.info(
            "Ollama 호출 — model=%s, prompt %d자, timeout %.0fs",
            self._model,
            len(prompt),
            self._timeout,
        )

        try:
            data = self._post_json(f"{self._base_url}/api/generate", body)
        except OSError as e:
            raise RuntimeError(
                f"Ollama 서버 연결 실패 ({self._base_url}). "
                f"`ollama serve` 또는 Ollama 앱 실행 확인. 원인: {e}"
            ) from e

        response_text = data.get("response", "")
        log.debug("Ollama 응답 — %d자", len(response_text))
        return response_text

    def _post_json(self, url: str, body: dict) -> dict:
        """HTTP POST + JSON 파싱. http_client 가 주입되면 그것 사용, 아니면 httpx."""
        if self._http is not None:
            response = self._http.post(url, json=body, timeout=self._timeout)
            response.raise_for_status()
            return response.json()
        # lazy import — httpx 는 notion-client 의 transitive 의존
        import httpx

        with httpx.Client(timeout=self._timeout) as client:
            response = client.post(url, json=body)
            response.raise_for_status()
            return response.json()

    def _parse_response(self, response_text: str) -> ExtractionResult:
        """응답에서 JSON 블록을 찾아 ExtractionResult 로 변환.

        format=json 사용 시 응답 전체가 JSON. 그래도 안전을 위해 JSON 블록 추출.
        """
        if not response_text.strip():
            log.warning("Ollama 응답이 빈 문자열 — 빈 결과 반환")
            return ExtractionResult(tasks=[])

        # 1차: 응답 전체가 JSON 인지 시도 (format=json 케이스)
        try:
            data = json.loads(response_text)
        except json.JSONDecodeError:
            # 2차: JSON 블록 정규식 추출 (구식 모델 fallback)
            match = _JSON_BLOCK.search(response_text)
            if not match:
                log.warning(
                    "Ollama 응답에서 JSON 블록 못 찾음 — 응답 일부: %r",
                    response_text[:200],
                )
                return ExtractionResult(tasks=[])
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError as e:
                log.warning("JSON 파싱 실패: %s — 응답 일부: %r", e, match.group(0)[:200])
                return ExtractionResult(tasks=[])

        tasks_raw = data.get("tasks") if isinstance(data, dict) else None
        if not isinstance(tasks_raw, list):
            log.warning("Ollama 응답에 'tasks' 배열이 없음 — 빈 결과 반환")
            return ExtractionResult(tasks=[])

        tasks: list[ExtractedTask] = []
        for item in tasks_raw:
            if not isinstance(item, dict):
                continue
            try:
                tasks.append(_dict_to_extracted_task(item))
            except Exception as e:  # noqa: BLE001 — 단일 항목 실패가 전체 망치지 않게
                log.warning("ExtractedTask 변환 실패 — 항목 스킵: %s, item=%r", e, item)
        return ExtractionResult(tasks=tasks)


# ──────────────────────────────────────────────────────────────
# 헬퍼 (ClaudeCLIClient 와 동일 — 향후 공통 모듈로 분리 가능)
# ──────────────────────────────────────────────────────────────
def _parse_due_date(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        return None
    candidates = [value, value.replace("Z", "+00:00"), value.split(".")[0]]
    for c in candidates:
        try:
            return datetime.fromisoformat(c)
        except ValueError:
            continue
    return None


def _dict_to_extracted_task(d: dict) -> ExtractedTask:
    return ExtractedTask(
        title=str(d.get("title") or "").strip() or "(제목 없음)",
        what=str(d.get("what") or "").strip(),
        context=str(d.get("context") or "").strip(),
        due_date=_parse_due_date(d.get("due_date")),
        assignee=str(d.get("assignee") or "미정").strip(),
        source="kakao",
        source_detail="",
        is_duplicate_of=(d.get("is_duplicate_of") or None) or None,
    )


def is_ollama_reachable(base_url: str = DEFAULT_OLLAMA_URL, timeout: float = 3.0) -> bool:
    """Ollama 서버가 응답 가능한지 빠른 ping — 운영 시작 시 사전 점검용."""
    try:
        import httpx

        with httpx.Client(timeout=timeout) as client:
            response = client.get(f"{base_url.rstrip('/')}/api/tags")
            return response.status_code == 200
    except Exception:  # noqa: BLE001
        return False
