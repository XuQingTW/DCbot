import discord
from discord import app_commands
from discord.ext import commands


class UserSetting(app_commands.Group):
    def __init__(self, cog: "SettingsCog"):
        super().__init__(name="user_setting", description="使用者設定")
        self.cog = cog

    def get_bucket(self, user_id: int):
        data = self.cog.state.data.setdefault("user_setting", {})
        return data.setdefault(str(user_id), {})

    @app_commands.command(name="sound", description="設定你呼叫此bot使用的音量預設值")
    @app_commands.describe(value="音量預設值，輸入 0.5 代表 50% 音量（預設為 0.05）")
    async def sound(self, interaction: discord.Interaction, value: float = 0.05):
        bucket = self.get_bucket(interaction.user.id)
        bucket["sound"] = value
        await self.cog.state.save_data()
        await interaction.response.send_message(
            f"音量設定完成，目前音量為 {bucket['sound']}",
            ephemeral=True,
        )

    @app_commands.command(name="loop", description="設定是否要循環播放")
    @app_commands.describe(value="請輸入 'on' 或 'off'")
    async def loop(self, interaction: discord.Interaction, value: str = "off"):
        if value.lower() not in ["on", "off"]:
            await interaction.response.send_message("輸入錯誤，請輸入 on 或 off", ephemeral=True)
            return
        bucket = self.get_bucket(interaction.user.id)
        bucket["loop"] = value.lower() == "on"
        await self.cog.state.save_data()
        await interaction.response.send_message(
            f"音樂循環播放設定完成，目前狀態為 {bucket['loop']}",
            ephemeral=True,
        )

    @app_commands.command(name="shuffle", description="設定是否要隨機播放")
    @app_commands.describe(value="請輸入 'on' 或 'off'")
    async def shuffle(self, interaction: discord.Interaction, value: str = "off"):
        if value.lower() not in ["on", "off"]:
            await interaction.response.send_message("輸入錯誤，請輸入 on 或 off", ephemeral=True)
            return
        bucket = self.get_bucket(interaction.user.id)
        bucket["shuffle"] = value.lower() == "on"
        await self.cog.state.save_data()
        await interaction.response.send_message(
            f"音樂隨機播放設定完成，目前狀態為 {bucket['shuffle']}",
            ephemeral=True,
        )


class SettingsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.group = UserSetting(self)
        self.bot.tree.add_command(self.group)

    @property
    def state(self):
        return self.bot.runtime_state

    async def cog_unload(self):
        self.bot.tree.remove_command(self.group.name)


async def setup(bot: commands.Bot):
    await bot.add_cog(SettingsCog(bot))
