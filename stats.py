import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from itertools import combinations
from typing import Optional

from coaching import (
    TimelineReport,
    analyze_match_timeline,
    compute_champion_pool_audit,
    compute_win_loss_split,
)
from config import (
    SUMMONERS,
    DAILY_MATCH_LOOKBACK_HOURS,
    WEEKLY_MATCH_LOOKBACK_DAYS,
    MAX_MATCHES_PER_PLAYER,
    IGNORE_REMAKES,
    REMAKE_MAX_SECONDS,
    ANNOUNCE_DEMOTIONS,
)
from db import Database
from riot_api import RiotAPI, RiotAPIError

logger = logging.getLogger(__name__)

TIER_ORDER = [
    "IRON",
    "BRONZE",
    "SILVER",
    "GOLD",
    "PLATINUM",
    "EMERALD",
    "DIAMOND",
    "MASTER",
    "GRANDMASTER",
    "CHALLENGER",
]
RANK_ORDER = ["IV", "III", "II", "I"]
APEX_TIERS = {"MASTER", "GRANDMASTER", "CHALLENGER"}

QUEUE_NAMES = {
    400: "Normal Draft",
    420: "Ranked Solo",
    430: "Normal Blind",
    440: "Ranked Flex",
    450: "ARAM",
    480: "Swiftplay",
    490: "Quickplay",
    700: "Clash",
    720: "ARAM Clash",
    870: "Co-op vs AI",
    880: "Co-op vs AI",
    890: "Co-op vs AI",
    900: "ARURF",
    1400: "Ultimate Spellbook",
    1700: "Arena",
    1710: "Arena",
    1900: "URF",
    2300: "Brawl",
    2400: "ARAM: Mayhem",
}

ARAM_QUEUE_IDS = {450, 720, 2400}

SUMMONER_SPELLS = {
    1: "Cleanse",
    3: "Exhaust",
    4: "Flash",
    6: "Ghost",
    7: "Heal",
    11: "Smite",
    12: "Teleport",
    13: "Clarity",
    14: "Ignite",
    21: "Barrier",
    32: "Snowball",
}


def queue_name(queue_id: int) -> str:
    return QUEUE_NAMES.get(queue_id, f"Queue {queue_id}" if queue_id else "Unknown")


def _format_summoner_spells(spell1_id: int, spell2_id: int) -> str:
    """Returns compact non-Flash spell hint (e.g. 'Teleport', 'Smite', 'Ghost/Teleport')."""
    names = [SUMMONER_SPELLS.get(int(s), "") for s in (spell1_id, spell2_id)]
    names = [n for n in names if n]
    if not names:
        return ""
    non_flash = [n for n in names if n != "Flash"]
    shown = non_flash if non_flash else names
    return "/".join(shown)


def resolve_riot_id(name_or_id: str, roster: Optional[list[str]] = None) -> str:
    """If input contains '#', return trimmed Riot ID. Otherwise, match case-insensitively
    against the name portion of tracked summoners. Raises ValueError if no match."""
    cleaned = name_or_id.strip()
    if "#" in cleaned:
        game_name, tag_line = cleaned.split("#", 1)
        if not game_name.strip() or not tag_line.strip():
            raise ValueError(f"Invalid Riot ID '{name_or_id}'. Expected format: Name#TAG")
        return f"{game_name.strip()}#{tag_line.strip()}"

    candidates = roster if roster is not None else SUMMONERS
    target = cleaned.lower()
    for full in candidates:
        name_part = full.split("#", 1)[0]
        if name_part.lower() == target:
            return full
    known = ", ".join(s.split("#", 1)[0] for s in candidates) or "(none)"
    raise ValueError(f"Unknown player '{name_or_id}'. Known: {known} (or use full Name#TAG)")


def rank_score(tier: str, rank: str, lp: int) -> int:
    """Monotonic score for comparing/sorting overall rank (UNRANKED < IRON IV < ... < CHALLENGER)."""
    t_upper = (tier or "").upper()
    if t_upper not in TIER_ORDER:
        return -1
    if t_upper == "MASTER":
        return 2800 + lp
    if t_upper == "GRANDMASTER":
        return 5000 + lp
    if t_upper == "CHALLENGER":
        return 8000 + lp
    t = TIER_ORDER.index(t_upper)
    r = RANK_ORDER.index(rank) if rank in RANK_ORDER else 0
    return t * 400 + r * 100 + lp


def continuous_lp(tier: str, rank: str, lp: int) -> Optional[int]:
    t_upper = (tier or "").upper()
    if t_upper not in TIER_ORDER:
        return None
    if t_upper in APEX_TIERS:
        return 2800 + lp
    t = TIER_ORDER.index(t_upper)
    r = RANK_ORDER.index(rank) if rank in RANK_ORDER else 0
    return t * 400 + r * 100 + lp


def calc_lp_delta(
    old_tier: str, old_rank: str, old_lp: int, new_tier: str, new_rank: str, new_lp: int
) -> int:
    old_val = continuous_lp(old_tier, old_rank, old_lp)
    new_val = continuous_lp(new_tier, new_rank, new_lp)
    if old_val is None or new_val is None:
        return 0
    return new_val - old_val


def format_rank(tier: str, rank: str, lp: int) -> str:
    t_upper = (tier or "").upper()
    if not t_upper or t_upper == "UNRANKED":
        return "Unranked"
    if t_upper in APEX_TIERS:
        return f"{t_upper.capitalize()} {lp} LP"
    return f"{t_upper.capitalize()} {rank} — {lp} LP"


@dataclass
class MatchRecord:
    match_id: str
    game_creation: int
    duration_seconds: int
    queue_id: int
    queue_name: str
    champion: str
    win: bool
    kills: int
    deaths: int
    assists: int
    cs: int
    damage_to_champions: int
    damage_share: float
    kill_participation: float
    vision_score: int
    gold_earned: int
    control_wards: int = 0
    quadra_kills: int = 0
    penta_kills: int = 0
    team_id: int = 0
    role: str = ""
    teammate_puuids: list[str] = field(default_factory=list)
    enemy_champions: list[str] = field(default_factory=list)
    item_ids: list[int] = field(default_factory=list)

    @property
    def kda(self) -> float:
        if self.deaths == 0:
            return float(self.kills + self.assists)
        return (self.kills + self.assists) / self.deaths

    @property
    def cs_per_min(self) -> float:
        minutes = self.duration_seconds / 60
        return self.cs / minutes if minutes > 0 else 0.0


