# 웨이브A 빌더 온라인 배포 가이드

## 다른 사람은 Python 설치 필요?

**아니요.** 배포 후에는 **브라우저만** 있으면 됩니다.  
URL 접속 → 이미지 URL 입력 → 생성 버튼 클릭만 하면 됩니다.  
둘 다 같은 URL로 접속해서 사용하면 됩니다.

---

## Vercel vs 다른 서비스 (중요)

이 프로젝트는 **쇼핑 썸네일 생성** 시:
- rembg(누끼), Gemini API, 이미지 처리 등 **30초~1분 이상** 걸림
- **rembg, onnxruntime** 등 용량이 큰 패키지 사용

**Vercel 한계:**
- 서버리스 함수 **최대 60초** 제한 (Pro 플랜)
- 대용량 Python 패키지 배포가 어려울 수 있음
- 브라우저 자동화(Playwright)는 Vercel에서 지원 안 함

**추천: Railway 또는 Render** (Python 백엔드에 적합)

---

## 방법 1: Railway로 배포 (추천)

1. [railway.app](https://railway.app) 가입
2. **New Project** → **Deploy from GitHub** (또는 GitHub에 코드 먼저 올리기)
3. 프로젝트 루트에 다음 파일 추가:

### `Procfile` (프로젝트 루트에 생성)
```
web: python -m uvicorn backend.main:app --host 0.0.0.0 --port $PORT
```

### `runtime.txt` (Python 버전 지정)
```
python-3.12.0
```

4. **Railway**에서:
   - **Variables**에 환경 변수 추가:
     - `GEMINI_API_KEY` (API 설정에서)
     - `REPLICATE_TOKEN` (선택, rembg 로컬 사용 시 불필요)
   - **Deploy** 실행

5. 배포 URL 확인 → 두 사람 모두 해당 URL로 접속

---

## 방법 2: Render로 배포

1. [render.com](https://render.com) 가입
2. **New** → **Web Service**
3. GitHub 저장소 연결
4. 설정:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn backend.main:app --host 0.0.0.0 --port $PORT`
   - **Runtime:** Python 3

5. **Environment**에 `GEMINI_API_KEY` 등 추가
6. **Deploy** 실행

---

## 방법 3: Vercel로 시도 (제한 있음)

Vercel은 **프론트엔드만** 배포하고, **백엔드는 별도 서비스**로 두는 방식이 좋습니다.

1. **프론트엔드 (index.html)** → Vercel에 정적 배포
2. **백엔드** → Railway/Render로 배포
3. `index.html`에서 API 주소를 `https://your-backend.railway.app` 등으로 변경

---

## 환경 변수 (API 키)

배포 시 다음 환경 변수를 설정해야 합니다:

| 변수명 | 설명 |
|--------|------|
| `GEMINI_API_KEY` | Google AI Studio에서 발급 |
| `REPLICATE_TOKEN` | (선택) rembg 로컬 실패 시 사용 |

---

## 배포 후 사용

1. 배포된 URL 접속 (예: `https://wava-builder.up.railway.app`)
2. **쇼핑** 메뉴 → 이미지 URL 입력 → **생성**
3. 두 사람 모두 같은 URL로 접속하면 됩니다.

---

## 참고: GitHub에 올리기

1. [github.com](https://github.com)에서 새 저장소 생성
2. 프로젝트 폴더에서:
   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/내아이디/저장소이름.git
   git push -u origin main
   ```
3. `.env` 파일은 **절대** 커밋하지 마세요 (API 키 노출). 환경 변수는 Railway/Render에서 설정.
