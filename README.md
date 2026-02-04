## 실행 방법 (로컬)

### 1) 백엔드(영상 Queue Mock)

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn backend.main:app --reload --port 8000
```

### 2) 프론트

`index.html`을 라이브서버(또는 간단한 정적서버)로 열어주세요.

예: VSCode Live Server, 또는

```bash
python -m http.server 5173
```

그 후 브라우저에서 `http://localhost:5173` 접속.

