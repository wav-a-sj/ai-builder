from __future__ import annotations

import asyncio
import base64
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

# SNS 연동
try:
    from backend import sns_auth
    from backend import sns_schedule
    from backend import sns_threads_youtube
except ImportError:
    import sns_auth  # noqa: F401
    import sns_schedule  # noqa: F401
    import sns_threads_youtube  # noqa: F401

# 프로젝트 루트 (backend 폴더의 상위)
ROOT = Path(__file__).resolve().parent.parent


class CreateVideoJobRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=5000)
    model_version: str = "veo-3.1"
    resolution: str = "1080p"
    has_model: bool = True
    model_gender: Optional[str] = None  # "female" | "male"
    model_age: Optional[str] = None  # "20s" | "30s" | "40s" | "50s"
    rules: list[str] = []
    # Reference images (Ingredients to Video)
    product_ref_base64: Optional[str] = None
    product_ref_mime: Optional[str] = None
    background_ref_base64: Optional[str] = None
    background_ref_mime: Optional[str] = None


@dataclass
class VideoJob:
    id: str
    status: str  # pending | processing | completed | failed
    prompt: str
    created_at: float
    updated_at: float
    progress: int = 0
    result_url: Optional[str] = None
    result_text: Optional[str] = None
    error: Optional[str] = None
    meta: Dict[str, Any] = None


JOBS: Dict[str, VideoJob] = {}

_schedule_task: Optional[asyncio.Task] = None


async def _run_scheduled_posts() -> None:
    """1분마다 예약 시간 된 항목 발행."""
    while True:
        try:
            due = getattr(sns_schedule, "get_due_items", lambda: [])()
            for item in due:
                cid = item.get("connection_id")
                caption = item.get("caption", "")
                image_url = item.get("image_url")
                video_url = item.get("video_url")
                item_id = item.get("id")
                if not cid or not caption:
                    sns_schedule.mark_failed(item_id or "", "connection_id or caption missing")
                    continue
                result = await sns_auth.post_to_connection(cid, caption, image_url, video_url)
                if result.get("ok"):
                    sns_schedule.mark_posted(item_id or "", result.get("post_id"))
                else:
                    sns_schedule.mark_failed(item_id or "", result.get("error", "unknown"))
        except Exception as e:
            pass  # 로그만 하고 다음 루프
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _schedule_task
    _schedule_task = asyncio.create_task(_run_scheduled_posts())
    yield
    if _schedule_task:
        _schedule_task.cancel()
        try:
            await _schedule_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Wava Video Queue (Mock)", lifespan=lifespan)

# file:// 로 열었을 때 Origin이 "null"로 오므로, null도 허용
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*", "null"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
@app.get("/index.html", response_class=HTMLResponse)
@app.get("/shopping", response_class=HTMLResponse)
async def serve_frontend() -> HTMLResponse:
    """프론트엔드 제공. file:// 대신 http://localhost:8000 으로 열면 CORS 문제 없음."""
    index_path = ROOT / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="index.html not found")
    return FileResponse(index_path, media_type="text/html")


async def _run_mock_job(job_id: str) -> None:
    job = JOBS.get(job_id)
    if not job:
        return
    try:
        # pending → processing
        await asyncio.sleep(2)
        job.status = "processing"
        job.updated_at = time.time()

        # Simulate 30~60s generation time with progress.
        for p in [10, 20, 35, 50, 65, 80, 92]:
            await asyncio.sleep(5)
            job.progress = p
            job.updated_at = time.time()

        await asyncio.sleep(3)
        job.progress = 100
        job.status = "completed"
        job.updated_at = time.time()
        job.result_text = (
            "Mock 완료: 실제 Veo 연동 시 이 자리에 결과 URL/파일이 들어갑니다."
        )
        # Placeholder URL (none). Keep as None until real integration.
        job.result_url = None
    except Exception as e:  # pragma: no cover
        job.status = "failed"
        job.error = str(e)
        job.updated_at = time.time()


