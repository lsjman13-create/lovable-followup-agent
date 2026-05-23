"""KakaoSender 오케스트레이션 통합 테스트 — fake Step 으로 흐름 검증."""

from __future__ import annotations

import pytest

from lovable_agent.domain import WindowSpec
from lovable_agent.output.kakao_sender import KakaoSender, default_steps
from lovable_agent.output.steps.base import SendContext, StepError


class _RecordingStep:
    """이름과 호출 여부만 기록하는 더미 Step."""

    def __init__(self, name: str, *, raise_error: bool = False) -> None:
        self.name = name
        self._raise = raise_error
        self.executed = False

    def execute(self, ctx: SendContext) -> None:
        self.executed = True
        if self._raise:
            raise StepError(self.name, f"{self.name} 의도된 실패")
        ctx.completed_steps.append(self.name)


def _target() -> WindowSpec:
    return WindowSpec(title_exact="김훈희")


# ──────────────────────────────────────────────────────────────
# 기본 동작
# ──────────────────────────────────────────────────────────────
def test_send_runs_all_steps_in_order():
    steps = [_RecordingStep(f"s{i}") for i in range(1, 7)]
    sender = KakaoSender(steps=steps)
    result = sender.send(_target(), "테스트 메시지")
    assert result.success is True
    assert result.completed_steps == ["s1", "s2", "s3", "s4", "s5", "s6"]
    assert all(s.executed for s in steps)


def test_send_returns_failed_step_on_error():
    steps = [
        _RecordingStep("ok1"),
        _RecordingStep("breakpoint", raise_error=True),
        _RecordingStep("ok2"),
    ]
    sender = KakaoSender(steps=steps)
    result = sender.send(_target(), "x")
    assert result.success is False
    assert result.failed_step == "breakpoint"
    assert result.completed_steps == ["ok1"]  # breakpoint 이후는 안 실행
    assert steps[2].executed is False
    assert "breakpoint" in (result.error_reason or "")


def test_send_handles_unexpected_exception():
    class _BoomStep:
        name = "boom"

        def execute(self, ctx: SendContext) -> None:
            raise RuntimeError("unexpected")

    sender = KakaoSender(steps=[_BoomStep()])
    result = sender.send(_target(), "x")
    assert result.success is False
    assert result.failed_step == "boom"
    assert "RuntimeError" in (result.error_reason or "")


# ──────────────────────────────────────────────────────────────
# 안전장치: 접두어 강제
# ──────────────────────────────────────────────────────────────
def test_send_prepends_prefix_to_message():
    """KakaoSender 내부에서 enforce_prefix 가 자동 적용 — Step 의 ctx.message 검증."""
    captured_message: list[str] = []

    class _CaptureStep:
        name = "capture"

        def execute(self, ctx: SendContext) -> None:
            captured_message.append(ctx.message)

    sender = KakaoSender(steps=[_CaptureStep()])
    sender.send(_target(), "보고서 부탁합니다")
    assert len(captured_message) == 1
    assert captured_message[0].startswith("[AI 자동 팔로우업] ")
    assert "보고서 부탁합니다" in captured_message[0]


def test_send_does_not_double_prefix():
    """이미 접두어 있는 메시지는 멱등 — 한 번만 prepend."""
    captured: list[str] = []

    class _CaptureStep:
        name = "capture"

        def execute(self, ctx: SendContext) -> None:
            captured.append(ctx.message)

    sender = KakaoSender(steps=[_CaptureStep()])
    sender.send(_target(), "[AI 자동 팔로우업] 이미 붙음")
    msg = captured[0]
    assert msg == "[AI 자동 팔로우업] 이미 붙음"  # 두 번 안 붙음


# ──────────────────────────────────────────────────────────────
# default_steps — 본구현 시퀀스 확인
# ──────────────────────────────────────────────────────────────
def test_default_steps_order():
    """기본 Step 시퀀스가 ARCHITECTURE §4.6.2 의 순서대로 구성되어야 함."""
    names = [s.name for s in default_steps()]
    assert names == [
        "ensure_friends_tab",  # 방어선 1
        "snapshot_hwnds",  # 방어선 2 (전)
        "open_chatroom",
        "verify_chatroom_title",  # 방어선 3
        "type_message",
        "press_enter",
    ]


# ──────────────────────────────────────────────────────────────
# 사고 시나리오 — 잘못된 톡방 검증
# ──────────────────────────────────────────────────────────────
def test_verify_failure_stops_before_typing():
    """제목 불일치 단계에서 멈춰 TypeMessage 가 실행되지 않아야 함 (오발송 방지)."""
    type_msg = _RecordingStep("type_message")
    press = _RecordingStep("press_enter")

    steps = [
        _RecordingStep("ensure_friends_tab"),
        _RecordingStep("snapshot_hwnds"),
        _RecordingStep("open_chatroom"),
        _RecordingStep("verify_chatroom_title", raise_error=True),  # 검증 실패
        type_msg,
        press,
    ]
    sender = KakaoSender(steps=steps)
    result = sender.send(_target(), "x")

    assert result.success is False
    assert result.failed_step == "verify_chatroom_title"
    # 핵심: type_message / press_enter 는 실행 안 됨 → 발송 사고 방지
    assert type_msg.executed is False
    assert press.executed is False


@pytest.mark.parametrize(
    "fail_at",
    [
        "ensure_friends_tab",
        "snapshot_hwnds",
        "open_chatroom",
        "verify_chatroom_title",
    ],
)
def test_any_pre_send_failure_blocks_typing(fail_at: str):
    """발송 직전 단계들 중 어디에서 실패하든 type_message 와 press_enter 는 안 실행."""
    type_msg = _RecordingStep("type_message")
    press = _RecordingStep("press_enter")

    steps_def = [
        _RecordingStep("ensure_friends_tab", raise_error=(fail_at == "ensure_friends_tab")),
        _RecordingStep("snapshot_hwnds", raise_error=(fail_at == "snapshot_hwnds")),
        _RecordingStep("open_chatroom", raise_error=(fail_at == "open_chatroom")),
        _RecordingStep("verify_chatroom_title", raise_error=(fail_at == "verify_chatroom_title")),
        type_msg,
        press,
    ]
    sender = KakaoSender(steps=steps_def)
    result = sender.send(_target(), "x")

    assert result.success is False
    assert result.failed_step == fail_at
    assert type_msg.executed is False
    assert press.executed is False
