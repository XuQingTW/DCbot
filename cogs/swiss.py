# -*- coding: utf-8 -*-
"""
Swiss Tournament (Button-Only, No Slash, No Subcommands)
- Registration / Swiss rounds / reporting / queries / auto Top4
- Buttons: join/leave/drop, winner, class picks, admin panel
- Round-complete workflow: render standings image and prompt organizer
- Atomic result write to avoid double-win
- Organizer tools (all via buttons & modals):
    * View roster
    * Manual add / drop / restore
    * Set table winner (with override guard)
    * Swap tables (一鍵改桌)
    * Swap opponents (黑箱換對手)
    * Ban / Unban / Batch Ban
    * Test: seed fake players / fill metas / simulate round
- Persist deck picks per match and to players (deck1, deck2, actual_class)
- Ban list enforcement on join
- Audit log for admin actions
- Only text command: !swiss (opens panel if organizer, else hint)
Requirements:
- discord.py v2.x
- aiosqlite
- matplotlib (optional; used to render standings image; falls back to text)
"""

from __future__ import annotations
import asyncio
import random
import time
import io
import re
import datetime as dt
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import aiosqlite
import discord
from discord.ext import commands

DB_PATH = "swiss.db"

# ---------- Constants ----------
CLASS_LABELS = ["精靈", "皇家", "巫師", "龍族", "夜魔", "主教", "復仇者"]

# ---------- Data rows ----------
@dataclass
class PlayerRow:
    id: int
    tournament_id: int
    user_id: int
    display_name: str
    active: int
    score: float

# ---------- Helpers ----------
def chunk_text(s: str, limit: int = 1800) -> List[str]:
    return [s[i:i + limit] for i in range(0, len(s), limit)]

