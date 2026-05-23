"""방어선 2 — 검색·Alt+Enter 전의 채팅창 HWND 스냅샷.

이후 OpenChatroomStep 이 새로 열린 HWND 를 식별할 수 있게 현재 상태를 기록.
"""

from __future__ import annotations

import logging

from lovable_agent.output import window_spec
from lovable_agent.output.steps.base import SendContext, Step

log = logging.getLogger(__name__)


class SnapshotHwndsStep:
    name = "snapshot_hwnds"

    def execute(self, ctx: SendContext) -> None:
        snap = window_spec.snapshot_chat_hwnds(exclude={ctx.main_hwnd} if ctx.main_hwnd else None)
        ctx.hwnds_before_open = snap
        log.debug("스냅샷 (전) %d개: %s", len(snap), [hex(h) for h in snap])
        ctx.completed_steps.append(self.name)


_check: Step = SnapshotHwndsStep()
