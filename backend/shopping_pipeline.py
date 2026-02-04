"""
쇼핑 썸네일 파이프라인:
1. Link Scraper: Playwright로 og:image, og:title 추출
2. Replicate: 누끼(배경 제거)
3. Gemini: 상품 분석(JSON) + 배경 생성
4. PIL: 제품+배경 합성 → 1000x1000 PNG
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import time
from typing import Any, Dict, Optional, Tuple

# Optional imports - fail gracefully if not installed
try:
    from playwright.async_api import async_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

try:
    from playwright_stealth import Stealth
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

try:
    import undetected_chromedriver as uc
    HAS_UC = True
except ImportError:
    HAS_UC = False

# rembg는 onnxruntime 필요. Python 3.14는 onnxruntime 미지원 → import 시 sys.exit(1)로 크래시하므로 선체크
try:
    import onnxruntime  # noqa: F401
    from rembg import remove as rembg_remove, new_session as rembg_new_session
    HAS_REMBG = True
except (ImportError, ModuleNotFoundError):
    HAS_REMBG = False
    rembg_remove = None
    rembg_new_session = None


SYSTEM_INSTRUCTION = (
    "상품의 원래 형태는 유지하면서 배경만 마법처럼 어울리게 바꿔줘. "
    "Keep the product's original form intact while magically changing only the background to match."
)


def _is_naver_error_page(html: str) -> bool:
    """네이버 에러/차단 페이지인지 확인 (상품 페이지가 아님)."""
    if not html or len(html) < 500:
        return True
    markers = [
        "현재 서비스 접속이 불가합니다",
        "module_error",
        "동시에 접속하는 이용자 수가 많거나",
        "시스템오류",
        "접속이 불가합니다",
    ]
    return any(m in html for m in markers)


def _extract_image_from_html(html: str) -> Tuple[Optional[str], Optional[str]]:
    """HTML/스크립트 내에서 이미지 URL 추출 (og:image, JSON, 정규식 등)."""
    if _is_naver_error_page(html):
        return (None, None)
    img, title = None, None
    # og:image / og:title
    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
    if not m:
        m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', html, re.I)
    if m:
        img = m.group(1).strip()
    m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
    if not m:
        m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']', html, re.I)
    if m:
        title = m.group(1).strip()
    if img and img.startswith("http"):
        return (img, title)
    # img[alt=대표이미지]
    if "대표이미지" in html:
        m = re.search(r'<img[^>]+alt=["\']대표이미지["\'][^>]+src=["\']([^"\']+)["\']', html)
        if not m:
            m = re.search(r'<img[^>]+src=["\']([^"\']+)["\'][^>]+alt=["\']대표이미지["\']', html)
        if m and m.group(1).startswith("http"):
            return (m.group(1).strip(), title)
    # shop-phinf URL (HTML 속성)
    m = re.search(r'(https?://[^"\'<>\s]*(?:shop-phinf|phinf\.pstatic)[^"\'<>\s]*\.(?:jpg|jpeg|png|webp)[^"\'<>\s]*)', html, re.I)
    if m and m.group(1).startswith("http"):
        return (m.group(1).strip(), title)
    # JSON/스크립트 내 이미지 URL (이스케이프 포함)
    for pat in [
        r'["\'](https?://[^"\']*shop-phinf[^"\']*\.(?:jpg|jpeg|png|webp)[^"\']*)["\']',
        r'"(https?://[^"]*phinf\.pstatic[^"]*\.(?:jpg|jpeg|png|webp)[^"]*)"',
        r'"imageUrl"\s*:\s*"([^"]+)"',
        r'"representativeImage"\s*:\s*"([^"]+)"',
        r'"image"\s*:\s*"([^"]+)"',
        r'"thumbUrl"\s*:\s*"([^"]+)"',
        r'"productImage"\s*:\s*"([^"]+)"',
    ]:
        m = re.search(pat, html, re.I)
        if m:
            u = m.group(1).replace("\\/", "/").strip()
            if u.startswith("http") and ("shop-phinf" in u or "phinf" in u or "pstatic" in u):
                if not re.search(r'logo|icon|banner|ad|spinner|1x1|pixel', u, re.I):
                    return (u, title)
    # 넓은 범위: pstatic 이미지
    m = re.search(r'(https?://[a-zA-Z0-9.-]*pstatic\.net/[^"\'<>\s]+\.(?:jpg|jpeg|png|webp)[^"\'<>\s]*)', html, re.I)
    if m and m.group(1).startswith("http"):
        u = m.group(1).strip()
        if not re.search(r'logo|icon|banner|ad', u, re.I):
            return (u, title)
    # 범용: og:image 외 product/상품 이미지 (브랜드 사이트 등)
    for pat in [
        r'"image"\s*:\s*"([^"]+)"',
        r'"productImage"\s*:\s*"([^"]+)"',
        r'"mainImage"\s*:\s*"([^"]+)"',
        r'"thumbnail"\s*:\s*"([^"]+)"',
        r'data-src=["\']([^"\']+\.(?:jpg|jpeg|png|webp)[^"\']*)["\']',
    ]:
        m = re.search(pat, html, re.I)
        if m:
            u = m.group(1).replace("\\/", "/").strip()
            if u.startswith("http") and not re.search(r'logo|icon|banner|ad|spinner|1x1|pixel', u, re.I):
                return (u, title)
    return (None, title)


def _scrape_with_uc(url: str, headless: bool = True) -> Tuple[Optional[str], Optional[str]]:
    """undetected-chromedriver로 크롤링 (네이버 봇 차단 우회). naver.com 먼저 방문 후 상품페이지."""
    if not HAS_UC:
        return (None, None)
    try:
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.common.by import By

        options = uc.ChromeOptions()
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--lang=ko-KR")
        options.add_argument("--window-position=-2400,-2400")
        driver = uc.Chrome(options=options, headless=headless, use_subprocess=True)
        try:
            if "smartstore.naver.com" in url or "brand.naver.com" in url or "shopping.naver.com" in url:
                driver.get("https://www.naver.com")
                time.sleep(1)
            driver.get(url)
            wait = WebDriverWait(driver, 12)
            try:
                wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'img[alt="대표이미지"], img[src*="shop-phinf"], meta[property="og:image"]')))
            except Exception:
                pass
            time.sleep(2)
            driver.execute_script("window.scrollTo(0, 400);")
            time.sleep(1)
            img = driver.execute_script("""
                var rep = document.querySelector('img[alt="대표이미지"]');
                if (rep && rep.src) return rep.src;
                var og = document.querySelector('meta[property="og:image"]');
                if (og && og.content) return og.content;
                var imgs = document.querySelectorAll('img[src*="shop-phinf.pstatic.net"], img[src*="phinf.pstatic"]');
                for (var i=0; i<imgs.length; i++) {
                    var s = imgs[i].src || '';
                    if (s && !/logo|icon|banner|ad/i.test(s)) return s;
                }
                var all = document.querySelectorAll('img[src]');
                for (var i=0; i<all.length; i++) {
                    var s = all[i].src || '';
                    if (s && /shop-phinf|phinf\\.pstatic/i.test(s)) return s;
                }
                return null;
            """)
            title = driver.execute_script("""
                var og = document.querySelector('meta[property="og:title"]');
                return og ? og.content : null;
            """)
            if img and str(img).startswith("http"):
                return (str(img).strip(), str(title).strip() if title else None)
        finally:
            driver.quit()
    except Exception:
        pass
    return (None, None)


def _try_naver_search_api(product_id: str, client_id: str, client_secret: str) -> Tuple[Optional[str], Optional[str]]:
    """네이버 쇼핑 검색 API로 상품 이미지 조회 (productId로 검색 시도)."""
    if not HAS_HTTPX or not product_id or not client_id or not client_secret:
        return (None, None)
    try:
        with httpx.Client(timeout=15) as client:
            r = client.get(
                "https://openapi.naver.com/v1/search/shop.json",
                params={"query": product_id, "display": 10},
                headers={"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret},
            )
            r.raise_for_status()
            data = r.json()
            for item in data.get("items", []):
                if str(item.get("productId")) == str(product_id):
                    img = item.get("image")
                    title = item.get("title", "").replace("<b>", "").replace("</b>", "")
                    if img and img.startswith("http"):
                        return (img, title or None)
            if data.get("items"):
                first = data["items"][0]
                if first.get("image", "").startswith("http"):
                    return (first["image"], first.get("title", "").replace("<b>", "").replace("</b>", "") or None)
    except Exception:
        pass
    return (None, None)


async def scrape_naver_product(
    url: str,
    naver_client_id: Optional[str] = None,
    naver_client_secret: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """httpx(빠름) → undetected-chromedriver → Playwright → 모바일 URL → Naver API. 무조건 크롤링 성공 목표."""
    # 0. httpx 먼저 시도 (가벼움, 일부 페이지는 초기 HTML에 이미지 포함)
    if HAS_HTTPX:
        urls_to_try = [url]
        if "smartstore.naver.com" in url and "m.smartstore" not in url:
            urls_to_try.insert(0, url.replace("smartstore.naver.com", "m.smartstore.naver.com"))
        for try_url in urls_to_try:
            try:
                async with httpx.AsyncClient(
                    timeout=8.0, follow_redirects=True,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    },
                ) as client:
                    r = await client.get(try_url)
                    r.raise_for_status()
                    img, title = _extract_image_from_html(r.text)
                    if img and img.startswith("http"):
                        return (img, title)
            except Exception:
                pass

    # 1. Playwright 먼저 (UC보다 빠름, domcontentloaded + og:image 즉시 추출)
    if HAS_PLAYWRIGHT:
        try:
            if HAS_STEALTH:
                pw_ctx = Stealth().use_async(async_playwright())
            else:
                pw_ctx = async_playwright()
            async with pw_ctx as p:
                try:
                    browser = await p.chromium.launch(channel="chrome", headless=True)
                except Exception:
                    browser = await p.chromium.launch(
                        headless=True,
                        args=["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage", "--no-sandbox"],
                    )
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                    viewport={"width": 1920, "height": 1080},
                    locale="ko-KR",
                    extra_http_headers={
                        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                        "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
                        "Sec-Ch-Ua-Mobile": "?0",
                        "Sec-Ch-Ua-Platform": '"Windows"',
                    },
                )
                page = await context.new_page()
                captured: list = []

                async def on_response(resp):
                    try:
                        if resp.url and ("product" in resp.url.lower() or "api" in resp.url or "graphql" in resp.url) and resp.status == 200:
                            ct = resp.headers.get("content-type", "")
                            if "json" in ct:
                                body = await resp.text()
                                if body and ("image" in body.lower() or "shop-phinf" in body or "phinf" in body):
                                    captured.append(body)
                    except Exception:
                        pass

                page.on("response", on_response)
                # 1) 상품 페이지 직접 방문 (naver 선방문 생략 시도 - 더 빠름)
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_timeout(2000)
                try:
                    og_img = await page.locator('meta[property="og:image"]').get_attribute("content")
                    og_title = await page.locator('meta[property="og:title"]').get_attribute("content")
                    if og_img and og_img.startswith("http"):
                        await browser.close()
                        return (og_img.strip(), og_title.strip() if og_title else None)
                except Exception:
                    pass
                # 2) 실패 시 naver 선방문 후 재시도
                if "smartstore.naver.com" in url or "brand.naver.com" in url or "shopping.naver.com" in url:
                    await page.goto("https://www.naver.com", wait_until="domcontentloaded", timeout=8000)
                    await page.wait_for_timeout(1500)
                    await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                    await page.wait_for_timeout(2500)
                try:
                    og_img = await page.locator('meta[property="og:image"]').get_attribute("content")
                    og_title = await page.locator('meta[property="og:title"]').get_attribute("content")
                    if og_img and og_img.startswith("http"):
                        await browser.close()
                        return (og_img.strip(), og_title.strip() if og_title else None)
                except Exception:
                    pass
                await page.evaluate("window.scrollTo(0, 400)")
                await page.wait_for_timeout(1000)
                img, title = await page.evaluate("""() => {
                    let img = null, title = null;
                    const tryImg = (s) => { if (s && s.startsWith('http') && !/logo|icon|banner|ad|spinner|1x1|pixel/i.test(s)) return s; return null; };
                    const repImg = document.querySelector('img[alt="대표이미지"]');
                    if (repImg) img = tryImg(repImg.src || repImg.getAttribute('data-src') || repImg.getAttribute('data-original'));
                    if (!img) {
                        const ogImg = document.querySelector('meta[property="og:image"]');
                        if (ogImg && ogImg.content) img = ogImg.content;
                    }
                    const ogTitle = document.querySelector('meta[property="og:title"]');
                    if (ogTitle && ogTitle.content) title = ogTitle.content;
                    if (!img) {
                        const selectors = [
                            'img[src*="shop-phinf.pstatic.net"]', 'img[src*="shop-phinf"]', 'img[src*="phinf.pstatic"]', 'img[src*="pstatic.net"]',
                            'img[data-src*="shop-phinf"]', 'img[data-src*="phinf"]', 'img[data-original*="phinf"]',
                            '[class*="product"] img', '[class*="Product"] img', '[class*="thumb"] img',
                            '[class*="goods"] img', '[class*="detail"] img', '[class*="slick"] img',
                            'main img', '[role="main"] img', '.product-detail img', '#content img'
                        ];
                        for (const sel of selectors) {
                            const el = document.querySelector(sel);
                            if (el) {
                                const s = (el.src || el.getAttribute('data-src') || el.getAttribute('data-original') || '').trim();
                                img = tryImg(s); if (img) break;
                            }
                        }
                    }
                    if (!img) {
                        const all = document.querySelectorAll('img[src]');
                        for (const el of all) {
                            const s = (el.src || '').trim();
                            if (s && /phinf|pstatic|shop-phinf|naver.*image/i.test(s) && !/logo|icon|banner|ad/i.test(s)) {
                                img = s; break;
                            }
                        }
                    }
                    if (!img) {
                        const first = document.querySelector('img[src^="http"][width], img[src^="http"][height]');
                        if (first && (first.naturalWidth || 0) >= 200) img = first.src;
                    }
                    return [img || null, title || null];
                }""")
                if not img and captured:
                    for body in captured:
                        img2, _ = _extract_image_from_html(body)
                        if img2:
                            img, title = img2, title or None
                            break
                await browser.close()
                if img and str(img).startswith("http"):
                    return (str(img).strip(), str(title).strip() if title else None)
        except Exception:
            pass

    # 2. undetected-chromedriver (Playwright 실패 시, 봇 차단 우회용 - 25초 소요)
    if HAS_UC:
        try:
            result = await asyncio.to_thread(_scrape_with_uc, url, True)
            if result[0]:
                return result
        except Exception:
            pass

    # 3. 모바일 URL 재시도 (10초 제한)
    if "smartstore.naver.com" in url and "m.smartstore" not in url:
        mobile_url = url.replace("smartstore.naver.com", "m.smartstore.naver.com")
        try:
            result = await asyncio.wait_for(
                scrape_naver_product(mobile_url, naver_client_id, naver_client_secret),
                timeout=12.0,
            )
            if result[0]:
                return result
        except asyncio.TimeoutError:
            pass

    # 4. 네이버 쇼핑 검색 API (productId 추출 후 검색)
    if naver_client_id and naver_client_secret:
        m = re.search(r"/products/(\d+)", url)
        if m:
            pid = m.group(1)
            result = await asyncio.to_thread(
                _try_naver_search_api, pid, naver_client_id, naver_client_secret
            )
            if result[0]:
                return result

    return (None, None)


_REMBG_SESSION: Optional[Any] = None


def _get_rembg_session():
    """고품질: bria-rmbg(이커머스 최적) → birefnet-general → isnet. REMBG_QUALITY=balanced 시 가벼운 모델 우선."""
    global _REMBG_SESSION
    if _REMBG_SESSION is not None:
        return _REMBG_SESSION
    if not rembg_new_session:
        return None
    quality = os.environ.get("REMBG_QUALITY", "high").lower()
    if quality in ("balanced", "low"):
        models = ("isnet-general-use", "u2net", "bria-rmbg")
    else:
        models = ("bria-rmbg", "birefnet-general", "isnet-general-use", "u2net")
    for model in models:
        try:
            _REMBG_SESSION = rembg_new_session(model)
            return _REMBG_SESSION
        except Exception:
            continue
    return None


def remove_background_local(image_bytes: bytes) -> Tuple[Optional[bytes], Optional[str]]:
    """로컬 rembg로 배경 제거. bria-rmbg + 2048해상도 + alpha_matting으로 고품질 누끼."""
    if not HAS_REMBG or not HAS_PIL:
        return (None, "rembg 또는 Pillow 미설치")
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        w, h = img.size
        quality = os.environ.get("REMBG_QUALITY", "high").lower()
        max_side = 2560 if quality == "ultra" else (2048 if quality == "high" else 1536)
        if max(w, h) > max_side:
            ratio = max_side / max(w, h)
            img = img.resize((int(w * ratio), int(h * ratio)), Image.Resampling.LANCZOS)
        session = _get_rembg_session()
        # high 모드: post_process_mask=False (bria 256단계 마스크 보존, morphological로 디테일 손실 방지)
        use_post = os.environ.get("REMBG_POST_PROCESS", "0").lower() in ("1", "true", "yes")
        out = rembg_remove(
            img,
            session=session,
            alpha_matting=True,
            alpha_matting_foreground_threshold=245,
            alpha_matting_background_threshold=8,
            alpha_matting_erode_size=3,  # 3: 병/화장품 등 선명한 엣지에 최적
            post_process_mask=use_post,
        )
        buf = io.BytesIO()
        out.save(buf, format="PNG")
        return (buf.getvalue(), None)
    except ModuleNotFoundError as e:
        if "onnxruntime" in str(e):
            return (None, "onnxruntime 미설치. Python 3.11/3.12 사용 또는 Replicate 토큰으로 대체.")
        return (None, f"로컬 누끼 실패: {str(e)[:80]}")
    except Exception as e:
        return (None, f"로컬 누끼 실패: {str(e)[:80]}")


def _download_image_bytes(image_url: str) -> Optional[bytes]:
    """이미지 다운로드. bytes 반환."""
    if not HAS_HTTPX:
        return None
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
            "Referer": "https://smartstore.naver.com/",
            "Accept": "image/*,*/*;q=0.8",
        }
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            r = client.get(image_url, headers=headers)
            r.raise_for_status()
            return r.content
    except Exception:
        return None


def _download_image_for_replicate(image_url: str) -> Optional[Tuple[bytes, str]]:
    """이미지 다운로드. (bytes, mime_type) 반환. 네이버 CDN은 Referer 필요."""
    if not HAS_HTTPX:
        return None
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
            "Referer": "https://smartstore.naver.com/",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        }
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            r = client.get(image_url, headers=headers)
            r.raise_for_status()
            content = r.content
            ct = r.headers.get("content-type", "image/jpeg")
            mime = "image/jpeg" if "jpeg" in ct or "jpg" in ct else "image/png" if "png" in ct else "image/webp" if "webp" in ct else "image/jpeg"
            return (content, mime)
    except Exception:
        return None


# Replicate 모델: Bria RMBG 2.0 (256단계 투명도, 이커머스 최적) → rembg 폴백
_REPLICATE_BRIA_VERSION = "063d41e5fbec2dcce4fa4ab5657f3ade0bf2c2625c73286a34af51cb181189c5"
_REPLICATE_REMBG_VERSION = "fb8af171cfa1616ddcf1242c093f9c46bcada5ad4cf6f2fbe8b81b330ec5c003"


def _replicate_remove(image_input: str, replicate_token: str, version: str) -> Tuple[Optional[bytes], Optional[str]]:
    """Replicate API 호출. (결과, 오류메시지) 반환."""
    if not HAS_HTTPX:
        return (None, "httpx 미설치")
    try:
        with httpx.Client(timeout=90) as client:
            r = client.post(
                "https://api.replicate.com/v1/predictions",
                headers={
                    "Authorization": f"Bearer {replicate_token}",
                    "Content-Type": "application/json",
                    "Prefer": "wait=60",
                },
                json={"version": version, "input": {"image": image_input}},
            )
            if r.status_code in (401, 403):
                return (None, f"Replicate API 인증 실패 (HTTP {r.status_code}). 토큰을 확인해주세요.")
            if r.status_code not in (200, 201):
                try:
                    err = r.json()
                    detail = err.get("detail", str(err))[:150]
                except Exception:
                    detail = r.text[:150]
                return (None, f"Replicate 오류 (HTTP {r.status_code}): {detail}")
            r.raise_for_status()
            data = r.json()
            out_url = data.get("output")
            if not out_url and data.get("status") in ("starting", "processing"):
                get_url = data.get("urls", {}).get("get")
                for _ in range(60):
                    time.sleep(1)
                    r2 = client.get(get_url, headers={"Authorization": f"Bearer {replicate_token}"})
                    r2.raise_for_status()
                    data = r2.json()
                    out_url = data.get("output")
                    if data.get("status") == "failed":
                        err = data.get("error", str(data))[:150]
                        return (None, f"Replicate 처리 실패: {err}")
                    if out_url or data.get("status") == "succeeded":
                        break
            if out_url and isinstance(out_url, str) and out_url.startswith("http"):
                r3 = client.get(out_url)
                r3.raise_for_status()
                return (r3.content, None)
            if isinstance(out_url, dict) and out_url.get("url"):
                r3 = client.get(out_url["url"])
                r3.raise_for_status()
                return (r3.content, None)
            return (None, "Replicate 출력 이미지를 가져올 수 없습니다.")
    except httpx.TimeoutException:
        return (None, "Replicate 요청 시간 초과. 잠시 후 다시 시도해주세요.")
    except Exception as e:
        return (None, f"Replicate 오류: {str(e)[:120]}")


def remove_background_replicate(image_url: str, replicate_token: str) -> Tuple[Optional[bytes], Optional[str]]:
    """Replicate로 배경 제거. Bria RMBG 2.0 우선 → rembg 폴백."""
    if not HAS_HTTPX:
        return (None, "httpx 미설치")
    image_input = image_url
    if "pstatic.net" in image_url or "naver" in image_url.lower():
        downloaded = _download_image_for_replicate(image_url)
        if downloaded:
            raw, mime = downloaded
            if len(raw) < 5 * 1024 * 1024:
                image_input = f"data:{mime};base64,{base64.b64encode(raw).decode()}"
        else:
            return (None, "네이버 이미지 다운로드 실패 (CDN 접근 불가)")
    # Bria RMBG 2.0 우선 (256단계 투명도, 상품 최적)
    result, err = _replicate_remove(image_input, replicate_token, _REPLICATE_BRIA_VERSION)
    if result:
        return (result, None)
    # rembg 폴백
    result, err2 = _replicate_remove(image_input, replicate_token, _REPLICATE_REMBG_VERSION)
    return (result, None) if result else (None, err or err2)


def _gemini_rest_generate(
    api_key: str,
    model: str,
    parts: list,
    generation_config: Optional[Dict[str, Any]] = None,
) -> Optional[dict]:
    """Gemini REST API 직접 호출 (google-genai 라이브러리 'previous' 오류 우회)."""
    if not HAS_HTTPX:
        return None
    try:
        payload: Dict[str, Any] = {"contents": [{"parts": parts}]}
        if generation_config:
            payload["generationConfig"] = generation_config
        with httpx.Client(timeout=90) as client:
            r = client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                json=payload,
            )
            r.raise_for_status()
            return r.json()
    except Exception:
        return None


def analyze_product_gemini(
    image_base64: str,
    product_title: str,
    gemini_api_key: str,
) -> Optional[Dict[str, Any]]:
    """Gemini로 상품 분석 → JSON (category, core_colors, background_concept)."""
    try:
        prompt = f"""다음 상품 이미지와 상품명을 분석해서, 아래 JSON 형식으로만 답변해줘. 다른 텍스트 없이 JSON만.

