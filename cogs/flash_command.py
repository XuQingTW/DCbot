import discord
from discord.ext import commands
import os

class MySlashCommands(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # 使用 @discord.app_commands.command 裝飾器定義 Slash Command
    @discord.app_commands.command(name="hello", description="向你打個招呼")
    async def hello(self, interaction: discord.Interaction):
        await interaction.response.send_message("哈囉！", ephemeral=True)

class ReloadCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # 限制只有 bot 擁有者可以使用此指令
    @commands.command(name='reloadcogs', help='重新載入所有 cogs')
    @commands.is_owner()
    async def reloadcogs(self, ctx):
        reloaded = []
        loaded = []
        errors = []
        for filename in os.listdir('./cogs'):
            if filename.endswith('.py'):
                cog_name = f'cogs.{filename[:-3]}'
                try:
                    if cog_name in self.bot.extensions:
                        await self.bot.reload_extension(cog_name)
                        reloaded.append(cog_name)
                    else:
                        await self.bot.load_extension(cog_name)
                        loaded.append(cog_name)
                except Exception as e:
                    errors.append(f'{filename[:-3]}：{e}')

        await self.bot.tree.sync()

        message = ''
        if reloaded:
            message += '已重新載入：' + ', '.join(reloaded) + '\n'
        if loaded:
            message += '已載入：' + ', '.join(loaded)
        if errors:
            message += '錯誤：' + '\n'.join(errors)
        await ctx.send(message)

async def setup(bot: commands.Bot):
    await bot.add_cog(MySlashCommands(bot))
    await bot.add_cog(ReloadCog(bot))
    # 將這個 Cog 裡的 Slash Command 同步至 Discord