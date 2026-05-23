"""검색 + 채팅창 별도 창 오픈.

흐름:
1. 메인 창 포그라운드 확보
2. Ctrl+F 검색창 활성화
3. 검색 Edit HWND 직접 클리어 + 검색어 클립보드·Ctrl+V
4. 검색 결과 첫 항목 선택 + 별도 창 오픈 (`open_method` 에 따라 다름)
5. HWND diff 로 새로 생긴 채팅창 식별 (방어선 2 의 두 번째 단계)

`open_method` 변형 (카톡 PC 버전·환경마다 다름):
- "alt_enter" — Enter + Alt+Enter (kakao-sender v2 기본, 일부 버전)
- "enter_only" — Enter 한 번만 (일부 버전은 Enter 만으로도 별도 창)
- "double_click" — 검색 결과 영역 좌표에 win32 더블클릭 메시지 전송
  (Alt+Enter / Enter 가 안 먹는 본인 환경 fallback)

이 Step 의 책임은 "채팅창을 열고 ctx.chat_hwnd 를 채우는 것" 까지. 제목 검증은
VerifyChatroomTitleStep 이 따로 담당 (방어선 3).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Literal

from lovable_agent.output import hwnd_utils, window_spec
from lovable_agent.output.steps.base import SendContext, Step, StepError

log = logging.getLogger(__name__)

# 기본 타이밍 — 카톡 응답 시간 고려해 보수적으로
DEFAULT_AFTER_CTRL_F_SEC = 0.4
DEFAULT_AFTER_PASTE_SEC = 0.6
DEFAULT_AFTER_ENTER_SEC = 0.25
DEFAULT_NEW_CHAT_TIMEOUT_SEC = 3.0
DEFAULT_POLL_INTERVAL_SEC = 0.1

OpenMethod = Literal["alt_enter", "enter_only", "double_click"]


class OpenChatroomStep:
    name = "open_chatroom"

    def __init__(
        self,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        hotkey: Callable[..., None] | None = None,
        press: Callable[[str], None] | None = None,
        after_ctrl_f_sec: float = DEFAULT_AFTER_CTRL_F_SEC,
        after_paste_sec: float = DEFAULT_AFTER_PASTE_SEC,
        after_enter_sec: float = DEFAULT_AFTER_ENTER_SEC,
        new_chat_timeout_sec: float = DEFAULT_NEW_CHAT_TIMEOUT_SEC,
        open_method: OpenMethod = "alt_enter",
    ) -> None:
        self._sleep = sleep
        self._clock = clock
        self._hotkey = hotkey
        self._press = press
        self._after_ctrl_f = after_ctrl_f_sec
        self._after_paste = after_paste_sec
        self._after_enter = after_enter_sec
        self._new_chat_timeout = new_chat_timeout_sec
        self._open_method = open_method

    def _get_hotkey(self) -> Callable[..., None]:
        if self._hotkey is not None:
            return self._hotkey
        import pyautogui  # type: ignore

        pyautogui.FAILSAFE = False
        return pyautogui.hotkey

    def _get_press(self) -> Callable[[str], None]:
        if self._press is not None:
            return self._press
        import pyautogui  # type: ignore

        return pyautogui.press

    def execute(self, ctx: SendContext) -> None:
        if ctx.main_hwnd is None:
            raise StepError(self.name, "main_hwnd 가 채워지지 않음 — EnsureFriendsTabStep 먼저")

        hwnd_utils.force_foreground(ctx.main_hwnd)
        self._sleep(0.15)

        # 1) Ctrl+F
        self._get_hotkey()("ctrl", "f")
        self._sleep(self._after_ctrl_f)

        # 2) 검색 Edit 찾기 (활성 탭 자동 감지 — 친구·채팅 둘 다 지원) + 클리어 + 검색어 입력
        search_edit = window_spec.find_search_edit_in_active_tab(ctx.main_hwnd)
        if search_edit is None:
            raise StepError(self.name, "활성 탭(친구/채팅)의 검색 Edit 을 찾지 못함")

        hwnd_utils.clear_edit(search_edit)
        hwnd_utils.set_focus(search_edit)
        hwnd_utils.set_clipboard_text(ctx.target.title_exact)
        self._sleep(0.05)
        self._get_hotkey()("ctrl", "v")
        self._sleep(self._after_paste)

        # 진단: 검색 Edit 의 실제 입력값 검증
        actual = hwnd_utils.get_edit_text(search_edit)
        log.info(
            "검색 Edit HWND=%s, 의도=%r, 실제 입력=%r",
            hex(search_edit),
            ctx.target.title_exact,
            actual,
        )
        if actual != ctx.target.title_exact:
            log.warning(
                "검색창 입력값 불일치 — 클립보드/포커스 문제 가능성. 의도=%r vs 실제=%r",
                ctx.target.title_exact,
                actual,
            )

        # 진단: 활성 탭의 목록 컨트롤 발견 여부
        list_hwnd = window_spec.find_active_list_control(ctx.main_hwnd)
        log.info(
            "활성 탭 목록 컨트롤(EVA_VH_ListControl_Dblclk): %s",
            hex(list_hwnd) if list_hwnd else "없음",
        )

        # 3) 별도 창 오픈 (open_method 분기)
        if self._open_method == "alt_enter":
            self._get_press()("enter")
            self._sleep(self._after_enter)
            self._get_hotkey()("alt", "enter")
        elif self._open_method == "enter_only":
            self._get_press()("enter")
            self._sleep(self._after_enter)
        elif self._open_method == "double_click":
            # 친구 목록 첫 항목 영역 좌표 추정 후 win32 WM_LBUTTONDBLCLK 전송
            self._double_click_first_result(ctx.main_hwnd)
        else:
            raise StepError(self.name, f"알 수 없는 open_method: {self._open_method!r}")

        # 4) HWND diff 대기
        new_hwnd = self._wait_new_chat(ctx.hwnds_before_open, ctx.main_hwnd)
        if new_hwnd is None:
            raise StepError(
                self.name,
                f"Alt+Enter 후 새 채팅창이 안 열림 (검색결과 없음 / 단축키 미수신 가능성): "
                f"target={ctx.target.title_exact!r}",
            )
        ctx.chat_hwnd = new_hwnd
        ctx.completed_steps.append(self.name)
        log.debug("새 채팅창 열림: hwnd=%s", hex(new_hwnd))

    def _double_click_first_result(self, main_hwnd: int) -> None:
        """활성 탭(친구/채팅)의 목록 컨트롤 첫 항목 영역에 win32 WM_LBUTTONDBLCLK 전송.

        Alt+Enter / Enter 가 안 먹는 환경에서의 fallback. 마우스를 실제로 움직이지
        않고 win32 메시지로 더블클릭 시뮬레이션 → 사용자가 마우스 만져도 안전.
        """
        list_hwnd = window_spec.find_active_list_control(main_hwnd)
        if list_hwnd is None:
            raise StepError(
                self.name,
                "활성 탭의 목록 컨트롤(EVA_VH_ListControl_Dblclk)을 못 찾음",
            )

        # 컨트롤의 client area 기준 좌측 상단 부근을 더블클릭 — 첫 항목 위치
        # (정확한 row 위치는 카톡 내부 가상화라 알 수 없으나 첫 보이는 항목은
        # 거의 항상 row 0 위치인 (10, 25) 근처)
        x, y = 10, 25
        lparam = (y << 16) | x  # MAKELPARAM(x, y)
        wparam = 0x0001  # MK_LBUTTON

        WM_LBUTTONDBLCLK = 0x0203
        WM_LBUTTONDOWN = 0x0201
        WM_LBUTTONUP = 0x0202

        # 더블클릭 시퀀스: Down → Up → Down (DBLCLK) → Up
        # ListControl 이 더블클릭으로만 별도 창 열림 가정
        win32 = hwnd_utils._get_win32()  # noqa: SLF001
        import contextlib

        with contextlib.suppress(Exception):
            win32.SendMessage(list_hwnd, WM_LBUTTONDOWN, wparam, lparam)
            win32.SendMessage(list_hwnd, WM_LBUTTONUP, 0, lparam)
            win32.SendMessage(list_hwnd, WM_LBUTTONDBLCLK, wparam, lparam)
            win32.SendMessage(list_hwnd, WM_LBUTTONUP, 0, lparam)
        log.debug("WM_LBUTTONDBLCLK 전송 → hwnd=%s at (%d,%d)", hex(list_hwnd), x, y)

    def _wait_new_chat(self, prior: set[int], main_hwnd: int) -> int | None:
        deadline = self._clock() + self._new_chat_timeout
        while self._clock() < deadline:
            diff = window_spec.snapshot_chat_hwnds(exclude={main_hwnd}) - prior
            if diff:
                # 여러 개가 동시에 생기는 극히 드문 케이스 — 임의 하나 선택
                return next(iter(diff))
            self._sleep(DEFAULT_POLL_INTERVAL_SEC)
        return None


_check: Step = OpenChatroomStep()