상품명: {product_title or '(없음)'}

JSON 형식:
{{
  "category": "상품 카테고리 (예: 화장품, 패션, 식품 등)",
  "core_colors": ["#hex1", "#hex2", "#hex3"],
  "background_concept": "이 상품에 어울리는 배경 (예: 부드러운 그라데이션, 은은한 텍스처. 제품 누끼와 자연스럽게 어울리도록 단순하고 평평한 느낌)"
}}
core_colors는 반드시 hex 코드(예: #ffcc00, #e8f4f8)로, 제품의 대표 색상 2~3개를 넣어줘."""
        resp = _gemini_rest_generate(
            gemini_api_key,
            "gemini-2.0-flash",
            [
                {"inline_data": {"mime_type": "image/png", "data": image_base64}},
                {"text": prompt},
            ],
        )
        if not resp:
            return None
        text = ""
        for c in resp.get("candidates", []):
            for p in c.get("content", {}).get("parts", []):
                if "text" in p:
                    text += p.get("text", "")
        text = text.strip()
        m = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text, re.DOTALL) or re.search(r"\{[^{}]*\}", text)
        if m:
            return json.loads(m.group())
        return None
    except Exception:
        return None


def generate_background_gemini(
    concept: Dict[str, Any],
    gemini_api_key: str,
) -> Optional[bytes]:
    """Gemini 이미지 생성 모델로 배경 생성. 1000x1000 PNG."""
    try:
        bg = concept.get("background_concept", "미니멀하고 깔끔한 광고 배경")
        colors = concept.get("core_colors", [])
        color_str = ", ".join(colors[:3]) if colors else ""
        prompt = (
            f"Create a 1000x1000 square background for product thumbnail. "
            f"Concept: {bg}. "
            f"Colors: {color_str or 'soft neutral'}. "
            f"Flat, soft gradient or subtle texture only. No dramatic lighting, no spotlights, no strong shadows. "
            f"No text, no products, no people. "
            f"Must blend naturally with product cutout placed on top - avoid complex scenes that clash with cutouts."
        )
        # gemini-2.5-flash-image: 이미지 생성 전용 모델 (responseModalities 필요)
        img_config = {"responseModalities": ["TEXT", "IMAGE"]}
        resp = _gemini_rest_generate(
            gemini_api_key,
            "gemini-2.5-flash-image",
            [{"text": prompt}],
            generation_config=img_config,
        )
        if not resp:
            # 폴백: gemini-3-pro-image-preview
            resp = _gemini_rest_generate(
                gemini_api_key,
                "gemini-3-pro-image-preview",
                [{"text": prompt}],
                generation_config=img_config,
            )
        if not resp:
            return None
        for c in resp.get("candidates", []):
            for p in c.get("content", {}).get("parts", []):
                inline = p.get("inlineData") or p.get("inline_data")
                if inline:
                    b64 = inline.get("data")
                    if b64:
                        return base64.b64decode(b64)
        return None
    except Exception:
        return None


