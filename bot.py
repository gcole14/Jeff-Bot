import discord
from discord.ext import commands
import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import logging
from datetime import datetime
import pytz

from config import DISCORD_TOKEN, CHANNEL_ID, DAILY_HOUR, DAILY_MINUTE, WEEKLY_DAY, WEEKLY_HOUR, WEEKLY_MINUTE, TIMEZONE
from riot_api import RiotAPI
from stats import StatsAggregator
from embeds import build_daily_embed, build_weekly_embed

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
bot = commands.Bot(command_prefix="!", intents=intents)
scheduler = AsyncIOScheduler(timezone=TIMEZONE)
riot = RiotAPI()
aggregator = StatsAggregator(riot)


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

    scheduler.start()
    logger.info(f"Scheduler started. Daily at {DAILY_HOUR}:{DAILY_MINUTE:02d}, Weekly on {WEEKLY_DAY} at {WEEKLY_HOUR}:{WEEKLY_MINUTE:02d} ({TIMEZONE})")


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
    """Look up a specific player. Usage: !stats Name#TAG"""
    await ctx.send(f"⏳ Looking up **{riot_id}**...")
    try:
        data = await aggregator.get_player_snapshot(riot_id)
        embed = build_player_snapshot_embed(data)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(f"❌ Could not fetch stats for `{riot_id}`: {e}")


@bot.command(name="lolhelp")
async def lol_help(ctx):
    embed = discord.Embed(
        title="📋 LoL Bot Commands",
        color=0xC89B3C
    )
    embed.add_field(name="!daily", value="Force a daily recap (admin only)", inline=False)
    embed.add_field(name="!weekly", value="Force a weekly digest (admin only)", inline=False)
    embed.add_field(name="!stats Name#TAG", value="Look up any player's current stats", inline=False)
    embed.add_field(name="!lolhelp", value="Show this message", inline=False)
    await ctx.send(embed=embed)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
