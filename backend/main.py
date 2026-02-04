from __future__ import annotations

import asyncio
import base64
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, Field

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


app = FastAPI(title="Wava Video Queue (Mock)")

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
        "<text x='120' y='190' font-family='Arial, sans-serif' font-size='44' font-weight='700' fill='#111827'>"
        "WavaA Shopping Thumbnail (Mock)"
        "</text>"
        "<text x='120' y='260' font-family='Arial, sans-serif' font-size='28' fill='#374151'>"
        "Backend pipeline will be connected next."
        "</text>"
        "<rect x='120' y='330' width='784' height='452' rx='32' fill='#f3f4f6'/>"
        "<text x='512' y='560' text-anchor='middle' font-family='Arial, sans-serif' font-size='34' fill='#6b7280'>"
        "상품 이미지 영역"
        "</text>"
        "<rect x='120' y='822' width='784' height='86' rx='24' fill='#111827'/>"
        "<text x='160' y='877' font-family='Arial, sans-serif' font-size='26' fill='white'>"
        "입력 링크:"
        "</text>"
        "<text x='260' y='877' font-family='Arial, sans-serif' font-size='26' fill='white'>"
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

