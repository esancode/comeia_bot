import asyncio
import httpx
import re

HEADERS_MOBILE = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Referer": "https://shopee.com.br/",
}

async def test():
    url = "https://shopee.com.br/api/v4/item/get?itemid=23344605990&shopid=340156976"
    async with httpx.AsyncClient(timeout=15, headers=HEADERS_MOBILE) as client:
        # Try to get cookies first
        await client.get("https://shopee.com.br/")
        print("Cookies:", client.cookies)
        
        r = await client.get(url)
        print("Status Code:", r.status_code)
        if r.status_code == 200:
            data = r.json()
            item = data.get("data", {})
            print("Video Info:", item.get("video_info_list"))
        else:
            print("Response:", r.text[:200])

asyncio.run(test())
