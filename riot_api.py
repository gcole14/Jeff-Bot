import aiohttp
import asyncio
import logging
import time
from typing import Optional
from config import RIOT_API_KEY, REGION, ROUTING, DDRAGON_VERSIONS_URL, DDRAGON_CHAMPION_URL

logger = logging.getLogger(__name__)

BASE_PLATFORM = f"https://{REGION}.api.riotgames.com"
BASE_ROUTING = f"https://{ROUTING}.api.riotgames.com"

DDRAGON_CACHE_TTL_SECONDS = 24 * 3600


class RiotAPIError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(f"Riot API {status}: {message}")


class RiotAPI:
    def __init__(self):
        self.headers = {"X-Riot-Token": RIOT_API_KEY}
        self._session: Optional[aiohttp.ClientSession] = None
        self._public_session: Optional[aiohttp.ClientSession] = None
        self._ddragon_version_cache: Optional[tuple[str, float]] = None
        self._ddragon_champion_cache: dict[str, dict] = {}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=self.headers)
        return self._session

    async def _get_public_session(self) -> aiohttp.ClientSession:
        if self._public_session is None or self._public_session.closed:
            self._public_session = aiohttp.ClientSession()
        return self._public_session

    async def _get_public(self, url: str):
        """Unauthenticated GET for Data Dragon CDN (no X-Riot-Token)."""
        session = await self._get_public_session()
        async with session.get(url) as resp:
            if resp.status != 200:
                raise RiotAPIError(resp.status, f"Data Dragon: {await resp.text()}")
            return await resp.json()

    async def _get(self, url: str, retries: int = 3) -> dict:
        session = await self._get_session()
        for attempt in range(retries):
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    elif resp.status == 429:
                        retry_after = int(resp.headers.get("Retry-After", 5))
                        logger.warning(f"Rate limited. Waiting {retry_after}s...")
                        await asyncio.sleep(retry_after)
                    elif resp.status == 404:
                        raise RiotAPIError(404, f"Not found: {url}")
                    else:
                        text = await resp.text()
                        raise RiotAPIError(resp.status, text)
            except RiotAPIError:
                raise
            except Exception as e:
                if attempt == retries - 1:
                    raise
                logger.warning(f"Request failed (attempt {attempt+1}): {e}")
                await asyncio.sleep(2 ** attempt)
        raise RiotAPIError(429, "Max retries exceeded due to rate limiting")

    # -------------------------------------------------------------------------
    # Account endpoints
    # -------------------------------------------------------------------------

    async def get_account_by_riot_id(self, game_name: str, tag_line: str) -> dict:
        """Returns puuid, gameName, tagLine."""
        url = f"{BASE_ROUTING}/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
        return await self._get(url)

    async def get_summoner_by_puuid(self, puuid: str) -> dict:
        """Returns summoner data including summonerId, profileIconId, summonerLevel."""
        url = f"{BASE_PLATFORM}/lol/summoner/v4/summoners/by-puuid/{puuid}"
        return await self._get(url)

    # -------------------------------------------------------------------------
    # Ranked endpoints
    # -------------------------------------------------------------------------

    async def get_ranked_stats(self, summoner_id: str) -> list:
        """Returns list of ranked queue entries (RANKED_SOLO_5x5, RANKED_FLEX_SR)."""
        url = f"{BASE_PLATFORM}/lol/league/v4/entries/by-summoner/{summoner_id}"
        return await self._get(url)

    async def get_ranked_stats_by_puuid(self, puuid: str) -> list:
        """Returns ranked entries using PUUID (newer accounts may not have summoner ID)."""
        url = f"{BASE_PLATFORM}/lol/league/v4/entries/by-puuid/{puuid}"
        return await self._get(url)

    # -------------------------------------------------------------------------
    # Match endpoints
    # -------------------------------------------------------------------------

    async def get_match_ids(self, puuid: str, start_time: int = None, count: int = 20, queue: int = None) -> list:
        """
        Get recent match IDs for a player.
        start_time: epoch seconds
        queue: 420 = ranked solo, 440 = ranked flex, None = all
        """
        url = f"{BASE_ROUTING}/lol/match/v5/matches/by-puuid/{puuid}/ids?count={count}"
        if start_time:
            url += f"&start={0}&startTime={start_time}"
        if queue:
            url += f"&queue={queue}"
        return await self._get(url)

    async def get_match(self, match_id: str) -> dict:
        """Get full match data."""
        url = f"{BASE_ROUTING}/lol/match/v5/matches/{match_id}"
        return await self._get(url)

    async def get_match_timeline(self, match_id: str) -> dict:
        """Get match timeline (optional, for detailed analysis)."""
        url = f"{BASE_ROUTING}/lol/match/v5/matches/{match_id}/timeline"
        return await self._get(url)

    # -------------------------------------------------------------------------
    # Champion mastery
    # -------------------------------------------------------------------------

    async def get_champion_masteries_top(self, puuid: str, count: int = 10) -> list:
        """Returns top N champion masteries for a player."""
        url = f"{BASE_PLATFORM}/lol/champion-mastery/v4/champion-masteries/by-puuid/{puuid}/top?count={count}"
        return await self._get(url)

    # -------------------------------------------------------------------------
    # Data Dragon (champion metadata)
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
        """Returns parsed champion.json payload (cached per version)."""
        if version in self._ddragon_champion_cache:
            return self._ddragon_champion_cache[version]
        data = await self._get_public(DDRAGON_CHAMPION_URL.format(version=version))
        self._ddragon_champion_cache[version] = data
        return data

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        if self._public_session and not self._public_session.closed:
            await self._public_session.close()
