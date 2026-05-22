# Architecture — Lovable 업무 팔로업 에이전트

> 작성일: 2026-05-23
> 버전: v1.0 (MVP)
> 관련 문서: [PRD.md](PRD.md), [DECISIONS.md](DECISIONS.md)

이 문서는 PRD에 정의된 요구사항을 **어떻게 기술적으로 구현할지** 정의합니다.

---

## 1. 개요

이 시스템은 매니저 PC에 상주하는 **Python 백그라운드 데몬 + 외부 서비스(Anthropic API, Notion API) + PC 카톡 클라이언트** 의 조합으로 구성된다. 카톡 대화의 `.txt` 익스포트를 분석해 노션 DB에 업무를 정리하고, 마감일에 맞춰 카톡 PC 클라이언트를 UI 자동화로 조작해 자동 메시지를 발송한다.

---

## 2. 기술적 목표·제약

### 2.1. 기술적 목표

- **간결성**: 입문자가 유지보수 가능한 수준의 구조 — Python 단일 프로세스 기반
- **격리**: AI 호출부는 인터페이스 분리, 추후 다른 운영 방식으로 전환 가능
- **회복력**: PC 재부팅·일시적 네트워크 단절에도 데이터 손실 없음
- **추적성**: 모든 자동 발송에 대해 사후 감사 가능 (로그 + 스크린샷)

### 2.2. 기술적 제약 (외부에서 주어짐)

- 카톡 일반 톡방 자동 발송은 PC UI 자동화 외 수단 없음 → Windows PC 필수
- 카톡 PC 메시지 DB는 암호화 → 수동 `.txt` 익스포트로 갈음
- Anthropic API 월 사용량 30,000원 상한

---

## 3. 상위 아키텍처

### 3.1. 시스템 컨텍스트

```
                  ┌────────────────────────────┐
                  │      매니저 (사용자)        │
                  └──┬──────────────────┬──────┘
       카톡 .txt 익스포트                │ 노션에서 검토·확정
                     │                  │
                     ▼                  ▼
                 ┌────────────────────────────┐
   ┌──────────►  │  Lovable 업무 팔로업       │  ◄──────────┐
   │             │       Agent (PC 데몬)       │             │
   │             └───┬────────────────┬───────┘             │
   │                 │                │                     │
   ▼                 ▼                ▼                     │
┌─────────┐   ┌──────────────┐  ┌──────────────┐    ┌──────────────┐
│ Notion  │   │  Anthropic   │  │ KakaoTalk PC │    │   담당자들    │
│  (API)  │   │ Claude API   │  │  (자동 조작) │───►│  (메시지 수신) │
└─────────┘   └──────────────┘  └──────────────┘    └──────────────┘
```

### 3.2. 시스템 내부 컴포넌트

