import asyncio
import json
from playwright.async_api import async_playwright

async def test():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
        )

        urls_found = []

        async def handle_response(response):
            if "api/v4" in response.url or "api/v2" in response.url:
                try:
                    text = await response.text()
                    data = json.loads(text)
                    print("API RESPONSE:", response.url)
                    # Dump strings that look like mp4
                    import re
                    mp4s = re.findall(r'https?://[^"]+\.mp4[^"]*', text)
                    if mp4s:
                        print("  Found MP4s in JSON:", set(mp4s))
                        urls_found.extend(mp4s)
                except Exception as e:
                    pass

        page.on("response", handle_response)
        
        # Let's test a known universal link structure or just a shopee video
        # I'll just search for a shopee video on google to get a valid URL
        
        await browser.close()

if __name__ == "__main__":
    asyncio.run(test())
