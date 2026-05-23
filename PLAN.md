# PLAN — 구현 로드맵

> 작성일: 2026-05-23
> 관련 문서: [README.md](README.md) · [PRD.md](PRD.md) · [ARCHITECTURE.md](ARCHITECTURE.md) · [DECISIONS.md](DECISIONS.md)

이 문서는 **"다음에 뭐 하면 되지?"** 에 즉답하기 위한 작업 계획서입니다. PRD가 "무엇을/왜", ARCHITECTURE가 "어떻게"라면, 이 문서는 **"어떤 순서로, 어디까지 확인하고 다음 단계로 가는가"** 를 다룹니다.

---

## 0. 작업 원칙 (3가지)

1. **작게 잘라서 검증한다.** 한 번에 다 만들지 않고, 각 Phase 끝에 "이건 확실히 된다"를 손으로 만져 확인한 뒤 다음으로 간다.
2. **외부 연결(Notion·Anthropic API)은 가장 마지막에 붙인다.** 미리 붙이면 API 키 발급·계정 권한 문제로 본 코드 작업이 자꾸 멈춘다. 그 전까지는 **모의(mock) 구현**으로 우회한다.
3. **위험이 큰 부분부터 검증한다.** 우리 시스템에서 가장 깨지기 쉬운 부분은 **카톡 PC 자동화**다. AI 분석·노션 동기화는 잘 알려진 영역이라 늦게 붙여도 안전하다.

---

## 1. 현재 상태 (Phase 1 완료)

- ✅ Phase 0 — 요구사항·설계·결정 사항 문서화 (PRD/ARCHITECTURE/DECISIONS/README/PLAN)
- ✅ Phase 0 — 핵심 의사결정 12건 + kakao-sender (v2) 설계 차용
- ✅ **Phase 1 — 코드 골조 완성** (2026-05-23): pyproject.toml + 디렉터리 트리 + LLMClient Protocol + Mock 구현 + main.py --dry-run. pytest 8 passed.
- ✅ **Phase 2 부분 — `scripts/investigate.py`** (2026-05-23): 카톡 HWND/UIA 덤프 도구 작성 + `--auto-open-self-chat` 옵션. 본인 PC 실측으로 WindowSpec 확정.
- ✅ **Phase 2 본구현 — 카톡 자동화 모듈 7개** (2026-05-23): `hwnd_utils.py` + `window_spec.py` + Step 6개 + `kakao_sender.py` 오케스트레이션. **124 passed** (단위 + 통합).
- ✅ **Phase 2 완료 기준 — 실 카톡 5회 연속 발송 성공** (2026-05-23): `scripts/send_test.py` 로 "나와의 채팅" 대상 5/5 성공. **본인 환경 실측 — 친구 탭이 아니라 채팅 탭에서 검색** + **Alt+Enter 가 아니라 더블클릭(WM_LBUTTONDBLCLK)** 으로 별도 창 오픈 확인. EnsureFriendsTabStep 옵션화·OpenChatroomStep open_method 옵션화로 양쪽 케이스 모두 지원.
- ✅ **Phase 3 통합 — Scheduler ↔ KakaoSender 연결** (2026-05-23): `output/send_dispatcher.py` 신규 — 발송 큐에서 queued 항목을 꺼내 KakaoSender.send() 호출, send_history 기록 + Notifier 알림. `--dry-run` 흐름이 7단계로 확장. pytest **133 passed** (+9 dispatcher tests).
- ✅ **Phase 3 실 AI 분석 검증** (2026-05-23): `process/claude_cli_client.py` 신규 — `claude -p` 비대화형 호출로 Anthropic API 키 없이 LLMClient Protocol 구현. `scripts/analyze_kakao.py` 로 실 카톡 .txt (91 메시지) 분석 → **17초만에 1건 정확 추출** (잡담 90건 자동 무시, context·assignee·due_date 정확). pytest **144 passed** (+11 ClaudeCLI). DECISIONS §8.2 "Claude Code 환경 직접 운영" 옵션 부분 실현.
- ✅ **Phase 4 Notion 부분 — 코드 작성 완료** (2026-05-23): `storage/notion_repo.py` 실 API 구현 + `scripts/setup_notion.py` 1회 DB 자동 생성 도구. notion-client mock 단위 테스트 19개. pytest **163 passed** (+19).
- ✅ **Phase 4 Notion 실 통합 검증** (2026-05-24): 사용자 토큰 + 부모 페이지로 setup 실행, DB 3개 생성. **notion-client 3.x API 변경 대응**: (1) `databases.query` → `data_sources.query` 로 이전. (2) DB 생성이 2단계 (빈 DB → `data_sources.update` 로 schema). 실 노션에 `add_task` → `list_active_tasks` 왕복 검증 성공. Phase 4 노션 부분 완전 통합.
- ✅ **진정한 e2e 통합 검증** (2026-05-24): `scripts/integration_e2e.py` — Mock 0개. 실 카톡 .txt 91 메시지 → 실 Claude CLI (22.6초, 3건 추출, 모두 중복 정확 식별) → 실 노션 add_task + patch → ReminderScheduler.tick (6시간 룰 정상) → 실 KakaoSender (6 Step 통과) → 본인 카톡 발송 성공. PRD 의 모든 흐름이 실 환경에서 한 번에 흐름.
- ✅ **Phase 5 운영 데몬 코드 완성** (2026-05-24): `main.py` 에 운영 모드 (`_run_daemon`) 추가 — 매 분 Scheduler.tick + Dispatcher, 매 5분 Inbox 폴링, Ctrl+C 우아한 종료, 파일 로그 회전. `scripts/install_startup.py` — Windows 시작 프로그램 폴더에 `lovable-agent.bat` 자동 등록 (Task Scheduler 보다 단순 — 사용자 선택). 사용자가 setup_startup 1회 실행 + PC 재부팅하면 운영 진입.
- ✅ **Phase 3 (mock 가능 부분) — 분석·저장 로직** (2026-05-23): 카톡 파서 + SQLite + Whitelist 더블체크 + Extractor + Scheduler(6시간 룰) + Watcher + Notifier. pytest **70 passed**, dry-run 6단계 통합 흐름.
- ⏳ 다음 — Phase 2 의 본인 PC 카톡 점검 + 자동화 Step 들 구현, 그 다음에야 발송 통합 검증 가능.

