"""카톡 .txt 익스포트 파일 파서.

대상 포맷 (우선순위):
1. **Windows PC 카톡** (주 대상)
   ```
   --------------- 2026년 5월 23일 토요일 ---------------
   [김매니저] [오전 10:30] 다음 주까지 보고서 부탁드립니다
   [나] [오전 10:31] 네, 알겠습니다
   ```
2. **안드로이드 모바일 카톡** (보조)
   ```
   2026년 5월 23일 오전 10:30, 김매니저 : 다음 주까지 보고서 부탁드립니다
   2026년 5월 23일 오전 10:31, 나 : 네, 알겠습니다
   ```

다음은 무시:
- 시스템 메시지 (`...님이 들어왔습니다`, `...님이 나갔습니다`)
- 첨부파일·이모티콘 placeholder (`<사진>`, `<이모티콘>` 등) — 메시지 자체는 유지하되 본문이 placeholder뿐이면 스킵
- 빈 줄, 헤더 (`저장한 날짜`, `대화내용`)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# ──────────────────────────────────────────────────────────────
# 정규식 — 두 포맷 모두 처리
# ──────────────────────────────────────────────────────────────
# PC: 날짜 구분선
_PC_DATE_LINE = re.compile(
    r"-+\s*(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일\s*(?:[월화수목금토일]요일)?\s*-+"
)
# PC: [발화자] [오전/오후 H:MM] 메시지
_PC_MESSAGE = re.compile(
    r"^\[(?P<speaker>[^\]]+)\]\s*\[(?P<ampm>오전|오후)\s+(?P<hour>\d{1,2}):(?P<minute>\d{2})\]\s*(?P<body>.*)$"
)
# 모바일: YYYY년 M월 D일 오전 H:MM, 발화자 : 메시지
_MOBILE_MESSAGE = re.compile(
    r"^(?P<year>\d{4})년\s+(?P<month>\d{1,2})월\s+(?P<day>\d{1,2})일\s+(?P<ampm>오전|오후)\s+(?P<hour>\d{1,2}):(?P<minute>\d{2}),\s*(?P<speaker>[^:]+?)\s*:\s*(?P<body>.*)$"
)

# 시스템 메시지 패턴 (정확한 매칭이 아니라 키워드 기반)
_SYSTEM_PATTERNS = (
    re.compile(r"님이\s*(들어왔|나갔|초대했)"),
    re.compile(r"^저장한 날짜\s*:"),
    re.compile(r"^대화내용$"),
)
# 본문이 이것뿐이면 의미 없는 첨부물로 보고 스킵
_PLACEHOLDER_ONLY = re.compile(r"^\s*<(사진|이모티콘|동영상|파일|음성메시지|지도)>\s*$")


@dataclass(frozen=True)
class KakaoMessage:
    """파싱된 카톡 메시지 한 줄."""

    timestamp: datetime
    speaker: str
    body: str


def _ampm_to_24h(ampm: str, hour: int) -> int:
    """'오전 12:30' → 0:30, '오후 1:00' → 13:00 변환."""
    if ampm == "오전":
        return 0 if hour == 12 else hour
    return 12 if hour == 12 else hour + 12


def _is_system_line(line: str) -> bool:
    return any(p.search(line) for p in _SYSTEM_PATTERNS)


def parse_kakao_text(text: str) -> list[KakaoMessage]:
    """카톡 .txt 본문 텍스트를 메시지 리스트로 파싱.

    Args:
        text: 카톡 익스포트 .txt 의 전체 내용 (utf-8).

    Returns:
        시스템 메시지·placeholder 만 있는 줄을 제외한 KakaoMessage 리스트.
        파싱 실패한 줄은 직전 메시지에 줄바꿈으로 이어붙임 (멀티라인 메시지 대응).
    """
    messages: list[KakaoMessage] = []
    current_date: tuple[int, int, int] | None = None  # (Y, M, D) — PC 포맷 전용

    for raw in text.splitlines():
        line = raw.rstrip("\r\n")
        if not line.strip():
            continue
        if _is_system_line(line):
            continue

        # PC: 날짜 구분선
        m = _PC_DATE_LINE.search(line)
        if m:
            current_date = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
            continue

        # PC 메시지
        m = _PC_MESSAGE.match(line)
        if m and current_date is not None:
            hour24 = _ampm_to_24h(m.group("ampm"), int(m.group("hour")))
            ts = datetime(
                current_date[0], current_date[1], current_date[2], hour24, int(m.group("minute"))
            )
            body = m.group("body").strip()
            if _PLACEHOLDER_ONLY.match(body):
                continue
            messages.append(
                KakaoMessage(timestamp=ts, speaker=m.group("speaker").strip(), body=body)
            )
            continue

        # 모바일 메시지
        m = _MOBILE_MESSAGE.match(line)
        if m:
            hour24 = _ampm_to_24h(m.group("ampm"), int(m.group("hour")))
            ts = datetime(
                int(m.group("year")),
                int(m.group("month")),
                int(m.group("day")),
                hour24,
                int(m.group("minute")),
            )
            body = m.group("body").strip()
            if _PLACEHOLDER_ONLY.match(body):
                continue
            messages.append(
                KakaoMessage(timestamp=ts, speaker=m.group("speaker").strip(), body=body)
            )
            continue

        # 매칭 안 됨 — 직전 메시지 본문에 줄바꿈으로 이어붙임 (멀티라인 메시지)
        if messages:
            last = messages[-1]
            messages[-1] = KakaoMessage(
                timestamp=last.timestamp,
                speaker=last.speaker,
                body=last.body + "\n" + line.strip(),
            )
        # 직전 메시지가 없는 헤더 영역은 그냥 무시

    return messages


def parse_kakao_file(path: str | Path) -> list[KakaoMessage]:
    """파일 경로 버전 — utf-8 / cp949 양쪽 시도."""
    p = Path(path)
    for encoding in ("utf-8", "utf-8-sig", "cp949"):
        try:
            return parse_kakao_text(p.read_text(encoding=encoding))
        except UnicodeDecodeError:
            continue
    # 모든 인코딩 실패 시 errors='replace' 로 마지막 시도
    return parse_kakao_text(p.read_text(encoding="utf-8", errors="replace"))


def format_for_llm(messages: list[KakaoMessage], max_messages: int = 50) -> str:
    """LLM 프롬프트에 넣기 좋은 형식으로 직렬화 — 최근 N건만."""
    selected = messages[-max_messages:] if max_messages > 0 else messages
    lines = []
    for m in selected:
        lines.append(f"[{m.timestamp:%Y-%m-%d %H:%M}] {m.speaker}: {m.body}")
    return "\n".join(lines)