class PlayerStats:
    def __init__(self, riot_id: str):
        self.riot_id = riot_id
        self.display_name = riot_id.split("#", 1)[0]

        # Account info
        self.puuid: Optional[str] = None
        self.summoner_level: int = 0
        self.profile_icon_id: int = 0

        # Ranked Solo
        self.solo_tier: str = "UNRANKED"
        self.solo_rank: str = ""
        self.solo_lp: int = 0
        self.solo_wins: int = 0
        self.solo_losses: int = 0

        # Period stats
        self.games_played: int = 0
        self.wins: int = 0
        self.losses: int = 0
        self.total_kills: int = 0
        self.total_deaths: int = 0
        self.total_assists: int = 0
        self.total_cs: int = 0
        self.total_gold: int = 0
        self.total_vision_score: int = 0
        self.total_control_wards: int = 0
        self.total_damage_to_champions: int = 0
        self.total_game_duration_seconds: int = 0
        self.quadras: int = 0
        self.pentas: int = 0
        self.remakes_skipped: int = 0
        self.champion_counts: dict[str, int] = defaultdict(int)
        self.queue_counts: dict[str, int] = defaultdict(int)
        self.lp_change: int = 0
        self.matches: list[MatchRecord] = []

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
    def cs_per_min(self) -> float:
        minutes = self.total_game_duration_seconds / 60
        return self.total_cs / minutes if minutes > 0 else 0.0

    @property
    def gold_per_min(self) -> float:
        minutes = self.total_game_duration_seconds / 60
        return self.total_gold / minutes if minutes > 0 else 0.0

    @property
    def avg_kills(self) -> float:
        return self.total_kills / self.games_played if self.games_played else 0.0

    @property
    def avg_deaths(self) -> float:
        return self.total_deaths / self.games_played if self.games_played else 0.0

    @property
    def avg_assists(self) -> float:
        return self.total_assists / self.games_played if self.games_played else 0.0

    @property
    def avg_vision_score(self) -> float:
        return self.total_vision_score / self.games_played if self.games_played else 0.0

    @property
    def avg_control_wards(self) -> float:
        return self.total_control_wards / self.games_played if self.games_played else 0.0

    @property
    def avg_damage_per_game(self) -> float:
        return self.total_damage_to_champions / self.games_played if self.games_played else 0.0

    @property
    def avg_kill_participation(self) -> float:
        if not self.matches:
            return 0.0
        return sum(m.kill_participation for m in self.matches) / len(self.matches)

    @property
    def avg_damage_share(self) -> float:
        if not self.matches:
            return 0.0
        return sum(m.damage_share for m in self.matches) / len(self.matches)

    @property
    def win_streak(self) -> int:
        if not self.matches:
            return 0
        first_win = self.matches[0].win
        streak = 0
        for m in self.matches:
            if m.win == first_win:
                streak += 1
            else:
                break
        return streak if first_win else -streak

    def formatted_streak(self) -> str:
        s = self.win_streak
        if s >= 2:
            return f"🔥 {s}W"
        if s <= -2:
            return f"🥶 {abs(s)}L"
        return ""

    @property
    def top_champions(self) -> list[tuple[str, int]]:
        sorted_champs = sorted(self.champion_counts.items(), key=lambda x: x[1], reverse=True)
        return sorted_champs[:3]

    @property
    def top_champions_detailed(self) -> list[dict]:
        """Top 3 champions with games, wins, losses, and win rate."""
        buckets: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # [games, wins]
        for m in self.matches:
            buckets[m.champion][0] += 1
            if m.win:
                buckets[m.champion][1] += 1
        ranked = sorted(buckets.items(), key=lambda kv: (kv[1][0], kv[1][1]), reverse=True)
        return [
            {
                "name": cname,
                "games": g,
                "wins": w,
                "losses": g - w,
                "wr": (w / g * 100) if g else 0.0,
            }
            for cname, (g, w) in ranked[:3]
        ]

    @property
    def overall_rank_score(self) -> int:
        return rank_score(self.solo_tier, self.solo_rank, self.solo_lp)

    def formatted_rank(self) -> str:
        return format_rank(self.solo_tier, self.solo_rank, self.solo_lp)

    def formatted_kda(self) -> str:
        return f"{self.total_kills}/{self.total_deaths}/{self.total_assists} ({self.kda:.2f})"


