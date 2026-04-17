import asyncio
import logging
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from typing import Optional

from config import (
    SUMMONERS, DAILY_MATCH_LOOKBACK_HOURS, WEEKLY_MATCH_LOOKBACK_DAYS, MAX_MATCHES_PER_PLAYER
)
from riot_api import RiotAPI, RiotAPIError

logger = logging.getLogger(__name__)

TIER_ORDER = ["IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER"]
RANK_ORDER = ["IV", "III", "II", "I"]


def rank_score(tier: str, rank: str, lp: int) -> int:
    t = TIER_ORDER.index(tier) if tier in TIER_ORDER else 0
    r = RANK_ORDER.index(rank) if rank in RANK_ORDER else 0
    return t * 400 + r * 100 + lp


def format_rank(tier: str, rank: str, lp: int) -> str:
    if tier in ("MASTER", "GRANDMASTER", "CHALLENGER"):
        return f"{tier.capitalize()} {lp} LP"
    return f"{tier.capitalize()} {rank} — {lp} LP"


class PlayerStats:
    def __init__(self, riot_id: str):
        self.riot_id = riot_id
        self.display_name = riot_id.split("#")[0]

        # Account info
        self.puuid: Optional[str] = None
        self.summoner_level: int = 0
        self.profile_icon_id: int = 0

        # Ranked
        self.solo_tier: str = "UNRANKED"
        self.solo_rank: str = ""
        self.solo_lp: int = 0
        self.solo_wins: int = 0
        self.solo_losses: int = 0

        # Period stats (daily or weekly)
        self.games_played: int = 0
        self.wins: int = 0
        self.losses: int = 0
        self.total_kills: int = 0
        self.total_deaths: int = 0
        self.total_assists: int = 0
        self.total_game_duration_seconds: int = 0
        self.champion_counts: dict = defaultdict(int)
        self.lp_change: int = 0  # Estimated LP change over period

        # Error state
        self.error: Optional[str] = None

    @property
    def win_rate(self) -> float:
        if self.games_played == 0:
            return 0.0
        return (self.wins / self.games_played) * 100

    @property
    def kda(self) -> float:
        if self.total_deaths == 0:
            return float(self.total_kills + self.total_assists)
        return (self.total_kills + self.total_assists) / self.total_deaths

    @property
    def hours_played(self) -> float:
        return self.total_game_duration_seconds / 3600

    @property
    def top_champions(self) -> list:
        sorted_champs = sorted(self.champion_counts.items(), key=lambda x: x[1], reverse=True)
        return sorted_champs[:3]

    @property
    def overall_rank_score(self) -> int:
        return rank_score(self.solo_tier, self.solo_rank, self.solo_lp)

    def formatted_rank(self) -> str:
        if self.solo_tier == "UNRANKED":
            return "Unranked"
        return format_rank(self.solo_tier, self.solo_rank, self.solo_lp)

    def formatted_kda(self) -> str:
        return f"{self.total_kills}/{self.total_deaths}/{self.total_assists} ({self.kda:.2f})"


class StatsAggregator:
    def __init__(self, riot: RiotAPI):
        self.riot = riot
        # Cache PUUIDs and summoner IDs so we don't re-fetch every time
        self._account_cache: dict = {}

    async def _resolve_player(self, riot_id: str) -> tuple[str, str]:
        """Returns (puuid, summoner_id) for a Riot ID."""
        if riot_id in self._account_cache:
            return self._account_cache[riot_id]

        parts = riot_id.split("#")
        if len(parts) != 2:
            raise ValueError(f"Invalid Riot ID format: {riot_id} (expected Name#TAG)")
        game_name, tag_line = parts

        account = await self.riot.get_account_by_riot_id(game_name, tag_line)
        puuid = account["puuid"]
        summoner = await self.riot.get_summoner_by_puuid(puuid)
        summoner_id = summoner["id"]
        level = summoner.get("summonerLevel", 0)
        icon = summoner.get("profileIconId", 0)

        self._account_cache[riot_id] = (puuid, summoner_id, level, icon)
        return self._account_cache[riot_id]

    async def _fetch_player_stats(self, riot_id: str, start_time_epoch: int) -> PlayerStats:
        ps = PlayerStats(riot_id)
        try:
            puuid, summoner_id, level, icon = await self._resolve_player(riot_id)
            ps.puuid = puuid
            ps.summoner_level = level
            ps.profile_icon_id = icon

            # Ranked stats
            ranked = await self.riot.get_ranked_stats(summoner_id)
            for entry in ranked:
                if entry.get("queueType") == "RANKED_SOLO_5x5":
                    ps.solo_tier = entry.get("tier", "UNRANKED")
                    ps.solo_rank = entry.get("rank", "")
                    ps.solo_lp = entry.get("leaguePoints", 0)
                    ps.solo_wins = entry.get("wins", 0)
                    ps.solo_losses = entry.get("losses", 0)

            # Match history for the period
            match_ids = await self.riot.get_match_ids(
                puuid,
                start_time=start_time_epoch,
                count=MAX_MATCHES_PER_PLAYER
            )

            # Fetch match details concurrently (throttle to avoid rate limits)
            semaphore = asyncio.Semaphore(3)

            async def fetch_match(mid):
                async with semaphore:
                    try:
                        return await self.riot.get_match(mid)
                    except RiotAPIError as e:
                        logger.warning(f"Could not fetch match {mid}: {e}")
                        return None

            matches = await asyncio.gather(*[fetch_match(mid) for mid in match_ids])

            for match in matches:
                if match is None:
                    continue
                info = match.get("info", {})
                participants = info.get("participants", [])

                # Find this player in the match
                player_data = next((p for p in participants if p.get("puuid") == puuid), None)
                if not player_data:
                    continue

                ps.games_played += 1
                if player_data.get("win"):
                    ps.wins += 1
                else:
                    ps.losses += 1

                ps.total_kills += player_data.get("kills", 0)
                ps.total_deaths += player_data.get("deaths", 0)
                ps.total_assists += player_data.get("assists", 0)
                ps.total_game_duration_seconds += info.get("gameDuration", 0)
                ps.champion_counts[player_data.get("championName", "Unknown")] += 1

        except RiotAPIError as e:
            logger.error(f"Riot API error for {riot_id}: {e}")
            ps.error = str(e)
        except Exception as e:
            logger.exception(f"Unexpected error for {riot_id}: {e}")
            ps.error = str(e)

        return ps

    async def get_daily_stats(self) -> list[PlayerStats]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=DAILY_MATCH_LOOKBACK_HOURS)
        start_epoch = int(cutoff.timestamp())
        tasks = [self._fetch_player_stats(rid, start_epoch) for rid in SUMMONERS]
        return await asyncio.gather(*tasks)

    async def get_weekly_stats(self) -> list[PlayerStats]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=WEEKLY_MATCH_LOOKBACK_DAYS)
        start_epoch = int(cutoff.timestamp())
        tasks = [self._fetch_player_stats(rid, start_epoch) for rid in SUMMONERS]
        return await asyncio.gather(*tasks)

    async def get_player_snapshot(self, riot_id: str) -> PlayerStats:
        """Single player lookup — last 24h stats."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        return await self._fetch_player_stats(riot_id, int(cutoff.timestamp()))