---

## 2. 전체 로드맵 한눈에

```
[Phase 0] ✅ 설계 문서
   │
   ▼
[Phase 1] 코드 골조 잡기              ← 외부 의존성 0
   │       (패키지 레이아웃, config, 인터페이스 정의, mock 구현)
   ▼
[Phase 2] 카톡 자동화 검증            ← 가장 위험한 부분 먼저
   │       (HWND 트리 실측 → Step·WindowSpec 구현 → 테스트 톡방 발송)
   ▼
[Phase 3] 분석·저장 로직 구현         ← mock LLM, mock Notion 사용
   │       (.txt 파서, Extractor 인터페이스, 발송 큐, 스케줄러)
   ▼
[Phase 4] 외부 연결                   ← 여기서 처음으로 API/Notion 붙임
   │       (Anthropic API 키, Notion Integration, 실제 DB)
   ▼
[Phase 5] 통합·안전장치·운영 시작     ← 3중 방어 검증 + 1개월 운영
```

각 Phase 끝에는 **"이걸 손으로 만져보고 확인했다"** 는 명시적 검증 단계가 있습니다. 검증 통과 못 하면 다음 Phase로 안 갑니다.

---

## 3. Phase별 상세

### Phase 1. 코드 골조 잡기

> "외부 연결 없이도 코드가 import / 실행되는 빈 골조" 까지 만든다.

**목표**
- 프로젝트 구조를 ARCHITECTURE §8.5의 디렉터리 트리대로 깔아둠
- 각 컴포넌트가 빈 클래스/함수 + 인터페이스(Protocol)로 존재
- mock 구현체로 `python -m lovable_agent --dry-run` 하면 아무 외부 호출 없이 한 사이클 도는 정도

