import asyncio
from playwright.async_api import async_playwright

async def test():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        page = await ctx.new_page()
        
        video_urls = []

        async def handle_response(response):
            try:
                content_type = response.headers.get("content-type", "")
                req_url = response.url
                
                if ".mp4" in req_url:
                    if req_url not in video_urls:
                        video_urls.append(req_url)
                
                elif "application/json" in content_type and ("api/v" in req_url or "graphql" in req_url):
                    text = await response.text()
                    import re
                    mp4s = re.findall(r'https?://[^"]+\.mp4[^"]*', text)
                    for mp4 in mp4s:
                        clean_mp4 = mp4.replace('\\u002F', '/').replace('\\/', '/')
                        if clean_mp4 not in video_urls:
                            video_urls.append(clean_mp4)
            except Exception:
                pass

        page.on("response", handle_response)
        
        url = "https://br.shp.ee/9j76sgxu?smtt=0.0.9"
        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        await page.wait_for_timeout(5000)
        
        print("ALL FOUND MP4 URLs:")
        for i, u in enumerate(video_urls):
            print(f"{i}: {u}")
            
        await browser.close()

if __name__ == "__main__":
    asyncio.run(test())
