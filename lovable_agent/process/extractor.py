"""Task Extractor — Phase 3에서 구현.

비정형 텍스트 + 기존 진행중 업무 목록 → LLMClient 호출 → ExtractionResult.
중복 판별까지 LLM에게 위임 (임베딩 미사용, DECISIONS.md §2 참조).
"""