**산출물**
- `pyproject.toml` (의존성: anthropic, notion-client, uiautomation, pywin32, watchdog, apscheduler, pillow, winotify, pytest, ruff)
- `lovable_agent/` 패키지 전체 디렉터리 (빈 파일들 포함)
- `config.example.toml`
- `lovable_agent/process/llm_client.py` — `LLMClient` Protocol
- `lovable_agent/process/mock_client.py` — 하드코딩된 응답을 돌려주는 mock
- `lovable_agent/storage/mock_notion_repo.py` — 메모리 dict 기반 가짜 노션
- 최소한의 `pytest` 환경

**진입 조건**
- Python 3.11+ 설치, `uv` 설치

**완료 기준**
- [x] `uv sync` 가 성공한다 *(2026-05-23, 36 패키지 + Python 3.14.3 자동 설치)*
- [x] `uv run pytest` 가 0개 테스트 통과로라도 끝난다 (에러 없이) *(2026-05-23, **8 passed in 0.06s**)*
- [x] `uv run python -m lovable_agent --dry-run` 가 mock 의존성으로 끝까지 한 사이클 돌고 정상 종료한다 *(2026-05-23)*
- [x] `ruff check` 통과 *(2026-05-23)*

**위험 / 주의**
- 입문자가 처음 만나는 Python 패키지 레이아웃 — 한 번 잘 잡아두면 나머지가 편해짐, 여기서 시간 좀 써도 된다
- mock 구현체는 "그럴듯한 응답을 돌려주는 가짜" 정도로 충분. 너무 정교하게 만들지 말 것

---

### Phase 2. 카톡 자동화 검증 (가장 위험한 부분 먼저)

> 우리가 가장 모르는 것 = 본인 PC에서 카톡 PC의 HWND 구조가 kakao-sender v2의 실측과 같은가? 같다면 Step별로 안정적으로 동작하는가?

**목표**
- 본인 환경(Windows + 카톡 PC 버전)에서 카톡 HWND 트리 실측
- 그 실측 결과로 `WindowSpec` 의 `class_name` / `expected_input_class` 값 확정
- Step 단위 자동화 구현 (오발송 방지 3중 방어 포함)
- **테스트용 톡방(나와의 채팅 / 본인 부계정 / 동료에게 양해)** 으로 5회 연속 성공 발송

**산출물**
- `scripts/investigate.py` — 카톡 HWND/UIA 트리 덤프 도구
- `docs/investigation-result.md` — 실측 결과 기록 (kakao-sender v2의 `docs/investigation.md` 와 비교)
- `lovable_agent/output/window_spec.py` — 실제 카톡 값으로 채워진 `WindowSpec`
- `lovable_agent/output/hwnd_utils.py` — win32 API 래퍼
- `lovable_agent/output/steps/*.py` — 모든 Step 구현체
- `lovable_agent/output/kakao_sender.py` — Step 오케스트레이션
- `lovable_agent/output/screenshot.py`

**진입 조건**
- Phase 1 완료
- 본인 PC에 카톡 PC 클라이언트 로그인 상태
- 테스트용 톡방 확보 (가급적 "나와의 채팅" 으로 시작)

