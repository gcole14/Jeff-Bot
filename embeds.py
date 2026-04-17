import discord
from datetime import datetime, timezone
from stats import PlayerStats

# LoL gold color
LOL_GOLD = 0xC89B3C
LOL_BLUE = 0x0BC4E3
LOL_RED = 0xC62B2B
LOL_GREEN = 0x22C55E

TIER_EMOTES = {
    "IRON": "⚙️",
    "BRONZE": "🥉",
    "SILVER": "🪙",
    "GOLD": "🥇",
    "PLATINUM": "💎",
    "EMERALD": "💚",
    "DIAMOND": "🔷",
    "MASTER": "👑",
    "GRANDMASTER": "👑",
    "CHALLENGER": "🏆",
    "UNRANKED": "❓",
}


def _rank_emote(tier: str) -> str:
    return TIER_EMOTES.get(tier.upper(), "❓")


def _win_rate_bar(win_rate: float, length: int = 10) -> str:
    filled = round(win_rate / 100 * length)
    return "█" * filled + "░" * (length - filled)


def _lp_change_str(lp_change: int) -> str:
    if lp_change > 0:
        return f"▲ +{lp_change} LP"
    elif lp_change < 0:
        return f"▼ {lp_change} LP"
    return "— LP"


def _player_field(ps: PlayerStats, period: str = "today") -> tuple[str, str]:
    if ps.error:
        return ps.display_name, f"⚠️ Could not fetch data: `{ps.error}`"

    if ps.games_played == 0:
        return ps.display_name, f"{_rank_emote(ps.solo_tier)} {ps.formatted_rank()}\n*No games {period}*"

    top_champs = ", ".join(f"{c} ×{n}" for c, n in ps.top_champions) or "N/A"
    wr_bar = _win_rate_bar(ps.win_rate)

    value = (
        f"{_rank_emote(ps.solo_tier)} **{ps.formatted_rank()}**\n"
        f"🎮 **{ps.games_played}** games  •  "
        f"**{ps.wins}W {ps.losses}L**  •  "
        f"**{ps.win_rate:.0f}%** WR\n"
        f"`{wr_bar}` \n"
        f"⚔️ KDA: **{ps.formatted_kda()}**\n"
        f"⏱️ {ps.hours_played:.1f}h played\n"
        f"🧙 {top_champs}"
    )
    return ps.display_name, value


def _awards_section(stats: list[PlayerStats]) -> str:
    active = [ps for ps in stats if ps.games_played > 0 and not ps.error]
    if not active:
        return "*No games played this period.*"

    lines = []

    # Best WR
    best_wr = max(active, key=lambda p: p.win_rate)
    lines.append(f"🏆 **Best Win Rate:** {best_wr.display_name} — {best_wr.win_rate:.0f}%")

    # Most games
    most_games = max(active, key=lambda p: p.games_played)
    lines.append(f"🎮 **Most Games:** {most_games.display_name} — {most_games.games_played} games")

    # Best KDA
    best_kda = max(active, key=lambda p: p.kda)
    lines.append(f"⚔️ **Best KDA:** {best_kda.display_name} — {best_kda.kda:.2f}")

    # Most hours
    most_hours = max(active, key=lambda p: p.hours_played)
    lines.append(f"⏱️ **Most Time:** {most_hours.display_name} — {most_hours.hours_played:.1f}h")

    # Highest rank
    highest_rank = max(active, key=lambda p: p.overall_rank_score)
    lines.append(f"👑 **Highest Rank:** {highest_rank.display_name} — {highest_rank.formatted_rank()}")

    return "\n".join(lines)