def _parse_hex(c: str) -> Optional[Tuple[int, int, int]]:
    """hex 문자열을 RGB 튜플로. #ffcc00 또는 ffcc00 형식."""
    try:
        h = str(c).strip().lstrip("#")
        if len(h) == 6 and all(x in "0123456789abcdefABCDEF" for x in h):
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except Exception:
        pass
    return None


def _extract_dominant_colors(image_bytes: bytes, n: int = 2) -> list:
    """제품 이미지에서 대표 색상 추출. (흰색/투명 제외)"""
    if not HAS_PIL:
        return []
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        img = img.resize((50, 50), Image.Resampling.LANCZOS)
        px = img.load()
        colors: Dict[Tuple[int, int, int], int] = {}
        for y in range(50):
            for x in range(50):
                r, g, b = px[x, y]
                if r + g + b < 720 and r + g + b > 30:  # 너무 밝거나 어둡지 않은 색
                    rgb = (r // 16 * 16, g // 16 * 16, b // 16 * 16)
                    colors[rgb] = colors.get(rgb, 0) + 1
        sorted_colors = sorted(colors.items(), key=lambda x: -x[1])[:n]
        return [c for c, _ in sorted_colors]
    except Exception:
        return []


def _make_gradient_bg(colors: Optional[list] = None, product_bytes: Optional[bytes] = None) -> Optional["Image.Image"]:
    """제품 색상 기반 그라데이션 배경. 상단 밝음 → 하단 제품 톤."""
    if not HAS_PIL:
        return None
    top, bottom = (255, 253, 255), (225, 220, 238)  # 기본(회색) 폴백
    rgb_top, rgb_bottom = None, None
    if colors:
        parsed = [_parse_hex(c) for c in colors[:3] if _parse_hex(str(c))]
        if len(parsed) >= 2:
            rgb_top, rgb_bottom = parsed[0], parsed[1]
        elif len(parsed) == 1:
            r, g, b = parsed[0]
            rgb_top = (min(255, r + 60), min(255, g + 55), min(255, b + 60))
            rgb_bottom = (max(0, r - 30), max(0, g - 30), max(0, b - 20))
    if not rgb_top and product_bytes:
        extracted = _extract_dominant_colors(product_bytes, 2)
        if len(extracted) >= 2:
            rgb_top = tuple(min(255, c + 80) for c in extracted[0])
            rgb_bottom = tuple(max(0, c - 40) for c in extracted[1])
        elif len(extracted) == 1:
            r, g, b = extracted[0]
            rgb_top = (min(255, r + 80), min(255, g + 75), min(255, b + 80))
            rgb_bottom = (max(0, r - 50), max(0, g - 50), max(0, b - 40))
    if rgb_top:
        top = tuple(min(255, max(0, c)) for c in rgb_top)
    if rgb_bottom:
        bottom = tuple(min(255, max(0, c)) for c in rgb_bottom)
    img = Image.new("RGB", (1000, 1000))
    px = img.load()
    for y in range(1000):
        t = y / 999
        rgb = tuple(int(top[i] * (1 - t) + bottom[i] * t) for i in range(3))
        for x in range(1000):
            px[x, y] = rgb
    return img.convert("RGBA")


def composite_thumbnail(product_png: bytes, background_png: bytes, core_colors: Optional[list] = None) -> Optional[bytes]:
    """제품(누끼)을 배경 위에 합성. 1000x1000 PNG. 누끼 높이 980 맞춤."""
    if not HAS_PIL:
        return None
    try:
        bg = Image.open(io.BytesIO(background_png)).convert("RGBA")
        bg = bg.resize((1000, 1000), Image.Resampling.LANCZOS)

        product = Image.open(io.BytesIO(product_png)).convert("RGBA")
        # 제품을 붉은 박스 크기 수준으로 (캔버스에 꽉 차게, 1000x1000)
        ratio = min(1000 / product.height, 1000 / product.width, 1.0)
        nw, nh = int(product.width * ratio), int(product.height * ratio)
        product = product.resize((nw, nh), Image.Resampling.LANCZOS)

        x = (1000 - nw) // 2
        y = (1000 - nh) // 2

        # 알파 엣지 정제: halo 제거 + 256단계 투명도 보존 (bria-rmbg 품질 활용)
        r, g, b, a = product.split()

        def _refine_alpha(v: int) -> int:
            if v < 95:
                return 0
            if v >= 250:
                return 255
            return int((v - 95) * 255 / (250 - 95))

        a = a.point(_refine_alpha, mode="L")
        product = Image.merge("RGBA", (r, g, b, a))

        bg.paste(product, (x, y), product)

        out = io.BytesIO()
        bg.convert("RGB").save(out, format="PNG", quality=95)
        return out.getvalue()
    except Exception:
        return None


async def run_pipeline(
    url: str,
    gemini_api_key: str,
    replicate_token: str,
    on_progress: Optional[callable] = None,
    naver_client_id: Optional[str] = None,
    naver_client_secret: Optional[str] = None,
    image_url: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """
    전체 파이프라인 실행. (result_data_url, error_message)
    image_url 있으면 스크래핑 건너뜀.
    """
    import asyncio

    # 1. 이미지 확보 (URL 다운로드 / 로컬 파일 / 크롤링)
    if on_progress:
        on_progress("scrape", 5)
    img_url, title = None, None
    img_bytes = None  # 로컬 파일용

    if image_url:
        raw = image_url.strip().strip('"').strip("'")
        if raw.startswith("http://") or raw.startswith("https://"):
            img_url = raw
        else:
            # 로컬 파일 경로 시도
            from pathlib import Path
            p = Path(raw)
            if p.exists() and p.is_file():
                try:
                    img_bytes = p.read_bytes()
                except Exception:
                    pass
            if not img_bytes:
                return (None, "이미지 URL(https://...) 또는 로컬 파일 경로를 입력해주세요.\n\n예: https://shop-phinf.pstatic.net/... 또는 C:\\Users\\...\\image.jpg")

    if not img_url and not img_bytes:
        if url and (url.startswith("http://") or url.startswith("https://")):
            try:
                img_url, title = await asyncio.wait_for(
                    scrape_naver_product(url, naver_client_id=naver_client_id, naver_client_secret=naver_client_secret),
                    timeout=50.0,
                )
            except asyncio.TimeoutError:
                img_url, title = None, None
        if not img_url:
            return (None, "상품 이미지를 찾을 수 없습니다. 이미지 URL(https://...)을 직접 입력해주세요.")

    if on_progress:
        on_progress("rembg", 20)

    # 2. 누끼: 로컬 rembg 우선 (무료, 10~15초) → Replicate 폴백 (유료, 402 시 크레딧 필요)
    product_png = None
    rembg_err = None

    if HAS_REMBG:
        def _local():
            raw = img_bytes if img_bytes else _download_image_bytes(img_url)
            if raw:
                return remove_background_local(raw)
            return (None, "이미지 다운로드 실패" if img_url else "로컬 파일 읽기 실패")
        product_png, rembg_err = await asyncio.to_thread(_local)

    if not product_png and replicate_token and img_url:
        def _replicate():
            return remove_background_replicate(img_url, replicate_token)
        product_png, rembg_err = await asyncio.to_thread(_replicate)

    if not product_png:
        if not HAS_REMBG and not replicate_token:
            return (None, "누끼 처리 불가: rembg 설치(pip install rembg) 또는 Replicate 토큰 필요")
        if rembg_err and "402" in rembg_err:
            return (None, rembg_err + "\n\n💳 Replicate 크레딧 충전: https://replicate.com/account/billing")
        return (None, rembg_err or "누끼 처리 실패")

    if on_progress:
        on_progress("analyze", 40)

    # 3. Gemini 분석 (blocking)
    b64 = base64.b64encode(product_png).decode()
    concept = await asyncio.to_thread(
        analyze_product_gemini, b64, title or "", gemini_api_key
    )
    if not concept:
        concept = {
            "category": "상품",
            "core_colors": ["#ffffff"],
            "background_concept": "미니멀 화이트 배경",
        }

    if on_progress:
        on_progress("background", 60)

    # 4. 배경 생성 (blocking)
    bg_png = await asyncio.to_thread(
        generate_background_gemini, concept, gemini_api_key
    )
    if not bg_png and HAS_PIL:
        # Gemini 이미지 생성 실패 시: 은은한 그라데이션 폴백
        top, bottom = (252, 250, 255), (240, 242, 248)
        bg = Image.new("RGB", (1000, 1000))
        px = bg.load()
        for y in range(1000):
            t = y / 999
            rgb = tuple(int(top[i] * (1 - t) + bottom[i] * t) for i in range(3))
            for x in range(1000):
                px[x, y] = rgb
        out = io.BytesIO()
        bg.save(out, format="PNG")
        bg_png = out.getvalue()
    if not bg_png:
        return (None, "배경 생성 실패")

    if on_progress:
        on_progress("composite", 85)

    # 5. 합성 (blocking) - core_colors로 그라데이션 톤 조정
    final = await asyncio.to_thread(
        composite_thumbnail, product_png, bg_png, concept.get("core_colors") if concept else None
    )
    if not final:
        return (None, "합성 실패")

    if on_progress:
        on_progress("done", 100)

    data_url = "data:image/png;base64," + base64.b64encode(final).decode()
    return (data_url, None)
