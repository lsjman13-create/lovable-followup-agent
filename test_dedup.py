from lovable_agent.ingest.kakao_parser import parse_kakao_text
from lovable_agent.storage.sqlite_repo import SqliteRepository

text = """
--------------- 2026년 5월 23일 토요일 ---------------
[김매니저] [오전 10:30] 다음 주 수요일까지 보고서 주세요
[나] [오전 10:31] 네 알겠습니다
"""

repo = SqliteRepository("c:/Users/이승준/lovable-agent/agent.db")
msgs = parse_kakao_text(text)

# 첫 번째 삽입
hashes = [m.message_hash for m in msgs]
new_hashes = repo.filter_new_messages(hashes)
print(f"첫 시도: 전체 {len(hashes)}개 중 새로운 해시 {len(new_hashes)}개")
repo.mark_messages_processed(new_hashes)

# 두 번째 삽입
new_hashes_2 = repo.filter_new_messages(hashes)
print(f"두 번째 시도: 새로운 해시 {len(new_hashes_2)}개 (0개여야 성공!)")

# 약간 수정된 새 메시지 삽입
text_new = text + "[나] [오전 10:35] 다 했습니다!\n"
msgs_new = parse_kakao_text(text_new)
hashes_new = [m.message_hash for m in msgs_new]
new_hashes_3 = repo.filter_new_messages(hashes_new)
print(f"세 번째 시도 (새 메시지 추가됨): 새로운 해시 {len(new_hashes_3)}개 (1개여야 성공!)")

repo.close()
