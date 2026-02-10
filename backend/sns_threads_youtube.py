"""
스레드(Threads) · 유튜브 연동.
환경 변수: THREADS_APP_ID, THREADS_APP_SECRET / GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict, Optional
from uuid import uuid4

import httpx

# sns_auth와 동일한 저장소 사용
try:
    from backend import sns_auth
except ImportError:
    import sns_auth

THREADS_OAUTH_URL = "https://threads.net/oauth/authorize"
THREADS_TOKEN_URL = "https://graph.threads.net/oauth/access_token"
THREADS_GRAPH = "https://graph.threads.net/v1.0"

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"


def get_threads_credentials() -> tuple[Optional[str], Optional[str]]:
    app_id = os.environ.get("THREADS_APP_ID", "").strip() or None
    app_secret = os.environ.get("THREADS_APP_SECRET", "").strip() or None
    return app_id, app_secret


def build_threads_auth_url(redirect_uri: str) -> Optional[str]:
    app_id, _ = get_threads_credentials()
    if not app_id:
        return None
    scope = "threads_basic,threads_content_publish"
    return f"{THREADS_OAUTH_URL}?client_id={app_id}&redirect_uri={redirect_uri}&scope={scope}&response_type=code"


async def exchange_threads_code(code: str, redirect_uri: str) -> Dict[str, Any]:
    app_id, app_secret = get_threads_credentials()
    if not app_id or not app_secret:
        return {"error": "THREADS_APP_ID or THREADS_APP_SECRET not set"}
    # code에서 #_ 제거 (Meta 문서 참고)
    code = (code or "").split("#")[0].strip()
    async with httpx.AsyncClient() as client:
        r = await client.post(
            THREADS_TOKEN_URL,
            data={
                "client_id": app_id,
                "client_secret": app_secret,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
                "code": code,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if r.status_code != 200:
        return {"error": r.text[:400]}
    data = r.json()
    access_token = data.get("access_token")
    user_id = data.get("user_id")
    if not access_token or not user_id:
        return {"error": "no access_token or user_id in response"}
    # 프로필 이름 조회 (선택)
    name = f"Threads ({user_id})"
    async with httpx.AsyncClient() as client:
        r2 = await client.get(
            f"{THREADS_GRAPH}/{user_id}",
            params={"access_token": access_token, "fields": "username"},
        )
        if r2.status_code == 200:
            try:
                name = (r2.json().get("username") or name).strip() or name
            except Exception:
                pass
    connections = sns_auth._load_connections()
    existing = {c.get("threads_user_id") for c in connections if c.get("platform") == "threads"}
    if str(user_id) in existing:
        return {"error": "이미 연동된 Threads 계정입니다."}
    connections.append({
        "id": str(uuid4()),
        "platform": "threads",
        "threads_user_id": str(user_id),
        "name": name,
        "access_token": access_token,
    })
    sns_auth._save_connections(connections)
    return {"ok": True, "name": name}


async def post_to_threads(threads_user_id: str, token: str, message: str, image_url: Optional[str] = None) -> Dict[str, Any]:
    """Threads에 텍스트 또는 이미지 포스트."""
    if image_url and image_url.startswith("http"):
        media_type = "IMAGE"
        params = {"media_type": media_type, "image_url": image_url, "text": message[:500], "access_token": token}
    else:
        media_type = "TEXT"
        params = {"media_type": media_type, "text": message[:500], "access_token": token}
    async with httpx.AsyncClient() as client:
        r1 = await client.post(
            f"{THREADS_GRAPH}/{threads_user_id}/threads",
            params=params,
        )
    if r1.status_code != 200:
        try:
            err = r1.json()
            return {"error": err.get("error", {}).get("message", r1.text)}
        except Exception:
            return {"error": r1.text[:300]}
    create = r1.json()
    creation_id = create.get("id")
    if not creation_id:
        return {"error": "creation_id not in response"}
    async with httpx.AsyncClient() as client:
        r2 = await client.post(
            f"{THREADS_GRAPH}/{threads_user_id}/threads_publish",
            params={"creation_id": creation_id, "access_token": token},
        )
    if r2.status_code != 200:
        try:
            err = r2.json()
            return {"error": err.get("error", {}).get("message", r2.text)}
        except Exception:
            return {"error": r2.text[:300]}
    data = r2.json()
    return {"ok": True, "post_id": data.get("id")}


# ---------- YouTube ----------
def get_youtube_credentials() -> tuple[Optional[str], Optional[str]]:
    cid = os.environ.get("GOOGLE_CLIENT_ID", "").strip() or None
    secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip() or None
    return cid, secret


def build_youtube_auth_url(redirect_uri: str, state: Optional[str] = None) -> Optional[str]:
    cid, _ = get_youtube_credentials()
    if not cid:
        return None
    from urllib.parse import urlencode
    params = {
        "client_id": cid,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": GOOGLE_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
    }
    if state:
        params["state"] = state
    return GOOGLE_AUTH_URL + "?" + urlencode(params)


async def exchange_youtube_code(code: str, redirect_uri: str) -> Dict[str, Any]:
    cid, secret = get_youtube_credentials()
    if not cid or not secret:
        return {"error": "GOOGLE_CLIENT_ID or GOOGLE_CLIENT_SECRET not set"}
    async with httpx.AsyncClient() as client:
        r = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": cid,
                "client_secret": secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if r.status_code != 200:
        return {"error": r.text[:400]}
    data = r.json()
    access_token = data.get("access_token")
    refresh_token = data.get("refresh_token")
    if not access_token:
        return {"error": "no access_token in response"}
    # 채널 목록 조회
    async with httpx.AsyncClient() as client:
        r2 = await client.get(
            "https://www.googleapis.com/youtube/v3/channels",
            params={"part": "snippet", "mine": "true"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
    channel_id = None
    name = "YouTube"
    if r2.status_code == 200:
        try:
            items = r2.json().get("items", [])
            if items:
                ch = items[0]
                channel_id = ch.get("id")
                name = (ch.get("snippet", {}).get("title") or name).strip() or name
        except Exception:
            pass
    if not channel_id:
        channel_id = "unknown"
    connections = sns_auth._load_connections()
    connections.append({
        "id": str(uuid4()),
        "platform": "youtube",
        "youtube_channel_id": channel_id,
        "name": name,
        "access_token": access_token,
        "refresh_token": refresh_token or "",
    })
    sns_auth._save_connections(connections)
    return {"ok": True, "name": name}


async def refresh_youtube_access_token(refresh_token: str) -> Dict[str, Any]:
    cid, secret = get_youtube_credentials()
    if not cid or not secret:
        return {"error": "GOOGLE_CLIENT_ID or GOOGLE_CLIENT_SECRET not set"}
    if not refresh_token:
        return {"error": "no refresh_token"}
    async with httpx.AsyncClient() as client:
        r = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": cid,
                "client_secret": secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if r.status_code != 200:
        return {"error": r.text[:400]}
    data = r.json()
    at = data.get("access_token")
    if not at:
        return {"error": "no access_token in refresh response"}
    return {"ok": True, "access_token": at, "expires_in": data.get("expires_in")}


async def _download_video_to_tempfile(video_url: str) -> Dict[str, Any]:
    if not video_url or not video_url.startswith("http"):
        return {"error": "invalid video_url"}
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
    tmp_path = tmp.name
    tmp.close()
    content_type = "video/mp4"
    size = None
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
            async with client.stream("GET", video_url) as r:
                if r.status_code != 200:
                    return {"error": f"video download failed: HTTP {r.status_code} {r.text[:200]}"}
                ct = r.headers.get("content-type") or ""
                if ct:
                    content_type = ct.split(";")[0].strip() or content_type
                cl = r.headers.get("content-length")
                if cl and cl.isdigit():
                    size = int(cl)
                with open(tmp_path, "wb") as f:
                    async for chunk in r.aiter_bytes(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
        if size is None:
            try:
                size = os.path.getsize(tmp_path)
            except Exception:
                size = None
        return {"ok": True, "path": tmp_path, "content_type": content_type, "size": size}
    except Exception as e:
        return {"error": f"video download error: {str(e)[:200]}", "path": tmp_path}


async def _start_resumable_upload(access_token: str, title: str, description: str, content_type: str, size: Optional[int]) -> Dict[str, Any]:
    url = "https://www.googleapis.com/upload/youtube/v3/videos"
    params = {"uploadType": "resumable", "part": "snippet,status"}
    meta = {
        "snippet": {
            "title": (title or "WavaA 업로드")[:100],
            "description": (description or "")[:4900],
            "categoryId": "22",
        },
        "status": {"privacyStatus": "unlisted"},
    }
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    if content_type:
        headers["X-Upload-Content-Type"] = content_type
    if isinstance(size, int) and size > 0:
        headers["X-Upload-Content-Length"] = str(size)
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(url, params=params, json=meta, headers=headers)
    if r.status_code not in (200, 201):
        return {"error": r.text[:400], "status_code": r.status_code}
    loc = r.headers.get("location") or r.headers.get("Location")
    if not loc:
        return {"error": "resumable upload location header missing"}
    return {"ok": True, "location": loc}


async def _upload_resumable(location: str, access_token: str, file_path: str, content_type: str) -> Dict[str, Any]:
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": content_type or "application/octet-stream"}
    try:
        with open(file_path, "rb") as f:
            async with httpx.AsyncClient(timeout=None) as client:
                r = await client.put(location, content=f, headers=headers)
    except Exception as e:
        return {"error": f"upload failed: {str(e)[:200]}"}
    if r.status_code not in (200, 201):
        try:
            return {"error": (r.json().get("error", {}) or {}).get("message", r.text[:400]), "status_code": r.status_code}
        except Exception:
            return {"error": r.text[:400], "status_code": r.status_code}
    data = r.json()
    vid = data.get("id")
    return {"ok": True, "video_id": vid, "raw": data}


async def post_to_youtube(
    access_token: str,
    refresh_token: str,
    title: str,
    description: str,
    video_url: str,
) -> Dict[str, Any]:
    """
    YouTube에 영상 업로드(Resumable).
    - 입력은 공개 접근 가능한 video_url (MP4 권장)
    - 필요 시 refresh_token으로 access_token 갱신 후 재시도
    """
    dl = await _download_video_to_tempfile(video_url)
    tmp_path = dl.get("path")
    if not dl.get("ok"):
        # 다운로드 실패해도 임시파일 있으면 정리
        try:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return {"error": dl.get("error") or "video_download_failed"}
    content_type = dl.get("content_type") or "video/mp4"
    size = dl.get("size")
    try:
        # 1차 시도
        start = await _start_resumable_upload(access_token, title, description, content_type, size)
        if start.get("error") and start.get("status_code") == 401 and refresh_token:
            ref = await refresh_youtube_access_token(refresh_token)
            if ref.get("ok"):
                access_token = ref["access_token"]
                start = await _start_resumable_upload(access_token, title, description, content_type, size)
                if start.get("ok"):
                    start["new_access_token"] = access_token
        if start.get("error"):
            return {"error": start.get("error") or "youtube_upload_start_failed"}
        loc = start["location"]
        up = await _upload_resumable(loc, access_token, tmp_path, content_type)
        if up.get("error") and up.get("status_code") == 401 and refresh_token:
            # 업로드 단계에서 401이면 refresh 후 새 세션으로 재시도
            ref = await refresh_youtube_access_token(refresh_token)
            if ref.get("ok"):
                access_token = ref["access_token"]
                start2 = await _start_resumable_upload(access_token, title, description, content_type, size)
                if start2.get("ok"):
                    up = await _upload_resumable(start2["location"], access_token, tmp_path, content_type)
                    if up.get("ok"):
                        up["new_access_token"] = access_token
        if up.get("error"):
            return {"error": up.get("error") or "youtube_upload_failed"}
        out: Dict[str, Any] = {"ok": True, "post_id": up.get("video_id")}
        if start.get("new_access_token"):
            out["new_access_token"] = start.get("new_access_token")
        if up.get("new_access_token"):
            out["new_access_token"] = up.get("new_access_token")
        return out
    finally:
        try:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
