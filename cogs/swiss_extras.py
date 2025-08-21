# -*- coding: utf-8 -*-
"""
Swiss Extras (Archive, Profile & Stats Panel - Dynamic Seasons)
- Owner/admin archive & export management via buttons
- Players: my aggregate, recent matches, today's result
- Stats panel: overall class WR / class-vs-class / my-class (scope: server/self; metric: overall/per-tournament)
- Seasons are dynamic (CRUD via UI): seasons table with slug/name/start_ts/end_ts
- Reads match_player_meta.actual (from SwissAll) + rounds/tournaments time
- No name conflicts with SwissAll; custom_ids prefixed with "swissx:"
"""

from __future__ import annotations
import asyncio
import json
import io
import time
import datetime as dt
from typing import Optional, List, Tuple, Dict, Any

import aiosqlite
import discord
from discord.ext import commands

DB_PATH = "swiss.db"  # must match SwissAll
CLASS_LABELS = ["精靈", "皇家", "巫師", "龍族", "夜魔", "主教", "復仇者"]


# ---------- tiny utils ----------
def _ts_of_date(s: str, end_of_day=False) -> Optional[int]:
    s = (s or "").strip()
    if not s:
        return None
    try:
        y, m, d = [int(x) for x in s.split("-")]
        t = dt.datetime(y, m, d, 23, 59, 59) if end_of_day else dt.datetime(y, m, d, 0, 0, 0)
        return int(t.timestamp())
    except Exception:
        return None

def _fmt_pct(x: float) -> str:
    return f"{x*100:.2f}%"


