import discord
import json
import os
from discord import app_commands
from discord.ext import commands

class PRTSCommand(commands.Cog):
    prts = app_commands.Group(
        name="prts",
        description="PRTS 系統控制指令"
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.snapshot_root = "snapshots"
        os.makedirs(self.snapshot_root, exist_ok=True)
        self.announcement_config = {}
        cfg_path = "data.json"
        if os.path.isfile(cfg_path):
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.announcement_config = data.get("announcement", {})
            except (json.JSONDecodeError, IOError):
                self.announcement_config = {}

    @prts.command(name="lockdown", description="PRTS 全面封鎖")
    async def lockdown(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("你沒有權限使用此指令", ephemeral=True)
            return

        guild = interaction.guild
        ann_id = self.announcement_config.get(str(guild.id))
        snapshot = {}

        # 1. 為此 guild 建立子資料夾
        guild_dir = os.path.join(self.snapshot_root, str(guild.id))
        os.makedirs(guild_dir, exist_ok=True)

        for channel in guild.channels:
            if not isinstance(channel, (discord.TextChannel, discord.ForumChannel, discord.Thread)):
                continue

            snapshot[str(channel.id)] = {}
            for target, ow in channel.overwrites.items():
                allow_val, deny_val = ow.pair()
                snapshot[str(channel.id)][str(target.id)] = {
                    "allow": allow_val.value,
                    "deny": deny_val.value
                }
                await channel.set_permissions(
                    target,
                    send_messages=False,
                    create_public_threads=False,
                    create_private_threads=False
                )

            if ann_id and channel.id == ann_id:
                bot_member = guild.me or guild.get_member(self.bot.user.id)
                if bot_member:
                    await channel.set_permissions(
                        bot_member,
                        send_messages=True,
                        create_public_threads=True,
                        create_private_threads=True
                    )

        # 2. 寫入到 snapshots/<guild.id>/snapshot.json
        path = os.path.join(guild_dir, "snapshot.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)

        await interaction.response.send_message("PRTS Lockdown 已啟動", ephemeral=True)
        channel = (self.bot.get_channel(int(ann_id)) if ann_id else None) or guild.system_channel
        if channel:
            await channel.send(f"PRTS 權限接管中，{interaction.user.mention} 已執行 lockdown")

    @prts.command(name="release", description="PRTS 解除封鎖並還原權限")
    async def release(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("你沒有權限使用此指令", ephemeral=True)
            return

        guild = interaction.guild
        guild_dir = os.path.join(self.snapshot_root, str(guild.id))
        path = os.path.join(guild_dir, "snapshot.json")
        if not os.path.isfile(path):
            await interaction.response.send_message("沒有找到封鎖快照，無法還原", ephemeral=True)
            return

        with open(path, "r", encoding="utf-8") as f:
            snapshot = json.load(f)
        default_role = guild.default_role

        for ch_id, targets in snapshot.items():
            channel = guild.get_channel(int(ch_id))
            if not channel or not isinstance(channel, (discord.TextChannel, discord.ForumChannel, discord.Thread)):
                continue

            for target_id, perms in targets.items():
                tid = int(target_id)
                if tid == default_role.id:
                    target = default_role
                else:
                    target = guild.get_member(tid) or guild.get_role(tid)
                if not target:
                    continue

                allow = discord.Permissions(perms.get("allow", 0))
                deny  = discord.Permissions(perms.get("deny", 0))
                ow = discord.PermissionOverwrite.from_pair(allow, deny)
                await channel.set_permissions(target, overwrite=ow)

        # 刪除快照檔案，可根據需求同時刪除空資料夾
        os.remove(path)
        # 若目標為空資料夾，可取消下一行註解自動刪除
        # os.rmdir(guild_dir)

        await interaction.response.send_message("PRTS Lockdown 已解除並還原權限", ephemeral=True)
        ann_id = self.announcement_config.get(str(guild.id))
        channel = (self.bot.get_channel(int(ann_id)) if ann_id else None) or guild.system_channel
        if channel:
            await channel.send(f"PRTS 權限已恢復，{interaction.user.mention} 已執行 release")

    @prts.command(name="set_announcement", description="設定公告頻道")
    @app_commands.describe(channel="要設定為公告的頻道")
    async def set_announcement(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("你沒有權限使用此指令", ephemeral=True)
            return
        guild = interaction.guild
        cfg_path = "data.json"
        data = {}
        if os.path.isfile(cfg_path):
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, IOError):
                data = {}
        if not isinstance(data.get("announcement"), dict):
            data["announcement"] = {}
        data["announcement"][str(guild.id)] = channel.id
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        self.announcement_config[str(guild.id)] = channel.id
        await interaction.response.send_message(f"已設定公告頻道為 {channel.mention}", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(PRTSCommand(bot))