@app.post("/api/video/jobs")
async def create_video_job(req: CreateVideoJobRequest) -> Dict[str, Any]:
    job_id = str(uuid4())
    now = time.time()
    job = VideoJob(
        id=job_id,
        status="pending",
        prompt=req.prompt,
        created_at=now,
        updated_at=now,
        progress=0,
        meta={
            "model_version": req.model_version,
            "resolution": req.resolution,
            "has_model": req.has_model,
            "model_gender": req.model_gender,
            "model_age": req.model_age,
            "rules": req.rules,
            "has_product_ref": bool(req.product_ref_base64),
            "has_background_ref": bool(req.background_ref_base64),
        },
    )
    JOBS[job_id] = job
    asyncio.create_task(_run_mock_job(job_id))
    return {"id": job_id, "status": job.status}


@app.get("/api/video/jobs/{job_id}")
async def get_video_job(job_id: str) -> Dict[str, Any]:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job_not_found")
    return asdict(job)


# -----------------------------
# Shopping thumbnail queue
# -----------------------------


class CreateShoppingThumbnailJobRequest(BaseModel):
    url: Optional[str] = Field(None, min_length=1, max_length=3000)  # 상품 페이지 (선택)
    image_url: Optional[str] = Field(None, min_length=5, max_length=3000)  # 이미지 URL 직접 입력
    gemini_api_key: Optional[str] = None
    replicate_token: Optional[str] = None
    naver_client_id: Optional[str] = None
    naver_client_secret: Optional[str] = None


@dataclass
class ShoppingThumbnailJob:
    id: str
    status: str  # pending | processing | completed | failed
    url: str
    created_at: float
    updated_at: float
    progress: int = 0
    result_data_url: Optional[str] = None
    error: Optional[str] = None
    meta: Dict[str, Any] = None
    image_url: Optional[str] = None  # 스크래핑 건너뛸 때 직접 입력


SHOPPING_JOBS: Dict[str, ShoppingThumbnailJob] = {}


def _build_mock_thumbnail_svg(product_url: str) -> str:
    # Keep output small and deterministic. Frontend will display as <img src="data:...">.
    safe = (product_url or "").strip().replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if len(safe) > 64:
        safe = safe[:61] + "..."
    return (
        "<svg xmlns='http://www.w3.org/2000/svg' width='1024' height='1024'>"
        "<defs>"
        "<linearGradient id='g' x1='0' y1='0' x2='1' y2='1'>"
        "<stop offset='0' stop-color='#0ea5e9'/>"
        "<stop offset='1' stop-color='#a855f7'/>"
        "</linearGradient>"
        "</defs>"
        "<rect width='1024' height='1024' fill='url(#g)'/>"
        "<rect x='72' y='72' width='880' height='880' rx='48' fill='rgba(255,255,255,0.92)'/>"
        "<text x='120' y='190' font-family='Pretendard, sans-serif' font-size='44' font-weight='700' fill='#111827'>"
        "WavaA Shopping Thumbnail (Mock)"
        "</text>"
        "<text x='120' y='260' font-family='Pretendard, sans-serif' font-size='28' fill='#374151'>"
        "Backend pipeline will be connected next."
        "</text>"
        "<rect x='120' y='330' width='784' height='452' rx='32' fill='#f3f4f6'/>"
        "<text x='512' y='560' text-anchor='middle' font-family='Pretendard, sans-serif' font-size='34' fill='#6b7280'>"
        "상품 이미지 영역"
        "</text>"
        "<rect x='120' y='822' width='784' height='86' rx='24' fill='#111827'/>"
        "<text x='160' y='877' font-family='Pretendard, sans-serif' font-size='26' fill='white'>"
        "입력 링크:"
        "</text>"
        "<text x='260' y='877' font-family='Pretendard, sans-serif' font-size='26' fill='white'>"
        + safe +
        "</text>"
        "</svg>"
    )


