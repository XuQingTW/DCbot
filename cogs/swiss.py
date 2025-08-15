# -*- coding: utf-8 -*-
"""
Swiss Tournament (All-in-One, Updated with Boot Panel & Date-Naming)
- Registration / Swiss rounds / reporting / queries / auto Top4 with custom rule
- Discord UI buttons: join/leave/drop, report wins/AFK/concede, admin panel
- Round-complete workflow: render standings image and ask organizer to continue
- NEW: Boot panel when no tournament exists, with "Create Tournament" button
- NEW: Default tournament name = today's local date (server)

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
                    score REAL NOT NULL DEFAULT 0,
                    UNIQUE(tournament_id, user_id)
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
            # best-effort migration
            try:
                await conn.execute("ALTER TABLE tournaments ADD COLUMN organizer_id INTEGER")
            except Exception:
                pass
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
        async with self.db() as conn:
            if result == "p1" and p1_id:
                await conn.execute("UPDATE players SET score=score+1 WHERE id=?", (p1_id,))
            elif result == "p2" and p2_id:
                await conn.execute("UPDATE players SET score=score+1 WHERE id=?", (p2_id,))
            elif result == "bye":
                if p1_id and not p2_id:
                    await conn.execute("UPDATE players SET score=score+1 WHERE id=?", (p1_id,))
                if p2_id and not p1_id:
                    await conn.execute("UPDATE players SET score=score+1 WHERE id=?", (p2_id,))
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
            await interaction.response.send_message("已標記退賽（下一輪不再配對）。", ephemeral=True)

    class MatchView(discord.ui.View):
        def __init__(self, cog: 'SwissAll', tid: int, rid: int, match_id: int, p1_id: Optional[int], p2_id: Optional[int]):
            super().__init__(timeout=None)
            self.cog = cog
            self.tid = tid
            self.rid = rid
            self.mid = match_id
            self.p1 = p1_id
            self.p2 = p2_id

        async def _ensure_perm(self, interaction: discord.Interaction) -> bool:
            if interaction.user.guild_permissions.manage_guild:
                return True
            async with self.cog.db() as conn:
                async with conn.execute("SELECT user_id FROM players WHERE id IN (?,?)", (self.p1 or -1, self.p2 or -1)) as cur:
                    rows = await cur.fetchall()
                    user_ids = {r[0] for r in rows}
            return interaction.user.id in user_ids

        async def _finalize(self, interaction: discord.Interaction, result: str, winner_pid: Optional[int], note: Optional[str] = None):
            await self.cog.set_match_result(self.mid, result, winner_pid, note)
            await self.cog.update_score_for_match(self.tid, self.p1, self.p2, result, winner_pid)
            await interaction.response.send_message(
                f"已回報：{result.upper()}" + (f"（winner pid={winner_pid}）" if winner_pid else ""),
                ephemeral=True
            )
            await self.cog._maybe_on_round_complete(self.tid, self.rid, interaction.channel)

        @discord.ui.button(label="P1 勝", style=discord.ButtonStyle.primary, custom_id="swiss:p1")
        async def p1win(self, interaction: discord.Interaction, button: discord.ui.Button):
            if not await self._ensure_perm(interaction):
                return await interaction.response.send_message("你無權回報這桌。", ephemeral=True)
            await self._finalize(interaction, "p1", self.p1)

        @discord.ui.button(label="P2 勝", style=discord.ButtonStyle.primary, custom_id="swiss:p2")
        async def p2win(self, interaction: discord.Interaction, button: discord.ui.Button):
            if not await self._ensure_perm(interaction):
                return await interaction.response.send_message("你無權回報這桌。", ephemeral=True)
            await self._finalize(interaction, "p2", self.p2)

        @discord.ui.button(label="P1 AFK", style=discord.ButtonStyle.secondary, custom_id="swiss:afk1")
        async def afk1(self, interaction: discord.Interaction, button: discord.ui.Button):
            if not await self._ensure_perm(interaction):
                return await interaction.response.send_message("你無權回報這桌。", ephemeral=True)
            await self._finalize(interaction, "p2", self.p2, note="AFK P1")

        @discord.ui.button(label="P2 AFK", style=discord.ButtonStyle.secondary, custom_id="swiss:afk2")
        async def afk2(self, interaction: discord.Interaction, button: discord.ui.Button):
            if not await self._ensure_perm(interaction):
                return await interaction.response.send_message("你無權回報這桌。", ephemeral=True)
            await self._finalize(interaction, "p1", self.p1, note="AFK P2")

        @discord.ui.button(label="P1 棄權", style=discord.ButtonStyle.danger, custom_id="swiss:concede1")
        async def concede1(self, interaction: discord.Interaction, button: discord.ui.Button):
            if not await self._ensure_perm(interaction):
                return await interaction.response.send_message("你無權回報這桌。", ephemeral=True)
            await self._finalize(interaction, "p2", self.p2, note="Concede P1")

        @discord.ui.button(label="P2 棄權", style=discord.ButtonStyle.danger, custom_id="swiss:concede2")
        async def concede2(self, interaction: discord.Interaction, button: discord.ui.Button):
            if not await self._ensure_perm(interaction):
                return await interaction.response.send_message("你無權回報這桌。", ephemeral=True)
            await self._finalize(interaction, "p1", self.p1, note="Concede P2")

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

        @discord.ui.button(label="建立四強（手動）", style=discord.ButtonStyle.secondary, custom_id="swiss:top4manual")
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

        @discord.ui.button(label="建立四強（自/手動）", style=discord.ButtonStyle.secondary, custom_id="swiss:top4btn")
        async def btn_top4(self, itx: discord.Interaction, button: discord.ui.Button):
            if not await self._adm(itx): return
            await self.cog.cmd_make_top4(itx, self.tid)

        @discord.ui.button(label="顯示積分表", style=discord.ButtonStyle.secondary, custom_id="swiss:standbtn")
        async def btn_stand(self, itx: discord.Interaction, button: discord.ui.Button):
            if not await self._adm(itx): return
            f = await self.cog.render_standings_image(self.tid, itx.channel)
            if f: await itx.response.send_message(file=f)
            else: await itx.response.send_message("已輸出文字排名。", ephemeral=True)

        @discord.ui.button(label="發送報名面板", style=discord.ButtonStyle.secondary, custom_id="swiss:regpanel")
        async def btn_regpanel(self, itx: discord.Interaction, button: discord.ui.Button):
            if not await self._adm(itx): return
            await itx.channel.send("報名/退出/退賽面板：", view=self.cog.RegView(self.cog, self.tid))
            await itx.response.send_message("已發送報名面板。", ephemeral=True)

        @discord.ui.button(label="我的成績（私訊）", style=discord.ButtonStyle.secondary, custom_id="swiss:mebtn")
        async def btn_me(self, itx: discord.Interaction, button: discord.ui.Button):
            await self.cog.ui_show_me(itx, self.tid, itx.user)

        @discord.ui.button(label="舉辦比賽（以今日日期）", style=discord.ButtonStyle.success, custom_id="swiss:newtoday")
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

        @discord.ui.button(label="舉辦比賽（以今日日期）", style=discord.ButtonStyle.success, custom_id="swiss:boot:create")
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
            await channel.send("❌ 選手不足（至少需要 2 人）。")
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

        for row in await self.list_matches_round(rid):
            mid, tno, p1, p2, res, _ = row
            if res == "bye":
                continue
            async with self.db() as conn:
                async with conn.execute("SELECT display_name FROM players WHERE id=?", (p1,)) as c1:
                    r1 = await c1.fetchone()
                async with conn.execute("SELECT display_name FROM players WHERE id=?", (p2,)) as c2:
                    r2 = await c2.fetchone()
            name1 = r1[0] if r1 else "?"
            name2 = r2[0] if r2 else "?"
            await channel.send(
                f"桌 {tno}: {name1} vs {name2}\n按鈕回報此桌結果 (match {mid})",
                view=self.MatchView(self, tid, rid, mid, p1, p2),
            )

    # -------------- Standings & tiebreaks --------------
    async def compute_standings(self, tid: int, active_only=True):
        async with self.db() as conn:
            async with conn.execute(
                "SELECT id,user_id,display_name,active,score FROM players WHERE tournament_id=?"
                + (" AND active=1" if active_only else ""),
                (tid,),
            ) as cur:
                prow = await cur.fetchall()
            async with conn.execute(
                "SELECT round_id, p1_id, p2_id, result, winner_player_id "
                "FROM matches WHERE tournament_id=?",
                (tid,),
            ) as cur:
                mrows = await cur.fetchall()

        players = {r[0]: {"pid": r[0], "user_id": r[1], "name": r[2], "active": r[3], "Pts": float(r[4])} for r in prow}
        for p in players.values():
            p.update({"wins": 0, "played": 0, "opp_pids": set()})

        for _, p1, p2, res, wpid in mrows:
            if res is None:
                continue
            if res == "bye":
                wp = players.get(wpid)
                if wp:
                    wp["wins"] += 1
                    wp["played"] += 1
                continue
            if p1 in players: players[p1]["played"] += 1
            if p2 in players: players[p2]["played"] += 1
            if wpid in players: players[wpid]["wins"] += 1
            if p1 in players and p2 in players:
                players[p1]["opp_pids"].add(p2)
                players[p2]["opp_pids"].add(p1)

        for p in players.values():
            p["MWP"] = (p["wins"] / p["played"]) if p["played"] > 0 else 0.0

        def avg(values: List[float]) -> float:
            return sum(values) / len(values) if values else 0.0

        for p in players.values():
            opps = [players[op] for op in p["opp_pids"] if op in players]
            p["OppMW"] = avg([op["MWP"] for op in opps])
            p["Opp"] = sum([op["Pts"] for op in opps])
            opp_opp_pts: List[float] = []
            for op in opps:
                o2 = [players[x]["Pts"] for x in op["opp_pids"] if x in players and x != p["pid"]]
                opp_opp_pts.extend(o2)
            p["OppOppAvgPts"] = avg(opp_opp_pts)
            opp_pts_avg = avg([players[op]["Pts"] for op in p["opp_pids"]])
            p["T1"] = (p["Pts"], opp_pts_avg, p["OppOppAvgPts"])

        ordered = sorted(
            players.values(),
            key=lambda x: (-x["T1"][0], -x["T1"][1], -x["T1"][2], x["name"].lower()),
        )
        rows = []
        for rank, p in enumerate(ordered, 1):
            rows.append({
                "rank": rank,
                "pid": p["pid"],
                "name": p["name"],
                "Pts": round(p["Pts"], 3),
                "MWP": round(p["MWP"], 4),
                "OppMW": round(p["OppMW"], 4),
                "Opp": round(p["Opp"], 3),
                "T1": f"{p['T1'][0]:.0f} / {p['T1'][1]:.3f} / {p['T1'][2]:.3f}",
                "played": p["played"],
                "wins": p["wins"],
                "active": p["active"],
            })
        return rows

    async def render_standings_image(self, tid: int, channel: discord.abc.Messageable) -> Optional[discord.File]:
        rows = await self.compute_standings(tid, active_only=False)
        headers = ["#", "選手", "Pts", "MWP", "OppMW", "Opp", "T1"]
        table = [[r["rank"], r["name"], r["Pts"], r["MWP"], r["OppMW"], r["Opp"], r["T1"]] for r in rows]
        try:
            import matplotlib.pyplot as plt
            fig, ax = plt.subplots(figsize=(10, min(0.6 * max(4, len(table)), 20)))
            ax.axis("off")
            tbl = ax.table(cellText=table, colLabels=headers, cellLoc="center", loc="upper left")
            tbl.auto_set_font_size(False)
            tbl.set_fontsize(9)
            tbl.scale(1, 1.2)
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

    # -------------- Auto Top4 rule --------------
    async def _auto_top4_if_condition(self, itx_or_channel, tid: int) -> bool:
        players = await self.fetch_players(tid, active_only=True)
        if len(players) < 4:
            return False

        async with self.db() as conn:
            async with conn.execute(
                "SELECT p1_id,p2_id,result,winner_player_id FROM matches WHERE tournament_id=?",
                (tid,),
            ) as cur:
                mrows = await cur.fetchall()

        played = {p.id: 0 for p in players}
        wins = {p.id: 0 for p in players}
        for p1, p2, res, wpid in mrows:
            if res is None:
                continue
            if res == "bye":
                if wpid in wins: wins[wpid] += 1
                if wpid in played: played[wpid] += 1
                continue
            if p1 in played: played[p1] += 1
            if p2 in played: played[p2] += 1
            if wpid in wins: wins[wpid] += 1

        losses = {pid: max(0, played.get(pid, 0) - wins.get(pid, 0)) for pid in played}
        undefeated = [p for p in players if losses.get(p.id, 0) == 0 and played.get(p.id, 0) > 0]
        one_loss = [p for p in players if losses.get(p.id, 0) == 1]

        if len(undefeated) == 2 and len(one_loss) == 2:
            rid = await self.create_round(tid)
            undefeated.sort(key=lambda x: x.display_name.lower())
            one_loss.sort(key=lambda x: x.display_name.lower())
            m1 = await self.add_match(tid, rid, 1, undefeated[0].id, undefeated[1].id)
            m2 = await self.add_match(tid, rid, 2, one_loss[0].id, one_loss[1].id)
            await self.set_status(tid, "top4")
            ch = itx_or_channel.channel if isinstance(itx_or_channel, (discord.Interaction, commands.Context)) else itx_or_channel
            await ch.send(
                f"✅ 自動四強條件達成：2 全勝 + 2 一敗。\n"
                f"冠亞戰（全勝）：{undefeated[0].display_name} vs {undefeated[1].display_name} (match {m1})\n"
                f"一敗組：{one_loss[0].display_name} vs {one_loss[1].display_name} (match {m2})"
            )
            await ch.send(f"冠亞戰按鈕 (match {m1})",
                          view=self.MatchView(self, tid, rid, m1, undefeated[0].id, undefeated[1].id))
            await ch.send(f"一敗組按鈕 (match {m2})",
                          view=self.MatchView(self, tid, rid, m2, one_loss[0].id, one_loss[1].id))
            return True
        return False

    # -------------- Round complete hook --------------
    async def _maybe_on_round_complete(self, tid: int, rid: int, channel: discord.abc.Messageable):
        rows = await self.list_matches_round(rid)
        if any(r[4] is None for r in rows):
            return

        await self.close_round(rid)
        await self.recompute_scores(tid)

        file = await self.render_standings_image(tid, channel)
        org_id = await self.get_organizer(tid)
        mention = f"<@{org_id}>" if org_id else "主辦者"

        if await self._auto_top4_if_condition(channel, tid):
            if file:
                await channel.send(content=f"{mention} 本輪已結束，且已自動建立四強。", file=file)
            else:
                await channel.send(f"{mention} 本輪已結束，且已自動建立四強。")
            return

        if file:
            await channel.send(content=f"{mention} 本輪已結束。是否前往下一輪？", file=file, view=self.NextStepView(self, tid))
        else:
            await channel.send(f"{mention} 本輪已結束。是否前往下一輪？", view=self.NextStepView(self, tid))

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
        status = await self.tour_status(tid)
        if status not in ("register", "seeding"):
            return await self._reply(itx_or_ctx, "目前狀態不允許開始新一輪。")
        players = await self.fetch_players(tid, active_only=True)
        if len(players) < 2:
            return await self._reply(itx_or_ctx, "❌ 選手不足（至少需要 2 人）。")
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
            return await self._reply(itx_or_ctx, "❌ 選手不足（至少需要 2 人）。")
        rid = await self.create_round(tid)
        ch = itx_or_ctx.channel if isinstance(itx_or_ctx, (discord.Interaction, commands.Context)) else itx_or_ctx
        await self._pair_and_post(ch, tid, rid)
        await self._reply(itx_or_ctx, "下一輪已建立。")

    async def cmd_make_top4(self, itx_or_ctx, tid: int):
        await self.setup_db()
        if await self._auto_top4_if_condition(itx_or_ctx, tid):
            return
        players = await self.fetch_players(tid, active_only=True)
        if len(players) != 4:
            return await self._reply(itx_or_ctx, "需要剛好 4 位選手（或達到 2 全勝 + 2 一敗條件）。")
        players.sort(key=lambda p: (-p.score, p.display_name.lower()))
        rid = await self.create_round(tid)
        m1 = await self.add_match(tid, rid, 1, players[0].id, players[3].id)
        m2 = await self.add_match(tid, rid, 2, players[1].id, players[2].id)
        await self.set_status(tid, "top4")
        ch = itx_or_ctx.channel if isinstance(itx_or_ctx, (discord.Interaction, commands.Context)) else itx_or_ctx
        await ch.send(
            f"四強（一般規則）：\n桌1: {players[0].display_name} vs {players[3].display_name} (match {m1})\n"
            f"桌2: {players[1].display_name} vs {players[2].display_name} (match {m2})"
        )
        await ch.send(f"桌1 回報按鈕 (match {m1})", view=self.MatchView(self, tid, rid, m1, players[0].id, players[3].id))
        await ch.send(f"桌2 回報按鈕 (match {m2})", view=self.MatchView(self, tid, rid, m2, players[1].id, players[2].id))
        await self._reply(itx_or_ctx, "四強建立完成。")

    async def ui_show_me(self, itx: discord.Interaction, tid: int, member: discord.Member):
        async with self.db() as conn:
            async with conn.execute(
                "SELECT id,display_name,score FROM players WHERE tournament_id=? AND user_id=?",
                (tid, member.id),
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return await itx.response.send_message("你不在本賽事名單中。", ephemeral=True)
        pid, dname, score = row
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
        lines = [f"{dname} 的成績（總分 {score}）：", *details]
        for ck in chunk_text("\n".join(lines)):
            await itx.response.send_message(ck, ephemeral=True)
            break

    # -------------- Commands --------------
    @commands.group(name="swiss", invoke_without_command=True)
    @commands.has_permissions(manage_guild=True)
    async def swiss_root(self, ctx: commands.Context):
        """顯示控制面板：若尚未建立賽事，顯示 Boot 面板（可一鍵舉辦比賽）。"""
        await self.setup_db()
        gid = ctx.guild.id
        tid = await self.guild_latest_tid(gid)
        if not tid:
            await ctx.send("Swiss 前置控制面板（尚未建立賽事）：", view=self.BootView(self, gid))
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
            return await ctx.send("Swiss 前置控制面板（尚未建立賽事）：", view=self.BootView(self, ctx.guild.id))
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
        await ctx.send(f"✅ 已將 {member.mention} 設為退賽（下輪不再配對）。")

    @swiss_root.command(name="standings")
    async def swiss_standings(self, ctx: commands.Context):
        tid = await self.guild_latest_tid(ctx.guild.id)
        if not tid: return await ctx.send("尚未建立賽事。")
        f = await self.render_standings_image(tid, ctx.channel)
        if f: await ctx.send(file=f)

    @swiss_root.group(name="test", invoke_without_command=True)
    @commands.has_permissions(manage_guild=True)
    async def swiss_test(self, ctx: commands.Context):
        await ctx.send("測試工具：`!swiss test seed <N>`、`!swiss test simulate`。")

    @swiss_test.command(name="seed")
    @commands.has_permissions(manage_guild=True)
    async def swiss_test_seed(self, ctx: commands.Context, n: int = 8):
        await self.setup_db()
        tid = await self.guild_latest_tid(ctx.guild.id)
        if not tid: return await ctx.send("尚未建立賽事。先 `!swiss` 並按「舉辦比賽」。")
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

    @swiss_test.command(name="simulate")
    @commands.has_permissions(manage_guild=True)
    async def swiss_test_simulate(self, ctx: commands.Context):
        tid = await self.guild_latest_tid(ctx.guild.id)
        if not tid: return await ctx.send("尚未建立賽事。")
        cur = await self.current_round(tid)
        if not cur: return await ctx.send("沒有進行中的輪次。")
        rid, rno, status = cur
        rows = await self.list_matches_round(rid)
        any_done = False
        for mid, tno, p1, p2, res, _ in rows:
            if res is not None: continue
            if p1 is None or p2 is None: continue
            w = p1 if random.random() < 0.5 else p2
            result = "p1" if w == p1 else "p2"
            await self.set_match_result(mid, result, w, "simulate")
            await self.update_score_for_match(tid, p1, p2, result, w)
            any_done = True
        if not any_done:
            return await ctx.send("沒有可隨機的對局。")
        await ctx.send("已隨機結束當輪未回報對局。")
        await self._maybe_on_round_complete(tid, rid, ctx.channel)

# ---------- setup ----------
async def setup(bot: commands.Bot):
    if bot.get_cog("SwissAll") is None:
        await bot.add_cog(SwissAll(bot))
