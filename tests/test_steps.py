"""Step 6개 단위 테스트 — fake win32 + fake pyautogui 주입."""

from __future__ import annotations

import pytest

from lovable_agent.domain import WindowSpec
from lovable_agent.output import hwnd_utils
from lovable_agent.output.steps.base import SendContext, StepError
from lovable_agent.output.steps.ensure_friends_tab import EnsureFriendsTabStep
from lovable_agent.output.steps.open_chatroom import OpenChatroomStep
from lovable_agent.output.steps.press_enter import PressEnterStep
from lovable_agent.output.steps.snapshot_hwnds import SnapshotHwndsStep
from lovable_agent.output.steps.type_message import TypeMessageStep
from lovable_agent.output.steps.verify_chatroom_title import VerifyChatroomTitleStep
from tests.test_hwnd_utils import FakeWin32


def _make_kakao_state(*, friends_active: bool = True) -> FakeWin32:
    """카톡 정상 상태 모형. friends_active=False 면 친구 탭 비활성."""
    return FakeWin32(
        toplevels=[
            (0x100, "EVA_Window_Dblclk", "카카오톡"),
            (0x200, "EVA_Window_Dblclk", "김훈희"),
        ],
        children={
            0x100: [
                (
                    0x110,
                    "ContactListView_0",
                    "ContactListView_NORMAL",
                    (0, 0, 300, 800),
                    friends_active,
                ),
                (
                    0x130,
                    "ChatRoomListView_0",
                    "ChatRoomListView_NORMAL",
                    (0, 0, 300, 800),
                    not friends_active,
                ),
            ],
            0x110: [
                (0x111, "Edit", "", (0, 0, 200, 30), True),
            ],
            0x200: [
                (0x210, "RICHEDIT50W", "", (0, 0, 400, 100), True),
                (0x220, "EVA_VH_ListControl_Dblclk", "", (0, 0, 400, 600), True),
            ],
        },
        foreground=0x100,
    )


def _make_ctx(target_title: str = "김훈희") -> SendContext:
    return SendContext(
        target=WindowSpec(title_exact=target_title),
        message="[AI 자동 팔로우업] 테스트",
    )


# ──────────────────────────────────────────────────────────────
# EnsureFriendsTabStep
# ──────────────────────────────────────────────────────────────
def test_ensure_friends_tab_happy():
    fake = _make_kakao_state(friends_active=True)
    hwnd_utils.set_win32_module(fake)
    try:
        ctx = _make_ctx()
        EnsureFriendsTabStep().execute(ctx)
        assert ctx.main_hwnd == 0x100
        assert "ensure_friends_tab" in ctx.completed_steps
    finally:
        hwnd_utils.set_win32_module(None)


def test_ensure_friends_tab_fails_when_main_missing():
    fake = FakeWin32(toplevels=[(0x500, "tooltips_class32", "")])
    hwnd_utils.set_win32_module(fake)
    try:
        with pytest.raises(StepError, match="메인 창"):
            EnsureFriendsTabStep().execute(_make_ctx())
    finally:
        hwnd_utils.set_win32_module(None)


def test_ensure_friends_tab_fails_when_inactive():
    fake = _make_kakao_state(friends_active=False)
    hwnd_utils.set_win32_module(fake)
    try:
        with pytest.raises(StepError, match="친구 탭"):
            EnsureFriendsTabStep().execute(_make_ctx())
    finally:
        hwnd_utils.set_win32_module(None)


# ──────────────────────────────────────────────────────────────
# SnapshotHwndsStep
# ──────────────────────────────────────────────────────────────
def test_snapshot_records_existing_chats_excluding_main():
    fake = _make_kakao_state()
    hwnd_utils.set_win32_module(fake)
    try:
        ctx = _make_ctx()
        ctx.main_hwnd = 0x100
        SnapshotHwndsStep().execute(ctx)
        # 메인 제외 → 김훈희 채팅창 0x200 만
        assert ctx.hwnds_before_open == {0x200}
        assert "snapshot_hwnds" in ctx.completed_steps
    finally:
        hwnd_utils.set_win32_module(None)


# ──────────────────────────────────────────────────────────────
# OpenChatroomStep
# ──────────────────────────────────────────────────────────────
class _HotkeyLog:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __call__(self, *keys: str) -> None:
        self.calls.append(("hotkey", *keys))


class _PressLog:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, key: str) -> None:
        self.calls.append(key)


def test_open_chatroom_happy_uses_alt_enter():
    """초기 스냅샷에 없던 채팅창을 새로 열고, Ctrl+F + Enter + Alt+Enter 순서로 키를 보냄."""
    fake = _make_kakao_state()
    hwnd_utils.set_win32_module(fake)
    try:
        ctx = _make_ctx()
        ctx.main_hwnd = 0x100
        # before 스냅샷에서 김훈희(0x200)를 빼서 'open 직후 새로 발견되는 채팅창' 으로 시뮬레이션
        ctx.hwnds_before_open = set()  # 비어있다 가정

        hotkey = _HotkeyLog()
        press = _PressLog()
        OpenChatroomStep(
            sleep=lambda s: None,
            hotkey=hotkey,
            press=press,
        ).execute(ctx)

        # ctx.chat_hwnd 채워짐
        assert ctx.chat_hwnd == 0x200
        # 키 순서 검증
        assert ("hotkey", "ctrl", "f") in hotkey.calls
        assert ("hotkey", "ctrl", "v") in hotkey.calls
        assert ("hotkey", "alt", "enter") in hotkey.calls
        assert "enter" in press.calls
    finally:
        hwnd_utils.set_win32_module(None)


