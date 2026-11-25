import discord
from datetime import datetime, timezone
from typing import Optional

from coaching import TimelineReport
from stats import PlayerStats, format_rank

# Theme colors
LOL_GOLD = 0xC89B3C
LOL_BLUE = 0x0BC4E3
LOL_RED = 0xC62B2B
LOL_GREEN = 0x22C55E
LOL_PURPLE = 0x9B59B6

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


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _rank_emote(tier: str) -> str:
    return TIER_EMOTES.get((tier or "").upper(), "❓")


def _win_rate_bar(win_rate: float, length: int = 10) -> str:
    clamped = max(0.0, min(100.0, win_rate))
    filled = round(clamped / 100 * length)
    return "█" * filled + "░" * (length - filled)


def _lp_change_str(lp_change: int) -> str:
    if lp_change > 0:
        return f"▲ +{lp_change} LP"
    elif lp_change < 0:
        return f"▼ {lp_change} LP"
    return ""


def _player_field(ps: PlayerStats, period: str = "today") -> tuple[str, str]:
    if ps.error:
        return ps.display_name, _truncate(f"⚠️ Could not fetch data: `{ps.error}`", 1024)

    lp_tag = _lp_change_str(ps.lp_change)
    rank_line = f"{_rank_emote(ps.solo_tier)} **{ps.formatted_rank()}**"
    if lp_tag:
        rank_line += f"  ({lp_tag})"

    if ps.games_played == 0:
        return ps.display_name, f"{rank_line}\n*No games {period}*"

    top_champs = ", ".join(f"{c} ×{n}" for c, n in ps.top_champions) or "N/A"
    wr_bar = _win_rate_bar(ps.win_rate)
    streak = ps.formatted_streak()
    streak_suffix = f"  •  {streak}" if streak else ""

    extras = []
    if ps.pentas:
        extras.append(f"⭐ {ps.pentas} Penta{'s' if ps.pentas > 1 else ''}")
    if ps.quadras:
        extras.append(f"💫 {ps.quadras} Quadra{'s' if ps.quadras > 1 else ''}")

    lines = [
        rank_line,
        f"🎮 **{ps.games_played}** games • **{ps.wins}W {ps.losses}L** • **{ps.win_rate:.0f}%** WR{streak_suffix}",
        f"`{wr_bar}`",
        f"⚔️ KDA: **{ps.formatted_kda()}** • {ps.cs_per_min:.1f} CS/m",
        f"💥 {ps.avg_damage_per_game:,.0f} avg dmg • ⏱️ {ps.hours_played:.1f}h",
        f"🧙 {top_champs}",
    ]
    if extras:
        lines.append(" • ".join(extras))

    return ps.display_name, _truncate("\n".join(lines), 1024)


def _awards_section(stats: list[PlayerStats]) -> str:
    active = [ps for ps in stats if ps.games_played > 0 and not ps.error]
    if not active:
        return "*No games played this period.*"

    lines = []

    best_wr = max(active, key=lambda p: (p.win_rate, p.games_played))
    lines.append(f"🏆 **Best Win Rate:** {best_wr.display_name} — {best_wr.win_rate:.0f}%")

    most_games = max(active, key=lambda p: p.games_played)
    lines.append(f"🎮 **Most Games:** {most_games.display_name} — {most_games.games_played} games")

    best_kda = max(active, key=lambda p: p.kda)
    lines.append(f"⚔️ **Best KDA:** {best_kda.display_name} — {best_kda.kda:.2f}")

    top_dmg = max(active, key=lambda p: p.avg_damage_per_game)
    lines.append(f"💥 **Damage Carry:** {top_dmg.display_name} — {top_dmg.avg_damage_per_game:,.0f} / game")

    most_hours = max(active, key=lambda p: p.hours_played)
    lines.append(f"⏱️ **Most Time:** {most_hours.display_name} — {most_hours.hours_played:.1f}h")

    ranked_active = [p for p in active if p.overall_rank_score >= 0]
    if ranked_active:
        highest_rank = max(ranked_active, key=lambda p: p.overall_rank_score)
        lines.append(f"👑 **Highest Rank:** {highest_rank.display_name} — {highest_rank.formatted_rank()}")

    lp_movers = [p for p in active if p.lp_change != 0]
    if lp_movers:
        climber = max(lp_movers, key=lambda p: p.lp_change)
        if climber.lp_change > 0:
            lines.append(f"📈 **Top Climber:** {climber.display_name} — +{climber.lp_change} LP")

    return "\n".join(lines)


def build_daily_embed(
    stats: list[PlayerStats], commentary: Optional[str] = None
) -> list[discord.Embed]:
    now = datetime.now(timezone.utc)
    timestamp_str = now.strftime("%B %d, %Y")

    main_embed = discord.Embed(
        title="⚔️ Daily LoL Recap",
        description=f"**{timestamp_str}** — Here's how the boys did today:",
        color=LOL_GOLD,
        timestamp=now,
    )
    main_embed.set_footer(text="Jeff Bot • Remakes excluded")

    for ps in stats[:24]:
        name, value = _player_field(ps, period="today")
        main_embed.add_field(name=name, value=value, inline=True)

    if len(stats) % 2 != 0 and len(main_embed.fields) < 25:
        main_embed.add_field(name="\u200b", value="\u200b", inline=True)

    awards_embed = discord.Embed(
        title="🏅 Daily Awards",
        description=_awards_section(stats),
        color=LOL_BLUE,
        timestamp=now,
    )
    if commentary:
        awards_embed.add_field(
            name="🧠 Jeff's Take",
            value=_truncate(commentary, 1024),
            inline=False,
        )
    awards_embed.set_footer(text="Jeff Bot")

    return [main_embed, awards_embed]