```
┌─────────────────────────── PC 데몬 (Python) ───────────────────────────┐
│                                                                          │
│  ┌──────────────────┐    ┌───────────────────┐                          │
│  │  Inbox Watcher   │    │  Notion Inbox     │                          │
│  │  (폴더 감시)      │    │  Poller           │                          │
│  └────────┬─────────┘    └────────┬──────────┘                          │
│           │ 새 .txt / 메모        │                                      │
│           └───────────┬───────────┘                                      │
│                       ▼                                                  │
│           ┌────────────────────────┐                                    │
│           │   Task Extractor       │ ◄── LLMClient (인터페이스)         │
│           │  (.txt + 기존 업무목록  │     ↓                              │
│           │   → 4요소 JSON 추출)   │     AnthropicAPIClient (구현)      │
│           └────────────┬───────────┘     ↓                              │
│                        ▼                 Anthropic Claude API           │
│           ┌────────────────────────┐                                    │
│           │  Repository            │ ◄────► Notion API (Tasks DB)       │
│           │  (Notion ↔ SQLite 동기) │ ◄────► SQLite (큐·이력·dedup)    │
│           └────────────┬───────────┘                                    │
│                        │                                                 │
│                        ▼                                                 │
│           ┌────────────────────────┐                                    │
│           │  Scheduler             │     매 분 단위로 due 체크          │
│           │  (APScheduler)         │                                    │
│           └────────────┬───────────┘                                    │
│                        │ "발송할 시간이다"                                │
│                        ▼                                                 │
│           ┌────────────────────────┐                                    │
│           │  KakaoTalk Sender      │ ─── pywinauto ─► 카톡 PC 클라이언트│
│           │  (화이트리스트 검증 +   │                                    │
│           │   접두어 + 스크린샷)    │                                    │
│           └────────────┬───────────┘                                    │
│                        │ 발송 결과                                       │
│                        ▼                                                 │
│           ┌────────────────────────┐                                    │
│           │  Notifier              │ ─── 데스크톱 알림 (실패 / 수동필요) │
│           └────────────────────────┘                                    │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 4. 컴포넌트별 상세

### 4.1. Inbox Watcher (`ingest/txt_watcher.py`)

- **역할**: 지정된 폴더(`~/lovable-agent/inbox/`)를 감시하다가 새 `.txt` 파일이 들어오면 처리 대기열에 올린다.
- **구현**: `watchdog` 라이브러리 사용
- **중복 방지**: 파일 SHA-256 해시를 SQLite `processed_files` 테이블에 기록. 같은 해시는 재처리 안 함.
- **출력**: 파싱된 카톡 대화 내용 (날짜/발화자/메시지 분리)

### 4.2. Notion Inbox Poller (`ingest/notion_poller.py`)

- **역할**: 노션의 Inbox 페이지를 5분마다 읽어 새 메모 블록을 처리 대기열에 올린다.
- **구현**: `notion-client` SDK + 폴링 루프
- **중복 방지**: 처리 완료된 블록은 노션에서 "처리됨" 태그(또는 색상 변경)로 마킹
- **출력**: 메모 텍스트 + 작성 시각

### 4.3. Task Extractor (`process/extractor.py`)

- **역할**: 비정형 텍스트에서 4요소(What/Context/Due Date/Assignee) + 중복 판별을 수행한다.
- **흐름**:
  1. 노션 Tasks DB에서 진행 중인 업무 목록 조회 (Title / Assignee / Due Date / 한줄요약)
  2. `LLMClient.extract_tasks(new_text, existing_tasks)` 호출
  3. 반환된 JSON을 검증 후 다음 분기:
     - `is_duplicate_of: <task_id>` 가 있으면 → 해당 업무에 메모 추가
     - 없으면 → 새 업무로 `검토 대기` 상태로 노션에 추가
- **LLM 인터페이스**:
  ```python
  class LLMClient(Protocol):
      def extract_tasks(
          self, text: str, existing_tasks: list[TaskSummary]
      ) -> ExtractionResult: ...
  ```
- **기본 구현**: `AnthropicAPIClient` (Anthropic SDK 사용, `claude-sonnet-4-6` 모델)
- **출력 스키마**:
  ```json
  {
    "tasks": [
      {
        "title": "...",
        "what": "...",
        "context": "...",
        "due_date": "2026-06-01T15:00:00",
        "assignee": "김매니저",
        "source_detail": "톡방명: MOP 운영",
        "is_duplicate_of": null
      }
    ]
  }
  ```

### 4.4. Repository (`storage/repository.py`)

- **역할**: 시스템 상태(작업·발송 큐·이력)의 단일 진실원(SoT)을 노션과 SQLite로 나눠 관리.
- **노션 (사용자 view)**:
  - `Tasks` DB: 매니저가 보는 업무 목록 (PRD §6.5 사용자 통제 대상)
  - `Chatroom Whitelist` DB: 발송 허용 톡방
  - `Inbox`: 수동 메모 입력
- **SQLite (운영 상태 / `~/lovable-agent/agent.db`)**:
  - `processed_files`: 이미 처리한 `.txt` 해시
  - `send_queue`: 발송 예정 큐 (task_id, scheduled_at, chatroom, message, status)
  - `send_history`: 발송 시도 결과 (성공/실패/스크린샷 경로 임시)
  - `whitelist_cache`: 화이트리스트 캐시 (5분 TTL)
- **동기화 방향**:
  - 노션 → SQLite: 매 폴링 시 (확정된 업무·화이트리스트 변경 사항)
  - SQLite → 노션: 발송 시도 결과 (이력 + 스크린샷 첨부)

### 4.5. Scheduler (`scheduling/scheduler.py`)

- **역할**: 매 분마다 due 도래한 업무를 발송 큐에 enqueue.
- **구현**: `APScheduler`의 `IntervalTrigger`
- **로직**:
  1. 노션 Tasks에서 `Status = 확정` + `AI Followup Enabled = true` 항목 조회
  2. 마감일 기준으로 발송 시점 계산 (예: D-1 09:00, D-day 09:00 — 정책으로 명문화)
  3. 아직 발송되지 않은 시점이면 `send_queue`에 추가
- **6시간 룰**: 발송 시점 이미 지난 경우, `(now - scheduled_at) > 6h` 면 `status = skipped_too_late` 로 기록 후 Notifier로 사용자에게 알림.

### 4.6. KakaoTalk Sender (`output/kakao_sender.py`)

- **역할**: 발송 큐에서 항목을 꺼내 카톡 PC를 자동 조작하여 메시지 발송.
- **자동화 기술**: **win32 HWND (`EnumChildWindows`) 주력 + UI Automation(UIA) 보조** — Python 라이브러리는 `pywin32` + `uiautomation` 병용. [kakao-sender (v2)](https://github.com/TurnaboutHero/kakao-sender-v2) Phase 3 실측 결과에 따른 설계.
- **이런 조합의 근거**:
  - 카톡 PC 메인 창은 UIA 트리가 빈 `PaneControl` 중첩뿐 → 친구 탭·검색창·친구 목록을 UIA로 식별 곤란
  - 채팅창(1:1 / 단톡방)은 **별도 top-level HWND** 이며 내부에 표준 `RICHEDIT50W` 입력창과 메시지 `ListControl` 노출 → win32 API로 직접 접근 가능
  - 메인 창의 활성 탭은 `ContactListView_*` / `ChatRoomListView_*` 같은 **자식 HWND 이름 + 사각형 좌표**로 판정 가능
  - **텍스트 입력은 `SendMessage(WM_SETTEXT)`** 사용 → 클립보드·포커스 상태에 독립
- **흐름**:
  1. 큐에서 다음 항목 pop
  2. **화이트리스트 더블체크**: 캐시 + 노션 원본 둘 다 조회. 미일치 시 즉시 발송 중단, Notifier로 사용자에게만 표시
  3. 메시지 텍스트 맨 앞에 `[AI 자동 팔로우업] ` prepend
  4. 오발송 방지 3중 방어 적용 (§4.6.3)
  5. 채팅창 HWND 식별 → `WM_SETTEXT`로 메시지 본문 입력 → Enter 전송
  6. 발송 직후 채팅창 HWND 스크린샷 캡처 (`Pillow.ImageGrab`)
  7. 노션 발송 이력에 결과 + 스크린샷 첨부
  8. 실패 시 큐 상태를 `failed`로 표시하고 Notifier 호출
- **재시도 정책**: 일시 실패 시 최대 3회 재시도, 그 후 사용자에게 알림

#### 4.6.1. `WindowSpec` 데이터클래스 (윈도우 식별)

화이트리스트 DB의 톡방 식별 정보를 단순 문자열로 다루지 않고 다음과 같은 데이터클래스로 캡슐화한다.

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class WindowSpec:
    title_exact: str               # 채팅창 제목 (완전일치, 동명이인 방지)
    class_name: str = "EVA_ChildWindow"   # 카톡 채팅창의 win32 클래스 이름 (예시)
    process_name: str = "KakaoTalk.exe"
    expected_input_class: str = "RICHEDIT50W"  # 입력창 자식 HWND 클래스

    def matches_hwnd(self, hwnd: int) -> bool:
        """win32 GetWindowText / GetClassName / GetWindowThreadProcessId 로 검증."""
        ...
```

