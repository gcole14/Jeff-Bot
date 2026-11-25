import asyncio
import time
import pytest

from ai import DeepSeekClient
from coaching import (
    analyze_match_timeline,
    compute_champion_pool_audit,
    compute_win_loss_split,
    evaluate_goal_progress,
    pick_next_coaching_goal,
)
from db import Database
from embeds import (
    build_daily_embed,
    build_weekly_embed,
    build_player_snapshot_embed,
    build_lastgame_embed,
    build_review_embed,
    build_coach_embed,
    build_pool_embed,
    build_goal_embed,
    build_scout_embed,
    build_squad_debrief_embed,
    build_comp_embed,
    build_ask_embed,
    build_promotion_embed,
    build_demotion_embed,
    build_versus_embed,
    build_damage_embed,
    build_synergy_embed,
    build_live_embed,
    build_mastery_embed,
    build_ai_embed,
    build_status_embed,
)
from riot_api import RiotAPI, RiotAuthError
from stats import (
    StatsAggregator,
    RankTracker,
    resolve_riot_id,
    rank_score,
    calc_lp_delta,
    format_rank,
)


def _make_match_payload(
    match_id: str,
    participants: list[dict],
    duration: int = 1800,
    queue_id: int = 420,
    creation_ms: int = 1_700_000_000_000,
) -> dict:
    return {
        "metadata": {"matchId": match_id},
        "info": {
            "gameCreation": creation_ms,
            "gameDuration": duration,
            "queueId": queue_id,
            "participants": participants,
            "teams": [
                {"teamId": 100, "objectives": {"dragon": {"kills": 2}, "baron": {"kills": 1}}},
                {"teamId": 200, "objectives": {"dragon": {"kills": 3}, "baron": {"kills": 0}}},
            ],
        },
    }


def _participant(
    puuid: str,
    pid: int = 1,
    champ: str = "Ahri",
    win: bool = True,
    kills: int = 8,
    deaths: int = 3,
    assists: int = 7,
    cs: int = 180,
    dmg: int = 24000,
    vision: int = 22,
    pinks: int = 1,
    team_id: int = 100,
    role: str = "MIDDLE",
) -> dict:
    return {
        "participantId": pid,
        "puuid": puuid,
        "championName": champ,
        "win": win,
        "kills": kills,
        "deaths": deaths,
        "assists": assists,
        "totalMinionsKilled": cs,
        "neutralMinionsKilled": 10,
        "totalDamageDealtToChampions": dmg,
        "visionScore": vision,
        "visionWardsBoughtInGame": pinks,
        "goldEarned": 12500,
        "quadraKills": 0,
        "pentaKills": 0,
        "teamId": team_id,
        "teamPosition": role,
        "item0": 6655,
        "item1": 4645,
    }


def _make_timeline_payload() -> dict:
    frames = []
    for minute in range(21):
        pframes = {
            "1": {"totalGold": 500 + minute * 420, "minionsKilled": minute * 7, "jungleMinionsKilled": 0, "xp": minute * 450},
            "2": {"totalGold": 500 + minute * 390, "minionsKilled": minute * 6, "jungleMinionsKilled": 2, "xp": minute * 430},
            "6": {"totalGold": 500 + minute * 360, "minionsKilled": minute * 6, "jungleMinionsKilled": 0, "xp": minute * 420},
        }
        events = []
        if minute == 6:
            # Solo kill by participant 1
            events.append({"type": "CHAMPION_KILL", "timestamp": 6 * 60_000, "killerId": 1, "victimId": 6, "assistingParticipantIds": []})
        if minute == 11:
            events.append({"type": "ITEM_PURCHASED", "timestamp": 11 * 60_000, "participantId": 1, "itemId": 6655})
        if minute == 17:
            # Death right before enemy dragon (throw death!) + cluster death at 19m
            events.append({"type": "CHAMPION_KILL", "timestamp": 17 * 60_000, "killerId": 6, "victimId": 1, "assistingParticipantIds": [7]})
            events.append({"type": "ELITE_MONSTER_KILL", "timestamp": 17 * 60_000 + 35_000, "monsterType": "DRAGON", "killerTeamId": 200})
        if minute == 19:
            events.append({"type": "CHAMPION_KILL", "timestamp": 19 * 60_000, "killerId": 6, "victimId": 1, "assistingParticipantIds": []})
        frames.append({"participantFrames": pframes, "events": events})
    return {"info": {"frames": frames}}