def build_weekly_embed(
    stats: list[PlayerStats],
    commentary: Optional[str] = None,
    synergy: Optional[dict] = None,
    goal_grades: Optional[list[str]] = None,
) -> list[discord.Embed]:
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
        timestamp=now,
    )
    main_embed.set_footer(text="Jeff Bot • Remakes excluded")

    for ps in stats[:24]:
        name, value = _player_field(ps, period="this week")
        main_embed.add_field(name=name, value=value, inline=True)

    if len(stats) % 2 != 0 and len(main_embed.fields) < 25:
        main_embed.add_field(name="\u200b", value="\u200b", inline=True)

    ranked_active = sorted(active, key=lambda p: (p.win_rate, p.games_played), reverse=True)
    lb_lines = []
    medals = ["🥇", "🥈", "🥉"]
    for i, ps in enumerate(ranked_active):
        medal = medals[i] if i < 3 else f"**{i+1}.**"
        lp_part = f" • {_lp_change_str(ps.lp_change)}" if ps.lp_change else ""
        lb_lines.append(
            f"{medal} **{ps.display_name}** — {ps.win_rate:.0f}% WR "
            f"({ps.wins}W {ps.losses}L) • {ps.kda:.2f} KDA • {ps.hours_played:.1f}h{lp_part}"
        )

    leaderboard_embed = discord.Embed(
        title="🏆 Weekly Leaderboard",
        description="\n".join(lb_lines) if lb_lines else "*No games this week.*",
        color=LOL_GOLD,
        timestamp=now,
    )

    if synergy and synergy.get("together_games", 0) > 0:
        best_duo = synergy["duos"][0] if synergy.get("duos") else None
        syn_text = (
            f"Squad together: **{synergy['together_wins']}W {synergy['together_losses']}L** "
            f"({synergy['together_wr']:.0f}% WR)"
        )
        if best_duo:
            syn_text += (
                f"\nMost active duo: **{best_duo['pair']}** — "
                f"{best_duo['wins']}W {best_duo['losses']}L ({best_duo['wr']:.0f}% WR)"
            )
        leaderboard_embed.add_field(name="🤝 Squad Synergy", value=syn_text, inline=False)

    if goal_grades:
        leaderboard_embed.add_field(
            name="🎯 Weekly Coaching Goals",
            value=_truncate("\n".join(goal_grades), 1024),
            inline=False,
        )

    awards_embed = discord.Embed(
        title="🏅 Weekly Awards",
        description=_awards_section(stats),
        color=LOL_BLUE,
        timestamp=now,
    )
    if commentary:
        awards_embed.add_field(
            name="🧠 Jeff's Take",
            value=_truncate(commentary, 1024),
            inline=False,
        )
    awards_embed.set_footer(text="Jeff Bot")

    return [main_embed, leaderboard_embed, awards_embed]


def build_player_snapshot_embed(
    ps: PlayerStats, days: int = 1, queue_label: str = "All Queues"
) -> discord.Embed:
    color = LOL_GREEN if not ps.error else LOL_RED
    period_label = "Last 24 hours" if days == 1 else f"Last {days} days"
    embed = discord.Embed(
        title=f"🔍 {ps.display_name}",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text=f"{period_label} • {queue_label}")

    if ps.error:
        embed.description = _truncate(f"⚠️ Error: `{ps.error}`", 4096)
        return embed

    lp_tag = _lp_change_str(ps.lp_change)
    rank_val = f"{_rank_emote(ps.solo_tier)} {ps.formatted_rank()}"
    if lp_tag:
        rank_val += f"\n{lp_tag}"

    embed.add_field(name="Rank", value=rank_val, inline=True)
    season_total = ps.solo_wins + ps.solo_losses
    season_wr = f" ({ps.solo_wins / season_total * 100:.0f}%)" if season_total else ""
    embed.add_field(name="Season SoloQ", value=f"{ps.solo_wins}W {ps.solo_losses}L{season_wr}", inline=True)
    embed.add_field(name="Level", value=str(ps.summoner_level or "—"), inline=True)

    if ps.games_played > 0:
        streak = ps.formatted_streak()
        streak_str = f" • {streak}" if streak else ""
        embed.add_field(
            name=period_label,
            value=(
                f"**{ps.games_played}** games • **{ps.wins}W {ps.losses}L** • **{ps.win_rate:.0f}%** WR{streak_str}\n"
                f"`{_win_rate_bar(ps.win_rate)}`"
            ),
            inline=False,
        )
        embed.add_field(name="KDA", value=ps.formatted_kda(), inline=True)
        embed.add_field(name="CS / min", value=f"{ps.cs_per_min:.1f}", inline=True)
        embed.add_field(name="Vision / game", value=f"{ps.avg_vision_score:.1f}", inline=True)
        embed.add_field(name="Avg Damage", value=f"{ps.avg_damage_per_game:,.0f} ({ps.avg_damage_share:.0f}%)", inline=True)
        embed.add_field(name="Kill Part.", value=f"{ps.avg_kill_participation:.0f}%", inline=True)
        embed.add_field(name="Hours", value=f"{ps.hours_played:.1f}h", inline=True)

        champs = ", ".join(f"{c} ×{n}" for c, n in ps.top_champions) or "N/A"
        embed.add_field(name="Top Champions", value=champs, inline=False)
    else:
        embed.description = f"*No games in the {period_label.lower()} ({queue_label}).*"

    return embed


