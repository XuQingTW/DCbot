import asyncio
import os

import aiohttp
import discord
from discord.ext import commands


JSON_URL = "https://opendata.cwa.gov.tw/api/v1/rest/datastore/E-A0015-001?Authorization=CWB-07D30AE2-5882-4240-9A5A-372F3F3EA24B&limit=1&offset=0&format=JSON"
WARNING_CHANNEL_ID = 1224902159200686110
OWNER_ID = 649969607406387200
CLEANUP_ROLE_ID = 1498971250138021960
CLEANUP_KICK_REASON = "大掃除期間沒有解除大掃除身分組"

class ForgetSystem(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.warning_task = None

    @property
    def state(self):
        return self.bot.runtime_state

    async def cog_unload(self):
        if self.warning_task:
            self.warning_task.cancel()

    async def warning_loop(self):
        await self.bot.wait_until_ready()
        channel = self.bot.get_channel(WARNING_CHANNEL_ID)
        if channel is None:
            return

        last_eq_id = str(self.state.data.get("id", ""))
        timeout = aiohttp.ClientTimeout(total=30)
        headers = {"Accept": "application/json"}
        backoff = 1

        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            while not self.bot.is_closed():
                try:
                    async with session.get(JSON_URL, ssl=False) as resp:
                        resp.raise_for_status()
                        payload = await resp.json(content_type=None)

                    eq = payload["records"]["Earthquake"][0]
                    eq_no = str(eq["EarthquakeNo"])
                    if eq_no != last_eq_id:
                        img = eq.get("ReportImageURI") or eq.get("ReportImageURL")
                        mag = float(eq["EarthquakeInfo"]["EarthquakeMagnitude"]["MagnitudeValue"])
                        content = (
                            f"地震編號：{eq_no}\n"
                            f"報告內容：{eq.get('ReportContent', '(無)')}\n"
                        )
                        if mag >= 7.0:
                            content = f"<@everyone>\n{content}"

                        embed = discord.Embed()
                        if img:
                            embed.set_image(url=img)
                        await channel.send(content=content, embed=embed)

                        self.state.data["id"] = eq_no
                        last_eq_id = eq_no
                        await self.state.save_data()
                    backoff = 1
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    await self.state.log_exception("warning_loop", e)
                    await asyncio.sleep(min(backoff, 60))
                    backoff = min(backoff * 2, 60)
                    continue

                await asyncio.sleep(30)

    @commands.Cog.listener()
    async def on_ready(self):
        game = discord.Game("二分之一的自殺 今日 反面")
        await self.bot.change_presence(status=discord.Status.online, activity=game)
        await self.bot.tree.sync()
        if self.warning_task is None or self.warning_task.done():
            self.warning_task = asyncio.create_task(self.warning_loop())
        loaded = ", ".join(sorted(self.bot.extensions.keys()))
        await self.state.append_debug_log(f"[startup] bot_user={self.bot.user}")
        await self.state.append_debug_log(f"[startup] loaded_extensions={loaded}")

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CommandNotFound):
            return
        await self.state.log_exception(f"command:{getattr(ctx.command, 'qualified_name', 'unknown')}", error)
        await ctx.send(f"發生錯誤: {type(error).__name__}")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        state = self.state.get_voice_state(member.guild.id)
        if not state or before.channel is None or after.channel is not None:
            return
        await asyncio.sleep(180)
        if len(before.channel.members) == 1:
            if state["vc"].is_playing():
                state["vc"].stop()
            await state["vc"].disconnect()
            self.state.remove_voice_state(member.guild.id)

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author == self.bot.user or message.author.id in self.state.data["ban"]:
            return
        if message.content == "我要開門":
            await message.channel.send(f"<@{message.author.id}> 這個門只能從另外一側開啟")
            return
        if message.channel.id in self.state.data["nh"] and len(message.content) == 6 and message.content.isdigit():
            await message.channel.send(f"https://nhentai.net/g/{message.content}/")

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        member = guild.get_member(payload.user_id)

        mapping = self.state.data["role"].get(str(payload.message_id), {})
        role_id = mapping.get(str(payload.emoji))
        if not role_id:
            return
        role = guild.get_role(role_id)
        if member and role:
            await member.add_roles(role)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload):
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        mapping = self.state.data["role"].get(str(payload.message_id), {})
        role_id = mapping.get(str(payload.emoji))
        if not role_id:
            return
        member = guild.get_member(payload.user_id)
        role = guild.get_role(role_id)
        if member and role:
            await member.remove_roles(role)

    @commands.command()
    async def p(self, ctx):
        if len(ctx.message.mentions) > 0:
            member = ctx.message.mentions[0]
        else:
            member = ctx.author
        await ctx.send(f"{member.name}'s avatar: {member.display_avatar.url}")

    @commands.command()
    async def owner(self, ctx, msg, c=None):
        if ctx.author.id != OWNER_ID:
            await ctx.send("沒有權限")
            return

        if msg == "restart":
            await ctx.send("這個重啟指令目前已停用")
            await self.state.append_debug_log("[owner] restart requested but disabled")
            return

        if msg == "nh":
            nh_list = self.state.data["nh"]
            if c is None:
                await ctx.send("已經開啟" if ctx.channel.id in nh_list else "尚未開啟")
                return
            if c == "on" and ctx.channel.id not in nh_list:
                nh_list.append(ctx.channel.id)
                await self.state.save_data()
                await ctx.send("開始在這個頻道作用")
                return
            if c == "off" and ctx.channel.id in nh_list:
                nh_list.remove(ctx.channel.id)
                await self.state.save_data()
                await ctx.send("取消在這個頻道作用")
                return

    @commands.command()
    async def admin(self, ctx, action, message_id, emoji, group_id):
        if not (ctx.author.guild_permissions.administrator or ctx.author.id == OWNER_ID):
            await ctx.send("你沒有權限")
            return

        if action == "set_group":
            self.state.data["role"][message_id] = {str(emoji): int(group_id)}
            await self.state.save_data()
            await ctx.send("done")
            return

        if action == "del_role":
            self.state.data["role"].pop(message_id, None)
            await self.state.save_data()
            await ctx.send("done")

    @commands.command(name="clear")
    async def clear_cleanup_role(self, ctx):
        if ctx.author.id != OWNER_ID:
            await ctx.send("沒有權限")
            return
        if ctx.guild is None:
            await ctx.send("這個指令只能在伺服器裡使用")
            return

        role = ctx.guild.get_role(CLEANUP_ROLE_ID)
        if role is None:
            await ctx.send("找不到大掃除身分組")
            return

        bot_member = ctx.guild.me or ctx.guild.get_member(self.bot.user.id)
        if bot_member is None:
            await ctx.send("找不到 bot 自己的成員資料")
            return
        if not bot_member.guild_permissions.kick_members:
            await ctx.send("失敗：bot 沒有「踢出成員」權限")
            return

        kicked = 0
        failed = 0
        hierarchy_blocked = 0
        owner_blocked = 0
        targets = list(role.members)
        await ctx.send(f"開始大掃除，目標 {len(targets)} 人")
        for member in targets:
            if member == ctx.guild.owner:
                owner_blocked += 1
                continue
            if member.top_role >= bot_member.top_role:
                hierarchy_blocked += 1
                continue
            try:
                await member.kick(reason=CLEANUP_KICK_REASON)
                kicked += 1
            except discord.Forbidden as e:
                failed += 1
                await self.state.log_exception(f"clear:kick_forbidden:{member.id}", e)
            except Exception as e:
                failed += 1
                await self.state.log_exception(f"clear:kick:{member.id}", e)

        await ctx.send(
            f"大掃除完成：踢出 {kicked} 人，失敗 {failed} 人，"
            f"身分組位階不足略過 {hierarchy_blocked} 人，伺服器擁有者略過 {owner_blocked} 人"
        )

    @commands.command()
    async def ban(self, ctx):
        if ctx.author.id == OWNER_ID and ctx.message.mentions:
            target = ctx.message.mentions[0].id
            if target not in self.state.data["ban"]:
                self.state.data["ban"].append(target)
                await self.state.save_data()
            await ctx.send("Done")

    @commands.command()
    async def unban(self, ctx):
        if ctx.author.id == OWNER_ID and ctx.message.mentions:
            target = ctx.message.mentions[0].id
            if target in self.state.data["ban"]:
                self.state.data["ban"].remove(target)
                await self.state.save_data()
            await ctx.send("Done")

    @commands.command()
    async def c(self, ctx):
        await ctx.send(str(self.state.voice_clients))

    @commands.command()
    async def chelp(self, ctx):
        await ctx.send(
            """```可憐打工仔的指令:
    !join - 加入語音頻道
    !leave - 離開語音頻道
    !play - 播放
           r/random (on/off/空氣)- 隨機播放
           l/loop (on/off/空氣) - 重複播放
           y/youtube (URL) - 播放youtube歌曲(雖然這樣說但twitch也可以撥放(抖音好像也可以)
                             #如果含有list的歌曲只會撥放歌曲
           n/next - 顯示下三首歌曲
           d/defult - 播放預設歌曲(我電腦的所有歌)
           (youtube 的 URL) - 可以直接播放
    !pause - 暫停
    !resume - 繼續
    !stop - 停止
    !next - 跳到下一首
    !scan - 更新音樂資料夾
    !chelp - 顯示指令説明
    !swiss - 瑞士輪比賽用
    !swiss test - 測試指令
    !swissx - 瑞士輪查詢系統```"""
        )

    @commands.command()
    async def news(self, ctx):
        await ctx.send(
            """```各位聖誕快樂，我不快樂

常駐訊息
!chelp可以看到這台機器人的指令表
如果有任何問題請私訊魆檠

2025/08/21 15:56更新資訊:
    1.新增swiss功能以及swissx附屬功能，詳細使用方法請在!chelp查看
    2.!play功能炸掉了不要使用，不確定神麼時候能修好或者不想修好```"""
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(ForgetSystem(bot))