# ---------- Cog ----------
class SwissAll(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._ready = False
        self._lock = asyncio.Lock()

    # -------------- DB --------------
    def db(self):
        # return async context manager (aiosqlite.connect)
        return aiosqlite.connect(DB_PATH)

    async def setup_db(self):
        if self._ready:
            return
        async with self.db() as conn:
            await conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS tournaments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'init',
                    reg_message_id INTEGER,
                    organizer_id INTEGER,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS players (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tournament_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    display_name TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    score REAL NOT NULL DEFAULT 0,
                    deck1 TEXT,
                    deck2 TEXT,
                    actual_class TEXT
                );
                CREATE TABLE IF NOT EXISTS rounds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tournament_id INTEGER NOT NULL,
                    round_no INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ongoing',
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS matches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tournament_id INTEGER NOT NULL,
                    round_id INTEGER NOT NULL,
                    table_no INTEGER NOT NULL,
                    p1_id INTEGER,
                    p2_id INTEGER,
                    result TEXT,                 -- 'p1','p2','bye'
                    winner_player_id INTEGER,
                    notes TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_matches_round ON matches(round_id);
                CREATE INDEX IF NOT EXISTS idx_players_tid ON players(tournament_id);

                -- Per-match per-player class picks
                CREATE TABLE IF NOT EXISTS match_player_meta (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    match_id INTEGER NOT NULL,
                    player_id INTEGER NOT NULL,      -- players.id
                    pick1 TEXT,
                    pick2 TEXT,
                    actual TEXT,
                    UNIQUE(match_id, player_id)
                );

                -- Ban list
                CREATE TABLE IF NOT EXISTS tournament_bans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tournament_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    reason TEXT,
                    by_user_id INTEGER,
                    created_at INTEGER NOT NULL,
                    UNIQUE(tournament_id, user_id)
                );

                -- Audit log
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tournament_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor_user_id INTEGER NOT NULL,
                    payload TEXT,
                    created_at INTEGER NOT NULL
                );
                """
            )

            # 清掉同賽事同 user_id 的重複報名，只保留最早的一筆
            try:
                await conn.executescript("""
                DELETE FROM players
                WHERE rowid NOT IN (
                    SELECT MIN(rowid) FROM players GROUP BY tournament_id, user_id
                );
                """)
            except Exception:
                pass
            # 建立唯一索引避免重複報名
            try:
                await conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uniq_players_tid_uid ON players(tournament_id, user_id)"
                )
            except Exception:
                pass

            await conn.commit()
        self._ready = True

    # -------------- Small utils --------------
    async def _audit(self, tid: int, actor_uid: int, action: str, payload: str = ""):
        async with self.db() as conn:
            await conn.execute(
                "INSERT INTO audit_logs(tournament_id,action,actor_user_id,payload,created_at) VALUES(?,?,?,?,?)",
                (tid, action, actor_uid, payload, int(time.time()))
            )
            await conn.commit()

    async def _is_organizer_user(self, tid: int, user: discord.abc.User) -> bool:
        org = await self.get_organizer(tid)
        try:
            # user 若是 Member 就有 guild 與 guild_permissions
            guild = getattr(user, "guild", None)
            is_owner = bool(guild and guild.owner_id == user.id)
            return (user.id == org) or is_owner or user.guild_permissions.manage_guild
        except AttributeError:
            # DM 或非 Member 的情況；只允許 organizer
            return (user.id == org)


    async def _resolve_member(self, guild: discord.Guild, token: str) -> Optional[discord.Member]:
        """Accepts @mention, <@!id>, id, or name#discrim/name."""
        token = token.strip()
        m = re.search(r"\d{15,20}", token)
        if m:
            uid = int(m.group(0))
            member = guild.get_member(uid) or (await guild.fetch_member(uid) if guild.chunked or guild.me else None)
            return member
        cand = guild.get_member_named(token)
        if cand: return cand
        # fallback: case-insensitive display_name match (first)
        token_lower = token.lower()
        for mm in guild.members:
            if mm.display_name.lower() == token_lower or mm.name.lower() == token_lower:
                return mm
        return None

    async def _player_pid_by_user(self, tid: int, user_id: int) -> Optional[int]:
        async with self.db() as conn:
            async with conn.execute("SELECT id FROM players WHERE tournament_id=? AND user_id=?", (tid, user_id)) as cur:
                r = await cur.fetchone()
                return int(r[0]) if r else None

    async def _find_match_by_pid(self, rid: int, pid: int) -> Optional[Tuple[int,int,Optional[int],Optional[int],Optional[str]]]:
        """
        依 players.id（pid）取得本輪該玩家的對局：
        回傳 (match_id, table_no, p1_id, p2_id, result)；找不到回傳 None
        """
        async with self.db() as conn:
            async with conn.execute(
                "SELECT id, table_no, p1_id, p2_id, result "
                "FROM matches WHERE round_id=? AND (p1_id=? OR p2_id=?) "
                "ORDER BY table_no LIMIT 1",
                (rid, pid, pid)
            ) as cur:
                r = await cur.fetchone()
        if not r:
            return None
        return (r[0], r[1], r[2], r[3], r[4])

    async def _find_user_round_match(self, tid: int, rid: int, user_id: int):
        """
        回傳 (player_pid, (mid, table_no, p1_id, p2_id, result, winner_player_id))；找不到則回傳 None
        """
        async with self.db() as conn:
            async with conn.execute(
                "SELECT id FROM players WHERE tournament_id=? AND user_id=?",
                (tid, user_id)
            ) as cur:
                r = await cur.fetchone()
            if not r:
                return None
            pid = r[0]
            async with conn.execute(
                "SELECT id, table_no, p1_id, p2_id, result, winner_player_id "
                "FROM matches WHERE round_id=? AND (p1_id=? OR p2_id=?) "
                "ORDER BY table_no LIMIT 1",
                (rid, pid, pid)
            ) as cur2:
                mrow = await cur2.fetchone()
        return (pid, mrow) if mrow else None

    # -------------- Tournament utils --------------
    async def create_tournament(self, guild_id: int, organizer_id: int, name: Optional[str]) -> int:
        name = name or dt.date.today().isoformat()
        async with self.db() as conn:
            await conn.execute(
                "INSERT INTO tournaments(guild_id,name,status,organizer_id,created_at) VALUES(?,?,?,?,?)",
                (guild_id, name, "register", organizer_id, int(time.time())),
            )
            async with conn.execute("SELECT last_insert_rowid()") as cur:
                (tid,) = await cur.fetchone()
            await conn.commit()
            return int(tid)

    async def guild_latest_tid(self, guild_id: int) -> Optional[int]:
        async with self.db() as conn:
            async with conn.execute(
                "SELECT id FROM tournaments WHERE guild_id=? ORDER BY id DESC LIMIT 1",
                (guild_id,),
            ) as cur:
                r = await cur.fetchone()
                return r[0] if r else None

    async def tour_status(self, tid: int) -> str:
        async with self.db() as conn:
            async with conn.execute("SELECT status FROM tournaments WHERE id=?", (tid,)) as cur:
                r = await cur.fetchone()
                return r[0] if r else "init"

    async def set_status(self, tid: int, status: str):
        async with self.db() as conn:
            await conn.execute("UPDATE tournaments SET status=? WHERE id=?", (status, tid))
            await conn.commit()

    async def get_organizer(self, tid: int) -> Optional[int]:
        async with self.db() as conn:
            async with conn.execute("SELECT organizer_id FROM tournaments WHERE id=?", (tid,)) as cur:
                r = await cur.fetchone()
                return r[0] if r else None

    async def add_player(self, tid: int, member: discord.abc.User, active: int = 1) -> str:
        """return 'banned' | 'new' | 'reactivated' | 'already'"""
        async with self.db() as conn:
            # ban check
            async with conn.execute(
                "SELECT 1 FROM tournament_bans WHERE tournament_id=? AND user_id=?",
                (tid, member.id)
            ) as cur:
                if await cur.fetchone():
                    return "banned"
            # try existing
            async with conn.execute(
                "SELECT id,active FROM players WHERE tournament_id=? AND user_id=?",
                (tid, member.id)
            ) as cur:
                row = await cur.fetchone()
            if row:
                pid, act = row
                if act == 1:
                    return "already"
                await conn.execute("UPDATE players SET active=1 WHERE id=?", (pid,))
                await conn.commit()
                return "reactivated"
            # new
            await conn.execute(
                "INSERT OR IGNORE INTO players(tournament_id,user_id,display_name,active) VALUES(?,?,?,?)",
                (tid, member.id, getattr(member, "display_name", getattr(member, "name", str(member.id))), active),
            )
            await conn.commit()
            return "new"

    async def mark_drop(self, tid: int, user_id: int):
        async with self.db() as conn:
            await conn.execute(
                "UPDATE players SET active=0 WHERE tournament_id=? AND user_id=?",
                (tid, user_id),
            )
            await conn.commit()

    async def fetch_players(self, tid: int, active_only=True) -> List[PlayerRow]:
        q = "SELECT id,tournament_id,user_id,display_name,active,score FROM players WHERE tournament_id=?"
        if active_only:
            q += " AND active=1"
        async with self.db() as conn:
            async with conn.execute(q, (tid,)) as cur:
                rows = await cur.fetchall()
                return [PlayerRow(*r) for r in rows]

    async def create_round(self, tid: int) -> int:
        async with self.db() as conn:
            async with conn.execute(
                "SELECT COALESCE(MAX(round_no),0)+1 FROM rounds WHERE tournament_id=?",
                (tid,),
            ) as cur:
                (rno,) = await cur.fetchone()
            await conn.execute(
                "INSERT INTO rounds(tournament_id,round_no,status,created_at) VALUES(?,?,?,?)",
                (tid, rno, "ongoing", int(time.time()))
            )
            await conn.commit()
            async with conn.execute(
                "SELECT id FROM rounds WHERE tournament_id=? AND round_no=?",
                (tid, rno),
            ) as cur:
                (rid,) = await cur.fetchone()
                return rid

    async def current_round(self, tid: int) -> Optional[Tuple[int, int, str]]:
        async with self.db() as conn:
            async with conn.execute(
                "SELECT id,round_no,status FROM rounds WHERE tournament_id=? ORDER BY round_no DESC LIMIT 1",
                (tid,),
            ) as cur:
                r = await cur.fetchone()
                return (r[0], r[1], r[2]) if r else None

    async def close_round(self, rid: int):
        async with self.db() as conn:
            await conn.execute("UPDATE rounds SET status='finished' WHERE id=?", (rid,))
            await conn.commit()

    async def add_match(
        self, tid: int, rid: int, table_no: int,
        p1_id: Optional[int], p2_id: Optional[int],
        result: Optional[str] = None, winner_player_id: Optional[int] = None, notes: Optional[str] = None
    ) -> int:
        async with self.db() as conn:
            await conn.execute(
                "INSERT INTO matches(tournament_id,round_id,table_no,p1_id,p2_id,result,winner_player_id,notes) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (tid, rid, table_no, p1_id, p2_id, result, winner_player_id, notes),
            )
            await conn.commit()
            async with conn.execute("SELECT last_insert_rowid()") as cur:
                (mid,) = await cur.fetchone()
                return int(mid)

    async def list_matches_round(self, rid: int):
        async with self.db() as conn:
            async with conn.execute(
                "SELECT id,table_no,p1_id,p2_id,result,winner_player_id FROM matches WHERE round_id=? ORDER BY table_no",
                (rid,),
            ) as cur:
                return await cur.fetchall()

    async def set_match_result(self, match_id: int, result: str, winner_pid: Optional[int], notes: Optional[str]):
        async with self.db() as conn:
            await conn.execute(
                "UPDATE matches SET result=?, winner_player_id=?, notes=? WHERE id=?",
                (result, winner_pid, notes, match_id),
            )
            await conn.commit()

    async def set_match_result_atomic(self, match_id: int, result: str, winner_pid: Optional[int]) -> tuple[bool, Optional[tuple[str, Optional[int]]]]:
        """
        Atomic write:
        - succeed only if current result IS NULL
        - return (ok, current) where:
            ok=True  => your write was applied
            ok=False => someone already set it; current=(existing_result, existing_winner_pid)
        """
        async with self.db() as conn:
            async with conn.execute("SELECT result, winner_player_id FROM matches WHERE id=?", (match_id,)) as cur:
                row = await cur.fetchone()
            if row and row[0] is not None:
                return (False, (row[0], row[1]))

            cur2 = await conn.execute(
                "UPDATE matches SET result=?, winner_player_id=? WHERE id=? AND result IS NULL",
                (result, winner_pid, match_id)
            )
            await conn.commit()
            ok = (cur2.rowcount or 0) > 0

            if ok:
                return (True, (result, winner_pid))
            else:
                async with conn.execute("SELECT result, winner_player_id FROM matches WHERE id=?", (match_id,)) as cur3:
                    row2 = await cur3.fetchone()
                return (False, (row2[0] if row2 else None, row2[1] if row2 else None))

    async def update_score_for_match(self, tid: int, p1_id: Optional[int], p2_id: Optional[int], result: str, winner_pid: Optional[int]):
        """每勝一場 +3 分；BYE 也視為 +3。"""
        delta = 3
        async with self.db() as conn:
            if result == "p1" and p1_id:
                await conn.execute("UPDATE players SET score=score+? WHERE id=?", (delta, p1_id))
            elif result == "p2" and p2_id:
                await conn.execute("UPDATE players SET score=score+? WHERE id=?", (delta, p2_id))
            elif result == "bye":
                if p1_id and not p2_id:
                    await conn.execute("UPDATE players SET score=score+? WHERE id=?", (delta, p1_id))
                if p2_id and not p1_id:
                    await conn.execute("UPDATE players SET score=score+? WHERE id=?", (delta, p2_id))
            await conn.commit()

    async def recompute_scores(self, tid: int):
        async with self.db() as conn:
            await conn.execute("UPDATE players SET score=0 WHERE tournament_id=?", (tid,))
            async with conn.execute(
                "SELECT p1_id,p2_id,result,winner_player_id FROM matches WHERE tournament_id=?",
                (tid,),
            ) as cur:
                rows = await cur.fetchall()
            await conn.commit()
        for p1, p2, res, wpid in rows:
            if not res:
                continue
            await self.update_score_for_match(tid, p1, p2, res, wpid)

    # ---------- Match meta helpers ----------
    async def _mpm_get(self, match_id: int, player_pid: int) -> Dict[str, Optional[str]]:
        async with self.db() as conn:
            async with conn.execute(
                "SELECT pick1,pick2,actual FROM match_player_meta WHERE match_id=? AND player_id=?",
                (match_id, player_pid)
            ) as cur:
                r = await cur.fetchone()
        if not r:
            return {"pick1": None, "pick2": None, "actual": None}
        return {"pick1": r[0], "pick2": r[1], "actual": r[2]}

    async def _mpm_upsert(self, match_id: int, player_pid: int, **fields):
        cols_allowed = ("pick1", "pick2", "actual")
        ins_pick1 = fields.get("pick1")
        ins_pick2 = fields.get("pick2")
        ins_actual = fields.get("actual")
        set_cols = [k for k in cols_allowed if k in fields]
        if not set_cols:
            async with self.db() as conn:
                await conn.execute(
                    "INSERT OR IGNORE INTO match_player_meta(match_id, player_id, pick1, pick2, actual) VALUES(?,?,?,?,?)",
                    (match_id, player_pid, ins_pick1, ins_pick2, ins_actual)
                )
                await conn.commit()
            return
        set_clause = ", ".join([f"{c}=?" for c in set_cols])
        set_vals = [fields[c] for c in set_cols]
        async with self.db() as conn:
            await conn.execute(
                """
                INSERT INTO match_player_meta(match_id, player_id, pick1, pick2, actual)
                VALUES(?,?,?,?,?)
                ON CONFLICT(match_id, player_id) DO UPDATE SET """ + set_clause,
                (match_id, player_pid, ins_pick1, ins_pick2, ins_actual, *set_vals)
            )
            await conn.commit()

    async def _player_set_decks_if_ready(self, player_pid: int, pick1: Optional[str], pick2: Optional[str], actual: Optional[str]):
        async with self.db() as conn:
            await conn.execute(
                "UPDATE players SET deck1=?, deck2=?, actual_class=? WHERE id=?",
                (pick1, pick2, actual, player_pid)
            )
            await conn.commit()

    # -------------- Views --------------
    class RegView(discord.ui.View):
        def __init__(self, cog: 'SwissAll', tid: int):
            super().__init__(timeout=None)
            self.cog = cog
            self.tid = tid

        @discord.ui.button(label="報名 Join", style=discord.ButtonStyle.success, custom_id="swiss:join")
        async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
            await self.cog.setup_db()
            status = await self.cog.add_player(self.tid, interaction.user, active=1)
            if status == "banned":
                return await interaction.response.send_message("你已被本賽事封禁，無法報名。", ephemeral=True)
            msg = {
                "new": "已加入報名。",
                "reactivated": "你已在名單中，已恢復參賽狀態。",
                "already": "你已在名單中（目前是參賽狀態）。"
            }.get(status, "OK")
            await interaction.response.send_message(msg, ephemeral=True)

        @discord.ui.button(label="退出 Leave", style=discord.ButtonStyle.secondary, custom_id="swiss:leave")
        async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
            await self.cog.setup_db()
            await self.cog.mark_drop(self.tid, interaction.user.id)
            await interaction.response.send_message("已退出／退賽。", ephemeral=True)

        @discord.ui.button(label="我要退賽 Drop", style=discord.ButtonStyle.danger, custom_id="swiss:drop")
        async def drop(self, interaction: discord.Interaction, button: discord.ui.Button):
            await self.cog.mark_drop(self.tid, interaction.user.id)
            await interaction.response.send_message("已標記退賽(下一輪不再配對)。", ephemeral=True)

    # -------- Round-level views: 三則面板(每輪各一) --------
    class RoundDeckView(discord.ui.View):
        """面板 1：使用牌組(每位玩家要選兩個不可重複；提供重置)"""
        def __init__(self, cog: 'SwissAll', tid: int, rid: int):
            super().__init__(timeout=None)
            self.cog = cog
            self.tid = tid
            self.rid = rid

        async def _pick(self, itx: discord.Interaction, label: str):
            r = await self.cog._find_user_round_match(self.tid, self.rid, itx.user.id)
            if not r:
                return await itx.response.send_message("找不到你在本輪的對局。", ephemeral=True)
            pid, (mid, *_rest) = r
            meta = await self.cog._mpm_get(mid, pid)
            p1, p2 = meta["pick1"], meta["pick2"]
            if label in (p1, p2):
                return await itx.response.send_message("不可重複選相同職業。", ephemeral=True)
            if p1 is None:
                await self.cog._mpm_upsert(mid, pid, pick1=label)
                await self.cog._player_set_decks_if_ready(pid, label, p2, meta["actual"])
                return await itx.response.send_message(f"已選擇第一職業：{label}", ephemeral=True)
            if p2 is None:
                await self.cog._mpm_upsert(mid, pid, pick2=label)
                await self.cog._player_set_decks_if_ready(pid, p1, label, meta["actual"])
                return await itx.response.send_message(f"已選擇第二職業：{label}", ephemeral=True)
            return await itx.response.send_message("你已選滿兩個職業，若要重選請按「按錯點我重製」。", ephemeral=True)

        async def _reset(self, itx: discord.Interaction):
            r = await self.cog._find_user_round_match(self.tid, self.rid, itx.user.id)
            if not r:
                return await itx.response.send_message("找不到你在本輪的對局。", ephemeral=True)
            pid, (mid, *_rest) = r
            await self.cog._mpm_upsert(mid, pid, pick1=None, pick2=None)
            await self.cog._player_set_decks_if_ready(pid, None, None, None)
            await itx.response.send_message("已重置你的兩個職業選擇。", ephemeral=True)

        # 七職業＋重置
        @discord.ui.button(label="精靈", style=discord.ButtonStyle.secondary, custom_id="swiss:rdeck:elf")
        async def d_elf(self, itx, _):   await self._pick(itx, "精靈")
        @discord.ui.button(label="皇家", style=discord.ButtonStyle.secondary, custom_id="swiss:rdeck:royal")
        async def d_royal(self, itx, _): await self._pick(itx, "皇家")
        @discord.ui.button(label="巫師", style=discord.ButtonStyle.secondary, custom_id="swiss:rdeck:witch")
        async def d_witch(self, itx, _): await self._pick(itx, "巫師")
        @discord.ui.button(label="龍族", style=discord.ButtonStyle.secondary, custom_id="swiss:rdeck:dragon")
        async def d_dragon(self, itx, _): await self._pick(itx, "龍族")
        @discord.ui.button(label="夜魔", style=discord.ButtonStyle.secondary, custom_id="swiss:rdeck:night")
        async def d_night(self, itx, _):  await self._pick(itx, "夜魔")
        @discord.ui.button(label="主教", style=discord.ButtonStyle.secondary, custom_id="swiss:rdeck:bishop")
        async def d_bishop(self, itx, _): await self._pick(itx, "主教")
        @discord.ui.button(label="復仇者", style=discord.ButtonStyle.secondary, custom_id="swiss:rdeck:avenger")
        async def d_avenger(self, itx, _):await self._pick(itx, "復仇者")

        @discord.ui.button(label="按錯點我重製", style=discord.ButtonStyle.danger, custom_id="swiss:rdeck:reset")
        async def d_reset(self, itx, _):  await self._reset(itx)

    class RoundWinnerView(discord.ui.View):
        """面板 2：贏家按此(公開公告『桌X：A 勝 B』；非臨時訊息) + 原子防重覆"""
        def __init__(self, cog: 'SwissAll', tid: int, rid: int):
            super().__init__(timeout=None)
            self.cog = cog
            self.tid = tid
            self.rid = rid

        @discord.ui.button(label="贏家", style=discord.ButtonStyle.success, custom_id="swiss:rwinner")
        async def b_winner(self, itx: discord.Interaction, _):
            r = await self.cog._find_user_round_match(self.tid, self.rid, itx.user.id)
            if not r:
                return await itx.response.send_message("找不到你在本輪的對局。", ephemeral=True)
            pid, (mid, table_no, p1_id, p2_id, result, _) = r
            if result is not None:
                return await itx.response.send_message("本桌已回報完成。", ephemeral=True)
            if pid not in (p1_id, p2_id):
                return await itx.response.send_message("這不是你的對局。", ephemeral=True)

            res = "p1" if pid == p1_id else "p2"

            ok, current = await self.cog.set_match_result_atomic(mid, res, pid)
            if not ok:
                # 已被他人搶先
                exist_res, exist_wpid = current or (None, None)
                # 查名字
                async with self.cog.db() as conn:
                    async with conn.execute("SELECT display_name FROM players WHERE id=?", (exist_wpid,)) as c:
                        rr = await c.fetchone()
                name = rr[0] if rr else "?"
                return await itx.response.send_message(f"本桌已由 {name} 回報完成。", ephemeral=True)

            # 計分（只有第一個成功的人會計分）
            await self.cog.update_score_for_match(self.tid, p1_id, p2_id, res, pid)

            # 公開公告
            async with self.cog.db() as conn:
                async with conn.execute("SELECT display_name FROM players WHERE id=?", (p1_id,)) as c1:
                    r1 = await c1.fetchone()
                async with conn.execute("SELECT display_name FROM players WHERE id=?", (p2_id,)) as c2:
                    r2 = await c2.fetchone()
            name1 = r1[0] if r1 else "?"
            name2 = r2[0] if r2 else "?"
            winner_name = name1 if res == "p1" else name2
            loser_name  = name2 if res == "p1" else name1
            await itx.channel.send(f"桌 {table_no}：{winner_name} 勝 {loser_name}(match {mid})")

            await itx.response.send_message("已記錄勝利並公告。", ephemeral=True)
            await self.cog._maybe_on_round_complete(self.tid, self.rid, itx.channel)

    class RoundActualView(discord.ui.View):
        """面板 3：實際職業(單選；寫入 match_player_meta 與 players.actual_class)"""
        def __init__(self, cog: 'SwissAll', tid: int, rid: int):
            super().__init__(timeout=None)
            self.cog = cog
            self.tid = tid
            self.rid = rid

        async def _set_actual(self, itx: discord.Interaction, label: str):
            r = await self.cog._find_user_round_match(self.tid, self.rid, itx.user.id)
            if not r:
                return await itx.response.send_message("找不到你在本輪的對局。", ephemeral=True)
            pid, (mid, *_rest) = r
            meta = await self.cog._mpm_get(mid, pid)
            p1, p2 = meta["pick1"], meta["pick2"]

            if not p1 or not p2:
                return await itx.response.send_message("請先完成兩個『使用牌組』的選擇，再回報實際職業。", ephemeral=True)
            if label not in (p1, p2):
                return await itx.response.send_message(f"實際職業需為你選的兩職業之一(目前：{p1} / {p2})。", ephemeral=True)

            await self.cog._mpm_upsert(mid, pid, actual=label)
            await self.cog._player_set_decks_if_ready(pid, p1, p2, label)
            await itx.response.send_message(f"已記錄你的實際職業：{label}", ephemeral=True)

        @discord.ui.button(label="精靈", style=discord.ButtonStyle.secondary, custom_id="swiss:ractual:elf")
        async def a_elf(self, itx, _):    await self._set_actual(itx, "精靈")
        @discord.ui.button(label="皇家", style=discord.ButtonStyle.secondary, custom_id="swiss:ractual:royal")
        async def a_royal(self, itx, _):  await self._set_actual(itx, "皇家")
        @discord.ui.button(label="巫師", style=discord.ButtonStyle.secondary, custom_id="swiss:ractual:witch")
        async def a_witch(self, itx, _):  await self._set_actual(itx, "巫師")
        @discord.ui.button(label="龍族", style=discord.ButtonStyle.secondary, custom_id="swiss:ractual:dragon")
        async def a_dragon(self, itx, _): await self._set_actual(itx, "龍族")
        @discord.ui.button(label="夜魔", style=discord.ButtonStyle.secondary, custom_id="swiss:ractual:night")
        async def a_night(self, itx, _):  await self._set_actual(itx, "夜魔")
        @discord.ui.button(label="主教", style=discord.ButtonStyle.secondary, custom_id="swiss:ractual:bishop")
        async def a_bishop(self, itx, _): await self._set_actual(itx, "主教")
        @discord.ui.button(label="復仇者", style=discord.ButtonStyle.secondary, custom_id="swiss:ractual:avenger")
        async def a_avenger(self, itx, _):await self._set_actual(itx, "復仇者")

    class NextStepView(discord.ui.View):
        def __init__(self, cog: 'SwissAll', tid: int):
            super().__init__(timeout=600)
            self.cog = cog
            self.tid = tid

        async def _is_organizer(self, itx: discord.Interaction) -> bool:
            org = await self.cog.get_organizer(self.tid)
            if itx.user.id == org or itx.user.guild_permissions.manage_guild:
                return True
            await itx.response.send_message("只有賽事建立者或管理員可以按此。", ephemeral=True)
            return False

        @discord.ui.button(label="下一輪配對", style=discord.ButtonStyle.primary, custom_id="swiss:continue")
        async def btn_next(self, itx: discord.Interaction, button: discord.ui.Button):
            if not await self._is_organizer(itx): return
            await self.cog.cmd_next_round(itx, self.tid)

        @discord.ui.button(label="建立決賽＋季軍戰(依前四)", style=discord.ButtonStyle.secondary, custom_id="swiss:top4manual")
        async def btn_top4(self, itx: discord.Interaction, button: discord.ui.Button):
            if not await self._is_organizer(itx): return
            await self.cog.cmd_make_top4(itx, self.tid)

    # ---------- Organizer Modals ----------
    class _RosterModal(discord.ui.Modal, title="目前名單（只讀）"):
        def __init__(self, cog: 'SwissAll', tid: int, roster_text: str):
            super().__init__()
            self.cog = cog; self.tid = tid
            self.info = discord.ui.TextInput(label="名單（自動生成，請關閉視窗即可）",
                                             style=discord.TextStyle.paragraph, default=roster_text, required=False)
            self.add_item(self.info)
        async def on_submit(self, itx: discord.Interaction):  # not used
            await itx.response.send_message("OK", ephemeral=True)

    class _ManualAddModal(discord.ui.Modal, title="手動加入/恢復"):
        who = discord.ui.TextInput(label="對象（@提及 / ID / 名稱）", required=True)
        def __init__(self, cog: 'SwissAll', tid: int):
            super().__init__(); self.cog = cog; self.tid = tid
        async def on_submit(self, itx: discord.Interaction):
            if not await self.cog._is_organizer_user(self.tid, itx.user):
                return await itx.response.send_message("沒有權限。", ephemeral=True)
            m = await self.cog._resolve_member(itx.guild, str(self.who))
            if not m: return await itx.response.send_message("找不到成員。", ephemeral=True)
            status = await self.cog.add_player(self.tid, m, active=1)
            if status == "banned":
                return await itx.response.send_message("該成員已被封禁，無法加入。", ephemeral=True)
            await self.cog._audit(self.tid, itx.user.id, "admin_manual_add", f"user={m.id}, status={status}")
            await itx.response.send_message(f"已加入/恢復：{m.display_name}（{status}）", ephemeral=True)

    class _ManualDropModal(discord.ui.Modal, title="手動退賽"):
        who = discord.ui.TextInput(label="對象（@提及 / ID / 名稱）", required=True)
        def __init__(self, cog: 'SwissAll', tid: int):
            super().__init__(); self.cog = cog; self.tid = tid
        async def on_submit(self, itx: discord.Interaction):
            if not await self.cog._is_organizer_user(self.tid, itx.user):
                return await itx.response.send_message("沒有權限。", ephemeral=True)
            m = await self.cog._resolve_member(itx.guild, str(self.who))
            if not m: return await itx.response.send_message("找不到成員。", ephemeral=True)
            await self.cog.mark_drop(self.tid, m.id)
            await self.cog._audit(self.tid, itx.user.id, "admin_manual_drop", f"user={m.id}")
            await itx.response.send_message(f"已設為退賽：{m.display_name}", ephemeral=True)

    class _SetWinnerModal(discord.ui.Modal, title="指定桌勝者（當前輪）"):
        table_no = discord.ui.TextInput(label="桌號（數字）", required=True)
        winner   = discord.ui.TextInput(label="勝者（輸入 p1 / p2 / @提及 / ID）", required=True)
        confirm  = discord.ui.TextInput(label="覆寫保護（若要覆寫已回報，請輸入 OVERRIDE）", required=False, placeholder="平時留空；覆蓋時輸入 OVERRIDE")
        note     = discord.ui.TextInput(label="備註（可空白）", required=False)
        def __init__(self, cog: 'SwissAll', tid: int):
            super().__init__(); self.cog = cog; self.tid = tid
        async def on_submit(self, itx: discord.Interaction):
            if not await self.cog._is_organizer_user(self.tid, itx.user):
                return await itx.response.send_message("沒有權限。", ephemeral=True)
            cur = await self.cog.current_round(self.tid)
            if not cur: return await itx.response.send_message("沒有進行中的輪次。", ephemeral=True)
            rid, _, _ = cur
            try:
                tno = int(str(self.table_no).strip())
            except ValueError:
                return await itx.response.send_message("桌號需為數字。", ephemeral=True)

            # 找到該桌 match
            async with self.cog.db() as conn:
                async with conn.execute(
                    "SELECT id,p1_id,p2_id,result,winner_player_id FROM matches WHERE round_id=? AND table_no=?",
                    (rid, tno)
                ) as cur2:
                    mrow = await cur2.fetchone()
            if not mrow:
                return await itx.response.send_message("找不到該桌。", ephemeral=True)

            mid, p1, p2, res, wpid = mrow
            winner_pid = None
            wtxt = str(self.winner).strip().lower()
            if wtxt in ("p1", "1"):
                winner_pid = p1; new_res = "p1"
            elif wtxt in ("p2", "2"):
                winner_pid = p2; new_res = "p2"
            else:
                mm = await self.cog._resolve_member(itx.guild, str(self.winner))
                if not mm: return await itx.response.send_message("無法解析勝者。", ephemeral=True)
                # map to pid
                pid = await self.cog._player_pid_by_user(self.tid, mm.id)
                if pid not in (p1, p2): return await itx.response.send_message("該玩家不在此桌。", ephemeral=True)
                winner_pid = pid; new_res = "p1" if pid == p1 else "p2"

            # 覆寫判斷
            override = (str(self.confirm).strip().upper() == "OVERRIDE")
            if res is not None and not override:
                return await itx.response.send_message("本桌已回報完成。若確定要覆寫，請在覆寫欄輸入 OVERRIDE。", ephemeral=True)

            async with self.cog.db() as conn:
                await conn.execute(
                    "UPDATE matches SET result=?, winner_player_id=?, notes=? WHERE id=?",
                    (new_res, winner_pid, f"ADMIN:{str(self.note)}", mid)
                )
                await conn.commit()

            # 重新計分（安全起見直接整體重算）
            await self.cog.recompute_scores(self.tid)
            await self.cog._audit(self.tid, itx.user.id, "admin_set_winner",
                                  f"t={tno}, mid={mid}, res={new_res}, winner_pid={winner_pid}, override={override}")
            await itx.response.send_message(f"已設定：桌 {tno} → {('P1' if new_res=='p1' else 'P2')} 勝。", ephemeral=True)

    class _SwapTableModal(discord.ui.Modal, title="一鍵改桌（交換兩位玩家當前桌號）"):
        a_text = discord.ui.TextInput(label="玩家A（@提及 / ID / 名稱）", required=True)
        b_text = discord.ui.TextInput(label="玩家B（@提及 / ID / 名稱）", required=True)
        note   = discord.ui.TextInput(label="原因/備註（可留空）", style=discord.TextStyle.paragraph, required=False)
        def __init__(self, cog: 'SwissAll', tid: int):
            super().__init__(); self.cog = cog; self.tid = tid
        async def on_submit(self, itx: discord.Interaction):
            if not await self.cog._is_organizer_user(self.tid, itx.user):
                return await itx.response.send_message("沒有權限。", ephemeral=True)
            cur = await self.cog.current_round(self.tid)
            if not cur: return await itx.response.send_message("目前沒有進行中的輪次。", ephemeral=True)
            rid, _, _ = cur
            mA = await self.cog._resolve_member(itx.guild, str(self.a_text))
            mB = await self.cog._resolve_member(itx.guild, str(self.b_text))
            if not mA or not mB:
                return await itx.response.send_message("找不到其中一位成員。", ephemeral=True)
            pidA = await self.cog._player_pid_by_user(self.tid, mA.id)
            pidB = await self.cog._player_pid_by_user(self.tid, mB.id)
            if not pidA or not pidB:
                return await itx.response.send_message("兩位都必須在本賽事名單內。", ephemeral=True)
            mrowA = await self.cog._find_match_by_pid(rid, pidA)
            mrowB = await self.cog._find_match_by_pid(rid, pidB)
            if not mrowA or not mrowB:
                return await itx.response.send_message("其中一位目前沒有被分配到桌號。", ephemeral=True)
            (midA, tnoA, _p1A, _p2A, resA), (midB, tnoB, _p1B, _p2B, resB) = mrowA, mrowB
            if (resA is not None) or (resB is not None):
                return await itx.response.send_message("有一桌已回報完成，無法交換。", ephemeral=True)

            async with self.cog.db() as conn:
                await conn.execute("UPDATE matches SET table_no=-1 WHERE id=?", (midA,))
                await conn.execute("UPDATE matches SET table_no=? WHERE id=?", (tnoA, midB))
                await conn.execute("UPDATE matches SET table_no=? WHERE id=?", (tnoB, midA))
                await conn.commit()

            await self.cog._audit(self.tid, itx.user.id, "admin_swap_table",
                                  f"A={mA.id}(mid={midA},t={tnoA}) <-> B={mB.id}(mid={midB},t={tnoB}); note={self.note}")
            await itx.response.send_message(
                f"已交換桌號：\n"
                f"- {mA.display_name}：桌 {tnoA} → {tnoB}\n"
                f"- {mB.display_name}：桌 {tnoB} → {tnoA}",
                ephemeral=True
            )

    class _SwapOppModal(discord.ui.Modal, title="黑箱換對手（交換兩位玩家所屬對局中的位置）"):
        a_text = discord.ui.TextInput(label="玩家A（@提及 / ID / 名稱）", required=True)
        b_text = discord.ui.TextInput(label="玩家B（@提及 / ID / 名稱）", required=True)
        note   = discord.ui.TextInput(label="原因/備註（可留空）", style=discord.TextStyle.paragraph, required=False)
        def __init__(self, cog: 'SwissAll', tid: int):
            super().__init__(); self.cog = cog; self.tid = tid
        async def on_submit(self, itx: discord.Interaction):
            if not await self.cog._is_organizer_user(self.tid, itx.user):
                return await itx.response.send_message("沒有權限。", ephemeral=True)
            cur = await self.cog.current_round(self.tid)
            if not cur: return await itx.response.send_message("目前沒有進行中的輪次。", ephemeral=True)
            rid, _, _ = cur
            mA = await self.cog._resolve_member(itx.guild, str(self.a_text))
            mB = await self.cog._resolve_member(itx.guild, str(self.b_text))
            if not mA or not mB:
                return await itx.response.send_message("找不到其中一位成員。", ephemeral=True)
            pidA = await self.cog._player_pid_by_user(self.tid, mA.id)
            pidB = await self.cog._player_pid_by_user(self.tid, mB.id)
            if not pidA or not pidB:
                return await itx.response.send_message("兩位都必須在本賽事名單內。", ephemeral=True)
            mrowA = await self.cog._find_match_by_pid(rid, pidA)
            mrowB = await self.cog._find_match_by_pid(rid, pidB)
            if not mrowA or not mrowB:
                return await itx.response.send_message("其中一位目前沒有對局。", ephemeral=True)
            (midA, tnoA, p1A, p2A, resA), (midB, tnoB, p1B, p2B, resB) = mrowA, mrowB
            if (resA is not None) or (resB is not None):
                return await itx.response.send_message("有一桌已回報完成，無法換對手。", ephemeral=True)
            # 找出 A/B 分別是各自對局的哪一側
            def side(pid, p1, p2): return "p1" if pid == p1 else ("p2" if pid == p2 else None)
            sideA = side(pidA, p1A, p2A); sideB = side(pidB, p1B, p2B)
            if not sideA or not sideB:
                return await itx.response.send_message("A 或 B 不在其對局中。", ephemeral=True)

            # 交換：把 midA 的 A 換成 B；把 midB 的 B 換成 A
            async with self.cog.db() as conn:
                if sideA == "p1":
                    await conn.execute("UPDATE matches SET p1_id=? WHERE id=?", (pidB, midA))
                else:
                    await conn.execute("UPDATE matches SET p2_id=? WHERE id=?", (pidB, midA))
                if sideB == "p1":
                    await conn.execute("UPDATE matches SET p1_id=? WHERE id=?", (pidA, midB))
                else:
                    await conn.execute("UPDATE matches SET p2_id=? WHERE id=?", (pidA, midB))
                await conn.commit()

            await self.cog._audit(self.tid, itx.user.id, "admin_swap_opponents",
                                  f"A={mA.id}(mid={midA},t={tnoA}) <-> B={mB.id}(mid={midB},t={tnoB}); note={self.note}")
            await itx.response.send_message(
                f"已交換對手：\n"
                f"- {mA.display_name} 與 {mB.display_name} 已交換所屬對局位置。", ephemeral=True
            )

    class _BanModal(discord.ui.Modal, title="封禁"):
        who = discord.ui.TextInput(label="對象（@提及 / ID / 名稱）", required=True)
        reason = discord.ui.TextInput(label="原因（可空白）", required=False)
        def __init__(self, cog: 'SwissAll', tid: int):
            super().__init__(); self.cog = cog; self.tid = tid
        async def on_submit(self, itx: discord.Interaction):
            if not await self.cog._is_organizer_user(self.tid, itx.user):
                return await itx.response.send_message("沒有權限。", ephemeral=True)
            m = await self.cog._resolve_member(itx.guild, str(self.who))
            if not m: return await itx.response.send_message("找不到成員。", ephemeral=True)
            async with self.cog.db() as conn:
                await conn.execute(
                    "INSERT OR IGNORE INTO tournament_bans(tournament_id,user_id,reason,by_user_id,created_at) VALUES(?,?,?,?,?)",
                    (self.tid, m.id, str(self.reason), itx.user.id, int(time.time()))
                )
                await conn.commit()
            await self.cog.mark_drop(self.tid, m.id)
            await self.cog._audit(self.tid, itx.user.id, "admin_ban", f"user={m.id}, reason={self.reason}")
            await itx.response.send_message(f"已封禁並退賽：{m.display_name}", ephemeral=True)

    class _UnbanModal(discord.ui.Modal, title="解禁"):
        who = discord.ui.TextInput(label="對象（@提及 / ID / 名稱）", required=True)
        def __init__(self, cog: 'SwissAll', tid: int):
            super().__init__(); self.cog = cog; self.tid = tid
        async def on_submit(self, itx: discord.Interaction):
            if not await self.cog._is_organizer_user(self.tid, itx.user):
                return await itx.response.send_message("沒有權限。", ephemeral=True)
            m = await self.cog._resolve_member(itx.guild, str(self.who))
            if not m: return await itx.response.send_message("找不到成員。", ephemeral=True)
            async with self.cog.db() as conn:
                await conn.execute(
                    "DELETE FROM tournament_bans WHERE tournament_id=? AND user_id=?",
                    (self.tid, m.id)
                )
                await conn.commit()
            await self.cog._audit(self.tid, itx.user.id, "admin_unban", f"user={m.id}")
            await itx.response.send_message(f"已解禁：{m.display_name}", ephemeral=True)

    class _BatchBanModal(discord.ui.Modal, title="批量封禁（每行一位，或以空白/逗號分隔）"):
        who_list = discord.ui.TextInput(
            label="對象清單",
            placeholder="貼上多個 @提及 / ID / 名稱，支援換行、逗號、空白分隔",
            style=discord.TextStyle.paragraph,
            required=True
        )
        reason = discord.ui.TextInput(label="原因（可空白）", required=False)
        def __init__(self, cog: 'SwissAll', tid: int):
            super().__init__(); self.cog = cog; self.tid = tid
        async def on_submit(self, itx: discord.Interaction):
            if not await self.cog._is_organizer_user(self.tid, itx.user):
                return await itx.response.send_message("沒有權限。", ephemeral=True)
            raw = str(self.who_list).replace(",", " ").replace("\n", " ").strip()
            tokens = [t for t in raw.split() if t]
            if not tokens:
                return await itx.response.send_message("清單為空。", ephemeral=True)
            rsn = str(self.reason) if self.reason else ""
            ok, not_found = [], []
            for tok in tokens:
                m = await self.cog._resolve_member(itx.guild, tok)
                if not m:
                    not_found.append(tok); continue
                async with self.cog.db() as conn:
                    await conn.execute(
                        "INSERT OR IGNORE INTO tournament_bans(tournament_id,user_id,reason,by_user_id,created_at) VALUES(?,?,?,?,?)",
                        (self.tid, m.id, rsn, itx.user.id, int(time.time()))
                    )
                    await conn.commit()
                await self.cog.mark_drop(self.tid, m.id)
                ok.append(m.display_name)
            await self.cog._audit(self.tid, itx.user.id, "admin_batch_ban",
                                  f"count={len(ok)}, reason={rsn}, not_found={len(not_found)}")
            msg = []
            msg.append(f"批量封禁完成：{len(ok)} 位")
            if ok: msg.append("已封禁並退賽： " + ", ".join(ok[:30]) + (" …" if len(ok) > 30 else ""))
            if not_found: msg.append("未辨識： " + ", ".join(not_found[:30]) + (" …" if len(not_found) > 30 else ""))
            await itx.response.send_message("\n".join(msg), ephemeral=True)

    class _TestSeedModal(discord.ui.Modal, title="測試：灌入 N 位假人"):
        n = discord.ui.TextInput(label="人數 N（1~200）", placeholder="例如 8", required=True)
        def __init__(self, cog: 'SwissAll', tid: int):
            super().__init__(); self.cog = cog; self.tid = tid
        async def on_submit(self, itx: discord.Interaction):
            if not await self.cog._is_organizer_user(self.tid, itx.user):
                return await itx.response.send_message("沒有權限。", ephemeral=True)
            try:
                n = max(1, min(200, int(str(self.n))))
            except ValueError:
                return await itx.response.send_message("請輸入 1~200 的整數。", ephemeral=True)
            async with self.cog.db() as conn:
                for i in range(n):
                    fake_uid = 10_000_000 + random.randint(1, 9_999_999)
                    name = f"測試玩家{str(i+1).zfill(2)}"
                    await conn.execute(
                        "INSERT OR IGNORE INTO players(tournament_id,user_id,display_name,active) VALUES(?,?,?,1)",
                        (self.tid, fake_uid, name)
                    )
                await conn.commit()
            await itx.response.send_message(f"已加入 {n} 位測試玩家。", ephemeral=True)

    class OpenPanelView(discord.ui.View):
        """公開訊息上的按鈕；主辦/管理點擊後以臨時訊息送出管理面板。"""
        def __init__(self, cog: 'SwissAll', tid: int):
            super().__init__(timeout=None)
            self.cog = cog
            self.tid = tid

        @discord.ui.button(label="打開管理面板（僅主辦/管理）", style=discord.ButtonStyle.primary, custom_id="swiss:openpanel")
        async def open_panel(self, itx: discord.Interaction, _):
            # 權限檢查
            if not await self.cog._is_organizer_user(self.tid, itx.user):
                return await itx.response.send_message("需要主辦者或管理員權限。", ephemeral=True)

            # 以臨時訊息送出完整管理面板
            await itx.response.send_message("Swiss 管理面板：", view=self.cog.PanelView(self.cog, self.tid), ephemeral=True)

    # -------------- Organizer Panel --------------
    class PanelView(discord.ui.View):
        """管理面板：集中所有 swiss 功能按鈕。"""
        def __init__(self, cog: 'SwissAll', tid: int):
            super().__init__(timeout=None)
            self.cog = cog
            self.tid = tid

        async def _adm(self, itx: discord.Interaction) -> bool:
            if await self.cog._is_organizer_user(self.tid, itx.user):
                return True
            await itx.response.send_message("需要主辦者或管理員權限。", ephemeral=True)
            return False

        @discord.ui.button(label="開始比賽", style=discord.ButtonStyle.success, custom_id="swiss:startbtn")
        async def btn_start(self, itx: discord.Interaction, button: discord.ui.Button):
            if not await self._adm(itx): return
            await self.cog.cmd_start_round(itx, self.tid)

        @discord.ui.button(label="下一輪配對", style=discord.ButtonStyle.primary, custom_id="swiss:nextbtn")
        async def btn_next(self, itx: discord.Interaction, button: discord.ui.Button):
            if not await self._adm(itx): return
            await self.cog.cmd_next_round(itx, self.tid)

        @discord.ui.button(label="建立決賽＋季軍戰", style=discord.ButtonStyle.secondary, custom_id="swiss:top4btn")
        async def btn_top4(self, itx: discord.Interaction, button: discord.ui.Button):
            if not await self._adm(itx): return
            await self.cog.cmd_make_top4(itx, self.tid)

        @discord.ui.button(label="顯示積分表", style=discord.ButtonStyle.secondary, custom_id="swiss:standbtn")
        async def btn_stand(self, itx: discord.Interaction, button: discord.ui.Button):
            if not await self._adm(itx): return
            if not itx.response.is_done():
                await itx.response.defer(ephemeral=True)
            f = await self.cog.render_standings_image(self.tid, itx.channel)
            if f:
                await itx.channel.send(file=f)
                await itx.followup.send("已送出積分表。", ephemeral=True)
            else:
                await itx.followup.send("已輸出文字排名。", ephemeral=True)

        @discord.ui.button(label="發送報名面板", style=discord.ButtonStyle.secondary, custom_id="swiss:regpanel")
        async def btn_regpanel(self, itx: discord.Interaction, button: discord.ui.Button):
            if not await self._adm(itx): return
            await itx.channel.send("報名/退出/退賽面板：", view=self.cog.RegView(self.cog, self.tid))
            await itx.response.send_message("已發送報名面板（公開）。", ephemeral=True)

        @discord.ui.button(label="我的成績(私訊)", style=discord.ButtonStyle.secondary, custom_id="swiss:mebtn")
        async def btn_me(self, itx: discord.Interaction, button: discord.ui.Button):
            await self.cog.ui_show_me(itx, self.tid, itx.user)

        # --- Roster & Registration management ---
        @discord.ui.button(label="查看名單", style=discord.ButtonStyle.secondary, custom_id="swiss:roster")
        async def btn_roster(self, itx: discord.Interaction, _):
            if not await self._adm(itx): return
            players = await self.cog.fetch_players(self.tid, active_only=False)
            lines = []
            for p in players:
                tag = "✅" if p.active else "❌"
                lines.append(f"{tag} {p.display_name} (uid={p.user_id}) 分數={p.score}")
            text = "\n".join(lines) if lines else "（目前沒有人）"
            await itx.response.send_modal(self.cog._RosterModal(self.cog, self.tid, text))

        @discord.ui.button(label="手動加入/恢復", style=discord.ButtonStyle.success, custom_id="swiss:add")
        async def btn_add(self, itx: discord.Interaction, _):
            if not await self._adm(itx): return
            await itx.response.send_modal(self.cog._ManualAddModal(self.cog, self.tid))

        @discord.ui.button(label="手動退賽", style=discord.ButtonStyle.danger, custom_id="swiss:dropmanual")
        async def btn_dropmanual(self, itx: discord.Interaction, _):
            if not await self._adm(itx): return
            await itx.response.send_modal(self.cog._ManualDropModal(self.cog, self.tid))

        # --- Pairing management ---
        @discord.ui.button(label="指定桌勝者", style=discord.ButtonStyle.primary, custom_id="swiss:setwinner")
        async def btn_setwinner(self, itx: discord.Interaction, _):
            if not await self._adm(itx): return
            await itx.response.send_modal(self.cog._SetWinnerModal(self.cog, self.tid))

        @discord.ui.button(label="一鍵改桌（換桌號）", style=discord.ButtonStyle.primary, custom_id="swiss:swaptable")
        async def btn_swaptable(self, itx: discord.Interaction, _):
            if not await self._adm(itx): return
            await itx.response.send_modal(self.cog._SwapTableModal(self.cog, self.tid))

        @discord.ui.button(label="黑箱換對手（交換位置）", style=discord.ButtonStyle.primary, custom_id="swiss:swapopp")
        async def btn_swapopp(self, itx: discord.Interaction, _):
            if not await self._adm(itx): return
            await itx.response.send_modal(self.cog._SwapOppModal(self.cog, self.tid))

        # --- Ban tools ---
        @discord.ui.button(label="封禁", style=discord.ButtonStyle.danger, custom_id="swiss:ban")
        async def btn_ban(self, itx: discord.Interaction, _):
            if not await self._adm(itx): return
            await itx.response.send_modal(self.cog._BanModal(self.cog, self.tid))

        @discord.ui.button(label="解禁", style=discord.ButtonStyle.secondary, custom_id="swiss:unban")
        async def btn_unban(self, itx: discord.Interaction, _):
            if not await self._adm(itx): return
            await itx.response.send_modal(self.cog._UnbanModal(self.cog, self.tid))

        @discord.ui.button(label="批量封禁", style=discord.ButtonStyle.danger, custom_id="swiss:batchban")
        async def btn_batchban(self, itx: discord.Interaction, _):
            if not await self._adm(itx): return
            await itx.response.send_modal(self.cog._BatchBanModal(self.cog, self.tid))

        # --- Test helpers (optional) ---
        @discord.ui.button(label="測試：灌入假人", style=discord.ButtonStyle.secondary, custom_id="swiss:test:seed")
        async def btn_test_seed(self, itx: discord.Interaction, _):
            if not await self._adm(itx): return
            await itx.response.send_modal(self.cog._TestSeedModal(self.cog, self.tid))

        @discord.ui.button(label="測試：填職業（不結算）", style=discord.ButtonStyle.secondary, custom_id="swiss:test:fillmeta")
        async def btn_test_fillmeta(self, itx: discord.Interaction, _):
            if not await self._adm(itx): return
            tid = self.tid
            cur = await self.cog.current_round(tid)
            if not cur:
                return await itx.response.send_message("沒有進行中的輪次。", ephemeral=True)
            rid, _, _ = cur
            rows = await self.cog.list_matches_round(rid)
            filled = 0
            for mid, _tno, p1, p2, res, _ in rows:
                if res is None and p1 and p2:
                    await self.cog._test_fill_for_match(mid, p1, p2)
                    filled += 1
            await itx.response.send_message(f"已為本輪 {filled} 桌填入兩職業與實際職業（不含 BYE/已結束）。", ephemeral=True)

        @discord.ui.button(label="測試：隨機結算本輪", style=discord.ButtonStyle.danger, custom_id="swiss:test:simulate")
        async def btn_test_simulate(self, itx: discord.Interaction, _):
            if not await self._adm(itx): return
            tid = self.tid
            cur = await self.cog.current_round(tid)
            if not cur:
                return await itx.response.send_message("沒有進行中的輪次。", ephemeral=True)
            rid, _rno, _status = cur
            rows = await self.cog.list_matches_round(rid)
            any_done = False
            for mid, tno, p1, p2, res, _ in rows:
                if res is not None or p1 is None or p2 is None:
                    continue
                await self.cog._test_fill_for_match(mid, p1, p2)
                winner_pid = p1 if random.random() < 0.5 else p2
                result = "p1" if winner_pid == p1 else "p2"
                ok, _ = await self.cog.set_match_result_atomic(mid, result, winner_pid)
                if ok:
                    await self.cog.update_score_for_match(tid, p1, p2, result, winner_pid)
                    async with self.cog.db() as conn:
                        name1 = (await (await conn.execute("SELECT display_name FROM players WHERE id=?", (p1,))).fetchone())[0]
                        name2 = (await (await conn.execute("SELECT display_name FROM players WHERE id=?", (p2,))).fetchone())[0]
                    winner_name = name1 if result == "p1" else name2
                    loser_name  = name2 if result == "p1" else name1
                    await itx.channel.send(f"桌 {tno}：{winner_name} 勝 {loser_name}(match {mid})")
                    any_done = True
            if not any_done:
                return await itx.response.send_message("沒有可模擬的對局（可能都是 BYE 或已回報）。", ephemeral=True)
            await itx.followup.send("已隨機完成回報並公告。檢查是否可結束本輪…", ephemeral=True)
            await self.cog._maybe_on_round_complete(tid, rid, itx.channel)

    class BootView(discord.ui.View):
        """尚未建立賽事時顯示的前置面板（任何人都可建立）。"""
        def __init__(self, cog: 'SwissAll', guild_id: int):
            super().__init__(timeout=None)
            self.cog = cog
            self.guild_id = guild_id

        @discord.ui.button(label="舉辦比賽(以今日日期)", style=discord.ButtonStyle.success, custom_id="swiss:boot:create")
        async def create(self, itx: discord.Interaction, button: discord.ui.Button):
            # 任何人都可建立；建立者成為 organizer
            name = dt.date.today().isoformat()
            tid = await self.cog.create_tournament(self.guild_id, itx.user.id, name)
            # 管理面板→臨時給建立者；報名面板→公開
            await itx.response.send_message(f"已建立 `{name}` (ID={tid})。管理面板僅你可見。", ephemeral=True)
            await itx.followup.send("Swiss 管理面板：", view=self.cog.PanelView(self.cog, tid), ephemeral=True)
            await itx.channel.send("報名/退出/退賽面板：", view=self.cog.RegView(self.cog, tid))


    # -------------- Pairing / Round flow --------------
    async def _pair_and_post(self, channel: discord.abc.Messageable, tid: int, rid: int):
        players = await self.fetch_players(tid, active_only=True)
        if len(players) < 2:
            await channel.send("❌ 選手不足(至少需要 2 人)。")
            return

        top = max(p.score for p in players)
        group = [p for p in players if p.score == top]
        others = [p for p in players if p.score != top]
        random.shuffle(group); random.shuffle(others)

        pairs: List[Tuple[Optional[PlayerRow], Optional[PlayerRow]]] = []
        while len(group) >= 2:
            a, b = group.pop(), group.pop()
            pairs.append((a, b))
        leftovers = group[:]
        pool = others + leftovers
        random.shuffle(pool)
        while len(pool) >= 2:
            a, b = pool.pop(), pool.pop()
            pairs.append((a, b))
        if pool:
            pairs.append((pool.pop(), None))  # BYE

        lines = ["本輪對戰表："]
        table = 1

        for p1, p2 in pairs:
            if p1 and p2:
                mid = await self.add_match(tid, rid, table, p1.id, p2.id)
                lines.append(f"桌 {table}: {p1.display_name} vs {p2.display_name} (match {mid})")
            elif p1 and not p2:
                mid = await self.add_match(tid, rid, table, p1.id, None, result="bye", winner_player_id=p1.id, notes="BYE")
                await self.update_score_for_match(tid, p1.id, None, "bye", p1.id)
                lines.append(f"桌 {table}: {p1.display_name} 免戰 (BYE) (match {mid})")
            elif p2 and not p1:
                mid = await self.add_match(tid, rid, table, None, p2.id, result="bye", winner_player_id=p2.id, notes="BYE")
                await self.update_score_for_match(tid, None, p2.id, "bye", p2.id)
                lines.append(f"桌 {table}: {p2.display_name} 免戰 (BYE) (match {mid})")
            table += 1
        await channel.send("\n".join(lines))

        # ✅ 每輪只送三則面板訊息(更不擁擠)
        await channel.send(
            "本輪回報面板：使用的雙職業",
            view=self.RoundDeckView(self, tid, rid)
        )
        await channel.send(
            "本輪回報面板(2/3)\n勝者請點以下按鈕",
            view=self.RoundWinnerView(self, tid, rid)
        )
        await channel.send(
            "本輪回報面板(3/3)\n使用職業(不管輸贏都需要填寫)",
            view=self.RoundActualView(self, tid, rid)
        )

    # -------------- Standings & tiebreaks --------------
    async def compute_standings(self, tid: int, active_only=True):
        """
        排行欄位：Pos | Player | Pts | MWP | OppMW | OPPT1
        定義：
        - Pts：每勝 +3，包含 BYE
        - MWP：自己的勝率，BYE 計一場勝利（分子+1、分母+1）
        - OppMW：所有「實際對手」的 MWP 平均（BYE 不算對手）
        - SOS：所有對手的 Pts 總和（BYE 無對手不納入）
        - SOSS：對手的對手 Pts 總和（各對手的對手清單彙總；同樣只數實際對手）
        - OPPT1 = 0.26123 + 0.004312*MP - 0.007638*SOS + 0.003810*SOSS + 0.23119*OppMW
        排序：Pts → OppMW → name
        """
        async with self.db() as conn:
            async with conn.execute(
                "SELECT id,user_id,display_name,active,score FROM players WHERE tournament_id=?",
                (tid,),
            ) as cur:
                prow = await cur.fetchall()
            async with conn.execute(
                "SELECT id, round_id, p1_id, p2_id, result, winner_player_id "
                "FROM matches WHERE tournament_id=?",
                (tid,),
            ) as cur:
                mrows = await cur.fetchall()

        players = {
            r[0]: {
                "pid": r[0],
                "user_id": r[1],
                "name": r[2],
                "active": r[3],
                "Pts": float(r[4]),
                "wins": 0,
                "played": 0,
                "opp_pids": set(),
            }
            for r in prow
        }

        for mid, rid, p1, p2, res, wpid in mrows:
            is_bye = (p1 is None) ^ (p2 is None)
            if res is None:
                continue
            if is_bye:
                winner = p1 if (p1 is not None and (res == "p1" or (res == "bye"))) else (
                        p2 if (p2 is not None and (res == "p2" or (res == "bye"))) else None)
                if winner in players:
                    players[winner]["wins"] += 1
                    players[winner]["played"] += 1
                continue
            if p1 in players: players[p1]["played"] += 1
            if p2 in players: players[p2]["played"] += 1
            if wpid in players: players[wpid]["wins"] += 1
            if p1 in players and p2 in players:
                players[p1]["opp_pids"].add(p2)
                players[p2]["opp_pids"].add(p1)

        for p in players.values():
            p["MWP"] = (p["wins"] / p["played"]) if p["played"] > 0 else 0.0

        def _pts(pid: int) -> float: return players[pid]["Pts"] if pid in players else 0.0
        def _mwp(pid: int) -> float: return players[pid]["MWP"] if pid in players else 0.0

        for p in players.values():
            opps = [players[op] for op in p["opp_pids"] if op in players]
            p["OppMW"] = (sum(_mwp(op["pid"]) for op in opps) / len(opps)) if opps else 0.0
            p["SOS"] = sum(_pts(op["pid"]) for op in opps)
            soss_sum = 0.0
            for op in opps:
                for op2 in op["opp_pids"]:
                    if op2 == p["pid"]: continue
                    soss_sum += _pts(op2)
            p["SOSS"] = soss_sum
            MP = p["Pts"]; SOS = p["SOS"]; SOSS = p["SOSS"]; OMW = p["OppMW"]
            p["OPPT1"] = 0.26123 + 0.004312 * MP - 0.007638 * SOS + 0.003810 * SOSS + 0.23119 * OMW

        ordered = sorted(
            players.values(),
            key=lambda x: (-x["Pts"], -x["OppMW"], x["name"].lower()),
        )

        rows = []
        for pos, p in enumerate(ordered, 1):
            if active_only and not p["active"]:
                continue
            rows.append({
                "rank": pos,
                "pid": p["pid"],
                "name": p["name"],
                "Pos": pos,
                "Player": p["name"],
                "Pts": round(p["Pts"], 3),
                "MWP": round(p["MWP"], 4),
                "OppMW": round(p["OppMW"], 4),
                "OPPT1": round(p["OPPT1"], 6),
            })
        return rows

    async def render_standings_image(self, tid: int, channel: discord.abc.Messageable) -> Optional[discord.File]:
        rows = await self.compute_standings(tid, active_only=False)
        headers = ["Pos", "Player", "Pts", "MWP", "OppMW", "OPPT1"]
        table = [
            [r["Pos"], r["Player"], r["Pts"], r["MWP"], r["OppMW"], round(r.get("OPPT1", 0.0), 4)]
            for r in rows
        ]
        try:
            import os
            import matplotlib
            import matplotlib.pyplot as plt
            from matplotlib import font_manager

            matplotlib.rcParams["axes.unicode_minus"] = False

            def _pick_cjk_font() -> "matplotlib.font_manager.FontProperties":
                env_path = os.getenv("SWISS_CJK_FONT")
                if env_path and os.path.isfile(env_path):
                    return font_manager.FontProperties(fname=env_path)
                candidates = [
                    "Microsoft JhengHei", "Microsoft YaHei", "SimHei", "PMingLiU", "MingLiU",
                    "PingFang TC", "PingFang SC", "Hiragino Sans",
                    "Noto Sans CJK TC", "Noto Sans CJK SC", "Noto Sans CJK JP",
                    "Noto Sans TC", "Source Han Sans TW", "Source Han Sans SC", "Source Han Sans JP",
                ]
                for name in candidates:
                    try:
                        path = font_manager.findfont(name, fallback_to_default=False)
                        if os.path.isfile(path):
                            return font_manager.FontProperties(fname=path)
                    except Exception:
                        continue
                return font_manager.FontProperties()

            fp = _pick_cjk_font()
            fig, ax = plt.subplots(figsize=(10, min(0.6 * max(4, len(table)), 20)))
            ax.axis("off")
            tbl = ax.table(cellText=table, colLabels=headers, cellLoc="center", loc="upper left")
            tbl.auto_set_font_size(False); tbl.set_fontsize(9); tbl.scale(1, 1.2)
            for cell in tbl.get_celld().values():
                cell.get_text().set_fontproperties(fp)
            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=200, bbox_inches="tight")
            plt.close(fig)
            buf.seek(0)
            return discord.File(buf, filename="standings.png")
        except Exception:
            lines = ["目前積分：", "```"]
            lines.append("\t".join(headers))
            for row in table:
                lines.append("\t".join(str(x) for x in row))
            lines.append("```")
            for ck in chunk_text("\n".join(lines)):
                await channel.send(ck)
            return None

    # -------------- Round complete hook --------------
    async def _maybe_on_round_complete(self, tid: int, rid: int, channel: discord.abc.Messageable):
        rows = await self.list_matches_round(rid)
        if any(r[4] is None for r in rows):  # 尚有未回報
            return
        await self.close_round(rid)
        await self._sync_round_meta_to_players(rid)
        await self.recompute_scores(tid)

        status = await self.tour_status(tid)

        async def _pid_name(pid: Optional[int]) -> str:
            if pid is None: return "?"
            async with self.db() as conn:
                async with conn.execute("SELECT display_name FROM players WHERE id=?", (pid,)) as c:
                    r = await c.fetchone()
                    return r[0] if r else str(pid)

        if status == "top4_finals":
            final_row = next((r for r in rows if r[1] == 1), None)
            third_row = next((r for r in rows if r[1] == 2), None)
            if not final_row or not third_row:
                async with self.db() as conn:
                    async with conn.execute(
                        "SELECT id,table_no,p1_id,p2_id,result,winner_player_id,notes FROM matches WHERE round_id=?",
                        (rid,)
                    ) as cur:
                        m2 = await cur.fetchall()
                for r in m2:
                    if r[6] == "FINAL":  final_row = r[:6]
                    if r[6] == "THIRD":  third_row = r[:6]

            if not final_row or not third_row:
                await channel.send("⚠️ 找不到決賽或季軍戰的對局資訊，請檢查回報。")
                return

            _, _, f_p1, f_p2, _, f_wpid = final_row
            _, _, t_p1, t_p2, _, t_wpid = third_row

            first_pid  = f_wpid
            second_pid = f_p2 if f_wpid == f_p1 else f_p1
            third_pid  = t_wpid
            fourth_pid = t_p2 if t_wpid == t_p1 else t_p1

            n1 = await _pid_name(first_pid)
            n2 = await _pid_name(second_pid)
            n3 = await _pid_name(third_pid)
            n4 = await _pid_name(fourth_pid)

            await self.set_status(tid, "finished")
            await self._sync_round_meta_to_players(rid)
            await self.recompute_scores(tid)
            await channel.send(
                "本賽事結束！最終名次：\n"
                f"第一名：{n1}\n"
                f"第二名：{n2}\n"
                f"第三名：{n3}\n"
                f"第四名：{n4}"
            )
            return

        file = await self.render_standings_image(tid, channel)
        org_id = await self.get_organizer(tid)
        mention = f"<@{org_id}>" if org_id else "主辦者"
        if file:
            await channel.send(content=f"{mention} 本輪已結束。是否前往下一輪？",
                               file=file, view=self.NextStepView(self, tid))
        else:
            await channel.send(f"{mention} 本輪已結束。是否前往下一輪？",
                               view=self.NextStepView(self, tid))

    # -------------- Internal helpers used by buttons/commands --------------
    async def _reply(self, itx_or_ctx, content: str):
        if isinstance(itx_or_ctx, discord.Interaction):
            if not itx_or_ctx.response.is_done():
                await itx_or_ctx.response.send_message(content, ephemeral=True)
            else:
                await itx_or_ctx.followup.send(content, ephemeral=True)
        else:
            await itx_or_ctx.send(content)

    async def _sync_round_meta_to_players(self, rid: int):
        async with self.db() as conn:
            async with conn.execute("SELECT id, p1_id, p2_id FROM matches WHERE round_id=?", (rid,)) as cur:
                matches = await cur.fetchall()
            for mid, p1, p2 in matches:
                for pid in (p1, p2):
                    if not pid: continue
                    async with conn.execute(
                        "SELECT pick1, pick2, actual FROM match_player_meta WHERE match_id=? AND player_id=?",
                        (mid, pid)
                    ) as c2:
                        meta = await c2.fetchone()
                    if meta:
                        pick1, pick2, actual = meta
                        await conn.execute(
                            "UPDATE players SET deck1=?, deck2=?, actual_class=? WHERE id=?",
                            (pick1, pick2, actual, pid)
                        )
            await conn.commit()

    async def cmd_start_round(self, itx_or_ctx, tid: int):
        await self.setup_db()
        status = await self.tour_status(tid)
        if status not in ("register", "seeding"):
            return await self._reply(itx_or_ctx, "目前狀態不允許開始新一輪。")
        players = await self.fetch_players(tid, active_only=True)
        if len(players) < 2:
            return await self._reply(itx_or_ctx, "❌ 選手不足(至少需要 2 人)。")
        await self.set_status(tid, "swiss")
        rid = await self.create_round(tid)
        ch = itx_or_ctx.channel if isinstance(itx_or_ctx, (discord.Interaction, commands.Context)) else itx_or_ctx
        await self._pair_and_post(ch, tid, rid)
        await self._reply(itx_or_ctx, "第一輪已建立。")

    async def cmd_next_round(self, itx_or_ctx, tid: int):
        await self.setup_db()
        status = await self.tour_status(tid)
        if status != "swiss":
            return await self._reply(itx_or_ctx, "目前非瑞士輪狀態。")
        cur = await self.current_round(tid)
        if cur and cur[2] == "ongoing":
            rows = await self.list_matches_round(cur[0])
            if any(r[4] is None for r in rows):
                return await self._reply(itx_or_ctx, "仍有對局未回報，無法進入下一輪。")
            await self.close_round(cur[0])
        players = await self.fetch_players(tid, active_only=True)
        if len(players) < 2:
            return await self._reply(itx_or_ctx, "❌ 選手不足(至少需要 2 人)。")
        rid = await self.create_round(tid)
        ch = itx_or_ctx.channel if isinstance(itx_or_ctx, (discord.Interaction, commands.Context)) else itx_or_ctx
        await self._pair_and_post(ch, tid, rid)
        await self._reply(itx_or_ctx, "下一輪已建立。")

    async def cmd_make_top4(self, itx_or_ctx, tid: int):
        await self.setup_db()
        status = await self.tour_status(tid)
        if status != "swiss":
            return await self._reply(itx_or_ctx, "目前無法建立決賽與季軍戰(需在瑞士輪階段)。")
        await self.recompute_scores(tid)
        standings = await self.compute_standings(tid, active_only=True)
        if len(standings) < 4:
            return await self._reply(itx_or_ctx, "需要至少 4 位有效選手才能建立決賽與季軍戰。")
        top4 = standings[:4]
        rid = await self.create_round(tid)
        mf = await self.add_match(tid, rid, 1, top4[0]["pid"], top4[1]["pid"], notes="FINAL")
        m3 = await self.add_match(tid, rid, 2, top4[2]["pid"], top4[3]["pid"], notes="THIRD")
        await self.set_status(tid, "top4_finals")
        ch = itx_or_ctx.channel if isinstance(itx_or_ctx, (discord.Interaction, commands.Context)) else itx_or_ctx
        await ch.send(
            "已建立最終對局(依積分前四)：\n"
            f"決賽：{top4[0]['name']} vs {top4[1]['name']} (match {mf})\n"
            f"季軍戰：{top4[2]['name']} vs {top4[3]['name']} (match {m3})"
        )
        await ch.send("本輪回報面板：使用雙職業", view=self.RoundDeckView(self, tid, rid))
        await ch.send("本輪回報面板(2/3)\n勝者請點以下按鈕", view=self.RoundWinnerView(self, tid, rid))
        await ch.send("本輪回報面板(3/3)\n使用職業(不管輸贏都需要填寫)", view=self.RoundActualView(self, tid, rid))

    async def ui_show_me(self, itx: discord.Interaction, tid: int, member: discord.Member):
        async with self.db() as conn:
            async with conn.execute(
                "SELECT id,display_name,score,deck1,deck2,actual_class FROM players WHERE tournament_id=? AND user_id=?",
                (tid, member.id),
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return await itx.response.send_message("你不在本賽事名單中。", ephemeral=True)
        pid, dname, score, deck1, deck2, actual = row
        async with self.db() as conn:
            q = (
                "SELECT r.round_no, m.table_no, CASE WHEN m.p1_id=? THEN m.p2_id ELSE m.p1_id END AS opp_pid, "
                "m.result, m.winner_player_id, m.id "
                "FROM matches m JOIN rounds r ON m.round_id=r.id "
                "WHERE m.tournament_id=? AND (m.p1_id=? OR m.p2_id=?) ORDER BY r.round_no, m.table_no"
            )
            async with conn.execute(q, (pid, tid, pid, pid)) as cur:
                rows = await cur.fetchall()

        opp_name: Dict[int, str] = {}
        details = []
        for rno, tno, opp_pid, res, wpid, mid in rows:
            if opp_pid and opp_pid not in opp_name:
                async with self.db() as conn:
                    async with conn.execute("SELECT display_name FROM players WHERE id=?", (opp_pid,)) as c2:
                        rr = await c2.fetchone()
                        opp_name[opp_pid] = rr[0] if rr else str(opp_pid)
            opp = "BYE" if opp_pid is None else opp_name.get(opp_pid, str(opp_pid))
            if res == "bye":
                you = "Win"
            elif res in ("p1", "p2"):
                you = "Win" if pid == wpid else "Loss"
            else:
                you = res or "?"
            details.append(f"R{rno} 桌{tno} vs {opp}: {you} (match {mid})")
        lines = [f"{dname} 的成績(總分 {score})：", f"最近紀錄的牌組：{deck1 or '-'}, {deck2 or '-'}；實際職業：{actual or '-'}", *details]
        for ck in chunk_text("\n".join(lines)):
            await itx.response.send_message(ck, ephemeral=True)
            break

    # -------------- Test helper --------------
    async def _test_fill_for_match(self, mid: int, p1: Optional[int], p2: Optional[int]):
        async def _fill_player(pid: Optional[int]):
            if not pid:
                return
            picks = random.sample(CLASS_LABELS, 2)
            actual = random.choice(picks)
            await self._mpm_upsert(mid, pid, pick1=picks[0], pick2=picks[1], actual=actual)
            await self._player_set_decks_if_ready(pid, picks[0], picks[1], actual)
        await _fill_player(p1)
        await _fill_player(p2)

    # -------------- Commands (only one: !swiss) --------------
    @commands.group(name="swiss", invoke_without_command=True)
    async def swiss_root(self, ctx: commands.Context):
        """公開送一則訊息；由按鈕開『臨時管理面板』。未建賽時任何人可建立。"""
        await self.setup_db()
        gid = ctx.guild.id
        tid = await self.guild_latest_tid(gid)

        if not tid:
            # 不再限制管理員：任何人都可見並可建立
            await ctx.send("Swiss 前置控制面板（尚未建立賽事）：", view=self.BootView(self, gid))
            return

        # 已有賽事：公開訊息 + 按鈕；面板權限由 _is_organizer_user 控制（發起人/管理員/擁有者）
        await ctx.send(
            f"Swiss 控制：目前賽事 ID={tid}\n"
            "－ 主辦/管理/伺服器擁有者可按下方按鈕，以**臨時訊息**開啟管理面板。\n"
            "－ 一般選手請使用公開的報名面板進行報名/退賽。",
            view=self.OpenPanelView(self, tid)
        )



# ---------- setup ----------
async def setup(bot: commands.Bot):
    if bot.get_cog("SwissAll") is None:
        await bot.add_cog(SwissAll(bot))
