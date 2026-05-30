# lovable-followup-agent

Lovable 팀의 매니저가 여러 채널로 흩어진 업무 정보를 한곳에 모으고, 마감이 다가오면 담당자에게 카카오톡으로 자동 리마인드를 보낼 수 있게 해주는 **개인용 매니징 보조 AI 에이전트**.

카카오톡 대화를 `.txt`로 받아 AI가 "할 일 / 맥락 / 마감일 / 담당자" 4요소를 뽑아 노션에 정리하고, 매니저가 노션에서 확정한 업무만 자동으로 카톡 리마인드 메시지를 보낸다. 자동 메시지는 항상 `[AI 자동 팔로우업] ` 접두어를 달고, 사전에 허용한(=화이트리스트에 등록한) 톡방에만 발송한다.

> **상태**: Phase 0 (설계 완료). 구현은 Phase 1부터.

---

## 이 저장소가 존재하는 이유

매니저는 매일 다음과 같은 부담을 진다.

- **채널 파편화** — 카톡(메인), 이메일, 회의, 노션, Google Drive로 정보가 흩어져 매일 일일이 대조해야 한다
- **카톡 노이즈** — 업무·개인 메시지가 섞여 있고, 며칠 전 지시사항을 찾는 데 큰 리소스가 든다
- **일정과 실행 관리의 괴리** — 캘린더에 기한은 있지만 "구체적으로 무엇을 / 누가 / 언제까지" 챙기는 일이 빠진다

이 시스템은 위 부담을 자동화하여 **업무 누락 위험을 낮추고 매니징 리소스를 절감**하는 데 목적이 있다.

---

## 설계 결정 요약

