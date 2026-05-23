"""방어선 1 — 메인 창 + 탭 활성 검증.

발송 시작 전 카톡 메인 창을 찾고, `expected_tab` 에 따라:
- "friends" (기본): 친구 탭 활성 강제 — 단톡방 혼재 결과 방지
- "chats": 채팅 탭 활성 강제 — '나와의 채팅' 같이 친구로 안 잡히는 톡방 대응
- "any": 친구 또는 채팅 탭 중 하나만 활성이면 통과
- None: 탭 검증 안 함 (메인 창 발견만)

자동 전환은 하지 않는다 (카톡 PC 가 사이드바 단축키 미제공). 잘못된 탭이면
즉시 실패 → 사용자에게 사이드바 클릭 안내.
"""

from __future__ import annotations

import logging
from typing import Literal

from lovable_agent.output import window_spec
from lovable_agent.output.steps.base import SendContext, Step, StepError

log = logging.getLogger(__name__)

ExpectedTab = Literal["friends", "chats", "any"]


class EnsureFriendsTabStep:
    """Step Protocol 호환 — 이름은 호환성 유지(friends 가 기본).

    실제 동작은 `expected_tab` 파라미터에 따라 친구/채팅/둘 다 OK 로 분기.
    """

    name = "ensure_friends_tab"

    def __init__(self, expected_tab: ExpectedTab | None = "friends") -> None:
        self._expected: ExpectedTab | None = expected_tab

    def execute(self, ctx: SendContext) -> None:
        main_hwnd = window_spec.find_main_window()
        if main_hwnd is None:
            raise StepError(self.name, "카톡 메인 창을 찾을 수 없음 — 카톡 PC 실행/로그인 확인")
        ctx.main_hwnd = main_hwnd
        log.debug("메인 창 발견: hwnd=%s", hex(main_hwnd))

        if self._expected is None:
            ctx.completed_steps.append(self.name)
            return

        friends_active = window_spec.is_friends_tab_active(main_hwnd)
        chats_active = window_spec.is_chats_tab_active(main_hwnd)

        if self._expected == "friends" and not friends_active:
            raise StepError(
                self.name,
                "친구 탭이 활성 상태가 아님. 카톡 사이드바에서 친구 아이콘 클릭 후 재시도.",
            )
        if self._expected == "chats" and not chats_active:
            raise StepError(
                self.name,
                "채팅 탭이 활성 상태가 아님. 카톡 사이드바에서 채팅 아이콘 클릭 후 재시도.",
            )
        if self._expected == "any" and not (friends_active or chats_active):
            raise StepError(
                self.name,
                "친구·채팅 탭 모두 비활성. 카톡 사이드바에서 친구 또는 채팅 아이콘 클릭 후 재시도.",
            )
        ctx.completed_steps.append(self.name)


# 정적 타입 체크 — Step Protocol 준수
_check: Step = EnsureFriendsTabStep()
