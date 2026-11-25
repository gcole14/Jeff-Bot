import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from ai import DeepSeekClient
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
    DB_PATH,
    RANK_HISTORY_FILE,
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
from riot_api import RiotAPI
from stats import StatsAggregator, RankTracker, resolve_riot_id

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


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
deepseek = DeepSeekClient()
aggregator = StatsAggregator(riot, db)
rank_tracker = RankTracker(aggregator, db)
scheduler = AsyncIOScheduler(timezone=TIMEZONE)


class JeffBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self._setup_done = False

    async def close(self) -> None:
        if scheduler.running:
            scheduler.shutdown(wait=False)
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
        commentary = await deepseek.summarize_recap(stats, period="daily") if deepseek.is_configured else None
        embeds = build_daily_embed(stats, commentary=commentary)
        for embed in embeds:
            await channel.send(embed=embed)
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
        embeds = build_weekly_embed(
            stats, commentary=commentary, synergy=synergy, goal_grades=goal_grades or None
        )
        for embed in embeds:
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
            commentary = (
                await deepseek.comment_rank_change(event) if deepseek.is_configured else None
            )
            if event.get("direction") == "demotion":
                embed = build_demotion_embed(event, commentary=commentary)
            else:
                embed = build_promotion_embed(event, commentary=commentary)
            await channel.send(embed=embed)
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
            commentary = (
                await deepseek.debrief_squad_game(debrief) if deepseek.is_configured else None
            )
            await channel.send(embed=build_squad_debrief_embed(debrief, commentary=commentary))
            db.mark_match_announced(debrief["match_id"])
            logger.info(f"Posted auto squad debrief for {debrief['match_id']}.")
    except Exception as e:
        logger.exception(f"Squad debrief check failed: {e}")


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
        CronTrigger(hour=DAILY_HOUR, minute=DAILY_MINUTE),
        id="daily_recap",
        replace_existing=True,
    )
    scheduler.add_job(
        post_weekly,
        CronTrigger(day_of_week=WEEKLY_DAY, hour=WEEKLY_HOUR, minute=WEEKLY_MINUTE),
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

    scheduler.start()
    logger.info(
        f"Scheduler started. Daily at {DAILY_HOUR}:{DAILY_MINUTE:02d}, "
        f"Weekly on {WEEKLY_DAY} at {WEEKLY_HOUR}:{WEEKLY_MINUTE:02d} ({TIMEZONE}), "
        f"Rank check every {RANK_CHECK_INTERVAL_HOURS}h, "
        f"Squad debriefs {'every ' + str(SQUAD_CHECK_INTERVAL_MINUTES) + 'm' if SQUAD_DEBRIEF_ENABLED else 'disabled'}."
    )

    asyncio.create_task(check_rank_changes())
    if SQUAD_DEBRIEF_ENABLED:
        asyncio.create_task(aggregator.seed_announced_squad_games())
    bot._setup_done = True


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


@bot.tree.command(name="stats", description="Look up a player's stats")
@app_commands.describe(
    player="Player nickname (tracked) or full Name#TAG",
    days="Lookback window in days (default: 1 = last 24h)",
    queue="Optional queue filter",
)
@app_commands.choices(queue=QUEUE_CHOICES)
@app_commands.autocomplete(player=player_autocomplete)
async def slash_stats(
    interaction: discord.Interaction,
    player: str,
    days: app_commands.Range[int, 1, 30] = 1,
    queue: int = 0,
):
    try:
        resolved = resolve_riot_id(player, aggregator.get_roster())
    except ValueError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        data = await aggregator.get_player_snapshot(resolved, days=days, queue=(queue or None))
        embed = build_player_snapshot_embed(
            data, days=days, queue_label=QUEUE_LABEL_MAP.get(queue, "All Queues")
        )
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"❌ Could not fetch stats for `{resolved}`: {e}")


