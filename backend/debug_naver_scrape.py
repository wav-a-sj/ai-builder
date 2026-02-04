"""
네이버 스마트스토어 페이지 분석 스크립트
실행: python -m backend.debug_naver_scrape
"""
import asyncio
import re
import sys

URL = "https://smartstore.naver.com/rocketdeliverymarket/products/13001605917"

async def main():
    print("=" * 60)
    print("네이버 스마트스토어 페이지 분석")
    print("=" * 60)
    print(f"URL: {URL}\n")

    # 1. httpx로 초기 HTML 확인
    try:
        import httpx
        print("[1] httpx로 HTML 가져오는 중...")
        async with httpx.AsyncClient(timeout=15, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"}
        ) as client:
            r = await client.get(URL)
            r.raise_for_status()
            html = r.text
        print(f"    HTML 길이: {len(html)} bytes")

        og_image = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
        if not og_image:
            og_image = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', html, re.I)
        print(f"    og:image: {og_image.group(1) if og_image else '(없음)'}")

        대표이미지 = "대표이미지" in html
        print(f"    '대표이미지' 텍스트 포함: {대표이미지}")

        shop_phinf = "shop-phinf" in html or "phinf.pstatic" in html
        print(f"    shop-phinf/phinf.pstatic URL 포함: {shop_phinf}")

        if 대표이미지:
            m = re.search(r'<img[^>]+alt=["\']대표이미지["\'][^>]+src=["\']([^"\']+)["\']', html)
            if not m:
                m = re.search(r'<img[^>]+src=["\']([^"\']+)["\'][^>]+alt=["\']대표이미지["\']', html)
            if not m:
                m = re.search(r'src=["\']([^"\']*shop-phinf[^"\']*)["\']', html)
            print(f"    추출된 이미지 URL: {m.group(1) if m else '(매칭 실패)'}")

        # script 태그 내 JSON 등에서 이미지 URL 찾기
        img_urls = re.findall(r'https?://[^"\'<>\s]*(?:shop-phinf|phinf\.pstatic)[^"\'<>\s]*\.(?:jpg|jpeg|png|webp)[^"\'<>\s]*', html, re.I)
        if img_urls:
            print(f"    정규식으로 찾은 이미지 수: {len(img_urls)}")
            print(f"    첫 번째: {img_urls[0][:80]}...")
    except Exception as e:
        print(f"    httpx 오류: {e}")

    # 2. Playwright로 분석
    try:
        from playwright.async_api import async_playwright
        print("\n[2] Playwright로 페이지 로드 중...")
        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(channel="chrome", headless=True)
            except Exception:
                browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800},
                locale="ko-KR",
            )
            page = await context.new_page()
            await page.goto(URL, wait_until="networkidle", timeout=40000)
            await page.wait_for_timeout(5000)

            title = await page.title()
            current_url = page.url
            print(f"    페이지 제목: {title[:60] if title else '(없음)'}...")
            print(f"    현재 URL: {current_url[:80]}...")

            html = await page.content()
            with open("debug_naver_page.html", "w", encoding="utf-8") as f:
                f.write(html)
            print(f"    HTML 저장됨: debug_naver_page.html ({len(html)} bytes)")

            # iframe 확인
            iframes = await page.evaluate("() => document.querySelectorAll('iframe').length")
            print(f"    iframe 개수: {iframes}")

            result = await page.evaluate("""() => {
                const ogImg = document.querySelector('meta[property="og:image"]');
                const repImg = document.querySelector('img[alt="대표이미지"]');
                const allPhinf = document.querySelectorAll('img[src*="shop-phinf"], img[src*="phinf"]');
                const allImgs = document.querySelectorAll('img');
                const imgSrcs = Array.from(allImgs).slice(0, 5).map(i => i.src || i.getAttribute('data-src') || '(no src)');
                return {
                    ogImage: ogImg ? ogImg.content : null,
                    repImgSrc: repImg ? (repImg.src || repImg.getAttribute('data-src')) : null,
                    repImgCount: document.querySelectorAll('img[alt="대표이미지"]').length,
                    phinfCount: allPhinf.length,
                    firstPhinf: allPhinf[0] ? allPhinf[0].src : null,
                    bodyImgCount: allImgs.length,
                    first5ImgSrcs: imgSrcs,
                };
            }""")
            await browser.close()

        print(f"    og:image: {result.get('ogImage') or '(없음)'}")
        print(f"    img[alt=대표이미지] 개수: {result.get('repImgCount', 0)}")
        print(f"    img[alt=대표이미지] src: {result.get('repImgSrc') or '(없음)'}")
        print(f"    shop-phinf 이미지 개수: {result.get('phinfCount', 0)}")
        print(f"    첫 shop-phinf src: {result.get('firstPhinf') or '(없음)'}")
        print(f"    전체 img 개수: {result.get('bodyImgCount', 0)}")
        for i, s in enumerate(result.get('first5ImgSrcs', [])[:5]):
            print(f"    img[{i}] src: {str(s)[:100]}...")

    except ImportError:
        print("\n[2] Playwright 미설치 - 건너뜀")
    except Exception as e:
        print(f"\n[2] Playwright 오류: {e}")

    print("\n" + "=" * 60)

if __name__ == "__main__":
    asyncio.run(main())
