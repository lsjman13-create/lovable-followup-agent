"""카톡 PC 의 win32 HWND / UIA 트리 덤프 도구 — Phase 2에서 구현.

사용법:
    uv run python scripts/investigate.py

목적: 본인 환경(카톡 PC 버전·DPI·테마)에서 채팅창의 정확한 class_name,
expected_input_class 값을 알아내 WindowSpec 의 기본값을 확정.

kakao-sender (v2) 의 docs/investigation.md 의 결과를 본인 환경에서 재현하는 단계.
"""
