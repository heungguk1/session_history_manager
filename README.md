# Session History Manager (shm)

Claude Code와 나눈 대화 세션을 색인해서 **나중에 다시 찾아볼 수 있게 해 주는** 로컬 도구입니다.
명령줄(CLI)과 브라우저 대시보드 두 가지로 쓸 수 있습니다.

- 프로젝트별 세션 목록, 키워드 검색, 날짜·프로젝트 필터
- 세션 상세 화면에서 대화를 말풍선 형태로 다시 보기
- 라이트·다크 모드, 모바일 화면 지원
- **표준 라이브러리만 사용** (설치할 패키지 없음)
- **읽기 전용**: Claude Code의 세션 파일(`~/.claude/projects`)은 절대 수정하지 않습니다

> 흐름 다이어그램은 [docs/FLOW_DIAGRAM.md](docs/FLOW_DIAGRAM.md)에 있습니다.

## 요구 사항

| 항목 | 내용 |
|---|---|
| Python | 3.9 이상 |
| 외부 패키지 | 없음 (테스트 실행 시에만 `pytest` 필요) |
| 데이터 | Claude Code를 사용한 적이 있어 `~/.claude/projects/` 아래에 세션 파일(`*.jsonl`)이 있어야 함 |
| OS | macOS, Linux에서 확인. Windows는 경로 규칙상 동작하도록 작성했지만 검증하지 않음 |

## 설치

```bash
git clone https://github.com/heungguk1/session_history_manager.git
cd session_history_manager
```

설치 단계는 없습니다. 저장소 폴더 안에서 `python3 -m shm ...` 형태로 실행합니다.

## 빠른 시작

```bash
python3 -m shm index    # 1. 세션 파일을 읽어 색인 DB를 만든다 (처음 한 번 + 새 대화가 생길 때마다)
python3 -m shm serve    # 2. 대시보드를 띄운다 → 출력된 주소(기본 http://127.0.0.1:8765)를 브라우저로 연다
```

종료는 `Ctrl+C`입니다.

> 대시보드는 색인을 갱신하지 않습니다. 새 대화를 보려면 `python3 -m shm index`를 다시 실행한 뒤 새로고침하세요.

## 명령어

| 명령 | 설명 | 주요 옵션 |
|---|---|---|
| `index` | 세션 파일을 읽어 색인 DB를 만들거나 갱신 | `--full` 전체 재색인 (기본은 바뀐 파일만), `--root` 세션 폴더 지정 |
| `list` | 세션 목록 (최근 활동 순) | `--project`, `--limit` (기본 100), `--json`, `--include-worktrees` |
| `search 검색어...` | 첫 질문과 사용자 메시지에서 키워드 검색 | `list`와 같음 |
| `show 세션ID` | 세션 하나의 정보 (ID 앞부분만 입력해도 됨) | `--json`, `--include-worktrees` |
| `serve` | 웹 대시보드 실행 | `--port` (기본 8765, `0`이면 빈 포트 자동 선택), `--host` (루프백 주소만 허용) |

모든 명령에 `--db 경로`로 색인 DB 위치를 지정할 수 있습니다.

```bash
python3 -m shm list --limit 20
python3 -m shm search 대시보드 디자인
python3 -m shm list --project /Users/me/projects/my-app --json
python3 -m shm show 9f20fbb4
python3 -m shm serve --port 0
```

## 웹 대시보드

| 화면 | 주소 | 내용 |
|---|---|---|
| 세션 목록 | `/` | 프로젝트 사이드바, 기간(오늘/7일/30일/전체 + 시작·종료일), 프로젝트, 검색어 필터 |
| 검색 | `/search?q=...` | 키워드 검색 결과 |
| 세션 상세 | `/session/<ID 또는 앞부분>` | 대화 말풍선, 시작~마지막 시간, 메시지 수, 세션 정보 |
| JSON API | `/api/sessions`, `/api/search`, `/api/session/<ID>` | 화면과 같은 데이터를 JSON으로 제공 |

- 필터 조건은 URL 쿼리(`?project=&from=&to=&q=`)에 남아서 새로고침하거나 링크를 공유해도 유지됩니다.
- 시간은 한국 시간(Asia/Seoul) 기준으로 `오늘 17:32`, `어제 09:05`, `2026-11-25 17:32` 형식으로 표시됩니다.
- 보안: 루프백 주소(`127.0.0.1`, `localhost`, `::1`)에서만 열리고, `GET`·`HEAD` 요청만 받습니다. DB는 읽기 전용(`mode=ro`)으로 엽니다.

## 설정 (환경 변수)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `SHM_CLAUDE_DIR` | `~/.claude/projects` | Claude Code 세션 파일 폴더 |
| `SHM_DB_PATH` | `~/.claude/session-history.sqlite3` | 색인 DB 위치 |

명령 옵션(`--root`, `--db`)이 환경 변수보다 우선합니다.

## 자동으로 숨기는 세션

다음 세션은 색인 DB에는 남지만 목록·검색·대시보드에는 보이지 않습니다. CLI에서는 `--include-worktrees`로 다시 볼 수 있습니다.

- **AI DEV DIRECTOR 작업 폴더**: 경로에 `.director_worktrees`가 들어간 세션
- **임시 폴더**: `/tmp`, `/private/tmp`, `/var/folders`, `/private/var/folders` 아래, 또는 경로에 `director-builder-`가 들어간 세션
- **AI DEV DIRECTOR가 실행한 AI 역할 세션**: 첫 메시지가 HEAD·빌더·리뷰어 역할 프롬프트(`You are HEAD...`, `You are the implementation Builder...` 등)인 세션

## 데이터와 개인정보

- 색인 DB에는 세션의 첫 질문과 **사용자 메시지 원문**이 저장됩니다. DB 파일을 다른 곳에 공유하지 마세요.
- 대시보드의 대화 화면은 DB가 아니라 원본 세션 파일을 그때그때 읽어서 보여 줍니다. 원본 파일이 지워졌으면 "세션 파일을 읽을 수 없습니다"라고 표시됩니다.
- 네트워크로 데이터를 보내지 않습니다. 대시보드도 외부 CSS나 스크립트를 불러오지 않습니다.

## 프로젝트 구조

```
shm/
├── __main__.py        # python -m shm 진입점
├── cli.py             # 명령어(index, list, search, show, serve)
├── parser.py          # 세션 JSONL 파일 읽기 (메타데이터, 대화 내용)
├── indexer.py         # SQLite 색인, 검색, 숨김 규칙
├── web.py             # 웹 대시보드 (http.server)
└── static/style.css   # 대시보드 스타일 (각 페이지에 인라인으로 삽입)
tests/                 # pytest 테스트
docs/FLOW_DIAGRAM.md   # 흐름 다이어그램
```

## 테스트

```bash
python3 -m pip install pytest
python3 -m pytest tests
```

## 알려진 제한

- 읽기 전용 도구라 태그, 메모, 즐겨찾기, 세션 삭제 같은 관리 기능은 아직 없습니다.
- 표시 시간대는 Asia/Seoul로 고정돼 있습니다. 시간대 데이터(`zoneinfo`)가 없는 환경에서는 UTC+9로 계산합니다.
- 검색은 SQLite `LIKE` 기반의 단순 키워드 검색이며, Claude의 답변 내용은 검색 대상이 아닙니다.