def build_lastgame_embed(game: dict, commentary: Optional[str] = None) -> discord.Embed:
    if game.get("error"):
        return discord.Embed(
            title=f"🎮 {game['display_name']}'s Last Game",
            description=_truncate(f"⚠️ Error: `{game['error']}`", 4096),
            color=LOL_RED,
            timestamp=datetime.now(timezone.utc),
        )

    m = game.get("match")
    if not m:
        return discord.Embed(
            title=f"🎮 {game['display_name']}'s Last Game",
            description="*No recent non-remake games found.*",
            color=LOL_BLUE,
            timestamp=datetime.now(timezone.utc),
        )

    mins, secs = divmod(m.duration_seconds, 60)
    result_str = "✅ VICTORY" if m.win else "❌ DEFEAT"
    color = LOL_GREEN if m.win else LOL_RED

    embed = discord.Embed(
        title=f"{result_str} — {game['display_name']} ({m.champion})",
        description=f"**{m.queue_name}** • {mins}m {secs:02d}s",
        color=color,
        timestamp=datetime.fromtimestamp(m.game_creation / 1000, tz=timezone.utc)
        if m.game_creation
        else datetime.now(timezone.utc),
    )

    embed.add_field(name="KDA", value=f"**{m.kills}/{m.deaths}/{m.assists}** ({m.kda:.2f})", inline=True)
    embed.add_field(name="CS", value=f"**{m.cs}** ({m.cs_per_min:.1f}/m)", inline=True)
    embed.add_field(name="Damage", value=f"**{m.damage_to_champions:,}** ({m.damage_share:.0f}%)", inline=True)
    embed.add_field(name="Kill Part.", value=f"{m.kill_participation:.0f}%", inline=True)
    embed.add_field(name="Vision", value=str(m.vision_score), inline=True)
    embed.add_field(name="Gold", value=f"{m.gold_earned:,}", inline=True)

    friends = game.get("friends_in_game") or []
    if friends:
        embed.add_field(name="🤝 Squadmates", value=", ".join(friends), inline=False)

    if commentary:
        embed.add_field(name="🧠 Jeff's Take", value=_truncate(commentary, 1024), inline=False)

    embed.set_footer(text=f"Match {m.match_id} • Run /review for full timeline VOD coaching")
    return embed


# -------------------------------------------------------------------------
# 1. Timeline VOD Review Embed (/review)
# -------------------------------------------------------------------------

def build_review_embed(report: TimelineReport, review_text: Optional[str]) -> discord.Embed:
    mins, secs = divmod(report.duration_seconds, 60)
    res = "✅ WIN" if report.win else "❌ LOSS"
    opp = f" vs {report.lane_opponent_champ}" if report.lane_opponent_champ else ""

    embed = discord.Embed(
        title=f"🎬 VOD Review — {report.display_name} ({report.champion}{opp})",
        description=(
            f"**{res}** • {report.queue_name} • {report.role} • {mins}m {secs:02d}s\n"
            f"⚔️ **{report.kills}/{report.deaths}/{report.assists}** ({report.kda:.2f}) • "
            f"🌾 **{report.cs}** ({report.cs_per_min:.1f}/m) • "
            f"💥 **{report.damage_to_champions:,}** ({report.damage_share:.0f}%) • "
            f"👁️ {report.vision_score} ({report.control_wards} pinks)"
        ),
        color=LOL_GREEN if report.win else LOL_RED,
        timestamp=datetime.now(timezone.utc),
    )

    # Laning phase diffs
    lane_lines = []
    if report.gold_diff_10 is not None:
        lane_lines.append(f"**@10m:** {report.gold_diff_10:+,}g • {report.cs_diff_10:+d} CS")
    if report.gold_diff_15 is not None:
        lane_lines.append(f"**@15m:** {report.gold_diff_15:+,}g • {report.cs_diff_15:+d} CS")
    lane_lines.append(
        f"**Pre-14m:** {report.pre14_kills}K ({report.solo_kills_pre14} solo) / "
        f"{report.pre14_deaths}D ({report.gank_deaths_pre14} ganked)"
    )
    embed.add_field(name="⏱️ Laning Phase", value="\n".join(lane_lines), inline=True)

    # Gold swing & objectives
    swing_lines = [
        f"Peak Lead: **+{report.max_team_gold_lead:,}g**",
        f"Max Deficit: **{report.max_team_gold_deficit:,}g**",
        f"Drakes: **{report.team_dragons}–{report.enemy_dragons}** • Barons: **{report.team_barons}–{report.enemy_barons}**",
    ]
    embed.add_field(name="📊 Macro & Swings", value="\n".join(swing_lines), inline=True)

    if report.item_timings:
        embed.add_field(
            name="🛒 Item Spikes",
            value=_truncate(" → ".join(report.item_timings), 1024),
            inline=False,
        )

    flags = []
    for d in report.objective_throw_deaths[:3]:
        flags.append(f"⚠️ {d}")
    for c in report.death_clusters[:2]:
        flags.append(f"💀 {c}")
    if flags:
        embed.add_field(name="🚩 Key Moments", value=_truncate("\n".join(flags), 1024), inline=False)

    if review_text:
        embed.add_field(name="🧠 Coach Jeff's VOD Breakdown", value=_truncate(review_text, 1024), inline=False)

    embed.set_footer(text=f"Match {report.match_id} • Powered by Riot Timeline + DeepSeek")
    return embed


