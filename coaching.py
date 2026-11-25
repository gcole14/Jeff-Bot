"""Analytics & coaching helpers: Match-v5 timeline VOD analyzer, win/loss splits,
champion pool auditing, weekly goal generation & evaluation, and squad debrief extraction."""

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional


ROLE_LABELS = {
    "TOP": "Top",
    "JUNGLE": "Jungle",
    "MIDDLE": "Mid",
    "BOTTOM": "ADC",
    "UTILITY": "Support",
}


def clean_role(role: str) -> str:
    return ROLE_LABELS.get((role or "").upper(), role.title() if role else "Flex")


@dataclass
class TimelineReport:
    match_id: str
    display_name: str
    champion: str
    role: str
    queue_name: str
    win: bool
    duration_seconds: int
    kills: int
    deaths: int
    assists: int
    cs: int
    cs_per_min: float
    damage_to_champions: int
    damage_share: float
    kill_participation: float
    vision_score: int
    control_wards: int
    gold_earned: int
    # Lane matchup
    lane_opponent_champ: Optional[str] = None
    gold_diff_10: Optional[int] = None
    cs_diff_10: Optional[int] = None
    xp_diff_10: Optional[int] = None
    gold_diff_15: Optional[int] = None
    cs_diff_15: Optional[int] = None
    xp_diff_15: Optional[int] = None
    # Team gold trajectory
    team_gold_diff_10: Optional[int] = None
    team_gold_diff_15: Optional[int] = None
    max_team_gold_lead: int = 0
    max_team_gold_deficit: int = 0
    # Key events
    pre14_kills: int = 0
    pre14_deaths: int = 0
    solo_kills_pre14: int = 0
    gank_deaths_pre14: int = 0
    objective_throw_deaths: list[str] = field(default_factory=list)
    death_clusters: list[str] = field(default_factory=list)
    item_timings: list[str] = field(default_factory=list)
    final_items: list[str] = field(default_factory=list)
    ally_champions: list[str] = field(default_factory=list)
    enemy_champions: list[str] = field(default_factory=list)
    team_dragons: int = 0
    enemy_dragons: int = 0
    team_barons: int = 0
    enemy_barons: int = 0
    friends_in_game: list[str] = field(default_factory=list)

    @property
    def kda(self) -> float:
        if self.deaths == 0:
            return float(self.kills + self.assists)
        return (self.kills + self.assists) / self.deaths


