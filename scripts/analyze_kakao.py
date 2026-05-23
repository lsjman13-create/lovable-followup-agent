"""카톡 .txt 익스포트 → Claude CLI 로 분석 → 4요소 추출 결과 표 출력.

API 키 없이 사용자 PC 의 로그인된 `claude` CLI 를 활용. Phase 3 의 분석·추출
흐름이 실제 AI 와 함께 동작하는지 검증.

사용법:
    uv run python scripts/analyze_kakao.py KakaoTalk_20260523_2138_09_263_group.txt
    uv run python scripts/analyze_kakao.py <파일> --output result.json
    uv run python scripts/analyze_kakao.py <파일> --recent 50  # 최근 N건만 분석

PII 주의:
- 카톡 .txt 본문에 사람 이름·업무 내용 다수 포함
- 결과 파일은 .gitignore (`docs/investigation-*.md`) 또는 임의 경로로
- 콘솔 출력에는 본문이 포함되므로 화면 공유 시 주의
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

with contextlib.suppress(Exception):
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from lovable_agent.domain import ExtractedTask, TaskSummary  # noqa: E402
from lovable_agent.ingest.kakao_parser import (  # noqa: E402
    format_for_llm,
    parse_kakao_file,
)
from lovable_agent.process.claude_cli_client import (  # noqa: E402
    ClaudeCLIClient,
    ensure_claude_cli_available,
)

log = logging.getLogger(__name__)


def _format_task_table(tasks: list[ExtractedTask]) -> str:
    """4요소 추출 결과를 사람이 읽기 좋은 마크다운 표로."""
    if not tasks:
        return "(추출된 업무 없음)"

    lines = ["| # | Title | What | Assignee | Due | Duplicate? |", "|---|---|---|---|---|---|"]
    for i, t in enumerate(tasks, 1):
        due_str = t.due_date.strftime("%Y-%m-%d %H:%M") if t.due_date else "—"
        dup = t.is_duplicate_of[:8] + "..." if t.is_duplicate_of else "—"
        lines.append(
            f"| {i} | {_cell(t.title, 40)} | {_cell(t.what, 50)} | "
            f"{_cell(t.assignee, 15)} | {due_str} | {dup} |"
        )
    return "\n".join(lines)


def _cell(text: str, width: int) -> str:
    """표 셀용 — 길면 자르고 줄바꿈은 공백으로."""
    one_line = text.replace("\n", " ").replace("|", "/").strip()
    if len(one_line) > width:
        return one_line[: width - 1] + "…"
    return one_line


def _format_task_detail(t: ExtractedTask) -> str:
    """단일 업무의 자세한 표시 (디버깅·검토용)."""
    parts = [
        f"  title    : {t.title}",
        f"  what     : {t.what}",
        f"  context  : {t.context}",
        f"  assignee : {t.assignee}",
        f"  due_date : {t.due_date.isoformat() if t.due_date else '미정'}",
    ]
    if t.is_duplicate_of:
        parts.append(f"  duplicate: {t.is_duplicate_of}")
    return "\n".join(parts)


def _tasks_to_json(tasks: list[ExtractedTask]) -> list[dict]:
    out = []
    for t in tasks:
        out.append(
            {
                "title": t.title,
                "what": t.what,
                "context": t.context,
                "assignee": t.assignee,
                "due_date": t.due_date.isoformat() if t.due_date else None,
                "is_duplicate_of": t.is_duplicate_of,
            }
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="analyze_kakao",
        description="카톡 .txt 익스포트를 Claude CLI 로 분석해 4요소 추출",
    )
    parser.add_argument("file", type=str, help="분석할 카톡 .txt 익스포트 파일")
    parser.add_argument(
        "--recent",
        type=int,
        default=0,
        help="최근 N건만 LLM 에 전달 (0=전체, 기본 0). 토큰 절약·노이즈 감소에 유용.",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="결과 JSON 파일 경로 (선택). 지정 시 stdout 으로도 같이 출력.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="claude CLI 호출 타임아웃 (초). 기본 120.",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    path = Path(args.file)
    if not path.exists():
        log.error("파일을 찾을 수 없음: %s", path)
        return 2

    # Claude CLI 사전 확인
    try:
        cli_path = ensure_claude_cli_available()
        log.info("Claude CLI 발견: %s", cli_path)
    except RuntimeError as e:
        log.error("%s", e)
        return 2

    # 1) 파싱
    log.info("파싱 시작: %s (%d bytes)", path.name, path.stat().st_size)
    messages = parse_kakao_file(path)
    log.info("파싱 완료 — %d개 메시지", len(messages))
    if not messages:
        log.warning("메시지 0건 — 빈 파일이거나 포맷 미인식. 종료.")
        return 1

    # 2) LLM 입력 형식화
    llm_input = format_for_llm(messages, max_messages=args.recent or 0)
    log.info("LLM 입력 텍스트 — %d자 (메시지 %d건)", len(llm_input), len(messages))

    # 3) Claude CLI 호출
    client = ClaudeCLIClient(timeout_sec=args.timeout)
    log.info("Claude CLI 호출 시작 (timeout=%.0fs) — 응답까지 수 초~수 분 소요", args.timeout)
    t0 = datetime.now()
    existing: list[TaskSummary] = []  # 시드 없음 (실 노션 통합은 Phase 4)
    result = client.extract_tasks(llm_input, existing)
    elapsed = (datetime.now() - t0).total_seconds()
    log.info("Claude CLI 응답 — %d건 추출 (%.1fs)", len(result.tasks), elapsed)

    # 4) 결과 표시
    print()
    print("=" * 80)
    print(f"카톡 분석 결과 — {path.name}")
    print(f"  메시지 {len(messages)}개 → 업무 {len(result.tasks)}건 추출 ({elapsed:.1f}초)")
    print("=" * 80)
    print()
    print(_format_task_table(result.tasks))
    print()
    print("=" * 80)
    print("상세")
    print("=" * 80)
    for i, t in enumerate(result.tasks, 1):
        print(f"\n[{i}]")
        print(_format_task_detail(t))

    # 5) 결과 JSON 저장 (선택)
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(_tasks_to_json(result.tasks), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.info("결과 JSON 저장: %s", out_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
