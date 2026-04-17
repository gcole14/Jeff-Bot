import asyncio
import json
import logging
import os
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


def resolve_riot_id(name_or_id: str) -> str:
    """If input contains '#', return as-is. Otherwise, match case-insensitively
    against the name portion of tracked SUMMONERS. Raises ValueError if no match."""
    if "#" in name_or_id:
        return name_or_id
    target = name_or_id.strip().lower()
    for full in SUMMONERS:
        name_part = full.split("#", 1)[0]
        if name_part.lower() == target:
            return full
    known = ", ".join(s.split("#")[0] for s in SUMMONERS)
    raise ValueError(f"Unknown player '{name_or_id}'. Known: {known} (or use full Name#TAG)")


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
        self.total_damage_to_champions: int = 0
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
        self._champion_id_map: dict[str, dict[int, str]] = {}  # version -> {id: name}

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
        level = summoner.get("summonerLevel", 0)
        icon = summoner.get("profileIconId", 0)

        self._account_cache[riot_id] = (puuid, level, icon)
        return self._account_cache[riot_id]

    async def _fetch_player_stats(self, riot_id: str, start_time_epoch: int) -> PlayerStats:
        ps = PlayerStats(riot_id)
        try:
            puuid, level, icon = await self._resolve_player(riot_id)
            ps.puuid = puuid
            ps.summoner_level = level
            ps.profile_icon_id = icon

            # Ranked stats
            ranked = await self.riot.get_ranked_stats_by_puuid(puuid)
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
                ps.total_damage_to_champions += player_data.get("totalDamageDealtToChampions", 0)
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

    async def fetch_rank_only(self, riot_id: str) -> dict:
        """Lightweight rank fetch (no match history). Used by rank tracker."""
        try:
            puuid, level, icon = await self._resolve_player(riot_id)
            ranked = await self.riot.get_ranked_stats_by_puuid(puuid)
            tier, rank, lp, wins, losses = "UNRANKED", "", 0, 0, 0
            for entry in ranked:
                if entry.get("queueType") == "RANKED_SOLO_5x5":
                    tier = entry.get("tier", "UNRANKED")
                    rank = entry.get("rank", "")
                    lp = entry.get("leaguePoints", 0)
                    wins = entry.get("wins", 0)
                    losses = entry.get("losses", 0)
            return {
                "riot_id": riot_id,
                "puuid": puuid,
                "tier": tier,
                "rank": rank,
                "lp": lp,
                "wins": wins,
                "losses": losses,
                "error": None,
            }
        except Exception as e:
            logger.warning(f"fetch_rank_only failed for {riot_id}: {e}")
            return {"riot_id": riot_id, "puuid": None, "error": str(e)}

    async def _get_champion_name_map(self) -> tuple[str, dict[int, str]]:
        """Returns (version, {championId: championName}) with caching."""
        version = await self.riot.get_ddragon_latest_version()
        if version in self._champion_id_map:
            return version, self._champion_id_map[version]
        data = await self.riot.get_ddragon_champions(version)
        mapping = {int(c["key"]): c["name"] for c in data.get("data", {}).values()}
        self._champion_id_map[version] = mapping
        return version, mapping

    async def get_champion_mastery(self, riot_id: str, count: int = 10) -> dict:
        """Returns top-N champion mastery info with names resolved via Data Dragon."""
        result = {
            "riot_id": riot_id,
            "display_name": riot_id.split("#")[0],
            "puuid": None,
            "version": None,
            "masteries": [],
            "error": None,
        }
        try:
            puuid, level, icon = await self._resolve_player(riot_id)
            result["puuid"] = puuid
            result["summoner_level"] = level
            result["profile_icon_id"] = icon

            masteries = await self.riot.get_champion_masteries_top(puuid, count=count)

            try:
                version, name_map = await self._get_champion_name_map()
                result["version"] = version
            except Exception as e:
                logger.warning(f"Could not load Data Dragon champion map: {e}")
                name_map = {}

            for m in masteries:
                cid = m.get("championId", 0)
                result["masteries"].append({
                    "champion_id": cid,
                    "name": name_map.get(cid, f"Champion {cid}"),
                    "level": m.get("championLevel", 0),
                    "points": m.get("championPoints", 0),
                })
        except RiotAPIError as e:
            result["error"] = str(e)
        except Exception as e:
            logger.exception(f"Mastery fetch failed for {riot_id}: {e}")
            result["error"] = str(e)
        return result


class RankTracker:
    """Tracks rank history across restarts and detects tier promotions."""

    def __init__(self, aggregator: StatsAggregator, history_path: str):
        self.aggregator = aggregator
        self.history_path = history_path
        self._history: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.history_path):
            return
        try:
            with open(self.history_path, "r") as f:
                self._history = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Could not load rank history ({e}); starting fresh.")
            self._history = {}

    def _save(self) -> None:
        try:
            tmp_path = f"{self.history_path}.tmp"
            with open(tmp_path, "w") as f:
                json.dump(self._history, f, indent=2)
            os.replace(tmp_path, self.history_path)
        except OSError as e:
            logger.error(f"Could not write rank history: {e}")

    async def check_promotions(self) -> list[dict]:
        """Fetches current rank for all summoners; returns list of promotion events."""
        promotions: list[dict] = []
        now_iso = datetime.now(timezone.utc).isoformat()

        for riot_id in SUMMONERS:
            snapshot = await self.aggregator.fetch_rank_only(riot_id)
            if snapshot.get("error") or not snapshot.get("puuid"):
                continue

            puuid = snapshot["puuid"]
            prev = self._history.get(puuid)
            new_tier = snapshot["tier"]

            if prev and prev.get("tier") != "UNRANKED" and new_tier != "UNRANKED":
                try:
                    old_idx = TIER_ORDER.index(prev["tier"])
                    new_idx = TIER_ORDER.index(new_tier)
                    if new_idx > old_idx:
                        promotions.append({
                            "riot_id": riot_id,
                            "display_name": riot_id.split("#")[0],
                            "puuid": puuid,
                            "old_tier": prev["tier"],
                            "old_rank": prev.get("rank", ""),
                            "old_lp": prev.get("lp", 0),
                            "new_tier": new_tier,
                            "new_rank": snapshot["rank"],
                            "new_lp": snapshot["lp"],
                        })
                except ValueError:
                    pass

            self._history[puuid] = {
                "riot_id": riot_id,
                "tier": new_tier,
                "rank": snapshot["rank"],
                "lp": snapshot["lp"],
                "wins": snapshot["wins"],
                "losses": snapshot["losses"],
                "updated_at": now_iso,
            }

        self._save()
        return promotions