- **단순 타이틀 매칭은 동명 톡방·동명이인 오발송 위험** → 클래스 이름 + 프로세스 + (입력창 클래스 존재) 조합으로 안정성 확보
- 노션 Whitelist DB의 컬럼이 이 데이터클래스의 필드들과 1:1 매핑됨
- 정확한 `class_name` 값은 Phase 2에서 `scripts/investigate.py`로 카톡 실제 HWND 트리를 덤프해 확정

#### 4.6.2. Step 단위 자동화 추상화

발송 한 번을 한 함수에 다 박지 않고, 각 단계를 별도 `Step` 클래스로 분리한다.

```python
class Step(Protocol):
    name: str
    def execute(self, ctx: SendContext) -> None: ...

class EnsureFriendsTabStep(Step): ...      # ① 친구 탭으로 강제 리셋
class SnapshotHwndsStep(Step): ...         # ② 현재 카톡 top-level HWND 스냅샷
class OpenChatroomStep(Step): ...          # 톡방 검색/선택
class VerifyChatroomTitleStep(Step): ...   # ③ 채팅창 제목 완전일치 검증
class TypeMessageStep(Step): ...           # WM_SETTEXT 로 본문 입력
class PressEnterStep(Step): ...            # 전송
class CaptureScreenshotStep(Step): ...     # 발송 직후 스크린샷
```

①②③은 §4.6.3 오발송 방지 3중 방어를 구성한다.

- 어느 Step에서 실패했는지 명확 → 노션 발송 이력의 `error_detail`이 구체적
- Step 단위 재시도 가능
- 단위 테스트 시 각 Step을 모킹 가능 → 카톡 PC 없이도 발송 로직 테스트 가능

#### 4.6.3. 오발송 방지 3중 방어

`[AI 자동 팔로우업]` 메시지가 잘못된 대상에게 가는 사고는 PRD §NFR-1.1의 Sev-1 위반이다. kakao-sender v2의 Phase 3 설계를 그대로 차용해 3중으로 막는다.

