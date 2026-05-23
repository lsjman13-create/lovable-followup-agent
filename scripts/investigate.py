"""카톡 PC 의 win32 HWND / UIA 트리 덤프 진단 도구 — Phase 2 실측 도구.

본 PC 환경(카톡 PC 버전·DPI·테마)에서 채팅창의 정확한 class_name 과 메시지
입력창의 자식 HWND 구조를 알아내, `lovable_agent/domain.py` 의 `WindowSpec`
기본값과 Phase 2 에서 구현할 Step 들의 가정값을 확정한다.

사용법:
    # 1) 카카오톡 PC 를 켜고 로그인 상태로 두기
    # 2) 채팅창 1~2개 미리 열어두기 (특히 "나와의 채팅" 추천)
    # 3) 실행
    uv run python scripts/investigate.py

    # 결과를 마크다운 파일로 저장
    uv run python scripts/investigate.py --output docs/investigation-2026-05-23.md

    # 자식 트리 깊이 제한 변경
    uv run python scripts/investigate.py --max-depth 5

    # ★ "나와의 채팅"을 자동으로 열어 RICHEDIT50W 까지 진단 후 자동으로 닫기.
    #   사용자 수동 조작 불필요. 단 카톡 메인 창 사이드바가 '친구' 탭이어야 함.
    uv run python scripts/investigate.py --auto-open-self-chat --output docs/investigation-2026-05-23.md

배경 (kakao-sender v2 Phase 3 실측 결과):
- 카톡 PC 메인 창의 UIA 트리는 빈 PaneControl 중첩뿐 → 친구 탭·검색창을 UIA로
  잡기 어려움. win32 HWND 가 더 안정.
- 채팅창(1:1 / 단톡방)은 별도 top-level HWND 이며 내부에 표준 RICHEDIT50W
  입력창과 메시지 ListControl 을 노출.
- 메인 창의 활성 탭은 ContactListView_* / ChatRoomListView_* 같은 자식 HWND
  이름 + 사각형 좌표로 판정 가능.

이 스크립트의 출력 결과가 본인 환경에서도 위 패턴과 일치하는지 확인하는 것이
Phase 2의 첫 검증 단계.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# ──────────────────────────────────────────────────────────────
# Platform guard — 이 스크립트는 Windows 전용
# ──────────────────────────────────────────────────────────────
if sys.platform != "win32":
    sys.stderr.write(
        "ERROR: scripts/investigate.py 는 Windows 전용 (카톡 PC 가 Windows 에만 존재).\n"
        f"  현재 플랫폼: {sys.platform}\n"
    )
    sys.exit(2)

try:
    import ctypes

    import win32clipboard  # type: ignore
    import win32con
    import win32gui
    import win32process
except ImportError as e:  # pragma: no cover
    sys.stderr.write(f"ERROR: pywin32 import 실패: {e}\n  uv sync 로 의존성을 먼저 설치하세요.\n")
    sys.exit(2)

try:
    import psutil  # type: ignore
except ImportError:
    psutil = None  # 선택 의존성 — 없어도 동작 (PID → 프로세스명 조회만 비활성)

try:
    import uiautomation as uia  # type: ignore
except ImportError as e:  # pragma: no cover
    sys.stderr.write(
        f"ERROR: uiautomation import 실패: {e}\n  uv sync 로 의존성을 먼저 설치하세요.\n"
    )
    sys.exit(2)


KAKAO_PROCESS_NAME = "KakaoTalk.exe"
INTERESTING_CLASSES = {
    # 카톡 입력창 후보 — kakao-sender v2 의 발견
    "RICHEDIT50W": "메시지 입력창 후보 (Rich Edit)",
    "EVA_ChildWindow": "카톡 자체 컨테이너",
    "EVA_VH_ListControl_Dblclk": "카톡 자체 리스트 컨트롤",
    "EVA_Window": "카톡 자체 윈도우",
    "Edit": "표준 Edit (검색창 등 가능)",
    "Button": "표준 Button",
}
HWND_NAME_HINTS = ("ContactListView_", "ChatRoomListView_")


@dataclass
class HwndInfo:
    """단일 HWND 의 진단 정보."""

    hwnd: int
    class_name: str
    window_text: str
    rect: tuple[int, int, int, int]
    visible: bool
    pid: int
    children: list[HwndInfo] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────
# 1. win32 트리 수집
# ──────────────────────────────────────────────────────────────
def _get_pid_of_hwnd(hwnd: int) -> int:
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    return pid


def _get_process_name(pid: int) -> str | None:
    if psutil is None:
        return None
    try:
        return psutil.Process(pid).name()
    except Exception:  # noqa: BLE001 — 권한 부족 / 종료된 프로세스 등 다양한 케이스
        return None


def _make_hwnd_info(hwnd: int) -> HwndInfo:
    try:
        rect = win32gui.GetWindowRect(hwnd)
    except Exception:  # noqa: BLE001
        rect = (0, 0, 0, 0)
    try:
        class_name = win32gui.GetClassName(hwnd) or ""
    except Exception:  # noqa: BLE001
        class_name = ""
    try:
        window_text = win32gui.GetWindowText(hwnd) or ""
    except Exception:  # noqa: BLE001
        window_text = ""
    return HwndInfo(
        hwnd=hwnd,
        class_name=class_name,
        window_text=window_text,
        rect=rect,
        visible=bool(win32gui.IsWindowVisible(hwnd)),
        pid=_get_pid_of_hwnd(hwnd),
    )


def _collect_children(parent_hwnd: int, depth: int, max_depth: int) -> list[HwndInfo]:
    if depth >= max_depth:
        return []
    children: list[HwndInfo] = []

    def cb(child_hwnd: int, _lparam: int) -> bool:
        info = _make_hwnd_info(child_hwnd)
        info.children = _collect_children(child_hwnd, depth + 1, max_depth)
        children.append(info)
        return True

    # 일부 HWND 는 EnumChildWindows 호출을 거부 (권한·상태) — 정상 케이스라 무시
    with contextlib.suppress(Exception):
        win32gui.EnumChildWindows(parent_hwnd, cb, 0)
    return children


def find_kakao_top_levels(max_depth: int) -> list[HwndInfo]:
    """카카오톡 프로세스의 모든 top-level HWND 와 그 자식 트리 수집."""
    matched: list[HwndInfo] = []

    def cb(hwnd: int, _lparam: int) -> bool:
        if not win32gui.IsWindow(hwnd):
            return True
        pid = _get_pid_of_hwnd(hwnd)
        proc_name = _get_process_name(pid)
        # process name 확인이 안 되면(psutil 미설치 등) class name 으로 보조
        class_name = win32gui.GetClassName(hwnd) or ""
        is_kakao = (proc_name and proc_name.lower() == KAKAO_PROCESS_NAME.lower()) or (
            "EVA" in class_name  # 카톡 자체 윈도우 클래스 접두어
        )
        if not is_kakao:
            return True
        info = _make_hwnd_info(hwnd)
        info.children = _collect_children(hwnd, depth=1, max_depth=max_depth)
        matched.append(info)
        return True

    win32gui.EnumWindows(cb, 0)
    return matched


# ──────────────────────────────────────────────────────────────
# 2. UIA 트리 수집 — 메인 창의 UIA 가 정말 비어있는지 검증
# ──────────────────────────────────────────────────────────────
def collect_uia_tree(hwnd: int, max_depth: int = 3) -> list[str]:
    """UIA 컨트롤 트리를 들여쓰기 텍스트로 반환."""
    lines: list[str] = []
    try:
        control = uia.ControlFromHandle(hwnd)
    except Exception as e:  # noqa: BLE001
        return [f"  (UIA ControlFromHandle 실패: {e})"]
    if control is None:
        return ["  (UIA control 없음)"]

    def walk(ctl, depth: int) -> None:
        if depth > max_depth:
            return
        indent = "  " * depth
        try:
            ctl_type = ctl.ControlTypeName
            name = ctl.Name or ""
            class_name = ctl.ClassName or ""
        except Exception as e:  # noqa: BLE001
            lines.append(f"{indent}(읽기 실패: {e})")
            return
        snippet = name[:40] + ("..." if len(name) > 40 else "")
        lines.append(f"{indent}- {ctl_type}  class={class_name!r}  name={snippet!r}")
        try:
            children = ctl.GetChildren()
        except Exception as e:  # noqa: BLE001
            lines.append(f"{indent}  (자식 조회 실패: {e})")
            return
        for child in children:
            walk(child, depth + 1)

    walk(control, depth=0)
    return lines


# ──────────────────────────────────────────────────────────────
# 3. 마크다운 포매팅
# ──────────────────────────────────────────────────────────────
def _format_rect(rect: tuple[int, int, int, int]) -> str:
    left, top, right, bottom = rect
    w, h = right - left, bottom - top
    return f"({left},{top})-({right},{bottom}) [{w}x{h}]"


def _annotate_class(class_name: str, window_text: str) -> str:
    notes: list[str] = []
    if class_name in INTERESTING_CLASSES:
        notes.append(INTERESTING_CLASSES[class_name])
    for hint in HWND_NAME_HINTS:
        if hint in window_text or hint in class_name:
            notes.append(f"이름 힌트 '{hint}' 매칭")
    return f"   ⭐ {' / '.join(notes)}" if notes else ""


def render_hwnd_tree(info: HwndInfo, indent: int = 0) -> str:
    pad = "  " * indent
    head = (
        f"{pad}- HWND=0x{info.hwnd:08X} class={info.class_name!r} "
        f"text={info.window_text!r} visible={info.visible} "
        f"rect={_format_rect(info.rect)} pid={info.pid}"
    )
    annotation = _annotate_class(info.class_name, info.window_text)
    lines = [head + annotation]
    for c in info.children:
        lines.append(render_hwnd_tree(c, indent + 1))
    return "\n".join(lines)


def render_report(tops: list[HwndInfo], uia_max_depth: int) -> str:
    out = io.StringIO()
    print(f"# Kakao Investigation — {datetime.now():%Y-%m-%d %H:%M:%S}", file=out)
    print(file=out)
    print(f"발견된 카톡 top-level HWND: **{len(tops)}**개", file=out)
    print(file=out)

    if not tops:
        print("⚠️ 카카오톡 프로세스의 윈도우를 찾지 못했습니다.", file=out)
        print("- KakaoTalk PC가 실행·로그인 상태인지 확인하세요.", file=out)
        print("- psutil 미설치 시 프로세스명으로 필터링되지 않을 수 있습니다.", file=out)
        return out.getvalue()

    # 요약: 흥미로운 클래스가 어디에 몇 개 있는지
    print("## 요약 — 흥미로운 클래스 발견 위치", file=out)
    print(file=out)
    print("| top-level | class | 흥미로운 자식 클래스 |", file=out)
    print("|---|---|---|", file=out)
    for top in tops:
        flat = _flatten(top)
        found = sorted({c.class_name for c in flat if c.class_name in INTERESTING_CLASSES})
        print(
            f"| `{top.window_text or '<제목없음>'}` (HWND=0x{top.hwnd:08X}) | "
            f"`{top.class_name}` | {', '.join(found) or '—'} |",
            file=out,
        )
    print(file=out)

    # top-level 별 상세
    for i, top in enumerate(tops, 1):
        print(f"## ({i}) {top.window_text or '<제목없음>'}", file=out)
        print(file=out)
        print(
            f"- HWND: `0x{top.hwnd:08X}` ({top.hwnd})\n"
            f"- ClassName: `{top.class_name}`\n"
            f"- Rect: `{_format_rect(top.rect)}`\n"
            f"- Visible: `{top.visible}`\n"
            f"- PID: `{top.pid}`",
            file=out,
        )
        print(file=out)
        print("### win32 자식 HWND 트리", file=out)
        print(file=out)
        print("```", file=out)
        print(render_hwnd_tree(top), file=out)
        print("```", file=out)
        print(file=out)
        print(f"### UIA 트리 (depth ≤ {uia_max_depth})", file=out)
        print(file=out)
        print("```", file=out)
        for line in collect_uia_tree(top.hwnd, max_depth=uia_max_depth):
            print(line, file=out)
        print("```", file=out)
        print(file=out)

    print("---", file=out)
    print(file=out)
    print("## 다음 단계 — 확인할 것", file=out)
    print(file=out)
    print(
        "1. 메인 창의 UIA 트리가 `PaneControl` 빈 중첩 위주인가? (kakao-sender v2 의 발견과 일치하는지)\n"
        "2. 채팅창(별도 top-level) 안에 `RICHEDIT50W` 자식이 존재하는가? "
        "(메시지 입력창)\n"
        "3. 메인 창 자식 중 `ContactListView_*` / `ChatRoomListView_*` 이름이 보이는가?\n"
        "4. 채팅창의 `class` (top-level 자체) 값이 무엇인가? "
        "→ `WindowSpec.class_name` 기본값으로 채택",
        file=out,
    )
    return out.getvalue()


def _flatten(info: HwndInfo) -> list[HwndInfo]:
    result = [info]
    for c in info.children:
        result.extend(_flatten(c))
    return result


# ──────────────────────────────────────────────────────────────
# 4. 자동 오픈 ("나와의 채팅") — kakao-sender v2 패턴 차용 미니 버전
# ──────────────────────────────────────────────────────────────
CHAT_WINDOW_CLASS = "EVA_Window_Dblclk"
MAIN_WINDOW_TITLE = "카카오톡"
FRIEND_TAB_PREFIX = "ContactListView_"
SELF_CHAT_NAME = "나와의 채팅"
EM_SETSEL = 0x00B1


def _find_main_window() -> int | None:
    """카톡 메인 창 — class=EVA_Window_Dblclk + title='카카오톡'."""
    found: list[int] = []

    def cb(hwnd: int, _lparam: object) -> bool:
        with contextlib.suppress(Exception):
            if (
                win32gui.IsWindowVisible(hwnd)
                and win32gui.GetClassName(hwnd) == CHAT_WINDOW_CLASS
                and win32gui.GetWindowText(hwnd) == MAIN_WINDOW_TITLE
            ):
                found.append(hwnd)
        return True

    win32gui.EnumWindows(cb, 0)
    return found[0] if found else None


def _snapshot_chat_hwnds(exclude: int | None = None) -> set[int]:
    """현재 모든 EVA_Window_Dblclk top-level (메인 창 제외) HWND 집합."""
    result: set[int] = set()

    def cb(hwnd: int, _lparam: object) -> bool:
        with contextlib.suppress(Exception):
            if (
                win32gui.IsWindowVisible(hwnd)
                and win32gui.GetClassName(hwnd) == CHAT_WINDOW_CLASS
                and hwnd != exclude
            ):
                result.add(hwnd)
        return True

    win32gui.EnumWindows(cb, 0)
    return result


def _is_friends_tab_active(main_hwnd: int) -> bool:
    """메인 창 자식 중 visible 한 ContactListView_* 가 있으면 친구 탭 활성."""
    found = [False]

    def cb(child_hwnd: int, _lparam: object) -> bool:
        with contextlib.suppress(Exception):
            if win32gui.IsWindowVisible(child_hwnd):
                title = win32gui.GetWindowText(child_hwnd) or ""
                if title.startswith(FRIEND_TAB_PREFIX):
                    found[0] = True
        return True

    with contextlib.suppress(Exception):
        win32gui.EnumChildWindows(main_hwnd, cb, 0)
    return found[0]


def _force_foreground(hwnd: int) -> None:
    """SetForegroundWindow 의 background-process 제약을 AttachThreadInput 으로 우회."""
    foreground = win32gui.GetForegroundWindow()
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
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        with contextlib.suppress(Exception):
            win32gui.BringWindowToTop(hwnd)
        with contextlib.suppress(Exception):
            win32gui.SetForegroundWindow(hwnd)
    finally:
        if attached:
            with contextlib.suppress(Exception):
                win32process.AttachThreadInput(current_thread, fg_thread, False)


def _set_clipboard_text(text: str) -> None:
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()


def _find_friend_tab_search_edit(main_hwnd: int) -> int | None:
    """친구 탭(ContactListView_*) 자식 중 첫 표준 Edit HWND."""
    contact_view = [0]

    def find_contact(hwnd: int, _lparam: object) -> bool:
        with contextlib.suppress(Exception):
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd) or ""
                if title.startswith(FRIEND_TAB_PREFIX):
                    contact_view[0] = hwnd
                    return False  # 첫 매칭만
        return True

    with contextlib.suppress(Exception):
        win32gui.EnumChildWindows(main_hwnd, find_contact, 0)
    if not contact_view[0]:
        return None

    edit_hwnd = [0]

    def find_edit(hwnd: int, _lparam: object) -> bool:
        with contextlib.suppress(Exception):
            if win32gui.GetClassName(hwnd) == "Edit":
                edit_hwnd[0] = hwnd
                return False
        return True

    with contextlib.suppress(Exception):
        win32gui.EnumChildWindows(contact_view[0], find_edit, 0)
    return edit_hwnd[0] or None


def _wait_new_chat_hwnd(prior: set[int], timeout: float = 3.0) -> int | None:
    """prior 집합에 없던 새 EVA_Window_Dblclk 채팅창 출현 대기."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        diff = _snapshot_chat_hwnds() - prior
        if diff:
            return next(iter(diff))
        time.sleep(0.1)
    return None