def _fmt_min(ts_ms: int) -> str:
    total_sec = max(0, ts_ms // 1000)
    return f"{total_sec // 60}:{total_sec % 60:02d}"


def analyze_match_timeline(
    match_payload: dict,
    timeline_payload: Optional[dict],
    puuid: str,
    display_name: str,
    item_map: dict[int, tuple[str, int]],
    queue_label: str,
    puuid_to_name: Optional[dict[str, str]] = None,
) -> Optional[TimelineReport]:
    """Combines Match-v5 summary + Match-v5 timeline into a structured VOD coaching report."""
    metadata = match_payload.get("metadata", {})
    info = match_payload.get("info", {})
    participants = info.get("participants", [])

    player = next((p for p in participants if p.get("puuid") == puuid), None)
    if not player:
        return None

    pid = int(player.get("participantId", 0))
    team_id = int(player.get("teamId", 0))
    role = (player.get("teamPosition") or player.get("individualPosition") or "").upper()
    duration = int(info.get("gameDuration", 0))
    minutes = duration / 60 if duration > 0 else 1.0

    allies = [p for p in participants if int(p.get("teamId", -1)) == team_id]
    enemies = [p for p in participants if int(p.get("teamId", -1)) != team_id]

    team_kills = sum(int(p.get("kills", 0)) for p in allies)
    team_dmg = sum(int(p.get("totalDamageDealtToChampions", 0)) for p in allies)

    kills = int(player.get("kills", 0))
    deaths = int(player.get("deaths", 0))
    assists = int(player.get("assists", 0))
    cs = int(player.get("totalMinionsKilled", 0)) + int(player.get("neutralMinionsKilled", 0))
    dmg = int(player.get("totalDamageDealtToChampions", 0))

    # Find direct lane opponent
    opp = None
    if role in ROLE_LABELS:
        opp = next(
            (e for e in enemies if (e.get("teamPosition") or "").upper() == role),
            None,
        )
    opp_pid = int(opp.get("participantId", 0)) if opp else 0

    final_items = []
    for slot in range(6):
        iid = int(player.get(f"item{slot}", 0) or 0)
        if iid:
            name, _ = item_map.get(iid, (f"Item {iid}", 0))
            final_items.append(name)

    # Team objective totals
    team_dragons = enemy_dragons = team_barons = enemy_barons = 0
    for t in info.get("teams", []):
        obj = t.get("objectives", {})
        d = int(obj.get("dragon", {}).get("kills", 0))
        b = int(obj.get("baron", {}).get("kills", 0))
        if int(t.get("teamId", -1)) == team_id:
            team_dragons, team_barons = d, b
        else:
            enemy_dragons, enemy_barons = d, b

    friends_in_game = []
    if puuid_to_name:
        friends_in_game = [
            puuid_to_name[p["puuid"]]
            for p in allies
            if p.get("puuid") and p["puuid"] != puuid and p["puuid"] in puuid_to_name
        ]

    report = TimelineReport(
        match_id=metadata.get("matchId", ""),
        display_name=display_name,
        champion=player.get("championName", "Unknown"),
        role=clean_role(role),
        queue_name=queue_label,
        win=bool(player.get("win")),
        duration_seconds=duration,
        kills=kills,
        deaths=deaths,
        assists=assists,
        cs=cs,
        cs_per_min=cs / minutes,
        damage_to_champions=dmg,
        damage_share=(dmg / team_dmg * 100) if team_dmg else 0.0,
        kill_participation=((kills + assists) / team_kills * 100) if team_kills else 0.0,
        vision_score=int(player.get("visionScore", 0)),
        control_wards=int(player.get("visionWardsBoughtInGame", 0)),
        gold_earned=int(player.get("goldEarned", 0)),
        lane_opponent_champ=opp.get("championName") if opp else None,
        final_items=final_items,
        ally_champions=[p.get("championName", "?") for p in allies],
        enemy_champions=[p.get("championName", "?") for p in enemies],
        team_dragons=team_dragons,
        enemy_dragons=enemy_dragons,
        team_barons=team_barons,
        enemy_barons=enemy_barons,
        friends_in_game=friends_in_game,
    )

    if not timeline_payload:
        return report

    frames = timeline_payload.get("info", {}).get("frames", [])
    if not frames:
        return report

    ally_pids = {str(int(p.get("participantId", 0))) for p in allies}
    enemy_pids = {str(int(p.get("participantId", 0))) for p in enemies}

    # Gold trajectory & @10/@15 lane diffs
    def _extract_diffs(frame_idx: int) -> tuple[Optional[int], Optional[int], Optional[int], Optional[int]]:
        if frame_idx >= len(frames):
            return None, None, None, None
        pframes = frames[frame_idx].get("participantFrames", {})
        me = pframes.get(str(pid))
        team_g = sum(int(pframes.get(p, {}).get("totalGold", 0)) for p in ally_pids)
        enemy_g = sum(int(pframes.get(p, {}).get("totalGold", 0)) for p in enemy_pids)
        t_diff = team_g - enemy_g
        if not me or not opp_pid:
            return None, None, None, t_diff
        op = pframes.get(str(opp_pid))
        if not op:
            return None, None, None, t_diff
        me_cs = int(me.get("minionsKilled", 0)) + int(me.get("jungleMinionsKilled", 0))
        op_cs = int(op.get("minionsKilled", 0)) + int(op.get("jungleMinionsKilled", 0))
        return (
            int(me.get("totalGold", 0)) - int(op.get("totalGold", 0)),
            me_cs - op_cs,
            int(me.get("xp", 0)) - int(op.get("xp", 0)),
            t_diff,
        )

    (
        report.gold_diff_10,
        report.cs_diff_10,
        report.xp_diff_10,
        report.team_gold_diff_10,
    ) = _extract_diffs(10)
    (
        report.gold_diff_15,
        report.cs_diff_15,
        report.xp_diff_15,
        report.team_gold_diff_15,
    ) = _extract_diffs(15)

    for fr in frames:
        pframes = fr.get("participantFrames", {})
        if not pframes:
            continue
        team_g = sum(int(pframes.get(p, {}).get("totalGold", 0)) for p in ally_pids)
        enemy_g = sum(int(pframes.get(p, {}).get("totalGold", 0)) for p in enemy_pids)
        diff = team_g - enemy_g
        if diff > report.max_team_gold_lead:
            report.max_team_gold_lead = diff
        if diff < report.max_team_gold_deficit:
            report.max_team_gold_deficit = diff

    # Parse timeline events
    death_timestamps: list[int] = []
    monster_events: list[tuple[int, str, bool]] = []  # (ts_ms, label, enemy_took)
    seen_major_items: set[int] = set()

    for fr in frames:
        for ev in fr.get("events", []):
            etype = ev.get("type")
            ts = int(ev.get("timestamp", 0))

            if etype == "CHAMPION_KILL":
                killer = int(ev.get("killerId", 0))
                victim = int(ev.get("victimId", 0))
                assisters = ev.get("assistingParticipantIds") or []

                if killer == pid and ts <= 14 * 60_000:
                    report.pre14_kills += 1
                    if not assisters:
                        report.solo_kills_pre14 += 1
                elif victim == pid:
                    death_timestamps.append(ts)
                    if ts <= 14 * 60_000:
                        report.pre14_deaths += 1
                        if len(assisters) >= 1:
                            report.gank_deaths_pre14 += 1

            elif etype == "ELITE_MONSTER_KILL":
                mtype = ev.get("monsterType", "")
                if mtype in ("DRAGON", "BARON_NASHOR", "RIFTHERALD", "HORDE", "ATAKHAN"):
                    killer_team = int(ev.get("killerTeamId", 0))
                    label = {
                        "DRAGON": "Dragon",
                        "BARON_NASHOR": "Baron",
                        "RIFTHERALD": "Herald",
                        "HORDE": "Voidgrubs",
                        "ATAKHAN": "Atakhan",
                    }.get(mtype, mtype.title())
                    monster_events.append((ts, label, killer_team != team_id))

            elif etype == "ITEM_PURCHASED" and int(ev.get("participantId", 0)) == pid:
                iid = int(ev.get("itemId", 0))
                if iid and iid not in seen_major_items:
                    iname, cost = item_map.get(iid, ("", 0))
                    if cost >= 2200 and iname:
                        seen_major_items.add(iid)
                        report.item_timings.append(f"{ts // 60_000}m {iname}")

    # Detect deaths within 60s before enemy took a major objective
    for d_ts in death_timestamps:
        for m_ts, m_label, enemy_took in monster_events:
            if enemy_took and 0 <= (m_ts - d_ts) <= 65_000:
                report.objective_throw_deaths.append(
                    f"Died at {_fmt_min(d_ts)} → enemy took {m_label} at {_fmt_min(m_ts)}"
                )
                break

    # Detect death clusters (2+ deaths within 3 minutes)
    i = 0
    while i < len(death_timestamps):
        j = i
        while j + 1 < len(death_timestamps) and (death_timestamps[j + 1] - death_timestamps[i]) <= 180_000:
            j += 1
        if j - i >= 1:
            count = j - i + 1
            report.death_clusters.append(
                f"{count} deaths between {_fmt_min(death_timestamps[i])}–{_fmt_min(death_timestamps[j])}"
            )
            i = j + 1
        else:
            i += 1

    return report


# -------------------------------------------------------------------------
# Wins vs. Losses Split & Champion Pool Audit
# -------------------------------------------------------------------------

def compute_win_loss_split(ps) -> dict:
    """Computes average performance in wins vs. losses so coaching pinpoints the actual bottleneck."""
    wins = [m for m in ps.matches if m.win]
    losses = [m for m in ps.matches if not m.win]

    def _agg(bucket: list) -> Optional[dict]:
        if not bucket:
            return None
        n = len(bucket)
        tot_k = sum(m.kills for m in bucket)
        tot_d = sum(m.deaths for m in bucket)
        tot_a = sum(m.assists for m in bucket)
        tot_sec = sum(m.duration_seconds for m in bucket)
        tot_cs = sum(m.cs for m in bucket)
        return {
            "games": n,
            "kda": ((tot_k + tot_a) / tot_d) if tot_d else float(tot_k + tot_a),
            "avg_kills": tot_k / n,
            "avg_deaths": tot_d / n,
            "avg_assists": tot_a / n,
            "cs_per_min": (tot_cs / (tot_sec / 60)) if tot_sec else 0.0,
            "avg_vision": sum(m.vision_score for m in bucket) / n,
            "avg_control_wards": sum(m.control_wards for m in bucket) / n,
            "avg_kp": sum(m.kill_participation for m in bucket) / n,
            "avg_dmg_share": sum(m.damage_share for m in bucket) / n,
            "avg_duration_min": (tot_sec / 60) / n,
        }

    return {
        "wins": _agg(wins),
        "losses": _agg(losses),
    }


def compute_champion_pool_audit(ps) -> dict:
    """Breaks down champion & role performance into Climb / Solid / Trap tiers."""
    champ_buckets: dict[str, list] = defaultdict(list)
    role_buckets: dict[str, list] = defaultdict(list)

    for m in ps.matches:
        champ_buckets[m.champion].append(m)
        if m.role:
            role_buckets[clean_role(m.role)].append(m)

    champs = []
    for cname, games in champ_buckets.items():
        n = len(games)
        w = sum(1 for g in games if g.win)
        l = n - w
        tot_k = sum(g.kills for g in games)
        tot_d = sum(g.deaths for g in games)
        tot_a = sum(g.assists for g in games)
        tot_sec = sum(g.duration_seconds for g in games)
        tot_cs = sum(g.cs for g in games)
        champs.append({
            "champion": cname,
            "games": n,
            "wins": w,
            "losses": l,
            "wr": (w / n * 100) if n else 0.0,
            "kda": ((tot_k + tot_a) / tot_d) if tot_d else float(tot_k + tot_a),
            "avg_deaths": tot_d / n,
            "cs_per_min": (tot_cs / (tot_sec / 60)) if tot_sec else 0.0,
            "avg_damage": sum(g.damage_to_champions for g in games) / n,
        })

    champs.sort(key=lambda c: (c["games"], c["wr"]), reverse=True)

    climb = [c for c in champs if c["games"] >= 2 and c["wr"] >= 55.0]
    solid = [c for c in champs if c["games"] >= 2 and 45.0 <= c["wr"] < 55.0]
    traps = [c for c in champs if c["games"] >= 2 and c["wr"] < 45.0]
    one_offs = [c for c in champs if c["games"] == 1]

    roles = []
    for rname, games in role_buckets.items():
        n = len(games)
        w = sum(1 for g in games if g.win)
        roles.append({
            "role": rname,
            "games": n,
            "wins": w,
            "losses": n - w,
            "wr": (w / n * 100) if n else 0.0,
        })
    roles.sort(key=lambda r: r["games"], reverse=True)

    return {
        "total_games": ps.games_played,
        "unique_champions": len(champs),
        "climb": climb,
        "solid": solid,
        "traps": traps,
        "one_offs": one_offs,
        "all_champs": champs,
        "roles": roles,
    }


# -------------------------------------------------------------------------
# Weekly Coaching Goals
# -------------------------------------------------------------------------

METRIC_LABELS = {
    "avg_deaths": ("Avg Deaths / Game", "deaths"),
    "cs_per_min": ("CS / Min", "CS/m"),
    "avg_vision": ("Vision Score / Game", "vision"),
    "avg_kp": ("Kill Participation", "% KP"),
    "control_wards": ("Control Wards / Game", "pinks"),
}


def extract_goal_metric(ps, metric: str) -> float:
    if ps.games_played == 0:
        return 0.0
    if metric == "avg_deaths":
        return ps.total_deaths / ps.games_played
    if metric == "cs_per_min":
        return ps.cs_per_min
    if metric == "avg_vision":
        return ps.avg_vision_score
    if metric == "avg_kp":
        return ps.avg_kill_participation
    if metric == "control_wards":
        return ps.avg_control_wards
    return 0.0


def pick_next_coaching_goal(ps) -> Optional[dict]:
    """Deterministically selects the highest-leverage measurable weekly goal for a player."""
    if ps.games_played == 0:
        return None

    avg_deaths = ps.total_deaths / ps.games_played
    cs_m = ps.cs_per_min
    vision = ps.avg_vision_score
    kp = ps.avg_kill_participation
    pinks = ps.avg_control_wards

    # Score how far each stat is from a healthy target (higher = bigger opportunity)
    candidates = []
    if avg_deaths >= 5.2:
        target = round(max(3.5, avg_deaths - 1.2), 1)
        candidates.append((avg_deaths - 4.5, "avg_deaths", "<=", target, round(avg_deaths, 1),
                           f"Cut average deaths from {avg_deaths:.1f} down to ≤ {target:.1f} per game"))
    if pinks < 1.8:
        target = round(min(2.5, max(1.0, pinks + 1.0)), 1)
        candidates.append((2.0 - pinks, "control_wards", ">=", target, round(pinks, 1),
                           f"Buy at least {target:.1f} Control Wards per game (currently {pinks:.1f})"))
    if vision < 20.0:
        target = round(vision + 4.5, 1)
        candidates.append(((20.0 - vision) / 6.0, "avg_vision", ">=", target, round(vision, 1),
                           f"Raise Vision Score from {vision:.1f} to ≥ {target:.1f} per game"))
    if 0 < cs_m < 6.2:
        # Don't push CS/m on Support mains
        supp_games = sum(1 for m in ps.matches if (m.role or "").upper() == "UTILITY")
        if supp_games < ps.games_played * 0.5:
            target = round(min(7.5, cs_m + 0.7), 1)
            candidates.append(((6.5 - cs_m) * 1.4, "cs_per_min", ">=", target, round(cs_m, 1),
                               f"Boost CS/min from {cs_m:.1f} to ≥ {target:.1f}"))
    if kp < 52.0:
        target = round(min(62.0, kp + 8.0), 0)
        candidates.append(((55.0 - kp) / 8.0, "avg_kp", ">=", target, round(kp, 1),
                           f"Increase Kill Participation from {kp:.0f}% to ≥ {target:.0f}%"))

    if not candidates:
        target = round(max(3.0, avg_deaths - 0.5), 1)
        candidates.append((1.0, "avg_deaths", "<=", target, round(avg_deaths, 1),
                           f"Keep deaths crisp at ≤ {target:.1f} per game (currently {avg_deaths:.1f})"))

    candidates.sort(key=lambda x: x[0], reverse=True)
    _, metric, op, target, baseline, desc = candidates[0]
    return {
        "metric": metric,
        "target_op": op,
        "target_value": target,
        "baseline_value": baseline,
        "description": desc,
    }


def evaluate_goal_progress(goal: dict, ps) -> dict:
    """Evaluates a player's current stats against an active coaching goal."""
    current = extract_goal_metric(ps, goal["metric"])
    target = float(goal["target_value"])
    op = goal["target_op"]
    met = (current <= target) if op == "<=" else (current >= target)
    label, unit = METRIC_LABELS.get(goal["metric"], (goal["metric"], ""))
    return {
        "goal": goal,
        "label": label,
        "unit": unit,
        "current": current,
        "target": target,
        "baseline": float(goal["baseline_value"]),
        "op": op,
        "games": ps.games_played,
        "met": met and ps.games_played > 0,
    }
