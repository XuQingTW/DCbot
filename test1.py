from discord.ext import commands
import discord


intents = discord.Intents.default()
intents.members = True
intents.message_content = True
# 創建 Discord Bot 對象
bot = commands.Bot(command_prefix='!',intents=intents)

# 在檔案結束之前可以打印出對象的內存地址
print(bot)