def _open_self_chat(report_lines: list[str]) -> tuple[int | None, list[str]]:
    """카톡 '나와의 채팅' 자동 오픈 + RICHEDIT50W 진단.

    Returns:
        (열린 채팅창 HWND or None, 추가 진단 마크다운 라인들)
    """
    import time

    import pyautogui  # type: ignore

    pyautogui.FAILSAFE = False
    pyautogui.PAUSE = 0.05

    lines: list[str] = []
    lines.append("## 🤖 자동 오픈 진단 (`--auto-open-self-chat`)")
    lines.append("")

    main_hwnd = _find_main_window()
    if main_hwnd is None:
        lines.append("❌ 카톡 메인 창을 찾을 수 없음. 카톡 PC 가 실행·로그인 상태인지 확인하세요.")
        return None, lines
    lines.append(f"- 메인 창: `0x{main_hwnd:08X}` ({MAIN_WINDOW_TITLE!r})")

    _force_foreground(main_hwnd)
    time.sleep(0.4)

    if not _is_friends_tab_active(main_hwnd):
        lines.append(
            "❌ 친구 탭이 활성 상태가 아님. 카톡 사이드바에서 **친구 아이콘**을 클릭한 뒤 재실행하세요.\n"
            "  (카톡 PC가 사이드바 단축키를 제공하지 않아 자동 전환 불가)"
        )
        return None, lines
    lines.append("- 친구 탭: ✓ 활성")

    # Ctrl+F 검색창 활성화
    pyautogui.hotkey("ctrl", "f")
    time.sleep(0.4)

    # 검색 Edit HWND 찾기
    search_edit = _find_friend_tab_search_edit(main_hwnd)
    if search_edit is None:
        lines.append(
            "❌ 친구 탭 검색창(Edit) 을 못 찾음. 카톡 PC 업데이트로 구조가 바뀌었을 가능성."
        )
        return None, lines
    lines.append(f"- 검색 Edit HWND: `0x{search_edit:08X}`")

    # 기존 내용 클리어 + 새 검색어 입력
    with contextlib.suppress(Exception):
        win32gui.SendMessage(search_edit, EM_SETSEL, 0, -1)
        win32gui.SendMessage(search_edit, win32con.WM_CLEAR, 0, 0)
        win32gui.SetFocus(search_edit)

    _set_clipboard_text(SELF_CHAT_NAME)
    time.sleep(0.1)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.6)  # 검색 결과 렌더링 대기
    lines.append(f"- 검색어 입력: {SELF_CHAT_NAME!r}")

    # HWND 스냅샷 (전)
    before = _snapshot_chat_hwnds(exclude=main_hwnd)

    # Enter + Alt+Enter (별도 창)
    pyautogui.press("enter")
    time.sleep(0.2)
    pyautogui.hotkey("alt", "enter")
    time.sleep(0.5)

    # HWND diff
    new_chat_hwnd = _wait_new_chat_hwnd(before | {main_hwnd}, timeout=3.0)
    if new_chat_hwnd is None:
        # 정리: 검색창 비우기
        with contextlib.suppress(Exception):
            win32gui.SendMessage(search_edit, EM_SETSEL, 0, -1)
            win32gui.SendMessage(search_edit, win32con.WM_CLEAR, 0, 0)
        lines.append(
            "❌ Alt+Enter 후 새 채팅창이 안 열림. 검색 결과에 '나와의 채팅' 이 없거나,\n"
            "  카톡 PC 가 단축키를 받지 못한 상태일 수 있음."
        )
        return None, lines

    title = win32gui.GetWindowText(new_chat_hwnd) or ""
    lines.append(f"- 새 채팅창 HWND: `0x{new_chat_hwnd:08X}` title={title!r}")

    # 자식 트리 덤프 + RICHEDIT 찾기
    children_info: list[str] = []
    richedit_found: list[tuple[int, str]] = []

    def child_cb(child_hwnd: int, _lparam: object) -> bool:
        with contextlib.suppress(Exception):
            cls = win32gui.GetClassName(child_hwnd) or ""
            txt = win32gui.GetWindowText(child_hwnd) or ""
            rect = win32gui.GetWindowRect(child_hwnd)
            vis = win32gui.IsWindowVisible(child_hwnd)
            marker = ""
            if "RICHEDIT" in cls.upper():
                marker = "  ⭐ 메시지 입력창!"
                richedit_found.append((child_hwnd, cls))
            children_info.append(
                f"  0x{child_hwnd:08X} class={cls!r:30} vis={vis} text={txt[:30]!r} "
                f"rect={_format_rect(rect)}{marker}"
            )
        return True

    with contextlib.suppress(Exception):
        win32gui.EnumChildWindows(new_chat_hwnd, child_cb, 0)

    lines.append("")
    lines.append("### 새 채팅창의 자식 HWND")
    lines.append("```")
    lines.extend(children_info)
    lines.append("```")
    lines.append("")

    if richedit_found:
        lines.append("### ✅ RICHEDIT 발견 — 카톡 PC 메시지 입력창")
        for hwnd, cls in richedit_found:
            lines.append(
                f"- `0x{hwnd:08X}` class **`{cls}`** ← Phase 2 `WindowSpec.expected_input_class` 후보"
            )
        lines.append("")
        lines.append(
            "**의미**: kakao-sender v2 의 가정 (`RICHEDIT50W` 또는 호환 RichEdit 클래스) 이 본인 환경에서도 성립.\n"
            "Phase 2 의 `output/hwnd_utils.py` 와 Step 모듈들이 같은 패턴으로 동작할 가능성 매우 높음."
        )
    else:
        lines.append("### ⚠️ RICHEDIT 클래스를 못 찾음")
        lines.append("kakao-sender v2 의 가정과 다름. Phase 2 설계 재검토 필요.")
    lines.append("")

    # 정리: 검색창 비우기 + 채팅창 닫기
    with contextlib.suppress(Exception):
        win32gui.SendMessage(search_edit, EM_SETSEL, 0, -1)
        win32gui.SendMessage(search_edit, win32con.WM_CLEAR, 0, 0)

    with contextlib.suppress(Exception):
        win32gui.PostMessage(new_chat_hwnd, win32con.WM_CLOSE, 0, 0)
    lines.append(f"- 정리: 채팅창 `0x{new_chat_hwnd:08X}` 에 WM_CLOSE 전송, 검색창 비움")

    return new_chat_hwnd, lines


