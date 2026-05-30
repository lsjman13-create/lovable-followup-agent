# Lovable 업무 팔로업 에이전트 — 진행 현황 & 개선 과제

> 작성일: 2026-05-24
> 작성 시점 커밋: `335ab7e feat(phase-4): OllamaClient` 이후 + Notion Inbox 본문 블록 읽기 확장

## 📊 진행 상황 (Phase 별)

| Phase | 상태 | 핵심 성과 |
|---|---|---|
| **0. 기획** | ✅ | PRD/ARCHITECTURE/DECISIONS/PLAN.md — 12+ 결정 문서화 |
| **1. 도메인 모델** | ✅ | TaskStatus, ExtractedTask, WindowSpec, SendQueueItem |
| **2. 카톡 발송** | ✅ | 6-step KakaoSender, 3중 방어, 5/5 실 발송 성공 |
| **3. 카톡 분석** | ✅ | 파서 + TaskExtractor + 중복 판별 |
| **4. Notion + LLM 실연동** | ✅ | NotionRepository (3.x API), **4가지 LLM 백엔드** |
| **5. 운영 데몬** | ✅ | main.py + 시작 프로그램 등록 + 로그 회전 |
| **추가: Inbox 경로** | ✅ | 페이지 본문 블록 읽기 (FR-1.1 부분 구현) |

### 검증된 실 e2e (Mock 0)

| 시나리오 | 결과 |
|---|---|
| `integration_e2e.py` — 로컬 .txt → ClaudeCLI | ✅ 실 카톡 발송 1건 도달 |
| `integration_e2e.py` — 로컬 .txt → Ollama | ✅ 실 카톡 발송 1건 도달 (244초) |
| `integration_inbox_e2e.py` — 노션 Inbox → Ollama | ✅ Inbox→Tasks 3건 (58초) |

**총 186/186 단위 테스트 통과**

### LLM 백엔드 (`LLMClient` Protocol)

| 백엔드 | 용도 | 검증 상태 |
|---|---|---|
| `MockLLMClient` | 오프라인 테스트 | ✅ |
| `ClaudeCLIClient` | 무료 (Claude Code 플랜) | ✅ 실 호출 |
| `OllamaClient` | PII 외부 유출 0 | ✅ 실 호출 |
| `AnthropicAPIClient` | 유료, 제일 안정 | ⚠️ 코드만 (실 호출 없음) |

---

## 🔴 즉시 고쳐야 할 것 (운영 시작 전 차단)

### 1. 노션 Inbox 첨부파일 처리 안 됨 (FR-1.1 갭)
- **현재**: 페이지 본문의 `paragraph/heading/list/code` 등 텍스트 블록만 읽음
- **PRD FR-1.1**: ".txt 파일을 노션에 올릴 수 있어야 한다"
- **갭**: 사용자가 `.txt` 를 파일로 첨부하면 무시됨 → 텍스트로 붙여넣기 강제
- **해결안**: `file`/`pdf` 블록의 url 을 다운로드해서 텍스트 추출 (단, Notion 의 file URL 은 일시 토큰, 만료 처리 필요)