async def _run_shopping_job(
    job_id: str,
    gemini_api_key: Optional[str] = None,
    replicate_token: Optional[str] = None,
    naver_client_id: Optional[str] = None,
    naver_client_secret: Optional[str] = None,
) -> None:
    job = SHOPPING_JOBS.get(job_id)
    if not job:
        return
    use_real_pipeline = (
        gemini_api_key and len(gemini_api_key) > 10
        and replicate_token and len(replicate_token) > 10
    )

    def on_progress(step: str, p: int) -> None:
        job.progress = p
        job.updated_at = time.time()

    try:
        job.status = "processing"
        job.updated_at = time.time()

        if use_real_pipeline:
            try:
                try:
                    from .shopping_pipeline import run_pipeline
                except ImportError:
                    from shopping_pipeline import run_pipeline
                data_url, err = await run_pipeline(
                    job.url,
                    gemini_api_key,
                    replicate_token,
                    on_progress=on_progress,
                    naver_client_id=naver_client_id,
                    naver_client_secret=naver_client_secret,
                    image_url=getattr(job, "image_url", None),
                )
                if err:
                    job.status = "failed"
                    job.error = err
                else:
                    job.result_data_url = data_url
                    job.progress = 100
                    job.status = "completed"
                    job.meta = {"pipeline": "playwright_replicate_gemini_composite"}
            except Exception as e:
                job.status = "failed"
                job.error = str(e)
        else:
            # Mock: 짧은 시뮬레이션 후 SVG
            for p in [15, 35, 55, 75, 92]:
                await asyncio.sleep(0.5)
                on_progress("mock", p)
            svg = _build_mock_thumbnail_svg(job.url)
            job.result_data_url = "data:image/svg+xml;charset=utf-8," + svg
            job.progress = 100
            job.status = "completed"
            job.meta = {"pipeline": "mock", "note": "Gemini/Replicate 키를 설정하면 실제 파이프라인이 실행됩니다."}

        job.updated_at = time.time()
    except Exception as e:  # pragma: no cover
        job.status = "failed"
        job.error = str(e)
        job.updated_at = time.time()


@app.post("/api/shopping/thumbnail/jobs")
async def create_shopping_thumbnail_job(req: CreateShoppingThumbnailJobRequest) -> Dict[str, Any]:
    image_url = (req.image_url or "").strip() or None
    url = (req.url or "").strip() or None
    if not image_url and not url:
        raise HTTPException(status_code=400, detail="image_url 또는 url 중 하나는 필수입니다.")
    # 이미지 URL만 있으면 url로도 사용 (파이프라인에서 image_url 우선 사용)
    effective_url = url or image_url or ""
    job_id = str(uuid4())
    now = time.time()
    job = ShoppingThumbnailJob(
        id=job_id,
        status="pending",
        url=effective_url,
        created_at=now,
        updated_at=now,
        progress=0,
        result_data_url=None,
        error=None,
        meta={"source": "image_url" if image_url else "naver_shopping_link"},
        image_url=image_url,
    )
    SHOPPING_JOBS[job_id] = job
    asyncio.create_task(_run_shopping_job(
        job_id,
        gemini_api_key=req.gemini_api_key,
        replicate_token=req.replicate_token,
        naver_client_id=req.naver_client_id,
        naver_client_secret=req.naver_client_secret,
    ))
    return {"id": job_id, "status": job.status}


@app.get("/api/shopping/thumbnail/jobs/{job_id}")
async def get_shopping_thumbnail_job(job_id: str) -> Dict[str, Any]:
    job = SHOPPING_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job_not_found")
    return asdict(job)


@app.get("/api/shopping/thumbnail/jobs/{job_id}/result")
async def get_shopping_thumbnail_result(job_id: str) -> Response:
    """이미지를 별도 URL로 반환 (큰 base64 JSON 대신 사용, 브라우저 렌더링 안정화)"""
    job = SHOPPING_JOBS.get(job_id)
    if not job or job.status != "completed" or not job.result_data_url:
        raise HTTPException(status_code=404, detail="result_not_ready")
    data_url = job.result_data_url
    if "," not in data_url:
        raise HTTPException(status_code=500, detail="invalid_result")
    _, b64 = data_url.split(",", 1)
    try:
        raw = base64.b64decode(b64)
    except Exception:
        raw = data_url.split(",", 1)[1].encode("utf-8")
    media = "image/png" if "image/png" in data_url else "image/svg+xml"
    return Response(content=raw, media_type=media)