class StatsAggregator:
    def __init__(self, riot: RiotAPI, db: Database):
        self.riot = riot
        self.db = db
        self._champion_id_map: dict[str, dict[int, str]] = {}
        self._item_id_map: dict[str, dict[int, tuple[str, int]]] = {}
        self._match_semaphore = asyncio.Semaphore(4)
        self._inflight_matches: dict[str, asyncio.Task] = {}
        self._inflight_timelines: dict[str, asyncio.Task] = {}

    def get_roster(self) -> list[str]:
        return self.db.get_roster()

    async def _resolve_player(self, riot_id: str) -> tuple[str, int, int]:
        """Returns (puuid, summoner_level, profile_icon_id) using SQLite cache."""
        cached = self.db.get_cached_account(riot_id)
        if cached:
            return cached["puuid"], cached["summoner_level"], cached["profile_icon_id"]

        parts = riot_id.split("#", 1)
        if len(parts) != 2:
            raise ValueError(f"Invalid Riot ID format: {riot_id} (expected Name#TAG)")
        game_name, tag_line = parts

        account = await self.riot.get_account_by_riot_id(game_name, tag_line)
        puuid = account["puuid"]
        summoner = await self.riot.get_summoner_by_puuid(puuid)
        level = int(summoner.get("summonerLevel", 0))
        icon = int(summoner.get("profileIconId", 0))

        self.db.upsert_account(riot_id, puuid, level, icon)
        return puuid, level, icon

    async def _fetch_match_cached(self, match_id: str) -> Optional[dict]:
        """Fetch a match from SQLite cache, deduplicating concurrent in-flight API calls."""
        cached = self.db.get_cached_match(match_id)
        if cached is not None:
            return cached

        existing_task = self._inflight_matches.get(match_id)
        if existing_task is not None:
            return await existing_task

        async def _load() -> Optional[dict]:
            async with self._match_semaphore:
                try:
                    payload = await self.riot.get_match(match_id)
                    if payload:
                        self.db.save_match(match_id, payload)
                    return payload
                except RiotAPIError as e:
                    logger.warning(f"Could not fetch match {match_id}: {e}")
                    return None

        task = asyncio.create_task(_load())
        self._inflight_matches[match_id] = task
        try:
            return await task
        finally:
            self._inflight_matches.pop(match_id, None)

    async def _fetch_timeline_cached(self, match_id: str) -> Optional[dict]:
        """Fetch a match timeline from SQLite cache with single-flight deduplication."""
        cached = self.db.get_cached_timeline(match_id)
        if cached is not None:
            return cached

        existing = self._inflight_timelines.get(match_id)
        if existing is not None:
            return await existing

        async def _load() -> Optional[dict]:
            async with self._match_semaphore:
                try:
                    payload = await self.riot.get_match_timeline(match_id)
                    if payload:
                        self.db.save_timeline(match_id, payload)
                    return payload
                except RiotAPIError as e:
                    logger.warning(f"Could not fetch timeline {match_id}: {e}")
                    return None

        task = asyncio.create_task(_load())
        self._inflight_timelines[match_id] = task
        try:
            return await task
        finally:
            self._inflight_timelines.pop(match_id, None)

    @staticmethod
    def _parse_match_record(match: dict, puuid: str) -> Optional[MatchRecord]:
        metadata = match.get("metadata", {})
        info = match.get("info", {})
        duration = int(info.get("gameDuration", 0))
        if IGNORE_REMAKES and duration < REMAKE_MAX_SECONDS:
            return None

        participants = info.get("participants", [])
        player_data = next((p for p in participants if p.get("puuid") == puuid), None)
        if not player_data:
            return None

        team_id = int(player_data.get("teamId", 0))
        teammates = [p for p in participants if int(p.get("teamId", -1)) == team_id]
        enemies = [p for p in participants if int(p.get("teamId", -1)) != team_id]
        team_kills = sum(int(p.get("kills", 0)) for p in teammates)
        team_dmg = sum(int(p.get("totalDamageDealtToChampions", 0)) for p in teammates)

        kills = int(player_data.get("kills", 0))
        deaths = int(player_data.get("deaths", 0))
        assists = int(player_data.get("assists", 0))
        dmg = int(player_data.get("totalDamageDealtToChampions", 0))
        cs = int(player_data.get("totalMinionsKilled", 0)) + int(
            player_data.get("neutralMinionsKilled", 0)
        )
        kp = ((kills + assists) / team_kills * 100) if team_kills > 0 else 0.0
        dmg_share = (dmg / team_dmg * 100) if team_dmg > 0 else 0.0
        qid = int(info.get("queueId", 0))
        items = [int(player_data.get(f"item{i}", 0) or 0) for i in range(6) if player_data.get(f"item{i}")]

        return MatchRecord(
            match_id=metadata.get("matchId", ""),
            game_creation=int(info.get("gameCreation", 0)),
            duration_seconds=duration,
            queue_id=qid,
            queue_name=queue_name(qid),
            champion=player_data.get("championName", "Unknown"),
            win=bool(player_data.get("win")),
            kills=kills,
            deaths=deaths,
            assists=assists,
            cs=cs,
            damage_to_champions=dmg,
            damage_share=dmg_share,
            kill_participation=kp,
            vision_score=int(player_data.get("visionScore", 0)),
            gold_earned=int(player_data.get("goldEarned", 0)),
            control_wards=int(player_data.get("visionWardsBoughtInGame", 0)),
            quadra_kills=int(player_data.get("quadraKills", 0)),
            penta_kills=int(player_data.get("pentaKills", 0)),
            team_id=team_id,
            role=player_data.get("teamPosition") or player_data.get("individualPosition") or "",
            teammate_puuids=[p.get("puuid", "") for p in teammates if p.get("puuid")],
            enemy_champions=[p.get("championName", "?") for p in enemies],
            item_ids=items,
        )

    async def _fetch_player_stats(
        self,
        riot_id: str,
        start_time_epoch: int,
        queue: Optional[int] = None,
        max_matches: int = MAX_MATCHES_PER_PLAYER,
    ) -> PlayerStats:
        ps = PlayerStats(riot_id)
        try:
            puuid, level, icon = await self._resolve_player(riot_id)
            ps.puuid = puuid
            ps.summoner_level = level
            ps.profile_icon_id = icon

            ranked = await self.riot.get_ranked_stats_by_puuid(puuid)
            for entry in ranked:
                if entry.get("queueType") == "RANKED_SOLO_5x5":
                    ps.solo_tier = entry.get("tier", "UNRANKED")
                    ps.solo_rank = entry.get("rank", "")
                    ps.solo_lp = int(entry.get("leaguePoints", 0))
                    ps.solo_wins = int(entry.get("wins", 0))
                    ps.solo_losses = int(entry.get("losses", 0))

            baseline = self.db.get_rank_at_or_after(puuid, start_time_epoch)
            if baseline and ps.solo_tier != "UNRANKED":
                ps.lp_change = calc_lp_delta(
                    baseline["tier"],
                    baseline["rank"],
                    baseline["lp"],
                    ps.solo_tier,
                    ps.solo_rank,
                    ps.solo_lp,
                )

            self.db.record_rank_snapshot(
                puuid=puuid,
                riot_id=riot_id,
                tier=ps.solo_tier,
                rank=ps.solo_rank,
                lp=ps.solo_lp,
                wins=ps.solo_wins,
                losses=ps.solo_losses,
            )

            match_ids = await self.riot.get_match_ids(
                puuid,
                start_time=start_time_epoch,
                count=max_matches,
                queue=queue,
            )

            matches = await asyncio.gather(*[self._fetch_match_cached(mid) for mid in match_ids])

            records: list[MatchRecord] = []
            for match in matches:
                if match is None:
                    continue
                duration = int(match.get("info", {}).get("gameDuration", 0))
                if IGNORE_REMAKES and duration < REMAKE_MAX_SECONDS:
                    ps.remakes_skipped += 1
                    continue
                rec = self._parse_match_record(match, puuid)
                if rec is None:
                    continue
                records.append(rec)

            records.sort(key=lambda r: r.game_creation, reverse=True)
            ps.matches = records

            for rec in records:
                ps.games_played += 1
                if rec.win:
                    ps.wins += 1
                else:
                    ps.losses += 1
                ps.total_kills += rec.kills
                ps.total_deaths += rec.deaths
                ps.total_assists += rec.assists
                ps.total_cs += rec.cs
                ps.total_gold += rec.gold_earned
                ps.total_vision_score += rec.vision_score
                ps.total_control_wards += rec.control_wards
                ps.total_damage_to_champions += rec.damage_to_champions
                ps.total_game_duration_seconds += rec.duration_seconds
                ps.quadras += rec.quadra_kills
                ps.pentas += rec.penta_kills
                ps.champion_counts[rec.champion] += 1
                ps.queue_counts[rec.queue_name] += 1

        except RiotAPIError as e:
            logger.error(f"Riot API error for {riot_id}: {e}")
            ps.error = str(e)
        except Exception as e:
            logger.exception(f"Unexpected error for {riot_id}: {e}")
            ps.error = str(e)

        return ps

    async def get_daily_stats(self, queue: Optional[int] = None) -> list[PlayerStats]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=DAILY_MATCH_LOOKBACK_HOURS)
        start_epoch = int(cutoff.timestamp())
        roster = self.get_roster()
        return await asyncio.gather(
            *[self._fetch_player_stats(rid, start_epoch, queue=queue) for rid in roster]
        )

    async def get_weekly_stats(self, queue: Optional[int] = None) -> list[PlayerStats]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=WEEKLY_MATCH_LOOKBACK_DAYS)
        start_epoch = int(cutoff.timestamp())
        roster = self.get_roster()
        return await asyncio.gather(
            *[self._fetch_player_stats(rid, start_epoch, queue=queue) for rid in roster]
        )

    async def get_player_snapshot(
        self, riot_id: str, days: int = 1, queue: Optional[int] = None
    ) -> PlayerStats:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        return await self._fetch_player_stats(riot_id, int(cutoff.timestamp()), queue=queue)

    async def get_last_game(self, riot_id: str) -> dict:
        result: dict = {
            "riot_id": riot_id,
            "display_name": riot_id.split("#", 1)[0],
            "match": None,
            "friends_in_game": [],
            "error": None,
        }
        try:
            puuid, _, _ = await self._resolve_player(riot_id)
            match_ids = await self.riot.get_match_ids(puuid, count=5)
            puuid_to_name = self.db.get_puuid_to_name_map()

            for mid in match_ids:
                payload = await self._fetch_match_cached(mid)
                if not payload:
                    continue
                rec = self._parse_match_record(payload, puuid)
                if rec is None:
                    continue
                friends = [
                    puuid_to_name[p]
                    for p in rec.teammate_puuids
                    if p != puuid and p in puuid_to_name
                ]
                result["match"] = rec
                result["friends_in_game"] = friends
                return result
        except RiotAPIError as e:
            result["error"] = str(e)
        except Exception as e:
            logger.exception(f"Last game lookup failed for {riot_id}: {e}")
            result["error"] = str(e)
        return result

    # -------------------------------------------------------------------------
    # Timeline VOD Review (/review)
    # -------------------------------------------------------------------------

    async def get_match_review(self, riot_id: str, match_offset: int = 0) -> tuple[Optional[TimelineReport], Optional[str]]:
        """Fetches match + timeline for a player's Nth most recent non-remake game."""
        try:
            puuid, _, _ = await self._resolve_player(riot_id)
            match_ids = await self.riot.get_match_ids(puuid, count=max(8, match_offset + 5))
            if not match_ids:
                return None, "No recent matches found."

            chosen_payload = None
            chosen_id = None
            seen_valid = 0
            for mid in match_ids:
                payload = await self._fetch_match_cached(mid)
                if not payload:
                    continue
                dur = int(payload.get("info", {}).get("gameDuration", 0))
                if IGNORE_REMAKES and dur < REMAKE_MAX_SECONDS:
                    continue
                if seen_valid == match_offset:
                    chosen_payload = payload
                    chosen_id = mid
                    break
                seen_valid += 1

            if not chosen_payload or not chosen_id:
                return None, "Could not find a non-remake match at that index."

            timeline, item_map = await asyncio.gather(
                self._fetch_timeline_cached(chosen_id),
                self._get_item_name_map(),
            )
            qid = int(chosen_payload.get("info", {}).get("queueId", 0))
            report = analyze_match_timeline(
                match_payload=chosen_payload,
                timeline_payload=timeline,
                puuid=puuid,
                display_name=riot_id.split("#", 1)[0],
                item_map=item_map,
                queue_label=queue_name(qid),
                puuid_to_name=self.db.get_puuid_to_name_map(),
            )
            return report, None
        except RiotAPIError as e:
            return None, str(e)
        except Exception as e:
            logger.exception(f"Match review failed for {riot_id}: {e}")
            return None, str(e)

    # -------------------------------------------------------------------------
    # Loading-Screen Scout (/scout)
    # -------------------------------------------------------------------------

    async def get_live_scout_data(self, riot_id: str) -> dict:
        """Pulls all 10 players in a live game with rank, mastery on current champ, and recent streak."""
        display = riot_id.split("#", 1)[0]
        result: dict = {
            "display_name": display,
            "in_game": False,
            "queue_name": "",
            "length_seconds": 0,
            "player_champ": "",
            "allies": [],
            "enemies": [],
            "error": None,
        }
        try:
            puuid, _, _ = await self._resolve_player(riot_id)
            game = await self.riot.get_active_game(puuid)
            if not game:
                return result

            result["in_game"] = True
            result["queue_name"] = queue_name(int(game.get("gameQueueConfigId", 0)))
            result["length_seconds"] = int(game.get("gameLength", 0))

            try:
                _, champ_map = await self._get_champion_name_map()
            except Exception:
                champ_map = {}

            participants = game.get("participants", [])
            me = next((p for p in participants if p.get("puuid") == puuid), None)
            my_team_id = int(me.get("teamId", 100)) if me else 100
            if me:
                result["player_champ"] = champ_map.get(int(me.get("championId", 0)), "Unknown")

            scout_sem = asyncio.Semaphore(3)

            async def _scout_participant(p: dict) -> dict:
                p_puuid = p.get("puuid", "")
                cid = int(p.get("championId", 0))
                cname = champ_map.get(cid, f"Champ {cid}")
                pname = p.get("riotId") or p.get("summonerName") or "Player"
                pname = pname.split("#", 1)[0]
                info = {
                    "name": pname,
                    "champion": cname,
                    "team_id": int(p.get("teamId", 0)),
                    "is_target": p_puuid == puuid,
                    "rank": "Unranked",
                    "solo_wr": None,
                    "mastery_pts": 0,
                    "recent_form": "",
                    "streak": 0,
                }
                if not p_puuid:
                    return info

                async with scout_sem:
                    try:
                        ranked = await self.riot.get_ranked_stats_by_puuid(p_puuid)
                        for entry in ranked:
                            if entry.get("queueType") == "RANKED_SOLO_5x5":
                                tier = entry.get("tier", "UNRANKED")
                                rank = entry.get("rank", "")
                                lp = int(entry.get("leaguePoints", 0))
                                w = int(entry.get("wins", 0))
                                l = int(entry.get("losses", 0))
                                info["rank"] = format_rank(tier, rank, lp)
                                if w + l > 0:
                                    info["solo_wr"] = round(w / (w + l) * 100)
                                break
                    except Exception:
                        pass

                    try:
                        mastery = await self.riot.get_champion_mastery_for_champ(p_puuid, cid)
                        if mastery:
                            info["mastery_pts"] = int(mastery.get("championPoints", 0))
                    except Exception:
                        pass

                    try:
                        recent_ids = await self.riot.get_match_ids(p_puuid, count=5)
                        form_chars = []
                        for mid in recent_ids[:5]:
                            m = await self._fetch_match_cached(mid)
                            if not m:
                                continue
                            rec = self._parse_match_record(m, p_puuid)
                            if rec is None:
                                continue
                            form_chars.append("W" if rec.win else "L")
                        info["recent_form"] = "".join(form_chars)
                        if form_chars:
                            first = form_chars[0]
                            st = 0
                            for ch in form_chars:
                                if ch == first:
                                    st += 1
                                else:
                                    break
                            info["streak"] = st if first == "W" else -st
                    except Exception:
                        pass

                return info

            scouted = await asyncio.gather(*[_scout_participant(p) for p in participants])
            result["allies"] = [s for s in scouted if s["team_id"] == my_team_id]
            result["enemies"] = [s for s in scouted if s["team_id"] != my_team_id]
        except RiotAPIError as e:
            result["error"] = str(e)
        except Exception as e:
            logger.exception(f"Live scout failed for {riot_id}: {e}")
            result["error"] = str(e)
        return result

    # -------------------------------------------------------------------------
    # Auto Squad-Game Debrief
    # -------------------------------------------------------------------------

    def _build_squad_debrief(self, payload: dict, roster_puuids: dict[str, str]) -> Optional[dict]:
        """If 2+ tracked friends played on the same team in `payload`, returns a structured debrief dict."""
        metadata = payload.get("metadata", {})
        info = payload.get("info", {})
        duration = int(info.get("gameDuration", 0))
        if IGNORE_REMAKES and duration < REMAKE_MAX_SECONDS:
            return None

        participants = info.get("participants", [])
        team_friends: dict[int, list[tuple[str, MatchRecord]]] = defaultdict(list)

        for p in participants:
            p_puuid = p.get("puuid", "")
            if p_puuid in roster_puuids:
                rec = self._parse_match_record(payload, p_puuid)
                if rec:
                    team_friends[rec.team_id].append((roster_puuids[p_puuid], rec))

        # Pick largest squad team with >= 2 tracked friends
        best_team = None
        for _, members in team_friends.items():
            if len(members) >= 2 and (best_team is None or len(members) > len(best_team)):
                best_team = members
        if not best_team:
            return None

        # Score each friend to pick Squad MVP
        def _mvp_score(item: tuple[str, MatchRecord]) -> float:
            _, r = item
            return r.kda * 1.5 + r.damage_share * 0.2 + r.kill_participation * 0.1 + r.cs_per_min * 0.5

        mvp_name, _ = max(best_team, key=_mvp_score)
        most_deaths_name, most_deaths_rec = max(best_team, key=lambda item: item[1].deaths)
        first_rec = best_team[0][1]

        return {
            "match_id": metadata.get("matchId", ""),
            "game_creation": first_rec.game_creation,
            "duration_seconds": duration,
            "queue_name": first_rec.queue_name,
            "win": first_rec.win,
            "members": [{"display_name": name, "record": rec} for name, rec in best_team],
            "mvp": mvp_name,
            "most_deaths": (most_deaths_name, most_deaths_rec.deaths),
        }

    async def find_squad_games(self, only_unannounced: bool = True, lookback_per_player: int = 3) -> list[dict]:
        """Checks recent matches across the roster for games where 2+ friends played together."""
        roster = self.get_roster()
        roster_puuids: dict[str, str] = {}
        match_id_counts: dict[str, int] = defaultdict(int)

        for rid in roster:
            try:
                puuid, _, _ = await self._resolve_player(rid)
                roster_puuids[puuid] = rid.split("#", 1)[0]
                mids = await self.riot.get_match_ids(puuid, count=lookback_per_player)
                for mid in mids:
                    match_id_counts[mid] += 1
            except Exception as e:
                logger.warning(f"Squad check skipped {rid}: {e}")

        candidate_ids = [mid for mid, cnt in match_id_counts.items() if cnt >= 2]
        debriefs = []
        for mid in candidate_ids:
            if only_unannounced and self.db.is_match_announced(mid):
                continue
            payload = await self._fetch_match_cached(mid)
            if not payload:
                continue
            debrief = self._build_squad_debrief(payload, roster_puuids)
            if debrief:
                debriefs.append(debrief)

        debriefs.sort(key=lambda d: d["game_creation"])
        return debriefs

    async def seed_announced_squad_games(self) -> int:
        """Marks existing recent squad games as announced on first boot so we don't spam old games."""
        if self.db.count_announced_matches() > 0:
            return 0
        existing = await self.find_squad_games(only_unannounced=False, lookback_per_player=5)
        for d in existing:
            self.db.mark_match_announced(d["match_id"])
        return len(existing)

    # -------------------------------------------------------------------------
    # Squad Synergy, Live Games, Ask & Comp Context
    # -------------------------------------------------------------------------

    def compute_synergy(self, stats: list[PlayerStats]) -> dict:
        team_buckets: dict[tuple[str, int], list[tuple[str, bool]]] = defaultdict(list)
        seen_player_match: set[tuple[str, str]] = set()

        for ps in stats:
            if ps.error:
                continue
            for m in ps.matches:
                key = (ps.display_name, m.match_id)
                if key in seen_player_match or not m.match_id:
                    continue
                seen_player_match.add(key)
                team_buckets[(m.match_id, m.team_id)].append((ps.display_name, m.win))

        together_wins = together_losses = solo_wins = solo_losses = 0
        duo_records: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
        stack_records: dict[int, list[int]] = defaultdict(lambda: [0, 0])

        for (_, _), members in team_buckets.items():
            size = len(members)
            win = members[0][1]
            if size >= 2:
                if win:
                    together_wins += 1
                    stack_records[size][0] += 1
                else:
                    together_losses += 1
                    stack_records[size][1] += 1

                names = sorted(name for name, _ in members)
                for pair in combinations(names, 2):
                    if win:
                        duo_records[pair][0] += 1
                    else:
                        duo_records[pair][1] += 1
            else:
                if win:
                    solo_wins += 1
                else:
                    solo_losses += 1

        together_games = together_wins + together_losses
        solo_games = solo_wins + solo_losses

        duos = []
        for (a, b), (w, l) in duo_records.items():
            g = w + l
            duos.append({
                "pair": f"{a} + {b}",
                "wins": w,
                "losses": l,
                "games": g,
                "wr": (w / g * 100) if g else 0.0,
            })
        duos.sort(key=lambda d: (d["games"], d["wr"]), reverse=True)

        return {
            "together_games": together_games,
            "together_wins": together_wins,
            "together_losses": together_losses,
            "together_wr": (together_wins / together_games * 100) if together_games else 0.0,
            "solo_games": solo_games,
            "solo_wins": solo_wins,
            "solo_losses": solo_losses,
            "solo_wr": (solo_wins / solo_games * 100) if solo_games else 0.0,
            "duos": duos,
            "stacks": dict(sorted(stack_records.items())),
        }

    async def get_synergy_stats(self, days: int = WEEKLY_MATCH_LOOKBACK_DAYS) -> dict:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        start_epoch = int(cutoff.timestamp())
        roster = self.get_roster()
        stats = await asyncio.gather(*[self._fetch_player_stats(rid, start_epoch) for rid in roster])
        data = self.compute_synergy(stats)
        data["days"] = days
        data["profile_icons"] = {ps.display_name: ps.profile_icon_id for ps in stats}
        return data

    async def get_live_games(self) -> list[dict]:
        roster = self.get_roster()
        try:
            _, champ_map = await self._get_champion_name_map()
        except Exception:
            champ_map = {}

        async def check_one(rid: str):
            try:
                puuid, _, _ = await self._resolve_player(rid)
                game = await self.riot.get_active_game(puuid)
                return rid, puuid, game, None
            except RiotAPIError as e:
                return rid, None, None, str(e)
            except Exception as e:
                return rid, None, None, str(e)

        results = await asyncio.gather(*[check_one(rid) for rid in roster])

        games_by_id: dict[int, dict] = {}
        for rid, puuid, game, err in results:
            if err:
                logger.warning(f"Live game check error for {rid}: {err}")
                continue
            if not game:
                continue
            game_id = int(game.get("gameId", 0))
            qid = int(game.get("gameQueueConfigId", 0))
            length = int(game.get("gameLength", 0))

            champ_name = "Unknown"
            for p in game.get("participants", []):
                if p.get("puuid") == puuid:
                    cid = int(p.get("championId", 0))
                    champ_name = champ_map.get(cid, f"Champ {cid}")
                    break

            if game_id not in games_by_id:
                games_by_id[game_id] = {
                    "game_id": game_id,
                    "queue_name": queue_name(qid),
                    "length_seconds": length,
                    "players": [],
                }
            games_by_id[game_id]["players"].append({
                "display_name": rid.split("#", 1)[0],
                "champion": champ_name,
            })

        return list(games_by_id.values())

    async def build_squad_context(self, days: int = 14) -> str:
        """Builds a compact, grounded text summary of the squad's recent stats for /ask & /comp."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        start_epoch = int(cutoff.timestamp())
        roster = self.get_roster()
        stats = await asyncio.gather(*[self._fetch_player_stats(rid, start_epoch) for rid in roster])
        synergy = self.compute_synergy(stats)

        lines = [f"Squad 14-day stats ({len(roster)} players):"]
        for ps in stats:
            if ps.error:
                lines.append(f"- {ps.display_name}: unavailable ({ps.error})")
                continue
            pool = compute_champion_pool_audit(ps)
            split = compute_win_loss_split(ps)
            top_c = ", ".join(
                f"{c['champion']} ({c['wins']}W-{c['losses']}L, {c['kda']:.1f} KDA)"
                for c in pool["all_champs"][:5]
            ) or "none"
            roles = ", ".join(f"{r['role']} {r['games']}g ({r['wr']:.0f}%)" for r in pool["roles"][:3]) or "none"
            lines.append(
                f"- {ps.display_name} [{ps.formatted_rank()}, LP delta {ps.lp_change:+d}]: "
                f"{ps.wins}W-{ps.losses}L ({ps.win_rate:.0f}% WR), KDA {ps.formatted_kda()}, "
                f"{ps.cs_per_min:.1f} CS/m, {ps.avg_vision_score:.1f} vis, "
                f"{ps.avg_control_wards:.1f} pinks, {ps.avg_damage_share:.0f}% dmg share, "
                f"{ps.avg_kill_participation:.0f}% KP | Roles: {roles} | Champs: {top_c}"
            )
            if split["wins"] and split["losses"]:
                w, l = split["wins"], split["losses"]
                lines.append(
                    f"  Wins vs Losses: W({w['avg_deaths']:.1f} deaths, {w['cs_per_min']:.1f} CS/m, {w['avg_kp']:.0f}% KP) "
                    f"vs L({l['avg_deaths']:.1f} deaths, {l['cs_per_min']:.1f} CS/m, {l['avg_kp']:.0f}% KP)"
                )

        if synergy["together_games"]:
            lines.append(
                f"Squad together: {synergy['together_wins']}W-{synergy['together_losses']}L "
                f"({synergy['together_wr']:.0f}% WR) vs Solo {synergy['solo_wins']}W-{synergy['solo_losses']}L "
                f"({synergy['solo_wr']:.0f}% WR)"
            )
            for d in synergy["duos"][:6]:
                lines.append(f"  Duo {d['pair']}: {d['wins']}W-{d['losses']}L ({d['wr']:.0f}% WR)")

        return "\n".join(lines)

    async def get_current_patch(self) -> str:
        """Returns the current League patch (e.g. '15.23') from cached Data Dragon versions."""
        try:
            ver = await self.riot.get_ddragon_latest_version()
            parts = ver.split(".")
            return ".".join(parts[:2]) if len(parts) >= 2 else ver
        except Exception as e:
            logger.debug(f"Could not fetch current patch version: {e}")
            return ""

    async def build_live_match_context(
        self, speaker_riot_id: Optional[str] = None
    ) -> tuple[str, list[str]]:
        """
        Builds a compact summary of the speaker's (or squad's) currently active live match
        for voice & /ask Q&A. Returns (context_text, live_champion_names).
        """
        try:
            version, champ_map = await self._get_champion_name_map()
            patch = ".".join(version.split(".")[:2]) if version else ""
        except Exception:
            champ_map = {}
            patch = ""

        squad_puuids = self.db.get_puuid_to_name_map() if self.db else {}

        game: Optional[dict] = None
        target_puuid: Optional[str] = None
        target_display: Optional[str] = None
        checked_puuids: set[str] = set()

        # 1. Check speaker's own live match first (fast single Spectator-v5 call)
        if speaker_riot_id:
            try:
                puuid, _, _ = await self._resolve_player(speaker_riot_id)
                checked_puuids.add(puuid)
                active = await self.riot.get_active_game(puuid)
                if active:
                    game = active
                    target_puuid = puuid
                    target_display = speaker_riot_id.split("#", 1)[0]
                    squad_puuids.setdefault(puuid, target_display)
            except Exception as e:
                logger.debug(f"Live match lookup skipped for {speaker_riot_id}: {e}")

        # 2. Fallback: check tracked squad roster in parallel (covers unlinked speakers or spectating friends)
        if game is None:
            roster = self.get_roster()

            async def _check_roster_member(rid: str):
                try:
                    puuid, _, _ = await self._resolve_player(rid)
                    if puuid in checked_puuids:
                        return None
                    active = await self.riot.get_active_game(puuid)
                    if active:
                        return rid, puuid, active
                except Exception:
                    pass
                return None

            found = await asyncio.gather(*[_check_roster_member(rid) for rid in roster])
            for item in found:
                if item is not None:
                    rid, puuid, active = item
                    game = active
                    target_puuid = puuid
                    squad_puuids.setdefault(puuid, rid.split("#", 1)[0])
                    break

        if not game:
            return "", []

        participants = game.get("participants", [])
        if not participants:
            return "", []

        qid = int(game.get("gameQueueConfigId", 0))
        q_label = queue_name(qid)
        length_s = max(0, int(game.get("gameLength", 0)))
        mins = length_s // 60

        anchor = next((p for p in participants if p.get("puuid") == target_puuid), participants[0])
        ally_team_id = int(anchor.get("teamId", 100))

        allies: list[str] = []
        enemies: list[str] = []
        live_champs: list[str] = []
        squad_in_game: list[str] = []
        speaker_champ = ""
        speaker_spells = ""

        for p in participants:
            cid = int(p.get("championId", 0))
            cname = champ_map.get(cid, f"Champ {cid}")
            if cname not in live_champs:
                live_champs.append(cname)

            spells = _format_summoner_spells(p.get("spell1Id", 0), p.get("spell2Id", 0))
            spell_str = f" ({spells})" if spells else ""

            p_puuid = p.get("puuid", "")
            tag = ""
            if target_display and p_puuid == target_puuid:
                tag = f" [ASKER: {target_display}]"
                speaker_champ = cname
                speaker_spells = spells
            elif p_puuid and p_puuid in squad_puuids:
                fname = squad_puuids[p_puuid]
                tag = f" [FRIEND: {fname}]"
                squad_in_game.append(f"{fname} on {cname}{spell_str}")

            entry = f"{cname}{spell_str}{tag}"
            if int(p.get("teamId", -1)) == ally_team_id:
                allies.append(entry)
            else:
                enemies.append(entry)

        header_bits = [q_label]
        if mins > 0:
            header_bits.append(f"{mins}m in")
        else:
            header_bits.append("loading/just started")
        if patch:
            header_bits.append(f"Patch {patch}")

        lines = [f"Live match ({', '.join(header_bits)}):"]
        if target_display and speaker_champ:
            sp = f" with {speaker_spells}" if speaker_spells else ""
            lines.append(f"{target_display} is currently playing {speaker_champ}{sp}.")
        elif squad_in_game:
            lines.append("Squad members in this match: " + ", ".join(squad_in_game) + ".")
        lines.append("Allies: " + ", ".join(allies))
        lines.append("Enemies: " + ", ".join(enemies))
        return "\n".join(lines), live_champs

    async def fetch_rank_only(self, riot_id: str) -> dict:
        try:
            puuid, _, icon = await self._resolve_player(riot_id)
            ranked = await self.riot.get_ranked_stats_by_puuid(puuid)
            tier, rank, lp, wins, losses = "UNRANKED", "", 0, 0, 0
            for entry in ranked:
                if entry.get("queueType") == "RANKED_SOLO_5x5":
                    tier = entry.get("tier", "UNRANKED")
                    rank = entry.get("rank", "")
                    lp = int(entry.get("leaguePoints", 0))
                    wins = int(entry.get("wins", 0))
                    losses = int(entry.get("losses", 0))
            return {
                "riot_id": riot_id,
                "puuid": puuid,
                "profile_icon_id": icon,
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
        version = await self.riot.get_ddragon_latest_version()
        if version in self._champion_id_map:
            return version, self._champion_id_map[version]
        data = await self.riot.get_ddragon_champions(version)
        mapping = {int(c["key"]): c["name"] for c in data.get("data", {}).values()}
        self._champion_id_map[version] = mapping
        return version, mapping

    async def _get_item_name_map(self) -> dict[int, tuple[str, int]]:
        """Returns {item_id: (item_name, total_gold)} cached by Data Dragon patch."""
        try:
            version = await self.riot.get_ddragon_latest_version()
            if version in self._item_id_map:
                return self._item_id_map[version]
            data = await self.riot.get_ddragon_items(version)
            mapping = {
                int(iid): (item.get("name", f"Item {iid}"), int(item.get("gold", {}).get("total", 0)))
                for iid, item in data.get("data", {}).items()
                if iid.isdigit()
            }
            self._item_id_map[version] = mapping
            return mapping
        except Exception as e:
            logger.warning(f"Could not load Data Dragon items: {e}")
            return {}

    async def resolve_item_ids_by_names(self, item_names: list[str]) -> tuple[str, dict[str, Optional[int]]]:
        """Resolves English item names to Summoner's Rift Data Dragon item IDs."""
        try:
            version = await self.riot.get_ddragon_latest_version()
            raw_items = (await self.riot.get_ddragon_items(version)).get("data", {})
        except Exception:
            version = ""
            raw_items = {}

        def _norm(s: str) -> str:
            return "".join(ch for ch in s.lower() if ch.isalnum())

        # Prefer Summoner's Rift (map 11) purchasable items with standard IDs (< 10000)
        by_norm: dict[str, tuple[int, int]] = {}
        for iid_str, item in raw_items.items():
            if not iid_str.isdigit():
                continue
            iid = int(iid_str)
            if iid >= 10000:
                continue
            maps = item.get("maps") or {}
            in_sr = 1 if maps.get("11", True) else 0
            purch = 1 if item.get("gold", {}).get("purchasable", True) else 0
            gold = int(item.get("gold", {}).get("total", 0))
            score = in_sr * 100_000 + purch * 10_000 + gold
            k = _norm(item.get("name", ""))
            prev = by_norm.get(k)
            if prev is None or score > prev[1]:
                by_norm[k] = (iid, score)

        resolved: dict[str, Optional[int]] = {}
        for name in item_names:
            if not name:
                continue
            target = _norm(name)
            if target in by_norm:
                resolved[name] = by_norm[target][0]
                continue
            match_id = None
            for k, (iid, _) in by_norm.items():
                if target in k or k in target:
                    match_id = iid
                    break
            resolved[name] = match_id
        return version, resolved

    async def get_champion_mastery(self, riot_id: str, count: int = 10) -> dict:
        result: dict = {
            "riot_id": riot_id,
            "display_name": riot_id.split("#", 1)[0],
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
                cid = int(m.get("championId", 0))
                result["masteries"].append({
                    "champion_id": cid,
                    "name": name_map.get(cid, f"Champion {cid}"),
                    "level": int(m.get("championLevel", 0)),
                    "points": int(m.get("championPoints", 0)),
                })
        except RiotAPIError as e:
            result["error"] = str(e)
        except Exception as e:
            logger.exception(f"Mastery fetch failed for {riot_id}: {e}")
            result["error"] = str(e)
        return result


class RankTracker:
    """Tracks rank history in SQLite and detects tier promotions & demotions."""

    def __init__(self, aggregator: StatsAggregator, db: Database):
        self.aggregator = aggregator
        self.db = db

    async def check_rank_changes(self) -> list[dict]:
        events: list[dict] = []

        for riot_id in self.aggregator.get_roster():
            snapshot = await self.aggregator.fetch_rank_only(riot_id)
            if snapshot.get("error") or not snapshot.get("puuid"):
                continue

            puuid = snapshot["puuid"]
            prev = self.db.get_latest_rank(puuid)
            new_tier = snapshot["tier"]

            if prev and prev.get("tier") != "UNRANKED" and new_tier != "UNRANKED":
                try:
                    old_idx = TIER_ORDER.index(prev["tier"])
                    new_idx = TIER_ORDER.index(new_tier)
                    if new_idx > old_idx:
                        events.append({
                            "direction": "promotion",
                            "riot_id": riot_id,
                            "display_name": riot_id.split("#", 1)[0],
                            "puuid": puuid,
                            "profile_icon_id": snapshot.get("profile_icon_id", 0),
                            "old_tier": prev["tier"],
                            "old_rank": prev.get("rank", ""),
                            "old_lp": prev.get("lp", 0),
                            "new_tier": new_tier,
                            "new_rank": snapshot["rank"],
                            "new_lp": snapshot["lp"],
                        })
                    elif new_idx < old_idx and ANNOUNCE_DEMOTIONS:
                        events.append({
                            "direction": "demotion",
                            "riot_id": riot_id,
                            "display_name": riot_id.split("#", 1)[0],
                            "puuid": puuid,
                            "profile_icon_id": snapshot.get("profile_icon_id", 0),
                            "old_tier": prev["tier"],
                            "old_rank": prev.get("rank", ""),
                            "old_lp": prev.get("lp", 0),
                            "new_tier": new_tier,
                            "new_rank": snapshot["rank"],
                            "new_lp": snapshot["lp"],
                        })
                except ValueError:
                    pass

            self.db.record_rank_snapshot(
                puuid=puuid,
                riot_id=riot_id,
                tier=new_tier,
                rank=snapshot["rank"],
                lp=snapshot["lp"],
                wins=snapshot["wins"],
                losses=snapshot["losses"],
            )

        return events
