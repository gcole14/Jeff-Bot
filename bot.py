import discord
from discord import app_commands
from discord.ext import commands
import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import logging
from datetime import datetime, timedelta, timezone

from config import (
    DISCORD_TOKEN, CHANNEL_ID, SUMMONERS,
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

    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} slash command(s).")
    except Exception as e:
        logger.exception(f"Failed to sync slash commands: {e}")

    scheduler.add_job(
        post_daily,
        CronTrigger(hour=DAILY_HOUR, minute=DAILY_MINUTE),
        id="daily_recap",
        replace_existing=True
    )

    scheduler.add_job(
        post_weekly,
        CronTrigger(day_of_week=WEEKLY_DAY, hour=WEEKLY_HOUR, minute=WEEKLY_MINUTE),
        id="weekly_digest",
        replace_existing=True
    )

    scheduler.add_job(
        check_rank_promotions,
        CronTrigger(hour=f"*/{RANK_CHECK_INTERVAL_HOURS}", minute=0),
        id="rank_promotion_check",
        replace_existing=True,
    )

    scheduler.start()
    logger.info(f"Scheduler started. Daily at {DAILY_HOUR}:{DAILY_MINUTE:02d}, Weekly on {WEEKLY_DAY} at {WEEKLY_HOUR}:{WEEKLY_MINUTE:02d} ({TIMEZONE})")

    asyncio.create_task(rank_tracker.check_promotions())


async def player_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    current_lower = current.lower()
    choices = []
    for full in SUMMONERS:
        name = full.split("#", 1)[0]
        if not current or current_lower in name.lower():
            choices.append(app_commands.Choice(name=name, value=name))
    return choices[:25]


@bot.tree.command(name="daily", description="Force a daily recap (admin only)")
@app_commands.default_permissions(administrator=True)
async def slash_daily(interaction: discord.Interaction):
    await interaction.response.send_message("⏳ Fetching daily stats...")
    await post_daily()


@bot.tree.command(name="weekly", description="Force a weekly digest (admin only)")
@app_commands.default_permissions(administrator=True)
async def slash_weekly(interaction: discord.Interaction):
    await interaction.response.send_message("⏳ Fetching weekly stats...")
    await post_weekly()


@bot.tree.command(name="stats", description="Look up a player's current stats")
@app_commands.describe(player="Player nickname (tracked) or full Name#TAG")
@app_commands.autocomplete(player=player_autocomplete)
async def slash_stats(interaction: discord.Interaction, player: str):
    try:
        resolved = resolve_riot_id(player)
    except ValueError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        data = await aggregator.get_player_snapshot(resolved)
        embed = build_player_snapshot_embed(data)
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"❌ Could not fetch stats for `{resolved}`: {e}")


@bot.tree.command(name="versus", description="Head-to-head comparison of two players")
@app_commands.describe(player1="First player", player2="Second player")
@app_commands.autocomplete(player1=player_autocomplete, player2=player_autocomplete)
async def slash_versus(interaction: discord.Interaction, player1: str, player2: str):
    try:
        p1 = resolve_riot_id(player1)
        p2 = resolve_riot_id(player2)
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


@bot.tree.command(name="mastery", description="Show top 10 champion mastery for a player")
@app_commands.describe(player="Player nickname (tracked) or full Name#TAG")
@app_commands.autocomplete(player=player_autocomplete)
async def slash_mastery(interaction: discord.Interaction, player: str):
    try:
        resolved = resolve_riot_id(player)
    except ValueError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        data = await aggregator.get_champion_mastery(resolved, count=10)
        await interaction.followup.send(embed=build_mastery_embed(data))
    except Exception as e:
        await interaction.followup.send(f"❌ Could not fetch mastery for `{resolved}`: {e}")


@bot.tree.command(name="lolhelp", description="Show all Jeff Bot commands")
async def slash_help(interaction: discord.Interaction):
    embed = discord.Embed(title="📋 Jeff Bot Commands", color=0xC89B3C)
    embed.add_field(name="/daily", value="Force a daily recap (admin only)", inline=False)
    embed.add_field(name="/weekly", value="Force a weekly digest (admin only)", inline=False)
    embed.add_field(name="/stats player", value="Look up a player's current stats", inline=False)
    embed.add_field(name="/versus player1 player2", value="Head-to-head comparison of two players", inline=False)
    embed.add_field(name="/damage", value="Weekly damage-to-champions leaderboard", inline=False)
    embed.add_field(name="/mastery player", value="Show top 10 champion mastery", inline=False)
    embed.add_field(name="/lolhelp", value="Show this message", inline=False)
    embed.set_footer(text=f"Tier promotions are auto-announced every {RANK_CHECK_INTERVAL_HOURS}h")
    await interaction.response.send_message(embed=embed)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