**완료 기준**
- [x] `investigate.py` 가 카톡 메인 창·채팅창의 win32 HWND + UIA 트리를 사람이 읽을 수 있게 출력한다 *(2026-05-23)*
- [x] kakao-sender v2 가 발견한 패턴(`RICHEDIT50W`, top-level HWND 등)이 본인 PC에서도 확인된다 *(2026-05-23, RICHEDIT50W + EVA_Window_Dblclk + EVA_VH_ListControl_Dblclk 직접 확인)*
- [x] **테스트 톡방에 `[AI 자동 팔로우업] 테스트` 메시지를 5회 연속 발송 성공** (실패 0회) *(2026-05-23, "나와의 채팅" 대상, tab_mode=chats + open_method=double_click 으로 5/5 성공)*
- [x] 일부러 잘못된 톡방명을 줬을 때 **3중 방어 중 한 곳에서 차단** (오발송 0) *(단위 테스트로는 통과 — `test_kakao_sender.py::test_any_pre_send_failure_blocks_typing` 4개 케이스. 실 발송 검증도 단계적 실패 사례로 확인됨 — 잘못된 친구 탭에서 시작 시 ensure_friends_tab/open_chatroom 단계에서 차단)*
- [ ] 발송 직후 스크린샷이 정상 캡처된다 *(screenshot.py 는 placeholder. PressEnter 직후 캡처 통합은 Phase 5 운영 단계에서 추가)*
- [x] 각 Step 별 단위 테스트 작성 (모킹된 HWND/UIA로) *(2026-05-23, **124 passed** — hwnd_utils 10 + window_spec 21 + steps 12 + kakao_sender 9 + 기존 72)*

**위험 / 주의**
- 본인 카톡 버전이 kakao-sender v2가 실측한 버전과 다르면 `class_name` 등이 다를 수 있음 — 그래서 `investigate.py` 가 먼저
- 카톡 다크모드/라이트모드, DPI 100%/150% 등 변형 환경에서도 한 번씩 시험
- 3중 방어를 일부러 무력화해보는 "역시험"이 중요 (잘 막히는지 확인)

---

### Phase 3. 분석·저장 로직 구현 (mock 으로 우회)

> 카톡 자동화가 검증된 위에, AI 분석·저장·스케줄링을 mock 의존성으로 다 만든다.

**목표**
- 카톡 `.txt` 익스포트 포맷 파서
- LLM Extractor 인터페이스 + mock 구현
- SQLite 저장소 + 발송 큐
- 스케줄러 (APScheduler)
- 6시간 룰
- safety 모듈 (화이트리스트 더블체크, 접두어 강제)
- Notifier (데스크톱 알림)

**산출물**
- `lovable_agent/ingest/kakao_parser.py`
- `lovable_agent/ingest/txt_watcher.py`
- `lovable_agent/process/extractor.py`
- `lovable_agent/storage/sqlite_repo.py` + migrations
- `lovable_agent/scheduling/scheduler.py`
- `lovable_agent/safety/whitelist.py`
- `lovable_agent/safety/prefix.py`
- `lovable_agent/output/notifier.py`

**진입 조건**
- Phase 2 완료
- 본인의 실제 카톡 `.txt` 샘플 1~2개 확보 (테스트 톡방 익스포트)

**완료 기준**
- [x] `.txt` 샘플 → 파서 통과 → mock Extractor 가 그럴듯한 4요소 반환 → mock 노션에 기록 → 발송 큐 enqueue 까지 **한 번에 흐름** *(2026-05-23, `--dry-run` 6단계 통합 흐름 확인)*
- [x] 위 흐름의 끝에서 Phase 2의 카톡 Sender 호출 + 테스트 톡방 발송 *(2026-05-23, SendDispatcher 로 Scheduler ↔ KakaoSender 연결. dry-run mock sender 로 흐름 검증, 실 카톡 발송은 send_test.py 로 5/5 성공)*
- [x] 6시간 룰 단위 테스트 (시간 mock 으로) *(2026-05-23, `test_scheduler.py` 18개 케이스)*
- [x] 화이트리스트 더블체크 단위 테스트 (캐시 vs mock 노션 불일치 케이스) *(2026-05-23, `test_whitelist.py` 7개)*
- [x] `[AI 자동 팔로우업] ` 접두어 강제 어서션 테스트 *(2026-05-23, 스케줄러 빌드 메시지 검증 + safety/prefix.py 멱등 검증)*
- [ ] Notifier 가 데스크톱 알림을 실제로 띄운다 (눈으로 확인) *(Windows 본인 PC 확인 필요)*