class FakeRiotAPI:
    def __init__(self, accounts, ranked, match_ids_by_puuid, matches_by_id, timelines_by_id=None, active_games=None):
        self.accounts = accounts
        self.ranked = ranked
        self.match_ids_by_puuid = match_ids_by_puuid
        self.matches_by_id = matches_by_id
        self.timelines_by_id = timelines_by_id or {}
        self.active_games = active_games or {}
        self.match_fetch_calls = 0

    async def get_account_by_riot_id(self, game_name, tag_line):
        key = f"{game_name}#{tag_line}"
        return {"puuid": self.accounts[key]}

    async def get_summoner_by_puuid(self, puuid):
        return {"summonerLevel": 150, "profileIconId": 1234}

    async def get_ranked_stats_by_puuid(self, puuid):
        return self.ranked.get(puuid, [])

    async def get_match_ids(self, puuid, start_time=None, count=20, queue=None):
        return self.match_ids_by_puuid.get(puuid, [])[:count]

    async def get_match(self, match_id):
        self.match_fetch_calls += 1
        await asyncio.sleep(0.005)
        return self.matches_by_id[match_id]

    async def get_match_timeline(self, match_id):
        return self.timelines_by_id.get(match_id)

    async def get_active_game(self, puuid):
        return self.active_games.get(puuid)

    async def get_champion_mastery_for_champ(self, puuid, champion_id):
        return {"championPoints": 85000 if puuid == "puuid-a" else 1200}

    async def get_ddragon_latest_version(self):
        return "14.23.1"

    async def get_ddragon_champions(self, version):
        return {"data": {"Ahri": {"key": "103", "name": "Ahri"}, "Zed": {"key": "238", "name": "Zed"}}}

    async def get_ddragon_items(self, version):
        return {
            "data": {
                "6655": {"name": "Luden's Companion", "gold": {"total": 2850}},
                "4645": {"name": "Shadowflame", "gold": {"total": 3200}},
            }
        }


def test_rank_math():
    assert rank_score("UNRANKED", "", 0) < rank_score("IRON", "IV", 0)
    assert rank_score("GOLD", "I", 99) < rank_score("PLATINUM", "IV", 0)
    assert rank_score("DIAMOND", "I", 100) <= rank_score("MASTER", "I", 0)
    assert rank_score("MASTER", "I", 500) < rank_score("GRANDMASTER", "I", 500)
    assert rank_score("GRANDMASTER", "I", 900) < rank_score("CHALLENGER", "I", 900)

    assert calc_lp_delta("GOLD", "II", 80, "GOLD", "I", 25) == 45
    assert calc_lp_delta("MASTER", "I", 200, "GRANDMASTER", "I", 350) == 150
    assert calc_lp_delta("UNRANKED", "", 0, "SILVER", "IV", 50) == 0
    assert format_rank("UNRANKED", "", 0) == "Unranked"
    assert format_rank("MASTER", "I", 240) == "Master 240 LP"


def test_resolve_riot_id():
    roster = ["Cookmysock#cook", "SwimboSlice#pizza"]
    assert resolve_riot_id("cookmysock", roster) == "Cookmysock#cook"
    assert resolve_riot_id("SomeoneElse#NA1", roster) == "SomeoneElse#NA1"
    with pytest.raises(ValueError):
        resolve_riot_id("UnknownPlayer", roster)
    with pytest.raises(ValueError):
        resolve_riot_id("BadFormat#", roster)


def test_riot_api_missing_key_raises_auth_error():
    api = RiotAPI(api_key="")
    with pytest.raises(RiotAuthError):
        asyncio.run(api.get_account_by_riot_id("Player One", "NA1"))


def test_timeline_vod_analyzer():
    match = _make_match_payload(
        "NA1_VOD",
        [
            _participant("puuid-a", pid=1, champ="Ahri", win=False, kills=6, deaths=4, assists=5, team_id=100, role="MIDDLE"),
            _participant("puuid-b", pid=2, champ="LeeSin", win=False, kills=4, deaths=3, assists=8, team_id=100, role="JUNGLE"),
            _participant("puuid-enemy", pid=6, champ="Zed", win=True, kills=9, deaths=2, assists=4, team_id=200, role="MIDDLE"),
        ],
    )
    timeline = _make_timeline_payload()
    item_map = {6655: ("Luden's Companion", 2850), 4645: ("Shadowflame", 3200)}

    report = analyze_match_timeline(
        match, timeline, "puuid-a", "Cookmysock", item_map, "Ranked Solo", {"puuid-b": "SwimboSlice"}
    )
    assert report is not None
    assert report.lane_opponent_champ == "Zed"
    assert report.gold_diff_10 == 600   # (420 - 360) * 10
    assert report.cs_diff_10 == 10      # (7 - 6) * 10
    assert report.solo_kills_pre14 == 1
    assert len(report.objective_throw_deaths) == 1
    assert "Dragon" in report.objective_throw_deaths[0]
    assert len(report.death_clusters) == 1
    assert "11m Luden's Companion" in report.item_timings
    assert report.friends_in_game == ["SwimboSlice"]

    embed = build_review_embed(report, "**Laning:** Ahead +600g at 10m.")
    assert "Ahri vs Zed" in embed.title