### 2. `max_input_chars=3000` 은 너무 작음
- **현재**: 17K자 카톡 → 18%만 처리됨 (=82% 누락)
- **위험**: 긴 회의 톡방에서 끝부분 메시지가 잘려나감
- **해결안**: 청크 분할 (#4 참조)

### 3. llama3:latest 의 한국어 추출 품질 낮음
- 한 케이스에서 4건 중 1건만 추출 + `what` 영어로 번역
- 담당자/마감일 인식 약함 (대부분 "미정")
- 셀모임 4건을 같은 카테고리로 단순 복제 (할루시네이션 우려)
- **해결안**: `exaone3.5:7.8b` (LG 한국어 모델) 로 교체 (#5 참조)

---

## 🟡 발전 필요 (1~2주 내)

### 4. 청크 분할 처리
- 절단(truncate) 대신 시간순 청크로 분할 → 여러 번 LLM 호출 → 결과 병합
- 17K자도 손실 없이 처리 가능
- 구현 위치: `TaskExtractor` 에 `chunk_size` 파라미터 + 결과 merge 로직

### 5. GPU / 한국어 강화 모델로 교체
- **현재**: llama3:latest 8B CPU → 3K자에 58~244초 (변동 ±50%)
- **권장**: `exaone3.5:7.8b` (LG 한국어 모델) + GPU
- **예상 효과**: 동일 입력 30초 이내, 한국어 추출 정확도 1.5~2배
- **사전 작업**: `ollama pull exaone3.5:7.8b` (~5GB)

### 6. 확정→발송 시나리오 e2e 미검증 ⭐ (가장 큰 갭)
- Inbox→Tasks 까지는 검증 ✅
- 매니저가 노션에서 `Status=확정` + `Chatroom` + `Due Date` 채운 후 데몬이 발송하는 자동 흐름은 **아직 실 환경에서 시간 흐름대로 검증 안 됨**
- 6시간 룰, D-1/D-day 오프셋, 화이트리스트 차단 등 통합 검증 필요

### 7. 데몬 24/7 운영 검증 없음
- `install_startup.py` 등록 → 재부팅 → 1주일 자동 운영 시나리오 미수행
- 메모리 누수, 노션 API rate-limit, 카톡 자동화 안정성 등 장기 운영 이슈는 미관측

### 8. 에러 시 사용자 알림 부재
- Ollama 타임아웃 / Notion API 오류 발생 시 **로그만 남고 사용자는 모름**
- `Notifier` 모듈은 있지만 실패 경로에 연결 안 됨
- 매니저가 "오늘 왜 알림 안 왔지?" 사후 발견 위험

---

## 🟢 추가 기능 / 운영 (1개월 이상)

### 9. AnthropicAPIClient 실 호출 검증 + 비용 상한
- 코드만 있고 실 검증 없음 → 클로드 코드 CLI 한도 초과 시 fallback 안 됨
- 월 30K원 상한 / Usage limit 정책 (PRD M0) 미구현

### 10. Inbox row 처리 결과 시각화
- 현재 Inbox는 `Processed` 체크박스만 토글
- 추출 결과 (몇 건 신규 / 몇 건 중복) 가 Inbox row 어디에도 안 보임
- 매니저가 "이 메모에서 뭐 추출됐지?" 확인하려면 Tasks DB 를 검색해야 함
- **해결안**: Inbox DB 에 `Extracted Count` (number) + `Extracted Task IDs` (rich_text) 컬럼 추가

### 11. PII 정리 자동화
- 테스트마다 사용자가 노션 row + 카톡 메시지 수동 삭제
- 운영 중에도 N개월 지난 Tasks 자동 아카이브 정책 없음

### 12. 모니터링/메트릭
- 일별 추출 건수, 발송 성공률, LLM 비용·시간 등 추적 안 됨
- 운영 1주 후 "잘 돌고 있나?" 판단 근거 부족

### 13. 사용자 가이드 부재
- README 에 "노션 Inbox 어디에 어떻게 붙여넣어야 하는지" 안내 없음
- 처음 보는 매니저가 셀프 온보딩 불가능

---

## 🔵 코드 품질 / 기술 부채

### 14. `integration_e2e.py` / `integration_inbox_e2e.py` 중복 50%+
- `_build_llm`, `_build_real_sender`, signal/encoding 셋업 등 동일 코드 양쪽에 복사됨
- `scripts/_common.py` 같은 헬퍼 모듈 필요

### 15. 회의록·이메일 등 다중 채널 미지원
- MVP 스코프대로 카톡만 — 의도된 제한
- 추후 확장 시 LLMClient/IngestProtocol 추상화는 이미 있어서 비교적 쉬움

### 16. 통합 테스트(integration test) 부족
- 단위 테스트 186개는 풍부
- `main.py` 의 daemon 루프 자체, `ReminderScheduler` 시간 흐름, Notion 폴링 인터벌 등 **시간을 흘려보내는 통합 테스트는 없음**

---

## ⚙️ 권장 다음 행동 (우선순위)

| 순위 | 작업 | 노력 | 가치 |
|---|---|---|---|
| 1 | **확정→발송 e2e 자동화** (#6) | 중 | 운영 진입 차단 해제 |
| 2 | **에러 시 Notifier 알림 연결** (#8) | 소 | "왜 알림 안 와?" 방지 |
| 3 | **청크 분할 처리** (#4) | 중 | 긴 카톡 누락 해결 |
| 4 | **README 사용자 가이드** (#13) | 소 | 셀프 운영 시작 가능 |
| 5 | **AnthropicAPIClient 실 검증 + 비용 상한** (#9) | 중 | ClaudeCLI fallback 안전망 |
| 6 | **24/7 운영 실험 1주** (#7) | 중 | 장기 안정성 확인 |
| 7 | exaone3.5 + GPU 셋업 (#5) | 중 | 품질·속도 동시 개선 |

지금 가장 큰 단일 갭은 **#6 (확정→발송 자동 흐름의 시간 흐름대로 실 검증)** 입니다. Inbox → Tasks 까지는 봤지만, "매니저가 노션에서 확정하고 6시간 뒤 자동 발송되는" 시나리오를 실제 시계 흐름으로 한 번 돌려봐야 운영 자신이 생깁니다.

---

## 📁 관련 문서

- [PRD.md](./PRD.md) — 기능 요구사항, 시나리오
- [ARCHITECTURE.md](./ARCHITECTURE.md) — 컴포넌트, 데이터 흐름
- [DECISIONS.md](./DECISIONS.md) — 결정 로그
- [PLAN.md](./PLAN.md) — Phase 별 작업 목록
- [README.md](./README.md) — 설치·실행 가이드
