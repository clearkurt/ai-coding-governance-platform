"""一次性真实 DeepSeek 连通性检查：验证 Responses API 端点与模型可用性。
不打印 API key。仅用于发布门槛 1 的初步验证，不提交。
"""

import asyncio

import httpx

from app.settings import get_settings


async def main() -> None:
    settings = get_settings()
    base = settings.deepseek_base_url.rstrip("/")
    url = f"{base}/responses"
    headers = {
        "Authorization": f"Bearer {settings.deepseek_api_key or ''}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "deepseek-v4-flash",
        "input": "请只回复两个字：正常",
        "stream": False,
    }
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(url, json=payload, headers=headers)
        print("endpoint:", url)
        print("status:", response.status_code)
        print("headers:", dict(response.headers))
        print("body head:", response.text[:800])

        # 流式验证：Codex 实际使用流式 Responses。
        stream_payload = {
            "model": "deepseek-v4-flash",
            "input": "请只回复两个字：正常",
            "stream": True,
        }
        async with client.stream("POST", url, json=stream_payload, headers=headers) as stream:
            print("stream status:", stream.status_code)
            lines: list[str] = []
            async for line in stream.aiter_lines():
                lines.append(line)
                if len(lines) >= 6:
                    break
            print("stream first lines:")
            for line in lines:
                print(" ", line[:200])


if __name__ == "__main__":
    asyncio.run(main())