| 방어선 | 무엇을 한다 | 막아주는 사고 |
|---|---|---|
| **① 친구 탭 강제** | 발송 시작 전 항상 카톡 메인 창의 **친구 탭**으로 한 번 리셋. 직전에 열려 있던 임의의 채팅창 컨텍스트를 명시적으로 비움 | 직전 사용자 액션이 만든 잔여 포커스가 엉뚱한 입력창에 텍스트 떨구는 사고 |
| **② HWND 스냅샷 diff** | 톡방 열기 직전 카톡 top-level HWND 목록을 캡처, 톡방 열기 후 다시 캡처. **새로 생긴 HWND 1개**만 발송 대상으로 인정 | 의도한 톡방이 안 열렸는데 기존에 떠 있던 다른 채팅창에 입력하는 사고 |
| **③ 채팅창 제목 완전일치** | 발송 직전 대상 HWND의 `GetWindowText` 결과가 `WindowSpec.title_exact`와 **완전히 동일**해야만 전송. 부분일치 / 정규식 / 공백차이 불허 | 동명 톡방·단톡방·검색결과 첫 항목에 맹목 발송하는 사고 |

세 단계 모두 통과해야만 `TypeMessageStep` → `PressEnterStep`이 실행된다. 어느 하나라도 실패하면 즉시 발송 중단 + Notifier 호출 + `send_history.success = false`.

#### 4.6.4. 상태 감지 (UIA 우선 + 시간 기반 fallback)

"메시지가 실제로 전송되었는가"를 픽셀 색상 한 점으로 판단하는 방식(원본 v5의 결함)은 다크모드/DPI/카톡 버전에 취약하다. 대신:

1. **UIA 우선**: 채팅창의 메시지 `ListControl`에서 가장 마지막 아이템의 발화자·시각 속성을 읽어 "방금 보낸 내 메시지"인지 확인
2. **시간 기반 fallback**: UIA가 응답하지 않으면 발송 후 1초 슬립 + 채팅창 스크린샷의 마지막 메시지 영역만 OCR로 부분 확인 (선택 옵션)
3. **실패해도 발송 자체는 성공으로 간주하지 않음** → 상태 감지 실패 시 `send_history.success = uncertain`, 사용자에게 알림

이 컴포넌트는 `output/detection.py`에 분리.

#### 4.6.5. 스팸 회피 (다발 발송 시)

같은 톡방·서로 다른 톡방에 단시간 다발 발송 시 카톡이 스팸으로 판단할 위험. 다음을 적용:

- **간격 랜덤화**: 메시지 간 기본 간격을 5~15초 사이 균등분포 랜덤 sleep
- **주기 휴식**: 연속 10건 발송 후 60~120초 휴식
- **문구 변주 (선택)**: 동일 내용 반복 시 자연어 변형을 LLM에 한 번 의뢰해 다양화

MVP 기준 매니저 단일 사용자 발송량은 적어 이 위험이 낮으나, 구조적으로 미리 설계에 반영해둠.

설정 키는 `config.toml`의 `[kakao]` 섹션에 노출.

### 4.7. Notifier (`output/notifier.py`)

- **역할**: 사용자 PC에 데스크톱 알림을 띄움.
- **구현**: `winotify` 또는 `plyer.notification`
- **알림 종류**:
  - 자동 발송 실패
  - 화이트리스트 미일치로 자동 발송 스킵 → "수동 처리 필요"
  - 6시간 초과 알림 누락 → "수동 처리 필요"
  - 시스템 자체 오류

---

## 5. 데이터 모델

### 5.1. 노션 (사용자 view)

#### `Tasks` DB

| 필드 | 타입 | 채우는 주체 | 비고 |
|---|---|---|---|
| Title | Text | 시스템 (AI) | 한줄 요약 |
| What | Text | 시스템 | 구체적 업무 |
| Context | Text | 시스템 | 발생 맥락 |
| Due Date | Date | 시스템 / 사용자 | 마감일 |
| Assignee | Person / Text | 시스템 | 담당자 |
| Source | Select | 시스템 | `kakao` / `manual` (Phase 2: `email`, `calendar`) |
| Source Detail | Text | 시스템 | 톡방명, 메모 작성일 등 |
| Status | Select | 사용자 | `검토 대기` / `확정` / `진행 중` / `완료` / `취소` |
| AI Followup Enabled | Checkbox | 시스템 자동 ☑ + 사용자 변경 가능 | 화이트리스트 매칭 시 기본 체크 |
| 발송 이력 | Text | 시스템 | 발송 로그 요약 (스크린샷 링크 포함) |