# ---------- SNS 연동 ----------
class SnsPostRequest(BaseModel):
    connection_id: str = Field(..., min_length=1)
    caption: str = Field(..., min_length=1)
    image_url: Optional[str] = None
    video_url: Optional[str] = None


@app.get("/api/sns/connections")
async def sns_list_connections() -> Dict[str, Any]:
    """연동된 SNS 계정 목록 (토큰 제외)."""
    try:
        list_public = getattr(sns_auth, "list_connections_public", None)
        if not list_public:
            return {"connections": []}
        return {"connections": list_public()}
    except Exception as e:
        return {"connections": [], "error": str(e)}


@app.get("/api/sns/auth/facebook")
async def sns_auth_facebook(request: Request) -> Response:
    """Facebook OAuth URL로 리다이렉트."""
    base = str(request.base_url).rstrip("/")
    redirect_uri = f"{base}/api/sns/callback/facebook"
    url = getattr(sns_auth, "build_facebook_auth_url", lambda u: None)(redirect_uri)
    if not url:
        raise HTTPException(
            status_code=503,
            detail="FACEBOOK_APP_ID를 설정해주세요. (설정 → SNS 연동 안내 참고)",
        )
    return RedirectResponse(url=url)


@app.get("/api/sns/callback/facebook")
async def sns_callback_facebook(request: Request, code: Optional[str] = None) -> RedirectResponse:
    """Facebook OAuth 콜백. 토큰 저장 후 프론트 설정 화면으로."""
    from urllib.parse import quote
    if not code:
        raise HTTPException(status_code=400, detail="code missing")
    base = str(request.base_url).rstrip("/")
    redirect_uri = f"{base}/api/sns/callback/facebook"
    result = await sns_auth.exchange_facebook_code(code, redirect_uri)
    front = (base.rsplit("/api/", 1)[0] or base).rstrip("/")
    if result.get("error"):
        return RedirectResponse(f"{front}/?sns_error=" + quote(result["error"]) + "#settings")
    name = result.get("name", "Facebook")
    return RedirectResponse(f"{front}/?sns_connected=facebook&name=" + quote(name) + "#settings")


@app.get("/api/sns/auth/threads")
async def sns_auth_threads(request: Request) -> Response:
    """Threads OAuth URL로 리다이렉트."""
    base = str(request.base_url).rstrip("/")
    redirect_uri = f"{base}/api/sns/callback/threads"
    url = sns_threads_youtube.build_threads_auth_url(redirect_uri)
    if not url:
        raise HTTPException(
            status_code=503,
            detail="THREADS_APP_ID를 설정해주세요. (Meta 앱 대시보드에서 Threads API 사용 설정)",
        )
    return RedirectResponse(url=url)


@app.get("/api/sns/callback/threads")
async def sns_callback_threads(request: Request, code: Optional[str] = None, error: Optional[str] = None) -> RedirectResponse:
    from urllib.parse import quote
    base = str(request.base_url).rstrip("/")
    front = (base.rsplit("/api/", 1)[0] or base).rstrip("/")
    if error or not code:
        return RedirectResponse(f"{front}/?sns_error=" + quote(error or "code missing") + "#settings")
    redirect_uri = f"{base}/api/sns/callback/threads"
    result = await sns_threads_youtube.exchange_threads_code(code, redirect_uri)
    if result.get("error"):
        return RedirectResponse(f"{front}/?sns_error=" + quote(result["error"]) + "#settings")
    name = result.get("name", "Threads")
    return RedirectResponse(f"{front}/?sns_connected=threads&name=" + quote(name) + "#settings")


