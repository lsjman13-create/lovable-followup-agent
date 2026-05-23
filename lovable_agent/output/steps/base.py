"""Step Protocol — 카톡 자동 발송의 각 단계가 따르는 인터페이스.

설계 결정 (ARCHITECTURE §4.6.2):
- 각 Step 은 단일 책임 (활성화 / 검색 / 입력 / 전송 등).
- Step 간 데이터 전달은 `SendContext` 객체를 통해 — 단일 진실원.
- Step 실패 시 `StepError` 예외. 호출자가 어느 Step 에서 깨졌는지 추적 가능.
- 각 Step 은 단위 테스트 가능 — 의존성(win32, pyautogui)을 SendContext 또는
  모듈 수준 주입으로 분리.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from lovable_agent.domain import WindowSpec


class StepError(RuntimeError):
    """Step 실행 실패. 어느 Step 인지 식별하기 위해 step_name 필드."""

    def __init__(self, step_name: str, reason: str) -> None:
        super().__init__(f"[{step_name}] {reason}")
        self.step_name = step_name
        self.reason = reason


@dataclass
class SendContext:
    """발송 1회의 모든 상태 — Step 간 공유.

    Step 들은 SendContext 의 필드를 읽거나 채워가며 동작한다. 호출자는 모든 Step
    을 실행한 후 SendContext 의 결과를 확인.
    """

    target: WindowSpec  # 발송 대상 채팅창 식별 정보
    message: str  # 보낼 본문 (접두어 prepend 완료 상태)

    # 진행 중 채워지는 필드들
    main_hwnd: int | None = None
    hwnds_before_open: set[int] = field(default_factory=set)
    chat_hwnd: int | None = None
    richedit_hwnd: int | None = None

    # 메타 — 디버깅·이력용
    completed_steps: list[str] = field(default_factory=list)


class Step(Protocol):
    """카톡 발송 한 단계의 인터페이스."""

    name: str

    def execute(self, ctx: SendContext) -> None:
        """ctx 를 수정하며 단계 수행. 실패 시 StepError raise."""
        ...