def cmd_auto_open_self_chat(uia_max_depth: int, output: str | None) -> int:
    """자동 오픈 진단 명령 — 기본 트리 덤프 + 자동 오픈 결과를 같이 출력."""
    print("[*] 자동 오픈 모드 — '나와의 채팅' 을 열어 진단합니다", file=sys.stderr)
    print("[!] 카톡 메인 창 사이드바가 **친구** 탭에 있는지 확인하세요", file=sys.stderr)

    # 기본 진단 먼저 (자동 오픈 후 채팅창 닫혀있을 때 메인 상태)
    auto_chat_hwnd, auto_lines = _open_self_chat([])

    # 일반 트리 덤프
    tops = find_kakao_top_levels(max_depth=4)
    report = render_report(tops, uia_max_depth=uia_max_depth)

    # 자동 오픈 섹션을 리포트 앞에 끼워넣음
    auto_section = "\n".join(auto_lines) + "\n\n---\n\n"
    full_report = auto_section + report
    print(full_report)

    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(full_report, encoding="utf-8")
        print(f"[*] 결과 저장: {path}", file=sys.stderr)

    return 0 if auto_chat_hwnd is not None else 4


# ──────────────────────────────────────────────────────────────
# 5. main
# ──────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="investigate",
        description="카톡 PC 의 win32 HWND / UIA 트리 덤프",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=4,
        help="win32 자식 HWND 트리 깊이 제한 (기본: 4)",
    )
    parser.add_argument(
        "--uia-depth",
        type=int,
        default=3,
        help="UIA 트리 깊이 제한 (기본: 3)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="결과를 저장할 마크다운 파일 경로 (예: docs/investigation-2026-05-23.md). "
        "지정 시 stdout 으로도 같이 출력.",
    )
    parser.add_argument(
        "--auto-open-self-chat",
        action="store_true",
        help=(
            "카톡 메인 창에서 Ctrl+F + '나와의 채팅' 검색 + Alt+Enter 로 채팅창을 자동 오픈해 "
            "RICHEDIT 입력창까지 진단한 뒤 자동으로 닫음. 카톡 사이드바가 '친구' 탭이어야 함."
        ),
    )
    args = parser.parse_args(argv)

    # Windows 콘솔의 한글 깨짐 완화 — 콘솔이 utf-8 미지원이면 조용히 패스
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8")

    # 분기: 자동 오픈 모드
    if args.auto_open_self_chat:
        return cmd_auto_open_self_chat(uia_max_depth=args.uia_depth, output=args.output)

    print(f"[*] 카카오톡 윈도우 탐색 시작 — max_depth={args.max_depth}", file=sys.stderr)
    if psutil is None:
        print(
            "[!] psutil 미설치 — 프로세스명 필터링 비활성, class 'EVA*' 기반으로만 매칭합니다.",
            file=sys.stderr,
        )
    tops = find_kakao_top_levels(max_depth=args.max_depth)
    print(f"[*] top-level HWND {len(tops)}개 발견", file=sys.stderr)

    report = render_report(tops, uia_max_depth=args.uia_depth)
    print(report)

    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report, encoding="utf-8")
        print(f"[*] 결과 저장: {path}", file=sys.stderr)

    return 0 if tops else 1


if __name__ == "__main__":
    sys.exit(main())
