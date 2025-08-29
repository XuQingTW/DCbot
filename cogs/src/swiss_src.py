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


def chunk_text(s: str, limit: int = 1800) -> List[str]:
    return [s[i:i + limit] for i in range(0, len(s), limit)]

class SwissSrc():
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

    async def render_roster_text(self, tid: int) -> str:
        """組出完整名單文字（含 active 標記、分數與 uid）。"""
        players = await self.fetch_players(tid, active_only=False)
        lines = []
        for p in players:
            tag = "✅" if p.active else "❌"
            lines.append(f"{tag} {p.display_name} (uid={p.user_id}) 分數={p.score}")
        return "\n".join(lines) if lines else "（目前沒有人）"

    async def _open_roster_safely(self, itx: discord.Interaction, tid: int):
        """名單 ≤3800 chars → 開 Modal；否則以附件+Embed 分頁回覆（ephemeral）。"""
        text = await self.render_roster_text(tid)

        # 方案 A：短名單 → Modal（預留空間避免接近 4000 的邊界）
        if len(text) <= 3800:
            return await itx.response.send_modal(self._RosterModal(self, tid, text))

        # 方案 B：長名單 → 文字檔附件 + 最多 10 頁 Embed 預覽
        buf = io.BytesIO(text.encode("utf-8"))
        buf.seek(0)
        file = discord.File(buf, filename=f"roster_{tid}.txt")

        pages = chunk_text(text, limit=1800)  # 你已有 chunk_text 工具
        embeds: list[discord.Embed] = []
        total = len(pages)
        for i, chunk in enumerate(pages[:10], 1):  # 安全起見：最多 10 個 embed
            em = discord.Embed(title=f"名單（第 {i}/{total} 頁）", description=chunk)
            embeds.append(em)

        content = f"名單字數 **{len(text)}** 超過 Modal 限制，已附上檔案供查看/複製。"
        if not itx.response.is_done():
            await itx.response.send_message(content, embeds=embeds, file=file, ephemeral=True)
        else:
            await itx.followup.send(content, embeds=embeds, file=file, ephemeral=True)

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