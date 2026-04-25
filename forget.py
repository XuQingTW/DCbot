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

    loaded = []
    skipped = []
    for filename in os.listdir("./cogs"):
        if not filename.endswith(".py"):
            continue
        module = filename[:-3]
        if module in runtime_first or module in skip_modules:
            continue
        try:
            await bot.load_extension(f"cogs.{module}")
            loaded.append(module)
        except commands.ExtensionFailed as e:
            if isinstance(e.original, ModuleNotFoundError):
                skipped.append((module, str(e.original)))
                continue
            raise

    runtime = getattr(bot, "runtime_state", None)
    if runtime is not None:
        for module in loaded:
            await runtime.append_debug_log(f"[extension] loaded={module}")
        for module, reason in skipped:
            await runtime.append_debug_log(f"[extension] skipped={module} reason={reason}")


async def main():
    async with bot:
        await load_extensions()
        with open("pwd", "r", encoding="utf-8") as file:
            pwd = json.load(file)
        await bot.start(pwd["tocken"])


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
