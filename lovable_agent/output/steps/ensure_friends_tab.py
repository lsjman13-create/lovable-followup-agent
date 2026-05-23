"""방어선 1 — 친구 탭 강제 검증.

발송 시작 전 카톡 메인 창의 활성 탭이 '친구' 인지 확인.

자동 전환은 하지 않는다 (카톡 PC 가 사이드바 단축키 미제공). 친구 탭이 아니면
즉시 실패 → 사용자에게 친구 탭으로 이동하라고 알림.

채팅 탭 검색은 단톡방을 결과에 섞어내므로, 동명 단톡으로의 오발송을 막기 위해
친구 탭 고정 필수.
"""

from __future__ import annotations

import logging

from lovable_agent.output import window_spec
from lovable_agent.output.steps.base import SendContext, Step, StepError

log = logging.getLogger(__name__)


class EnsureFriendsTabStep:
    name = "ensure_friends_tab"

    def execute(self, ctx: SendContext) -> None:
        main_hwnd = window_spec.find_main_window()
        if main_hwnd is None:
            raise StepError(self.name, "카톡 메인 창을 찾을 수 없음 — 카톡 PC 실행/로그인 확인")
        ctx.main_hwnd = main_hwnd
        log.debug("메인 창 발견: hwnd=%s", hex(main_hwnd))

        if not window_spec.is_friends_tab_active(main_hwnd):
            raise StepError(
                self.name,
                "친구 탭이 활성 상태가 아님. 카톡 사이드바에서 친구 아이콘 클릭 후 재시도.",
            )
        ctx.completed_steps.append(self.name)


# 정적 타입 체크 — Step Protocol 준수
_check: Step = EnsureFriendsTabStep()
