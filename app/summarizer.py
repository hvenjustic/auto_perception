from __future__ import annotations

import difflib
import os
from typing import Any

import httpx


class ChangeSummarizer:
    def __init__(self) -> None:
        self.api_base = (
            os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com")
            .strip()
            .rstrip("/")
        )
        self.api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        self.model = os.getenv("DEEPSEEK_MODEL", "").strip() or "deepseek-chat"
        self.prompt = "对比旧网页和新网页的差异，总结网页更新内容，仅输出总结部分"

    async def summarize(self, page_url: str, old_text: str, new_text: str) -> str:
        old_text = (old_text or "").strip()
        new_text = (new_text or "").strip()
        if self.api_key:
            summary = await self._summarize_with_deepseek(page_url, old_text, new_text)
            if summary:
                return summary
        return self._fallback_summary(old_text, new_text)

    async def _summarize_with_deepseek(
        self, page_url: str, old_text: str, new_text: str
    ) -> str | None:
        endpoint = f"{self.api_base}/chat/completions"
        prompt = (
            f"{self.prompt}\n\n"
            f"页面: {page_url}\n"
            f"旧网页:\n{old_text[:4000]}\n\n"
            f"新网页:\n{new_text[:4000]}"
        )
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(endpoint, json=payload, headers=headers)
            if resp.status_code >= 400:
                return None
            data = resp.json()
            content = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
            )
            return content or None
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
            return None

    def _fallback_summary(self, old_text: str, new_text: str) -> str:
        if not old_text and new_text:
            first = " ".join(new_text.split()[:40])
            return f"检测到新增页面内容。摘要：{first[:120]}"

        old_lines = [x.strip() for x in old_text.splitlines() if x.strip()]
        new_lines = [x.strip() for x in new_text.splitlines() if x.strip()]
        diff = difflib.ndiff(old_lines, new_lines)
        added: list[str] = []
        removed_count = 0
        for line in diff:
            if line.startswith("+ "):
                added.append(line[2:].strip())
            elif line.startswith("- "):
                removed_count += 1
            if len(added) >= 3:
                break

        added_preview = "；".join(x[:40] for x in added if x)
        if not added_preview:
            return "页面内容发生变化，但提取到的文本差异较小。"
        return f"页面有更新，新增重点 {len(added)} 处，删除 {removed_count} 处。新增内容示例：{added_preview}"
