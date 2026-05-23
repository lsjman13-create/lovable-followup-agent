"""window_spec 매칭 로직 + 카톡 헬퍼 테스트."""

from __future__ import annotations

import pytest

from lovable_agent.domain import WindowSpec
from lovable_agent.output import hwnd_utils, window_spec
from tests.test_hwnd_utils import FakeWin32


@pytest.fixture()
def kakao_state():
    """카톡 PC 의 정상 상태를 흉내내는 fake — 본인 PC 실측을 반영."""
    fake = FakeWin32(
        toplevels=[
            (0x100, "EVA_Window_Dblclk", "카카오톡"),  # 메인 창
            (0x200, "EVA_Window_Dblclk", "김훈희"),  # 1:1 채팅창
            (0x300, "EVA_Window_Dblclk", "MOP 운영방"),  # 단톡 채팅창
            (0x400, "EVA_Window_Dblclk", ""),  # 광고 팝업 (제외 대상)
            (0x500, "tooltips_class32", ""),  # 노이즈
        ],
        children={
            0x100: [
                # 친구 탭 활성 상태
                (0x110, "ContactListView_0", "ContactListView_NORMAL", (0, 0, 300, 800), True),
                (0x120, "EVA_VH_ListControl_Dblclk", "", (0, 0, 300, 800), True),
            ],
            0x110: [
                (0x111, "Edit", "", (0, 0, 200, 30), True),  # 검색창
            ],
            0x200: [
                (0x210, "RICHEDIT50W", "", (0, 0, 400, 100), True),
                (0x220, "EVA_VH_ListControl_Dblclk", "", (0, 0, 400, 600), True),
            ],
            0x300: [
                (0x310, "RICHEDIT50W", "", (0, 0, 400, 100), True),
                (0x320, "EVA_VH_ListControl_Dblclk", "", (0, 0, 400, 600), True),
            ],
        },
    )
    hwnd_utils.set_win32_module(fake)
    yield fake
    hwnd_utils.set_win32_module(None)


# ──────────────────────────────────────────────────────────────
# matches_hwnd
# ──────────────────────────────────────────────────────────────
def test_matches_hwnd_happy_path(kakao_state):
    spec = WindowSpec(title_exact="김훈희")
    assert window_spec.matches_hwnd(spec, 0x200) is True


def test_matches_hwnd_rejects_different_title(kakao_state):
    spec = WindowSpec(title_exact="김훈희")
    assert window_spec.matches_hwnd(spec, 0x300) is False  # MOP 운영방


def test_matches_hwnd_rejects_partial_match(kakao_state):
    """제목 부분일치 거부 — '김훈희' 인데 '김' 으로 매칭 시도하면 거부."""
    spec = WindowSpec(title_exact="김")
    assert window_spec.matches_hwnd(spec, 0x200) is False


def test_matches_hwnd_rejects_trailing_space(kakao_state):
    spec = WindowSpec(title_exact="김훈희 ")
    assert window_spec.matches_hwnd(spec, 0x200) is False


def test_matches_hwnd_requires_richedit_child(kakao_state):
    """채팅창인 척하는 빈 윈도우 (자식에 RICHEDIT 없음) 는 거부."""
    spec = WindowSpec(title_exact="")
    # 0x400 은 EVA_Window_Dblclk 이지만 자식이 없고 title='' 임
    assert window_spec.matches_hwnd(spec, 0x400) is False


def test_matches_hwnd_returns_false_for_unknown_hwnd(kakao_state):
    spec = WindowSpec(title_exact="any")
    assert window_spec.matches_hwnd(spec, 0xDEAD) is False


# ──────────────────────────────────────────────────────────────
# find_main_window
# ──────────────────────────────────────────────────────────────
def test_find_main_window(kakao_state):
    assert window_spec.find_main_window() == 0x100


def test_find_main_window_returns_none_when_absent():
    fake = FakeWin32(toplevels=[(0x500, "tooltips_class32", "")])
    hwnd_utils.set_win32_module(fake)
    try:
        assert window_spec.find_main_window() is None
    finally:
        hwnd_utils.set_win32_module(None)


