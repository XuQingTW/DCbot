import discord
from discord.ext import commands

class flash_command(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

        @commands.command(name='flash_test_command')
        async def flash_test_command(self, ctx):
            await ctx.send('flash_test_command is working')

async def setup(bot):
    await bot.add_cog(flash_command(bot))