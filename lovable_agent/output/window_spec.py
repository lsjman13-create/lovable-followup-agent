"""WindowSpec 매칭 로직 + 카톡 창 식별 헬퍼.

`domain.py` 의 WindowSpec dataclass 는 식별 정보의 캐리어 역할만 하고,
실제 매칭 알고리즘과 카톡 특화 검색 로직은 여기에 둔다.

ARCHITECTURE §4.6.1 참조.
"""

from __future__ import annotations

import logging

from lovable_agent.domain import WindowSpec
from lovable_agent.output import hwnd_utils

log = logging.getLogger(__name__)

# 카톡 PC 상수 — 본인 환경 실측(2026-05-23) + kakao-sender v2 확인
KAKAO_WINDOW_CLASS = "EVA_Window_Dblclk"
MAIN_WINDOW_TITLE = "카카오톡"
FRIEND_TAB_PREFIX = "ContactListView_"
KAKAO_PROCESS_NAME = "KakaoTalk.exe"
STANDARD_EDIT_CLASS = "Edit"


def matches_hwnd(spec: WindowSpec, hwnd: int) -> bool:
    """주어진 HWND 가 spec 과 일치하는지 검증 — 동명이인 오발송 방지.

    검증 항목:
    1. 클래스 이름 일치
    2. 제목 완전일치 (부분일치 / 공백 차이도 거부)
    3. (선택) 기대 입력창 클래스를 자식으로 가지고 있는가
    """
    # 1) 클래스
    children_list = hwnd_utils.list_children(hwnd)
    # list_children 는 hwnd 자체 정보를 안 주므로 toplevel 목록에서 찾음
    top = next(
        (row for row in hwnd_utils.list_toplevel() if row[0] == hwnd),
        None,
    )
    if top is None:
        return False
    _hwnd, class_name, title = top

    if class_name != spec.class_name:
        return False

    # 2) 제목 완전일치 — strip 등 변형 없이 그대로 비교
    if title != spec.title_exact:
        return False

    # 3) 입력창 자식 존재 여부 (선택적)
    if spec.expected_input_class:
        has_input = any(c[1] == spec.expected_input_class for c in children_list)
        if not has_input:
            return False

    return True


def find_main_window() -> int | None:
    """카톡 메인 창 — class=EVA_Window_Dblclk + title='카카오톡'. 없으면 None."""
    for hwnd, cls, title in hwnd_utils.list_toplevel():
        if cls == KAKAO_WINDOW_CLASS and title == MAIN_WINDOW_TITLE:
            return hwnd
    return None


def snapshot_chat_hwnds(exclude: set[int] | None = None) -> set[int]:
    """현재 visible 한 모든 EVA_Window_Dblclk top-level HWND 집합 (메인 창 등 exclude).

    HWND 스냅샷 diff (방어선 2) 의 입력. 채팅창인지 / 광고 팝업인지는 여기서 안 가린다
    — 단순히 전체 EVA_Window_Dblclk 를 모으고, 새로 생긴 것 1개만 인정하는 로직은
    호출자 (Step) 가 담당.
    """
    excluded = exclude or set()
    return {
        hwnd
        for hwnd, cls, _title in hwnd_utils.list_toplevel()
        if cls == KAKAO_WINDOW_CLASS and hwnd not in excluded
    }


def find_chat_by_title(title_exact: str, exclude: set[int] | None = None) -> int | None:
    """제목 완전일치로 채팅창 찾기. 메인 창과 빈 제목은 제외."""
    excluded = exclude or set()
    for hwnd, cls, title in hwnd_utils.list_toplevel():
        if cls != KAKAO_WINDOW_CLASS:
            continue
        if hwnd in excluded:
            continue
        if not title or title == MAIN_WINDOW_TITLE:
            continue
        if title == title_exact:
            return hwnd
    return None


def is_friends_tab_active(main_hwnd: int) -> bool:
    """메인 창에서 친구 탭이 활성인지 — visible 한 ContactListView_* 자식 존재 여부."""
    for _child, _cls, title, _rect, visible in hwnd_utils.list_children(main_hwnd):
        if visible and title.startswith(FRIEND_TAB_PREFIX):
            return True
    return False


def find_friend_tab_search_edit(main_hwnd: int) -> int | None:
    """친구 탭의 검색 Edit HWND. 친구 탭이 비활성이면 None."""
    # 1) visible 한 ContactListView_* 찾기
    contact_view: int | None = None
    for child, _cls, title, _rect, visible in hwnd_utils.list_children(main_hwnd):
        if visible and title.startswith(FRIEND_TAB_PREFIX):
            contact_view = child
            break
    if contact_view is None:
        return None

    # 2) 그 안의 첫 표준 Edit
    return hwnd_utils.find_first_child_by_class(contact_view, STANDARD_EDIT_CLASS)


def find_richedit_in_chat(chat_hwnd: int, expected_class: str = "RICHEDIT50W") -> int | None:
    """채팅창 안의 메시지 입력 RichEdit HWND."""
    return hwnd_utils.find_first_child_by_class(chat_hwnd, expected_class)