def build_daily_embed(stats: list[PlayerStats]) -> list[discord.Embed]:
    now = datetime.now(timezone.utc)
    timestamp_str = now.strftime("%B %d, %Y")

    main_embed = discord.Embed(
        title="⚔️ Daily LoL Recap",
        description=f"**{timestamp_str}** — Here's how the boys did today:",
        color=LOL_GOLD,
        timestamp=now
    )
    main_embed.set_footer(text="League of Legends Stats Bot")

    for ps in stats:
        name, value = _player_field(ps, period="today")
        main_embed.add_field(name=name, value=value, inline=True)

    # Pad to even columns
    if len(stats) % 2 != 0:
        main_embed.add_field(name="\u200b", value="\u200b", inline=True)

    awards_embed = discord.Embed(
        title="🏅 Daily Awards",
        description=_awards_section(stats),
        color=LOL_BLUE,
        timestamp=now
    )
    awards_embed.set_footer(text="League of Legends Stats Bot")

    return [main_embed, awards_embed]


def build_weekly_embed(stats: list[PlayerStats]) -> list[discord.Embed]:
    now = datetime.now(timezone.utc)
    timestamp_str = now.strftime("%B %d, %Y")

    active = [ps for ps in stats if ps.games_played > 0 and not ps.error]
    total_games = sum(ps.games_played for ps in active)
    total_hours = sum(ps.hours_played for ps in active)

    main_embed = discord.Embed(
        title="📊 Weekly LoL Digest",
        description=(
            f"**Week ending {timestamp_str}**\n"
            f"👥 Group total: **{total_games} games** • **{total_hours:.1f}h** played"
        ),
        color=LOL_GOLD,
        timestamp=now
    )
    main_embed.set_footer(text="League of Legends Stats Bot")

    for ps in stats:
        name, value = _player_field(ps, period="this week")
        main_embed.add_field(name=name, value=value, inline=True)

    if len(stats) % 2 != 0:
        main_embed.add_field(name="\u200b", value="\u200b", inline=True)

    # Leaderboard embed
    ranked_active = sorted(active, key=lambda p: p.win_rate, reverse=True)
    lb_lines = []
    medals = ["🥇", "🥈", "🥉"]
    for i, ps in enumerate(ranked_active):
        medal = medals[i] if i < 3 else f"**{i+1}.**"
        lb_lines.append(
            f"{medal} **{ps.display_name}** — {ps.win_rate:.0f}% WR "
            f"({ps.wins}W {ps.losses}L) • {ps.kda:.2f} KDA • {ps.hours_played:.1f}h"
        )

    leaderboard_embed = discord.Embed(
        title="🏆 Weekly Leaderboard",
        description="\n".join(lb_lines) if lb_lines else "*No games this week.*",
        color=LOL_GOLD,
        timestamp=now
    )

    awards_embed = discord.Embed(
        title="🏅 Weekly Awards",
        description=_awards_section(stats),
        color=LOL_BLUE,
        timestamp=now
    )
    awards_embed.set_footer(text="League of Legends Stats Bot")

    return [main_embed, leaderboard_embed, awards_embed]


def build_player_snapshot_embed(ps: PlayerStats) -> discord.Embed:
    """Single player lookup embed."""
    color = LOL_GREEN if not ps.error else LOL_RED
    embed = discord.Embed(
        title=f"🔍 {ps.display_name}",
        color=color,
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_footer(text="Last 24 hours")

    if ps.error:
        embed.description = f"⚠️ Error: `{ps.error}`"
        return embed

    embed.add_field(name="Rank", value=f"{_rank_emote(ps.solo_tier)} {ps.formatted_rank()}", inline=True)
    embed.add_field(name="Season Record", value=f"{ps.solo_wins}W {ps.solo_losses}L", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)

    if ps.games_played > 0:
        embed.add_field(name="Today", value=f"{ps.games_played} games • {ps.wins}W {ps.losses}L • {ps.win_rate:.0f}% WR", inline=False)
        embed.add_field(name="KDA", value=ps.formatted_kda(), inline=True)
        embed.add_field(name="Hours", value=f"{ps.hours_played:.1f}h", inline=True)
        champs = ", ".join(f"{c} ×{n}" for c, n in ps.top_champions) or "N/A"
        embed.add_field(name="Champions", value=champs, inline=False)
    else:
        embed.description = f"{_rank_emote(ps.solo_tier)} {ps.formatted_rank()}\n*No games in the last 24 hours.*"

    return embed
