"""Playwright-based card renderer for Jeff Bot.

Renders HTML templates to 1200x630 PNG screenshots and returns them as
discord.File objects ready to post.
"""

import asyncio
import io
import json
import logging
import os
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import discord
from playwright.async_api import async_playwright, Browser

from stats import PlayerStats

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"

_TIER_MAP = {
    "IRON": "iron", "BRONZE": "bronze", "SILVER": "silver", "GOLD": "gold",
    "PLATINUM": "platinum", "EMERALD": "emerald", "DIAMOND": "diamond",
    "MASTER": "master", "GRANDMASTER": "grandmaster", "CHALLENGER": "challenger",
}


def _format_damage(n: int) -> str:
    if n >= 1_000_000:
        v = n / 1_000_000
        return f"{v:.1f}M"
    if n >= 1_000:
        v = n / 1_000
        return f"{v:.1f}k" if (v % 1) >= 0.05 else f"{v:.0f}k"
    return str(n)


def _champ_kda(cstats: dict) -> float:
    games = max(cstats["games"], 1)
    deaths_pg = cstats["deaths"] / games
    return round((cstats["kills"] / games + cstats["assists"] / games) / max(deaths_pg, 1), 2)


def _build_weekly_data(ps: PlayerStats, version: str, date_range: str) -> dict:
    tier = _TIER_MAP.get(ps.solo_tier, "iron")
    games = max(ps.games_played, 1)
    avg_k = round(ps.total_kills / games, 1)
    avg_d = round(ps.total_deaths / games, 1)
    avg_a = round(ps.total_assists / games, 1)

    tag = ps.riot_id.split("#")[1] if "#" in ps.riot_id else "NA1"
    icon_url = (
        f"https://ddragon.leagueoflegends.com/cdn/{version}/img/profileicon/{ps.profile_icon_id}.png"
        if ps.profile_icon_id and version else None
    )

    champs = []
    for name, count in ps.top_champions:
        cstats = ps.champion_stats.get(name) or {"games": count, "wins": 0, "kills": 0, "deaths": 0, "assists": 0}
        wr = round(cstats["wins"] / max(cstats["games"], 1) * 100)
        kda = _champ_kda(cstats)
        champ_icon = (
            f"https://ddragon.leagueoflegends.com/cdn/{version}/img/champion/{name}.png"
            if version else None
        )
        champs.append({"name": name, "games": count, "wr": wr, "kda": kda, "iconUrl": champ_icon})

    return {
        "summoner": {
            "name": ps.display_name,
            "tag": tag,
            "level": ps.summoner_level,
            "iconUrl": icon_url,
        },
        "week": {
            "label": "WEEKLY STATS",
            "dateRange": date_range,
            "gamesPlayed": ps.games_played,
            "wins": ps.wins,
            "losses": ps.losses,
            "winRate": round(ps.win_rate),
            "kda": {
                "k": avg_k,
                "d": avg_d,
                "a": avg_a,
                "ratio": round(ps.kda, 2),
            },
            "cs": round(ps.cs_per_min, 1),
            "vision": round(ps.avg_vision_score, 1),
            "kp": round(ps.kill_participation * 100),
            "lpChange": ps.lp_change,
            "currentLp": ps.solo_lp,
            "mostPlayedRole": ps.most_played_role,
            "roleSplit": ps.role_split,
            "dmgTotal": _format_damage(ps.total_damage_to_champions),
        },
        "champs": champs,
    }