@app.get("/api/sns/auth/youtube")
async def sns_auth_youtube(request: Request) -> Response:
    """YouTube(Google) OAuth URL로 리다이렉트."""
    base = str(request.base_url).rstrip("/")
    redirect_uri = f"{base}/api/sns/callback/youtube"
    url = sns_threads_youtube.build_youtube_auth_url(redirect_uri)
    if not url:
        raise HTTPException(
            status_code=503,
            detail="GOOGLE_CLIENT_ID를 설정해주세요. (Google Cloud Console에서 YouTube API 사용 설정)",
        )
    return RedirectResponse(url=url)


@app.get("/api/sns/callback/youtube")
async def sns_callback_youtube(request: Request, code: Optional[str] = None, error: Optional[str] = None) -> RedirectResponse:
    from urllib.parse import quote
    base = str(request.base_url).rstrip("/")
    front = (base.rsplit("/api/", 1)[0] or base).rstrip("/")
    if error or not code:
        return RedirectResponse(f"{front}/?sns_error=" + quote(error or "code missing") + "#settings")
    redirect_uri = f"{base}/api/sns/callback/youtube"
    result = await sns_threads_youtube.exchange_youtube_code(code, redirect_uri)
    if result.get("error"):
        return RedirectResponse(f"{front}/?sns_error=" + quote(result["error"]) + "#settings")
    name = result.get("name", "YouTube")
    return RedirectResponse(f"{front}/?sns_connected=youtube&name=" + quote(name) + "#settings")


@app.post("/api/sns/disconnect/{connection_id}")
async def sns_disconnect(connection_id: str) -> Dict[str, Any]:
    """연동 계정 하나 해제 (connection_id 기준)."""
    disconnect = getattr(sns_auth, "disconnect_connection", lambda cid: False)
    if disconnect(connection_id):
        return {"ok": True}
    raise HTTPException(status_code=404, detail="connection_not_found")


@app.post("/api/sns/post")
async def sns_post(req: SnsPostRequest) -> Dict[str, Any]:
    """지정한 연동 계정(connection_id)으로 게시."""
    result = await sns_auth.post_to_connection(
        req.connection_id, req.caption, req.image_url, req.video_url
    )
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


# ---------- SNS 예약 발행 ----------
class SnsScheduleRequest(BaseModel):
    connection_id: str = Field(..., min_length=1)
    caption: str = Field(..., min_length=1)
    scheduled_at: str = Field(..., min_length=1)  # ISO datetime
    image_url: Optional[str] = None
    video_url: Optional[str] = None
    idea: Optional[str] = None


@app.get("/api/sns/schedule")
async def sns_list_schedule(include_posted: bool = False) -> Dict[str, Any]:
    items = getattr(sns_schedule, "list_scheduled", lambda **kw: [])(include_posted=include_posted)
    return {"items": items}


@app.post("/api/sns/schedule")
async def sns_add_schedule(req: SnsScheduleRequest) -> Dict[str, Any]:
    item = sns_schedule.add_scheduled(
        connection_id=req.connection_id,
        caption=req.caption,
        scheduled_at=req.scheduled_at,
        image_url=req.image_url,
        video_url=req.video_url,
        idea=req.idea,
    )
    return item


@app.delete("/api/sns/schedule/{item_id}")
async def sns_delete_schedule(item_id: str) -> Dict[str, Any]:
    if sns_schedule.delete_scheduled(item_id):
        return {"ok": True}
    raise HTTPException(status_code=404, detail="schedule_not_found")