# ──────────────────────────────────────────────────────────────
# snapshot_chat_hwnds
# ──────────────────────────────────────────────────────────────
def test_snapshot_includes_all_eva_window_dblclk(kakao_state):
    """메인 창·채팅창·광고 모두 EVA_Window_Dblclk — 모두 포함."""
    snap = window_spec.snapshot_chat_hwnds()
    assert snap == {0x100, 0x200, 0x300, 0x400}


def test_snapshot_can_exclude_main(kakao_state):
    snap = window_spec.snapshot_chat_hwnds(exclude={0x100})
    assert 0x100 not in snap
    assert {0x200, 0x300, 0x400}.issubset(snap)


def test_snapshot_diff_detects_new_chat(kakao_state):
    """현실 시나리오: 검색 후 '김훈희' 채팅창이 새로 떴다고 가정."""
    before = {0x100, 0x400}  # 메인 + 광고
    after = window_spec.snapshot_chat_hwnds()
    diff = after - before
    assert diff == {0x200, 0x300}  # 새로 발견된 채팅창들


# ──────────────────────────────────────────────────────────────
# find_chat_by_title
# ──────────────────────────────────────────────────────────────
def test_find_chat_by_title_happy(kakao_state):
    assert window_spec.find_chat_by_title("김훈희") == 0x200
    assert window_spec.find_chat_by_title("MOP 운영방") == 0x300


def test_find_chat_by_title_excludes_main(kakao_state):
    """제목이 '카카오톡' 인 메인 창은 채팅창으로 간주하지 않음."""
    assert window_spec.find_chat_by_title("카카오톡") is None


def test_find_chat_by_title_excludes_empty(kakao_state):
    """제목 빈 EVA_Window_Dblclk 은 채팅창 아님."""
    assert window_spec.find_chat_by_title("") is None


def test_find_chat_by_title_returns_none_when_no_match(kakao_state):
    assert window_spec.find_chat_by_title("존재하지 않는 톡방") is None


# ──────────────────────────────────────────────────────────────
# is_friends_tab_active
# ──────────────────────────────────────────────────────────────
def test_is_friends_tab_active_when_visible(kakao_state):
    assert window_spec.is_friends_tab_active(0x100) is True


def test_is_friends_tab_inactive_when_not_visible():
    fake = FakeWin32(
        toplevels=[(0x100, "EVA_Window_Dblclk", "카카오톡")],
        children={
            0x100: [
                (0x110, "ContactListView_0", "ContactListView_NORMAL", (0, 0, 300, 800), False),
                (0x130, "ChatRoomListView_0", "ChatRoomListView_NORMAL", (0, 0, 300, 800), True),
            ],
        },
    )
    hwnd_utils.set_win32_module(fake)
    try:
        # ContactListView 가 invisible → 친구 탭 비활성
        assert window_spec.is_friends_tab_active(0x100) is False
    finally:
        hwnd_utils.set_win32_module(None)


# ──────────────────────────────────────────────────────────────
# find_friend_tab_search_edit
# ──────────────────────────────────────────────────────────────
def test_find_friend_tab_search_edit(kakao_state):
    assert window_spec.find_friend_tab_search_edit(0x100) == 0x111


def test_find_friend_tab_search_edit_returns_none_when_tab_inactive():
    fake = FakeWin32(
        toplevels=[(0x100, "EVA_Window_Dblclk", "카카오톡")],
        children={
            0x100: [(0x110, "ContactListView_0", "x", (0, 0, 300, 800), False)],  # 비활성
        },
    )
    hwnd_utils.set_win32_module(fake)
    try:
        assert window_spec.find_friend_tab_search_edit(0x100) is None
    finally:
        hwnd_utils.set_win32_module(None)


# ──────────────────────────────────────────────────────────────
# find_richedit_in_chat
# ──────────────────────────────────────────────────────────────
def test_find_richedit_in_chat(kakao_state):
    assert window_spec.find_richedit_in_chat(0x200) == 0x210


def test_find_richedit_in_chat_returns_none_when_absent():
    fake = FakeWin32(
        toplevels=[(0x200, "EVA_Window_Dblclk", "test")],
        children={0x200: [(0x210, "SomeOtherClass", "", (0, 0, 1, 1), True)]},
    )
    hwnd_utils.set_win32_module(fake)
    try:
        assert window_spec.find_richedit_in_chat(0x200) is None
    finally:
        hwnd_utils.set_win32_module(None)
