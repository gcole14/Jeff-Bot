import aiohttp
import asyncio
import logging
import ssl
import time
from typing import Optional
from urllib.parse import quote

try:
    import certifi
    _SSL_CONTEXT: Optional[ssl.SSLContext] = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = None

from config import (
    RIOT_API_KEY,
    REGION,
    ROUTING,
    DDRAGON_VERSIONS_URL,
    DDRAGON_CHAMPION_URL,
    DDRAGON_ITEM_URL,
)

logger = logging.getLogger(__name__)

BASE_PLATFORM = f"https://{REGION}.api.riotgames.com"
BASE_ROUTING = f"https://{ROUTING}.api.riotgames.com"

DDRAGON_CACHE_TTL_SECONDS = 24 * 3600
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)


class RiotAPIError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(f"Riot API {status}: {message}")


class RiotAuthError(RiotAPIError):
    """Raised on 401/403 (missing, invalid, or expired 24h Riot dev key)."""


class RiotAPI:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key: str = (api_key if api_key is not None else RIOT_API_KEY) or ""
        self._session: Optional[aiohttp.ClientSession] = None
        self._public_session: Optional[aiohttp.ClientSession] = None
        self._ddragon_version_cache: Optional[tuple[str, float]] = None
        self._ddragon_champion_cache: dict[str, dict] = {}
        self._ddragon_item_cache: dict[str, dict] = {}

    def set_api_key(self, new_key: str) -> None:
        """Hot-swap the Riot API key in memory (useful when refreshing 24h dev keys)."""
        self.api_key = new_key.strip()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=_SSL_CONTEXT) if _SSL_CONTEXT else None
            self._session = aiohttp.ClientSession(timeout=REQUEST_TIMEOUT, connector=connector)
        return self._session

    async def _get_public_session(self) -> aiohttp.ClientSession:
        if self._public_session is None or self._public_session.closed:
            connector = aiohttp.TCPConnector(ssl=_SSL_CONTEXT) if _SSL_CONTEXT else None
            self._public_session = aiohttp.ClientSession(timeout=REQUEST_TIMEOUT, connector=connector)
        return self._public_session

    async def _get_public(self, url: str):
        """Unauthenticated GET for Data Dragon CDN (no X-Riot-Token)."""
        session = await self._get_public_session()
        async with session.get(url) as resp:
            if resp.status != 200:
                raise RiotAPIError(resp.status, f"Data Dragon: {await resp.text()}")
            return await resp.json()

    async def _get(self, url: str, retries: int = 3):
        if not self.api_key:
            raise RiotAuthError(
                401,
                "RIOT_API_KEY is not set. Add it to .env or update it with /setriotkey.",
            )

        session = await self._get_session()
        headers = {"X-Riot-Token": self.api_key}

        for attempt in range(retries):
            try:
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    elif resp.status == 429:
                        retry_after = int(resp.headers.get("Retry-After", 5))
                        logger.warning(f"Riot rate limited. Waiting {retry_after}s...")
                        await asyncio.sleep(retry_after)
                    elif resp.status in (401, 403):
                        raise RiotAuthError(
                            resp.status,
                            "Riot API key expired or invalid (dev keys expire every 24h). "
                            "Refresh it in .env or run /setriotkey.",
                        )
                    elif resp.status == 404:
                        raise RiotAPIError(404, f"Not found: {url}")
                    elif resp.status >= 500 and attempt < retries - 1:
                        logger.warning(f"Riot server error {resp.status} (attempt {attempt + 1})")
                        await asyncio.sleep(2 ** attempt)
                    else:
                        text = await resp.text()
                        raise RiotAPIError(resp.status, text)
            except RiotAPIError:
                raise
            except Exception as e:
                if attempt == retries - 1:
                    raise
                logger.warning(f"Request failed (attempt {attempt + 1}): {e}")
                await asyncio.sleep(2 ** attempt)
        raise RiotAPIError(429, "Max retries exceeded due to rate limiting")

    # -------------------------------------------------------------------------
    # Account endpoints
    # -------------------------------------------------------------------------

    async def get_account_by_riot_id(self, game_name: str, tag_line: str) -> dict:
        encoded_name = quote(game_name.strip(), safe="")
        encoded_tag = quote(tag_line.strip(), safe="")
        url = f"{BASE_ROUTING}/riot/account/v1/accounts/by-riot-id/{encoded_name}/{encoded_tag}"
        return await self._get(url)

    async def get_summoner_by_puuid(self, puuid: str) -> dict:
        url = f"{BASE_PLATFORM}/lol/summoner/v4/summoners/by-puuid/{quote(puuid, safe='')}"
        return await self._get(url)

    # -------------------------------------------------------------------------
    # Ranked endpoints
    # -------------------------------------------------------------------------

    async def get_ranked_stats_by_puuid(self, puuid: str) -> list:
        url = f"{BASE_PLATFORM}/lol/league/v4/entries/by-puuid/{quote(puuid, safe='')}"
        return await self._get(url)

    # -------------------------------------------------------------------------
    # Match endpoints
    # -------------------------------------------------------------------------

    async def get_match_ids(
        self,
        puuid: str,
        start_time: Optional[int] = None,
        count: int = 20,
        queue: Optional[int] = None,
    ) -> list[str]:
        all_ids: list[str] = []
        start = 0
        remaining = max(0, count)
        encoded_puuid = quote(puuid, safe="")

        while remaining > 0:
            page_size = min(100, remaining)
            url = (
                f"{BASE_ROUTING}/lol/match/v5/matches/by-puuid/{encoded_puuid}/ids"
                f"?start={start}&count={page_size}"
            )
            if start_time is not None:
                url += f"&startTime={int(start_time)}"
            if queue is not None:
                url += f"&queue={int(queue)}"

            page = await self._get(url)
            if not page:
                break
            all_ids.extend(page)
            if len(page) < page_size:
                break
            start += len(page)
            remaining -= len(page)

        return all_ids

    async def get_match(self, match_id: str) -> dict:
        url = f"{BASE_ROUTING}/lol/match/v5/matches/{quote(match_id, safe='')}"
        return await self._get(url)

    async def get_match_timeline(self, match_id: str) -> dict:
        url = f"{BASE_ROUTING}/lol/match/v5/matches/{quote(match_id, safe='')}/timeline"
        return await self._get(url)

    # -------------------------------------------------------------------------
    # Spectator (live game)
    # -------------------------------------------------------------------------

    async def get_active_game(self, puuid: str) -> Optional[dict]:
        url = f"{BASE_PLATFORM}/lol/spectator/v5/active-games/by-summoner/{quote(puuid, safe='')}"
        try:
            return await self._get(url)
        except RiotAPIError as e:
            if e.status == 404:
                return None
            raise

    # -------------------------------------------------------------------------
    # Champion mastery
    # -------------------------------------------------------------------------

    async def get_champion_masteries_top(self, puuid: str, count: int = 10) -> list:
        url = (
            f"{BASE_PLATFORM}/lol/champion-mastery/v4/champion-masteries/"
            f"by-puuid/{quote(puuid, safe='')}/top?count={int(count)}"
        )
        return await self._get(url)

    async def get_champion_mastery_for_champ(self, puuid: str, champion_id: int) -> Optional[dict]:
        url = (
            f"{BASE_PLATFORM}/lol/champion-mastery/v4/champion-masteries/"
            f"by-puuid/{quote(puuid, safe='')}/by-champion/{int(champion_id)}"
        )
        try:
            return await self._get(url)
        except RiotAPIError as e:
            if e.status == 404:
                return None
            raise

    # -------------------------------------------------------------------------
    # Health / status
    # -------------------------------------------------------------------------

    async def verify_key(self) -> tuple[bool, str]:
        if not self.api_key or self.api_key == "your_riot_api_key_here":
            return False, "RIOT_API_KEY is not set"
        url = f"{BASE_PLATFORM}/lol/status/v4/platform-data"
        try:
            data = await self._get(url, retries=1)
            name = data.get("name", REGION.upper())
            return True, f"Valid ({name})"
        except RiotAuthError as e:
            return False, str(e)
        except Exception as e:
            return False, str(e)

    # -------------------------------------------------------------------------
    # Data Dragon (champion & item metadata)
    # -------------------------------------------------------------------------

    async def get_ddragon_latest_version(self) -> str:
        now = time.time()
        if self._ddragon_version_cache:
            version, fetched_at = self._ddragon_version_cache
            if now - fetched_at < DDRAGON_CACHE_TTL_SECONDS:
                return version
        versions = await self._get_public(DDRAGON_VERSIONS_URL)
        version = versions[0]
        self._ddragon_version_cache = (version, now)
        return version

    async def get_ddragon_champions(self, version: str) -> dict:
        if version in self._ddragon_champion_cache:
            return self._ddragon_champion_cache[version]
        data = await self._get_public(DDRAGON_CHAMPION_URL.format(version=version))
        self._ddragon_champion_cache[version] = data
        return data

    async def get_ddragon_items(self, version: str) -> dict:
        if version in self._ddragon_item_cache:
            return self._ddragon_item_cache[version]
        data = await self._get_public(DDRAGON_ITEM_URL.format(version=version))
        self._ddragon_item_cache[version] = data
        return data

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        if self._public_session and not self._public_session.closed:
            await self._public_session.close()