# ---------- Cog ----------
class SwissExtras(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._ready = False
        self._lock = asyncio.Lock()

    # ---------- common helpers ----------
    def db(self):
        return aiosqlite.connect(DB_PATH)

    async def _send_ephemeral(self, itx: discord.Interaction, content: Optional[str] = None, **kwargs):
        if not itx.response.is_done():
            await itx.response.send_message(content, ephemeral=True, **kwargs)
        else:
            await itx.followup.send(content, ephemeral=True, **kwargs)

    # ---------- DB setup (base tables + dynamic seasons) ----------
    async def setup_db(self):
        if self._ready:
            return
        async with self.db() as conn:
            # base (mirror minimal SwissAll tables to avoid import order issues)
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS tournaments(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'init',
                reg_message_id INTEGER,
                organizer_id INTEGER,
                created_at INTEGER NOT NULL,
                finished_at INTEGER,
                archived INTEGER NOT NULL DEFAULT 0
            );""")
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS rounds(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tournament_id INTEGER NOT NULL,
                round_no INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'ongoing',
                created_at INTEGER NOT NULL
            );""")
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS matches(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tournament_id INTEGER NOT NULL,
                round_id INTEGER NOT NULL,
                table_no INTEGER NOT NULL,
                p1_id INTEGER,
                p2_id INTEGER,
                result TEXT,                 -- 'p1','p2','bye','void'
                winner_player_id INTEGER,
                notes TEXT
            );""")
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS players(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tournament_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                score REAL NOT NULL DEFAULT 0
            );""")
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS match_player_meta(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER NOT NULL,
                player_id INTEGER NOT NULL,      -- players.id
                pick1 TEXT,
                pick2 TEXT,
                actual TEXT,
                UNIQUE(match_id, player_id)
            );""")

            # ---- seasons table (dynamic) ----
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS seasons (
                slug TEXT PRIMARY KEY,            -- 例：s2, s3, evo2025
                name TEXT NOT NULL,               -- 顯示名稱
                start_ts INTEGER NOT NULL,        -- 起始(含)
                end_ts   INTEGER                  -- 結束(含)；NULL 表示開放式
            )""")
            
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS seasons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                slug TEXT UNIQUE NOT NULL
            )""")


            # seed defaults only when empty
            async with conn.execute("SELECT COUNT(*) FROM seasons") as c:
                (cnt,) = await c.fetchone()
            if cnt == 0:
                s2_end = int(dt.datetime(2025, 8, 27, 23, 59, 59).timestamp())
                s3_start = int(dt.datetime(2025, 8, 28, 0, 0, 0).timestamp())
                await conn.execute(
                    "INSERT INTO seasons(slug,name,start_ts,end_ts) VALUES(?,?,?,?)",
                    ("s2", "第二期（無限進化）", 0, s2_end)
                )
                await conn.execute(
                    "INSERT INTO seasons(slug,name,start_ts,end_ts) VALUES(?,?,?,NULL)",
                    ("s3", "第三期", s3_start)
                )

            await conn.commit()
        self._ready = True

    # ---------- seasons helpers ----------
    async def seasons_all(self) -> List[dict]:
        async with self.db() as conn:
            async with conn.execute(
                "SELECT slug,name,start_ts,end_ts FROM seasons ORDER BY start_ts"
            ) as c:
                rows = await c.fetchall()
        out = []
        for slug, name, s, e in rows:
            out.append({"slug": slug, "name": name, "start": int(s), "end": (None if e is None else int(e))})
        return out

    async def season_by_slug(self, slug: str) -> Optional[dict]:
        async with self.db() as conn:
            async with conn.execute(
                "SELECT slug,name,start_ts,end_ts FROM seasons WHERE slug=?",
                (slug,)
            ) as c:
                r = await c.fetchone()
        return None if not r else {"slug": r[0], "name": r[1], "start": int(r[2]), "end": (None if r[3] is None else int(r[3]))}

    def _season_bounds(self, season: Optional[dict]) -> tuple[int, int]:
        """回傳 (start_ts, end_ts_inclusive)。season=None 表示不限制。"""
        if not season:
            return (0, 32503680000)  # ~ year 3000
        start = int(season["start"])
        end   = int(season["end"]) if season["end"] is not None else 32503680000
        return (start, end)

    async def seasons_upsert(self, slug: str, name: str, start_ts: int, end_ts: Optional[int]):
        async with self.db() as conn:
            await conn.execute(
                "INSERT INTO seasons(slug,name,start_ts,end_ts) VALUES(?,?,?,?) "
                "ON CONFLICT(slug) DO UPDATE SET name=excluded.name, start_ts=excluded.start_ts, end_ts=excluded.end_ts",
                (slug, name, start_ts, end_ts)
            )
            await conn.commit()

    async def seasons_delete(self, slug: str) -> bool:
        async with self.db() as conn:
            await conn.execute("DELETE FROM seasons WHERE slug=?", (slug,))
            await conn.commit()
        return True

    # ---------- common lookups ----------
    async def guild_latest_tid(self, guild_id: int) -> Optional[int]:
        async with self.db() as conn:
            async with conn.execute(
                "SELECT id FROM tournaments WHERE guild_id=? ORDER BY id DESC LIMIT 1",
                (guild_id,)
            ) as cur:
                r = await cur.fetchone()
                return r[0] if r else None

    async def get_organizer(self, tid: int) -> Optional[int]:
        async with self.db() as conn:
            async with conn.execute("SELECT organizer_id FROM tournaments WHERE id=?", (tid,)) as cur:
                r = await cur.fetchone()
                return r[0] if r else None

    # ---------- archive / export ops ----------
    async def archive_current(self, guild_id: int) -> Optional[int]:
        tid = await self.guild_latest_tid(guild_id)
        if not tid:
            return None
        async with self.db() as conn:
            await conn.execute(
                "UPDATE tournaments SET archived=1, finished_at=? WHERE id=?",
                (int(time.time()), tid)
            )
            await conn.commit()
        return tid

    async def export_tournament_json(self, tid: int) -> Optional[discord.File]:
        dump: Dict[str, Any] = {}
        async with self.db() as conn:
            async with conn.execute(
                "SELECT id,guild_id,name,status,organizer_id,created_at,finished_at,archived "
                "FROM tournaments WHERE id=?",
                (tid,)
            ) as cur:
                t = await cur.fetchone()
            if not t:
                return None
            dump["tournament"] = {
                "id": t[0], "guild_id": t[1], "name": t[2], "status": t[3],
                "organizer_id": t[4], "created_at": t[5],
                "finished_at": t[6], "archived": t[7]
            }
            async with conn.execute(
                "SELECT id,tournament_id,user_id,display_name,active,score FROM players WHERE tournament_id=?",
                (tid,)
            ) as cur:
                dump["players"] = [
                    {"id": r[0], "tournament_id": r[1], "user_id": r[2], "display_name": r[3],
                     "active": r[4], "score": float(r[5])}
                    for r in await cur.fetchall()
                ]
            async with conn.execute(
                "SELECT id,tournament_id,round_no,status,created_at FROM rounds WHERE tournament_id=? ORDER BY round_no",
                (tid,)
            ) as cur:
                dump["rounds"] = [
                    {"id": r[0], "tournament_id": r[1], "round_no": r[2], "status": r[3], "created_at": r[4]}
                    for r in await cur.fetchall()
                ]
            async with conn.execute(
                "SELECT id,tournament_id,round_id,table_no,p1_id,p2_id,result,winner_player_id,notes "
                "FROM matches WHERE tournament_id=? ORDER BY round_id, table_no",
                (tid,)
            ) as cur:
                dump["matches"] = [
                    {"id": r[0], "tournament_id": r[1], "round_id": r[2], "table_no": r[3],
                     "p1_id": r[4], "p2_id": r[5], "result": r[6], "winner_player_id": r[7], "notes": r[8]}
                    for r in await cur.fetchall()
                ]
        buf = io.BytesIO(json.dumps(dump, ensure_ascii=False, indent=2).encode("utf-8"))
        return discord.File(buf, filename=f"tournament_{tid}.json")

    # ---------- user stats & history ----------
    async def _user_pids(self, guild_id: int, user_id: int) -> List[int]:
        async with self.db() as conn:
            async with conn.execute(
                "SELECT p.id FROM players p JOIN tournaments t ON p.tournament_id=t.id "
                "WHERE t.guild_id=? AND p.user_id=?",
                (guild_id, user_id)
            ) as cur:
                return [r[0] for r in await cur.fetchall()]

    async def compute_user_aggregate(self, guild_id: int, user_id: int):
        async with self.db() as conn:
            async with conn.execute(
                "SELECT p.id FROM players p JOIN tournaments t ON p.tournament_id=t.id WHERE t.guild_id=? AND p.user_id=?",
                (guild_id, user_id)
            ) as cur:
                pids = [r[0] for r in await cur.fetchall()]
            if not pids:
                return {"played": 0, "wins": 0, "losses": 0, "winrate": 0.0}

            q = ("SELECT m.p1_id, m.p2_id, m.result, m.winner_player_id FROM matches m "
                 f"WHERE m.p1_id IN ({','.join('?'*len(pids))}) OR m.p2_id IN ({','.join('?'*len(pids))})")
            async with conn.execute(q, (*pids, *pids)) as cur:
                rows = await cur.fetchall()

        wins = losses = 0
        for p1, p2, res, wpid in rows:
            if res not in ("p1", "p2"):
                continue
            if wpid in pids:
                wins += 1
            else:
                losses += 1
        played = wins + losses
        wr = (wins / played) if played else 0.0
        return {"played": played, "wins": wins, "losses": losses, "winrate": round(wr, 4)}

    async def fetch_user_recent_matches(self, guild_id: int, user_id: int, limit: int = 20):
        async with self.db() as conn:
            async with conn.execute(
                "SELECT p.id FROM players p JOIN tournaments t ON p.tournament_id=t.id "
                "WHERE t.guild_id=? AND p.user_id=?", (guild_id, user_id)
            ) as cur:
                pids = [r[0] for r in await cur.fetchall()]
            if not pids:
                return []

            q = (
                "SELECT m.id, t.name, r.round_no, m.table_no, m.p1_id, m.p2_id, "
                "m.result, m.winner_player_id, r.created_at "
                "FROM matches m "
                "JOIN rounds r ON m.round_id=r.id "
                "JOIN tournaments t ON t.id=r.tournament_id "
               f"WHERE m.p1_id IN ({','.join('?'*len(pids))}) OR m.p2_id IN ({','.join('?'*len(pids))}) "
                "ORDER BY r.created_at DESC, r.round_no DESC, m.table_no ASC LIMIT ?"
            )
            async with conn.execute(q, (*pids, *pids, limit)) as cur:
                rows = await cur.fetchall()

            # map pid -> name
            pid2name: Dict[int, str] = {}
            uniq: set[int] = set()
            for r in rows:
                p1 = r[4]; p2 = r[5]
                if p1 is not None: uniq.add(p1)
                if p2 is not None: uniq.add(p2)
            if uniq:
                placeholders = ",".join("?" for _ in uniq)
                async with conn.execute(
                    f"SELECT id, display_name FROM players WHERE id IN ({placeholders})",
                    tuple(uniq)
                ) as cur:
                    for pid, name in await cur.fetchall():
                        pid2name[pid] = name

        out = []
        for mid, tname, rno, tno, p1, p2, res, wpid, rts in rows:
            if p1 in pids:
                opp = pid2name.get(p2, "BYE" if p2 is None else str(p2))
            else:
                opp = pid2name.get(p1, "BYE" if p1 is None else str(p1))
            if res == "bye":
                outcome = "Win (BYE)"
            elif res in ("p1", "p2"):
                outcome = "Win" if (wpid in pids) else "Loss"
            else:
                outcome = res or "?"
            out.append({
                "match_id": mid,
                "tournament": tname,
                "round": rno,
                "table": tno,
                "opponent": opp,
                "outcome": outcome,
                "ts": rts
            })
        return out

    async def fetch_today_results(self, guild_id: int, user_id: int):
        today = dt.date.today()
        start = int(dt.datetime.combine(today, dt.time.min).timestamp())
        end   = int(dt.datetime.combine(today, dt.time.max).timestamp())
        results = []

        # 取今天建立的賽事
        async with self.db() as conn:
            async with conn.execute(
                "SELECT id,name,status FROM tournaments "
                "WHERE guild_id=? AND created_at BETWEEN ? AND ? ORDER BY id DESC",
                (guild_id, start, end)
            ) as cur:
                tours = await cur.fetchall()

        # 嘗試拿 SwissAll 的計算（精準名次）
        swiss = self.bot.get_cog("SwissAll")

        for tid, name, status in tours:
            # 找出「我」在該賽事對應的 players.id 與目前分數
            async with self.db() as conn:
                async with conn.execute(
                    "SELECT id, score FROM players WHERE tournament_id=? AND user_id=?",
                    (tid, user_id)
                ) as cur:
                    me = await cur.fetchone()
            if not me:
                continue
            my_pid, my_score = me

            # 計算今天在該賽事的 W/L（排除 BYE）
            async with self.db() as conn:
                async with conn.execute(
                    "SELECT p1_id,p2_id,result,winner_player_id "
                    "FROM matches WHERE tournament_id=? AND (p1_id=? OR p2_id=?)",
                    (tid, my_pid, my_pid)
                ) as cur:
                    mrows = await cur.fetchall()
            w = l = 0
            for p1, p2, res, wpid in mrows:
                if res not in ("p1", "p2"):
                    continue
                if wpid == my_pid:
                    w += 1
                else:
                    l += 1

            # 預設參賽人數
            async with self.db() as conn:
                async with conn.execute(
                    "SELECT COUNT(1) FROM players WHERE tournament_id=?",
                    (tid,)
                ) as cur:
                    (participants,) = await cur.fetchone()

            # === 關鍵：用 SwissAll 的 standings 取得精準名次（若可用）===
            my_rank = None
            if swiss and hasattr(swiss, "compute_standings"):
                try:
                    # 先確保分數一致（SwissAll 的 recompute 會用 matches 重算）
                    if hasattr(swiss, "recompute_scores"):
                        await swiss.recompute_scores(tid)

                    rows = await swiss.compute_standings(tid, active_only=False)
                    # rows: [{"rank":1,"pid":...,"name":...,"Pts":...,"T1":"..."...}, ...]
                    for r in rows:
                        if r.get("pid") == my_pid:
                            my_rank = r.get("rank")
                            break
                except Exception:
                    # 若計算失敗，fallback 到舊的估算
                    my_rank = None

            # 若沒 SwissAll 或取不到，fallback：分數降序 + 名字排序
            if my_rank is None:
                async with self.db() as conn:
                    async with conn.execute(
                        "SELECT id, display_name, score FROM players WHERE tournament_id=? "
                        "ORDER BY score DESC, display_name COLLATE NOCASE ASC",
                        (tid,)
                    ) as cur:
                        ordered = await cur.fetchall()
                for i, (pid, _nm, sc) in enumerate(ordered, 1):
                    if pid == my_pid:
                        my_rank = i
                        break

            results.append({
                "tid": tid,
                "name": name,
                "status": status,
                "wins": w,
                "losses": l,
                "score": float(my_score),
                "participants": participants,
                "rank": my_rank  # ← 現在是精準名次（可 fallback）
            })
        return results


    # ---------- core stats (season-aware) ----------
    async def _class_stats(
        self,
        guild_id: int,
        class_a: str,
        class_b: Optional[str],          # None = 對所有職業
        scope: str,                      # "all" or "self"
        user_id: Optional[int],
        metric: str,                     # "overall" or "per_tournament"
        period: str                      # "all" or a slug in seasons
    ) -> Dict[str, float]:

        season = None if period == "all" else await self.season_by_slug(period)
        start_ts, end_ts = self._season_bounds(season)

        # tournaments in range
        async with self.db() as conn:
            async with conn.execute(
                "SELECT id FROM tournaments WHERE guild_id=? AND created_at BETWEEN ? AND ?",
                (guild_id, start_ts, end_ts)
            ) as c:
                tids = [r[0] for r in await c.fetchall()]
        if not tids:
            return {"matches": 0, "wins": 0, "losses": 0, "wr": 0.0, "avg_wr": 0.0}

        rows = []
        async with self.db() as conn:
            my_pids: set[int] = set()
            if scope == "self" and user_id is not None:
                async with conn.execute(
                    "SELECT p.id FROM players p JOIN tournaments t ON p.tournament_id=t.id "
                    "WHERE t.guild_id=? AND p.user_id=?",
                    (guild_id, user_id)
                ) as cur:
                    my_pids = {r[0] for r in await cur.fetchall()}

            q = (
                "SELECT m.id, t.id as tid, m.p1_id, m.p2_id, m.winner_player_id, "
                "mp1.actual as c1, mp2.actual as c2 "
                "FROM matches m "
                "JOIN rounds r ON r.id = m.round_id "
                "JOIN tournaments t ON t.id = m.tournament_id "
                "LEFT JOIN match_player_meta mp1 ON mp1.match_id = m.id AND mp1.player_id = m.p1_id "
                "LEFT JOIN match_player_meta mp2 ON mp2.match_id = m.id AND mp2.player_id = m.p2_id "
                "WHERE t.guild_id=? AND t.id IN ({}) AND m.result IN ('p1','p2') "
                "AND mp1.actual IS NOT NULL AND mp2.actual IS NOT NULL "
                "ORDER BY t.id ASC"
            ).format(",".join("?" * len(tids)))
            async with conn.execute(q, (guild_id, *tids)) as cur:
                rows = await cur.fetchall()

        total = wins = 0
        by_tid: Dict[int, Tuple[int, int]] = {}  # tid -> (wins, total)

        for _mid, tid, p1, p2, wpid, c1, c2 in rows:
            if c1 is None or c2 is None:
                continue

            if scope == "self":
                if p1 in my_pids and c1 == class_a:
                    me_side = 1
                elif p2 in my_pids and c2 == class_a:
                    me_side = 2
                else:
                    continue
                if class_b and ((me_side == 1 and c2 != class_b) or (me_side == 2 and c1 != class_b)):
                    continue
                total += 1
                win = int((me_side == 1 and wpid == p1) or (me_side == 2 and wpid == p2))
                wins += win
                w, t = by_tid.get(tid, (0, 0))
                by_tid[tid] = (w + win, t + 1)
                continue

            # 全伺服器
            if class_b:
                cond = (c1 == class_a and c2 == class_b) or (c2 == class_a and c1 == class_b)
                if not cond:
                    continue
                a_pid = p1 if c1 == class_a else p2
            else:
                if c1 == class_a:
                    a_pid = p1
                elif c2 == class_a:
                    a_pid = p2
                else:
                    continue

            total += 1
            win = int(wpid == a_pid)
            wins += win
            w, t = by_tid.get(tid, (0, 0))
            by_tid[tid] = (w + win, t + 1)

        losses = max(0, total - wins)
        wr = (wins / total) if total else 0.0
        avg_wr = 0.0
        if metric == "per_tournament" and by_tid:
            parts = [(w / t) for (w, t) in by_tid.values() if t > 0]
            avg_wr = (sum(parts) / len(parts)) if parts else 0.0

        return {"matches": total, "wins": wins, "losses": losses, "wr": round(wr, 4), "avg_wr": round(avg_wr, 4)}

    # ---------- UI: Seasons manage modals ----------
    class SeasonUpsertModal(discord.ui.Modal, title="新增 / 更新賽季"):
        slug = discord.ui.TextInput(label="識別碼 slug（例：s3, evo2025）", required=True)
        name = discord.ui.TextInput(label="顯示名稱（例：第三期 / 無限進化）", required=True)
        start = discord.ui.TextInput(label="開始日期 YYYY-MM-DD", required=True)
        end = discord.ui.TextInput(label="結束日期 YYYY-MM-DD（可留空）", required=False)

        def __init__(self, cog: "SwissExtras"):
            super().__init__(timeout=300)
            self.cog = cog

        async def on_submit(self, itx: discord.Interaction):
            await self.cog.setup_db()
            if not itx.user.guild_permissions.manage_guild:
                return await itx.response.send_message("需要管理權限。", ephemeral=True)

            s_ts = _ts_of_date(str(self.start.value))
            e_raw = str(self.end.value).strip()
            e_ts = _ts_of_date(e_raw, end_of_day=True) if e_raw else None
            if not s_ts or (e_ts is not None and e_ts < s_ts):
                return await itx.response.send_message("日期格式錯誤或結束日期早於開始日期。", ephemeral=True)

            await self.cog.seasons_upsert(str(self.slug.value).strip(), str(self.name.value).strip(), s_ts, e_ts)
            await itx.response.send_message("✅ 賽季已新增/更新。", ephemeral=True)

    class SeasonDeleteModal(discord.ui.Modal, title="刪除賽季"):
        slug = discord.ui.TextInput(label="識別碼 slug（要刪除的）", required=True)

        def __init__(self, cog: "SwissExtras"):
            super().__init__(timeout=180)
            self.cog = cog

        async def on_submit(self, itx: discord.Interaction):
            await self.cog.setup_db()
            if not itx.user.guild_permissions.manage_guild:
                return await itx.response.send_message("需要管理權限。", ephemeral=True)
            ok = await self.cog.seasons_delete(str(self.slug.value).strip())
            await itx.response.send_message("✅ 已刪除。" if ok else "刪除失敗或不存在。", ephemeral=True)

    # ---------- UI: Stats Panel (dynamic seasons) ----------
    class StatsPanelView(discord.ui.View):
        """
        勝率查詢面板（嚴守每列 ≤ 5）： 
        - Row0: 期別選單（動態）
        - Row1: 我方職業
        - Row2: 對手職業（含「全部」）
        - Row3: 兩顆切換（範圍 / 口徑）
        - Row4: 查詢 / 各職業總覽
        """
        CLASS_OPTS = [discord.SelectOption(label=x, value=x) for x in CLASS_LABELS]

        def __init__(self, cog: "SwissExtras", guild_id: int, user_id: int, seasons: List[dict]):
            super().__init__(timeout=900)
            self.cog = cog
            self.guild_id = guild_id
            self.user_id = user_id

            self.period: str = "all"            # "all" or slug
            self.scope: str = "all"              # "all" | "self"
            self.metric: str = "overall"         # "overall" | "per_tournament"
            self.class_a: Optional[str] = None
            self.class_b: Optional[str] = None

            # --- Row 0: period select (dynamic) ---
            opts = [discord.SelectOption(label="全部期別", value="all", default=True)]
            for s in seasons:
                s_name = s["name"]
                s_start = dt.datetime.fromtimestamp(s["start"]).strftime("%Y-%m-%d")
                if s["end"] is None:
                    subtitle = f"{s_start} 起"
                else:
                    s_end = dt.datetime.fromtimestamp(s["end"]).strftime("%Y-%m-%d")
                    subtitle = f"{s_start} ~ {s_end}"
                opts.append(discord.SelectOption(label=f"{s_name}（{subtitle}）", value=s["slug"]))

            sel_period = discord.ui.Select(placeholder="選擇期別", min_values=1, max_values=1, row=0, options=opts)
            async def _on_period(itx: discord.Interaction):
                self.period = sel_period.values[0]
                await itx.response.defer(ephemeral=True)
            sel_period.callback = _on_period
            self.add_item(sel_period)

            # --- Row 1: class A ---
            sel_a = discord.ui.Select(placeholder="我方職業（必選）", options=self.CLASS_OPTS, min_values=1, max_values=1, row=1)
            async def _on_a(itx: discord.Interaction):
                self.class_a = sel_a.values[0]
                await itx.response.defer(ephemeral=True)
            sel_a.callback = _on_a
            self.add_item(sel_a)

            # --- Row 2: class B ---
            sel_b = discord.ui.Select(
                placeholder="對手職業（預設全部）",
                options=[discord.SelectOption(label="全部", value="ALL", default=True)] + self.CLASS_OPTS,
                min_values=1, max_values=1, row=2
            )
            async def _on_b(itx: discord.Interaction):
                self.class_b = None if sel_b.values[0] == "ALL" else sel_b.values[0]
                await itx.response.defer(ephemeral=True)
            sel_b.callback = _on_b
            self.add_item(sel_b)

            # --- Row 3: toggles ---
            btn_scope = discord.ui.Button(label="範圍：全伺服器（點我切換）", style=discord.ButtonStyle.secondary, row=3)
            async def _toggle_scope(itx: discord.Interaction):
                self.scope = "self" if self.scope == "all" else "all"
                btn_scope.label = f"範圍：{'全伺服器' if self.scope=='all' else '只看自己'}（點我切換）"
                await itx.response.edit_message(view=self)
            btn_scope.callback = _toggle_scope
            self.add_item(btn_scope)

            btn_metric = discord.ui.Button(label="口徑：總勝率（點我切換）", style=discord.ButtonStyle.secondary, row=3)
            async def _toggle_metric(itx: discord.Interaction):
                self.metric = "per_tournament" if self.metric == "overall" else "overall"
                btn_metric.label = f"口徑：{'總勝率' if self.metric=='overall' else '單次賽事'}（點我切換）"
                await itx.response.edit_message(view=self)
            btn_metric.callback = _toggle_metric
            self.add_item(btn_metric)

            # --- Row 4: actions ---
            btn_q = discord.ui.Button(label="查詢", style=discord.ButtonStyle.primary, row=4)
            async def _query(itx: discord.Interaction):
                await self.cog.setup_db()
                if not self.class_a:
                    return await itx.response.send_message("請先選擇『我方職業』。", ephemeral=True)
                stats = await self.cog._class_stats(
                    guild_id=self.guild_id,
                    class_a=self.class_a,
                    class_b=self.class_b,
                    scope=self.scope,
                    user_id=self.user_id,
                    metric=self.metric,
                    period=self.period
                )
                msg = [
                    "查詢結果：",
                    f"- 期別：{('全部' if self.period=='all' else self.period)}",
                    f"- 範圍：{'全伺服器' if self.scope=='all' else '只看自己'}",
                    f"- 口徑：{'總勝率' if self.metric=='overall' else '單次賽事勝率'}",
                    f"- 我方職業：{self.class_a}",
                    f"- 對手職業：{self.class_b or '全部'}",
                    f"- 對局數：{stats['matches']} ；勝 {stats['wins']} / 負 {stats['losses']}",
                    f"- 勝率：{_fmt_pct(stats['wr'])}",
                ]
                if self.metric == "per_tournament":
                    msg.append(f"- 單次賽事勝率（均值）：{_fmt_pct(stats['avg_wr'])}")
                await itx.response.send_message("\n".join(msg), ephemeral=True)
            btn_q.callback = _query
            self.add_item(btn_q)

            btn_ov = discord.ui.Button(label="各職業總覽", style=discord.ButtonStyle.secondary, row=4)
            async def _overview(itx: discord.Interaction):
                await self.cog.setup_db()
                classes = CLASS_LABELS if not self.class_a else (self.class_a,)
                header = [
                    "各職業總覽：",
                    f"- 期別：{('全部' if self.period=='all' else self.period)}",
                    f"- 範圍：{'全伺服器' if self.scope=='all' else '只看自己'}",
                    f"- 計算方式：{'總勝率' if self.metric=='overall' else '單次賽事勝率'}",
                ]
                lines = header[:]
                for ca in classes:
                    s_all = await self.cog._class_stats(
                        guild_id=self.guild_id, class_a=ca, class_b=None,
                        scope=self.scope, user_id=self.user_id,
                        metric=self.metric, period=self.period
                    )
                    line = f"{ca}：{s_all['matches']} 場，{_fmt_pct(s_all['wr'])}"
                    if self.metric == "per_tournament":
                        line += f"（單次賽事均值 { _fmt_pct(s_all['avg_wr']) }）"
                    if self.class_b:
                        s_vs = await self.cog._class_stats(
                            guild_id=self.guild_id, class_a=ca, class_b=self.class_b,
                            scope=self.scope, user_id=self.user_id,
                            metric=self.metric, period=self.period
                        )
                        line += f"｜對 {self.class_b}：{s_vs['matches']} 場，{_fmt_pct(s_vs['wr'])}"
                    lines.append(line)

                msg = "\n".join(lines)
                if len(msg) > 1900:
                    msg = msg[:1900] + "…"
                await itx.response.send_message(msg, ephemeral=True)
            btn_ov.callback = _overview
            self.add_item(btn_ov)

    # ---------- UI: User Panel ----------
    class UserPanelView(discord.ui.View):
        def __init__(self, cog: "SwissExtras"):
            super().__init__(timeout=None)
            self.cog = cog

        @discord.ui.button(label="我的綜合戰績", style=discord.ButtonStyle.primary, custom_id="swissx:user:agg")
        async def btn_agg(self, itx: discord.Interaction, _):
            await self.cog.setup_db()
            stats = await self.cog.compute_user_aggregate(itx.guild.id, itx.user.id)
            wr = f"{stats['winrate']*100:.2f}%" if stats["played"] else "N/A"
            await itx.response.send_message(
                f"你的綜合戰績：\n- 場數：{stats['played']}（勝 {stats['wins']} / 負 {stats['losses']}）\n- 勝率：{wr}",
                ephemeral=True
            )

        @discord.ui.button(label="我的最近對局", style=discord.ButtonStyle.secondary, custom_id="swissx:user:recent")
        async def btn_recent(self, itx: discord.Interaction, _):
            await self.cog.setup_db()
            rows = await self.cog.fetch_user_recent_matches(itx.guild.id, itx.user.id, limit=20)
            if not rows:
                return await itx.response.send_message("查無你的對局紀錄。", ephemeral=True)
            lines = []
            for r in rows:
                ts = dt.datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d")
                lines.append(f"[{ts}] {r['tournament']} R{r['round']} 桌{r['table']} vs {r['opponent']} → {r['outcome']} (match {r['match_id']})")
            msg = "你的最近對局（最多 20 筆）：\n" + "\n".join(lines)
            await itx.response.send_message(msg[:1900] if len(msg)>1900 else msg, ephemeral=True)

        @discord.ui.button(label="今天比賽成績", style=discord.ButtonStyle.secondary, custom_id="swissx:user:today")
        async def btn_today(self, itx: discord.Interaction, _):
            await self.cog.setup_db()
            res = await self.cog.fetch_today_results(itx.guild.id, itx.user.id)
            if not res:
                return await itx.response.send_message("今天沒有你的賽事紀錄（或尚未建立/記分）。", ephemeral=True)
            lines = []
            for r in res:
                rank_txt = f"{r['rank']}/{r['participants']}" if r.get("rank") else f"{r['participants']} 人"
                lines.append(
                    f"- {r['name']}（ID={r['tid']}，{r['status']}）"
                    f"：{r['wins']}勝{r['losses']}負，積分 {r['score']}，名次 {rank_txt}"
                )
            await itx.response.send_message("你在**今天**的賽事：\n" + "\n".join(lines), ephemeral=True)


        @discord.ui.button(label="開啟勝率查詢面板", style=discord.ButtonStyle.success, custom_id="swissx:user:stats")
        async def btn_stats(self, itx: discord.Interaction, _):
            await self.cog.setup_db()
            seasons = await self.cog.seasons_all()
            await itx.response.send_message(
                "勝率查詢面板（僅自己可見）：",
                view=self.cog.StatsPanelView(self.cog, itx.guild.id, itx.user.id, seasons),
                ephemeral=True
            )

    # ---------- UI: Admin (archive/export/seasons) ----------
    class ExportIdModal(discord.ui.Modal, title="導出賽事 JSON（輸入賽事 ID）"):
        tid_input = discord.ui.TextInput(label="賽事 ID", placeholder="例如：42", required=True)
        def __init__(self, cog: "SwissExtras"): super().__init__(timeout=180); self.cog = cog
        async def on_submit(self, itx: discord.Interaction):
            try: tid = int(str(self.tid_input.value).strip())
            except Exception: return await itx.response.send_message("格式錯誤，請輸入數字 ID。", ephemeral=True)
            if not itx.user.guild_permissions.manage_guild: return await itx.response.send_message("需要管理權限。", ephemeral=True)
            f = await self.cog.export_tournament_json(tid)
            if not f: return await self.cog._send_ephemeral(itx, "找不到該賽事。")
            await self.cog._send_ephemeral(itx, "已導出指定賽事檔案：", file=f)

    class DeleteIdModal(discord.ui.Modal, title="刪除賽事（危險操作）"):
        tid_input = discord.ui.TextInput(label="賽事 ID", placeholder="將永久刪除該賽事及其對局/玩家/輪次", required=True)
        confirm = discord.ui.TextInput(label="輸入 DELETE 以確認", required=True)
        def __init__(self, cog: "SwissExtras"): super().__init__(timeout=180); self.cog = cog
        async def on_submit(self, itx: discord.Interaction):
            if str(self.confirm.value).strip().upper() != "DELETE":
                return await itx.response.send_message("未確認，已取消。", ephemeral=True)
            try: tid = int(str(self.tid_input.value).strip())
            except Exception: return await itx.response.send_message("格式錯誤，請輸入數字 ID。", ephemeral=True)
            if not itx.user.guild_permissions.manage_guild: return await itx.response.send_message("需要管理權限。", ephemeral=True)
            async with self.cog.db() as conn:
                async with conn.execute("SELECT 1 FROM tournaments WHERE id=?", (tid,)) as cur:
                    if not await cur.fetchone():
                        return await self.cog._send_ephemeral(itx, "找不到該賽事。")
                await conn.execute("DELETE FROM matches WHERE tournament_id=?", (tid,))
                await conn.execute("DELETE FROM rounds WHERE tournament_id=?", (tid,))
                await conn.execute("DELETE FROM players WHERE tournament_id=?", (tid,))
                await conn.execute("DELETE FROM tournaments WHERE id=?", (tid,))
                await conn.commit()
            await self.cog._send_ephemeral(itx, f"✅ 已刪除賽事 ID={tid} 及其所有相關資料。")

    class ArchivePanelView(discord.ui.View):
        def __init__(self, cog: "SwissExtras", guild_id: int):
            super().__init__(timeout=None)
            self.cog = cog; self.guild_id = guild_id
        async def _latest_tid(self) -> Optional[int]: return await self.cog.guild_latest_tid(self.guild_id)

        @discord.ui.button(label="存檔目前賽事", style=discord.ButtonStyle.success, custom_id="swissx:adm:archive")
        async def btn_archive(self, itx, _):
            await self.cog.setup_db()
            tid = await self._latest_tid()
            if not tid: return await self.cog._send_ephemeral(itx, "無現行賽事。")
            if not itx.user.guild_permissions.manage_guild: return await self.cog._send_ephemeral(itx, "需要管理權限。")
            tid2 = await self.cog.archive_current(self.guild_id)
            await self.cog._send_ephemeral(itx, f"✅ 已存檔賽事 ID={tid2}。" if tid2 else "存檔失敗。")

        @discord.ui.button(label="導出目前賽事 JSON", style=discord.ButtonStyle.primary, custom_id="swissx:adm:export_latest")
        async def btn_export_latest(self, itx, _):
            await self.cog.setup_db()
            tid = await self._latest_tid()
            if not tid: return await itx.response.send_message("無現行賽事。", ephemeral=True)
            if not itx.user.guild_permissions.manage_guild: return await itx.response.send_message("需要管理權限。", ephemeral=True)
            f = await self.cog.export_tournament_json(tid)
            if not f: return await itx.followup.send("導出失敗。", ephemeral=True)
            await itx.followup.send(file=f, ephemeral=True)

        @discord.ui.button(label="依 ID 導出 JSON", style=discord.ButtonStyle.secondary, custom_id="swissx:adm:export_id")
        async def btn_export_id(self, itx, _):
            await self.cog.setup_db()
            if not itx.user.guild_permissions.manage_guild: return await itx.response.send_message("需要管理權限。", ephemeral=True)
            await itx.response.send_modal(self.cog.ExportIdModal(self.cog))

        @discord.ui.button(label="刪除賽事（Danger）", style=discord.ButtonStyle.danger, custom_id="swissx:adm:delete")
        async def btn_delete(self, itx, _):
            await self.cog.setup_db()
            if not itx.user.guild_permissions.manage_guild: return await itx.response.send_message("需要管理權限。", ephemeral=True)
            await itx.response.send_modal(self.cog.DeleteIdModal(self.cog))

        @discord.ui.button(label="賽季管理（新增/更新/刪除）", style=discord.ButtonStyle.secondary, custom_id="swissx:adm:seasons_manage")
        async def btn_seasons_manage(self, itx, _):
            await self.cog.setup_db()
            if not itx.user.guild_permissions.manage_guild:
                return await itx.response.send_message("需要管理權限。", ephemeral=True)

            seasons = await self.cog.seasons_all()
            if not seasons:
                msg = "目前沒有賽季。"
            else:
                def fmt(ts):
                    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts is not None else "∞"
                lines = [f"- {s['slug']}：{s['name']}（{fmt(s['start'])} ~ {fmt(s['end'])}）" for s in seasons]
                msg = "目前賽季：\n" + "\n".join(lines)

            view = discord.ui.View(timeout=180)

            async def _upsert_cb(itx2: discord.Interaction):
                await itx2.response.send_modal(self.cog.SeasonUpsertModal(self.cog))
            async def _delete_cb(itx2: discord.Interaction):
                await itx2.response.send_modal(self.cog.SeasonDeleteModal(self.cog))

            b1 = discord.ui.Button(label="新增/更新", style=discord.ButtonStyle.success)
            b1.callback = _upsert_cb
            b2 = discord.ui.Button(label="刪除", style=discord.ButtonStyle.danger)
            b2.callback = _delete_cb
            view.add_item(b1); view.add_item(b2)
            await self.cog._send_ephemeral(itx, msg, view=view)

    class ChooseTournamentView(discord.ui.View):
        """在指定日期找到多場賽事時，用下拉讓使用者選一個。"""
        def __init__(self, cog: "SwissExtras", itx: discord.Interaction, items: list[tuple[int, str, int]]):
            super().__init__(timeout=180)  # (tid, name, created_at)
            self.cog = cog
            self.itx = itx

            options = []
            for tid, name, ts in items:
                date_str = dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
                label = f"{name} (ID={tid})"
                desc  = f"建立於 {date_str}"
                options.append(discord.SelectOption(label=label[:100], value=str(tid), description=desc[:100]))

            select = discord.ui.Select(placeholder="選擇要輸出的賽事", options=options, min_values=1, max_values=1)
            async def _on_pick(sel_itx: discord.Interaction):
                tid_chosen = int(select.values[0])
                # 讀名稱作為標題
                async with self.cog.db() as conn:
                    async with conn.execute("SELECT name FROM tournaments WHERE id=?", (tid_chosen,)) as c:
                        r = await c.fetchone()
                        tname = r[0] if r else f"ID={tid_chosen}"
                title = f"**{tname}** 的最終排名（不含決賽/季軍戰）："
                await self.cog._post_final_table_for_tid(sel_itx, tid_chosen, title)
                # 關閉選單
                await sel_itx.edit_original_response(view=None)
            select.callback = _on_pick
            self.add_item(select)

    class DateTableModal(discord.ui.Modal, title="查指定日期最終排名圖（不含決賽）"):
        date = discord.ui.TextInput(label="日期（YYYY-MM-DD）", placeholder="例如：2025-08-28", required=True)

        def __init__(self, cog: "SwissExtras"):
            super().__init__(timeout=180)
            self.cog = cog

        async def on_submit(self, itx: discord.Interaction):
            await self.cog.setup_db()
            # 解析日期 → 起訖 timestamp
            ds = str(self.date.value).strip()
            start = _ts_of_date(ds, end_of_day=False)
            end   = _ts_of_date(ds, end_of_day=True)
            if not start or not end:
                return await itx.response.send_message("日期格式錯誤，請用 YYYY-MM-DD。", ephemeral=True)

            # 撈該日該伺服器的所有賽事
            async with self.cog.db() as conn:
                async with conn.execute(
                    "SELECT id, name, created_at FROM tournaments "
                    "WHERE guild_id=? AND created_at BETWEEN ? AND ? "
                    "ORDER BY id DESC",
                    (itx.guild.id, start, end)
                ) as cur:
                    tours = await cur.fetchall()

            if not tours:
                return await itx.response.send_message("該日期沒有賽事。", ephemeral=True)

            if len(tours) == 1:
                tid, tname, _ts = tours[0]
                title = f"**{tname}** 的最終排名（不含決賽/季軍戰）："
                await self.cog._post_final_table_for_tid(itx, tid, title)
                return

            # 多場：用下拉供選
            pretty_day = ds
            await itx.response.send_message(
                f"{pretty_day} 有多個賽事，請選擇要輸出的：",
                view=self.cog.ChooseTournamentView(self.cog, itx, [(t[0], t[1], t[2]) for t in tours]),
                ephemeral=True
            )

    # ---------- UI: Top-level extras panel ----------
    class ExtrasPanelView(discord.ui.View):
        """Top-level panel: user panel, stats panel, and admin panel entry."""
        def __init__(self, cog: "SwissExtras"):
            super().__init__(timeout=None)
            self.cog = cog

        @discord.ui.button(label="開啟我的戰績面板", style=discord.ButtonStyle.primary, custom_id="swissx:open:userpanel")
        async def btn_userpanel(self, itx, _):
            await self.cog.setup_db()
            await itx.response.send_message("Swiss 附加：個人戰績面板", view=self.cog.UserPanelView(self.cog), ephemeral=True)

        @discord.ui.button(label="開啟勝率查詢（全部按鈕操作）", style=discord.ButtonStyle.success, custom_id="swissx:open:statspanel")
        async def btn_statspanel(self, itx: discord.Interaction, _: discord.ui.Button):
            await self.cog.setup_db()
            seasons = await self.cog.seasons_all()
            await itx.response.send_message(
                "Swiss 附加：勝率查詢面板",
                view=self.cog.StatsPanelView(self.cog, itx.guild.id, itx.user.id, seasons),
                ephemeral=True
            )

        @discord.ui.button(label="開啟存檔管理（管理員）", style=discord.ButtonStyle.secondary, custom_id="swissx:open:adm")
        async def btn_admpanel(self, itx, _):
            await self.cog.setup_db()
            tid = await self.cog.guild_latest_tid(itx.guild.id)
            if not itx.user.guild_permissions.manage_guild and not (tid and (await self.cog.get_organizer(tid)) == itx.user.id):
                return await self.cog._send_ephemeral(itx, "需要管理權限或主辦者。")
            await self.cog._send_ephemeral(
                itx,
                "Swiss 附加：存檔管理面板（僅自己可見）",
                view=self.cog.ArchivePanelView(self.cog, itx.guild.id)
            )
                
        @discord.ui.button(label="查指定日期最終排名圖", style=discord.ButtonStyle.secondary, custom_id="swissx:open:date_table")
        async def btn_date_table(self, itx: discord.Interaction, _):
            await self.cog.setup_db()
            await itx.response.send_modal(self.cog.DateTableModal(self.cog))


    # ---------- Commands ----------
    @commands.group(name="swissx", invoke_without_command=True)
    async def swissx_root(self, ctx: commands.Context):
        """Swiss 附加功能：個人戰績 & 存檔管理 & 勝率查詢 面板"""
        await self.setup_db()
        await ctx.send("Swiss 附加總面板：", view=self.ExtrasPanelView(self))

    @swissx_root.command(name="panel")
    async def swissx_panel(self, ctx: commands.Context):
        await self.setup_db()
        await ctx.send("Swiss 附加總面板：", view=self.ExtrasPanelView(self))

    @swissx_root.command(name="export")
    @commands.has_permissions(manage_guild=True)
    async def swissx_export(self, ctx: commands.Context, tid: Optional[int] = None):
        """管理員：導出目前或指定賽事 JSON"""
        await self.setup_db()
        if tid is None:
            tid = await self.guild_latest_tid(ctx.guild.id)
        if tid is None:
            return await ctx.send("沒有找到賽事。")
        f = await self.export_tournament_json(tid)
        if not f:
            return await ctx.send("導出失敗。")
        await ctx.send(file=f)

    async def compute_standings_excl_finals(self, tid: int, active_only: bool = False):
        """
        產出和 SwissAll 相同欄位，但只計『瑞士輪』：
        - 排除 notes IN ('FINAL','THIRD') 的對局
        - Pts：每勝 +3，BYE 也 +3（只算拿到 BYE 的人）
        - MWP：勝/場，其中 BYE 計「拿到 BYE 的人」 played+1、wins+1（不建立對手關係）
        - OppMW：只取『實際對手』的 MWP 平均（BYE 不算對手）
        - OPPT1 = 0.26123 + 0.004312*MP - 0.007638*SOS + 0.003810*SOSS + 0.23119*OMW
        排序：Pts → OppMW → name
        回傳 rows：包含內部相容欄位 pid/name，也包含顯示用 Pos/Player。
        """
        # 1) 基礎資料
        async with self.db() as conn:
            async with conn.execute(
                "SELECT id,user_id,display_name,active FROM players WHERE tournament_id=?",
                (tid,)
            ) as cur:
                prows = await cur.fetchall()

            async with conn.execute(
                "SELECT p1_id,p2_id,result,winner_player_id,notes "
                "FROM matches WHERE tournament_id=?",
                (tid,)
            ) as cur:
                mrows = await cur.fetchall()

        players: Dict[int, Dict[str, any]] = {
            r[0]: {
                "pid": r[0],
                "user_id": r[1],
                "name": r[2],
                "active": r[3],
                "Pts": 0.0,           # 我們自己算（只含瑞士輪）
                "wins": 0,
                "played": 0,
                "opp_pids": set(),
            } for r in prows
        }

        # 2) 掃描對局（排除決賽/季軍戰）
        for p1, p2, res, wpid, notes in mrows:
            is_final_round = (notes in ("FINAL", "THIRD"))
            if is_final_round:
                continue
            if res is None:
                continue

            is_bye = (p1 is None) ^ (p2 is None)

            if is_bye:
                # BYE：只有拿到 BYE 的那位記分
                if res == "bye":
                    winner = p1 if (p1 is not None) else p2
                else:
                    # 若被以 p1/p2 寫入，也只會有一邊存在；仍以勝方為準
                    winner = wpid
                if winner in players:
                    players[winner]["wins"] += 1
                    players[winner]["played"] += 1
                    players[winner]["Pts"] += 3.0
                continue

            # 一般對局
            if p1 in players: players[p1]["played"] += 1
            if p2 in players: players[p2]["played"] += 1
            if wpid in players:
                players[wpid]["wins"] += 1
                players[wpid]["Pts"] += 3.0

            if p1 in players and p2 in players:
                players[p1]["opp_pids"].add(p2)
                players[p2]["opp_pids"].add(p1)

        # 3) MWP
        for p in players.values():
            p["MWP"] = (p["wins"] / p["played"]) if p["played"] > 0 else 0.0

        # 4) OppMW / SOS / SOSS / OPPT1
        def _pts(pid: int) -> float: return players[pid]["Pts"] if pid in players else 0.0
        def _mwp(pid: int) -> float: return players[pid]["MWP"] if pid in players else 0.0

        for p in players.values():
            opps = [players[op] for op in p["opp_pids"] if op in players]
            p["OppMW"] = (sum(_mwp(op["pid"]) for op in opps) / len(opps)) if opps else 0.0
            p["SOS"] = sum(_pts(op["pid"]) for op in opps)

            soss_sum = 0.0
            for op in opps:
                for op2 in op["opp_pids"]:
                    if op2 == p["pid"]:
                        continue
                    soss_sum += _pts(op2)
            p["SOSS"] = soss_sum

            MP, SOS, SOSS, OMW = p["Pts"], p["SOS"], p["SOSS"], p["OppMW"]
            p["OPPT1"] = 0.26123 + 0.004312 * MP - 0.007638 * SOS + 0.003810 * SOSS + 0.23119 * OMW

        # 5) 排序 & 輸出列
        ordered = sorted(players.values(), key=lambda x: (-x["Pts"], -x["OppMW"], x["name"].lower()))
        rows = []
        pos = 0
        for p in ordered:
            if active_only and not p["active"]:
                continue
            pos += 1
            rows.append({
                # 內部相容
                "pid": p["pid"],
                "name": p["name"],
                # 顯示欄位
                "Pos": pos,
                "Player": p["name"],
                "Pts": round(p["Pts"], 3),
                "MWP": round(p["MWP"], 4),
                "OppMW": round(p["OppMW"], 4),
                "OPPT1": round(p["OPPT1"], 6),
            })
        return rows

    async def render_standings_image_rows(self, rows: List[Dict[str, any]]) -> Optional[discord.File]:
        headers = ["Pos", "Player", "Pts", "MWP", "OppMW", "OPPT1"]
        table = [[r["Pos"], r["Player"], r["Pts"], r["MWP"], r["OppMW"], r["OPPT1"]] for r in rows]
        try:
            import os, io
            import matplotlib
            import matplotlib.pyplot as plt
            from matplotlib import font_manager
            matplotlib.rcParams["axes.unicode_minus"] = False

            def _pick_cjk_font():
                env_path = os.getenv("SWISS_CJK_FONT")
                if env_path and os.path.isfile(env_path):
                    return font_manager.FontProperties(fname=env_path)
                candidates = [
                    "Microsoft JhengHei","Microsoft YaHei","SimHei","PMingLiU","MingLiU",
                    "PingFang TC","PingFang SC","Hiragino Sans",
                    "Noto Sans CJK TC","Noto Sans CJK SC","Noto Sans CJK JP",
                    "Noto Sans TC","Source Han Sans TW","Source Han Sans SC","Source Han Sans JP",
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
            plt.close(fig); buf.seek(0)
            return discord.File(buf, filename="standings_today.png")
        except Exception:
            return None
        
    async def _post_final_table_for_tid(self, itx: discord.Interaction, tid: int, title: str):
        """用 swissx 的算法（排除決賽/季軍戰）繪出圖片並公開貼到頻道。"""
        rows = await self.compute_standings_excl_finals(tid, active_only=False)
        if not rows:
            return await self._send_ephemeral(itx, "查無排名資料。")
        if not itx.response.is_done():
            await itx.response.defer(ephemeral=True)
        f = await self.render_standings_image_rows(rows)
        if f:
            await itx.channel.send(content=title, file=f)
            await itx.followup.send("已發布該日期的最終排名圖。", ephemeral=True)
        else:
            headers = ["Pos","Player","Pts","MWP","OppMW","OPPT1"]
            lines = [title, "```", "\t".join(headers)]
            for r in rows:
                lines.append("\t".join(str(r[k]) for k in headers))
            lines.append("```")
            msg = "\n".join(lines)
            await itx.channel.send(msg if len(msg) < 1900 else msg[:1900] + "…")
            await itx.followup.send("已發布該日期的最終排名（文字）。", ephemeral=True)


# ---------- setup ----------
async def setup(bot: commands.Bot):
    if bot.get_cog("SwissExtras") is None:
        await bot.add_cog(SwissExtras(bot))