def test_full_pipeline_caching_coaching_scout_debrief_and_embeds(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    db.seed_roster(["Cookmysock#cook", "SwimboSlice#pizza"])
    assert db.get_roster() == ["Cookmysock#cook", "SwimboSlice#pizza"]

    now = int(time.time())
    db.record_rank_snapshot("puuid-a", "Cookmysock#cook", "GOLD", "II", 50, 10, 10, recorded_at=now - 7200)

    shared_match = _make_match_payload(
        "NA1_SHARED",
        [
            _participant("puuid-a", pid=1, champ="Ahri", win=True, kills=10, deaths=2, assists=8, dmg=30000, pinks=2, team_id=100, role="MIDDLE"),
            _participant("puuid-b", pid=2, champ="LeeSin", win=True, kills=6, deaths=4, assists=14, dmg=18000, pinks=1, team_id=100, role="JUNGLE"),
            _participant("puuid-e", pid=6, champ="Zed", win=False, kills=3, deaths=7, assists=2, dmg=14000, team_id=200, role="MIDDLE"),
        ],
        duration=1800,
        creation_ms=(now - 3600) * 1000,
    )
    remake_match = _make_match_payload(
        "NA1_REMAKE",
        [_participant("puuid-a", pid=1, champ="Yasuo", win=False, kills=0, deaths=1, assists=0, dmg=400, team_id=100)],
        duration=190,
        creation_ms=(now - 1800) * 1000,
    )
    solo_loss = _make_match_payload(
        "NA1_SOLO",
        [
            _participant("puuid-a", pid=1, champ="Ahri", win=False, kills=3, deaths=8, assists=2, dmg=15000, pinks=0, team_id=100, role="MIDDLE"),
            _participant("puuid-e", pid=6, champ="Zed", win=True, kills=11, deaths=2, assists=4, dmg=29000, team_id=200, role="MIDDLE"),
        ],
        duration=1500,
        creation_ms=(now - 600) * 1000,
    )

    fake_riot = FakeRiotAPI(
        accounts={"Cookmysock#cook": "puuid-a", "SwimboSlice#pizza": "puuid-b"},
        ranked={
            "puuid-a": [{"queueType": "RANKED_SOLO_5x5", "tier": "GOLD", "rank": "II", "leaguePoints": 74, "wins": 11, "losses": 10}],
            "puuid-b": [{"queueType": "RANKED_SOLO_5x5", "tier": "SILVER", "rank": "I", "leaguePoints": 40, "wins": 8, "losses": 9}],
        },
        match_ids_by_puuid={
            "puuid-a": ["NA1_SOLO", "NA1_REMAKE", "NA1_SHARED"],
            "puuid-b": ["NA1_SHARED"],
            "puuid-e": ["NA1_SHARED"],
        },
        matches_by_id={
            "NA1_SHARED": shared_match,
            "NA1_REMAKE": remake_match,
            "NA1_SOLO": solo_loss,
        },
        timelines_by_id={"NA1_SOLO": _make_timeline_payload()},
        active_games={
            "puuid-a": {
                "gameId": 999,
                "gameQueueConfigId": 420,
                "gameLength": 620,
                "participants": [
                    {"puuid": "puuid-a", "championId": 103, "teamId": 100, "riotId": "Cookmysock#cook"},
                    {"puuid": "puuid-e", "championId": 238, "teamId": 200, "riotId": "EnemyMid#NA1"},
                ],
            },
        },
    )

    agg = StatsAggregator(fake_riot, db)
    stats = asyncio.run(agg.get_weekly_stats())

    # Single-flight deduplication + SQLite cache
    assert fake_riot.match_fetch_calls == 3
    stats_again = asyncio.run(agg.get_weekly_stats())
    assert fake_riot.match_fetch_calls == 3

    a, b = stats_again
    assert a.games_played == 2
    assert a.remakes_skipped == 1
    assert a.lp_change == 24

    # Wins vs Losses split + Pool Audit + Weekly Coaching Goals
    split = compute_win_loss_split(a)
    assert split["wins"]["avg_deaths"] == 2.0
    assert split["losses"]["avg_deaths"] == 8.0

    pool = compute_champion_pool_audit(a)
    assert pool["solid"][0]["champion"] == "Ahri"
    assert pool["roles"][0]["role"] == "Mid"

    picked = pick_next_coaching_goal(a)
    assert picked is not None
    goal = db.set_coaching_goal("puuid-a", "Cookmysock#cook", **picked)
    ev = evaluate_goal_progress(goal, a)
    assert ev["games"] == 2

    # Timeline review via aggregator (cached in SQLite)
    report, err = asyncio.run(agg.get_match_review("Cookmysock#cook", match_offset=0))
    assert err is None and report is not None
    assert report.match_id == "NA1_SOLO"

    # Live scout
    scout = asyncio.run(agg.get_live_scout_data("Cookmysock#cook"))
    assert scout["in_game"] is True
    assert len(scout["allies"]) == 1 and len(scout["enemies"]) == 1

    # Squad debrief detection + deduplication
    debriefs = asyncio.run(agg.find_squad_games(only_unannounced=True))
    assert len(debriefs) == 1
    assert debriefs[0]["match_id"] == "NA1_SHARED"
    assert debriefs[0]["mvp"] in ("Cookmysock", "SwimboSlice")
    db.mark_match_announced("NA1_SHARED")
    assert asyncio.run(agg.find_squad_games(only_unannounced=True)) == []

    # Squad context for /ask & /comp
    ctx = asyncio.run(agg.build_squad_context(days=14))
    assert "Cookmysock" in ctx and "Squad together" in ctx

    # Synergy
    syn = agg.compute_synergy(stats_again)
    assert syn["duos"][0]["pair"] == "Cookmysock + SwimboSlice"

    # Rank promotion & demotion tracking
    tracker = RankTracker(agg, db)
    fake_riot.ranked["puuid-a"][0]["tier"] = "PLATINUM"
    fake_riot.ranked["puuid-b"][0]["tier"] = "BRONZE"
    events = asyncio.run(tracker.check_rank_changes())
    assert {e["display_name"]: e["direction"] for e in events} == {
        "Cookmysock": "promotion",
        "SwimboSlice": "demotion",
    }

    # Verify all embeds construct cleanly
    for embed in build_daily_embed(stats_again, commentary="Great games"):
        assert len(embed.fields) <= 25
    for embed in build_weekly_embed(stats_again, commentary="Summary", synergy=syn, goal_grades=["✅ Cookmysock"]):
        assert len(embed.fields) <= 25
    assert build_player_snapshot_embed(a).title == "🔍 Cookmysock"
    assert build_lastgame_embed(asyncio.run(agg.get_last_game("Cookmysock#cook")), "Unlucky").title.startswith("❌ DEFEAT")
    assert build_coach_embed(a, split, "Stop dying in losses").title.startswith("📋 Coach Jeff")
    assert build_pool_embed(a, pool, "Spam Ahri").title.startswith("🎯 Champion Pool Audit")
    assert build_goal_embed("Cookmysock", ev, is_new=True).title.startswith("🎯 Weekly Coaching Goal")
    assert build_scout_embed(scout, "Camp Zed").title.startswith("🔭 Live Scout")
    assert build_squad_debrief_embed(debriefs[0], "Clean W").title.startswith("✅ SQUAD VICTORY")
    assert build_comp_embed("Comp 1: Ahri + Lee Sin", notes="teamfight").title.startswith("🧩")
    assert build_ask_embed("Why am I losing?", "Too many deaths", "Grant").title.startswith("🧠")
    assert build_promotion_embed(events[0], "Let's go!").title.startswith("🎉")
    assert build_demotion_embed(events[1], "RIP").title.startswith("📉")
    assert build_versus_embed(a, b).title == "⚔️ Cookmysock vs SwimboSlice"
    assert build_damage_embed(stats_again).title == "💥 Weekly Damage Leaderboard"
    assert build_synergy_embed(syn).title == "🤝 Squad Synergy"
    assert build_live_embed(asyncio.run(agg.get_live_games())).title == "🟢 Live Games"
    assert build_mastery_embed({"display_name": "Cookmysock", "masteries": [{"name": "Ahri", "level": 7, "points": 120000}], "version": "14.23.1"}).title
    assert build_ai_embed(a, "Nice CS bro", mode="roast").title.startswith("🔥")
    assert build_status_embed({
        "riot_ok": True,
        "riot_msg": "Valid",
        "ai_ok": True,
        "ai_msg": "Connected",
        "roster": db.get_roster(),
        "cached_matches": db.count_cached_matches(),
        "channel_label": "#lol",
        "next_runs": [],
    }).title == "🩺 Jeff Bot Status"

    # DeepSeek gracefully returns None when unconfigured
    ai = DeepSeekClient(api_key="", enabled=False)
    assert asyncio.run(ai.summarize_recap(stats_again)) is None
    assert asyncio.run(ai.roast_player(a)) is None

    db.close()
