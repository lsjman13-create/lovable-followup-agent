"""카톡 파서 단위 테스트."""

from __future__ import annotations

from datetime import datetime

from lovable_agent.ingest.kakao_parser import (
    KakaoMessage,
    format_for_llm,
    parse_kakao_text,
)

# ──────────────────────────────────────────────────────────────
# PC 카톡 포맷
# ──────────────────────────────────────────────────────────────
PC_SAMPLE = """\
저장한 날짜 : 2026-05-23 12:00:00
대화내용

--------------- 2026년 5월 23일 토요일 ---------------
[김매니저] [오전 10:30] 다음 주 수요일까지 MOP 보고서 부탁드립니다
[나] [오전 10:31] 네, 알겠습니다
[김매니저] [오전 10:32] <사진>
[김매니저] [오전 10:33] 이 자료 참고하시면 됩니다
--------------- 2026년 5월 24일 일요일 ---------------
[박팀장] [오후 2:00] 미팅 잡혔습니다
"""


def test_pc_format_extracts_messages_with_correct_timestamps():
    messages = parse_kakao_text(PC_SAMPLE)
    # 시스템 헤더 / placeholder-only 메시지(<사진>) 제외하고 4건
    assert len(messages) == 4
    assert messages[0] == KakaoMessage(
        timestamp=datetime(2026, 5, 23, 10, 30),
        speaker="김매니저",
        body="다음 주 수요일까지 MOP 보고서 부탁드립니다",
    )
    assert messages[3].speaker == "박팀장"
    assert messages[3].timestamp == datetime(2026, 5, 24, 14, 0)


def test_pc_format_filters_placeholder_only_lines():
    """<사진>, <이모티콘> 등 본문이 placeholder뿐인 메시지는 제외."""
    messages = parse_kakao_text(PC_SAMPLE)
    bodies = [m.body for m in messages]
    assert "<사진>" not in bodies


def test_pc_format_ampm_conversion():
    """오전 12시 → 0시, 오후 12시 → 12시 변환 확인."""
    sample = (
        "--------------- 2026년 5월 23일 토요일 ---------------\n"
        "[A] [오전 12:30] 자정 직후\n"
        "[B] [오후 12:30] 정오 직후\n"
        "[C] [오후 11:59] 자정 직전\n"
    )
    msgs = parse_kakao_text(sample)
    assert msgs[0].timestamp.hour == 0
    assert msgs[1].timestamp.hour == 12
    assert msgs[2].timestamp.hour == 23


# ──────────────────────────────────────────────────────────────
# 모바일 포맷
# ──────────────────────────────────────────────────────────────
MOBILE_SAMPLE = """\
2026년 5월 23일 오전 10:30, 김매니저 : 다음 주 수요일까지 부탁드립니다
2026년 5월 23일 오전 10:31, 나 : 네, 알겠습니다
2026년 5월 23일 오후 2:00, 박팀장 : 미팅 잡혔습니다
"""


def test_mobile_format_basic():
    messages = parse_kakao_text(MOBILE_SAMPLE)
    assert len(messages) == 3
    assert messages[0].speaker == "김매니저"
    assert messages[2].timestamp == datetime(2026, 5, 23, 14, 0)


# ──────────────────────────────────────────────────────────────
# 멀티라인 메시지
# ──────────────────────────────────────────────────────────────
def test_continuation_lines_attached_to_previous():
    sample = (
        "--------------- 2026년 5월 23일 토요일 ---------------\n"
        "[김매니저] [오전 10:30] 다음 사항을 정리해주세요\n"
        "1. 일정\n"
        "2. 담당자\n"
        "3. 마감일\n"
        "[나] [오전 10:31] 네\n"
    )
    msgs = parse_kakao_text(sample)
    assert len(msgs) == 2
    assert "1. 일정" in msgs[0].body
    assert "3. 마감일" in msgs[0].body
    assert msgs[1].body == "네"


# ──────────────────────────────────────────────────────────────
# 시스템 메시지 필터
# ──────────────────────────────────────────────────────────────
def test_system_messages_filtered():
    sample = (
        "--------------- 2026년 5월 23일 토요일 ---------------\n"
        "이승준님이 들어왔습니다.\n"
        "[김매니저] [오전 10:30] 안녕하세요\n"
        "박팀장님이 나갔습니다.\n"
    )
    msgs = parse_kakao_text(sample)
    assert len(msgs) == 1
    assert msgs[0].speaker == "김매니저"


# ──────────────────────────────────────────────────────────────
# format_for_llm
# ──────────────────────────────────────────────────────────────
def test_format_for_llm_truncates_to_max_messages():
    sample_lines = ["--------------- 2026년 5월 23일 토요일 ---------------"]
    for i in range(20):
        sample_lines.append(f"[A] [오전 10:{i:02d}] message {i}")
    msgs = parse_kakao_text("\n".join(sample_lines))
    assert len(msgs) == 20

    rendered = format_for_llm(msgs, max_messages=5)
    rendered_lines = rendered.splitlines()
    assert len(rendered_lines) == 5
    # 마지막 5건 (15~19) 이 포함
    assert "message 19" in rendered_lines[-1]
    assert "message 15" in rendered_lines[0]


def test_empty_text_returns_empty_list():
    assert parse_kakao_text("") == []
    assert parse_kakao_text("\n\n\n") == []
