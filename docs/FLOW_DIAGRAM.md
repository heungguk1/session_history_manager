# 흐름 다이어그램

Session History Manager의 데이터 흐름과 요청 처리 순서입니다. 사용법은 [README](../README.md)를 보세요.

## 1. 전체 구조

```mermaid
flowchart LR
    subgraph src["Claude Code (읽기만 함)"]
        J["~/.claude/projects/<br/>프로젝트폴더/세션ID.jsonl"]
    end

    subgraph shm["shm 패키지"]
        P["parser.py<br/>JSONL 파싱"]
        I["indexer.py<br/>SQLite 색인·검색·숨김 규칙"]
        C["cli.py<br/>index · list · search · show · serve"]
        W["web.py<br/>대시보드 (http.server)"]
        S["static/style.css"]
    end

    DB[("~/.claude/<br/>session-history.sqlite3")]
    U(["사용자"])
    B(["브라우저<br/>127.0.0.1:8765"])

    U -- "python3 -m shm ..." --> C
    C -- "index" --> I
    I -- "세션 파일 목록·파싱" --> P
    P -- "읽기 전용" --> J
    I -- "저장 / 조회" --> DB
    C -- "list · search · show" --> I
    C -- "serve" --> W
    W -- "읽기 전용(mode=ro) 조회" --> I
    W -- "상세 화면: 대화 원문 읽기" --> P
    S -. "시작 시 한 번 읽어 페이지에 삽입" .-> W
    B <-- "GET / HEAD" --> W
```

## 2. 색인 (`python3 -m shm index`)

```mermaid
flowchart TD
    A["shm index"] --> B["세션 파일 찾기<br/>~/.claude/projects/*/*.jsonl"]
    B --> C{"--full 옵션?"}
    C -- "아니오 (기본)" --> D{"DB 기록과 수정시각·크기가 같은가?"}
    D -- "같음" --> E["건너뜀 (skipped)"]
    D -- "다름 / 새 파일" --> F
    C -- "예" --> F["parse_session<br/>한 줄씩 JSON 파싱"]
    F --> G["메타데이터 추출<br/>• 첫 질문 → 제목<br/>• 시작·마지막 시간<br/>• 메시지 수<br/>• 프로젝트 경로(cwd)<br/>• 사용자 메시지 원문"]
    G --> H[("sessions 테이블에 저장<br/>(file_path 기준 덮어쓰기)")]
    B --> I["DB에는 있는데 사라진 파일"]
    I --> J["DB 행 삭제 (removed)"]
    H --> K["indexed / skipped / removed 개수 출력"]
    E --> K
    J --> K
```

## 3. 대시보드 요청 처리 (`python3 -m shm serve`)

```mermaid
sequenceDiagram
    autonumber
    participant B as 브라우저
    participant W as web.py
    participant I as indexer.py
    participant DB as SQLite (읽기 전용)
    participant F as 세션 원본 파일

    B->>W: GET /?project=&from=&to=&q=
    W->>W: 경로 확인 (모르는 경로는 DB를 열기 전에 404)
    W->>I: DB 열기 (mode=ro)
    alt DB 파일 없음
        W-->>B: 503 "먼저 python3 -m shm index 를 실행하세요"
    else 정상
        W->>W: 필터 해석 (잘못된 날짜는 무시)<br/>한국 날짜 → UTC 범위로 변환
        I->>DB: 조회 + 숨김 규칙 (LIMIT 전에 적용)
        DB-->>I: 세션 행
        I-->>W: 세션 목록, 프로젝트별 개수
        W-->>B: HTML (사이드바 · 필터 · 세션 카드)
    end

    B->>W: GET /session/9f20fbb4
    W->>I: ID 앞부분으로 찾기
    alt 0개
        W-->>B: 404
    else 2개 이상
        W-->>B: 409 후보 목록
    else 1개
        W->>F: 대화 읽기 (읽기 전용, 최대 400개 메시지)
        F-->>W: 사용자·Claude 메시지 (도구 호출 기록 제외)
        W-->>B: HTML (말풍선 대화 + 세션 정보)
    end
```

## 4. 숨김 규칙

목록·검색·상세·API·프로젝트 사이드바에 모두 같은 SQL 조건이 적용됩니다 (`indexer.py`의 `_WORKTREE_EXCLUDE_SQL`).

```mermaid
flowchart TD
    R["세션 행"] --> A{"경로에 .director_worktrees ?"}
    A -- "예" --> X["숨김"]
    A -- "아니오" --> B{"임시 폴더?<br/>/tmp · /private/tmp ·<br/>/var/folders · director-builder-"}
    B -- "예" --> X
    B -- "아니오" --> C{"첫 메시지가<br/>AI DEV DIRECTOR 역할 프롬프트?<br/>You are HEAD... 등"}
    C -- "예" --> X
    C -- "아니오" --> V["표시"]
    X -. "CLI --include-worktrees" .-> V
```

## 5. 안전 장치 요약

| 위치 | 장치 |
|---|---|
| 세션 원본 파일 | 읽기만 함. 색인과 대시보드 모두 쓰기 없음 |
| 색인 DB | 대시보드는 `mode=ro`로 열고, 색인 갱신은 `shm index`에서만 |
| 서버 주소 | 루프백(`127.0.0.1`, `localhost`, `::1`)만 허용 |
| HTTP 메서드 | `GET`, `HEAD`만 허용 |
| 출력 | 모든 값 HTML 이스케이프, 외부 리소스 참조 없음 |