#### `Chatroom Whitelist` DB

| 필드 | 타입 | 비고 |
|---|---|---|
| 톡방명 | Text | 사람이 보는 이름 |
| 카톡 윈도우 타이틀 | Text | pywinauto가 찾을 정확한 타이틀 (정규식 가능) |
| Active | Checkbox | true일 때만 발송 대상 |
| 비고 | Text | |

#### `Inbox` 페이지

- 자유 텍스트 + `.txt` 파일 첨부 입력처
- 시스템이 처리한 블록은 색상/이모지로 마킹

### 5.2. SQLite (운영 상태)

```sql
CREATE TABLE processed_files (
    file_hash TEXT PRIMARY KEY,
    file_name TEXT NOT NULL,
    processed_at TIMESTAMP NOT NULL
);

CREATE TABLE send_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,            -- 노션 Tasks 페이지 ID
    chatroom_title TEXT NOT NULL,     -- 화이트리스트 윈도우 타이틀
    message TEXT NOT NULL,
    scheduled_at TIMESTAMP NOT NULL,
    status TEXT NOT NULL,             -- queued / sent / failed / skipped_too_late / skipped_not_whitelisted
    attempted_count INTEGER DEFAULT 0,
    last_attempted_at TIMESTAMP
);

CREATE TABLE send_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    send_queue_id INTEGER NOT NULL,
    sent_at TIMESTAMP NOT NULL,
    success BOOLEAN NOT NULL,
    error_detail TEXT,
    screenshot_path TEXT,             -- 임시 로컬 경로 (노션 업로드 후 삭제)
    FOREIGN KEY (send_queue_id) REFERENCES send_queue(id)
);

CREATE TABLE whitelist_cache (
    chatroom_title TEXT PRIMARY KEY,
    active BOOLEAN NOT NULL,
    cached_at TIMESTAMP NOT NULL
);
```

---

## 6. 주요 데이터 흐름 (시퀀스)

### 6.1. 새 카톡 `.txt` 처리 (FR-1, FR-2)

```
User                Watcher        Extractor        Anthropic API     Notion
 │                    │               │                  │              │
 │ .txt 폴더에 떨굼   │               │                  │              │
 │───────────────────►│               │                  │              │
 │                    │ 해시 확인     │                  │              │
 │                    │ (중복 X)      │                  │              │
 │                    │──────────────►│                  │              │
 │                    │               │ 기존 진행중 업무 조회            │
 │                    │               │─────────────────────────────────►│
 │                    │               │◄─────────────────────────────────│
 │                    │               │                  │              │
 │                    │               │ 4요소 추출 요청   │              │
 │                    │               │─────────────────►│              │
 │                    │               │◄─────────────────│ JSON 응답    │
 │                    │               │                  │              │
 │                    │               │ 신규/중복 분기   │              │
 │                    │               │ → Tasks DB 추가/갱신             │
 │                    │               │─────────────────────────────────►│
 │                    │               │                  │              │
 │                    │ 해시 기록     │                  │              │
 │                    │◄──────────────│                  │              │
```

### 6.2. 리마인드 자동 발송 (FR-3)

```
Scheduler        Repository        Sender           Notion         KakaoTalk PC
   │                 │                │                │                │
   │ 매 분 tick      │                │                │                │
   │ due 항목 조회   │                │                │                │
   │────────────────►│                │                │                │
   │                 │ 노션 Tasks 조회                  │                │
   │                 │───────────────────────────────►│                │
   │                 │◄───────────────────────────────│ 확정+활성 목록  │
   │ send_queue       │                │                │                │
   │ enqueue         │                │                │                │
   │────────────────►│                │                │                │
   │                 │                │                │                │
   │                 │ 큐 pop         │                │                │
   │                 │───────────────►│                │                │
   │                 │                │ 화이트리스트   │                │
   │                 │                │ 더블체크       │                │
   │                 │                │───────────────►│                │
   │                 │                │◄───────────────│                │
   │                 │                │                │                │
   │                 │                │ [화이트리스트 일치 시]            │
   │                 │                │ 메시지 prepend │                │
   │                 │                │ pywinauto 발송 │                │
   │                 │                │───────────────────────────────►│
   │                 │                │ 스크린샷 캡처                    │
   │                 │                │                │                │
   │                 │                │ 발송 이력 기록 │                │
   │                 │                │───────────────►│                │
```

### 6.3. 6시간 룰 적용 (FR-4)

```
PC 부팅 직후, Scheduler 첫 tick:
  ├─ send_queue에서 status=queued 항목 조회
  ├─ 각 항목 마다:
  │   if (now - scheduled_at) <= 6h:
  │     → Sender로 정상 전달
  │   else:
  │     → status = skipped_too_late
  │     → Notifier로 사용자에게만 알림
  │     → Tasks DB의 해당 행에 "수동 처리 필요" 표시
```

