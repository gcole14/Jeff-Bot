import aiohttp
import json
import logging
import ssl
from typing import Optional

try:
    import certifi
    _SSL_CONTEXT: Optional[ssl.SSLContext] = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = None

from coaching import TimelineReport
from config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL, AI_ENABLED

logger = logging.getLogger(__name__)

AI_TIMEOUT = aiohttp.ClientTimeout(total=35)

JEFF_SYSTEM_PROMPT = (
    "You are Jeff, the sharp-tongued but lovable League of Legends stats bot for a tight-knit "
    "4-person friend group's Discord server. You speak casually like one of the boys—witty, "
    "playful trash-talk when someone feeds or goes on a loss streak, genuine hype when someone "
    "carries or climbs. Keep responses punchy, grounded ONLY in the exact stats provided "
    "(never invent champions, KDA, or ranks not in the data), and free of slurs or genuinely mean attacks."
)

COACH_SYSTEM_PROMPT = (
    "You are Coach Jeff, a sharp Challenger-level League of Legends analyst coaching a friend group. "
    "Give concise, concrete, high-signal League advice grounded strictly in the provided data "
    "(timestamps, gold/CS diffs, win-vs-loss splits, champion pool, enemy comp, and items). "
    "Skip fluff and generic platitudes; cite the exact numbers that matter."
)