def test_open_chatroom_timeout_when_no_new_chat():
    """before 스냅샷에 0x200 이 이미 있어서 diff 가 비면 timeout 후 실패."""
    fake = _make_kakao_state()
    hwnd_utils.set_win32_module(fake)
    try:
        ctx = _make_ctx()
        ctx.main_hwnd = 0x100
        ctx.hwnds_before_open = {0x200}  # 이미 열려있다고 가정

        # 시계 mock — 즉시 deadline 초과
        clock_values = iter([0.0, 99.0])
        with pytest.raises(StepError, match="새 채팅창이 안 열림"):
            OpenChatroomStep(
                sleep=lambda s: None,
                clock=lambda: next(clock_values),
                hotkey=_HotkeyLog(),
                press=_PressLog(),
                new_chat_timeout_sec=1.0,
            ).execute(ctx)
    finally:
        hwnd_utils.set_win32_module(None)


# ──────────────────────────────────────────────────────────────
# VerifyChatroomTitleStep
# ──────────────────────────────────────────────────────────────
def test_verify_chatroom_title_happy():
    fake = _make_kakao_state()
    hwnd_utils.set_win32_module(fake)
    try:
        ctx = _make_ctx(target_title="김훈희")
        ctx.chat_hwnd = 0x200
        VerifyChatroomTitleStep().execute(ctx)
        assert ctx.richedit_hwnd == 0x210
    finally:
        hwnd_utils.set_win32_module(None)


def test_verify_chatroom_title_rejects_wrong_title_and_closes():
    """잘못된 채팅창을 열었으면 WM_CLOSE 로 닫고 실패."""
    fake = _make_kakao_state()
    hwnd_utils.set_win32_module(fake)
    try:
        ctx = _make_ctx(target_title="다른사람")
        ctx.chat_hwnd = 0x200  # 실제 제목은 '김훈희'
        with pytest.raises(StepError, match="매칭 실패"):
            VerifyChatroomTitleStep().execute(ctx)
        # WM_CLOSE 보낸 흔적 검증
        close_msgs = [m for m in fake.post_message_log if m[1] == 0x0010]
        assert (0x200, 0x0010, 0, 0) in close_msgs
    finally:
        hwnd_utils.set_win32_module(None)


# ──────────────────────────────────────────────────────────────
# TypeMessageStep
# ──────────────────────────────────────────────────────────────
def test_type_message_uses_clipboard_and_paste():
    """WM_SETTEXT 가 아니라 클립보드 + Ctrl+V 경로."""
    fake = _make_kakao_state()
    hwnd_utils.set_win32_module(fake)
    try:
        ctx = _make_ctx()
        ctx.chat_hwnd = 0x200
        ctx.richedit_hwnd = 0x210

        hotkey = _HotkeyLog()
        TypeMessageStep(sleep=lambda s: None, hotkey=hotkey).execute(ctx)

        assert ("hotkey", "ctrl", "v") in hotkey.calls
        # SetFocus 가 RichEdit 에 호출됐는지
        assert 0x210 in fake.set_focus_log
    finally:
        hwnd_utils.set_win32_module(None)


def test_type_message_fails_when_richedit_missing():
    fake = _make_kakao_state()
    hwnd_utils.set_win32_module(fake)
    try:
        ctx = _make_ctx()
        ctx.chat_hwnd = 0x200
        # richedit_hwnd 없음
        with pytest.raises(StepError, match="richedit_hwnd"):
            TypeMessageStep().execute(ctx)
    finally:
        hwnd_utils.set_win32_module(None)


# ──────────────────────────────────────────────────────────────
# PressEnterStep
# ──────────────────────────────────────────────────────────────
def test_press_enter_sends_enter_key():
    fake = _make_kakao_state()
    hwnd_utils.set_win32_module(fake)
    try:
        ctx = _make_ctx()
        ctx.chat_hwnd = 0x200
        ctx.richedit_hwnd = 0x210

        press = _PressLog()
        # verify_sent=False 로 RichEdit 텍스트 읽기 회피 (sys.platform 비-win32 환경 대응)
        PressEnterStep(sleep=lambda s: None, press=press, verify_sent=False).execute(ctx)

        assert "enter" in press.calls
    finally:
        hwnd_utils.set_win32_module(None)


def test_press_enter_fails_when_chat_hwnd_missing():
    fake = _make_kakao_state()
    hwnd_utils.set_win32_module(fake)
    try:
        ctx = _make_ctx()
        # chat_hwnd 없음
        with pytest.raises(StepError, match="chat_hwnd"):
            PressEnterStep().execute(ctx)
    finally:
        hwnd_utils.set_win32_module(None)