---

## 7. 기술 스택

| 영역 | 선택 | 버전(권장) | 비고 |
|---|---|---|---|
| 언어 | Python | 3.11+ | 타입힌트·`asyncio`·`Protocol` 활용 |
| AI SDK | `anthropic` | 최신 | Anthropic Claude API |
| 노션 SDK | `notion-client` | 최신 | 비공식이지만 사실상 표준 |
| 카톡 자동화 (주력) | `pywin32` | 최신 | win32 `EnumChildWindows`, `SendMessage(WM_SETTEXT)`, `GetWindowText`, `GetClassName` 사용. kakao-sender v2 Phase 3 실측 결과 반영. |
| 카톡 자동화 (보조) | `uiautomation` | 최신 | UIA로 상태 감지(메시지 리스트 확인) 및 일부 컨트롤 탐색 보조. |
| 폴더 감시 | `watchdog` | 최신 | |
| 스케줄러 | `APScheduler` | 3.x | 인프로세스 |
| 로컬 DB | `sqlite3` (표준 라이브러리) | — | `peewee` 정도의 가벼운 ORM은 선택 |
| 화면 캡처 | `Pillow.ImageGrab` | 최신 | |
| 데스크톱 알림 | `winotify` 또는 `plyer` | 최신 | |
| 부팅 자동실행 | Windows 작업 스케줄러 | — | OS 기능 |
| 패키지 관리 | `uv` 또는 `poetry` | 최신 | `uv` 추천(빠름) |
| 테스트 | `pytest` | 최신 | |

---

## 8. 런타임 / 배포

### 8.1. 실행 모델

- **단일 Python 프로세스** (멀티 스레드: APScheduler 스레드 + watchdog 스레드 + 메인 루프)
- 시작 시: SQLite 초기화 → 노션 동기화 → 스케줄러 시작 → watcher 시작 → 대기
- 종료 시: 진행 중인 발송 완료 후 안전하게 종료

### 8.2. 부팅 자동실행

- Windows 작업 스케줄러에 다음 트리거 등록:
  - "사용자 로그인 시"
  - 작업: `pythonw.exe -m lovable_agent.main`
- 콘솔 창 없이 백그라운드 실행 (`pythonw`)

### 8.3. 설정 (`config.toml`)

```toml
[notion]
api_token_env = "NOTION_API_TOKEN"       # 환경변수 이름
tasks_db_id = "..."
whitelist_db_id = "..."
inbox_page_id = "..."

[anthropic]
api_key_env = "ANTHROPIC_API_KEY"
model = "claude-sonnet-4-6"
fallback_model = "claude-opus-4-7"

[paths]
inbox_folder = "~/lovable-agent/inbox/"
db_path = "~/lovable-agent/agent.db"
screenshot_temp_dir = "~/lovable-agent/screenshots/"

[scheduling]
reminder_check_interval_seconds = 60
notion_poll_interval_seconds = 300
late_reminder_threshold_hours = 6
default_reminder_offsets_hours = [24, 0]   # D-1, D-day

[kakao]
max_send_retries = 3
send_retry_delay_seconds = 30
# 오발송 방지 (§4.6.3)
require_friends_tab_reset = true
require_hwnd_snapshot_diff = true
require_title_exact_match = true
# 스팸 회피 (§4.6.5)
inter_message_delay_min_seconds = 5
inter_message_delay_max_seconds = 15
rest_after_n_messages = 10
rest_duration_min_seconds = 60
rest_duration_max_seconds = 120
# 상태 감지 (§4.6.4)
detection_mode = "uia_first"   # uia_first | time_only | uia_with_ocr_fallback

[safety]
message_prefix = "[AI 자동 팔로우업] "
require_status_confirmed = true
double_check_whitelist = true
```

### 8.4. 비밀 관리

- `NOTION_API_TOKEN`, `ANTHROPIC_API_KEY`는 환경변수 또는 Windows `credential manager`에 저장
- `.env` 파일은 `.gitignore`에 포함, 절대 커밋 금지

### 8.5. 디렉터리 구조

