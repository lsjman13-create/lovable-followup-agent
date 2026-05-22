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
    import win32con  # noqa: F401  — 인터프리터 검증
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
# 4. main
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
    args = parser.parse_args(argv)

    # Windows 콘솔의 한글 깨짐 완화 — 콘솔이 utf-8 미지원이면 조용히 패스
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8")

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
        from pathlib import Path

        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report, encoding="utf-8")
        print(f"[*] 결과 저장: {path}", file=sys.stderr)

    return 0 if tops else 1


if __name__ == "__main__":
    sys.exit(main())
