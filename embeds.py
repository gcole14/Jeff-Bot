import discord
from datetime import datetime, timezone
from stats import PlayerStats, format_rank

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


def build_promotion_embed(promo: dict) -> discord.Embed:
    """Celebration embed when a player goes up a tier."""
    old_emote = _rank_emote(promo["old_tier"])
    new_emote = _rank_emote(promo["new_tier"])
    old_fmt = format_rank(promo["old_tier"], promo["old_rank"], promo["old_lp"])
    new_fmt = format_rank(promo["new_tier"], promo["new_rank"], promo["new_lp"])

    embed = discord.Embed(
        title=f"🎉 {promo['display_name']} Promoted!",
        description=f"{old_emote} **{promo['old_tier'].capitalize()}** ➡️ {new_emote} **{promo['new_tier'].capitalize()}**",
        color=LOL_GOLD,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Previous Rank", value=old_fmt, inline=True)
    embed.add_field(name="New Rank", value=new_fmt, inline=True)
    embed.set_footer(text="Congrats! 🏆")
    return embed


def _winner(v1, v2) -> tuple[bool, bool]:
    if v1 == v2:
        return False, False
    return (v1 > v2, v2 > v1)


def build_versus_embed(a: PlayerStats, b: PlayerStats) -> discord.Embed:
    """Side-by-side comparison of two players over the last week."""
    embed = discord.Embed(
        title=f"⚔️ {a.display_name} vs {b.display_name}",
        color=LOL_BLUE,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="Weekly comparison")

    if a.error or b.error:
        issues = []
        if a.error:
            issues.append(f"{a.display_name}: `{a.error}`")
        if b.error:
            issues.append(f"{b.display_name}: `{b.error}`")
        embed.description = "⚠️ Could not load one or both players:\n" + "\n".join(issues)
        embed.colour = LOL_RED
        return embed

    a_top = a.top_champions[0][0] if a.top_champions else "N/A"
    b_top = b.top_champions[0][0] if b.top_champions else "N/A"

    categories = [
        ("Rank",     a.formatted_rank(),               b.formatted_rank(),              a.overall_rank_score,       b.overall_rank_score),
        ("Games",    str(a.games_played),              str(b.games_played),             a.games_played,             b.games_played),
        ("Win Rate", f"{a.win_rate:.0f}%",             f"{b.win_rate:.0f}%",            a.win_rate,                 b.win_rate),
        ("W / L",    f"{a.wins}W {a.losses}L",         f"{b.wins}W {b.losses}L",        a.wins,                     b.wins),
        ("KDA",      f"{a.kda:.2f}",                   f"{b.kda:.2f}",                  a.kda,                      b.kda),
        ("Hours",    f"{a.hours_played:.1f}h",         f"{b.hours_played:.1f}h",        a.hours_played,             b.hours_played),
        ("Damage",   f"{a.total_damage_to_champions:,}", f"{b.total_damage_to_champions:,}", a.total_damage_to_champions, b.total_damage_to_champions),
    ]

    lines = [
        f"**{_rank_emote(a.solo_tier)} {a.display_name}**  ⚔️  **{b.display_name} {_rank_emote(b.solo_tier)}**",
        "",
    ]
    for label, a_val, b_val, a_cmp, b_cmp in categories:
        a_win, b_win = _winner(a_cmp, b_cmp)
        a_prefix = "🏆 " if a_win else ""
        b_prefix = " 🏆" if b_win else ""
        lines.append(f"**{label}** — {a_prefix}{a_val}  •  {b_val}{b_prefix}")

    lines.append(f"**Top Champ** — {a_top}  •  {b_top}")

    embed.description = "\n".join(lines)
    return embed


def build_damage_embed(stats: list[PlayerStats]) -> discord.Embed:
    """Weekly damage-to-champions leaderboard."""
    now = datetime.now(timezone.utc)
    active = [ps for ps in stats if ps.games_played > 0 and not ps.error]
    ranked = sorted(active, key=lambda p: p.total_damage_to_champions, reverse=True)

    embed = discord.Embed(
        title="💥 Weekly Damage Leaderboard",
        color=LOL_RED,
        timestamp=now,
    )
    embed.set_footer(text="Total damage to champions • last 7 days")

    if not ranked:
        embed.description = "*No games played this week.*"
        return embed

    medals = ["🥇", "🥈", "🥉"]
    lines = []
    total = 0
    for i, ps in enumerate(ranked):
        medal = medals[i] if i < 3 else f"**{i+1}.**"
        per_game = ps.total_damage_to_champions / ps.games_played if ps.games_played else 0
        total += ps.total_damage_to_champions
        lines.append(
            f"{medal} **{ps.display_name}** — {ps.total_damage_to_champions:,} dmg "
            f"({ps.games_played} games • {per_game:,.0f} / game)"
        )

    # Include unranked/errored at the bottom as informational
    inactive = [ps for ps in stats if ps not in active]
    for ps in inactive:
        if ps.error:
            lines.append(f"⚠️ **{ps.display_name}** — error: `{ps.error}`")
        else:
            lines.append(f"— **{ps.display_name}** — no games this week")

    embed.description = "\n".join(lines)
    embed.add_field(name="Group Total", value=f"**{total:,}** damage", inline=False)
    return embed


def build_mastery_embed(mastery: dict) -> discord.Embed:
    """Top-N champion mastery embed."""
    if mastery.get("error"):
        embed = discord.Embed(
            title=f"🧙 {mastery['display_name']}'s Top Champions",
            description=f"⚠️ Error: `{mastery['error']}`",
            color=LOL_RED,
            timestamp=datetime.now(timezone.utc),
        )
        return embed

    masteries = mastery.get("masteries", [])
    if not masteries:
        description = "*No mastery data available.*"
    else:
        lines = []
        for i, m in enumerate(masteries, 1):
            lines.append(f"**{i}.** {m['name']} — Level {m['level']} • {m['points']:,} pts")
        description = "\n".join(lines)

    embed = discord.Embed(
        title=f"🧙 {mastery['display_name']}'s Top Champions",
        description=description,
        color=LOL_GOLD,
        timestamp=datetime.now(timezone.utc),
    )
    version = mastery.get("version")
    footer = f"Patch {version}" if version else "Champion mastery"
    embed.set_footer(text=footer)
    return embed
