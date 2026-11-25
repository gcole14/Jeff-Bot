"""Fetches and extracts official League of Legends patch notes from leagueoflegends.com."""

import html as html_lib
import json
import logging
import re
import ssl
from typing import Optional

import aiohttp

try:
    import certifi
    _SSL_CONTEXT: Optional[ssl.SSLContext] = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = None

logger = logging.getLogger(__name__)

PATCH_INDEX_URL = "https://www.leagueoflegends.com/en-us/news/tags/patch-notes/"
RIOT_SITE_ORIGIN = "https://www.leagueoflegends.com"
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=20)
USER_AGENT = "Mozilla/5.0 (compatible; JeffBot/2.0; +https://jeff.grantcole.net)"

# Sections we drop so the LLM focuses purely on gameplay changes
STOP_HEADINGS = {
    "bugfixes & qol changes",
    "bugfixes",
    "upcoming skins & chromas",
    "upcoming skins",
    "skins",
    "mythic shop",
}


def _extract_patch_version(slug_or_title: str) -> str:
    m = re.search(r"(\d{2})[.-](\d{1,2})", slug_or_title)
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    return slug_or_title


def parse_patch_html_to_text(body_html: str, max_chars: int = 16000) -> str:
    """Converts Riot's patchNotesRichText HTML body into compact, structured gameplay text."""
    # Insert newline markers at block boundaries before stripping tags
    text = re.sub(r"<(h[234])[^>]*>(.*?)</\1>", lambda m: f"\n### {m.group(1).upper()}: {m.group(2)}\n", body_html, flags=re.S | re.I)
    text = re.sub(r"<li[^>]*>", "\n- ", text, flags=re.I)
    text = re.sub(r"</?(p|div|br|tr)[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_lib.unescape(text)

    out_lines: list[str] = []
    skip_section = False
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        if line.startswith("### H2:"):
            heading = line.replace("### H2:", "").strip()
            if heading.lower() in STOP_HEADINGS:
                skip_section = True
                continue
            skip_section = False
            out_lines.append(f"\n## {heading}")
        elif skip_section:
            continue
        elif line.startswith("### H3:"):
            out_lines.append(f"### {line.replace('### H3:', '').strip()}")
        elif line.startswith("### H4:"):
            out_lines.append(f"#### {line.replace('### H4:', '').strip()}")
        else:
            out_lines.append(line)

    joined = "\n".join(out_lines).strip()
    return joined[:max_chars]


class PatchNotesFetcher:
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=_SSL_CONTEXT) if _SSL_CONTEXT else None
            self._session = aiohttp.ClientSession(
                timeout=HTTP_TIMEOUT,
                connector=connector,
                headers={"User-Agent": USER_AGENT},
            )
        return self._session

    async def get_latest_patch_info(self) -> Optional[dict]:
        """Returns {version, title, url, publish_date, body_text} for the newest official LoL patch."""
        session = await self._get_session()
        try:
            async with session.get(PATCH_INDEX_URL) as resp:
                if resp.status != 200:
                    logger.warning(f"Patch index HTTP {resp.status}")
                    return None
                index_html = await resp.text()
        except Exception as e:
            logger.warning(f"Failed to fetch patch notes index: {e}")
            return None

        matches = re.findall(r'(/en-us/news/game-updates/[^"<>]*patch[^"<>]*notes[^"<>]*)', index_html)
        if not matches:
            logger.warning("Could not find any patch note links on Riot index page.")
            return None

        # Pick first League of Legends patch notes link (skip TFT if present)
        chosen_path = next((p for p in matches if "teamfight" not in p.lower() and "tft" not in p.lower()), matches[0])
        full_url = f"{RIOT_SITE_ORIGIN}{chosen_path}"

        try:
            async with session.get(full_url) as resp:
                if resp.status != 200:
                    logger.warning(f"Patch page HTTP {resp.status}: {full_url}")
                    return None
                page_html = await resp.text()
        except Exception as e:
            logger.warning(f"Failed to fetch patch page {full_url}: {e}")
            return None

        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page_html, re.S)
        if not m:
            logger.warning("Could not locate __NEXT_DATA__ on patch notes page.")
            return None

        try:
            next_data = json.loads(m.group(1))
        except json.JSONDecodeError as e:
            logger.warning(f"Invalid __NEXT_DATA__ JSON: {e}")
            return None

        page = next_data.get("props", {}).get("pageProps", {}).get("page", {})
        title = page.get("title") or f"League of Legends Patch {_extract_patch_version(chosen_path)} Notes"
        blades = page.get("blades", [])

        publish_date = ""
        body_html = ""
        for blade in blades:
            btype = blade.get("type")
            if btype == "articleMasthead":
                publish_date = (blade.get("publishDate") or "")[:10]
            elif btype == "patchNotesRichText":
                body_html += "\n" + (blade.get("richText", {}).get("body") or "")

        if not body_html.strip():
            logger.warning(f"No patchNotesRichText body found on {full_url}")
            return None

        version = _extract_patch_version(title) or _extract_patch_version(chosen_path)
        return {
            "version": version,
            "title": title,
            "url": full_url,
            "publish_date": publish_date,
            "body_text": parse_patch_html_to_text(body_html),
        }

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