**위험 / 주의**
- mock Extractor 는 "고정된 가짜 응답"이 아니라 **"항상 같은 입력에 같은 가짜 응답"** 정도면 충분. Phase 4에서 실제 Claude로 갈아끼울 것
- SQLite 마이그레이션은 단순하게 — `peewee`나 `alembic` 같은 무거운 도구 도입 X. 자체 SQL 파일로 충분

---

### Phase 4. 외부 연결 (여기서 처음으로 API/Notion 붙임)

> 지금까지 mock 으로 돌던 시스템에 실제 Claude API 와 Notion 을 끼운다.

**목표**
- Anthropic API 키 발급 + 월 30,000원 상한 설정
- Anthropic SDK 기반 `AnthropicAPIClient` 구현 (LLMClient Protocol의 실제 구현)
- Notion Integration 생성 + 회사 워크스페이스 권한 처리
- Notion 에 Tasks / Whitelist / Inbox DB 3개 생성
- `NotionRepository` 구현 (mock 의 인터페이스 그대로)
- `config.toml` 에 실제 DB ID·토큰 환경변수 매핑

**산출물**
- `lovable_agent/process/anthropic_client.py`
- `lovable_agent/storage/notion_repo.py`
- 실제 노션 DB 3개 (스키마는 ARCHITECTURE §5.1 그대로)
- `.env.example` (환경변수 템플릿)

**진입 조건**
- Phase 3 완료
- 회사 노션 정책 확인 완료 (외부 AI 연결 허용 여부)
- Anthropic Console 계정

**완료 기준**
- [ ] Anthropic Console에서 **월 30,000원 상한 설정 완료** (스크린샷 보관 권장)
- [ ] 실제 카톡 .txt 1개로 **AI 가 4요소를 80% 이상 정확도로 추출** (PRD §8 성공지표)
- [ ] mock 으로 돌던 통합 시나리오가 실제 API + 실제 노션으로도 동일 결과
- [ ] 노션 Tasks DB 에 `검토 대기` 상태로 새 행이 정상 추가됨
- [ ] 노션에서 `확정` 으로 바꾸면 발송 큐에 정상 enqueue 됨

**위험 / 주의**
- **회사 노션 정책으로 외부 AI 연결이 막히면 여기서 멈춤** — PRD R6. 미리 IT 확인 권장.
- Notion Integration OAuth 시 회사 워크스페이스 admin 승인이 필요할 수 있음
- LLM Extractor 의 프롬프트는 mock 단계에서 짠 것을 그대로 쓰지 말고, 실제 카톡 `.txt` 로 한두 번 튜닝 필요
- API 호출 비용은 처음 며칠 자주 확인 (Console → Usage)

---

### Phase 5. 통합·안전장치·운영 시작

> 모든 게 연결된 상태에서 안전장치를 최종 검증하고 실 운영 개시.

**목표**
- 안전장치 3종(검토 대기 / 화이트리스트 더블체크 / 발송 이력+스크린샷) 모두 적용
- Windows 작업 스케줄러 등록 (부팅 자동 실행)
- 한 달 운영 후 성공 지표 점검

**산출물**
- 부팅 자동 실행 등록 (`pythonw -m lovable_agent`)
- 운영 메트릭 노션 페이지 (ARCHITECTURE §10.2)
- `docs/operations.md` — 운영 가이드 (장애 대응, 수동 개입 절차)

**진입 조건**
- Phase 4 완료
- 화이트리스트 톡방 결정 + 노션 등록 완료

**완료 기준**
- [ ] PC 재부팅 후 데몬 자동 실행 확인
- [ ] 5종 시나리오(PRD §5의 S1~S5) 손으로 다 통과
- [ ] **모든 NFR-1 (Sev-1) 통과** — 잘못된 톡방 발송 0건, 검토 대기 항목 자동발송 0건, 접두어 누락 0건, 발송 이력 정확도 100%
- [ ] 1주일 실 운영 후 성공 지표 1차 점검 (정확도·매니징 부담 정성평가)
- [ ] 1개월 운영 후 PRD §8 성공 지표 평가 — 통과 시 MVP 완료, 실패 시 회고 후 다음 사이클

