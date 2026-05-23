"""hwnd_utils 단위 테스트 — fake win32 모듈 주입으로 검증."""

from __future__ import annotations

import pytest

from lovable_agent.output import hwnd_utils


class FakeWin32:
    """단위 테스트용 가짜 win32 — 데이터로 행동을 결정."""

    def __init__(
        self,
        toplevels: list[tuple[int, str, str]] | None = None,
        children: dict[int, list[tuple[int, str, str, tuple[int, int, int, int], bool]]]
        | None = None,
        foreground: int = 0,
        iconic: dict[int, bool] | None = None,
        visible_default: bool = True,
    ) -> None:
        self.toplevels = toplevels or []
        self.children = children or {}
        self._foreground = foreground
        self._iconic = iconic or {}
        self._visible_default = visible_default

        self.send_message_log: list[tuple[int, int, object, object]] = []
        self.post_message_log: list[tuple[int, int, object, object]] = []
        self.set_focus_log: list[int] = []
        self.set_foreground_log: list[int] = []
        self.bring_to_top_log: list[int] = []
        self.show_window_log: list[tuple[int, int]] = []

    def EnumWindows(self, cb, lparam) -> None:
        for hwnd, _cls, _title in self.toplevels:
            cb(hwnd, lparam)

    def EnumChildWindows(self, parent, cb, lparam) -> None:
        for row in self.children.get(parent, []):
            cb(row[0], lparam)

    def GetClassName(self, hwnd: int) -> str:
        for h, cls, _t in self.toplevels:
            if h == hwnd:
                return cls
        for parent_children in self.children.values():
            for row in parent_children:
                if row[0] == hwnd:
                    return row[1]
        return ""

    def GetWindowText(self, hwnd: int) -> str:
        for h, _c, title in self.toplevels:
            if h == hwnd:
                return title
        for parent_children in self.children.values():
            for row in parent_children:
                if row[0] == hwnd:
                    return row[2]
        return ""

    def GetWindowRect(self, hwnd: int) -> tuple[int, int, int, int]:
        for parent_children in self.children.values():
            for row in parent_children:
                if row[0] == hwnd:
                    return row[3]
        return (0, 0, 0, 0)

    def IsWindowVisible(self, hwnd: int) -> bool:
        for parent_children in self.children.values():
            for row in parent_children:
                if row[0] == hwnd:
                    return row[4]
        return self._visible_default

    def IsIconic(self, hwnd: int) -> bool:
        return self._iconic.get(hwnd, False)

    def ShowWindow(self, hwnd: int, cmd: int) -> None:
        self.show_window_log.append((hwnd, cmd))

    def BringWindowToTop(self, hwnd: int) -> None:
        self.bring_to_top_log.append(hwnd)

    def SetForegroundWindow(self, hwnd: int) -> None:
        self.set_foreground_log.append(hwnd)
        self._foreground = hwnd

    def GetForegroundWindow(self) -> int:
        return self._foreground

    def SetFocus(self, hwnd: int) -> int:
        self.set_focus_log.append(hwnd)
        return 0

    def SendMessage(self, hwnd: int, msg: int, wparam, lparam) -> int:
        self.send_message_log.append((hwnd, msg, wparam, lparam))
        return 0

    def PostMessage(self, hwnd: int, msg: int, wparam, lparam) -> None:
        self.post_message_log.append((hwnd, msg, wparam, lparam))


@pytest.fixture()
def fake():
    fw = FakeWin32(
        toplevels=[
            (0x100, "EVA_Window_Dblclk", "카카오톡"),
            (0x200, "EVA_Window_Dblclk", "김훈희"),
            (0x300, "tooltips_class32", ""),
        ],
        children={
            0x100: [
                (0x110, "ContactListView_0", "", (0, 0, 300, 800), True),
                (0x120, "EVA_VH_ListControl_Dblclk", "", (0, 0, 300, 800), True),
            ],
            0x200: [
                (0x210, "RICHEDIT50W", "", (0, 0, 400, 100), True),
                (0x220, "EVA_VH_ListControl_Dblclk", "", (0, 0, 400, 600), True),
            ],
        },
        foreground=0x999,
    )
    hwnd_utils.set_win32_module(fw)
    yield fw
    hwnd_utils.set_win32_module(None)


# ──────────────────────────────────────────────────────────────
# Enumeration
# ──────────────────────────────────────────────────────────────
def test_list_toplevel_returns_tuples(fake):
    rows = hwnd_utils.list_toplevel()
    assert len(rows) == 3
    assert (0x100, "EVA_Window_Dblclk", "카카오톡") in rows


def test_list_children(fake):
    rows = hwnd_utils.list_children(0x200)
    assert len(rows) == 2
    classes = {r[1] for r in rows}
    assert "RICHEDIT50W" in classes
    assert "EVA_VH_ListControl_Dblclk" in classes


def test_find_first_child_by_class(fake):
    found = hwnd_utils.find_first_child_by_class(0x200, "RICHEDIT50W")
    assert found == 0x210
    assert hwnd_utils.find_first_child_by_class(0x200, "NonExistent") is None
    assert hwnd_utils.find_first_child_by_class(0x999, "Anything") is None  # 없는 부모


# ──────────────────────────────────────────────────────────────
# Edit 메시지 전송
# ──────────────────────────────────────────────────────────────
def test_set_edit_text_sends_wm_settext(fake):
    hwnd_utils.set_edit_text(0x210, "안녕")
    msgs = [m for m in fake.send_message_log if m[0] == 0x210]
    assert len(msgs) == 1
    assert msgs[0][1] == 0x000C  # WM_SETTEXT
    assert msgs[0][3] == "안녕"


def test_clear_edit_sends_em_setsel_and_wm_clear(fake):
    hwnd_utils.clear_edit(0x210)
    msgs = [m for m in fake.send_message_log if m[0] == 0x210]
    msg_codes = [m[1] for m in msgs]
    assert hwnd_utils.EM_SETSEL in msg_codes
    assert 0x0303 in msg_codes  # WM_CLEAR


def test_post_close_sends_wm_close(fake):
    hwnd_utils.post_close(0x200)
    assert (0x200, 0x0010, 0, 0) in fake.post_message_log


# ──────────────────────────────────────────────────────────────
# 포그라운드
# ──────────────────────────────────────────────────────────────
def test_force_foreground_skips_when_already_foreground():
    fake = FakeWin32(foreground=0x200)
    hwnd_utils.set_win32_module(fake)
    try:
        hwnd_utils.force_foreground(0x200)
        # 이미 foreground 이므로 SetForegroundWindow 호출 안 함
        assert fake.set_foreground_log == []
    finally:
        hwnd_utils.set_win32_module(None)


def test_force_foreground_restores_iconic():
    fake = FakeWin32(foreground=0x999, iconic={0x200: True})
    hwnd_utils.set_win32_module(fake)
    try:
        hwnd_utils.force_foreground(0x200)
        assert (0x200, 9) in fake.show_window_log  # SW_RESTORE
    finally:
        hwnd_utils.set_win32_module(None)


def test_force_foreground_calls_set_foreground_window():
    fake = FakeWin32(foreground=0x999)
    hwnd_utils.set_win32_module(fake)
    try:
        hwnd_utils.force_foreground(0x200)
        assert 0x200 in fake.set_foreground_log
    finally:
        hwnd_utils.set_win32_module(None)


def test_set_focus_logs_call(fake):
    hwnd_utils.set_focus(0x210)
    assert 0x210 in fake.set_focus_log
