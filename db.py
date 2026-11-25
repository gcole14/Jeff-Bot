import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class Database:
    """SQLite persistence for roster, account cache, match/timeline cache, rank snapshots,
    coaching goals, and announced squad games."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS roster (
                riot_id TEXT PRIMARY KEY,
                added_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS accounts (
                riot_id_lower TEXT PRIMARY KEY,
                riot_id TEXT NOT NULL,
                puuid TEXT NOT NULL,
                summoner_level INTEGER NOT NULL DEFAULT 0,
                profile_icon_id INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS matches (
                match_id TEXT PRIMARY KEY,
                game_creation INTEGER NOT NULL DEFAULT 0,
                game_duration INTEGER NOT NULL DEFAULT 0,
                queue_id INTEGER NOT NULL DEFAULT 0,
                payload TEXT NOT NULL,
                fetched_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS timelines (
                match_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                fetched_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS rank_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                puuid TEXT NOT NULL,
                riot_id TEXT NOT NULL,
                queue_type TEXT NOT NULL DEFAULT 'RANKED_SOLO_5x5',
                tier TEXT NOT NULL,
                rank TEXT NOT NULL,
                lp INTEGER NOT NULL,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                recorded_at INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_rank_snapshots_puuid_time
                ON rank_snapshots (puuid, queue_type, recorded_at DESC);

            CREATE TABLE IF NOT EXISTS coaching_goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                puuid TEXT NOT NULL,
                riot_id TEXT NOT NULL,
                metric TEXT NOT NULL,
                target_op TEXT NOT NULL,
                target_value REAL NOT NULL,
                baseline_value REAL NOT NULL,
                description TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
            );

            CREATE INDEX IF NOT EXISTS idx_coaching_goals_puuid_status
                ON coaching_goals (puuid, status, created_at DESC);

            CREATE TABLE IF NOT EXISTS announced_matches (
                match_id TEXT PRIMARY KEY,
                announced_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS discord_links (
                discord_user_id TEXT PRIMARY KEY,
                riot_id TEXT NOT NULL,
                linked_at INTEGER NOT NULL
            );
            """
        )
        self._conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # -------------------------------------------------------------------------
    # Roster
    # -------------------------------------------------------------------------

    def seed_roster(self, default_summoners: list[str]) -> None:
        cur = self._conn.execute("SELECT COUNT(*) FROM roster")
        if cur.fetchone()[0] > 0:
            return
        now = datetime.now(timezone.utc).isoformat()
        self._conn.executemany(
            "INSERT OR IGNORE INTO roster (riot_id, added_at) VALUES (?, ?)",
            [(rid, now) for rid in default_summoners],
        )
        self._conn.commit()

    def get_roster(self) -> list[str]:
        cur = self._conn.execute("SELECT riot_id FROM roster ORDER BY rowid ASC")
        return [row["riot_id"] for row in cur.fetchall()]

    def add_to_roster(self, riot_id: str) -> bool:
        existing = self.get_roster()
        if any(r.lower() == riot_id.lower() for r in existing):
            return False
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT INTO roster (riot_id, added_at) VALUES (?, ?)",
            (riot_id, now),
        )
        self._conn.commit()
        return True

    def remove_from_roster(self, name_or_id: str) -> Optional[str]:
        target = name_or_id.strip().lower()
        for rid in self.get_roster():
            if rid.lower() == target or rid.split("#", 1)[0].lower() == target:
                self._conn.execute("DELETE FROM roster WHERE riot_id = ?", (rid,))
                self._conn.commit()
                return rid
        return None

    # -------------------------------------------------------------------------
    # Account cache
    # -------------------------------------------------------------------------

    def get_cached_account(self, riot_id: str, max_age_seconds: int = 24 * 3600) -> Optional[dict]:
        cur = self._conn.execute(
            "SELECT riot_id, puuid, summoner_level, profile_icon_id, updated_at "
            "FROM accounts WHERE riot_id_lower = ?",
            (riot_id.lower(),),
        )
        row = cur.fetchone()
        if not row:
            return None
        if int(time.time()) - row["updated_at"] > max_age_seconds:
            return None
        return dict(row)

    def upsert_account(
        self, riot_id: str, puuid: str, summoner_level: int = 0, profile_icon_id: int = 0
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO accounts (riot_id_lower, riot_id, puuid, summoner_level, profile_icon_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(riot_id_lower) DO UPDATE SET
                riot_id = excluded.riot_id,
                puuid = excluded.puuid,
                summoner_level = excluded.summoner_level,
                profile_icon_id = excluded.profile_icon_id,
                updated_at = excluded.updated_at
            """,
            (riot_id.lower(), riot_id, puuid, summoner_level, profile_icon_id, int(time.time())),
        )
        self._conn.commit()

    def get_puuid_to_name_map(self) -> dict[str, str]:
        cur = self._conn.execute("SELECT puuid, riot_id FROM accounts")
        return {row["puuid"]: row["riot_id"].split("#", 1)[0] for row in cur.fetchall()}

    # -------------------------------------------------------------------------
    # Match & timeline cache
    # -------------------------------------------------------------------------

    def get_cached_match(self, match_id: str) -> Optional[dict]:
        cur = self._conn.execute("SELECT payload FROM matches WHERE match_id = ?", (match_id,))
        row = cur.fetchone()
        if not row:
            return None
        try:
            return json.loads(row["payload"])
        except json.JSONDecodeError:
            return None

    def save_match(self, match_id: str, payload: dict) -> None:
        info = payload.get("info", {})
        self._conn.execute(
            """
            INSERT OR REPLACE INTO matches
                (match_id, game_creation, game_duration, queue_id, payload, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                match_id,
                int(info.get("gameCreation", 0)),
                int(info.get("gameDuration", 0)),
                int(info.get("queueId", 0)),
                json.dumps(payload),
                int(time.time()),
            ),
        )
        self._conn.commit()

    def count_cached_matches(self) -> int:
        cur = self._conn.execute("SELECT COUNT(*) FROM matches")
        return int(cur.fetchone()[0])

    def get_cached_timeline(self, match_id: str) -> Optional[dict]:
        cur = self._conn.execute("SELECT payload FROM timelines WHERE match_id = ?", (match_id,))
        row = cur.fetchone()
        if not row:
            return None
        try:
            return json.loads(row["payload"])
        except json.JSONDecodeError:
            return None

    def save_timeline(self, match_id: str, payload: dict) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO timelines (match_id, payload, fetched_at) VALUES (?, ?, ?)",
            (match_id, json.dumps(payload), int(time.time())),
        )
        self._conn.commit()

    # -------------------------------------------------------------------------
    # Rank snapshots
    # -------------------------------------------------------------------------

    def record_rank_snapshot(
        self,
        puuid: str,
        riot_id: str,
        tier: str,
        rank: str,
        lp: int,
        wins: int,
        losses: int,
        queue_type: str = "RANKED_SOLO_5x5",
        recorded_at: Optional[int] = None,
    ) -> None:
        ts = recorded_at if recorded_at is not None else int(time.time())
        latest = self.get_latest_rank(puuid, queue_type=queue_type)
        if latest and (
            latest["tier"] == tier
            and latest["rank"] == rank
            and latest["lp"] == lp
            and latest["wins"] == wins
            and latest["losses"] == losses
            and (ts - latest["recorded_at"]) < 6 * 3600
        ):
            return
        self._conn.execute(
            """
            INSERT INTO rank_snapshots
                (puuid, riot_id, queue_type, tier, rank, lp, wins, losses, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (puuid, riot_id, queue_type, tier, rank, lp, wins, losses, ts),
        )
        self._conn.commit()

    def get_latest_rank(self, puuid: str, queue_type: str = "RANKED_SOLO_5x5") -> Optional[dict]:
        cur = self._conn.execute(
            """
            SELECT puuid, riot_id, tier, rank, lp, wins, losses, recorded_at
            FROM rank_snapshots
            WHERE puuid = ? AND queue_type = ?
            ORDER BY recorded_at DESC, id DESC
            LIMIT 1
            """,
            (puuid, queue_type),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def get_rank_at_or_after(
        self, puuid: str, since_epoch: int, queue_type: str = "RANKED_SOLO_5x5"
    ) -> Optional[dict]:
        cur = self._conn.execute(
            """
            SELECT puuid, riot_id, tier, rank, lp, wins, losses, recorded_at
            FROM rank_snapshots
            WHERE puuid = ? AND queue_type = ? AND recorded_at <= ?
            ORDER BY recorded_at DESC, id DESC
            LIMIT 1
            """,
            (puuid, queue_type, since_epoch),
        )
        row = cur.fetchone()
        if row:
            return dict(row)

        cur = self._conn.execute(
            """
            SELECT puuid, riot_id, tier, rank, lp, wins, losses, recorded_at
            FROM rank_snapshots
            WHERE puuid = ? AND queue_type = ? AND recorded_at >= ?
            ORDER BY recorded_at ASC, id ASC
            LIMIT 1
            """,
            (puuid, queue_type, since_epoch),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    # -------------------------------------------------------------------------
    # Coaching goals
    # -------------------------------------------------------------------------

    def get_active_goal(self, puuid: str) -> Optional[dict]:
        cur = self._conn.execute(
            """
            SELECT * FROM coaching_goals
            WHERE puuid = ? AND status = 'active'
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (puuid,),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def set_coaching_goal(
        self,
        puuid: str,
        riot_id: str,
        metric: str,
        target_op: str,
        target_value: float,
        baseline_value: float,
        description: str,
        duration_days: int = 7,
    ) -> dict:
        now = int(time.time())
        expires = now + duration_days * 86400
        self._conn.execute(
            "UPDATE coaching_goals SET status = 'replaced' WHERE puuid = ? AND status = 'active'",
            (puuid,),
        )
        cur = self._conn.execute(
            """
            INSERT INTO coaching_goals
                (puuid, riot_id, metric, target_op, target_value, baseline_value,
                 description, created_at, expires_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
            """,
            (puuid, riot_id, metric, target_op, target_value, baseline_value, description, now, expires),
        )
        self._conn.commit()
        return {
            "id": cur.lastrowid,
            "puuid": puuid,
            "riot_id": riot_id,
            "metric": metric,
            "target_op": target_op,
            "target_value": target_value,
            "baseline_value": baseline_value,
            "description": description,
            "created_at": now,
            "expires_at": expires,
            "status": "active",
        }

    def close_coaching_goal(self, goal_id: int, status: str) -> None:
        self._conn.execute("UPDATE coaching_goals SET status = ? WHERE id = ?", (status, goal_id))
        self._conn.commit()

    # -------------------------------------------------------------------------
    # Announced squad matches
    # -------------------------------------------------------------------------

    def is_match_announced(self, match_id: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM announced_matches WHERE match_id = ?", (match_id,)
        )
        return cur.fetchone() is not None

    def mark_match_announced(self, match_id: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO announced_matches (match_id, announced_at) VALUES (?, ?)",
            (match_id, int(time.time())),
        )
        self._conn.commit()

    def count_announced_matches(self) -> int:
        cur = self._conn.execute("SELECT COUNT(*) FROM announced_matches")
        return int(cur.fetchone()[0])

    # -------------------------------------------------------------------------
    # Discord user -> Riot ID links (for zero-arg /build, etc.)
    # -------------------------------------------------------------------------

    def link_discord_user(self, discord_user_id: int, riot_id: str) -> None:
        self._conn.execute(
            """
            INSERT INTO discord_links (discord_user_id, riot_id, linked_at)
            VALUES (?, ?, ?)
            ON CONFLICT(discord_user_id) DO UPDATE SET
                riot_id = excluded.riot_id,
                linked_at = excluded.linked_at
            """,
            (str(discord_user_id), riot_id, int(time.time())),
        )
        self._conn.commit()

    def get_linked_riot_id(self, discord_user_id: int) -> Optional[str]:
        cur = self._conn.execute(
            "SELECT riot_id FROM discord_links WHERE discord_user_id = ?",
            (str(discord_user_id),),
        )
        row = cur.fetchone()
        return row["riot_id"] if row else None

    # -------------------------------------------------------------------------
    # Legacy migration
    # -------------------------------------------------------------------------

    def migrate_legacy_rank_history(self, json_path: str) -> int:
        if not json_path or not os.path.exists(json_path):
            return 0
        try:
            with open(json_path, "r") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"Skipping legacy rank history migration ({e})")
            return 0

        migrated = 0
        for puuid, entry in data.items():
            if self.get_latest_rank(puuid):
                continue
            ts = int(time.time())
            if entry.get("updated_at"):
                try:
                    ts = int(datetime.fromisoformat(entry["updated_at"]).timestamp())
                except ValueError:
                    pass
            self.record_rank_snapshot(
                puuid=puuid,
                riot_id=entry.get("riot_id", "Unknown#NA1"),
                tier=entry.get("tier", "UNRANKED"),
                rank=entry.get("rank", ""),
                lp=int(entry.get("lp", 0)),
                wins=int(entry.get("wins", 0)),
                losses=int(entry.get("losses", 0)),
                recorded_at=ts,
            )
            migrated += 1
        if migrated:
            logger.info(f"Migrated {migrated} legacy rank record(s) from {json_path}")
        return migrated