**위험 / 주의**
- 운영 시작 직후 1주일은 "자동 발송 켜고 끄고"를 자유롭게 하면서 익숙해질 것
- 잘못 발송 사고가 0건이라는 게 자동화 검증 끝 — 어느 한 건이라도 나오면 즉시 멈추고 원인 분석
- 본 시스템이 매니저 본인 부담을 줄이는 게 목적이지, 새로운 부담을 추가하면 안 됨. 사용감이 안 좋으면 사용 안 하게 됨

---

## 4. 미루어 둔 외부 의존성 — 어떻게 mock 으로 우회하나

| 외부 의존성 | mock 전략 | 진짜로 갈아끼우는 시점 |
|---|---|---|
| Anthropic Claude API | `MockLLMClient` — 입력 텍스트 길이·키워드에 따라 정해진 4요소 JSON 반환 | Phase 4 |
| Notion API (Tasks·Whitelist·Inbox) | `MockNotionRepository` — Python dict 기반 인메모리 저장 | Phase 4 |
| 카톡 PC | `FakeKakaoEnvironment` — Step 단위 모킹 (HWND 호출을 가짜 응답으로 대체) | Phase 2 일부, Phase 3·5 통합 테스트 |

**핵심 원칙**: 인터페이스(Protocol)는 처음부터 진짜 같이 설계, 구현체만 mock → real 로 갈아끼움. 이 부분은 ARCHITECTURE §4.3·§15에 이미 반영.

---

## 5. 일정 가늠 (러프, 입문자 페이스 기준)

> 본업 외 시간으로 진행한다고 가정. 익숙한 사람이면 절반 정도 단축 가능.

| Phase | 가늠 | 비고 |
|---|---|---|
| Phase 1 | 2~3일 | Python 패키지 구조에 익숙해지는 시간 포함 |
| Phase 2 | **3~5일** | 카톡 HWND 실측이 중심. 시행착오 시간 넉넉히 |
| Phase 3 | 3~4일 | 가장 코드량 많은 단계 |
| Phase 4 | 1~2일 | Notion·API 셋업 자체는 빠름, 회사 정책 확인이 변수 |
| Phase 5 | 1주 + 운영 1개월 | 운영 안정화 시간 |

**총** 약 2~3주의 작업 + 1개월 운영 시험.

---

## 6. "지금 당장 다음에 뭐 하지?" 체크리스트

Phase 1 은 끝났다 (2026-05-23). 다음 한 주 안에 할 일은 **Phase 2 시작 준비**:

- [ ] 본인 PC 에 카카오톡 PC 클라이언트가 로그인된 상태 확인
- [ ] 테스트용 톡방 확보 (가급적 "나와의 채팅" 으로 시작)
- [x] `scripts/investigate.py` 작성 *(2026-05-23)*
- [x] `--auto-open-self-chat` 옵션 추가 — 카톡 메인 창 포그라운드 + 친구 탭 확인 + Ctrl+F + 검색 + Alt+Enter + HWND diff + WM_CLOSE 자동 닫기 *(2026-05-23)*
- [x] 본인 PC 에서 실행 *(2026-05-23, 자동 오픈은 친구 탭 미활성으로 가드레일 중단됨. 그 와중에 일반 진단이 '김훈희' 채팅창에서 RICHEDIT50W 직접 확인)*
- [x] 덤프 결과를 보고 kakao-sender v2 의 패턴과 비교 — **완전 일치** *(2026-05-23)*
  - 메인 창 = `EVA_Window_Dblclk` + title `카카오톡` ✓
  - 채팅창 = `EVA_Window_Dblclk` (제목 있음) ✓
  - 메시지 입력창 = `RICHEDIT50W` ✓
  - 메시지 리스트 = `EVA_VH_ListControl_Dblclk` ✓
