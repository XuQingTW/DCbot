import discord
from discord import app_commands
from discord.ext import commands
import json
import aiofiles
import os

# 非同步讀取 data.json，如果檔案不存在或內容為空，回傳空字典
async def read_data():
    if not os.path.exists("data.json"):
        return {}
    async with aiofiles.open("data.json", "r") as f:
        content = await f.read()
        if not content:
            return {}
        return json.loads(content)

# 非同步寫入 data.json
async def write_data(data):
    async with aiofiles.open("data.json", "w") as f:
        await f.write(json.dumps(data, ensure_ascii=False, indent=4))

# 定義一個指令群組，所有子指令都屬於 /user_setting
class UserSetting(app_commands.Group):
    def __init__(self):
        super().__init__(name="user_setting", description="使用者設定")

    # /user_setting sound [value]
    @app_commands.command(name="sound", description="設定你呼叫此bot使用的音量預設值")
    @app_commands.describe(value="音量預設值，輸入 0.5 代表 50% 音量（預設為 0.05）")
    async def sound(self, interaction: discord.Interaction, value: float = 0.05):
        data = await read_data()
        user_id = str(interaction.user.id)
        if user_id not in data:
            data["user_setting"][user_id] = {}
        data["user_setting"][user_id]["sound"] = value
        await write_data(data)
        await interaction.response.send_message(
            f"音量設定完成，目前音量為 {data['user_setting'][user_id]['sound']}",
            ephemeral=True
        )

    # /user_setting loop [value]
    @app_commands.command(name="loop", description="設定是否要循環播放")
    @app_commands.describe(value="請輸入 'on' 或 'off'")
    async def loop(self, interaction: discord.Interaction, value: str = "off"):
        data = await read_data()
        if value.lower() not in ["on", "off"]:
            await interaction.response.send_message("輸入錯誤，請輸入 on 或 off", ephemeral=True)
            return
        status = True if value.lower() == "on" else False
        user_id = str(interaction.user.id)
        if user_id not in data:
            data[user_id] = {}
        data["user_setting"][user_id]["loop"] = status
        await write_data(data)
        await interaction.response.send_message(
            f"音樂循環播放設定完成，目前狀態為 {data['user_setting'][user_id]['loop']}",
            ephemeral=True
        )

    # /user_setting shuffle [value]
    @app_commands.command(name="shuffle", description="設定是否要隨機播放")
    @app_commands.describe(value="請輸入 'on' 或 'off'")
    async def shuffle(self, interaction: discord.Interaction, value: str = "off"):
        data = await read_data()
        if value.lower() not in ["on", "off"]:
            await interaction.response.send_message("輸入錯誤，請輸入 on 或 off", ephemeral=True)
            return
        status = True if value.lower() == "on" else False
        user_id = str(interaction.user.id)
        if user_id not in data['user_setting']:
            data['user_setting'][user_id] = {}
        data["user_setting"][user_id]["shuffle"] = status
        await write_data(data)
        await interaction.response.send_message(
            f"音樂隨機播放設定完成，目前狀態為 {data['user_setting'][user_id]['shuffle']}",
            ephemeral=True
        )

# 建立一個 Cog 將群組加入 bot.tree（方便管理其他指令）
class SettingsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.tree.add_command(UserSetting())

async def setup(bot: commands.Bot):
    await bot.add_cog(SettingsCog(bot))