class DeepSeekClient:
    """Async DeepSeek Chat Completions client using aiohttp (zero extra SDKs)."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        enabled: Optional[bool] = None,
    ):
        self.api_key = (api_key if api_key is not None else DEEPSEEK_API_KEY) or ""
        self.base_url = (base_url or DEEPSEEK_BASE_URL).rstrip("/")
        self.model = model or DEEPSEEK_MODEL
        self.enabled = AI_ENABLED if enabled is None else (enabled and bool(self.api_key))
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def is_configured(self) -> bool:
        return bool(self.enabled and self.api_key)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=_SSL_CONTEXT) if _SSL_CONTEXT else None
            self._session = aiohttp.ClientSession(timeout=AI_TIMEOUT, connector=connector)
        return self._session

    async def _chat(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 300,
        temperature: float = 0.75,
        json_mode: bool = False,
    ) -> Optional[str]:
        if not self.is_configured:
            return None

        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        try:
            session = await self._get_session()
            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(f"DeepSeek API {resp.status}: {body[:200]}")
                    return None
                data = await resp.json()
                choices = data.get("choices", [])
                if not choices:
                    return None
                content = choices[0].get("message", {}).get("content", "").strip()
                return content or None
        except Exception as e:
            logger.warning(f"DeepSeek request failed: {e}")
            return None

    async def verify_key(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "DEEPSEEK_API_KEY not set (AI features disabled)"
        if not self.enabled:
            return False, "Disabled via AI_ENABLED=false"
        url = f"{self.base_url}/models"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            session = await self._get_session()
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    return True, f"Connected ({self.model})"
                body = await resp.text()
                return False, f"HTTP {resp.status}: {body[:120]}"
        except Exception as e:
            return False, str(e)

    # -------------------------------------------------------------------------
    # 1. Timeline VOD Review (/review)
    # -------------------------------------------------------------------------

    async def review_match_timeline(self, report: TimelineReport) -> Optional[str]:
        lines = [
            f"Player: {report.display_name} on {report.champion} ({report.role}) — {'WIN' if report.win else 'LOSS'}",
            f"Queue: {report.queue_name} | Duration: {report.duration_seconds // 60}m {report.duration_seconds % 60:02d}s",
            f"Scoreline: {report.kills}/{report.deaths}/{report.assists} ({report.kda:.2f} KDA) | "
            f"CS: {report.cs} ({report.cs_per_min:.1f}/m) | Vision: {report.vision_score} ({report.control_wards} pinks)",
            f"Damage: {report.damage_to_champions:,} ({report.damage_share:.0f}% team dmg) | KP: {report.kill_participation:.0f}%",
            f"Allies: {', '.join(report.ally_champions)}",
            f"Enemies: {', '.join(report.enemy_champions)}",
            f"Final items: {', '.join(report.final_items) or 'None'}",
            f"Objectives: Team {report.team_dragons} Drakes / {report.team_barons} Barons vs "
            f"Enemy {report.enemy_dragons} Drakes / {report.enemy_barons} Barons",
        ]
        if report.lane_opponent_champ:
            diff_parts = []
            if report.gold_diff_10 is not None:
                diff_parts.append(f"@10m: {report.gold_diff_10:+d}g, {report.cs_diff_10:+d} CS")
            if report.gold_diff_15 is not None:
                diff_parts.append(f"@15m: {report.gold_diff_15:+d}g, {report.cs_diff_15:+d} CS")
            lines.append(f"Lane vs {report.lane_opponent_champ}: {' | '.join(diff_parts) or 'N/A'}")
        lines.append(
            f"Early lane (<14m): {report.pre14_kills}K ({report.solo_kills_pre14} solo) / "
            f"{report.pre14_deaths}D ({report.gank_deaths_pre14} from ganks/skirmishes)"
        )
        lines.append(
            f"Team gold swing: peak lead +{report.max_team_gold_lead:,}g, "
            f"deepest deficit {report.max_team_gold_deficit:,}g"
        )
        if report.item_timings:
            lines.append("Item spikes: " + ", ".join(report.item_timings))
        if report.objective_throw_deaths:
            lines.append("Objective-window deaths: " + "; ".join(report.objective_throw_deaths))
        if report.death_clusters:
            lines.append("Death clusters: " + "; ".join(report.death_clusters))

        prompt = (
            "Write a tight, high-impact VOD review in 3 short labeled sections (under 180 words total):\n"
            "**Laning:** (cite @10/@15 diffs & early kills/deaths)\n"
            "**Turning Points:** (cite throw/objective deaths, gold swings, or tempo)\n"
            "**Build & Fix for Next Game:** (critique itemization vs enemy comp + 1 concrete habit to change)\n\n"
            + "\n".join(lines)
        )
        return await self._chat(COACH_SYSTEM_PROMPT, prompt, max_tokens=320, temperature=0.55)

    # -------------------------------------------------------------------------
    # 2. Wins vs Losses Coaching (/coach) & Champion Pool Audit (/pool)
    # -------------------------------------------------------------------------

    async def coach_player(self, ps, split: dict, pool: dict) -> Optional[str]:
        if ps.games_played == 0:
            return None

        champs = ", ".join(
            f"{c['champion']} ({c['wins']}W-{c['losses']}L, {c['kda']:.1f} KDA)"
            for c in pool.get("all_champs", [])[:6]
        ) or "N/A"
        roles = ", ".join(
            f"{r['role']} {r['wins']}W-{r['losses']}L" for r in pool.get("roles", [])[:3]
        ) or "N/A"

        lines = [
            f"Player: {ps.display_name} ({ps.formatted_rank()})",
            f"Sample ({ps.games_played} games): {ps.wins}W-{ps.losses}L ({ps.win_rate:.0f}% WR)",
            f"Overall: KDA {ps.formatted_kda()}, {ps.cs_per_min:.1f} CS/m, "
            f"{ps.avg_vision_score:.1f} vision ({ps.avg_control_wards:.1f} pinks/g), "
            f"{ps.avg_kill_participation:.0f}% KP, {ps.avg_damage_share:.0f}% dmg share",
            f"Roles: {roles}",
            f"Champions: {champs}",
        ]
        if split.get("wins"):
            w = split["wins"]
            lines.append(
                f"IN WINS ({w['games']}g): {w['avg_kills']:.1f}/{w['avg_deaths']:.1f}/{w['avg_assists']:.1f} "
                f"({w['kda']:.2f} KDA), {w['cs_per_min']:.1f} CS/m, {w['avg_vision']:.1f} vis, "
                f"{w['avg_kp']:.0f}% KP, {w['avg_dmg_share']:.0f}% dmg"
            )
        if split.get("losses"):
            l = split["losses"]
            lines.append(
                f"IN LOSSES ({l['games']}g): {l['avg_kills']:.1f}/{l['avg_deaths']:.1f}/{l['avg_assists']:.1f} "
                f"({l['kda']:.2f} KDA), {l['cs_per_min']:.1f} CS/m, {l['avg_vision']:.1f} vis, "
                f"{l['avg_kp']:.0f}% KP, {l['avg_dmg_share']:.0f}% dmg"
            )

        prompt = (
            "Analyze the Win vs Loss split and champion/role stats above. Give 3 sharp, concrete "
            "bullet points under 170 words total:\n"
            "1. The #1 stat separating their wins from their losses\n"
            "2. Champion pool / role adjustment\n"
            "3. One specific in-game habit to focus on next session\n\n"
            + "\n".join(lines)
        )
        return await self._chat(COACH_SYSTEM_PROMPT, prompt, max_tokens=280, temperature=0.55)

    async def audit_pool_verdict(self, ps, pool: dict) -> Optional[str]:
        if not pool.get("all_champs"):
            return None
        lines = [
            f"Player: {ps.display_name} ({ps.formatted_rank()}), {pool['total_games']} games across {pool['unique_champions']} champs.",
            "Climb champs: " + (", ".join(f"{c['champion']} ({c['wins']}W-{c['losses']}L)" for c in pool["climb"]) or "none"),
            "Trap/bleed champs: " + (", ".join(f"{c['champion']} ({c['wins']}W-{c['losses']}L)" for c in pool["traps"]) or "none"),
            "Roles: " + (", ".join(f"{r['role']} ({r['wins']}W-{r['losses']}L)" for r in pool["roles"]) or "none"),
        ]
        prompt = (
            "Write a punchy 2-3 sentence champion pool verdict telling this player who to spam to climb "
            "and who to stop picking immediately.\n\n" + "\n".join(lines)
        )
        return await self._chat(COACH_SYSTEM_PROMPT, prompt, max_tokens=150, temperature=0.6)

    # -------------------------------------------------------------------------
    # 3. Loading-Screen Gameplan (/scout)
    # -------------------------------------------------------------------------

    async def scout_live_game(self, scout: dict) -> Optional[str]:
        def _fmt_side(players: list[dict]) -> str:
            out = []
            for p in players:
                tag = " [USER]" if p["is_target"] else ""
                wr = f", {p['solo_wr']}% WR" if p["solo_wr"] is not None else ""
                pts = f"{p['mastery_pts'] // 1000}k pts" if p["mastery_pts"] >= 1000 else f"{p['mastery_pts']} pts"
                form = f", last5={p['recent_form']}" if p["recent_form"] else ""
                out.append(f"- {p['champion']} ({p['name']}{tag}): {p['rank']}{wr}, {pts}{form}")
            return "\n".join(out)

        prompt = (
            f"Loading-screen scout for {scout['display_name']} playing {scout['player_champ']} "
            f"in {scout['queue_name']}.\n\n"
            f"ALLY TEAM:\n{_fmt_side(scout['allies'])}\n\n"
            f"ENEMY TEAM:\n{_fmt_side(scout['enemies'])}\n\n"
            "Give a fast 30-second loading-screen gameplan (under 180 words) with 3 bullets:\n"
            "• **Target / Exploit:** (call out tilted loss streaks, first-timers, or biggest enemy threat)\n"
            "• **Win Condition:** (how your comp beats theirs)\n"
            "• **Situational Build:** (exact defensive/pen/anti-heal items for this lobby)"
        )
        return await self._chat(COACH_SYSTEM_PROMPT, prompt, max_tokens=280, temperature=0.55)

    # -------------------------------------------------------------------------
    # 4. Squad Debrief & Draft Helper (/comp)
    # -------------------------------------------------------------------------

    async def debrief_squad_game(self, debrief: dict) -> Optional[str]:
        lines = [
            f"Result: {'WIN' if debrief['win'] else 'LOSS'} ({debrief['queue_name']}, {debrief['duration_seconds'] // 60}m)",
            f"Squad MVP: {debrief['mvp']} | Most Deaths: {debrief['most_deaths'][0]} ({debrief['most_deaths'][1]}D)",
        ]
        for m in debrief["members"]:
            r = m["record"]
            lines.append(
                f"- {m['display_name']} on {r.champion}: {r.kills}/{r.deaths}/{r.assists}, "
                f"{r.cs_per_min:.1f} CS/m, {r.damage_to_champions:,} dmg ({r.damage_share:.0f}%), "
                f"{r.kill_participation:.0f}% KP, {r.vision_score} vision"
            )
        prompt = (
            "Write a punchy 2-3 sentence post-game squad debrief for Discord: highlight who carried, "
            "playfully call out any feeding or macro gap, and end with 1 quick coaching note.\n\n"
            + "\n".join(lines)
        )
        return await self._chat(JEFF_SYSTEM_PROMPT, prompt, max_tokens=180, temperature=0.8)

    async def suggest_squad_comps(self, squad_context: str, notes: str = "") -> Optional[str]:
        extra = f"\nUser request / constraints: {notes}\n" if notes else ""
        prompt = (
            "Using ONLY champions each friend actually plays well from the squad data below, "
            "suggest 2 synergistic 4-man squad team comps (under 220 words total). "
            "For each comp, assign each friend a champion, state the team win condition, "
            "and recommend what kind of random 5th role/champ fits best.\n"
            f"{extra}\n{squad_context}"
        )
        return await self._chat(COACH_SYSTEM_PROMPT, prompt, max_tokens=340, temperature=0.65)

    # -------------------------------------------------------------------------
    # 5. Interactive Q&A (/ask)
    # -------------------------------------------------------------------------

    async def answer_question(self, question: str, squad_context: str, asker: str) -> Optional[str]:
        prompt = (
            f"Discord user '{asker}' asks: {question}\n\n"
            f"Here is the squad's live tracked data:\n{squad_context}\n\n"
            "Answer directly and concisely (under 220 words). Use exact stats from the data "
            "when relevant, and League expertise for matchup/build/macro questions."
        )
        return await self._chat(COACH_SYSTEM_PROMPT, prompt, max_tokens=340, temperature=0.6)

    # -------------------------------------------------------------------------
    # 6. Adaptive Build Recommender (/build)
    # -------------------------------------------------------------------------

    async def recommend_build(
        self,
        champion: str,
        enemies: list[str],
        allies: Optional[list[str]] = None,
        role: str = "",
        patch: str = "",
        notes: str = "",
    ) -> Optional[dict]:
        """Returns structured JSON build path adapted to the enemy & ally team comps."""
        enemy_str = ", ".join(enemies) if enemies else "Standard mixed damage enemy comp"
        ally_str = ", ".join(allies) if allies else "Not specified"
        role_str = role or "Standard main role"
        patch_str = patch or "current League of Legends patch"
        extra = f"\nExtra context: {notes}" if notes else ""

        prompt = (
            f"Recommend the optimal League of Legends item build order and runes for {champion} "
            f"({role_str}) on {patch_str}.\n"
            f"Enemy team: {enemy_str}\n"
            f"Ally team: {ally_str}{extra}\n\n"
            "Return ONLY valid JSON with exact official current-season English League item names "
            "(no removed Mythics like Mythic Galeforce/Mythic Duskblade/Everfrost/Crown) in this schema:\n"
            "{\n"
            '  "starter": {"item": "Exact Item Name", "why": "short 6-10 word reason"},\n'
            '  "boots": {"item": "Exact Boots Name", "why": "short 6-10 word reason"},\n'
            '  "core": [\n'
            '    {"item": "1st Item", "why": "short reason tailored to enemy comp"},\n'
            '    {"item": "2nd Item", "why": "short reason tailored to enemy comp"},\n'
            '    {"item": "3rd Item", "why": "short reason tailored to enemy comp"}\n'
            "  ],\n"
            '  "situational": [\n'
            '    {"item": "Situational 1", "why": "short trigger e.g. Anti-heal vs Soraka/Mundo"},\n'
            '    {"item": "Situational 2", "why": "short trigger"},\n'
            '    {"item": "Situational 3", "why": "short trigger"}\n'
            "  ],\n"
            '  "runes": "Keystone + Secondary tree (e.g. Electrocute • Sorcery)",\n'
            '  "gameplan": "1-2 concise sentences on power spikes and how to play into this enemy comp."\n'
            "}"
        )
        raw = await self._chat(
            COACH_SYSTEM_PROMPT,
            prompt,
            max_tokens=480,
            temperature=0.35,
            json_mode=True,
        )
        if not raw:
            return None
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("core"):
                return data
        except json.JSONDecodeError as e:
            logger.warning(f"DeepSeek build JSON parse failed: {e}")
        return None

    # -------------------------------------------------------------------------
    # 7. Patch Notes Summarizer (/patch & Auto Patch Alerts)
    # -------------------------------------------------------------------------

    async def summarize_patch_notes(
        self,
        version: str,
        body_text: str,
        squad_champs: Optional[dict[str, list[str]]] = None,
    ) -> Optional[dict]:
        """Summarizes official Riot patch notes into structured buffs, nerfs, items/systems & squad alerts."""
        squad_section = ""
        if squad_champs:
            lines = [
                f"- {name}: {', '.join(champs)}"
                for name, champs in squad_champs.items()
                if champs
            ]
            if lines:
                squad_section = (
                    "\nOur friend group's recently played champions (flag any that got changed!):\n"
                    + "\n".join(lines)
                    + "\n"
                )

        prompt = (
            f"Summarize official League of Legends Patch {version} notes strictly from the text below.\n"
            f"{squad_section}\n"
            "Return ONLY valid JSON matching this exact schema:\n"
            "{\n"
            f'  "patch": "{version}",\n'
            '  "headline": "Punchy 5-10 word headline of the biggest patch themes",\n'
            '  "buffs": [\n'
            '    {"champion": "ExactChampionName", "summary": "concise 4-8 word buff summary"}\n'
            "  ],\n"
            '  "nerfs": [\n'
            '    {"champion": "ExactChampionName", "summary": "concise 4-8 word nerf summary"}\n'
            "  ],\n"
            '  "adjusted": [\n'
            '    {"champion": "ExactChampionName", "summary": "concise 4-8 word adjustment summary"}\n'
            "  ],\n"
            '  "items_and_systems": [\n'
            '    {"name": "Item or System Name", "tag": "BUFF|NERF|ADJ|NEW", "summary": "concise 5-9 word change"}\n'
            "  ],\n"
            '  "squad_alerts": [\n'
            '    "Short 1-line callout if a friend plays a changed champ, or key meta takeaway"\n'
            "  ]\n"
            "}\n"
            "Limits: up to 6 buffs, up to 6 nerfs, up to 3 adjusted (Summoner's Rift champions first), "
            "up to 4 items_and_systems (include notable items, modes like ARAM: Mayhem, or features), "
            "up to 3 squad_alerts. Use exact official English champion names.\n\n"
            f"OFFICIAL PATCH NOTES:\n{body_text}"
        )
        raw = await self._chat(
            COACH_SYSTEM_PROMPT,
            prompt,
            max_tokens=720,
            temperature=0.25,
            json_mode=True,
        )
        if not raw:
            return None
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and (data.get("buffs") or data.get("nerfs") or data.get("items_and_systems")):
                return data
        except json.JSONDecodeError as e:
            logger.warning(f"DeepSeek patch summary JSON parse failed: {e}")
        return None

    # -------------------------------------------------------------------------
    # Existing Recap / Roast / LastGame / Rank Alert Methods
    # -------------------------------------------------------------------------

    async def summarize_recap(
        self, stats: list, period: str = "daily", synergy: Optional[dict] = None
    ) -> Optional[str]:
        active = [ps for ps in stats if ps.games_played > 0 and not ps.error]
        if not active:
            return None

        lines = [f"Period: {period}"]
        for ps in stats:
            if ps.error:
                continue
            if ps.games_played == 0:
                lines.append(f"- {ps.display_name}: 0 games ({ps.formatted_rank()})")
                continue
            champs = ", ".join(f"{c} x{n}" for c, n in ps.top_champions) or "N/A"
            lp_str = f", LP change: {ps.lp_change:+d}" if ps.lp_change else ""
            streak_str = f", streak: {ps.formatted_streak()}" if abs(ps.win_streak) >= 2 else ""
            lines.append(
                f"- {ps.display_name} ({ps.formatted_rank()}{lp_str}): "
                f"{ps.wins}W-{ps.losses}L ({ps.win_rate:.0f}% WR), "
                f"KDA {ps.formatted_kda()}, {ps.cs_per_min:.1f} CS/m, "
                f"{ps.avg_damage_per_game:,.0f} avg dmg, {ps.hours_played:.1f}h, "
                f"champs: {champs}{streak_str}"
            )

        if synergy and synergy.get("together_games", 0) > 0:
            lines.append(
                f"Squad games together: {synergy['together_wins']}W-"
                f"{synergy['together_losses']}L ({synergy['together_wr']:.0f}% WR)"
            )

        prompt = (
            "Write a short 2-3 sentence Discord recap commentary ('Jeff's Take') for the group "
            "highlighting the biggest winner, funniest struggle, or squad trend. No bullet points, "
            "no markdown headers.\n\n" + "\n".join(lines)
        )
        return await self._chat(JEFF_SYSTEM_PROMPT, prompt, max_tokens=180)

    async def roast_player(self, ps) -> Optional[str]:
        if ps.games_played == 0:
            prompt = (
                f"Write a short, playful 2-sentence roast of {ps.display_name} "
                f"(currently {ps.formatted_rank()}) for dodging League all week (0 games played)."
            )
            return await self._chat(JEFF_SYSTEM_PROMPT, prompt, max_tokens=140)

        worst_game = min(ps.matches, key=lambda m: (m.win, m.kda)) if ps.matches else None
        best_game = max(ps.matches, key=lambda m: (m.win, m.kda)) if ps.matches else None
        champs = ", ".join(f"{c} x{n}" for c, n in ps.top_champions) or "N/A"

        lines = [
            f"Player: {ps.display_name} ({ps.formatted_rank()})",
            f"Last 7 days: {ps.games_played} games ({ps.wins}W-{ps.losses}L, {ps.win_rate:.0f}% WR)",
            f"KDA: {ps.formatted_kda()} | Avg Deaths/game: {ps.total_deaths / ps.games_played:.1f}",
            f"CS/min: {ps.cs_per_min:.1f} | Avg Vision: {ps.avg_vision_score:.1f} | "
            f"Avg Dmg: {ps.avg_damage_per_game:,.0f} ({ps.avg_damage_share:.0f}% team dmg)",
            f"Hours played: {ps.hours_played:.1f}h | Top champs: {champs}",
        ]
        if ps.lp_change:
            lines.append(f"LP delta: {ps.lp_change:+d} LP")
        if worst_game:
            lines.append(
                f"Worst recent game: {worst_game.champion} ({'W' if worst_game.win else 'L'}) "
                f"{worst_game.kills}/{worst_game.deaths}/{worst_game.assists}, "
                f"{worst_game.cs_per_min:.1f} CS/m, {worst_game.damage_to_champions:,} dmg"
            )
        if best_game and best_game is not worst_game:
            lines.append(
                f"Best recent game: {best_game.champion} ({'W' if best_game.win else 'L'}) "
                f"{best_game.kills}/{best_game.deaths}/{best_game.assists}"
            )

        prompt = (
            "Roast this friend in 3-4 punchy sentences using their exact stats above. "
            "Be funny and specific (call out deaths, CS, LP bleed, champ choices, or worst game). "
            "If they actually popped off, give a reluctant backhanded compliment.\n\n"
            + "\n".join(lines)
        )
        return await self._chat(JEFF_SYSTEM_PROMPT, prompt, max_tokens=220, temperature=0.95)

    async def comment_last_game(self, game: dict) -> Optional[str]:
        m = game["match"]
        friends = ", ".join(game.get("friends_in_game", [])) or "solo"
        prompt = (
            f"Give a 1-2 sentence post-game reaction to {game['display_name']}'s latest match:\n"
            f"Result: {'WIN' if m.win else 'LOSS'} ({m.queue_name}, {m.duration_seconds // 60}m)\n"
            f"Champion: {m.champion} | KDA: {m.kills}/{m.deaths}/{m.assists}\n"
            f"CS/min: {m.cs_per_min:.1f} | Damage: {m.damage_to_champions:,} ({m.damage_share:.0f}% team dmg)\n"
            f"KP: {m.kill_participation:.0f}% | Vision: {m.vision_score} | Friends with them: {friends}"
        )
        return await self._chat(JEFF_SYSTEM_PROMPT, prompt, max_tokens=110)

    async def comment_rank_change(self, event: dict) -> Optional[str]:
        direction = event.get("direction", "promotion")
        if direction == "promotion":
            prompt = (
                f"Write a hype 1-sentence Discord celebration for {event['display_name']} "
                f"promoting from {event['old_tier']} to {event['new_tier']}!"
            )
        else:
            prompt = (
                f"Write a playful, lighthearted 1-sentence roast for {event['display_name']} "
                f"demoting from {event['old_tier']} down to {event['new_tier']}."
            )
        return await self._chat(JEFF_SYSTEM_PROMPT, prompt, max_tokens=80)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
