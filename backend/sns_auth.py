"""
SNS 연동: Facebook OAuth + 페이지 게시. 연동 계정 여러 개 지원(클라이언트별).
환경 변수: FACEBOOK_APP_ID, FACEBOOK_APP_SECRET
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parent.parent
SNS_DATA_DIR = ROOT / "data"
SNS_CONNECTIONS_FILE = SNS_DATA_DIR / "sns_connections.json"

FB_GRAPH = "https://graph.facebook.com/v21.0"
FB_OAUTH_URL = "https://www.facebook.com/v21.0/dialog/oauth"


def _load_connections() -> List[Dict[str, Any]]:
    if not SNS_CONNECTIONS_FILE.exists():
        return []
    try:
        data = json.loads(SNS_CONNECTIONS_FILE.read_text(encoding="utf-8"))
        conns = data.get("connections", [])
        # 기존 항목에 id 없으면 부여
        for c in conns:
            if not c.get("id"):
                c["id"] = str(uuid4())
        return conns
    except Exception:
        return []


def _save_connections(connections: List[Dict[str, Any]]) -> None:
    SNS_DATA_DIR.mkdir(parents=True, exist_ok=True)
    SNS_CONNECTIONS_FILE.write_text(
        json.dumps({"connections": connections}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_facebook_app_credentials() -> tuple[Optional[str], Optional[str]]:
    app_id = os.environ.get("FACEBOOK_APP_ID", "").strip() or None
    app_secret = os.environ.get("FACEBOOK_APP_SECRET", "").strip() or None
    return app_id, app_secret


def build_facebook_auth_url(redirect_uri: str, app_id: Optional[str] = None, state: Optional[str] = None) -> Optional[str]:
    app_id = (app_id or "").strip() or get_facebook_app_credentials()[0]
    if not app_id:
        return None
    scopes = "pages_show_list,pages_read_engagement,pages_manage_posts,pages_messaging,read_insights,instagram_basic,instagram_content_publish,instagram_manage_insights"
    url = f"{FB_OAUTH_URL}?client_id={app_id}&redirect_uri={redirect_uri}&scope={scopes}&response_type=code"
    if state:
        from urllib.parse import quote
        url += "&state=" + quote(state)
    return url


async def exchange_facebook_code(
    code: str, redirect_uri: str, app_id: Optional[str] = None, app_secret: Optional[str] = None
) -> Dict[str, Any]:
    """코드로 액세스 토큰 교환 후 페이지 목록 조회. 첫 페이지 토큰 저장."""
    app_id = (app_id or "").strip() or get_facebook_app_credentials()[0]
    app_secret = (app_secret or "").strip() or get_facebook_app_credentials()[1]
    if not app_id or not app_secret:
        return {"error": "FACEBOOK_APP_ID or FACEBOOK_APP_SECRET not set"}

    async with httpx.AsyncClient() as client:
        # 1) Short-lived user token
        token_url = f"{FB_GRAPH}/oauth/access_token"
        r = await client.get(
            token_url,
            params={
                "client_id": app_id,
                "client_secret": app_secret,
                "redirect_uri": redirect_uri,
                "code": code,
            },
        )
        if r.status_code != 200:
            return {"error": f"token exchange failed: {r.text}"}
        data = r.json()
        short_token = data.get("access_token")
        if not short_token:
            return {"error": "no access_token in response"}

        # 2) Long-lived user token
        long_url = f"{FB_GRAPH}/oauth/access_token"
        r2 = await client.get(
            long_url,
            params={
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "fb_exchange_token": short_token,
            },
        )
        long_token = short_token
        if r2.status_code == 200:
            long_data = r2.json()
            long_token = long_data.get("access_token") or short_token

        # 3) Pages (with instagram_business_account for IG 연동)
        r3 = await client.get(
            f"{FB_GRAPH}/me/accounts",
            params={"access_token": long_token, "fields": "id,name,access_token"},
        )
        if r3.status_code != 200:
            return {"error": f"pages list failed: {r3.text}"}
        pages_data = r3.json()
        pages = pages_data.get("data", [])
        if not pages:
            return {"error": "연동 가능한 Facebook 페이지가 없습니다. 페이지 관리자여야 합니다."}

        connections = _load_connections()
        existing_page_ids = {c.get("page_id") for c in connections if c.get("platform") == "facebook"}
        existing_ig_ids = {c.get("ig_user_id") for c in connections if c.get("platform") == "instagram"}
        added = []
        for page in pages:
            page_id = page.get("id")
            if not page_id or page_id in existing_page_ids:
                continue
            page_name = page.get("name", "Facebook Page")
            page_token = page.get("access_token", "")
            connections.append({
                "id": str(uuid4()),
                "platform": "facebook",
                "page_id": page_id,
                "name": page_name,
                "access_token": page_token,
            })
            existing_page_ids.add(page_id)
            added.append(page_name)

            # 4) Instagram 비즈니스 계정 (페이지에 연결된 경우)
            r4 = await client.get(
                f"{FB_GRAPH}/{page_id}",
                params={"access_token": page_token, "fields": "instagram_business_account"},
            )
            if r4.status_code == 200:
                page_detail = r4.json()
                ig_account = page_detail.get("instagram_business_account")
                if ig_account and isinstance(ig_account, dict):
                    ig_id = ig_account.get("id")
                    if ig_id and ig_id not in existing_ig_ids:
                        r5 = await client.get(
                            f"{FB_GRAPH}/{ig_id}",
                            params={"access_token": page_token, "fields": "username,name"},
                        )
                        ig_name = page_name + " (IG)"
                        if r5.status_code == 200:
                            ig_data = r5.json()
                            ig_name = ig_data.get("username") or ig_data.get("name") or ig_name
                        connections.append({
                            "id": str(uuid4()),
                            "platform": "instagram",
                            "ig_user_id": ig_id,
                            "page_id": page_id,
                            "name": ig_name,
                            "access_token": page_token,
                        })
                        existing_ig_ids.add(ig_id)
                        added.append(ig_name)

        _save_connections(connections)
        return {"ok": True, "added": added, "name": added[0] if len(added) == 1 else f"{len(added)}개 계정"}


def list_connections_public() -> List[Dict[str, Any]]:
    """토큰 제외한 연동 목록 (프론트 표시용). 각 계정마다 고유 id."""
    connections = _load_connections()
    return [
        {
            "id": c.get("id"),
            "platform": c.get("platform"),
            "name": c.get("name"),
            "page_id": c.get("page_id"),
            "ig_user_id": c.get("ig_user_id"),
            "threads_user_id": c.get("threads_user_id"),
            "youtube_channel_id": c.get("youtube_channel_id"),
        }
        for c in connections
    ]


def get_connection_by_id(connection_id: str) -> Optional[Dict[str, Any]]:
    connections = _load_connections()
    for c in connections:
        if c.get("id") == connection_id:
            return c
    return None


def disconnect_connection(connection_id: str) -> bool:
    connections = _load_connections()
    before = len(connections)
    connections = [c for c in connections if c.get("id") != connection_id]
    if len(connections) < before:
        _save_connections(connections)
        return True
    return False


def update_connection_tokens(connection_id: str, access_token: Optional[str] = None, refresh_token: Optional[str] = None) -> bool:
    """YouTube 등 토큰 갱신 시 저장소에 반영."""
    connections = _load_connections()
    updated = False
    for c in connections:
        if c.get("id") == connection_id:
            if access_token:
                c["access_token"] = access_token
                updated = True
            if refresh_token is not None:
                c["refresh_token"] = refresh_token
                updated = True
            break
    if updated:
        _save_connections(connections)
    return updated


async def _post_to_facebook(
    token: str, page_id: str, message: str, image_url: Optional[str] = None
) -> Dict[str, Any]:
    url = f"{FB_GRAPH}/{page_id}/feed"
    params: Dict[str, Any] = {"access_token": token, "message": message}
    if image_url and image_url.startswith("http"):
        params["link"] = image_url
    async with httpx.AsyncClient() as client:
        r = await client.post(url, params=params)
    if r.status_code != 200:
        try:
            err = r.json()
            return {"error": err.get("error", {}).get("message", r.text)}
        except Exception:
            return {"error": r.text or f"HTTP {r.status_code}"}
    data = r.json()
    return {"ok": True, "post_id": data.get("id")}


async def _post_to_instagram(
    token: str, ig_user_id: str, message: str, image_url: Optional[str] = None
) -> Dict[str, Any]:
    """Instagram Content Publishing: media 생성 후 publish."""
    if not image_url or not image_url.startswith("http"):
        return {"error": "인스타그램 피드 게시에는 공개 접근 가능한 이미지 URL이 필요합니다."}
    async with httpx.AsyncClient() as client:
        r1 = await client.post(
            f"{FB_GRAPH}/{ig_user_id}/media",
            params={
                "access_token": token,
                "image_url": image_url,
                "caption": message[:2200] if message else "",
            },
        )
    if r1.status_code != 200:
        try:
            err = r1.json()
            return {"error": err.get("error", {}).get("message", r1.text)}
        except Exception:
            return {"error": r1.text or f"HTTP {r1.status_code}"}
    create = r1.json()
    creation_id = create.get("id")
    if not creation_id:
        return {"error": "미디어 생성 ID를 받지 못했습니다."}
    async with httpx.AsyncClient() as client:
        r2 = await client.post(
            f"{FB_GRAPH}/{ig_user_id}/media_publish",
            params={"access_token": token, "creation_id": creation_id},
        )
    if r2.status_code != 200:
        try:
            err = r2.json()
            return {"error": err.get("error", {}).get("message", r2.text)}
        except Exception:
            return {"error": r2.text or f"HTTP {r2.status_code}"}
    data = r2.json()
    return {"ok": True, "post_id": data.get("id")}


async def post_to_connection(
    connection_id: str, message: str, image_url: Optional[str] = None, video_url: Optional[str] = None
) -> Dict[str, Any]:
    """지정한 연동 계정(connection_id)으로 게시. Facebook / Instagram 지원."""
    conn = get_connection_by_id(connection_id)
    if not conn:
        return {"error": "연동 계정을 찾을 수 없습니다."}
    platform = conn.get("platform")
    token = conn.get("access_token")
    if not token:
        return {"error": "토큰이 없습니다."}
    if platform == "facebook":
        page_id = conn.get("page_id")
        if not page_id:
            return {"error": "페이지 정보가 없습니다."}
        return await _post_to_facebook(token, page_id, message, image_url)
    if platform == "instagram":
        ig_user_id = conn.get("ig_user_id")
        if not ig_user_id:
            return {"error": "인스타그램 계정 정보가 없습니다."}
        return await _post_to_instagram(token, ig_user_id, message, image_url)
    if platform == "threads":
        try:
            from backend import sns_threads_youtube
        except ImportError:
            import sns_threads_youtube
        threads_user_id = conn.get("threads_user_id")
        if not threads_user_id:
            return {"error": "Threads 계정 정보가 없습니다."}
        return await sns_threads_youtube.post_to_threads(threads_user_id, token, message, image_url)
    if platform == "youtube":
        try:
            from backend import sns_threads_youtube
        except ImportError:
            import sns_threads_youtube
        if not video_url or not str(video_url).startswith("http"):
            return {"error": "YouTube 발행은 공개 접근 가능한 MP4 영상 URL(video_url)이 필요합니다."}
        refresh_token = conn.get("refresh_token") or ""
        result = await sns_threads_youtube.post_to_youtube(
            access_token=token,
            refresh_token=refresh_token,
            title=(message.splitlines()[0] or message)[:90],
            description=message[:4900],
            video_url=video_url,
        )
        if result.get("new_access_token"):
            update_connection_tokens(connection_id, access_token=str(result.get("new_access_token")))
        return result
    return {"error": "해당 계정은 아직 발행을 지원하지 않습니다."}


async def get_connection_insights(connection_id: str) -> Dict[str, Any]:
    """연동 계정의 성과 지표 (Facebook Page / Instagram)."""
    conn = get_connection_by_id(connection_id)
    if not conn:
        return {"error": "연동 계정을 찾을 수 없습니다."}
    platform = conn.get("platform")
    token = conn.get("access_token")
    if not token:
        return {"error": "토큰이 없습니다."}
    out: Dict[str, Any] = {"connection_id": connection_id, "platform": platform, "name": conn.get("name"), "metrics": {}}
    async with httpx.AsyncClient() as client:
        if platform == "facebook":
            page_id = conn.get("page_id")
            if not page_id:
                return {**out, "error": "페이지 정보 없음"}
            import time as _t
            since_ts = int(_t.time()) - 86400 * 2  # 2일 전
            r = await client.get(
                f"{FB_GRAPH}/{page_id}/insights",
                params={
                    "access_token": token,
                    "metric": "page_fans,page_impressions,page_engaged_users",
                    "period": "day",
                    "since": since_ts,
                },
            )
            if r.status_code == 200:
                data = r.json()
                for item in data.get("data", []):
                    name = item.get("name", "")
                    values = item.get("values", [])
                    if values:
                        out["metrics"][name] = values[-1].get("value", 0)
            else:
                out["metrics"]["_error"] = r.text[:200]
        elif platform == "instagram":
            ig_id = conn.get("ig_user_id")
            if not ig_id:
                return {**out, "error": "인스타그램 계정 정보 없음"}
            r = await client.get(
                f"{FB_GRAPH}/{ig_id}/insights",
                params={
                    "access_token": token,
                    "metric": "impressions,reach,profile_views",
                    "period": "day",
                },
            )
            if r.status_code == 200:
                data = r.json()
                for item in data.get("data", []):
                    name = item.get("name", "")
                    values = item.get("values", [])
                    if values:
                        out["metrics"][name] = values[-1].get("value", 0)
            else:
                out["metrics"]["_error"] = r.text[:200]
        else:
            return {**out, "error": "성과 조회 미지원 플랫폼"}
    return out


# ---------- 1. AI 댓글 답변 ----------
async def list_page_posts(connection_id: str, limit: int = 10) -> Dict[str, Any]:
    """Facebook 페이지 최근 게시물 목록 (댓글 관리용)."""
    conn = get_connection_by_id(connection_id)
    if not conn or conn.get("platform") != "facebook":
        return {"error": "Facebook 페이지 연동이 필요합니다.", "posts": []}
    token = conn.get("access_token")
    page_id = conn.get("page_id")
    if not token or not page_id:
        return {"error": "토큰 또는 페이지 정보가 없습니다.", "posts": []}
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{FB_GRAPH}/{page_id}/feed",
            params={
                "access_token": token,
                "fields": "id,message,created_time,permalink_url",
                "limit": limit,
            },
        )
    if r.status_code != 200:
        return {"error": r.text[:300], "posts": []}
    data = r.json()
    return {"posts": data.get("data", [])}


async def list_post_comments(connection_id: str, post_id: str) -> Dict[str, Any]:
    """게시물 댓글 목록 (Facebook)."""
    conn = get_connection_by_id(connection_id)
    if not conn or conn.get("platform") != "facebook":
        return {"error": "Facebook 페이지 연동이 필요합니다.", "comments": []}
    token = conn.get("access_token")
    if not token:
        return {"error": "토큰이 없습니다.", "comments": []}
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{FB_GRAPH}/{post_id}/comments",
            params={
                "access_token": token,
                "fields": "id,message,from,created_time",
                "order": "chronological",
                "filter": "stream",
            },
        )
    if r.status_code != 200:
        return {"error": r.text[:300], "comments": []}
    data = r.json()
    return {"comments": data.get("data", [])}


async def reply_to_comment(connection_id: str, comment_id: str, message: str) -> Dict[str, Any]:
    """댓글에 답글 등록 (Facebook)."""
    conn = get_connection_by_id(connection_id)
    if not conn or conn.get("platform") != "facebook":
        return {"error": "Facebook 페이지 연동이 필요합니다."}
    token = conn.get("access_token")
    if not token:
        return {"error": "토큰이 없습니다."}
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{FB_GRAPH}/{comment_id}/comments",
            params={"access_token": token},
            data={"message": message[:8000]},
        )
    if r.status_code != 200:
        try:
            err = r.json()
            return {"error": err.get("error", {}).get("message", r.text)}
        except Exception:
            return {"error": r.text or f"HTTP {r.status_code}"}
    data = r.json()
    return {"ok": True, "id": data.get("id")}


async def private_reply_to_comment(connection_id: str, comment_id: str, message: str) -> Dict[str, Any]:
    """댓글에 비공개 답글(DM) 보내기 (Facebook)."""
    conn = get_connection_by_id(connection_id)
    if not conn or conn.get("platform") != "facebook":
        return {"error": "Facebook 페이지 연동이 필요합니다."}
    token = conn.get("access_token")
    if not token:
        return {"error": "토큰이 없습니다."}
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{FB_GRAPH}/{comment_id}/private_replies",
            params={"access_token": token},
            data={"message": message[:8000]},
        )
    if r.status_code != 200:
        try:
            err = r.json()
            return {"error": err.get("error", {}).get("message", r.text)}
        except Exception:
            return {"error": r.text or f"HTTP {r.status_code}"}
    data = r.json()
    return {"ok": True, "id": data.get("id")}