@bot.tree.command(name="lastgame", description="Detailed breakdown of a player's most recent game")
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
        commentary = (
            await deepseek.comment_last_game(game)
            if deepseek.is_configured and game.get("match") and not game.get("error")
            else None
        )
        await interaction.followup.send(embed=build_lastgame_embed(game, commentary=commentary))
    except Exception as e:
        await interaction.followup.send(f"❌ Could not fetch last game for `{resolved}`: {e}")


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
        review_text = (
            await deepseek.review_match_timeline(report) if deepseek.is_configured else None
        )
        await interaction.followup.send(embed=build_review_embed(report, review_text))
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
        advice = await deepseek.coach_player(ps, split, pool) if deepseek.is_configured else None
        await interaction.followup.send(embed=build_coach_embed(ps, split, advice, days=days))
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
        verdict = await deepseek.audit_pool_verdict(ps, pool) if deepseek.is_configured else None
        await interaction.followup.send(embed=build_pool_embed(ps, pool, verdict, days=days))
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
        await interaction.followup.send(embed=build_goal_embed(ps_14.display_name, ev, is_new=is_new))
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
        gameplan = (
            await deepseek.scout_live_game(scout)
            if deepseek.is_configured and scout.get("in_game") and not scout.get("error")
            else None
        )
        await interaction.followup.send(embed=build_scout_embed(scout, gameplan))
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
        commentary = (
            await deepseek.debrief_squad_game(latest) if deepseek.is_configured else None
        )
        await interaction.followup.send(embed=build_squad_debrief_embed(latest, commentary=commentary))
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
        context = await aggregator.build_squad_context(days=14)
        answer = await deepseek.answer_question(question, context, interaction.user.display_name)
        if not answer:
            await interaction.followup.send("❌ DeepSeek didn't answer — check bot logs.")
            return
        await interaction.followup.send(
            embed=build_ask_embed(question, answer, interaction.user.display_name)
        )
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
        await interaction.followup.send(embed=build_versus_embed(a, b))
    except Exception as e:
        await interaction.followup.send(f"❌ Versus lookup failed: {e}")


@bot.tree.command(name="damage", description="Weekly damage-to-champions leaderboard")
async def slash_damage(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        stats = await aggregator.get_weekly_stats()
        await interaction.followup.send(embed=build_damage_embed(stats))
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
        await interaction.followup.send(embed=build_synergy_embed(data))
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
        await interaction.followup.send(embed=build_mastery_embed(data))
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
    (riot_ok, riot_msg), (ai_ok, ai_msg) = await asyncio.gather(
        riot.verify_key(),
        deepseek.verify_key(),
    )
    next_runs = []
    for job in scheduler.get_jobs():
        if job.next_run_time:
            next_runs.append(f"• **{job.id}**: {job.next_run_time.strftime('%a %b %d, %I:%M %p %Z')}")

    channel = bot.get_channel(CHANNEL_ID) if CHANNEL_ID else None
    channel_label = f"#{channel.name}" if channel and hasattr(channel, "name") else (str(CHANNEL_ID) if CHANNEL_ID else "Not set")

    embed = build_status_embed({
        "riot_ok": riot_ok,
        "riot_msg": riot_msg,
        "ai_ok": ai_ok,
        "ai_msg": ai_msg,
        "roster": aggregator.get_roster(),
        "cached_matches": db.count_cached_matches(),
        "channel_label": channel_label,
        "next_runs": next_runs,
    })
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="lolhelp", description="Show all Jeff Bot commands")
async def slash_help(interaction: discord.Interaction):
    embed = discord.Embed(title="📋 Jeff Bot Commands", color=0xC89B3C)
    embed.add_field(
        name="🧠 AI Coaching & Scouting (DeepSeek)",
        value=(
            "`/review player [game_ago]` — Timeline VOD review (@10/@15 diffs, throws, build)\n"
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
        name="⚙️ Admin & Ops",
        value="`/roster list | add | remove` • `/daily` • `/weekly` • `/setriotkey` • `/status`",
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
