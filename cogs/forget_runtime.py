import json
import os
from typing import Any, Dict

import aiofiles
import discord
from discord.ext import commands


DEFAULT_DATA: Dict[str, Any] = {
    "id": "",
    "ban": [],
    "nh": [],
    "role": {},
    "channel": [],
    "announcement": {},
    "music_list": {},
    "user_setting": {},
}


class RuntimeState:
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.base_dir = os.path.abspath(os.getcwd())
        self.data_path = os.path.join(self.base_dir, "data.json")
        self.music_path = os.path.join(self.base_dir, "music.json")
        self.pwd_path = os.path.join(self.base_dir, "pwd")
        self.data: Dict[str, Any] = {}
        self.music_catalog = []
        self.passwords: Dict[str, Any] = {}
        self.voice_clients: Dict[int, Dict[str, Any]] = {}

    async def load(self):
        self.music_catalog = await self._read_json_file(self.music_path, default=[])
        self.data = await self._read_json_file(self.data_path, default={})
        self.passwords = await self._read_json_file(self.pwd_path, default={})
        self._ensure_defaults()

    async def _read_json_file(self, path: str, default: Any):
        if not os.path.exists(path):
            return default
        async with aiofiles.open(path, "r", encoding="utf-8") as f:
            content = await f.read()
        if not content.strip():
            return default
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            if path.endswith("pwd"):
                cleaned = content.strip()
                if cleaned.startswith("[") and cleaned.endswith("]"):
                    cleaned = "{" + cleaned[1:-1] + "}"
                return eval(cleaned, {"__builtins__": {}}, {})
            raise

    def _ensure_defaults(self):
        if not isinstance(self.data, dict):
            self.data = {}

        migrated_user_setting = {}
        for key, value in list(self.data.items()):
            if key.isdigit() and isinstance(value, dict):
                if any(field in value for field in ("sound", "loop", "shuffle")):
                    migrated_user_setting[key] = value
                    del self.data[key]

        for key, value in DEFAULT_DATA.items():
            if key not in self.data or not isinstance(self.data[key], type(value)):
                if isinstance(value, dict):
                    self.data[key] = dict(value)
                elif isinstance(value, list):
                    self.data[key] = list(value)
                else:
                    self.data[key] = value

        if migrated_user_setting:
            self.data["user_setting"].update(migrated_user_setting)

    async def save_data(self):
        async with aiofiles.open(self.data_path, "w", encoding="utf-8") as f:
            await f.write(json.dumps(self.data, ensure_ascii=False, indent=2))

    def get_voice_state(self, guild_id: int):
        return self.voice_clients.get(guild_id)

    def ensure_voice_state(self, guild_id: int, vc: discord.VoiceClient):
        state = self.voice_clients.get(guild_id)
        if state is None:
            state = {
                "vc": vc,
                "list": [],
                "random": False,
                "loop": False,
                "stop": False,
                "song": 0,
                "sound": 0.01,
                "r": False,
            }
            self.voice_clients[guild_id] = state
        else:
            state["vc"] = vc
        return state

    def remove_voice_state(self, guild_id: int):
        self.voice_clients.pop(guild_id, None)


class RuntimeStateCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.state = RuntimeState(bot)

    async def cog_load(self):
        await self.state.load()
        self.bot.runtime_state = self.state


async def setup(bot: commands.Bot):
    await bot.add_cog(RuntimeStateCog(bot))