```
lovable-agent/
├── pyproject.toml
├── config.toml
├── lovable_agent/
│   ├── __init__.py
│   ├── main.py                      # 엔트리포인트
│   ├── config.py                    # 설정 로딩
│   ├── ingest/
│   │   ├── txt_watcher.py
│   │   ├── notion_poller.py
│   │   └── kakao_parser.py
│   ├── process/
│   │   ├── extractor.py
│   │   ├── llm_client.py            # Protocol 정의
│   │   └── anthropic_client.py      # 구현
│   ├── storage/
│   │   ├── repository.py
│   │   ├── notion_repo.py
│   │   ├── sqlite_repo.py
│   │   └── migrations/
│   ├── scheduling/
│   │   └── scheduler.py
│   ├── output/
│   │   ├── kakao_sender.py          # 발송 오케스트레이션
│   │   ├── window_spec.py           # WindowSpec 데이터클래스 (§4.6.1)
│   │   ├── hwnd_utils.py            # win32 EnumChildWindows / WM_SETTEXT 래퍼
│   │   ├── detection.py             # 상태 감지 (UIA 우선 + fallback, §4.6.4)
│   │   ├── steps/                   # Step 단위 자동화 (§4.6.2)
│   │   │   ├── __init__.py
│   │   │   ├── base.py              # Step Protocol
│   │   │   ├── ensure_friends_tab.py    # ① 친구 탭 강제 (§4.6.3 방어선 1)
│   │   │   ├── snapshot_hwnds.py        # ② HWND 스냅샷 (§4.6.3 방어선 2)
│   │   │   ├── open_chatroom.py
│   │   │   ├── verify_chatroom_title.py # ③ 제목 완전일치 (§4.6.3 방어선 3)
│   │   │   ├── type_message.py          # WM_SETTEXT 사용
│   │   │   └── press_enter.py
│   │   ├── screenshot.py
│   │   └── notifier.py
│   ├── scripts/
│   │   └── investigate.py           # 카톡 HWND / UIA 트리 덤프 (Phase 2 실측)
│   └── safety/
│       ├── whitelist.py
│       └── prefix.py
└── tests/
    ├── test_extractor.py
    ├── test_whitelist.py
    └── ...
```

---

## 9. 보안 / 프라이버시

- **9.1.** API 키는 평문 파일 금지. 환경변수 또는 Windows 자격증명 저장소
- **9.2.** Anthropic API에 보내기 전, 카톡 .txt에서 명백한 PII(주민번호 패턴 등)는 마스킹 처리(`process/sanitizer.py`)
- **9.3.** SQLite 파일 위치는 사용자 홈 디렉터리 하위, OS 권한으로 보호
- **9.4.** 스크린샷은 노션 업로드 성공 시 로컬 사본 즉시 삭제 (NFR-3.3)
- **9.5.** 노션 토큰의 권한 범위는 필요한 DB·페이지로 최소화

---

## 10. 운영 (Observability)

### 10.1. 로깅

- 표준 출력 대신 파일 로그 (`~/lovable-agent/logs/agent.log`)
- 일자별 회전 (10개 보존)
- 레벨: `INFO` 기본, `DEBUG`는 개발 시
- 민감 정보(메시지 본문, 담당자 이름) 마스킹

### 10.2. 운영 메트릭 (노션 Status 페이지)

운영자가 한눈에 보는 노션 페이지에 다음 표시:
- 마지막 노션 폴링 성공 시각
- 마지막 .txt 처리 성공 시각
- 큐 대기 건수
- 오늘 발송 성공/실패 건수
- 이번 달 누적 Anthropic 토큰 사용량 (Console에서 별도 확인)

### 10.3. 에러 처리 정책

| 에러 종류 | 처리 |
|---|---|
| 노션 API 일시 오류 | 지수 백오프 재시도 (최대 3회), 그래도 실패면 로그 + 다음 사이클 |
| Anthropic API 일시 오류 | 지수 백오프 재시도, 실패 시 해당 .txt를 보류 큐로 이동 후 알림 |
| 카톡 메인 창 못 찾음 | 5초 대기 후 1회 재시도, 실패 시 발송 실패 처리 + 알림 |
| 친구 탭 리셋 실패 (`EnsureFriendsTabStep`) | 1회 재시도, 실패 시 즉시 중단 + 알림 (방어선 1 무너지면 발송 안 함) |
| HWND 스냅샷 diff 결과 0개 또는 2개 이상 (`SnapshotHwndsStep`) | 즉시 중단 + 알림. "새 채팅창이 안 열렸거나, 다른 채팅창이 동시에 열림" |
| 채팅창 제목 불일치 (`VerifyChatroomTitleStep`) | 즉시 중단 + 알림. 화이트리스트의 `title_exact` 변경 가능성 안내 |
| `WM_SETTEXT` 입력 실패 (`TypeMessageStep`) | 채팅창 HWND 재탐색 후 1회 재시도 |
| 상태 감지 실패 (`detection.py`) | 발송 자체는 성공으로 기록하지 않음 → `success = uncertain` + 알림 |
| 스크린샷 캡처 실패 | 발송 자체는 성공으로 기록 + 스크린샷 누락 표기 |
| SQLite 락 | 단일 프로세스이므로 발생 가능성 낮음. 발생 시 짧은 슬립 후 재시도 |