@app.get("/api/sns/schedule/suggested-times")
async def sns_suggested_times(connection_id: Optional[str] = None) -> Dict[str, Any]:
    """스마트 스케줄링: engagement가 높은 시간대 추천 (휴리스틱 + 연동 계정 인사이트 반영)."""
    from datetime import datetime, timedelta
    now = datetime.now()
    # 일반적으로 SNS 참여가 높은 시간대 (한국 기준)
    slots = [
        ("07:00", "아침 출근 시간대"),
        ("12:00", "점심 시간"),
        ("19:00", "저녁 퇴근 후"),
    ]
    suggested = []
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    for t, label in slots:
        suggested.append({
            "datetime": tomorrow.strftime("%Y-%m-%d") + "T" + t + ":00",
            "label": label,
        })

    # connection_id가 주어지면 계정 존재 여부만 확인해 이유 문구에 반영 (향후 맞춤 추천 확장용)
    reason = "일반적으로 참여가 높은 시간대입니다. 연동 계정 성과 데이터가 쌓이면 맞춤 추천을 제공할 예정입니다."
    if connection_id:
        try:
            c = getattr(sns_auth, "get_connection_by_id", lambda _cid: None)(connection_id)
            if c:
                nm = (c.get("name") or c.get("platform") or "선택 계정").strip()
                reason = f"'{nm}' 기준으로 추천합니다. (현재는 기본 휴리스틱이며, 데이터가 쌓이면 더 정확해집니다.)"
            else:
                reason = "선택한 계정을 찾을 수 없어 기본 추천을 표시합니다."
        except Exception:
            pass
    return {"suggested": suggested, "reason": reason}


# ---------- SNS 성과 분석 ----------
@app.get("/api/sns/insights/{connection_id}")
async def sns_insights(connection_id: str) -> Dict[str, Any]:
    result = await sns_auth.get_connection_insights(connection_id)
    if result.get("error") and "metrics" not in result:
        raise HTTPException(status_code=400, detail=result.get("error", "unknown"))
    return result


class SnsInsightsReportRequest(BaseModel):
    connection_id: Optional[str] = None
    gemini_api_key: Optional[str] = None