- [x] 본인 환경 값으로 `WindowSpec` 의 `class_name` / `expected_input_class` 기본값 확정 *(2026-05-23, domain.py 갱신: class_name=EVA_Window_Dblclk, expected_input_class=RICHEDIT50W, expected_list_class=EVA_VH_ListControl_Dblclk)*

Phase 2 끝났다는 신호 = "테스트 톡방에 `[AI 자동 팔로우업] 테스트` 5회 연속 발송 성공 + 잘못된 톡방명에서 3중 방어 차단 확인"

---

## 7. 이 문서의 사용법

- 매일 작업 시작 전 §6 체크리스트 확인
- 각 Phase 끝나면 **완료 기준 체크박스를 한 번에 다 채워서 커밋** — 빈 칸이 있으면 다음 Phase 진입 X
- README의 Phase 로그도 같은 시점에 업데이트
- 새로운 위험을 발견하면 해당 Phase의 "위험 / 주의" 섹션에 추가

---

## 부록. 변경 이력

| 날짜 | 변경 |
|---|---|
| 2026-05-23 | 초안 작성 (Phase 1~5). 외부 연결을 Phase 4로 미루는 새 순서 반영. |
| 2026-05-23 | Phase 1 완료. 코드 골조·Mock 구현·검증 4종 통과. §6 체크리스트를 Phase 2 준비로 갱신. |
| 2026-05-23 | Phase 2 `investigate.py` 코드 작성 완료 (본인 PC 실 실행 대기). Phase 3 mock 가능 부분 완료 — 카톡 파서·SQLite·Whitelist·Extractor·Scheduler·Watcher·Notifier. pytest 70 passed. |
| 2026-05-23 | Phase 2 본구현 완료 — hwnd_utils + window_spec + Step 6개 + kakao_sender 오케스트레이션. WindowSpec 본인 환경 실측 기본값 확정. pytest **124 passed**. 실 카톡 발송 검증은 본인 PC 별도 단계. |
| 2026-05-23 | **Phase 2 완료 기준 달성** — "나와의 채팅" 대상 5/5 발송 성공. 핵심 실측 결과: 본인을 검색하려면 **친구 탭이 아니라 채팅 탭**에서 본명으로, **Alt+Enter 가 아니라 더블클릭**으로 별도 창 오픈. 코드에 양쪽 케이스 모두 옵션화. |
| 2026-05-23 | **Phase 3 통합 완료** — SendDispatcher 신규로 Scheduler 발송 큐 ↔ KakaoSender 연결. dry-run 7단계 흐름 (파서 → Extractor → Whitelist → Scheduler → 큐 미리보기 → Dispatcher → Notifier). pytest 133 passed. |
| 2026-05-23 | **Phase 3 실 AI 분석 검증** — ClaudeCLIClient 로 실 카톡 .txt 91 메시지 → 17초만에 actionable 업무 1건 정확 추출. Anthropic API 키 없이 동작. Phase 4 안 거치고도 Phase 3 흐름 검증 완료. pytest 144 passed. |
| 2026-05-23 | **Phase 4 Notion 코드 완성** — notion_repo.py + setup_notion.py + 단위 테스트. 사용자가 토큰 발급 + 부모 페이지 결정 + setup 1회 실행만 남음. Inbox 를 ARCHITECTURE 의 "페이지" 가 아니라 DB 로 결정. pytest 163 passed. |
| 2026-05-24 | **Phase 4 Notion 실 통합 검증** — 사용자 토큰으로 DB 3개 생성·schema 적용·왕복 쿼리 성공. notion-client 3.x API 변경 (databases.query → data_sources.query, DB schema 는 data_sources.update 로 2단계) 대응. |
| 2026-05-24 | **진정한 e2e + Phase 5 운영 데몬 코드 완성** — integration_e2e.py 로 mock 0개 통합 검증, main.py 운영 루프 (매 분 polling + 발송, Ctrl+C 우아한 종료, 파일 로그). install_startup.py 로 시작 프로그램 폴더 .bat 자동 등록. 사용자 1회 실행만 남음. |
