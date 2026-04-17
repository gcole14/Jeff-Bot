import aiohttp
import asyncio
import logging
from typing import Optional
from config import RIOT_API_KEY, REGION, ROUTING

logger = logging.getLogger(__name__)

BASE_PLATFORM = f"https://{REGION}.api.riotgames.com"
BASE_ROUTING = f"https://{ROUTING}.api.riotgames.com"


class RiotAPIError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(f"Riot API {status}: {message}")


class RiotAPI:
    def __init__(self):
        self.headers = {"X-Riot-Token": RIOT_API_KEY}
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=self.headers)
        return self._session

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

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
