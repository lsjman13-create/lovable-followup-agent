"""[AI 자동 팔로우업] 접두어 강제 — 단순하지만 NFR-1.3 Sev-1 사고 방지."""

from __future__ import annotations

DEFAULT_PREFIX = "[AI 자동 팔로우업] "


def enforce_prefix(message: str, prefix: str = DEFAULT_PREFIX) -> str:
    """접두어가 없으면 붙이고, 이미 있으면 그대로 둔다.

    Why: 외부 모듈 어디서든 메시지를 만들 때 실수로 접두어를 빠뜨려도 발송 직전
    이 함수를 통과시키면 차단됨. 발송 경로의 단일 진입점에서 호출.
    """
    return message if message.startswith(prefix) else prefix + message
