import discord
from discord.ext import commands
import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import logging
from datetime import datetime, timedelta, timezone

from config import (
    DISCORD_TOKEN, CHANNEL_ID,
    DAILY_HOUR, DAILY_MINUTE, WEEKLY_DAY, WEEKLY_HOUR, WEEKLY_MINUTE, TIMEZONE,
    WEEKLY_MATCH_LOOKBACK_DAYS,
    RANK_CHECK_INTERVAL_HOURS, RANK_HISTORY_FILE,
)
from riot_api import RiotAPI
from stats import StatsAggregator, RankTracker, resolve_riot_id
from embeds import (
    build_daily_embed, build_weekly_embed, build_player_snapshot_embed,
    build_promotion_embed, build_versus_embed, build_mastery_embed,
    build_damage_embed,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
scheduler = AsyncIOScheduler(timezone=TIMEZONE)
riot = RiotAPI()
aggregator = StatsAggregator(riot)
rank_tracker = RankTracker(aggregator, RANK_HISTORY_FILE)


async def post_daily():
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        logger.error(f"Could not find channel {CHANNEL_ID}")
        return
    logger.info("Posting daily recap...")
    try:
        stats = await aggregator.get_daily_stats()
        embeds = build_daily_embed(stats)
        for embed in embeds:
            await channel.send(embed=embed)
        logger.info("Daily recap posted successfully.")
    except Exception as e:
        logger.exception(f"Failed to post daily recap: {e}")


async def post_weekly():
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        logger.error(f"Could not find channel {CHANNEL_ID}")
        return
    logger.info("Posting weekly digest...")
    try:
        stats = await aggregator.get_weekly_stats()
        embeds = build_weekly_embed(stats)
        for embed in embeds:
            await channel.send(embed=embed)
        logger.info("Weekly digest posted successfully.")
    except Exception as e:
        logger.exception(f"Failed to post weekly digest: {e}")


async def check_rank_promotions():
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        logger.error(f"Could not find channel {CHANNEL_ID}")
        return
    try:
        promotions = await rank_tracker.check_promotions()
        for promo in promotions:
            await channel.send(embed=build_promotion_embed(promo))
        if promotions:
            logger.info(f"Posted {len(promotions)} promotion alert(s).")
    except Exception as e:
        logger.exception(f"Rank promotion check failed: {e}")


@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")

    # Daily recap
    scheduler.add_job(
        post_daily,
        CronTrigger(hour=DAILY_HOUR, minute=DAILY_MINUTE),
        id="daily_recap",
        replace_existing=True
    )

    # Weekly digest (e.g. Monday morning)
    scheduler.add_job(
        post_weekly,
        CronTrigger(day_of_week=WEEKLY_DAY, hour=WEEKLY_HOUR, minute=WEEKLY_MINUTE),
        id="weekly_digest",
        replace_existing=True
    )

    # Rank promotion poller
    scheduler.add_job(
        check_rank_promotions,
        CronTrigger(hour=f"*/{RANK_CHECK_INTERVAL_HOURS}", minute=0),
        id="rank_promotion_check",
        replace_existing=True,
    )

    scheduler.start()
    logger.info(f"Scheduler started. Daily at {DAILY_HOUR}:{DAILY_MINUTE:02d}, Weekly on {WEEKLY_DAY} at {WEEKLY_HOUR}:{WEEKLY_MINUTE:02d} ({TIMEZONE})")

    # Seed rank history silently on startup so the first real check has a baseline
    asyncio.create_task(rank_tracker.check_promotions())


@bot.command(name="daily")
@commands.has_permissions(administrator=True)
async def force_daily(ctx):
    """Force post a daily recap right now."""
    await ctx.send("⏳ Fetching daily stats...")
    await post_daily()


@bot.command(name="weekly")
@commands.has_permissions(administrator=True)
async def force_weekly(ctx):
    """Force post a weekly digest right now."""
    await ctx.send("⏳ Fetching weekly stats...")
    await post_weekly()


@bot.command(name="stats")
async def player_stats(ctx, *, riot_id: str):
    """Look up a specific player. Usage: !stats Name  (or Name#TAG)"""
    try:
        resolved = resolve_riot_id(riot_id)
    except ValueError as e:
        await ctx.send(f"❌ {e}")
        return
    await ctx.send(f"⏳ Looking up **{resolved}**...")
    try:
        data = await aggregator.get_player_snapshot(resolved)
        embed = build_player_snapshot_embed(data)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(f"❌ Could not fetch stats for `{resolved}`: {e}")


@bot.command(name="versus")
async def versus(ctx, player1: str, player2: str):
    """Head-to-head comparison. Usage: !versus Name1 Name2"""
    try:
        p1 = resolve_riot_id(player1)
        p2 = resolve_riot_id(player2)
    except ValueError as e:
        await ctx.send(f"❌ {e}")
        return
    await ctx.send(f"⏳ Comparing **{p1}** vs **{p2}**...")
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=WEEKLY_MATCH_LOOKBACK_DAYS)
        start_epoch = int(cutoff.timestamp())
        a, b = await asyncio.gather(
            aggregator._fetch_player_stats(p1, start_epoch),
            aggregator._fetch_player_stats(p2, start_epoch),
        )
        await ctx.send(embed=build_versus_embed(a, b))
    except Exception as e:
        await ctx.send(f"❌ Versus lookup failed: {e}")


@bot.command(name="damage")
async def damage(ctx):
    """Weekly damage-to-champions leaderboard. Usage: !damage"""
    await ctx.send("⏳ Crunching damage numbers for the past week...")
    try:
        stats = await aggregator.get_weekly_stats()
        await ctx.send(embed=build_damage_embed(stats))
    except Exception as e:
        await ctx.send(f"❌ Could not fetch damage stats: {e}")


@bot.command(name="mastery")
async def mastery(ctx, *, riot_id: str):
    """Top 10 champion mastery. Usage: !mastery Name  (or Name#TAG)"""
    try:
        resolved = resolve_riot_id(riot_id)
    except ValueError as e:
        await ctx.send(f"❌ {e}")
        return
    await ctx.send(f"⏳ Fetching mastery for **{resolved}**...")
    try:
        data = await aggregator.get_champion_mastery(resolved, count=10)
        await ctx.send(embed=build_mastery_embed(data))
    except Exception as e:
        await ctx.send(f"❌ Could not fetch mastery for `{resolved}`: {e}")


@bot.command(name="lolhelp")
async def lol_help(ctx):
    embed = discord.Embed(
        title="📋 LoL Bot Commands",
        color=0xC89B3C
    )
    embed.add_field(name="!daily", value="Force a daily recap (admin only)", inline=False)
    embed.add_field(name="!weekly", value="Force a weekly digest (admin only)", inline=False)
    embed.add_field(name="!stats Name", value="Look up a player's current stats (use nickname for tracked players, Name#TAG for others)", inline=False)
    embed.add_field(name="!versus Name1 Name2", value="Head-to-head comparison of two players", inline=False)
    embed.add_field(name="!damage", value="Weekly damage-to-champions leaderboard", inline=False)
    embed.add_field(name="!mastery Name", value="Show top 10 champion mastery", inline=False)
    embed.add_field(name="!lolhelp", value="Show this message", inline=False)
    embed.set_footer(text=f"Tier promotions are auto-announced every {RANK_CHECK_INTERVAL_HOURS}h")
    await ctx.send(embed=embed)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