@app.post("/api/sns/insights/report")
async def sns_insights_report(req: SnsInsightsReportRequest) -> Dict[str, Any]:
    """AI 성과 리포트: 연동 계정 지표를 분석·요약한 리포트 생성."""
    import os
    api_key = (req.gemini_api_key or "").strip() or os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="Gemini API Key가 필요합니다.")
    conns = sns_auth.list_connections_public()
    if req.connection_id:
        conns = [c for c in conns if c.get("id") == req.connection_id]
    if not conns:
        raise HTTPException(status_code=400, detail="연동 계정이 없습니다.")
    reports = []
    for c in conns:
        cid = c.get("id")
        data = await sns_auth.get_connection_insights(cid)
        metrics = data.get("metrics") or {}
        if metrics.get("_error"):
            reports.append({"name": data.get("name"), "report": "지표를 불러올 수 없습니다."})
            continue
        import httpx
        prompt = f"""다음은 SNS 연동 계정 '{data.get("name", "")}' ({data.get("platform", "")})의 최근 지표입니다. 
2~3문장으로 요약하고, 개선을 위한 추천 한두 가지를 간단히 작성해주세요. 한국어로 답하세요.

지표: {metrics}

요약 및 추천:"""
        try:
            r = await httpx.AsyncClient().post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}",
                json={
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.5, "maxOutputTokens": 512},
                },
                timeout=30.0,
            )
            if r.status_code != 200:
                reports.append({"name": data.get("name"), "report": "AI 생성 실패"})
                continue
            j = r.json()
            text = (j.get("candidates") or [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
            reports.append({"name": data.get("name"), "platform": data.get("platform"), "metrics": metrics, "report": text or "(생성 실패)"})
        except Exception as e:
            reports.append({"name": data.get("name"), "report": str(e)[:200]})
    return {"reports": reports}


@app.get("/api/sns/insights")
async def sns_insights_all() -> Dict[str, Any]:
    """모든 연동 계정의 성과 (목록 + 각 계정 지표)."""
    conns = sns_auth.list_connections_public()
    results = []
    for c in conns:
        cid = c.get("id")
        if not cid:
            continue
        try:
            data = await sns_auth.get_connection_insights(cid)
            results.append(data)
        except Exception:
            results.append({"connection_id": cid, "platform": c.get("platform"), "name": c.get("name"), "metrics": {}, "error": "조회 실패"})
    return {"connections": results}


# ---------- 1. AI 댓글 답변 ----------
@app.get("/api/sns/posts")
async def sns_list_posts(connection_id: str, limit: int = 10) -> Dict[str, Any]:
    result = await sns_auth.list_page_posts(connection_id, limit=limit)
    if result.get("error") and not result.get("posts"):
        raise HTTPException(status_code=400, detail=result.get("error", "unknown"))
    return result


@app.get("/api/sns/comments")
async def sns_list_comments(connection_id: str, post_id: str) -> Dict[str, Any]:
    result = await sns_auth.list_post_comments(connection_id, post_id)
    if result.get("error") and not result.get("comments"):
        raise HTTPException(status_code=400, detail=result.get("error", "unknown"))
    return result


class SnsCommentReplyRequest(BaseModel):
    connection_id: str = Field(..., min_length=1)
    comment_id: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1, max_length=8000)


@app.post("/api/sns/comments/reply")
async def sns_reply_comment(req: SnsCommentReplyRequest) -> Dict[str, Any]:
    result = await sns_auth.reply_to_comment(req.connection_id, req.comment_id, req.message)
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


class SnsCommentAiReplyRequest(BaseModel):
    connection_id: str = Field(..., min_length=1)
    comment_id: str = Field(..., min_length=1)
    comment_text: str = Field(..., min_length=1)
    post_message: Optional[str] = None
    gemini_api_key: Optional[str] = None


@app.post("/api/sns/comments/ai-reply")
async def sns_ai_reply_comment(req: SnsCommentAiReplyRequest) -> Dict[str, Any]:
    import os
    api_key = (req.gemini_api_key or "").strip() or os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="Gemini API Key가 필요합니다. 요청 body 또는 GEMINI_API_KEY 환경 변수.")
    import httpx
    prompt = f"""다음은 우리 페이지 게시물에 달린 댓글입니다. 브랜드에 친화적이고 간결하게 답글 한 문장을 작성해주세요. 이모지 1~2개 사용 가능. 답글만 출력하고 다른 설명은 하지 마세요.

게시물 내용: { (req.post_message or "")[:500] }

댓글: {req.comment_text[:1000]}

답글:"""
    try:
        r = await httpx.AsyncClient().post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}",
            json={
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.7, "maxOutputTokens": 256},
            },
            timeout=30.0,
        )
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail="AI 생성 실패")
        data = r.json()
        text = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
        if not text:
            raise HTTPException(status_code=502, detail="AI 답글 생성 결과가 비어 있습니다.")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=str(e))
    result = await sns_auth.reply_to_comment(req.connection_id, req.comment_id, text)
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return {"ok": True, "reply_text": text, "id": result.get("id")}


@app.post("/api/sns/comments/ai-private-reply")
async def sns_ai_private_reply(req: SnsCommentAiReplyRequest) -> Dict[str, Any]:
    """AI 생성 메시지를 댓글 작성자에게 비공개 답글(DM)로 전송."""
    import os
    api_key = (req.gemini_api_key or "").strip() or os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="Gemini API Key가 필요합니다.")
    import httpx
    prompt = f"""다음은 우리 페이지 게시물에 달린 댓글입니다. 댓글 작성자에게 보낼 친근하고 간결한 비공개 메시지(DM) 한 문장을 작성해주세요. 이모지 1~2개 사용 가능. 메시지만 출력하세요.

게시물: {(req.post_message or "")[:300]}
댓글: {req.comment_text[:800]}

비공개 메시지:"""
    try:
        r = await httpx.AsyncClient().post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}",
            json={
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.7, "maxOutputTokens": 256},
            },
            timeout=30.0,
        )
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail="AI 생성 실패")
        data = r.json()
        text = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
        if not text:
            raise HTTPException(status_code=502, detail="AI 메시지 생성 결과가 비어 있습니다.")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=str(e))
    result = await sns_auth.private_reply_to_comment(req.connection_id, req.comment_id, text)
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return {"ok": True, "message": text, "id": result.get("id")}

