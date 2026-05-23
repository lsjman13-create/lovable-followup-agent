"""win32 API 래퍼 — 카톡 자동화의 저수준 빌딩 블록.

설계 결정:
- 모든 win32 호출을 한 모듈로 격리해서 단위 테스트에서 mock 주입 가능하게.
- 함수는 순수 wrapper 에 가깝게 — 비즈니스 로직(친구 탭 판정, 채팅창 식별)은
  `window_spec.py` 와 Step 모듈로 분리.
- kakao-sender v2 의 `engine/windows.py` 패턴 차용.

PRD §NFR-1, ARCHITECTURE §4.6 참조.
"""

from __future__ import annotations

import contextlib
import logging
import sys
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


class _Win32Module(Protocol):
    """단위 테스트에서 win32gui 를 fake 로 대체할 때 사용하는 최소 인터페이스."""

    def EnumWindows(self, cb, lparam) -> None: ...
    def EnumChildWindows(self, parent, cb, lparam) -> None: ...
    def GetClassName(self, hwnd: int) -> str: ...
    def GetWindowText(self, hwnd: int) -> str: ...
    def GetWindowRect(self, hwnd: int) -> tuple[int, int, int, int]: ...
    def IsWindowVisible(self, hwnd: int) -> bool: ...
    def IsIconic(self, hwnd: int) -> bool: ...
    def ShowWindow(self, hwnd: int, cmd: int) -> None: ...
    def BringWindowToTop(self, hwnd: int) -> None: ...
    def SetForegroundWindow(self, hwnd: int) -> None: ...
    def GetForegroundWindow(self) -> int: ...
    def SetFocus(self, hwnd: int) -> int: ...
    def SendMessage(self, hwnd: int, msg: int, wparam, lparam) -> int: ...
    def PostMessage(self, hwnd: int, msg: int, wparam, lparam) -> None: ...


# ──────────────────────────────────────────────────────────────
# 모듈 수준 의존성 주입 포인트
# ──────────────────────────────────────────────────────────────
# 실제 win32gui 는 Windows 에서만 import 가능. 다른 OS 에서도 import 자체는 되도록
# lazy 로 처리. 테스트 시 `set_win32_module(fake)` 로 교체 가능.
_win32_module: _Win32Module | None = None
_win32_con_WM_CLOSE = 0x0010
_win32_con_WM_SETTEXT = 0x000C
_win32_con_WM_GETTEXT = 0x000D
_win32_con_WM_GETTEXTLENGTH = 0x000E
_win32_con_WM_CLEAR = 0x0303
_win32_con_WM_SETFOCUS = 0x0007
_win32_con_SW_RESTORE = 9
EM_SETSEL = 0x00B1


def _get_win32() -> _Win32Module:
    """win32gui 모듈을 lazy 로 가져온다. 테스트에서 set_win32_module 로 교체된 경우 그것 우선."""
    global _win32_module
    if _win32_module is not None:
        return _win32_module
    if sys.platform != "win32":
        raise RuntimeError(
            "hwnd_utils 는 Windows 전용 (또는 테스트에서 set_win32_module 로 fake 주입 필요)"
        )
    import win32gui  # type: ignore

    _win32_module = win32gui  # type: ignore[assignment]
    return _win32_module  # type: ignore[return-value]


def set_win32_module(module: _Win32Module | None) -> None:
    """테스트 진입점 — fake win32 모듈을 주입하거나 (None 으로) 초기화."""
    global _win32_module
    _win32_module = module


# ──────────────────────────────────────────────────────────────
# Enumeration
# ──────────────────────────────────────────────────────────────
def list_toplevel() -> list[tuple[int, str, str]]:
    """현재 visible top-level HWND: [(hwnd, class_name, title)]."""
    w = _get_win32()
    rows: list[tuple[int, str, str]] = []

    def cb(hwnd: int, _lparam: object) -> bool:
        with contextlib.suppress(Exception):
            if w.IsWindowVisible(hwnd):
                rows.append((hwnd, w.GetClassName(hwnd) or "", w.GetWindowText(hwnd) or ""))
        return True

    w.EnumWindows(cb, 0)
    return rows


def list_children(hwnd: int) -> list[tuple[int, str, str, tuple[int, int, int, int], bool]]:
    """직접 자식 HWND: [(hwnd, class, title, rect, visible)] — 한 단계만, 비재귀."""
    w = _get_win32()
    rows: list[tuple[int, str, str, tuple[int, int, int, int], bool]] = []

    def cb(child: int, _lparam: object) -> bool:
        with contextlib.suppress(Exception):
            rows.append(
                (
                    child,
                    w.GetClassName(child) or "",
                    w.GetWindowText(child) or "",
                    w.GetWindowRect(child),
                    bool(w.IsWindowVisible(child)),
                )
            )
        return True

    with contextlib.suppress(Exception):
        w.EnumChildWindows(hwnd, cb, 0)
    return rows


