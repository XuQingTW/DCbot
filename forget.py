import json
import os

import discord
from discord.ext import commands


intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)
bot.remove_command("help")


async def load_extensions():
    runtime_first = ["forget_runtime"]
    skip_modules = {"forget", "__init__"}
    for name in runtime_first:
        await bot.load_extension(f"cogs.{name}")

    for filename in os.listdir("./cogs"):
        if not filename.endswith(".py"):
            continue
        module = filename[:-3]
        if module in runtime_first or module in skip_modules:
            continue
        try:
            await bot.load_extension(f"cogs.{module}")
            print(f"已載入 {module} 指令")
        except commands.ExtensionFailed as e:
            if isinstance(e.original, ModuleNotFoundError):
                print(f"跳過 {module}，缺少依賴: {e.original}")
                continue
            raise


async def main():
    async with bot:
        await load_extensions()
        with open("pwd", "r", encoding="utf-8") as file:
            pwd = json.load(file)
        await bot.start(pwd["tocken"])


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
