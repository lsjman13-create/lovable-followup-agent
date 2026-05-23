"""Claude Code CLI 기반 LLMClient 구현 — Anthropic API 키 없이 동작.

설계:
- 사용자 PC 에 이미 로그인된 `claude` CLI 를 subprocess 로 호출
- `claude -p "<프롬프트>"` 비대화형 모드 사용 (stdin 은 명시적으로 비움)
- 응답에서 JSON 블록을 파싱해 ExtractionResult 반환
- DECISIONS.md §8.2 의 "Claude Code 환경 직접 운영" 옵션의 부분 실현

vs AnthropicAPIClient (Phase 4):
- API 키 불필요
- 비용: 사용자의 Claude 구독에 청구 (월 30,000원 상한과 별개)
- 속도: subprocess 오버헤드로 API 직접 호출보다 느림 (~2~5초/호출)
- 안정성: CLI 인터페이스가 약속된 계약이 아닌 도구 — 향후 변경 가능
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from lovable_agent.domain import ExtractedTask, ExtractionResult, TaskSummary

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """당신은 카카오톡 대화에서 업무(액션 아이템)를 추출하는 도우미입니다.

다음 카톡 대화를 읽고, 발생한 업무를 4요소로 추출하세요:
- **title**: 한 줄 요약 (50자 이내)
- **what**: 구체적인 업무 내용
- **context**: 발생 맥락 (누가 누구에게, 왜)
- **due_date**: ISO 8601 datetime (예: "2026-06-01T15:00:00"). 명시 없으면 null.
- **assignee**: 업무 담당자 이름. 명시 없으면 "미정".

추출 원칙:
1. 단순 잡담·인사·이미지·이모티콘은 무시. 실행 가능한 업무만.
2. 기존 진행 중인 업무 목록(EXISTING_TASKS)이 주어진다. 비슷한 업무가 있다면
   `is_duplicate_of` 필드에 그 task_id 를 넣고, `context` 에 새로 받은 추가 맥락만 적어라.
3. 중복이 아니면 `is_duplicate_of: null`.

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

업무가 0건이면 `{"tasks": []}` 만 출력하세요."""


# Claude CLI 응답에서 JSON 블록을 안전하게 추출
_JSON_BLOCK = re.compile(r"\{[\s\S]*\}")


class ClaudeCLIClient:
    """LLMClient Protocol 구현 — `claude -p` 비대화형 호출."""

    def __init__(
        self,
        claude_executable: str | None = None,
        timeout_sec: float = 120.0,
        model: str | None = None,
    ) -> None:
        """
        Args:
            claude_executable: claude CLI 실행 파일 경로. None 이면 PATH 에서 자동 탐색.
            timeout_sec: 호출당 타임아웃.
            model: --model 옵션으로 전달할 모델명 (선택). None 이면 CLI 기본값.
        """
        if claude_executable is None:
            claude_executable = _resolve_claude_exe()
        self._exe = claude_executable
        self._timeout = timeout_sec
        self._model = model

    # ─── 공개 API ───
    def extract_tasks(self, text: str, existing_tasks: list[TaskSummary]) -> ExtractionResult:
        """LLMClient Protocol — 비정형 텍스트 → 4요소 추출."""
        if not text.strip():
            log.debug("빈 텍스트 — LLM 호출 스킵")
            return ExtractionResult(tasks=[])

        prompt = self._build_prompt(text, existing_tasks)
        response_text = self._invoke_claude(prompt)
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

    def _invoke_claude(self, prompt: str) -> str:
        """claude -p 비대화형 호출.

        프롬프트는 **stdin 으로 전달** — Windows 명령 라인 길이 제한(~8KB) 회피.
        CLI 의 prompt argument 는 비워두고, --input-format text 기본으로 stdin 수신.
        """
        cmd = [self._exe, "-p"]
        if self._model:
            cmd.extend(["--model", self._model])

        log.info("claude CLI 호출 — prompt %d자 (stdin), timeout %.0fs", len(prompt), self._timeout)
        try:
            result = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"claude CLI 타임아웃 ({self._timeout}s)") from e
        except OSError as e:
            raise RuntimeError(f"claude CLI 실행 실패: {e}") from e

        if result.returncode != 0:
            stderr_excerpt = (result.stderr or "")[:500]
            raise RuntimeError(f"claude CLI 실패 (exit={result.returncode}): {stderr_excerpt}")

        stdout = result.stdout or ""
        log.debug("claude 응답 — %d자", len(stdout))
        return stdout

    def _parse_response(self, response_text: str) -> ExtractionResult:
        """응답에서 JSON 블록을 찾아 ExtractionResult 로 변환."""
        match = _JSON_BLOCK.search(response_text)
        if not match:
            log.warning(
                "응답에서 JSON 블록을 찾지 못함 — 빈 결과 반환. 응답 일부: %r", response_text[:200]
            )
            return ExtractionResult(tasks=[])

        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as e:
            log.warning("JSON 파싱 실패: %s — 응답 일부: %r", e, match.group(0)[:200])
            return ExtractionResult(tasks=[])

        tasks_raw = data.get("tasks") if isinstance(data, dict) else None
        if not isinstance(tasks_raw, list):
            log.warning("응답에 'tasks' 배열이 없음 — 빈 결과 반환")
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
# 헬퍼
# ──────────────────────────────────────────────────────────────
def _parse_due_date(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        return None
    # ISO 8601, 흔한 변형 모두 시도
    candidates = [
        value,
        value.replace("Z", "+00:00"),
        value.split(".")[0],  # microsecond 제거
    ]
    for c in candidates:
        try:
            return datetime.fromisoformat(c)
        except ValueError:
            continue
    log.debug("due_date 파싱 실패: %r", value)
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


def _resolve_claude_exe() -> str:
    """PATH 에서 claude 찾고, npm shim(.CMD/.BAT) 이면 실제 .exe 경로로 변환.

    PATH 가 비어있을 때를 대비해 npm 표준 설치 경로 fallback 도 시도.
    """
    found = shutil.which("claude")
    if found is not None:
        path = Path(found)
        if path.suffix.upper() in (".CMD", ".BAT", ".PS1"):
            # npm shim 의 표준 위치
            exe_candidate = (
                path.parent
                / "node_modules"
                / "@anthropic-ai"
                / "claude-code"
                / "bin"
                / "claude.exe"
            )
            if exe_candidate.exists():
                log.debug("npm shim → exe 직접 경로로 변환: %s", exe_candidate)
                return str(exe_candidate)
        return str(path)

    # Fallback: PATH 에 없으면 npm 표준 설치 경로 직접 탐색 (PowerShell tool 의 PATH
    # reset 등에서 안정성 확보)
    npm_default = (
        Path.home()
        / "AppData"
        / "Roaming"
        / "npm"
        / "node_modules"
        / "@anthropic-ai"
        / "claude-code"
        / "bin"
        / "claude.exe"
    )
    if npm_default.exists():
        log.debug("PATH 미발견 — npm 표준 경로 fallback 사용: %s", npm_default)
        return str(npm_default)

    raise RuntimeError(
        "claude CLI 를 PATH 에서도, npm 표준 경로에서도 찾을 수 없음. "
        "https://docs.claude.com/en/docs/claude-code 참조"
    )


def ensure_claude_cli_available() -> Path:
    """Claude CLI 가 PATH 에 있는지 미리 확인 (스크립트 시작 시 호출)."""
    return Path(_resolve_claude_exe())