# -------------------------------------------------------------------------
# 2. Coach (Wins vs Losses), Pool Audit & Weekly Goals
# -------------------------------------------------------------------------

def build_coach_embed(ps: PlayerStats, split: dict, advice: Optional[str], days: int = 14) -> discord.Embed:
    embed = discord.Embed(
        title=f"📋 Coach Jeff — {ps.display_name}",
        description=f"{_rank_emote(ps.solo_tier)} **{ps.formatted_rank()}** • Last {days}d: **{ps.wins}W {ps.losses}L** ({ps.win_rate:.0f}% WR)",
        color=LOL_BLUE,
        timestamp=datetime.now(timezone.utc),
    )

    if split.get("wins"):
        w = split["wins"]
        embed.add_field(
            name=f"✅ In Wins ({w['games']}g)",
            value=(
                f"KDA: **{w['avg_kills']:.1f}/{w['avg_deaths']:.1f}/{w['avg_assists']:.1f}** ({w['kda']:.2f})\n"
                f"CS/m: **{w['cs_per_min']:.1f}** • KP: **{w['avg_kp']:.0f}%**\n"
                f"Vision: **{w['avg_vision']:.1f}** ({w['avg_control_wards']:.1f} pinks)"
            ),
            inline=True,
        )
    if split.get("losses"):
        l = split["losses"]
        embed.add_field(
            name=f"❌ In Losses ({l['games']}g)",
            value=(
                f"KDA: **{l['avg_kills']:.1f}/{l['avg_deaths']:.1f}/{l['avg_assists']:.1f}** ({l['kda']:.2f})\n"
                f"CS/m: **{l['cs_per_min']:.1f}** • KP: **{l['avg_kp']:.0f}%**\n"
                f"Vision: **{l['avg_vision']:.1f}** ({l['avg_control_wards']:.1f} pinks)"
            ),
            inline=True,
        )

    if advice:
        embed.add_field(name="🧠 Coaching Takeaways", value=_truncate(advice, 1024), inline=False)

    embed.set_footer(text="Powered by DeepSeek • Use /pool for champ audit or /goal for a weekly target")
    return embed


def build_pool_embed(ps: PlayerStats, pool: dict, verdict: Optional[str], days: int = 30) -> discord.Embed:
    embed = discord.Embed(
        title=f"🎯 Champion Pool Audit — {ps.display_name}",
        description=(
            f"**{pool['total_games']} games** across **{pool['unique_champions']} champions** "
            f"(last {days} days)"
        ),
        color=LOL_GOLD,
        timestamp=datetime.now(timezone.utc),
    )

    def _fmt_bucket(items: list[dict], limit: int = 5) -> str:
        return "\n".join(
            f"• **{c['champion']}** — {c['wins']}W {c['losses']}L (**{c['wr']:.0f}%**) • "
            f"{c['kda']:.2f} KDA • {c['cs_per_min']:.1f} CS/m"
            for c in items[:limit]
        )

    if pool["climb"]:
        embed.add_field(name="🟢 Climb Champs (55%+ WR)", value=_fmt_bucket(pool["climb"]), inline=False)
    if pool["solid"]:
        embed.add_field(name="🟡 Solid Picks (45–54% WR)", value=_fmt_bucket(pool["solid"]), inline=False)
    if pool["traps"]:
        embed.add_field(name="🔴 LP Bleed / Trap Champs (<45% WR)", value=_fmt_bucket(pool["traps"]), inline=False)
    if not (pool["climb"] or pool["solid"] or pool["traps"]) and pool["one_offs"]:
        embed.add_field(name="🎲 One-Off Picks (1 game each)", value=_fmt_bucket(pool["one_offs"], limit=8), inline=False)

    if pool["roles"]:
        role_str = "  •  ".join(
            f"**{r['role']}:** {r['wins']}W {r['losses']}L ({r['wr']:.0f}%)" for r in pool["roles"][:5]
        )
        embed.add_field(name="🧭 Role Breakdown", value=role_str, inline=False)

    if verdict:
        embed.add_field(name="🧠 Coach Jeff's Verdict", value=_truncate(verdict, 1024), inline=False)

    embed.set_footer(text="Minimum 2 games to classify • Powered by DeepSeek")
    return embed


