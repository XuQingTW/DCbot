# -*- coding: utf-8 -*-
"""
Swiss Tournament (All-in-One, Updated with Boot Panel & Date-Naming)
- Registration / Swiss rounds / reporting / queries / auto Top4 with custom rule
- Discord UI buttons: join/leave/drop, report wins/AFK/concede, admin panel
- Round-complete workflow: render standings image and ask organizer to continue
- NEW (Match UI overhaul): per-table 3-message flow (deck picks ×2 w/ reset, winner announce, actual class)
- NEW: Persist class picks to DB per match and onto players table (deck1, deck2, actual_class)

Requirements:
- discord.py v2.x
- aiosqlite
- matplotlib (optional; used to render standings image; falls back to text if unavailable)
"""

from __future__ import annotations
import asyncio
import random
import time
import io
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
    async def _ack(self, itx_or_ctx, ephemeral: bool = True):
        """若是 Interaction 且尚未回覆，先做 defer，避免 10062。"""
        if isinstance(itx_or_ctx, discord.Interaction) and not itx_or_ctx.response.is_done():
            await itx_or_ctx.response.defer(ephemeral=ephemeral)
    
    async def _find_user_round_match(self, tid: int, rid: int, user_id: int):
        """
        回傳 (player_pid, (mid, table_no, p1_id, p2_id, result, winner_player_id))；找不到則回傳 None
        """
        async with self.db() as conn:
            # 找此賽事內，該 Discord 使用者對應的 players.id
            async with conn.execute(
                "SELECT id FROM players WHERE tournament_id=? AND user_id=?",
                (tid, user_id)
            ) as cur:
                r = await cur.fetchone()
            if not r:
                return None
            pid = r[0]
            # 找本輪屬於此玩家的對局(若同時有多桌，取桌號最小的一桌)
            async with conn.execute(
                "SELECT id, table_no, p1_id, p2_id, result, winner_player_id "
                "FROM matches WHERE round_id=? AND (p1_id=? OR p2_id=?) "
                "ORDER BY table_no LIMIT 1",
                (rid, pid, pid)
            ) as cur2:
                mrow = await cur2.fetchone()
        return (pid, mrow) if mrow else None
    
    async def _sync_round_meta_to_players(self, rid: int):
        """
        將本輪所有對局的 match_player_meta(pick1,pick2,actual) 同步回 players(deck1,deck2,actual_class)。
        用於保險：決賽/季軍賽或任何一輪結束時都確保玩家資料已更新。
        """
        async with self.db() as conn:
            async with conn.execute("SELECT id, p1_id, p2_id FROM matches WHERE round_id=?", (rid,)) as cur:
                matches = await cur.fetchall()

            for mid, p1, p2 in matches:
                for pid in (p1, p2):
                    if not pid:
                        continue
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


    # -------------- DB --------------
    def db(self):
        # return async context manager (FIX for previous coroutine issue)
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
                    score REAL NOT NULL DEFAULT 0
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
                    result TEXT,                 -- 'p1','p2','bye','void'
                    winner_player_id INTEGER,
                    notes TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_matches_round ON matches(round_id);
                CREATE INDEX IF NOT EXISTS idx_players_tid ON players(tournament_id);
                """
            )
            # best-effort migrations
            try:
                await conn.execute("ALTER TABLE tournaments ADD COLUMN organizer_id INTEGER")
            except Exception:
                pass
            # 新增玩家欄位：deck1, deck2, actual_class
            for col in ("deck1", "deck2", "actual_class"):
                try:
                    await conn.execute(f"ALTER TABLE players ADD COLUMN {col} TEXT")
                except Exception:
                    pass
            # 每局每位玩家的職業選擇資料
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS match_player_meta (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    match_id INTEGER NOT NULL,
                    player_id INTEGER NOT NULL,      -- players.id
                    pick1 TEXT,
                    pick2 TEXT,
                    actual TEXT,
                    UNIQUE(match_id, player_id)
                )
                """
            )
            await conn.commit()
        self._ready = True

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

    async def add_player(self, tid: int, member: discord.abc.User, active: int = 1):
        async with self.db() as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO players(tournament_id,user_id,display_name,active) VALUES(?,?,?,?)",
                (tid, member.id, getattr(member, "display_name", getattr(member, "name", str(member.id))), active),
            )
            await conn.commit()

    async def mark_drop(self, tid: int, user_id: int):
        async with self.db() as conn:
            await conn.execute(
                "UPDATE players SET active=0 WHERE tournament_id=? AND user_id=?",
                (tid, user_id),
            )
            await conn.commit()

    async def remove_player(self, tid: int, user_id: int):
        await self.mark_drop(tid, user_id)

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
                (tid, rno, "ongoing", int(time.time()))),
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
        """
        正確的 UPSERT：
        - 首次寫入時，INSERT 就帶入你提供的欄位值(不再全是 None)
        - 後續再次寫同一列，UPDATE 只改變你指定的欄位(pick1/pick2/actual)
        """
        # 只接受這三欄
        cols_allowed = ("pick1", "pick2", "actual")
        # INSERT 時要放入的值(沒給就 None)
        ins_pick1 = fields.get("pick1")
        ins_pick2 = fields.get("pick2")
        ins_actual = fields.get("actual")

        # UPDATE 子句與參數
        set_cols = [k for k in cols_allowed if k in fields]
        if not set_cols:
            # 什麼都沒要更新就提早結束
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
        """把玩家最後一次的選擇同步到 players 表(deck1, deck2, actual_class)。"""
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
            await self.cog.add_player(self.tid, interaction.user, active=1)
            await interaction.response.send_message("已加入報名。", ephemeral=True)

        @discord.ui.button(label="退出 Leave", style=discord.ButtonStyle.secondary, custom_id="swiss:leave")
        async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
            await self.cog.setup_db()
            await self.cog.remove_player(self.tid, interaction.user.id)
            await interaction.response.send_message("已退出報名。", ephemeral=True)

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
        """面板 2：贏家按此(公開公告『桌X：A 勝 B』；非臨時訊息)"""
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
            await self.cog.set_match_result(mid, res, pid, "WIN_BTN")
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

            # ✅ 檢查「實際職業」必須在已選的雙職業中
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

    class PanelView(discord.ui.View):
        """管理面板：集中所有 swiss 功能按鈕。"""
        def __init__(self, cog: 'SwissAll', tid: int):
            super().__init__(timeout=None)
            self.cog = cog
            self.tid = tid

        async def _adm(self, itx: discord.Interaction) -> bool:
            if itx.user.guild_permissions.manage_guild:
                return True
            await itx.response.send_message("需要管理伺服器權限。", ephemeral=True)
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
            await itx.response.send_message("已發送報名面板。", ephemeral=True)

        @discord.ui.button(label="我的成績(私訊)", style=discord.ButtonStyle.secondary, custom_id="swiss:mebtn")
        async def btn_me(self, itx: discord.Interaction, button: discord.ui.Button):
            await self.cog.ui_show_me(itx, self.tid, itx.user)

        @discord.ui.button(label="舉辦比賽(以今日日期)", style=discord.ButtonStyle.success, custom_id="swiss:newtoday")
        async def btn_new_today(self, itx: discord.Interaction, button: discord.ui.Button):
            if not await self._adm(itx): return
            name = dt.date.today().isoformat()
            tid2 = await self.cog.create_tournament(itx.guild.id, itx.user.id, name)
            await itx.channel.send(f"已建立 `{name}` (ID={tid2})。")
            await itx.channel.send("Swiss 管理面板：", view=self.cog.PanelView(self.cog, tid2))
            await itx.channel.send("報名/退出/退賽面板：", view=self.cog.RegView(self.cog, tid2))
            await itx.response.send_message("新賽事已建立並送出面板。", ephemeral=True)

    class BootView(discord.ui.View):
        """尚未建立賽事時顯示的前置面板。"""
        def __init__(self, cog: 'SwissAll', guild_id: int):
            super().__init__(timeout=None)
            self.cog = cog
            self.guild_id = guild_id

        async def _adm(self, itx: discord.Interaction) -> bool:
            if itx.user.guild_permissions.manage_guild:
                return True
            await itx.response.send_message("需要管理伺服器權限。", ephemeral=True)
            return False

        @discord.ui.button(label="舉辦比賽(以今日日期)", style=discord.ButtonStyle.success, custom_id="swiss:boot:create")
        async def create(self, itx: discord.Interaction, button: discord.ui.Button):
            if not await self._adm(itx): return
            name = dt.date.today().isoformat()
            tid = await self.cog.create_tournament(self.guild_id, itx.user.id, name)
            await itx.channel.send(f"已建立 `{name}` (ID={tid})。")
            await itx.channel.send("Swiss 管理面板：", view=self.cog.PanelView(self.cog, tid))
            await itx.channel.send("報名/退出/退賽面板：", view=self.cog.RegView(self.cog, tid))
            await itx.response.send_message("賽事建立完成。", ephemeral=True)

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
        created_msgs: List[Tuple[int,int,Optional[int],Optional[int]]] = []  # (mid, table, p1, p2)

        for p1, p2 in pairs:
            if p1 and p2:
                mid = await self.add_match(tid, rid, table, p1.id, p2.id)
                lines.append(f"桌 {table}: {p1.display_name} vs {p2.display_name} (match {mid})")
                created_msgs.append((mid, table, p1.id, p2.id))
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
            * MP = Pts
            * OppMW 用 0~1 浮點小數
        排序：Pts → OPPT1 → name（如需改成 Pts → OppMW，只要改 key 即可）
        """
        # 取玩家
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
            async with conn.execute(
                "SELECT id, tournament_id FROM rounds WHERE tournament_id=?",
                (tid,),
            ) as cur:
                _ = await cur.fetchall()  # 保留介面一致，實際不需回合資訊

        # 基本結構
        players = {
            r[0]: {
                "pid": r[0],
                "user_id": r[1],
                "name": r[2],
                "active": r[3],
                "Pts": float(r[4]),   # 已是 3 分制（由 update_score_for_match 確保）
                "wins": 0,
                "played": 0,
                "opp_pids": set(),   # 實際對手（不包含 BYE）
            }
            for r in prow
        }

        # 計 MWP 與對手集合
        for mid, rid, p1, p2, res, wpid in mrows:
            # 判斷 BYE（只有一邊有人）
            is_bye = (p1 is None) ^ (p2 is None)
            if res is None:
                continue  # 未回報不計
            if is_bye:
                # BYE：只有得 BYE 的那一位 +1 勝、played +1；不記對手
                winner = p1 if (p1 is not None and (res == "p1" or (res == "bye"))) else (
                        p2 if (p2 is not None and (res == "p2" or (res == "bye"))) else None)
                if winner in players:
                    players[winner]["wins"] += 1
                    players[winner]["played"] += 1
                # 另一邊其實不存在，不加入對手
                continue

            # 一般對局：雙方 played +1、勝者 wins +1、互相加入對手
            if p1 in players: players[p1]["played"] += 1
            if p2 in players: players[p2]["played"] += 1
            if wpid in players: players[wpid]["wins"] += 1
            if p1 in players and p2 in players:
                players[p1]["opp_pids"].add(p2)
                players[p2]["opp_pids"].add(p1)

        # 算每位 MWP（浮點小數）
        for p in players.values():
            p["MWP"] = (p["wins"] / p["played"]) if p["played"] > 0 else 0.0

        # 先算每位的 MWP 完成後，才能算 OppMW / SOS / SOSS
        # 方便取值
        def _pts(pid: int) -> float:
            return players[pid]["Pts"] if pid in players else 0.0

        def _mwp(pid: int) -> float:
            return players[pid]["MWP"] if pid in players else 0.0

        # OppMW, SOS, SOSS
        for p in players.values():
            opps = [players[op] for op in p["opp_pids"] if op in players]

            # OppMW：對手 MWP 平均
            if opps:
                p["OppMW"] = sum(_mwp(op["pid"]) for op in opps) / len(opps)
            else:
                p["OppMW"] = 0.0

            # SOS：對手 Pts 總和
            p["SOS"] = sum(_pts(op["pid"]) for op in opps)

            # SOSS：對手的對手 Pts 總和（不包含 p 自己）
            soss_sum = 0.0
            for op in opps:
                for op2 in op["opp_pids"]:
                    if op2 == p["pid"]:
                        continue
                    soss_sum += _pts(op2)
            p["SOSS"] = soss_sum

            # OPPT1：線性組合（OppMW 用 0~1 浮點）
            MP = p["Pts"]
            SOS = p["SOS"]
            SOSS = p["SOSS"]
            OMW = p["OppMW"]
            p["OPPT1"] = 0.26123 + 0.004312 * MP - 0.007638 * SOS + 0.003810 * SOSS + 0.23119 * OMW

        # 產出排序
        ordered = sorted(
            players.values(),
            key=lambda x: (-x["Pts"], -x["OppMW"], x["name"].lower()),
        )
        # 如果你要改成 Pts → OppMW，改 key 成：
        # key=lambda x: (-x["Pts"], -x["OppMW"], x["name"].lower()),

        # 組輸出列
        rows = []
        for pos, p in enumerate(ordered, 1):
            if active_only and not p["active"]:
                continue
            rows.append({
            # 供內部使用（相容既有程式）
            "rank": pos,
            "pid": p["pid"],
            "name": p["name"],
            # 用於輸出顯示
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

        # 只保留 5 欄：Pos/Player/Pts/MWP/OppMW/OPPT1
        headers = ["Pos", "Player", "Pts", "MWP", "OppMW", "OPPT1"]
        # 這裡預期 rows 內已有 r["OPPT1"] (float)；若尚未實作，先以 0.0 暫代
        # 這段替換 render_standings_image() 裡的 table 生成
        table = [
            [r["Pos"], r["Player"], r["Pts"], r["MWP"], r["OppMW"], round(r.get("OPPT1", 0.0), 4)]
            for r in rows
        ]


        try:
            import io
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
            tbl.auto_set_font_size(False)
            tbl.set_fontsize(9)
            tbl.scale(1, 1.2)
            for cell in tbl.get_celld().values():
                cell.get_text().set_fontproperties(fp)

            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=200, bbox_inches="tight")
            plt.close(fig)
            buf.seek(0)
            return discord.File(buf, filename="standings.png")

        except Exception:
            # 文字後備輸出也同步只顯示 5 欄
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

        # 關閉本回合 → 同步本輪職業到 players → 重算分數（包含本輪）
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
            await self.set_status(tid, "finished")
            # 決賽/季軍戰最終再同步一次（若玩家最後一刻才按完實際職業）
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

        # 瑞士輪的一般流程：發排名圖並詢問是否繼續
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

    async def cmd_start_round(self, itx_or_ctx, tid: int):
        await self.setup_db()
        await self._ack(itx_or_ctx, ephemeral=True)

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
        await self._ack(itx_or_ctx, ephemeral=True)

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

        # 重新計分以確保最新
        await self.recompute_scores(tid)

        standings = await self.compute_standings(tid, active_only=True)
        if len(standings) < 4:
            return await self._reply(itx_or_ctx, "需要至少 4 位有效選手才能建立決賽與季軍戰。")

        top4 = standings[:4]  # 依 T1 排序後的前四名

        # 建立「決賽」(1v2) 與「季軍戰」(3v4) 同一個 round
        rid = await self.create_round(tid)
        mf = await self.add_match(tid, rid, 1, top4[0]["pid"], top4[1]["pid"], notes="FINAL")
        m3 = await self.add_match(tid, rid, 2, top4[2]["pid"], top4[3]["pid"], notes="THIRD")

        # 進入 top4_finals 狀態
        await self.set_status(tid, "top4_finals")

        ch = itx_or_ctx.channel if isinstance(itx_or_ctx, (discord.Interaction, commands.Context)) else itx_or_ctx
        await ch.send(
            "已建立最終對局(依積分前四)：\n"
            f"決賽：{top4[0]['name']} vs {top4[1]['name']} (match {mf})\n"
            f"季軍戰：{top4[2]['name']} vs {top4[3]['name']} (match {m3})"
        )

        # ✅ 決賽輪同樣採用「每輪三則回報面板」
        await ch.send(
            "本輪回報面板(1/3)\n使用雙職業\n",
            view=self.RoundDeckView(self, tid, rid)
        )
        await ch.send(
            "本輪回報面板(2/3)\n勝者請點以下按鈕",
            view=self.RoundWinnerView(self, tid, rid)
        )
        await ch.send(
            "本輪回報面板(3/3)\n使用職業(不管輸贏都需要填寫)",
            view=self.RoundActualView(self, tid, rid)
        )

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

    # -------------- Commands --------------
    @commands.group(name="swiss", invoke_without_command=True)
    @commands.has_permissions(manage_guild=True)
    async def swiss_root(self, ctx: commands.Context):
        """顯示控制面板：若尚未建立賽事，顯示 Boot 面板(可一鍵舉辦比賽)。"""
        await self.setup_db()
        gid = ctx.guild.id
        tid = await self.guild_latest_tid(gid)
        if not tid:
            await ctx.send("Swiss 前置控制面板(尚未建立賽事)：", view=self.BootView(self, gid))
            return
        await ctx.send("Swiss 管理面板：", view=self.PanelView(self, tid))
        await ctx.send("報名/退出/退賽面板：", view=self.RegView(self, tid))

    @swiss_root.command(name="init")
    @commands.has_permissions(manage_guild=True)
    async def swiss_init(self, ctx: commands.Context, *, name: Optional[str] = None):
        await self.setup_db()
        tid = await self.create_tournament(ctx.guild.id, ctx.author.id, name or dt.date.today().isoformat())
        await ctx.send(f"已建立 `{dt.date.today().isoformat() if not name else name}` (ID={tid})。")
        await ctx.send("Swiss 管理面板：", view=self.PanelView(self, tid))
        await ctx.send("報名/退出/退賽面板：", view=self.RegView(self, tid))

    @swiss_root.command(name="start")
    @commands.has_permissions(manage_guild=True)
    async def swiss_start(self, ctx: commands.Context):
        tid = await self.guild_latest_tid(ctx.guild.id)
        if not tid:
            return await ctx.send("尚未建立賽事。")
        await self.cmd_start_round(ctx, tid)

    @swiss_root.command(name="next")
    @commands.has_permissions(manage_guild=True)
    async def swiss_next(self, ctx: commands.Context):
        tid = await self.guild_latest_tid(ctx.guild.id)
        if not tid:
            return await ctx.send("尚未建立賽事。")
        await self.cmd_next_round(ctx, tid)

    @swiss_root.command(name="top4")
    @commands.has_permissions(manage_guild=True)
    async def swiss_top4(self, ctx: commands.Context):
        tid = await self.guild_latest_tid(ctx.guild.id)
        if not tid:
            return await ctx.send("尚未建立賽事。")
        await self.cmd_make_top4(ctx, tid)

    @swiss_root.command(name="panel")
    @commands.has_permissions(manage_guild=True)
    async def swiss_panel(self, ctx: commands.Context):
        tid = await self.guild_latest_tid(ctx.guild.id)
        if not tid:
            return await ctx.send("Swiss 前置控制面板(尚未建立賽事)：", view=self.BootView(self, ctx.guild.id))
        await ctx.send("Swiss 管理面板：", view=self.PanelView(self, tid))

    @swiss_root.command(name="reg")
    async def swiss_reg(self, ctx: commands.Context):
        tid = await self.guild_latest_tid(ctx.guild.id)
        if not tid:
            return await ctx.send("尚未建立賽事。先建立或使用 `!swiss` 的前置面板。")
        await ctx.send("報名/退出/退賽面板：", view=self.RegView(self, tid))

    @swiss_root.command(name="drop")
    async def swiss_drop(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        tid = await self.guild_latest_tid(ctx.guild.id)
        if not tid:
            return await ctx.send("尚未建立賽事。")
        if member is None:
            member = ctx.author
        elif not ctx.author.guild_permissions.manage_guild and member.id != ctx.author.id:
            return await ctx.send("只有管理員能幫他人退賽。")
        await self.mark_drop(tid, member.id)
        await ctx.send(f"✅ 已將 {member.mention} 設為退賽(下輪不再配對)。")

    @swiss_root.command(name="standings")
    async def swiss_standings(self, ctx: commands.Context):
        tid = await self.guild_latest_tid(ctx.guild.id)
        if not tid: return await ctx.send("尚未建立賽事。")
        f = await self.render_standings_image(tid, ctx.channel)
        if f: await ctx.send(file=f)

    @swiss_root.group(name="test", invoke_without_command=True)
    @commands.has_permissions(manage_guild=True)
    async def swiss_test(self, ctx: commands.Context):
        await ctx.send(
            "測試工具：\n"
            "`!swiss test seed <N>`：加入 N 位測試玩家\n"
            "`!swiss test simulate`：為當前輪次的每桌自動：填兩職業(不重複)＋實際職業＋隨機決勝，並**公開公告**\n"
            "`!swiss test fillmeta`：只填兩職業與實際職業，不決勝(測 UI 與資料寫入)"
        )

    @swiss_test.command(name="seed")
    @commands.has_permissions(manage_guild=True)
    async def swiss_test_seed(self, ctx: commands.Context, n: int = 8):
        await self.setup_db()
        tid = await self.guild_latest_tid(ctx.guild.id)
        if not tid:
            return await ctx.send("尚未建立賽事。先 `!swiss` 並按「舉辦比賽」。")
        async with self.db() as conn:
            for i in range(n):
                fake_uid = 10_000_000 + random.randint(1, 9_999_999)
                name = f"測試玩家{str(i+1).zfill(2)}"
                await conn.execute(
                    "INSERT OR IGNORE INTO players(tournament_id,user_id,display_name,active) VALUES(?,?,?,1)",
                    (tid, fake_uid, name)
                )
            await conn.commit()
        await ctx.send(f"已加入 {n} 位測試玩家。")

    async def _test_fill_for_match(self, mid: int, p1: Optional[int], p2: Optional[int]):
        """測試輔助：為一桌的雙方填入 pick1/pick2(不重複) 與 actual，並同步到 players。"""
        async def _fill_player(pid: Optional[int]):
            if not pid:
                return
            # 隨機兩職業(不重複)
            picks = random.sample(CLASS_LABELS, 2)
            actual = random.choice(picks)
            await self._mpm_upsert(mid, pid, pick1=picks[0], pick2=picks[1], actual=actual)
            await self._player_set_decks_if_ready(pid, picks[0], picks[1], actual)

        await _fill_player(p1)
        await _fill_player(p2)

    @swiss_test.command(name="fillmeta")
    @commands.has_permissions(manage_guild=True)
    async def swiss_test_fillmeta(self, ctx: commands.Context):
        """只填職業資料，不決出勝負。"""
        tid = await self.guild_latest_tid(ctx.guild.id)
        if not tid:
            return await ctx.send("尚未建立賽事。")
        cur = await self.current_round(tid)
        if not cur:
            return await ctx.send("沒有進行中的輪次。請先開始一輪。")
        rid, rno, status = cur
        rows = await self.list_matches_round(rid)
        filled = 0
        for mid, tno, p1, p2, res, _ in rows:
            if p1 is None or p2 is None:
                continue
            await self._test_fill_for_match(mid, p1, p2)
            filled += 1
        await ctx.send(f"已為本輪 {filled} 桌填入兩職業與實際職業(不含 BYE 與已結束對局)。")

    @swiss_test.command(name="simulate")
    @commands.has_permissions(manage_guild=True)
    async def swiss_test_simulate(self, ctx: commands.Context):
        """
        為當前輪次所有尚未回報的非 BYE 對局：
        1) 自動填兩職業(不重複)與實際職業
        2) 隨機決出勝負，寫入分數
        3) **公開**貼出「桌 X：A 勝 B(match mid)」
        4) 最後檢查是否可結束本輪
        """
        tid = await self.guild_latest_tid(ctx.guild.id)
        if not tid:
            return await ctx.send("尚未建立賽事。")
        cur = await self.current_round(tid)
        if not cur:
            return await ctx.send("沒有進行中的輪次。")
        rid, rno, status = cur

        rows = await self.list_matches_round(rid)
        any_done = False
        for mid, tno, p1, p2, res, _ in rows:
            # 略過已回報或 BYE
            if res is not None or p1 is None or p2 is None:
                continue

            # 1) 填兩職業與實際職業
            await self._test_fill_for_match(mid, p1, p2)

            # 2) 隨機決勝
            winner_pid = p1 if random.random() < 0.5 else p2
            result = "p1" if winner_pid == p1 else "p2"
            await self.set_match_result(mid, result, winner_pid, "simulate")
            await self.update_score_for_match(tid, p1, p2, result, winner_pid)

            # 3) 公開公告(與 WinnerView 的文字一致)
            async with self.db() as conn:
                async with conn.execute("SELECT display_name FROM players WHERE id=?", (p1,)) as c1:
                    r1 = await c1.fetchone()
                async with conn.execute("SELECT display_name FROM players WHERE id=?", (p2,)) as c2:
                    r2 = await c2.fetchone()
            name1 = r1[0] if r1 else "?"
            name2 = r2[0] if r2 else "?"
            winner_name = name1 if result == "p1" else name2
            loser_name  = name2 if result == "p1" else name1
            await ctx.channel.send(f"桌 {tno}：{winner_name} 勝 {loser_name}(match {mid})")

            any_done = True

        if not any_done:
            return await ctx.send("沒有可模擬的對局(可能都是 BYE 或已回報)。")

        await ctx.send("已隨機完成回報並公告。檢查是否可結束本輪…")
        await self._maybe_on_round_complete(tid, rid, ctx.channel)


# ---------- setup ----------
async def setup(bot: commands.Bot):
    if bot.get_cog("SwissAll") is None:
        await bot.add_cog(SwissAll(bot))
