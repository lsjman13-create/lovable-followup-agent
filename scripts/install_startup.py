"""Windows 시작 프로그램 폴더에 lovable-agent.bat 자동 생성.

PC 로그인 시 자동으로 운영 데몬이 백그라운드에서 시작되게 한다. 한 번만 실행.

사용법:
    uv run python scripts/install_startup.py            # .bat 생성
    uv run python scripts/install_startup.py --uninstall  # .bat 제거
    uv run python scripts/install_startup.py --dry-run   # 변경 X, 경로만 출력

생성되는 파일:
- 위치: %APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\lovable-agent.bat
- 내용: 현재 프로젝트 경로를 작업 디렉터리로 잡고 pythonw 로 백그라운드 실행
- 콘솔 창 안 뜸 (pythonw)

검증 (사용자 1회):
1. 본 스크립트 실행 후 PC 재부팅 (또는 로그아웃·로그인)
2. 작업 관리자 → "백그라운드 프로세스" 에서 python 또는 pythonw 검색
3. 로그 파일 확인: ~/lovable-agent/logs/agent.log
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sys
from pathlib import Path

with contextlib.suppress(Exception):
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

log = logging.getLogger(__name__)

# 시작 프로그램 폴더 (사용자별)
STARTUP_DIR = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup"
BAT_FILENAME = "lovable-agent.bat"


def _project_root() -> Path:
    """이 스크립트의 부모의 부모 = 프로젝트 루트."""
    return Path(__file__).resolve().parent.parent


def _bat_content(project_root: Path) -> str:
    """시작 프로그램 .bat 의 내용.

    - cd /d 로 프로젝트 루트로 이동
    - uv run pythonw 로 콘솔 없이 운영 모드 백그라운드 실행
    - 로그는 ~/lovable-agent/logs/agent.log 로 자동 (main.py 가 처리)
    """
    return (
        "@echo off\r\n"
        "rem Lovable Followup Agent — auto-installed by scripts/install_startup.py\r\n"
        "rem 이 파일을 삭제하면 부팅 자동 실행이 해제됩니다.\r\n"
        f'cd /d "{project_root}"\r\n'
        "start /B uv run pythonw -m lovable_agent\r\n"
    )


def install(dry_run: bool = False) -> int:
    if sys.platform != "win32":
        log.error("Windows 전용 스크립트 — 현재 플랫폼: %s", sys.platform)
        return 2

    if not STARTUP_DIR.exists():
        log.error("시작 프로그램 폴더를 찾을 수 없음: %s", STARTUP_DIR)
        log.error("  Win+R → shell:startup 으로 직접 확인하고 환경변수 APPDATA 점검")
        return 2

    root = _project_root()
    bat_path = STARTUP_DIR / BAT_FILENAME
    content = _bat_content(root)

    log.info("프로젝트 루트   : %s", root)
    log.info("생성할 .bat 경로 : %s", bat_path)
    log.info(".bat 내용:")
    for line in content.splitlines():
        log.info("    %s", line)

    if dry_run:
        log.info("[DRY-RUN] 실 생성 안 함")
        return 0

    if bat_path.exists():
        log.info("기존 파일 있음 — 덮어씁니다")

    # cp949 (Windows 기본 batch 인코딩) 으로 저장
    bat_path.write_text(content, encoding="cp949", newline="")
    log.info("✅ 시작 프로그램 등록 완료")
    log.info("")
    log.info("다음에 PC 재부팅·로그아웃·로그인 시 자동으로 운영 데몬이 시작됩니다.")
    log.info("작업 관리자에서 'pythonw' 또는 'python' 으로 검색해 동작 확인 가능.")
    log.info("로그 위치: ~/lovable-agent/logs/agent.log")
    return 0


def uninstall(dry_run: bool = False) -> int:
    if sys.platform != "win32":
        log.error("Windows 전용 스크립트")
        return 2
    bat_path = STARTUP_DIR / BAT_FILENAME
    if not bat_path.exists():
        log.info("등록된 파일 없음 (이미 제거됨 또는 미설치): %s", bat_path)
        return 0
    log.info("제거 대상: %s", bat_path)
    if dry_run:
        log.info("[DRY-RUN] 실 제거 안 함")
        return 0
    bat_path.unlink()
    log.info("✅ 시작 프로그램 등록 해제 완료")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="install_startup",
        description="Windows 시작 프로그램 폴더에 lovable-agent 자동 실행 등록",
    )
    parser.add_argument("--uninstall", action="store_true", help="등록 해제")
    parser.add_argument("--dry-run", action="store_true", help="변경 X, 경로만 출력")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.uninstall:
        return uninstall(dry_run=args.dry_run)
    return install(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