def build_goal_embed(display_name: str, eval_result: dict, is_new: bool = False) -> discord.Embed:
    goal = eval_result["goal"]
    met = eval_result["met"]
    games = eval_result["games"]
    status = "🆕 New Weekly Goal Set!" if is_new else ("✅ Currently On Track!" if met else "⏳ Keep Grinding!")
    color = LOL_GREEN if met else LOL_GOLD

    embed = discord.Embed(
        title=f"🎯 Weekly Coaching Goal — {display_name}",
        description=f"**{goal['description']}**",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Baseline", value=f"{eval_result['baseline']:.1f} {eval_result['unit']}", inline=True)
    embed.add_field(name="Target", value=f"{eval_result['op']} {eval_result['target']:.1f} {eval_result['unit']}", inline=True)
    embed.add_field(
        name="Current Progress",
        value=f"{eval_result['current']:.1f} {eval_result['unit']} ({games}g)" if games else "0 games since set",
        inline=True,
    )
    expires_dt = datetime.fromtimestamp(goal["expires_at"], tz=timezone.utc).strftime("%b %d")
    embed.set_footer(text=f"{status} • Graded in Weekly Digest (target date: {expires_dt})")
    return embed


# -------------------------------------------------------------------------
# 3. Loading-Screen Scout Embed (/scout)
# -------------------------------------------------------------------------

def build_scout_embed(scout: dict, gameplan: Optional[str]) -> discord.Embed:
    if scout.get("error"):
        return discord.Embed(
            title=f"🔭 Live Scout — {scout['display_name']}",
            description=_truncate(f"⚠️ Error: `{scout['error']}`", 4096),
            color=LOL_RED,
            timestamp=datetime.now(timezone.utc),
        )

    if not scout.get("in_game"):
        return discord.Embed(
            title=f"🔭 Live Scout — {scout['display_name']}",
            description=f"*{scout['display_name']} is not currently in a live game.* Run `/scout` while on the loading screen!",
            color=LOL_BLUE,
            timestamp=datetime.now(timezone.utc),
        )

    mins = max(0, scout["length_seconds"]) // 60
    embed = discord.Embed(
        title=f"🔭 Live Scout — {scout['display_name']} ({scout['player_champ']})",
        description=f"**{scout['queue_name']}** • {mins}m in",
        color=LOL_PURPLE,
        timestamp=datetime.now(timezone.utc),
    )

    def _fmt_team(players: list[dict]) -> str:
        lines = []
        for p in players:
            me_tag = " ⭐" if p["is_target"] else ""
            wr = f" ({p['solo_wr']}%)" if p["solo_wr"] is not None else ""
            pts = f"{p['mastery_pts'] // 1000}k" if p["mastery_pts"] >= 1000 else f"{p['mastery_pts']}"
            badge = ""
            if p["streak"] <= -3:
                badge = f" 🥶{abs(p['streak'])}L"
            elif p["streak"] >= 3:
                badge = f" 🔥{p['streak']}W"
            elif p["mastery_pts"] < 5000:
                badge = " 🆕"
            lines.append(f"**{p['champion']}**{me_tag} — {p['rank']}{wr} • {pts} pts{badge}")
        return "\n".join(lines) or "N/A"

    embed.add_field(name="🔵 Your Team", value=_truncate(_fmt_team(scout["allies"]), 1024), inline=False)
    embed.add_field(name="🔴 Enemy Team", value=_truncate(_fmt_team(scout["enemies"]), 1024), inline=False)

    if gameplan:
        embed.add_field(name="🧠 Loading-Screen Gameplan", value=_truncate(gameplan, 1024), inline=False)

    embed.set_footer(text="🔥 Hot streak • 🥶 Tilt/loss streak • 🆕 Low mastery • Powered by DeepSeek")
    return embed


# -------------------------------------------------------------------------
# 4. Squad Debrief & Comp Helper Embeds
# -------------------------------------------------------------------------

def build_squad_debrief_embed(debrief: dict, commentary: Optional[str]) -> discord.Embed:
    mins, secs = divmod(debrief["duration_seconds"], 60)
    n = len(debrief["members"])
    res = "✅ SQUAD VICTORY" if debrief["win"] else "❌ SQUAD DEFEAT"
    color = LOL_GREEN if debrief["win"] else LOL_RED

    embed = discord.Embed(
        title=f"{res} — {n}-Stack Debrief",
        description=f"**{debrief['queue_name']}** • {mins}m {secs:02d}s",
        color=color,
        timestamp=datetime.fromtimestamp(debrief["game_creation"] / 1000, tz=timezone.utc)
        if debrief.get("game_creation")
        else datetime.now(timezone.utc),
    )

    for m in debrief["members"]:
        r = m["record"]
        crown = " 👑" if m["display_name"] == debrief["mvp"] else ""
        embed.add_field(
            name=f"{m['display_name']}{crown} ({r.champion})",
            value=(
                f"⚔️ **{r.kills}/{r.deaths}/{r.assists}** ({r.kda:.2f})\n"
                f"🌾 {r.cs_per_min:.1f} CS/m • 👁️ {r.vision_score}\n"
                f"💥 {r.damage_to_champions:,} ({r.damage_share:.0f}%) • KP {r.kill_participation:.0f}%"
            ),
            inline=True,
        )

    if len(debrief["members"]) % 2 != 0 and len(embed.fields) < 25:
        embed.add_field(name="\u200b", value="\u200b", inline=True)

    md_name, md_count = debrief["most_deaths"]
    embed.add_field(
        name="🏅 Highlights",
        value=f"👑 **Squad MVP:** {debrief['mvp']}  •  💀 **Most Deaths:** {md_name} ({md_count})",
        inline=False,
    )

    if commentary:
        embed.add_field(name="🧠 Jeff's Squad Debrief", value=_truncate(commentary, 1024), inline=False)

    embed.set_footer(text=f"Match {debrief['match_id']} • Run /review <player> for timeline breakdown")
    return embed


def build_comp_embed(suggestions: str, notes: str = "") -> discord.Embed:
    embed = discord.Embed(
        title="🧩 Squad Draft & Comp Builder",
        description=_truncate(suggestions, 4096),
        color=LOL_PURPLE,
        timestamp=datetime.now(timezone.utc),
    )
    footer = "Built from your squad's real 14-day champion pools • Powered by DeepSeek"
    if notes:
        embed.add_field(name="📝 Request", value=_truncate(notes, 1024), inline=False)
    embed.set_footer(text=footer)
    return embed


# -------------------------------------------------------------------------
# 5. Interactive /ask Embed
# -------------------------------------------------------------------------

def build_ask_embed(question: str, answer: str, asker: str) -> discord.Embed:
    embed = discord.Embed(
        title="🧠 Ask Coach Jeff",
        description=_truncate(answer, 4096),
        color=LOL_BLUE,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name=f"❓ {asker} asked", value=_truncate(question, 1024), inline=False)
    embed.set_footer(text="Grounded in your squad's tracked stats • Powered by DeepSeek")
    return embed


def build_item_build_embed(
    champion: str,
    build: dict,
    enemies: Optional[list[str]] = None,
    allies: Optional[list[str]] = None,
    role: str = "",
    patch: str = "",
) -> discord.Embed:
    """Concise companion rationale embed attached beneath the rendered Build Path PNG card."""
    title_role = f" ({role})" if role else ""
    embed = discord.Embed(
        title=f"🛠️ {champion}{title_role} — Build Breakdown",
        color=LOL_GOLD,
        timestamp=datetime.now(timezone.utc),
    )
    if build.get("gameplan"):
        embed.description = _truncate(build["gameplan"], 4096)

    core_lines = []
    if build.get("starter", {}).get("item"):
        core_lines.append(f"**Starter:** {build['starter']['item']} — *{build['starter'].get('why', '')}*")
    if build.get("boots", {}).get("item"):
        core_lines.append(f"**Boots:** {build['boots']['item']} — *{build['boots'].get('why', '')}*")
    for idx, c in enumerate(build.get("core", [])[:3], 1):
        core_lines.append(f"**Core {idx}:** {c['item']} — *{c.get('why', '')}*")
    if core_lines:
        embed.add_field(name="⚔️ Core Order", value=_truncate("\n".join(core_lines), 1024), inline=False)

    sit_lines = [
        f"• **{s['item']}** — *{s.get('why', '')}*"
        for s in build.get("situational", [])[:3]
        if s.get("item")
    ]
    if sit_lines:
        embed.add_field(name="🔮 Situational Swaps", value=_truncate("\n".join(sit_lines), 1024), inline=False)

    if build.get("runes"):
        embed.add_field(name="✨ Runes", value=_truncate(build["runes"], 1024), inline=True)
    if enemies:
        embed.add_field(name="🔴 vs Enemy", value=_truncate(", ".join(enemies), 1024), inline=True)
    if allies:
        embed.add_field(name="🔵 Allies", value=_truncate(", ".join(allies), 1024), inline=True)

    footer = f"Patch {patch} • Powered by DeepSeek" if patch else "Powered by DeepSeek"
    embed.set_footer(text=footer)
    return embed


# -------------------------------------------------------------------------
# Rank Alerts, Versus, Damage, Synergy, Live, Mastery, Roast, Status
# -------------------------------------------------------------------------

def build_promotion_embed(event: dict, commentary: Optional[str] = None) -> discord.Embed:
    old_emote = _rank_emote(event["old_tier"])
    new_emote = _rank_emote(event["new_tier"])
    old_fmt = format_rank(event["old_tier"], event["old_rank"], event["old_lp"])
    new_fmt = format_rank(event["new_tier"], event["new_rank"], event["new_lp"])

    embed = discord.Embed(
        title=f"🎉 {event['display_name']} Promoted!",
        description=f"{old_emote} **{event['old_tier'].capitalize()}** ➡️ {new_emote} **{event['new_tier'].capitalize()}**",
        color=LOL_GOLD,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Previous Rank", value=old_fmt, inline=True)
    embed.add_field(name="New Rank", value=new_fmt, inline=True)
    if commentary:
        embed.add_field(name="🧠 Jeff Says", value=_truncate(commentary, 1024), inline=False)
    embed.set_footer(text="Congrats! 🏆")
    return embed


def build_demotion_embed(event: dict, commentary: Optional[str] = None) -> discord.Embed:
    old_emote = _rank_emote(event["old_tier"])
    new_emote = _rank_emote(event["new_tier"])
    old_fmt = format_rank(event["old_tier"], event["old_rank"], event["old_lp"])
    new_fmt = format_rank(event["new_tier"], event["new_rank"], event["new_lp"])

    embed = discord.Embed(
        title=f"📉 {event['display_name']} Demoted...",
        description=f"{old_emote} **{event['old_tier'].capitalize()}** ➡️ {new_emote} **{event['new_tier'].capitalize()}**",
        color=LOL_RED,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Previous Rank", value=old_fmt, inline=True)
    embed.add_field(name="Current Rank", value=new_fmt, inline=True)
    if commentary:
        embed.add_field(name="🧠 Jeff Says", value=_truncate(commentary, 1024), inline=False)
    embed.set_footer(text="Bounce back incoming 💀")
    return embed


def _winner(v1, v2) -> tuple[bool, bool]:
    if v1 == v2:
        return False, False
    return (v1 > v2, v2 > v1)


def build_versus_embed(a: PlayerStats, b: PlayerStats) -> discord.Embed:
    embed = discord.Embed(
        title=f"⚔️ {a.display_name} vs {b.display_name}",
        color=LOL_BLUE,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="Weekly comparison (last 7 days)")

    if a.error or b.error:
        issues = []
        if a.error:
            issues.append(f"{a.display_name}: `{a.error}`")
        if b.error:
            issues.append(f"{b.display_name}: `{b.error}`")
        embed.description = _truncate("⚠️ Could not load one or both players:\n" + "\n".join(issues), 4096)
        embed.colour = LOL_RED
        return embed

    a_top = a.top_champions[0][0] if a.top_champions else "N/A"
    b_top = b.top_champions[0][0] if b.top_champions else "N/A"

    categories = [
        ("Rank",     a.formatted_rank(),                     b.formatted_rank(),                    a.overall_rank_score,        b.overall_rank_score),
        ("Games",    str(a.games_played),                    str(b.games_played),                   a.games_played,              b.games_played),
        ("Win Rate", f"{a.win_rate:.0f}%",                   f"{b.win_rate:.0f}%",                  a.win_rate,                  b.win_rate),
        ("W / L",    f"{a.wins}W {a.losses}L",               f"{b.wins}W {b.losses}L",              a.wins,                      b.wins),
        ("KDA",      f"{a.kda:.2f}",                         f"{b.kda:.2f}",                        a.kda,                       b.kda),
        ("CS / min", f"{a.cs_per_min:.1f}",                  f"{b.cs_per_min:.1f}",                 a.cs_per_min,                b.cs_per_min),
        ("Vision",   f"{a.avg_vision_score:.1f}",            f"{b.avg_vision_score:.1f}",           a.avg_vision_score,          b.avg_vision_score),
        ("Kill Part.", f"{a.avg_kill_participation:.0f}%",   f"{b.avg_kill_participation:.0f}%",    a.avg_kill_participation,    b.avg_kill_participation),
        ("Hours",    f"{a.hours_played:.1f}h",               f"{b.hours_played:.1f}h",              a.hours_played,              b.hours_played),
        ("Damage",   f"{a.total_damage_to_champions:,}",     f"{b.total_damage_to_champions:,}",    a.total_damage_to_champions, b.total_damage_to_champions),
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

    embed.description = _truncate("\n".join(lines), 4096)
    return embed


def build_damage_embed(stats: list[PlayerStats]) -> discord.Embed:
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
        total += ps.total_damage_to_champions
        lines.append(
            f"{medal} **{ps.display_name}** — {ps.total_damage_to_champions:,} dmg "
            f"({ps.games_played} games • {ps.avg_damage_per_game:,.0f} / game)"
        )

    inactive = [ps for ps in stats if ps not in active]
    for ps in inactive:
        if ps.error:
            lines.append(f"⚠️ **{ps.display_name}** — error: `{ps.error}`")
        else:
            lines.append(f"— **{ps.display_name}** — no games this week")

    embed.description = _truncate("\n".join(lines), 4096)
    embed.add_field(name="Group Total", value=f"**{total:,}** damage", inline=False)
    return embed


def build_synergy_embed(synergy: dict) -> discord.Embed:
    days = synergy.get("days", 7)
    embed = discord.Embed(
        title="🤝 Squad Synergy",
        color=LOL_PURPLE,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text=f"Last {days} days • Remakes excluded")

    tog_g = synergy.get("together_games", 0)
    solo_g = synergy.get("solo_games", 0)
    if tog_g == 0 and solo_g == 0:
        embed.description = "*No tracked games found in this window.*"
        return embed

    summary = []
    if tog_g:
        summary.append(
            f"👥 **Playing Together:** {synergy['together_wins']}W {synergy['together_losses']}L "
            f"(**{synergy['together_wr']:.0f}%** WR across {tog_g} games)"
        )
    else:
        summary.append("👥 **Playing Together:** 0 games")

    if solo_g:
        summary.append(
            f"🧍 **Playing Solo:** {synergy['solo_wins']}W {synergy['solo_losses']}L "
            f"(**{synergy['solo_wr']:.0f}%** WR across {solo_g} games)"
        )

    embed.description = "\n".join(summary)

    duos = synergy.get("duos", [])
    if duos:
        duo_lines = []
        for d in duos:
            bar = _win_rate_bar(d["wr"], length=8)
            duo_lines.append(
                f"**{d['pair']}** — {d['wins']}W {d['losses']}L ({d['wr']:.0f}%) `{bar}`"
            )
        embed.add_field(name="Duo Records", value=_truncate("\n".join(duo_lines), 1024), inline=False)

    stacks = synergy.get("stacks", {})
    if stacks:
        stack_lines = [
            f"**{size}-stack:** {w}W {l}L ({w / (w + l) * 100:.0f}%)"
            for size, (w, l) in stacks.items()
            if (w + l) > 0
        ]
        if stack_lines:
            embed.add_field(name="By Party Size", value="  •  ".join(stack_lines), inline=False)

    return embed


def build_live_embed(live_games: list[dict]) -> discord.Embed:
    embed = discord.Embed(
        title="🟢 Live Games",
        color=LOL_GREEN if live_games else LOL_BLUE,
        timestamp=datetime.now(timezone.utc),
    )
    if not live_games:
        embed.description = "*Nobody from the squad is currently in a match.*"
        return embed

    for g in live_games[:25]:
        mins = max(0, g["length_seconds"]) // 60
        players_str = "\n".join(
            f"• **{p['display_name']}** on **{p['champion']}**" for p in g["players"]
        )
        header = f"{g['queue_name']} ({mins}m in)"
        if len(g["players"]) >= 2:
            header = f"🤝 {len(g['players'])}-Stack • {header}"
        embed.add_field(name=header, value=players_str, inline=False)
    embed.set_footer(text="Run /scout <player> for a DeepSeek loading-screen gameplan")
    return embed


def build_mastery_embed(mastery: dict) -> discord.Embed:
    if mastery.get("error"):
        return discord.Embed(
            title=f"🧙 {mastery['display_name']}'s Top Champions",
            description=_truncate(f"⚠️ Error: `{mastery['error']}`", 4096),
            color=LOL_RED,
            timestamp=datetime.now(timezone.utc),
        )

    masteries = mastery.get("masteries", [])
    if not masteries:
        description = "*No mastery data available.*"
    else:
        lines = [
            f"**{i}.** {m['name']} — Level {m['level']} • {m['points']:,} pts"
            for i, m in enumerate(masteries, 1)
        ]
        description = "\n".join(lines)

    embed = discord.Embed(
        title=f"🧙 {mastery['display_name']}'s Top Champions",
        description=description,
        color=LOL_GOLD,
        timestamp=datetime.now(timezone.utc),
    )
    version = mastery.get("version")
    embed.set_footer(text=f"Patch {version}" if version else "Champion mastery")
    return embed


def build_ai_embed(ps: PlayerStats, text: str, mode: str = "roast") -> discord.Embed:
    is_roast = mode == "roast"
    embed = discord.Embed(
        title=f"🔥 Jeff Roasts {ps.display_name}" if is_roast else f"📋 Coach Jeff — {ps.display_name}",
        description=_truncate(text, 4096),
        color=LOL_RED if is_roast else LOL_BLUE,
        timestamp=datetime.now(timezone.utc),
    )
    if ps.games_played > 0:
        embed.set_footer(
            text=(
                f"7d: {ps.wins}W {ps.losses}L ({ps.win_rate:.0f}% WR) • "
                f"{ps.kda:.2f} KDA • {ps.cs_per_min:.1f} CS/m • Powered by DeepSeek"
            )
        )
    else:
        embed.set_footer(text="Powered by DeepSeek")
    return embed


def build_status_embed(status: dict) -> discord.Embed:
    all_ok = status["riot_ok"]
    embed = discord.Embed(
        title="🩺 Jeff Bot Status",
        color=LOL_GREEN if all_ok else LOL_RED,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="Riot API",
        value=f"{'✅' if status['riot_ok'] else '❌'} {status['riot_msg']}",
        inline=False,
    )
    embed.add_field(
        name="DeepSeek AI",
        value=f"{'✅' if status['ai_ok'] else '⚪'} {status['ai_msg']}",
        inline=False,
    )
    if status.get("voice_msg"):
        embed.add_field(
            name="Voice / TTS",
            value=status["voice_msg"],
            inline=False,
        )
    embed.add_field(
        name="Tracked Squad",
        value=", ".join(status["roster"]) or "(empty)",
        inline=False,
    )
    embed.add_field(name="Cached Matches", value=str(status["cached_matches"]), inline=True)
    embed.add_field(name="Target Channel", value=status["channel_label"], inline=True)
    if status.get("next_runs"):
        embed.add_field(name="Next Scheduled Runs", value="\n".join(status["next_runs"]), inline=False)
    return embed
