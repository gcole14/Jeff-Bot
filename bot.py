import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from ai import DeepSeekClient
from cards import (
    AssetCache,
    render_build_card,
    render_coach_card,
    render_damage_card,
    render_debrief_card,
    render_goal_card,
    render_mastery_card,
    render_match_card,
    render_patch_card,
    render_player_card,
    render_pool_card,
    render_rank_change_card,
    render_scout_card,
    render_squad_card,
    render_synergy_card,
    render_versus_card,
)
from coaching import (
    compute_champion_pool_audit,
    compute_win_loss_split,
    evaluate_goal_progress,
    pick_next_coaching_goal,
)
from config import (
    ENV_PATH,
    DISCORD_TOKEN,
    GUILD_ID,
    CHANNEL_ID,
    SUMMONERS,
    DAILY_HOUR,
    DAILY_MINUTE,
    WEEKLY_DAY,
    WEEKLY_HOUR,
    WEEKLY_MINUTE,
    TIMEZONE,
    WEEKLY_MATCH_LOOKBACK_DAYS,
    RANK_CHECK_INTERVAL_HOURS,
    SQUAD_DEBRIEF_ENABLED,
    SQUAD_CHECK_INTERVAL_MINUTES,
    PATCH_CHECK_ENABLED,
    PATCH_CHECK_INTERVAL_HOURS,
    DB_PATH,
    RANK_HISTORY_FILE,
)
from db import Database
from embeds import (
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
    build_item_build_embed,
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
from patch_notes import PatchNotesFetcher
from riot_api import RiotAPI
from listen import STTClient, VoiceListenerManager
from stats import StatsAggregator, RankTracker, resolve_riot_id
from tts import HybridTTSClient
from voice import VoiceManager, check_voice_prereqs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# VOICE_DEBUG=true surfaces discord.py's voice/DAVE (E2EE) handshake logs for troubleshooting silent playback
if os.getenv("VOICE_DEBUG", "").strip().lower() in ("1", "true", "yes"):
    for _name in ("discord.voice_state", "discord.voice_client", "discord.gateway"):
        logging.getLogger(_name).setLevel(logging.DEBUG)


def _update_env_var(env_path: Path, key: str, value: str) -> None:
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    prefix = f"{key}="
    replaced = False
    for i, line in enumerate(lines):
        if line.strip().startswith(prefix):
            lines[i] = f"{key}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# Initialize core services
db = Database(DB_PATH)
db.seed_roster(SUMMONERS)
db.migrate_legacy_rank_history(RANK_HISTORY_FILE)

riot = RiotAPI()
assets = AssetCache(riot)
patch_fetcher = PatchNotesFetcher()
deepseek = DeepSeekClient()
aggregator = StatsAggregator(riot, db)
rank_tracker = RankTracker(aggregator, db)
tts_client = HybridTTSClient()
stt_client = STTClient()
voice_manager = VoiceManager(tts_client)
voice_listener = VoiceListenerManager(
    voice_manager=voice_manager,
    stt_client=stt_client,
    deepseek=deepseek,
    aggregator=aggregator,
    db=db,
)
voice_manager.set_lifecycle_hooks(
    on_join=voice_listener.on_voice_join,
    on_leave=voice_listener.on_voice_leave,
)
scheduler = AsyncIOScheduler(timezone=TIMEZONE)


class JeffBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self._setup_done = False

    async def close(self) -> None:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        await voice_listener.close()
        await voice_manager.close()
        await assets.close()
        await patch_fetcher.close()
        await riot.close()
        await deepseek.close()
        db.close()
        await super().close()


bot = JeffBot()


async def _get_target_channel() -> Optional[discord.abc.Messageable]:
    if not CHANNEL_ID:
        logger.error("CHANNEL_ID is not configured in .env")
        return None
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(CHANNEL_ID)
        except Exception as e:
            logger.error(f"Could not fetch channel {CHANNEL_ID}: {e}")
            return None
    return channel


async def post_daily(target_channel: Optional[discord.abc.Messageable] = None) -> tuple[bool, str]:
    channel = target_channel or await _get_target_channel()
    if not channel:
        return False, f"Could not find target channel ({CHANNEL_ID}). Set CHANNEL_ID in .env."
    logger.info("Posting daily recap...")
    try:
        stats = await aggregator.get_daily_stats()
        date_str = datetime.now(ZoneInfo(TIMEZONE)).strftime("%B %d, %Y")
        card_buf = await render_squad_card(stats, "Daily LoL Recap", f"{date_str} • Last 24 Hours", assets)
        # The card carries everything; no follow-up text embed.
        await channel.send(file=discord.File(card_buf, filename="daily_recap.png"))
        logger.info("Daily recap posted successfully.")
        return True, "Daily recap posted!"
    except Exception as e:
        logger.exception(f"Failed to post daily recap: {e}")
        return False, f"Failed to post daily recap: {e}"


async def post_weekly(target_channel: Optional[discord.abc.Messageable] = None) -> tuple[bool, str]:
    channel = target_channel or await _get_target_channel()
    if not channel:
        return False, f"Could not find target channel ({CHANNEL_ID}). Set CHANNEL_ID in .env."
    logger.info("Posting weekly digest...")
    try:
        stats = await aggregator.get_weekly_stats()
        synergy = aggregator.compute_synergy(stats)

        # Grade active weekly coaching goals and rotate new ones for active players
        goal_grades: list[str] = []
        for ps in stats:
            if ps.error or not ps.puuid:
                continue
            active_goal = db.get_active_goal(ps.puuid)
            if active_goal:
                ev = evaluate_goal_progress(active_goal, ps)
                if ps.games_played > 0:
                    badge = "✅ Hit!" if ev["met"] else "❌ Missed"
                    goal_grades.append(
                        f"{badge} **{ps.display_name}**: {ev['current']:.1f} vs {ev['op']} {ev['target']:.1f} {ev['unit']}"
                    )
                    db.close_coaching_goal(active_goal["id"], "completed" if ev["met"] else "missed")
            if ps.games_played >= 3:
                nxt = pick_next_coaching_goal(ps)
                if nxt:
                    db.set_coaching_goal(ps.puuid, ps.riot_id, **nxt)

        commentary = (
            await deepseek.summarize_recap(stats, period="weekly", synergy=synergy)
            if deepseek.is_configured
            else None
        )
        active = [ps for ps in stats if ps.games_played > 0 and not ps.error]
        total_games = sum(ps.games_played for ps in active)
        total_hours = sum(ps.hours_played for ps in active)
        card_buf = await render_squad_card(
            stats,
            "Weekly LoL Digest",
            f"Last 7 Days • {total_games} Games • {total_hours:.1f}h Played",
            assets,
        )
        embeds = build_weekly_embed(
            stats, commentary=commentary, synergy=synergy, goal_grades=goal_grades or None
        )
        await channel.send(file=discord.File(card_buf, filename="weekly_digest.png"))
        for embed in embeds[1:]:
            await channel.send(embed=embed)
        logger.info("Weekly digest posted successfully.")
        return True, "Weekly digest posted!"
    except Exception as e:
        logger.exception(f"Failed to post weekly digest: {e}")
        return False, f"Failed to post weekly digest: {e}"


async def check_rank_changes() -> None:
    channel = await _get_target_channel()
    try:
        events = await rank_tracker.check_rank_changes()
        if not events:
            return
        if not channel:
            logger.warning(f"Detected {len(events)} rank change(s), but CHANNEL_ID is unavailable.")
            return
        for event in events:
            commentary, card_buf = await asyncio.gather(
                deepseek.comment_rank_change(event) if deepseek.is_configured else asyncio.sleep(0, result=None),
                render_rank_change_card(event, assets),
            )
            content = f"🧠 **Jeff Says:** {commentary}" if commentary else None
            await channel.send(
                content=content,
                file=discord.File(card_buf, filename=f"{event['display_name']}_rank.png"),
            )
            if commentary:
                guild_id = getattr(getattr(channel, "guild", None), "id", None)
                voice_manager.speak_if_connected(guild_id, commentary)
        logger.info(f"Posted {len(events)} rank change alert(s).")
    except Exception as e:
        logger.exception(f"Rank change check failed: {e}")


async def check_squad_debriefs() -> None:
    if not SQUAD_DEBRIEF_ENABLED:
        return
    channel = await _get_target_channel()
    if not channel:
        return
    try:
        debriefs = await aggregator.find_squad_games(only_unannounced=True, lookback_per_player=3)
        for debrief in debriefs:
            commentary, card_buf = await asyncio.gather(
                deepseek.debrief_squad_game(debrief) if deepseek.is_configured else asyncio.sleep(0, result=None),
                render_debrief_card(debrief, assets),
            )
            content = f"🧠 **Jeff's Squad Debrief:** {commentary}" if commentary else None
            await channel.send(
                content=content,
                file=discord.File(card_buf, filename=f"squad_debrief_{debrief['match_id']}.png"),
            )
            if commentary:
                guild_id = getattr(getattr(channel, "guild", None), "id", None)
                voice_manager.speak_if_connected(guild_id, commentary)
            db.mark_match_announced(debrief["match_id"])
            logger.info(f"Posted auto squad debrief for {debrief['match_id']}.")
    except Exception as e:
        logger.exception(f"Squad debrief check failed: {e}")


async def _get_squad_top_champs() -> dict[str, list[str]]:
    """Collects each tracked friend's recently played champions for personalized patch callouts."""
    result: dict[str, list[str]] = {}
    try:
        stats = await aggregator.get_weekly_stats()
        for ps in stats:
            if not ps.error and ps.top_champions:
                result[ps.display_name] = [c for c, _ in ps.top_champions]
    except Exception as e:
        logger.debug(f"Could not fetch squad champs for patch summary: {e}")
    return result


async def build_latest_patch_payload(force_refresh: bool = False) -> tuple[Optional[dict], Optional[dict], Optional[str]]:
    """Fetches latest official Riot patch notes + cached or fresh DeepSeek summary."""
    info = await patch_fetcher.get_latest_patch_info()
    if not info:
        return None, None, "Could not fetch official Riot patch notes right now."

    cached = db.get_announced_patch(info["version"])
    if cached and cached.get("summary") and not force_refresh:
        return info, cached["summary"], None

    if not deepseek.is_configured:
        return info, None, "DeepSeek AI is required to summarize patch notes (set DEEPSEEK_API_KEY in .env)."

    squad_champs = await _get_squad_top_champs()
    summary = await deepseek.summarize_patch_notes(
        version=info["version"],
        body_text=info["body_text"],
        squad_champs=squad_champs,
    )
    if not summary:
        return info, None, "DeepSeek did not return a patch summary — try again in a moment."

    db.save_announced_patch(info["version"], info["url"], summary=summary)
    return info, summary, None


async def check_new_patch() -> None:
    if not PATCH_CHECK_ENABLED:
        return
    try:
        info = await patch_fetcher.get_latest_patch_info()
        if not info:
            return
        if db.get_announced_patch(info["version"]):
            return

        # On first-ever run, seed current patch silently so we don't spam an already-live patch;
        # every future patch release will auto-post immediately!
        if db.count_announced_patches() == 0:
            db.save_announced_patch(info["version"], info["url"], summary=None)
            logger.info(f"Seeded baseline patch {info['version']} (future patches will auto-announce).")
            return

        channel = await _get_target_channel()
        if not channel or not deepseek.is_configured:
            return

        squad_champs = await _get_squad_top_champs()
        summary = await deepseek.summarize_patch_notes(
            version=info["version"],
            body_text=info["body_text"],
            squad_champs=squad_champs,
        )
        if not summary:
            return

        card_buf = await render_patch_card(summary, assets, publish_date=info.get("publish_date", ""))
        await channel.send(
            content=f"🆕 **League of Legends Patch {info['version']} is live!** <{info['url']}>",
            file=discord.File(card_buf, filename=f"patch_{info['version']}.png"),
        )
        db.save_announced_patch(info["version"], info["url"], summary=summary)
        logger.info(f"Auto-announced new Patch {info['version']}!")
    except Exception as e:
        logger.exception(f"Patch check failed: {e}")


@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")

    if bot._setup_done:
        logger.info("Reconnect detected — skipping scheduler/sync setup.")
        return

    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            logger.info(f"Synced {len(synced)} slash command(s) instantly to guild {GUILD_ID}.")
        else:
            synced = await bot.tree.sync()
            logger.info(f"Synced {len(synced)} global slash command(s) (may take up to 1h to appear).")
    except Exception as e:
        logger.exception(f"Failed to sync slash commands: {e}")

    scheduler.add_job(
        post_daily,
        CronTrigger(hour=DAILY_HOUR, minute=DAILY_MINUTE, timezone=TIMEZONE),
        id="daily_recap",
        replace_existing=True,
    )
    scheduler.add_job(
        post_weekly,
        CronTrigger(day_of_week=WEEKLY_DAY, hour=WEEKLY_HOUR, minute=WEEKLY_MINUTE, timezone=TIMEZONE),
        id="weekly_digest",
        replace_existing=True,
    )
    scheduler.add_job(
        check_rank_changes,
        IntervalTrigger(hours=max(1, RANK_CHECK_INTERVAL_HOURS)),
        id="rank_change_check",
        replace_existing=True,
    )
    if SQUAD_DEBRIEF_ENABLED:
        scheduler.add_job(
            check_squad_debriefs,
            IntervalTrigger(minutes=max(5, SQUAD_CHECK_INTERVAL_MINUTES)),
            id="squad_debrief_check",
            replace_existing=True,
        )
    if PATCH_CHECK_ENABLED:
        scheduler.add_job(
            check_new_patch,
            IntervalTrigger(hours=max(1, PATCH_CHECK_INTERVAL_HOURS)),
            id="patch_notes_check",
            replace_existing=True,
        )

    scheduler.start()
    logger.info(
        f"Scheduler started. Daily at {DAILY_HOUR}:{DAILY_MINUTE:02d}, "
        f"Weekly on {WEEKLY_DAY} at {WEEKLY_HOUR}:{WEEKLY_MINUTE:02d} ({TIMEZONE}), "
        f"Rank check every {RANK_CHECK_INTERVAL_HOURS}h, "
        f"Squad debriefs {'every ' + str(SQUAD_CHECK_INTERVAL_MINUTES) + 'm' if SQUAD_DEBRIEF_ENABLED else 'disabled'}, "
        f"Patch watch {'every ' + str(PATCH_CHECK_INTERVAL_HOURS) + 'h' if PATCH_CHECK_ENABLED else 'disabled'}."
    )

    asyncio.create_task(check_rank_changes())
    if SQUAD_DEBRIEF_ENABLED:
        asyncio.create_task(aggregator.seed_announced_squad_games())
    if PATCH_CHECK_ENABLED:
        asyncio.create_task(check_new_patch())
    bot._setup_done = True


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
):
    await voice_manager.handle_voice_state_update(member, before, after)


