# 웨이브A AI 솔루션 (WAVA Builder)

AI 기반 웹 디자이너 인터페이스. 이미지·카드뉴스·영상 제작, AI 채팅, 쇼핑 썸네일, SNS 플로우 등 다양한 제작 기능을 제공합니다.

## 주요 기능

- **홈** – 대화형 시작 화면
- **채팅** – AI 채팅, 폴더별 작업 목록
- **이미지** – 광고 소재 생성·편집, 디자인 규칙 적용
- **카드뉴스** – 카드뉴스 슬라이드 제작, 4:5 비율
- **영상** – 영상 생성 요청 (백엔드 Queue Mock 연동)
- **쇼핑** – 썸네일 생성 (이미지 URL 입력)
- **SNS 플로우** – 아이디어·캡션·해시태그·유튜브 URL 연동
- **설정** – Gemini API Key, Replicate 토큰 등

## UI 동작

- **이미지·카드뉴스** 섹션에서는 입력창 **위쪽의 다른 색(회색) 영역**—이미지 붙여넣기/첨부 미리보기만 **숨겨지고**, **텍스트 입력 영역(디자인 요청 입력창)은 그대로 보입니다.**
- **영상** 섹션에서는 이미지 첨부가 비활성화됩니다.

## 실행 방법 (로컬)

### 방법 1: 백엔드 한 번에 실행 (권장)

백엔드 서버가 **index.html도 함께** 제공하므로, 아래만 실행한 뒤 브라우저에서 접속하면 됩니다.

**PowerShell 또는 CMD**에서 프로젝트 폴더로 이동한 뒤:

```bash
cd c:\Users\admin\Desktop\wava-builder
```

**가상환경 만들기 (최초 1회)**

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

**서버 실행**

```bash
.venv\Scripts\activate
uvicorn backend.main:app --reload --port 8000
```

브라우저에서 **http://localhost:8000** 접속.

- 종료: 터미널에서 `Ctrl+C`

---

### 방법 2: 배치 파일로 실행 (Windows)

| 파일 | 동작 |
|------|------|
| **start.bat** | 백엔드만 실행 → 약 3초 후 브라우저에서 http://localhost:8000 자동 열림 (venv 없으면 시스템 Python 사용) |
| **start-프론트만.bat** | 프론트만 실행 (정적 서버 5500) → http://localhost:5500/index.html (쇼핑/영상 API 미동작) |

`start.bat`을 더블클릭하거나, 터미널에서:

```bash
cd c:\Users\admin\Desktop\wava-builder
start.bat
```

---

### 방법 3: 백엔드와 프론트를 터미널 두 개로 분리

**터미널 1 – 백엔드**

```bash
cd c:\Users\admin\Desktop\wava-builder
.venv\Scripts\activate
uvicorn backend.main:app --reload --port 8000
```

**터미널 2 – 프론트 (정적 서버)**

```bash
cd c:\Users\admin\Desktop\wava-builder
python -m http.server 5173
```

- 백엔드(API): http://localhost:8000  
- 프론트만: http://localhost:5173 (index.html 열어서 사용, API는 8000으로 요청)

---

### 테스트 체크리스트

1. **http://localhost:8000** 접속 → 홈/채팅/이미지/카드뉴스 메뉴가 보이는지 확인
2. **이미지** 또는 **카드뉴스** 선택 → 입력창 위 회색(첨부 미리보기) 영역만 **숨겨지고**, 텍스트 입력창은 **보이는지** 확인
3. **설정**에서 Gemini API Key 저장 후, 채팅 또는 이미지 생성 요청 동작 확인

## 프로젝트 구조

| 경로 | 설명 |
|------|------|
| `index.html` | 단일 페이지 앱 (Tailwind, html2canvas, jsPDF, Pretendard) |
| `backend/` | FastAPI 백엔드 (영상 API, 쇼핑 파이프라인, SNS 등) |
| `requirements.txt` | Python 의존성 |
| `Procfile`, `runtime.txt` | Railway 등 배포용 |
| `DEPLOY.md` | 온라인 배포 가이드 (Railway, Render 등) |

## 배포

온라인 배포 방법은 **DEPLOY.md**를 참고하세요. (Railway / Render 권장)