| 영역 | 선택 | 근거 |
|---|---|---|
| MVP 범위 | 카카오톡 단일 채널 | 가장 노이즈가 크고 누락 위험이 집중된 곳부터 검증. Gmail/Calendar/Alt는 Phase 2 |
| 카톡 수신 | `.txt` 익스포트를 사용자가 직접 업로드 | PC 카톡은 메시지를 암호화 저장 → 자동 수신 불가, 수동 익스포트가 가장 안정적 |
| 카톡 송신 | **win32 HWND (`EnumChildWindows`) 주력 + UIA(`uiautomation`) 보조** + `WindowSpec` + Step 추상화 + **오발송 방지 3중 방어** | 일반 톡방에는 공식 API 없음. 카톡 메인창은 UIA 트리가 부실해 친구 탭/검색을 잡기 어려움 → win32 HWND가 더 안정. 채팅창은 별도 top-level HWND로 표준 `RICHEDIT50W`를 노출하므로 `SendMessage(WM_SETTEXT)`로 직접 입력 가능. [kakao-sender (v2)](https://github.com/TurnaboutHero/kakao-sender-v2) 실측(Phase 3) 결과를 그대로 차용 |
| 발송 대상 통제 | Notion 기반 화이트리스트 (명시적 허용만 발송) | AI 자동 메시지를 받기에 부적절한 인사 관계 존재 — 기본 차단 + 명시적 허용 정책 |
| 메시지 접두어 | `[AI 자동 팔로우업] ` 강제 prepend | 수신자가 시스템 알림임을 명확히 인지 → 매니저-담당자 감정 마찰 완화 |
| 출력 플랫폼 | Notion DB (Tasks / Whitelist / Inbox) | 팀이 이미 Notion 사용 중 — 학습 비용 0 |
| AI 엔진 | Anthropic Claude API + 월 30,000원 상한 | API가 자동화에 본래 적합. 월 상한으로 폭주 사고만 차단 |
| 중복 업무 판별 | Claude 프롬프트에 기존 진행중 업무 포함 (임베딩 X) | MVP 규모(활성 수십 개)에 충분. 임베딩 인프라 도입 부담 회피 |
| 메모 입력 | Notion Inbox 페이지 (트레이 앱 X) | 이미 노션 쓰고 있어 새로 만들 게 적음 |
| 발송 검증 | 스크린샷 캡처 후 Notion 발송 이력에 첨부 | OCR 검증은 사고 발생 시 강화 — MVP는 단순함 우선 |
| 지연 알림 정책 | 6시간 룰 (초과 시 자동 발송 스킵, 사용자에게만 표시) | 너무 늦은 자동 알림은 받는 사람을 당황시켜 역효과 |
| 런타임 환경 | 매니저의 Windows PC 상주 | 카톡 PC 자동화가 필요해 클라우드 단독 운영 불가 |

상세 근거와 대안은 [`DECISIONS.md`](DECISIONS.md) 참조.

---

## 빠른 시작 (사용자 가이드)

> **현재 Phase 5 (운영 진입)까지 구현이 모두 완료되어 즉시 사용 가능합니다.**

### 1. 사전 준비

1. **로컬 AI 모델 설치 (Ollama)**
   - PC에 [Ollama](https://ollama.com/)를 설치합니다.
   - 터미널(또는 명령 프롬프트)을 열고 한국어 특화 모델을 다운로드합니다:
     ```powershell
     ollama pull exaone3.5:7.8b
     ```
2. **Notion 통합(Integration) 생성 + 토큰 발급**
   - [Notion Integrations](https://www.notion.so/profile/integrations) → 새 Integration → Internal 권한
   - 발급된 토큰(`secret_...`)을 복사해 둡니다.

### 2. 설치 및 환경 설정

```powershell
# 저장소 클론 및 폴더 이동
git clone https://github.com/lsjman13-create/lovable-followup-agent.git
cd lovable-followup-agent

# 가상환경 + 의존성 설치 (uv 필요)
uv venv
uv pip install -e .
```

환경변수로 노션 API 토큰을 설정합니다. (Windows 시스템 환경변수에 등록 권장)
```powershell
$env:NOTION_API_TOKEN = "secret_..."
```

### 3. 노션 DB 자동 생성

에이전트가 사용할 3개의 DB(Tasks, Whitelist, Inbox)를 노션에 자동으로 만들어줍니다.
```powershell
uv run scripts/setup_notion.py --token %NOTION_API_TOKEN% --page-id <비어있는_부모페이지_ID>
```
실행이 완료되면 루트 폴더에 `config.toml` 파일이 생성되며 노션 ID들이 자동 등록됩니다.

### 4. 화이트리스트 등록 및 부팅 시 자동 실행

카톡 오발송을 막기 위해 **미리 허용된(Whitelist) 톡방에만 발송**합니다.
1. 노션에 생성된 `Whitelist` DB를 열고, 자동 알림을 보낼 대상 톡방 이름(예: `이승준`, `MOP 운영방`)을 정확히 입력합니다.

데몬이 컴퓨터 부팅 시 자동으로 백그라운드에서 실행되도록 등록합니다.
```powershell
uv run scripts/install_startup.py
```
이후 컴퓨터를 재부팅하거나, 바탕화면 우측 하단 트레이 근처에서 백그라운드로 실행됩니다.

### 5. 매일 사용하는 방법

1. **카톡 내용 추출**: 업무 지시나 중요한 논의가 있던 카톡 방에서 `대화 내보내기 → 텍스트만 보내기`를 합니다.
2. **노션 Inbox에 텍스트 붙여넣기**: 노션에 생성된 `Inbox` 페이지에 해당 텍스트를 그대로 붙여넣습니다 (텍스트가 아무리 길어도 에이전트가 3,000자씩 분할하여 완벽하게 처리합니다).
3. **업무 스크랩 (자동)**: 1~5분 내에 백그라운드 에이전트가 텍스트를 읽고 `Tasks` DB에 할 일, 담당자, 마감일을 정리해 줍니다.
4. **매니저 검토 및 확정**: `Tasks` DB에 `검토 대기` 상태로 들어온 업무를 확인하고, 이상이 없다면 상태를 `확정`으로 변경합니다.
5. **카톡 자동 리마인드 (자동)**: 마감 시점이 다가오면 (D-1, D-day 등) 담당자 카톡방으로 에이전트가 자동으로 알림 메시지를 발송합니다!

---

## 디렉터리 구조

```
lovable-followup-agent/
├── README.md                        # 본 문서
├── PRD.md                           # 제품 요구사항
├── ARCHITECTURE.md                  # 기술 아키텍처
├── DECISIONS.md                     # 의사결정 기록
├── pyproject.toml                   # uv/Python 프로젝트 메타
├── config.example.toml              # 설정 템플릿
├── lovable_agent/                   # 메인 패키지
│   ├── __init__.py
│   ├── main.py                      # 엔트리포인트
│   ├── config.py                    # 설정 로딩
│   ├── ingest/                      # 데이터 인입
│   │   ├── txt_watcher.py           # inbox 폴더 감시
│   │   ├── notion_poller.py         # Notion Inbox 폴링
│   │   └── kakao_parser.py          # 카톡 .txt 포맷 파싱
│   ├── process/                     # AI 분석
│   │   ├── extractor.py             # 4요소 추출 오케스트레이션
│   │   ├── llm_client.py            # LLMClient Protocol (격리 인터페이스)
│   │   └── anthropic_client.py      # Anthropic API 구현체
│   ├── storage/                     # 데이터 저장
│   │   ├── repository.py            # Notion ↔ SQLite 단일 진입점
│   │   ├── notion_repo.py
│   │   ├── sqlite_repo.py
│   │   └── migrations/
│   ├── scheduling/                  # 시간 기반 트리거
│   │   └── scheduler.py             # APScheduler 기반 due 체크
│   ├── output/                      # 발송·알림
│   │   ├── kakao_sender.py          # 발송 오케스트레이션
│   │   ├── window_spec.py           # WindowSpec 데이터클래스
│   │   ├── steps/                   # Step 단위 자동화 (UIA)
│   │   │   ├── base.py              # Step Protocol
│   │   │   ├── activate_kakao.py
│   │   │   ├── open_chatroom.py
│   │   │   ├── focus_input.py
│   │   │   ├── type_message.py
│   │   │   └── press_enter.py
│   │   ├── screenshot.py            # 발송 직후 스크린샷 캡처
│   │   └── notifier.py              # 데스크톱 알림
│   └── safety/                      # 안전장치
│       ├── whitelist.py             # 화이트리스트 더블체크
│       └── prefix.py                # [AI 자동 팔로우업] 접두어 강제
└── tests/
    ├── test_extractor.py
    ├── test_whitelist.py
    └── ...
```

---

## 주요 문서

| 문서 | 역할 |
|---|---|
| [README.md](README.md) | 본 문서 — 프로젝트 정문, 빠른 진입 |
| [PRD.md](PRD.md) | **무엇을 / 왜 만드는가** — 사용자·시나리오·기능·NFR·성공지표·위험 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | **어떻게 만드는가** — 컴포넌트·데이터 모델·시퀀스·스택·운영 |
| [DECISIONS.md](DECISIONS.md) | **무엇을·왜 그렇게 결정했는가** — 입문자용 정리 + 결정 기록 |

---

## 단계별 진행 (Phase Log)

| Phase | 시점 | 내용 |
|---|---|---|
| **Phase 0** | 2026-05-22 ~ 2026-05-23 | 요구사항·설계 확정. PRD/ARCHITECTURE/DECISIONS/README 작성. 핵심 의사결정 12건 정리. [kakao-sender (v2)](https://github.com/TurnaboutHero/kakao-sender-v2) 의 win32 HWND 주력 + UIA 보조 + 3중 방어 설계 차용. |
| **Phase 1** | 2026-05-23 ✅ | 코드 골조 완성: `pyproject.toml`(uv 기반) + `lovable_agent/` 패키지 트리 + `LLMClient` Protocol + `MockLLMClient` + `MockNotionRepository` + `main.py --dry-run`. 검증 4종 통과 — `uv sync` ✓ / `pytest 8 passed` ✓ / `ruff check` ✓ / `--dry-run` 한 사이클 정상 종료 ✓. 외부 호출 0건. |
| **Phase 2** | 2026-05-23 ✅ **완료** | `scripts/investigate.py` + `--auto-open-self-chat` + 본인 PC 실측 (RICHEDIT50W·EVA_Window_Dblclk·EVA_VH_ListControl_Dblclk 확인) → `hwnd_utils.py` + `window_spec.py` + Step 6개 (3중 방어 포함) + `kakao_sender.py` 오케스트레이션. pytest **124 passed**. `scripts/send_test.py` 로 "나와의 채팅" **5/5 발송 성공**. 본인 환경 실측: 친구 탭이 아니라 **채팅 탭에서 검색** + Alt+Enter 가 아니라 **더블클릭(WM_LBUTTONDBLCLK)** 으로 별도 창 오픈 — 둘 다 옵션화. |
| **Phase 3** | 2026-05-23 ✅ **완료** | 분석·저장 로직 + Phase 2 통합 — 카톡 .txt 파서, Extractor, SQLite 저장소(+migrations), 6시간 룰 스케줄러, 화이트리스트 더블체크, 폴더 watcher, Notifier, **SendDispatcher**(발송 큐 ↔ KakaoSender 연결). pytest **133 passed**, `--dry-run` 7단계 통합 흐름 검증. |
| **Phase 4** | 2026-05-24 ✅ | 노션 부분: setup_notion.py 로 DB 3개 자동 생성·schema 적용 + 실 노션 통합 검증. Anthropic API 키 대신 **ClaudeCLIClient (`claude -p`)** 로 우회 (API 키 없이 실 AI 분석). |
| **Phase 5** | 2026-05-24 ✅ (코드), 사용자 1회 실행 대기 | 운영 데몬 (`main.py` 매 분 polling + 발송 루프, Ctrl+C 우아한 종료, 파일 로그 회전). `install_startup.py` 로 시작 프로그램 폴더에 .bat 자동 등록 (Task Scheduler 대신 단순화). 본인이 `install_startup.py` 1회 실행 + PC 재부팅하면 운영 진입. |

각 Phase가 종료되는 시점에 본 표에 **완료 날짜**와 **실측 결과**를 채워 누적 기록한다.

---

## 참고

- 오픈소스 [kakao-sender (v2)](https://github.com/TurnaboutHero/kakao-sender-v2) 프로젝트의 설계 패턴을 차용:
  - `WindowSpec` 데이터클래스 — 윈도우 식별 캡슐화
  - Step 단위 자동화 추상화 — 발송 단계 분리
  - **win32 HWND 주력 + UIA 보조** 접근 (kakao-sender Phase 3 실측 결과 반영)
  - **오발송 방지 3중 방어** — 친구 탭 강제 + HWND 스냅샷 diff + 채팅창 제목 완전일치
  - 텍스트 입력에 `SendMessage(WM_SETTEXT)` 사용 (클립보드·포커스 독립)

  본 저장소는 그 사본이 아니며, 의존성으로 포함하지도 않는다. 설계 영감만 받음.
- "Claude Code 환경에서 사용자가 직접 운영하는" 대안 아키텍처를 [`DECISIONS.md` §8.2](DECISIONS.md) 와 [`ARCHITECTURE.md` §15](ARCHITECTURE.md) 에 옵션으로 보존. API 비용·약관·자동화 균형이 바뀔 경우 재검토.

---

## 라이선스

내부용. 외부 배포 전 별도 검토 필요.