def find_first_child_by_class(hwnd: int, target_class: str) -> int | None:
    """주어진 클래스를 가진 첫 자식 HWND. 없으면 None."""
    for child_hwnd, cls, _title, _rect, _visible in list_children(hwnd):
        if cls == target_class:
            return child_hwnd
    return None


# ──────────────────────────────────────────────────────────────
# 포커스·포그라운드
# ──────────────────────────────────────────────────────────────
def force_foreground(hwnd: int) -> None:
    """SetForegroundWindow 가 백그라운드 프로세스에서 silently reject 되는 것을
    AttachThreadInput 트릭으로 우회.

    Windows 가 포커스 도용을 막기 위해 백그라운드 → 포그라운드 전환을 거부한다.
    현재 포그라운드 창의 입력 스레드에 일시적으로 attach 하면 같은 입력 큐에서
    요청하는 모양이 되어 허용됨.
    """
    w = _get_win32()
    if w.IsIconic(hwnd):
        w.ShowWindow(hwnd, _win32_con_SW_RESTORE)

    if w.GetForegroundWindow() == hwnd:
        return

    # AttachThreadInput 트릭은 ctypes 가 필요 — Windows 실 환경에서만 적용
    if sys.platform == "win32":
        try:
            import ctypes

            import win32process  # type: ignore

            foreground = w.GetForegroundWindow()
            current_thread = ctypes.windll.kernel32.GetCurrentThreadId()
            fg_thread = 0
            if foreground:
                fg_thread, _ = win32process.GetWindowThreadProcessId(foreground)

            attached = False
            if fg_thread and fg_thread != current_thread:
                with contextlib.suppress(Exception):
                    win32process.AttachThreadInput(current_thread, fg_thread, True)
                    attached = True

            try:
                with contextlib.suppress(Exception):
                    w.BringWindowToTop(hwnd)
                with contextlib.suppress(Exception):
                    w.SetForegroundWindow(hwnd)
            finally:
                if attached:
                    with contextlib.suppress(Exception):
                        win32process.AttachThreadInput(current_thread, fg_thread, False)
        except ImportError:
            # ctypes / win32process 미사용 환경 (테스트 등)
            with contextlib.suppress(Exception):
                w.BringWindowToTop(hwnd)
            with contextlib.suppress(Exception):
                w.SetForegroundWindow(hwnd)
    else:
        # 비 Windows 환경 (테스트): 단순 호출만
        with contextlib.suppress(Exception):
            w.BringWindowToTop(hwnd)
        with contextlib.suppress(Exception):
            w.SetForegroundWindow(hwnd)


def set_focus(hwnd: int) -> None:
    w = _get_win32()
    with contextlib.suppress(Exception):
        w.SetFocus(hwnd)


# ──────────────────────────────────────────────────────────────
# 메시지 전송 (Edit / RichEdit 직접 조작)
# ──────────────────────────────────────────────────────────────
def clear_edit(hwnd: int) -> None:
    """표준 Edit 또는 RichEdit 의 텍스트를 전체 선택 후 삭제."""
    w = _get_win32()
    with contextlib.suppress(Exception):
        w.SendMessage(hwnd, EM_SETSEL, 0, -1)
    with contextlib.suppress(Exception):
        w.SendMessage(hwnd, _win32_con_WM_CLEAR, 0, 0)


def set_edit_text(hwnd: int, text: str) -> None:
    """WM_SETTEXT 로 Edit/RichEdit 에 직접 텍스트 주입. 클립보드·포커스 독립."""
    w = _get_win32()
    with contextlib.suppress(Exception):
        w.SendMessage(hwnd, _win32_con_WM_SETTEXT, 0, text)


def get_edit_text(hwnd: int) -> str:
    """WM_GETTEXT 로 Edit/RichEdit 현재 텍스트 읽기."""
    if sys.platform != "win32":
        return ""
    try:
        import ctypes

        length = ctypes.windll.user32.SendMessageW(hwnd, _win32_con_WM_GETTEXTLENGTH, 0, 0)
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.SendMessageW(hwnd, _win32_con_WM_GETTEXT, length + 1, buf)
        return buf.value
    except Exception as e:  # noqa: BLE001
        log.debug("get_edit_text 실패 hwnd=%s: %s", hex(hwnd), e)
        return ""


def post_close(hwnd: int) -> None:
    """특정 HWND 에 WM_CLOSE 비동기 전송 (포그라운드 무관)."""
    w = _get_win32()
    with contextlib.suppress(Exception):
        w.PostMessage(hwnd, _win32_con_WM_CLOSE, 0, 0)


# ──────────────────────────────────────────────────────────────
# 클립보드
# ──────────────────────────────────────────────────────────────
def set_clipboard_text(text: str) -> None:
    """유니코드 텍스트를 Windows 클립보드에 넣음. Windows 외 환경에선 no-op."""
    if sys.platform != "win32":
        return
    try:
        import win32clipboard  # type: ignore
        import win32con  # type: ignore

        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
        finally:
            win32clipboard.CloseClipboard()
    except Exception as e:  # noqa: BLE001
        log.warning("클립보드 쓰기 실패 (계속 진행): %s", e)