---

## 11. 안전장치 구현 매핑 (PRD §NFR-1)

| PRD 요구 | 구현 위치 | 검증 방법 |
|---|---|---|
| NFR-1.1 (화이트리스트 미일치 발송 차단) | `safety/whitelist.py` + Sender에서 double check | 단위 테스트: 캐시·노션 불일치 케이스 |
| NFR-1.1 (보강) — 오발송 방지 3중 방어 | `output/steps/ensure_friends_tab.py`, `snapshot_hwnds.py`, `verify_chatroom_title.py` (§4.6.3) | Step별 단위 테스트 + 통합 시나리오 (동명 톡방·전환 누락 케이스) |
| NFR-1.2 (검토 대기 자동발송 차단) | Scheduler에서 Status 필터링 | 단위 테스트: Status 별 enqueue 동작 |
| NFR-1.3 (`[AI 자동 팔로우업] ` 접두어) | `safety/prefix.py` + Sender 최종 직전 prepend | 어서션 테스트, 발송 직전 한 번 더 확인 |
| NFR-1.4 (이력 정확 기록) | `send_history` + 노션 발송 이력 양방향 기록 | E2E 테스트 |

---

## 12. 테스트 전략

| 레벨 | 대상 | 도구 |
|---|---|---|
| 단위 테스트 | Extractor 출력 스키마, 화이트리스트 로직, 접두어 강제 | pytest + Anthropic API 모의 응답 |
| 통합 테스트 | Repository ↔ Notion, Scheduler 동작 | 실제 dev용 노션 DB |
| 카톡 발송 PoC | pywinauto 카톡 자동화 | 본인 PC + 테스트 톡방 |
| E2E 시나리오 | PRD §5의 S1~S5 | 수동 (MVP 단계에서는 자동화하지 않음) |

---

## 13. 단계별 구현 우선순위

PRD §12 마일스톤과 매칭.

| 마일스톤 | 우선 구현할 모듈 |
|---|---|
| M0 | (인프라 셋업, 코드 없음) |
| M1 | `storage/notion_repo.py` 의 DB 생성/조회 함수 |
| M2 | `process/anthropic_client.py`, `process/extractor.py` (PoC) |
| M3 | `scripts/investigate.py`로 카톡 HWND 트리 실측 → `output/window_spec.py`, `output/hwnd_utils.py`, `output/steps/*` (3중 방어 포함), `output/detection.py`, `output/kakao_sender.py`, `output/screenshot.py` (PoC) |
| M4 | `ingest/*`, `scheduling/scheduler.py`, `storage/sqlite_repo.py` 결합 |
| M5 | `safety/*`, `output/notifier.py`, 통합 테스트 |
| M6 | 운영 시작 + 메트릭 수집 |

---

## 14. 미해결 / 보류 사항

| ID | 항목 | 결정 미룬 이유 |
|---|---|---|
| O1 | 리마인드 발송 시점 정책(D-1 09:00 / D-day 09:00 외 추가?) | 운영 1주차 사용 후 매니저 선호에 맞춰 조정 |
| O2 | 노션 Inbox 폴링 vs 트레이 앱 메모창 | 노션 Inbox 먼저, 부족하면 트레이 추가 |
| O3 | 카톡 발송 시 동시성 (여러 톡방 동시 발송) | MVP는 직렬 처리. 초당 발송량이 문제될 때 검토 |
| O4 | Anthropic 모델 동적 선택 (Sonnet ↔ Opus) | MVP는 Sonnet 고정. 정확도 부족 시 fallback 로직 추가 |
| O5 | 백업·재해 복구 (SQLite 파일 손상 시) | 사용자 수동 백업으로 충분, 자동 백업은 후순위 |

---

## 15. 옵션으로 보존된 대안 아키텍처

[DECISIONS.md §8.2](DECISIONS.md)에 기록된 **"Claude Code 환경에서 사용자가 직접 운영하는 방식"** 은 다음 차이를 가짐:

- Task Extractor의 LLMClient 인터페이스 구현체가 Anthropic API 대신 Claude Code 세션이 됨
- Watcher/Poller는 의미 없어지고 사용자가 Claude Code에서 자연어로 트리거
- Sender 데몬은 그대로 유지 (시간 기반 자동 발송 부분)

본 MVP에서 LLMClient를 인터페이스로 분리한 이유 중 하나가 이 옵션으로의 전환 가능성을 열어두기 위함. 실제 전환 시 변경되는 코드는 `process/anthropic_client.py` 와 인입(ingest) 부분 정도로 국한됨.