def _build_versus_data(a: PlayerStats, b: PlayerStats, version: str, date_range: str) -> dict:
    def _player(ps: PlayerStats) -> dict:
        tier = _TIER_MAP.get(ps.solo_tier, "iron")
        games = max(ps.games_played, 1)
        avg_k = round(ps.total_kills / games, 1)
        avg_d = round(ps.total_deaths / games, 1)
        avg_a = round(ps.total_assists / games, 1)
        tag = ps.riot_id.split("#")[1] if "#" in ps.riot_id else "NA1"
        icon_url = (
            f"https://ddragon.leagueoflegends.com/cdn/{version}/img/profileicon/{ps.profile_icon_id}.png"
            if ps.profile_icon_id and version else None
        )

        top = ps.top_champions
        top_champ = None
        if top:
            champ_name, champ_games = top[0]
            champ_icon = (
                f"https://ddragon.leagueoflegends.com/cdn/{version}/img/champion/{champ_name}.png"
                if version else None
            )
            top_champ = {"name": champ_name, "games": champ_games, "iconUrl": champ_icon}

        return {
            "name": ps.display_name,
            "tag": tag,
            "level": ps.summoner_level,
            "iconUrl": icon_url,
            "tier": tier,
            "division": ps.solo_rank or "",
            "games": ps.games_played,
            "wins": ps.wins,
            "losses": ps.losses,
            "winRate": round(ps.win_rate),
            "kda": round(ps.kda, 2),
            "kdaLine": f"{avg_k} / {avg_d} / {avg_a}",
            "kp": round(ps.kill_participation * 100),
            "cs": round(ps.cs_per_min, 1),
            "vision": round(ps.avg_vision_score, 1),
            "lpChange": ps.lp_change,
            "dmgTotal": _format_damage(ps.total_damage_to_champions),
            "dmgTotalRaw": ps.total_damage_to_champions,
            "topChamp": top_champ,
        }

    return {
        "versus": {
            "week": {"label": "WEEKLY DUEL", "dateRange": date_range},
            "left": _player(a),
            "right": _player(b),
        }
    }


class CardRenderer:
    """Manages a Playwright browser instance for rendering stat cards."""

    def __init__(self):
        self._playwright = None
        self._browser: Browser | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        logger.info("CardRenderer: Playwright browser started.")

    async def stop(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("CardRenderer: Playwright browser stopped.")

    async def render_weekly_card(self, ps: PlayerStats, version: str, date_range: str) -> discord.File:
        data = _build_weekly_data(ps, version, date_range)
        tier = _TIER_MAP.get(ps.solo_tier, "iron")
        png = await self._screenshot("card_weekly.html", data, tier)
        safe_name = ps.display_name.replace(" ", "_")
        return discord.File(io.BytesIO(png), filename=f"weekly_{safe_name}.png")

    async def render_versus_card(
        self, a: PlayerStats, b: PlayerStats, version: str, date_range: str
    ) -> discord.File:
        data = _build_versus_data(a, b, version, date_range)
        png = await self._screenshot("card_versus.html", data, "master")
        name_a = a.display_name.replace(" ", "_")
        name_b = b.display_name.replace(" ", "_")
        return discord.File(io.BytesIO(png), filename=f"versus_{name_a}_vs_{name_b}.png")

    async def _screenshot(self, template_name: str, data: dict, tier: str) -> bytes:
        template_path = TEMPLATES_DIR / template_name
        template_html = template_path.read_text(encoding="utf-8")

        injection = (
            "<script>\n"
            f"window.CARD_DATA = {json.dumps(data, ensure_ascii=False)};\n"
            f"window.CARD_TIER = {json.dumps(tier)};\n"
            "</script>"
        )
        html = template_html.replace("<!-- DATA_INJECTION -->", injection, 1)

        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                suffix=".html", mode="w", encoding="utf-8", delete=False
            ) as f:
                f.write(html)
                tmp_path = f.name

            async with self._lock:
                page = await self._browser.new_page(
                    viewport={"width": 1200, "height": 630}
                )
                try:
                    await page.goto(f"file://{tmp_path}", wait_until="networkidle", timeout=20000)
                    await page.wait_for_selector(
                        "#card-root[data-ready='1']", timeout=15000
                    )
                    element = await page.query_selector("#card-root > div")
                    png = await element.screenshot(type="png")
                    return png
                finally:
                    await page.close()
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
