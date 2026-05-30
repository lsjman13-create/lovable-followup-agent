"""Ollama 연결·추출 품질 빠른 점검.

사용법:
    uv run python scripts/check_ollama.py
    uv run python scripts/check_ollama.py --kakao-txt KakaoTalk_xxx.txt

수행 내용:
1) /api/tags 로 서버 reachability + 설치된 모델 목록 조회
2) config.toml 의 [llm] 섹션에서 모델 이름 확인 (없으면 환경변수 사용)
3) 합성 카톡 텍스트 (또는 --kakao-txt 로 전달된 실 텍스트) 로 1회 추출 시도
4) 소요시간 + 추출된 업무 개수 + 첫 항목 미리보기 출력
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Windows cp949 콘솔에서 한글·유니코드 대시 출력 보장
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# scripts/ 에서 직접 실행될 때 import path 보정
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lovable_agent.config import load_config  # noqa: E402
from lovable_agent.ingest.kakao_parser import format_for_llm, parse_kakao_text  # noqa: E402
from lovable_agent.process.ollama_client import OllamaClient, is_ollama_reachable  # noqa: E402

_FAKE_KAKAO_TXT = """\
--------------- 2026년 5월 23일 토요일 ---------------
[김매니저] [오전 10:30] 다음 주 수요일까지 MOP 8월 운영 보고서 초안 부탁드립니다
[나] [오전 10:31] 네, 알겠습니다. 5월 27일까지 공유드릴게요
[김매니저] [오전 10:32] 회의 내용도 같이 정리해 주세요
[박팀장] [오후 2:00] 6월 첫 주 GGE 일정 확정해주세요
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Ollama 연결 점검 및 추출 검증")
    parser.add_argument(
        "--kakao-txt",
        type=str,
        default=None,
        help="실 KakaoTalk_*.txt 파일 경로 (없으면 내장 샘플 사용)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="모델 override — 미지정 시 config.toml [llm] ollama_model 사용",
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default=None,
        help="Ollama 서버 URL override — 미지정 시 config.toml 사용",
    )
    args = parser.parse_args()

    config = load_config()
    model = args.model or config.llm.ollama_model
    base_url = args.base_url or config.llm.ollama_base_url

    print(f"[1/3] Ollama 서버 ping — {base_url}")
    if not is_ollama_reachable(base_url):
        print("     ✗ 연결 실패. `ollama serve` 또는 Ollama 앱 실행 확인.")
        return 2
    print("     ✓ 응답 OK")

    print(f"[2/3] 모델 — {model}")
    print("     설치된 모델 목록 확인은 `ollama list` 로 직접 확인하세요.")

    if args.kakao_txt:
        text = Path(args.kakao_txt).read_text(encoding="utf-8")
        messages = parse_kakao_text(text)
        prompt_text = format_for_llm(messages)
        print(f"     입력: {args.kakao_txt} — {len(messages)}개 메시지, {len(prompt_text)}자")
    else:
        prompt_text = _FAKE_KAKAO_TXT
        print(f"     입력: 내장 샘플 ({len(prompt_text)}자)")

    print("[3/3] 1회 추출 시도 — 모델이 클수록 오래 걸립니다…")
    client = OllamaClient(
        model=model,
        base_url=base_url,
        timeout_sec=config.llm.ollama_timeout_sec,
        use_json_format=config.llm.ollama_use_json_format,
    )

    started = time.monotonic()
    try:
        result = client.extract_tasks(prompt_text, [])
    except RuntimeError as e:
        print(f"     ✗ 호출 실패: {e}")
        return 3
    elapsed = time.monotonic() - started

    print(f"     ✓ 완료 — {elapsed:.1f}초, 추출 {len(result.tasks)}건")
    for i, t in enumerate(result.tasks[:3], 1):
        due = t.due_date.isoformat() if t.due_date else "미정"
        print(f"     [{i}] {t.title} | {t.assignee} | due={due}")
        if t.what:
            print(f"         what: {t.what[:80]}")
    if len(result.tasks) > 3:
        print(f"     … 외 {len(result.tasks) - 3}건")

    if not result.tasks:
        print()
        print("⚠ 추출 0건. 가능한 원인:")
        print("  - 모델이 한국어 추론에 약함 → exaone3.5:7.8b / qwen2.5:7b 권장")
        print("  - 입력 텍스트에 실제 업무가 없거나 잡담만 있음")
        print("  - JSON 응답 형식 불일치 → use_json_format=false 로 재시도")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