# -------------------------------------------------------------------------
# Autocomplete & Choices
# -------------------------------------------------------------------------

QUEUE_CHOICES = [
    app_commands.Choice(name="All Queues", value=0),
    app_commands.Choice(name="Ranked Solo", value=420),
    app_commands.Choice(name="Ranked Flex", value=440),
    app_commands.Choice(name="ARAM", value=450),
    app_commands.Choice(name="Normal Draft", value=400),
]
QUEUE_LABEL_MAP = {c.value: c.name for c in QUEUE_CHOICES}


async def player_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    current_lower = current.lower()
    choices = []
    for full in aggregator.get_roster():
        name = full.split("#", 1)[0]
        if not current or current_lower in name.lower() or current_lower in full.lower():
            choices.append(app_commands.Choice(name=name, value=name))
    return choices[:25]


# -------------------------------------------------------------------------
# Core Stats Slash Commands
# -------------------------------------------------------------------------

@bot.tree.command(name="daily", description="Force a daily recap right now (admin only)")
@app_commands.default_permissions(administrator=True)
async def slash_daily(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    ok, msg = await post_daily(target_channel=await _get_target_channel() or interaction.channel)
    await interaction.followup.send(f"{'✅' if ok else '❌'} {msg}", ephemeral=True)


@bot.tree.command(name="weekly", description="Force a weekly digest right now (admin only)")
@app_commands.default_permissions(administrator=True)
async def slash_weekly(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    ok, msg = await post_weekly(target_channel=await _get_target_channel() or interaction.channel)
    await interaction.followup.send(f"{'✅' if ok else '❌'} {msg}", ephemeral=True)


ROLE_CHOICES = [
    app_commands.Choice(name="Auto / Main Role", value=""),
    app_commands.Choice(name="Top", value="Top"),
    app_commands.Choice(name="Jungle", value="Jungle"),
    app_commands.Choice(name="Mid", value="Mid"),
    app_commands.Choice(name="ADC", value="ADC"),
    app_commands.Choice(name="Support", value="Support"),
]


@bot.tree.command(name="stats", description="Render a visual League stats card for a player")
@app_commands.describe(
    player="Player nickname (tracked) or full Name#TAG",
    days="Lookback window in days (default: 7)",
    queue="Optional queue filter",
)
@app_commands.choices(queue=QUEUE_CHOICES)
@app_commands.autocomplete(player=player_autocomplete)
async def slash_stats(
    interaction: discord.Interaction,
    player: str,
    days: app_commands.Range[int, 1, 30] = 7,
    queue: int = 0,
):
    try:
        resolved = resolve_riot_id(player, aggregator.get_roster())
    except ValueError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        qlabel = QUEUE_LABEL_MAP.get(queue, "All Queues")
        data = await aggregator.get_player_snapshot(resolved, days=days, queue=(queue or None))
        if data.error:
            await interaction.followup.send(embed=build_player_snapshot_embed(data, days=days, queue_label=qlabel))
            return
        card_buf = await render_player_card(data, assets, days=days, queue_label=qlabel)
        await interaction.followup.send(file=discord.File(card_buf, filename=f"{data.display_name}_stats.png"))
    except Exception as e:
        logger.exception(f"Stats card failed for {resolved}: {e}")
        await interaction.followup.send(f"❌ Could not fetch stats for `{resolved}`: {e}")


@bot.tree.command(name="lastgame", description="Visual match card & Jeff's take on a player's latest game")
@app_commands.describe(player="Player nickname (tracked) or full Name#TAG")
@app_commands.autocomplete(player=player_autocomplete)
async def slash_lastgame(interaction: discord.Interaction, player: str):
    try:
        resolved = resolve_riot_id(player, aggregator.get_roster())
    except ValueError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        game = await aggregator.get_last_game(resolved)
        if game.get("error") or not game.get("match"):
            await interaction.followup.send(embed=build_lastgame_embed(game))
            return
        m = game["match"]
        commentary, card_buf = await asyncio.gather(
            deepseek.comment_last_game(game) if deepseek.is_configured else asyncio.sleep(0, result=None),
            render_match_card(
                display_name=game["display_name"],
                champion=m.champion,
                win=m.win,
                queue_name=m.queue_name,
                duration_seconds=m.duration_seconds,
                kills=m.kills,
                deaths=m.deaths,
                assists=m.assists,
                cs=m.cs,
                cs_per_min=m.cs_per_min,
                damage=m.damage_to_champions,
                damage_share=m.damage_share,
                kp=m.kill_participation,
                vision=m.vision_score,
                gold=m.gold_earned,
                assets=assets,
                item_ids=m.item_ids,
                role=m.role,
            ),
        )
        content = f"**🧠 Jeff's Take:** {commentary}" if commentary else None
        friends = game.get("friends_in_game") or []
        if friends:
            squad_line = f"🤝 **Squadmates:** {', '.join(friends)}"
            content = f"{squad_line}\n{content}" if content else squad_line
        await interaction.followup.send(
            content=content,
            file=discord.File(card_buf, filename=f"{game['display_name']}_lastgame.png"),
        )
    except Exception as e:
        logger.exception(f"Lastgame failed for {resolved}: {e}")
        await interaction.followup.send(f"❌ Could not fetch last game for `{resolved}`: {e}")


@bot.tree.command(name="link", description="Link your Discord account to your Riot ID for 1-click /build")
@app_commands.describe(player="Your Riot ID nickname (tracked) or full Name#TAG")
@app_commands.autocomplete(player=player_autocomplete)
async def slash_link(interaction: discord.Interaction, player: str):
    try:
        resolved = resolve_riot_id(player, aggregator.get_roster())
    except ValueError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return
    db.link_discord_user(interaction.user.id, resolved)
    await interaction.response.send_message(
        f"✅ Linked your Discord account to **`{resolved}`**! Now you can just run `/build` with no arguments whenever you're in a game.",
        ephemeral=True,
    )


async def _detect_live_lobby(riot_id: str) -> Optional[dict]:
    """Returns live lobby info (champion, allies, enemies, queue_name) if player is in-game."""
    puuid, _, _ = await aggregator._resolve_player(riot_id)
    live_game = await riot.get_active_game(puuid)
    if not live_game:
        return None
    _, champ_map = await aggregator._get_champion_name_map()
    parts = live_game.get("participants", [])
    me = next((p for p in parts if p.get("puuid") == puuid), None)
    my_tid = int(me.get("teamId", 100)) if me else 100
    qid = int(live_game.get("gameQueueConfigId", 0))
    from stats import queue_name as _qname
    return {
        "riot_id": riot_id,
        "display_name": riot_id.split("#", 1)[0],
        "champion": champ_map.get(int(me.get("championId", 0)), "") if me else "",
        "enemies": [
            champ_map.get(int(p.get("championId", 0)), "Unknown")
            for p in parts
            if int(p.get("teamId", -1)) != my_tid
        ],
        "allies": [
            champ_map.get(int(p.get("championId", 0)), "Unknown")
            for p in parts
            if int(p.get("teamId", -1)) == my_tid and p.get("puuid") != puuid
        ],
        "queue_id": qid,
        "queue_name": _qname(qid),
    }


@bot.tree.command(name="build", description="Adaptive build path & runes — auto-detects live games or takes manual champs")
@app_commands.describe(
    player="Optional player to auto-detect from their live game (leave blank to auto-detect you!)",
    champion="Optional manual champion override (for champ select / theorycrafting)",
    enemies="Optional enemy champions, comma-separated (auto-filled if in a live game)",
    allies="Optional ally champions, comma-separated (auto-filled if in a live game)",
    role="Optional role",
    notes="Optional notes (e.g. 'enemy Zed is 5-0', 'need frontline', 'ARAM')",
)
@app_commands.choices(role=ROLE_CHOICES)
@app_commands.autocomplete(player=player_autocomplete)
async def slash_build(
    interaction: discord.Interaction,
    player: Optional[str] = None,
    champion: Optional[str] = None,
    enemies: Optional[str] = None,
    allies: Optional[str] = None,
    role: str = "",
    notes: str = "",
):
    if not deepseek.is_configured:
        await interaction.response.send_message(
            "⚠️ DeepSeek AI is not configured yet. Add `DEEPSEEK_API_KEY` to `.env` to unlock `/build`.",
            ephemeral=True,
        )
        return

    await interaction.response.defer()
    try:
        enemy_list = [e.strip() for e in (enemies or "").split(",") if e.strip()]
        ally_list = [a.strip() for a in (allies or "").split(",") if a.strip()]
        chosen_champ = (champion or "").strip()
        extra_notes = notes.strip()
        detected_for: Optional[str] = None

        # Auto-detect live game whenever champion wasn't manually provided (or player was passed)
        if not chosen_champ or player:
            lobby: Optional[dict] = None
            target_riot_id: Optional[str] = None

            if player:
                try:
                    target_riot_id = resolve_riot_id(player, aggregator.get_roster())
                except ValueError as e:
                    await interaction.followup.send(f"❌ {e}")
                    return
                lobby = await _detect_live_lobby(target_riot_id)
                if not lobby and not chosen_champ:
                    await interaction.followup.send(
                        f"ℹ️ `{target_riot_id.split('#', 1)[0]}` isn't currently in a live game. "
                        "Run `/build` once you're on the loading screen, or pass `champion:` manually!"
                    )
                    return
            else:
                # 1. Try caller's linked Riot ID first
                linked_id = db.get_linked_riot_id(interaction.user.id)
                if linked_id:
                    lobby = await _detect_live_lobby(linked_id)

                # 2. If not linked or not in game, scan the 4 tracked squad members
                if not lobby:
                    roster = aggregator.get_roster()
                    checked = await asyncio.gather(
                        *[_detect_live_lobby(rid) for rid in roster],
                        return_exceptions=True,
                    )
                    live_lobbies = [c for c in checked if isinstance(c, dict)]

                    if len(live_lobbies) == 1:
                        lobby = live_lobbies[0]
                    elif len(live_lobbies) > 1:
                        # Prefer a lobby whose Riot name matches caller's Discord name
                        u_names = {
                            interaction.user.name.lower(),
                            interaction.user.display_name.lower(),
                        }
                        matched = next(
                            (lb for lb in live_lobbies if lb["display_name"].lower() in u_names),
                            None,
                        )
                        if matched:
                            lobby = matched
                        else:
                            live_names = ", ".join(f"`{lb['display_name']}` ({lb['champion']})" for lb in live_lobbies)
                            await interaction.followup.send(
                                f"🟢 Multiple squadmates are in-game right now: {live_names}.\n"
                                "Run `/build player:<name>`, or run `/link <your-name>` once so plain `/build` always picks you automatically!"
                            )
                            return
                    else:
                        await interaction.followup.send(
                            "ℹ️ Nobody in the squad is currently in a live game.\n"
                            "• **In loading screen / in-game:** just run `/build` (run `/link` once so it always knows who you are)\n"
                            "• **In champ select:** pass `champion:` manually, e.g. `/build champion:Ahri enemies:Malphite, Zed, Mundo`"
                        )
                        return

            if lobby:
                detected_for = f"{lobby['display_name']} ({lobby['queue_name']})"
                if not chosen_champ:
                    chosen_champ = lobby["champion"]
                if not enemy_list:
                    enemy_list = lobby["enemies"]
                if not ally_list:
                    ally_list = lobby["allies"]
                from stats import ARAM_QUEUE_IDS
                if lobby["queue_id"] in ARAM_QUEUE_IDS and "aram" not in extra_notes.lower():
                    extra_notes = f"{lobby['queue_name']} (single-lane ARAM teamfight mode — no split-pushing, use ARAM-optimal starter/items). {extra_notes}".strip()
                elif lobby["queue_id"] in (1700, 1710) and "arena" not in extra_notes.lower():
                    extra_notes = f"Arena 2v2 mode. {extra_notes}".strip()

        if not chosen_champ:
            await interaction.followup.send("❌ Could not determine champion — please specify `champion:`.")
            return

        try:
            patch = await riot.get_ddragon_latest_version()
        except Exception:
            patch = ""

        build = await deepseek.recommend_build(
            champion=chosen_champ,
            enemies=enemy_list,
            allies=ally_list,
            role=role,
            patch=patch,
            notes=extra_notes,
        )
        if not build:
            await interaction.followup.send("❌ DeepSeek didn't return a build — try again in a second.")
            return

        all_item_names = []
        if build.get("starter", {}).get("item"):
            all_item_names.append(build["starter"]["item"])
        if build.get("boots", {}).get("item"):
            all_item_names.append(build["boots"]["item"])
        for c in build.get("core", []):
            if c.get("item"):
                all_item_names.append(c["item"])
        for s in build.get("situational", []):
            if s.get("item"):
                all_item_names.append(s["item"])

        patch_ver, item_id_map = await aggregator.resolve_item_ids_by_names(all_item_names)
        card_buf = await render_build_card(
            champion=chosen_champ,
            build=build,
            item_ids=item_id_map,
            assets=assets,
            enemies=enemy_list,
            allies=ally_list,
            role=role,
            patch=patch_ver or patch,
            subtitle_extra=detected_for or "",
        )
        await interaction.followup.send(
            file=discord.File(card_buf, filename=f"{chosen_champ.lower()}_build.png"),
        )
    except Exception as e:
        logger.exception(f"Build recommendation failed: {e}")
        await interaction.followup.send(f"❌ Build recommendation failed: {e}")


# -------------------------------------------------------------------------
# DeepSeek AI Coaching & Analysis Commands
# -------------------------------------------------------------------------

@bot.tree.command(name="review", description="Timeline-powered VOD review of a recent match")
@app_commands.describe(
    player="Player nickname (tracked) or full Name#TAG",
    game_ago="0 = latest game (default), 1 = game before that, etc.",
)
@app_commands.autocomplete(player=player_autocomplete)
async def slash_review(
    interaction: discord.Interaction,
    player: str,
    game_ago: app_commands.Range[int, 0, 5] = 0,
):
    try:
        resolved = resolve_riot_id(player, aggregator.get_roster())
    except ValueError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        report, err = await aggregator.get_match_review(resolved, match_offset=game_ago)
        if err or not report:
            await interaction.followup.send(f"❌ Could not review match for `{resolved}`: {err or 'Not found'}")
            return
        review_text, (_, item_id_map) = await asyncio.gather(
            deepseek.review_match_timeline(report) if deepseek.is_configured else asyncio.sleep(0, result=None),
            aggregator.resolve_item_ids_by_names(report.final_items),
        )
        card_buf = await render_match_card(
            display_name=report.display_name,
            champion=report.champion,
            win=report.win,
            queue_name=report.queue_name,
            duration_seconds=report.duration_seconds,
            kills=report.kills,
            deaths=report.deaths,
            assists=report.assists,
            cs=report.cs,
            cs_per_min=report.cs_per_min,
            damage=report.damage_to_champions,
            damage_share=report.damage_share,
            kp=report.kill_participation,
            vision=report.vision_score,
            gold=report.gold_earned,
            assets=assets,
            item_ids=[item_id_map.get(n) or 0 for n in report.final_items],
            lane_opponent=report.lane_opponent_champ,
            gold_diff_10=report.gold_diff_10,
            gold_diff_15=report.gold_diff_15,
            role=report.role,
        )
        # Send the VOD match card + a lean coaching-only embed (no duplicate scoreboard fields)
        vod_embed = None
        flags = [f"⚠️ {d}" for d in report.objective_throw_deaths[:3]] + [
            f"💀 {c}" for c in report.death_clusters[:2]
        ]
        if review_text or flags:
            vod_embed = discord.Embed(
                title=f"🧠 Coach Jeff's VOD Breakdown — {report.display_name}",
                description=(review_text or "")[:4096] or None,
                color=0x22C55E if report.win else 0xC62B2B,
            )
            if flags:
                vod_embed.add_field(name="🚩 Key Moments", value="\n".join(flags)[:1024], inline=False)
            vod_embed.set_footer(text=f"Match {report.match_id} • Powered by Riot Timeline + DeepSeek")

        if vod_embed:
            await interaction.followup.send(
                file=discord.File(card_buf, filename=f"{report.display_name}_vod.png"),
                embed=vod_embed,
            )
        else:
            await interaction.followup.send(
                file=discord.File(card_buf, filename=f"{report.display_name}_vod.png"),
            )
    except Exception as e:
        await interaction.followup.send(f"❌ VOD review failed: {e}")


@bot.tree.command(name="coach", description="Wins-vs-Losses coaching breakdown from Coach Jeff")
@app_commands.describe(
    player="Player nickname (tracked) or full Name#TAG",
    days="Lookback window in days (default: 14)",
)
@app_commands.autocomplete(player=player_autocomplete)
async def slash_coach(
    interaction: discord.Interaction,
    player: str,
    days: app_commands.Range[int, 3, 30] = 14,
):
    try:
        resolved = resolve_riot_id(player, aggregator.get_roster())
    except ValueError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        ps = await aggregator.get_player_snapshot(resolved, days=days)
        if ps.error:
            await interaction.followup.send(f"❌ Could not fetch stats for `{resolved}`: {ps.error}")
            return
        if ps.games_played == 0:
            await interaction.followup.send(
                f"📋 `{ps.display_name}` has 0 games in the last {days} days — queue up first!"
            )
            return
        split = compute_win_loss_split(ps)
        pool = compute_champion_pool_audit(ps)
        advice, card_buf = await asyncio.gather(
            deepseek.coach_player(ps, split, pool) if deepseek.is_configured else asyncio.sleep(0, result=None),
            render_coach_card(ps, split, assets, days=days),
        )
        file = discord.File(card_buf, filename=f"{ps.display_name}_coach.png")
        if advice:
            tip_embed = discord.Embed(
                title=f"🧠 Coach Jeff's Takeaways — {ps.display_name}",
                description=advice[:4096],
                color=0x0BC4E3,
            )
            tip_embed.set_footer(text="Powered by DeepSeek • Use /pool for champ audit or /goal for a weekly target")
            await interaction.followup.send(file=file, embed=tip_embed)
        else:
            await interaction.followup.send(file=file)
    except Exception as e:
        await interaction.followup.send(f"❌ Coaching lookup failed: {e}")


@bot.tree.command(name="pool", description="Audit your champion pool: Climb Champs vs LP Bleed traps")
@app_commands.describe(
    player="Player nickname (tracked) or full Name#TAG",
    days="Lookback window in days (default: 30)",
)
@app_commands.autocomplete(player=player_autocomplete)
async def slash_pool(
    interaction: discord.Interaction,
    player: str,
    days: app_commands.Range[int, 7, 60] = 30,
):
    try:
        resolved = resolve_riot_id(player, aggregator.get_roster())
    except ValueError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        ps = await aggregator.get_player_snapshot(resolved, days=days)
        if ps.error:
            await interaction.followup.send(f"❌ Could not fetch stats for `{resolved}`: {ps.error}")
            return
        if ps.games_played == 0:
            await interaction.followup.send(f"🎯 `{ps.display_name}` has 0 games in the last {days} days.")
            return
        pool = compute_champion_pool_audit(ps)
        verdict, card_buf = await asyncio.gather(
            deepseek.audit_pool_verdict(ps, pool) if deepseek.is_configured else asyncio.sleep(0, result=None),
            render_pool_card(ps, pool, assets, days=days),
        )
        content = f"🧠 **Coach Jeff's Verdict:** {verdict}" if verdict else None
        await interaction.followup.send(
            content=content,
            file=discord.File(card_buf, filename=f"{ps.display_name}_pool.png"),
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Pool audit failed: {e}")


@bot.tree.command(name="goal", description="View or generate your weekly measurable coaching goal")
@app_commands.describe(
    player="Player nickname (tracked) or full Name#TAG",
    refresh="Generate a brand-new goal right now",
)
@app_commands.autocomplete(player=player_autocomplete)
async def slash_goal(
    interaction: discord.Interaction,
    player: str,
    refresh: bool = False,
):
    try:
        resolved = resolve_riot_id(player, aggregator.get_roster())
    except ValueError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        ps_14 = await aggregator.get_player_snapshot(resolved, days=14)
        if ps_14.error or not ps_14.puuid:
            await interaction.followup.send(f"❌ Could not fetch stats for `{resolved}`: {ps_14.error}")
            return

        active = db.get_active_goal(ps_14.puuid)
        is_new = False
        if not active or refresh:
            picked = pick_next_coaching_goal(ps_14)
            if not picked:
                await interaction.followup.send(
                    f"🎯 `{ps_14.display_name}` needs at least 1 recent game to set a coaching goal."
                )
                return
            active = db.set_coaching_goal(ps_14.puuid, resolved, **picked)
            is_new = True

        # Measure progress using matches played since the goal was created
        progress_ps = await aggregator._fetch_player_stats(resolved, int(active["created_at"]))
        ev = evaluate_goal_progress(active, progress_ps)
        card_buf = await render_goal_card(
            ps_14.display_name, ps_14.profile_icon_id, ev, assets, is_new=is_new
        )
        await interaction.followup.send(
            file=discord.File(card_buf, filename=f"{ps_14.display_name}_goal.png")
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Goal check failed: {e}")


@bot.tree.command(name="scout", description="Loading-screen scout & DeepSeek gameplan for a live game")
@app_commands.describe(player="Player currently in a live game")
@app_commands.autocomplete(player=player_autocomplete)
async def slash_scout(interaction: discord.Interaction, player: str):
    try:
        resolved = resolve_riot_id(player, aggregator.get_roster())
    except ValueError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        scout = await aggregator.get_live_scout_data(resolved)
        if scout.get("error") or not scout.get("in_game"):
            await interaction.followup.send(embed=build_scout_embed(scout, None))
            return
        gameplan, card_buf = await asyncio.gather(
            deepseek.scout_live_game(scout) if deepseek.is_configured else asyncio.sleep(0, result=None),
            render_scout_card(scout, assets),
        )
        file = discord.File(card_buf, filename=f"{scout['display_name']}_scout.png")
        if gameplan:
            gp_embed = discord.Embed(
                title=f"🧠 Loading-Screen Gameplan — {scout['display_name']}",
                description=gameplan[:4096],
                color=0x9B59B6,
            )
            gp_embed.set_footer(text="Powered by DeepSeek • Run /build for full item path card")
            await interaction.followup.send(file=file, embed=gp_embed)
            voice_manager.speak_if_connected(interaction.guild_id, gameplan)
        else:
            await interaction.followup.send(file=file)
    except Exception as e:
        await interaction.followup.send(f"❌ Scout failed: {e}")


@bot.tree.command(name="debrief", description="Post-game squad debrief for your most recent group game")
async def slash_debrief(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        debriefs = await aggregator.find_squad_games(only_unannounced=False, lookback_per_player=10)
        if not debriefs:
            await interaction.followup.send("*No recent games found with 2+ squadmates on the same team.*")
            return
        latest = debriefs[-1]
        commentary, card_buf = await asyncio.gather(
            deepseek.debrief_squad_game(latest) if deepseek.is_configured else asyncio.sleep(0, result=None),
            render_debrief_card(latest, assets),
        )
        content = f"🧠 **Jeff's Squad Debrief:** {commentary}" if commentary else None
        await interaction.followup.send(
            content=content,
            file=discord.File(card_buf, filename=f"squad_debrief_{latest['match_id']}.png"),
        )
        if commentary:
            voice_manager.speak_if_connected(interaction.guild_id, commentary)
    except Exception as e:
        await interaction.followup.send(f"❌ Squad debrief failed: {e}")


@bot.tree.command(name="comp", description="Build synergistic 4-man squad comps from your real champ pools")
@app_commands.describe(notes="Optional constraints (e.g. 'teamfight comp', 'enemy locked Malphite')")
async def slash_comp(interaction: discord.Interaction, notes: str = ""):
    if not deepseek.is_configured:
        await interaction.response.send_message(
            "⚠️ DeepSeek AI is not configured yet. Add `DEEPSEEK_API_KEY` to `.env` to unlock `/comp`.",
            ephemeral=True,
        )
        return
    await interaction.response.defer()
    try:
        context = await aggregator.build_squad_context(days=14)
        suggestions = await deepseek.suggest_squad_comps(context, notes=notes)
        if not suggestions:
            await interaction.followup.send("❌ DeepSeek didn't return comp ideas — check bot logs.")
            return
        await interaction.followup.send(embed=build_comp_embed(suggestions, notes=notes))
    except Exception as e:
        await interaction.followup.send(f"❌ Comp builder failed: {e}")


@bot.tree.command(name="ask", description="Ask Coach Jeff anything about your squad's stats, builds, or macro")
@app_commands.describe(question="Your question (e.g. 'Why do I keep losing on Ahri?' or 'What counters Mundo?')")
async def slash_ask(interaction: discord.Interaction, question: str):
    if not deepseek.is_configured:
        await interaction.response.send_message(
            "⚠️ DeepSeek AI is not configured yet. Add `DEEPSEEK_API_KEY` to `.env` to unlock `/ask`.",
            ephemeral=True,
        )
        return
    await interaction.response.defer()
    try:
        linked_riot_id = db.get_linked_riot_id(interaction.user.id)
        context, live_res, patch_ver = await asyncio.gather(
            aggregator.build_squad_context(days=14),
            aggregator.build_live_match_context(linked_riot_id),
            aggregator.get_current_patch(),
            return_exceptions=True,
        )
        squad_ctx = context if isinstance(context, str) else ""
        live_ctx = live_res[0] if isinstance(live_res, tuple) else ""
        patch_str = patch_ver if isinstance(patch_ver, str) else ""
        answer = await deepseek.answer_question(
            question,
            squad_ctx,
            interaction.user.display_name,
            speaker_riot_id=linked_riot_id,
            live_match_context=live_ctx or None,
            patch_version=patch_str or None,
        )
        if not answer:
            await interaction.followup.send("❌ DeepSeek didn't answer — check bot logs.")
            return
        await interaction.followup.send(
            embed=build_ask_embed(question, answer, interaction.user.display_name)
        )
        voice_manager.speak_if_connected(interaction.guild_id, answer)
    except Exception as e:
        await interaction.followup.send(f"❌ Ask failed: {e}")


@bot.tree.command(name="roast", description="Let Jeff roast a player using their last 7 days of stats")
@app_commands.describe(player="Player nickname (tracked) or full Name#TAG")
@app_commands.autocomplete(player=player_autocomplete)
async def slash_roast(interaction: discord.Interaction, player: str):
    if not deepseek.is_configured:
        await interaction.response.send_message(
            "⚠️ DeepSeek AI is not configured yet. Add `DEEPSEEK_API_KEY` to `.env` to unlock `/roast`.",
            ephemeral=True,
        )
        return
    try:
        resolved = resolve_riot_id(player, aggregator.get_roster())
    except ValueError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        ps = await aggregator.get_player_snapshot(resolved, days=WEEKLY_MATCH_LOOKBACK_DAYS)
        if ps.error:
            await interaction.followup.send(f"❌ Could not fetch stats for `{resolved}`: {ps.error}")
            return
        roast = await deepseek.roast_player(ps)
        if not roast:
            await interaction.followup.send("❌ DeepSeek didn't return a roast — check bot logs.")
            return
        await interaction.followup.send(embed=build_ai_embed(ps, roast, mode="roast"))
        voice_manager.speak_if_connected(interaction.guild_id, roast)
    except Exception as e:
        await interaction.followup.send(f"❌ Roast failed: {e}")


# -------------------------------------------------------------------------
# Group Comparison, Synergy, Live & Mastery Commands
# -------------------------------------------------------------------------

@bot.tree.command(name="versus", description="Head-to-head comparison of two players (last 7 days)")
@app_commands.describe(player1="First player", player2="Second player")
@app_commands.autocomplete(player1=player_autocomplete, player2=player_autocomplete)
async def slash_versus(interaction: discord.Interaction, player1: str, player2: str):
    roster = aggregator.get_roster()
    try:
        p1 = resolve_riot_id(player1, roster)
        p2 = resolve_riot_id(player2, roster)
    except ValueError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=WEEKLY_MATCH_LOOKBACK_DAYS)
        start_epoch = int(cutoff.timestamp())
        a, b = await asyncio.gather(
            aggregator._fetch_player_stats(p1, start_epoch),
            aggregator._fetch_player_stats(p2, start_epoch),
        )
        if a.error or b.error:
            await interaction.followup.send(embed=build_versus_embed(a, b))
            return
        card_buf = await render_versus_card(a, b, assets)
        await interaction.followup.send(
            file=discord.File(card_buf, filename=f"{a.display_name}_vs_{b.display_name}.png")
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Versus lookup failed: {e}")


@bot.tree.command(name="damage", description="Weekly damage-to-champions leaderboard")
async def slash_damage(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        stats = await aggregator.get_weekly_stats()
        card_buf = await render_damage_card(stats, assets)
        await interaction.followup.send(file=discord.File(card_buf, filename="weekly_damage.png"))
    except Exception as e:
        await interaction.followup.send(f"❌ Could not fetch damage stats: {e}")


@bot.tree.command(name="synergy", description="Squad & duo win rates over the last N days")
@app_commands.describe(days="Lookback window in days (default: 7)")
async def slash_synergy(
    interaction: discord.Interaction, days: app_commands.Range[int, 1, 30] = 7
):
    await interaction.response.defer()
    try:
        data = await aggregator.get_synergy_stats(days=days)
        card_buf = await render_synergy_card(data, assets)
        await interaction.followup.send(file=discord.File(card_buf, filename="squad_synergy.png"))
    except Exception as e:
        await interaction.followup.send(f"❌ Could not compute synergy: {e}")


@bot.tree.command(name="live", description="See who in the squad is currently in a match")
async def slash_live(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        games = await aggregator.get_live_games()
        await interaction.followup.send(embed=build_live_embed(games))
    except Exception as e:
        await interaction.followup.send(f"❌ Live check failed: {e}")


@bot.tree.command(name="mastery", description="Show top 10 champion mastery for a player")
@app_commands.describe(player="Player nickname (tracked) or full Name#TAG")
@app_commands.autocomplete(player=player_autocomplete)
async def slash_mastery(interaction: discord.Interaction, player: str):
    try:
        resolved = resolve_riot_id(player, aggregator.get_roster())
    except ValueError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        data = await aggregator.get_champion_mastery(resolved, count=10)
        if data.get("error"):
            await interaction.followup.send(embed=build_mastery_embed(data))
            return
        card_buf = await render_mastery_card(data, assets)
        await interaction.followup.send(
            file=discord.File(card_buf, filename=f"{data['display_name']}_mastery.png")
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Could not fetch mastery for `{resolved}`: {e}")


# -------------------------------------------------------------------------
# Roster Management Group
# -------------------------------------------------------------------------

roster_group = app_commands.Group(name="roster", description="Manage tracked players")


@roster_group.command(name="list", description="Show all tracked Riot IDs")
async def roster_list(interaction: discord.Interaction):
    players = aggregator.get_roster()
    if not players:
        await interaction.response.send_message("No players tracked yet. Use `/roster add Name#TAG`.")
        return
    formatted = "\n".join(f"• `{p}`" for p in players)
    await interaction.response.send_message(f"**Tracked Squad ({len(players)}):**\n{formatted}")


@roster_group.command(name="add", description="Add a Riot ID to the tracked squad")
@app_commands.describe(riot_id="Full Riot ID in Name#TAG format")
async def roster_add(interaction: discord.Interaction, riot_id: str):
    if "#" not in riot_id or not all(p.strip() for p in riot_id.split("#", 1)):
        await interaction.response.send_message("❌ Please use full `Name#TAG` format.", ephemeral=True)
        return
    cleaned = "#".join(p.strip() for p in riot_id.split("#", 1))
    await interaction.response.defer()
    try:
        await aggregator._resolve_player(cleaned)
    except Exception as e:
        await interaction.followup.send(f"❌ Could not verify `{cleaned}` with Riot: {e}")
        return
    added = db.add_to_roster(cleaned)
    if added:
        await interaction.followup.send(f"✅ Added `{cleaned}` to the tracked squad!")
    else:
        await interaction.followup.send(f"ℹ️ `{cleaned}` is already on the roster.")


@roster_group.command(name="remove", description="Remove a player from the tracked squad")
@app_commands.describe(player="Nickname or full Name#TAG")
@app_commands.autocomplete(player=player_autocomplete)
async def roster_remove(interaction: discord.Interaction, player: str):
    removed = db.remove_from_roster(player)
    if removed:
        await interaction.response.send_message(f"🗑️ Removed `{removed}` from the tracked squad.")
    else:
        await interaction.response.send_message(f"❌ `{player}` is not on the roster.", ephemeral=True)


bot.tree.add_command(roster_group)


# -------------------------------------------------------------------------
# Hybrid Voice Command Group (/voice)
# -------------------------------------------------------------------------

voice_group = app_commands.Group(
    name="voice",
    description="Let Jeff join voice chat and talk using your Desktop GPU / CM3588 hybrid TTS",
)


async def voice_name_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    voices = await tts_client.list_voices()
    q = current.lower().strip()
    matches = [v for v in voices if not q or q in v.lower()]
    return [app_commands.Choice(name=v, value=v) for v in matches[:25]]


@voice_group.command(name="join", description="Bring Jeff Bot into your current voice channel")
async def voice_join(interaction: discord.Interaction):
    user_voice = getattr(interaction.user, "voice", None)
    if not user_voice or not user_voice.channel:
        await interaction.response.send_message(
            "❌ Hop into a voice channel first, then run `/voice join`.",
            ephemeral=True,
        )
        return
    await interaction.response.defer(ephemeral=True)
    will_listen = await voice_listener.should_join_undeafened()
    ok, msg = await voice_manager.join(user_voice.channel, self_deaf=not will_listen)
    if not ok:
        await interaction.followup.send(f"❌ {msg}", ephemeral=True)
        return

    status = await tts_client.get_status()
    backend_label = status["active_backend"]
    listening_now = bool(interaction.guild_id and voice_listener.is_listening(interaction.guild_id))
    if listening_now:
        listen_label = 'ON (say "Hey Jeff")'
    elif voice_listener.desired_enabled:
        listen_label = "STANDBY (PC STT offline)"
    else:
        listen_label = "OFF (`/voice listen on`)"

    await interaction.followup.send(
        f"🎙️ {msg}\n"
        f"**Active TTS:** {backend_label} • **Voice:** `{tts_client.active_voice}` • "
        f"**Auto-speak:** {'ON' if voice_manager.auto_speak else 'OFF'}\n"
        f"**Listening:** {listen_label}",
        ephemeral=True,
    )
    if tts_client.has_any_backend and interaction.guild_id:
        greeting = (
            "Yo, Jeff is in the call. [chuckle] Don't feed."
            if tts_client.primary_supports_tags
            else "Yo, Jeff is in the call. Don't feed."
        )
        voice_manager.speak_if_connected(interaction.guild_id, greeting)


@voice_group.command(name="leave", description="Disconnect Jeff Bot from voice chat")
async def voice_leave(interaction: discord.Interaction):
    if not interaction.guild_id:
        await interaction.response.send_message("❌ Run this inside a server.", ephemeral=True)
        return
    ok, msg = await voice_manager.leave(interaction.guild_id)
    prefix = "👋" if ok else "ℹ️"
    await interaction.response.send_message(f"{prefix} {msg}", ephemeral=True)


@voice_group.command(name="say", description="Make Jeff say something out loud in voice chat")
@app_commands.describe(
    text="What Jeff should say (Chatterbox supports [laugh], [chuckle], [sigh])",
    voice="Optional cloned voice clip name from your desktop voice_worker/voices/ folder",
)
@app_commands.autocomplete(voice=voice_name_autocomplete)
async def voice_say(interaction: discord.Interaction, text: str, voice: Optional[str] = None):
    if not interaction.guild_id:
        await interaction.response.send_message("❌ Run this inside a server.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    if not voice_manager.is_connected(interaction.guild_id):
        user_voice = getattr(interaction.user, "voice", None)
        if not user_voice or not user_voice.channel:
            await interaction.followup.send("❌ Jeff isn't in a voice channel—join one first or run `/voice join`.", ephemeral=True)
            return
        will_listen = await voice_listener.should_join_undeafened()
        ok, msg = await voice_manager.join(user_voice.channel, self_deaf=not will_listen)
        if not ok:
            await interaction.followup.send(f"❌ {msg}", ephemeral=True)
            return

    if not tts_client.has_any_backend:
        await interaction.followup.send(
            "⚠️ No TTS backend is configured yet. Set `VOICE_WORKER_URL`, `VOICE_FALLBACK_TTS_URL`, or `ELEVENLABS_API_KEY` in `.env`.",
            ephemeral=True,
        )
        return

    res = await voice_manager.speak(interaction.guild_id, text, voice=voice)
    if not res:
        await interaction.followup.send("❌ All configured TTS backends are unreachable right now — run `/voice status`.", ephemeral=True)
        return

    await interaction.followup.send(
        f"🗣️ *\"{text[:200]}\"*\n"
        f"`{res.backend}` • voice `{res.voice}` • `{res.latency_ms}ms`",
        ephemeral=True,
    )


@voice_group.command(name="ask", description="Ask Jeff a quick question and hear his spoken reply in VC")
@app_commands.describe(question="Question for Jeff (e.g. 'Who is inting the hardest this week?')")
async def voice_ask(interaction: discord.Interaction, question: str):
    if not deepseek.is_configured:
        await interaction.response.send_message(
            "⚠️ DeepSeek AI is not configured yet (`DEEPSEEK_API_KEY`).",
            ephemeral=True,
        )
        return
    if not interaction.guild_id:
        await interaction.response.send_message("❌ Run this inside a server.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    if not voice_manager.is_connected(interaction.guild_id):
        user_voice = getattr(interaction.user, "voice", None)
        if user_voice and user_voice.channel:
            will_listen = await voice_listener.should_join_undeafened()
            await voice_manager.join(user_voice.channel, self_deaf=not will_listen)

    try:
        linked_riot_id = db.get_linked_riot_id(interaction.user.id)
        context, live_res, patch_ver = await asyncio.gather(
            aggregator.build_squad_context(days=14),
            aggregator.build_live_match_context(linked_riot_id),
            aggregator.get_current_patch(),
            return_exceptions=True,
        )
        squad_ctx = context if isinstance(context, str) else ""
        live_ctx = live_res[0] if isinstance(live_res, tuple) else ""
        patch_str = patch_ver if isinstance(patch_ver, str) else ""
        reply = await deepseek.answer_voice_question(
            question,
            squad_ctx,
            interaction.user.display_name,
            allow_paralinguistic=tts_client.primary_supports_tags,
            speaker_riot_id=linked_riot_id,
            live_match_context=live_ctx or None,
            patch_version=patch_str or None,
        )
        if not reply:
            await interaction.followup.send("❌ DeepSeek didn't answer — check bot logs.", ephemeral=True)
            return

        res = (
            await voice_manager.speak(interaction.guild_id, reply)
            if voice_manager.is_connected(interaction.guild_id)
            else None
        )
        embed = build_ask_embed(question, reply, interaction.user.display_name)
        if res:
            embed.set_footer(text=f"Spoken via {res.backend} ({res.voice}) in {res.latency_ms}ms")
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Voice ask failed: {e}", ephemeral=True)


@voice_group.command(name="set", description="Switch Jeff's active cloned voice")
@app_commands.describe(voice="Voice name (filename stem in voice_worker/voices/)")
@app_commands.autocomplete(voice=voice_name_autocomplete)
async def voice_set(interaction: discord.Interaction, voice: str):
    cleaned = voice.strip().lower()
    if not cleaned:
        await interaction.response.send_message("❌ Provide a voice name.", ephemeral=True)
        return
    tts_client.active_voice = cleaned
    await interaction.response.send_message(f"🎛️ Switched Jeff's voice to **`{cleaned}`**!", ephemeral=True)


@voice_group.command(name="autospeak", description="Toggle whether Jeff automatically speaks /scout, /roast, /debrief & /ask in VC")
@app_commands.describe(enabled="True to auto-speak while connected to VC, False for /voice say & /voice ask only")
async def voice_autospeak(interaction: discord.Interaction, enabled: bool):
    voice_manager.auto_speak = enabled
    state_str = "ON — Jeff will speak `/scout`, `/roast`, `/debrief`, and `/ask` while in VC." if enabled else "OFF — Jeff will only speak on `/voice say` and `/voice ask`."
    await interaction.response.send_message(f"🔊 Auto-speak is now **{state_str}**", ephemeral=True)


@voice_group.command(name="listen", description='Toggle hands-free voice listening ("Hey Jeff")')
@app_commands.describe(mode="Turn voice listening on or off")
@app_commands.choices(
    mode=[
        app_commands.Choice(name="on", value="on"),
        app_commands.Choice(name="off", value="off"),
    ]
)
async def voice_listen(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True)
    enabled = mode.value == "on"
    res = await voice_listener.set_enabled(enabled, guild_id=interaction.guild_id)
    stt = res["stt"]

    if not enabled:
        await interaction.followup.send(
            "🔇 Voice listening is now **OFF** (Jeff is self-deafened; typed `/voice` commands still work).",
            ephemeral=True,
        )
        return

    if res["active"]:
        await interaction.followup.send(
            f'👂 Voice listening is **ON** — say **"Hey Jeff ..."** in voice chat (or **"Jeff stop"** to cut him off).\n'
            f"STT: `{stt['detail']}`",
            ephemeral=True,
        )
    elif stt["available"]:
        await interaction.followup.send(
            f'👂 Voice listening is **ON** and will activate as soon as Jeff joins a voice channel (`/voice join`).\n'
            f"STT: `{stt['detail']}`",
            ephemeral=True,
        )
    else:
        await interaction.followup.send(
            f"⚠️ Voice listening is **ON (standby)**, but PC STT isn't reachable right now (`{stt['detail']}`).\n"
            "Jeff will stay deafened until your PC voice worker comes online, then start listening automatically.",
            ephemeral=True,
        )


@voice_group.command(name="status", description="Show hybrid voice backend health (5070 Ti GPU vs CM3588 fallback)")
async def voice_status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    prereqs_ok, missing = check_voice_prereqs()
    st, listen_st = await asyncio.gather(
        tts_client.get_status(),
        voice_listener.get_status(interaction.guild_id),
    )
    ch = voice_manager.connected_channel(interaction.guild_id) if interaction.guild_id else None

    embed = discord.Embed(
        title="🎙️ Jeff Bot Hybrid Voice Status",
        color=0x22C55E if st["active_backend"] != "None online" and prereqs_ok else 0xC89B3C,
    )
    embed.add_field(
        name="Voice Channel",
        value=f"🟢 Connected to **{ch.name}**" if ch else "⚪ Not connected (`/voice join`)",
        inline=True,
    )
    embed.add_field(
        name="Active Route",
        value=f"**{st['active_backend']}**\nVoice: `{st['active_voice']}` • Auto-speak: `{'ON' if voice_manager.auto_speak else 'OFF'}`",
        inline=True,
    )

    stt = listen_st["stt"]
    if listen_st["active"]:
        listen_state = '🟢 Active (say "Hey Jeff")'
    elif listen_st["desired_enabled"] and stt["available"]:
        listen_state = "🟡 Ready (join VC to listen)"
    elif listen_st["desired_enabled"]:
        listen_state = "🟡 Standby (PC STT offline)"
    else:
        listen_state = "⚪ Off (`/voice listen on`)"

    stt_icon = "✅" if stt["available"] else ("⚪" if not stt["configured"] else "🔴")
    dave_info = ""
    if listen_st.get("dave"):
        d = listen_st["dave"]
        dave_info = f"\nDAVE v{d.get('dave_protocol_version', '?')}: `{d.get('ok', 0)}` decrypted, `{d.get('fail', 0)}` failed"
    embed.add_field(
        name='Voice Listening ("Hey Jeff")',
        value=f"**Listener:** {listen_state}\n**STT:** {stt_icon} {stt['detail']}{dave_info}",
        inline=False,
    )

    p = st["primary"]
    f = st["fallback"]
    el = st["elevenlabs"]
    embed.add_field(
        name="TTS Backends (Failover Order)",
        value=(
            f"1. **Desktop GPU:** {'✅' if p['ok'] else ('⚪' if not p['configured'] else '🔴')} {p['detail']}\n"
            f"2. **CM3588 Kokoro:** {'✅' if f['ok'] else ('⚪' if not f['configured'] else '🔴')} {f['detail']}\n"
            f"3. **ElevenLabs:** {'✅' if el['ok'] else '⚪'} {el['detail']}"
        ),
        inline=False,
    )
    if st["voices"]:
        embed.add_field(
            name="Cloned Voices Available",
            value=", ".join(f"`{v}`" for v in st["voices"]),
            inline=False,
        )
    if not prereqs_ok:
        embed.add_field(
            name="⚠️ Missing Voice Dependencies",
            value="\n".join(f"• {m}" for m in missing),
            inline=False,
        )
    last = st.get("last_result")
    if last:
        embed.set_footer(
            text=f"Last line: {last['backend']} ({last['voice']}) in {last['latency_ms']}ms • Session total: {st['total_chars_spoken']} chars"
        )
    await interaction.followup.send(embed=embed, ephemeral=True)


bot.tree.add_command(voice_group)


# -------------------------------------------------------------------------
# Ops & Help Commands
# -------------------------------------------------------------------------

@bot.tree.command(name="setriotkey", description="Hot-swap the Riot API key without restarting (admin)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(key="Your new RGAPI-... key from developer.riotgames.com")
async def slash_setriotkey(interaction: discord.Interaction, key: str):
    await interaction.response.defer(ephemeral=True)
    old_key = riot.api_key
    riot.set_api_key(key)
    ok, msg = await riot.verify_key()
    if not ok:
        riot.set_api_key(old_key)
        await interaction.followup.send(f"❌ Key rejected by Riot ({msg}). Kept previous key.", ephemeral=True)
        return
    try:
        _update_env_var(ENV_PATH, "RIOT_API_KEY", key.strip())
    except OSError as e:
        await interaction.followup.send(
            f"✅ Key active in memory ({msg}), but could not write `.env`: {e}",
            ephemeral=True,
        )
        return
    await interaction.followup.send(f"✅ Riot API key updated & saved to `.env` ({msg})!", ephemeral=True)


@bot.tree.command(name="status", description="Check Jeff Bot health, API keys, and schedule")
async def slash_status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    (riot_ok, riot_msg), (ai_ok, ai_msg), voice_st = await asyncio.gather(
        riot.verify_key(),
        deepseek.verify_key(),
        tts_client.get_status(),
    )
    next_runs = []
    for job in scheduler.get_jobs():
        if job.next_run_time:
            next_runs.append(f"• **{job.id}**: {job.next_run_time.strftime('%a %b %d, %I:%M %p %Z')}")

    channel = bot.get_channel(CHANNEL_ID) if CHANNEL_ID else None
    channel_label = f"#{channel.name}" if channel and hasattr(channel, "name") else (str(CHANNEL_ID) if CHANNEL_ID else "Not set")

    prereqs_ok, _ = check_voice_prereqs()
    voice_icon = "✅" if (prereqs_ok and voice_st["active_backend"] != "None online") else "⚪"
    voice_msg = f"{voice_icon} {voice_st['active_backend']} (voice: `{voice_st['active_voice']}`)"

    embed = build_status_embed({
        "riot_ok": riot_ok,
        "riot_msg": riot_msg,
        "ai_ok": ai_ok,
        "ai_msg": ai_msg,
        "voice_msg": voice_msg,
        "roster": aggregator.get_roster(),
        "cached_matches": db.count_cached_matches(),
        "channel_label": channel_label,
        "next_runs": next_runs,
    })
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="patch", description="Render an ASCII card of the latest LoL patch buffs, nerfs & squad impact")
@app_commands.describe(refresh="Re-generate the AI summary instead of using cached notes")
async def slash_patch(interaction: discord.Interaction, refresh: bool = False):
    await interaction.response.defer()
    try:
        info, summary, err = await build_latest_patch_payload(force_refresh=refresh)
        if err or not info or not summary:
            await interaction.followup.send(f"❌ {err or 'Could not load patch notes.'}")
            return
        card_buf = await render_patch_card(summary, assets, publish_date=info.get("publish_date", ""))
        await interaction.followup.send(
            content=f"📜 **Full Patch {info['version']} Notes:** <{info['url']}>",
            file=discord.File(card_buf, filename=f"patch_{info['version']}.png"),
        )
    except Exception as e:
        logger.exception(f"Patch command failed: {e}")
        await interaction.followup.send(f"❌ Could not load patch notes: {e}")


@bot.tree.command(name="lolhelp", description="Show all Jeff Bot commands")
async def slash_help(interaction: discord.Interaction):
    embed = discord.Embed(title="📋 Jeff Bot Commands", color=0xC89B3C)
    embed.add_field(
        name="🧠 AI Coaching & Scouting (DeepSeek)",
        value=(
            "`/patch [refresh]` — Latest LoL patch buffs, nerfs & squad impact card\n"
            "`/build [player] [champion] [enemies]` — Adaptive item build card & runes\n"
            "`/review player [game_ago]` — Timeline VOD review card (@10/@15 diffs, throws, build)\n"
            "`/coach player [days]` — Wins vs. Losses coaching breakdown\n"
            "`/pool player [days]` — Climb Champs vs. LP Bleed audit\n"
            "`/goal player [refresh]` — Track your weekly measurable coaching goal\n"
            "`/scout player` — Live loading-screen scout & 30s gameplan\n"
            "`/comp [notes]` — Build synergistic 4-man comps from your squad's pools\n"
            "`/ask question` — Ask Coach Jeff anything backed by your squad's stats\n"
            "`/roast player` — Playful data-backed roast"
        ),
        inline=False,
    )
    embed.add_field(
        name="📊 Stats & Squad",
        value=(
            "`/stats player [days] [queue]` — Player stats & rank\n"
            "`/lastgame player` — Most recent game breakdown\n"
            "`/debrief` — Combined squad scoreboard for your latest group game\n"
            "`/synergy [days]` — Win rates together vs. solo & duo pairs\n"
            "`/versus player1 player2` — 7-day head-to-head\n"
            "`/damage` • `/live` • `/mastery player`"
        ),
        inline=False,
    )
    embed.add_field(
        name="🎙️ Hybrid Voice",
        value=(
            "`/voice join` • `/voice leave` — Bring Jeff into your voice channel\n"
            '`/voice listen on|off` — Hands-free **"Hey Jeff"** wake-word listening (say **"Jeff stop"** to interrupt)\n'
            "`/voice say text [voice]` • `/voice ask question` — Make Jeff talk or answer on mic\n"
            "`/voice set voice` • `/voice autospeak` • `/voice status` — Swap cloned voices & check GPU/STT/fallback"
        ),
        inline=False,
    )
    embed.add_field(
        name="⚙️ Admin & Ops",
        value="`/link player` • `/roster list | add | remove` • `/daily` • `/weekly` • `/setriotkey` • `/status`",
        inline=False,
    )
    embed.set_footer(
        text=f"Rank alerts every {RANK_CHECK_INTERVAL_HOURS}h • Auto squad debriefs every {SQUAD_CHECK_INTERVAL_MINUTES}m"
    )
    await interaction.response.send_message(embed=embed)


# -------------------------------------------------------------------------
# Preflight Check CLI (`python bot.py --check`)
# -------------------------------------------------------------------------

async def run_preflight_check() -> int:
    print("=== Jeff Bot Preflight Check ===\n")
    ok_all = True

    if DISCORD_TOKEN and DISCORD_TOKEN != "your_discord_bot_token_here":
        print(f"[OK]   DISCORD_TOKEN is set ({len(DISCORD_TOKEN)} chars)")
    else:
        print("[FAIL] DISCORD_TOKEN is missing in .env")
        ok_all = False

    if CHANNEL_ID:
        print(f"[OK]   CHANNEL_ID = {CHANNEL_ID}")
    else:
        print("[WARN] CHANNEL_ID is not set (scheduled recaps, rank alerts & squad debriefs need it)")

    if GUILD_ID:
        print(f"[OK]   GUILD_ID = {GUILD_ID} (instant slash-command sync enabled)")
    else:
        print("[INFO] GUILD_ID not set (will use global sync, which can take up to 1h)")

    riot_ok, riot_msg = await riot.verify_key()
    print(f"[{'OK' if riot_ok else 'FAIL'}]   Riot API: {riot_msg}")
    if not riot_ok:
        ok_all = False
    else:
        print("\nChecking tracked summoners:")
        for rid in aggregator.get_roster():
            try:
                puuid, level, _ = await aggregator._resolve_player(rid)
                print(f"  [OK]   {rid} (Level {level}, PUUID {puuid[:10]}...)")
            except Exception as e:
                print(f"  [FAIL] {rid} — {e}")
                ok_all = False

    ai_ok, ai_msg = await deepseek.verify_key()
    tag = "OK" if ai_ok else ("INFO" if not deepseek.api_key else "FAIL")
    print(f"\n[{tag}]   DeepSeek AI: {ai_msg}")
    if deepseek.api_key and not ai_ok:
        ok_all = False

    print(f"\n[OK]   SQLite DB: {DB_PATH} ({db.count_cached_matches()} cached matches)")

    prereqs_ok, missing = check_voice_prereqs()
    voice_st = await tts_client.get_status()
    v_tag = "OK" if (prereqs_ok and voice_st["active_backend"] != "None online") else "INFO"
    print(f"[{v_tag}]   Voice / TTS: {voice_st['active_backend']} (prereqs: {'ready' if prereqs_ok else ', '.join(missing)})")

    await tts_client.close()
    await riot.close()
    await deepseek.close()
    db.close()

    print(
        "\n"
        + (
            "All checks passed! Run `python bot.py` to start Jeff Bot."
            if ok_all
            else "Fix the [FAIL] items above in .env, then re-run `python bot.py --check`."
        )
    )
    return 0 if ok_all else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Jeff Bot — LoL Discord AI Coach & Stats Bot")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate .env, Riot API, DeepSeek API, and tracked Riot IDs without starting Discord",
    )
    args = parser.parse_args()

    if args.check:
        sys.exit(asyncio.run(run_preflight_check()))

    if not DISCORD_TOKEN or DISCORD_TOKEN == "your_discord_bot_token_here":
        print(
            "ERROR: DISCORD_TOKEN is not set.\n"
            "Fill in DISCORD_TOKEN in .env and run `python bot.py --check` first."
        )
        sys.exit(1)

    bot.run(DISCORD_TOKEN)
