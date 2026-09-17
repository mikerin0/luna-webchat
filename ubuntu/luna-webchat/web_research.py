import re
from urllib.parse import parse_qs, unquote, urlencode, urlparse

import httpx

from config import WEB_RESEARCH_ENABLED, WEB_RESEARCH_MAX_RESULTS, WEB_RESEARCH_TIMEOUT


def _clean_html_text(raw: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", raw, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _decode_duckduckgo_href(href: str) -> str:
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/l/?"):
        qs = parse_qs(urlparse(href).query)
        uddg = qs.get("uddg", [""])[0]
        if uddg:
            return unquote(uddg)
    return href


async def _web_research_context(query: str) -> str:
    if not WEB_RESEARCH_ENABLED:
        return ""

    q = query.strip()
    if not q:
        return ""

    search_url = "https://html.duckduckgo.com/html/?" + urlencode({"q": q})
    headers = {"User-Agent": "Mozilla/5.0 (compatible; LunaLocal/1.0)"}

    try:
        async with httpx.AsyncClient(timeout=WEB_RESEARCH_TIMEOUT, follow_redirects=True) as client:
            r = await client.get(search_url, headers=headers)
            r.raise_for_status()
            html = r.text
    except httpx.HTTPError:
        return ""

    results: list[tuple[str, str]] = []
    for match in re.finditer(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, flags=re.IGNORECASE):
        href, title_html = match.groups()
        url = _decode_duckduckgo_href(href)
        if not url.startswith("http"):
            continue
        title = _clean_html_text(title_html)
        if title:
            results.append((title, url))
        if len(results) >= WEB_RESEARCH_MAX_RESULTS:
            break

    if not results:
        return ""

    findings: list[str] = []
    async with httpx.AsyncClient(timeout=WEB_RESEARCH_TIMEOUT, follow_redirects=True) as client:
        for idx, (title, url) in enumerate(results, start=1):
            snippet = ""
            try:
                page = await client.get(url, headers=headers)
                if page.status_code == 200 and page.text:
                    snippet = _clean_html_text(page.text)[:900]
            except httpx.HTTPError:
                snippet = ""

            if snippet:
                findings.append(f"[{idx}] {title} - {url}\\nSnippet: {snippet}")
            else:
                findings.append(f"[{idx}] {title} - {url}")

    if not findings:
        return ""

    return (
        "Web research findings (from public pages). "
        "Use these as references and cite source URLs in your answer:\n\n"
        + "\n\n".join(findings)
    )
