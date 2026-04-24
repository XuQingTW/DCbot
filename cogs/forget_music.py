import asyncio
import os
import random
from typing import Optional

import discord
import yt_dlp
from discord import FFmpegPCMAudio
from discord.ext import commands


ytdl_format_options = {
    "extract_flat": True,
    "skip_download": True,
    "format": "bestaudio/best",
    "postprocessors": [
        {
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "256",
        }
    ],
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
}

ytdl_list_format_options = {
    "extract_flat": True,
    "skip_download": True,
    "format": "bestaudio/best",
    "postprocessors": [
        {
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "256",
        }
    ],
    "quiet": True,
    "no_warnings": True,
}


class ForgetMusic(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def state(self):
        return self.bot.runtime_state

    def get_ffmpeg_options(self, volume: float):
        return f'-vn -filter:a "volume={volume}"'

    def get_user_settings(self, user_id: int):
        return self.state.data.setdefault("user_setting", {}).setdefault(str(user_id), {})

    def clean_song_url(self, song):
        if song[0] == 1 and "&list=" in song[1]:
            song = [song[0], song[1].split("&list=")[0]]
        return song

    async def ensure_joined(self, ctx) -> Optional[dict]:
        if not ctx.author.voice:
            await ctx.send("You are not connected to a voice channel.")
            return None
        voice_channel = ctx.author.voice.channel
        state = self.state.get_voice_state(ctx.guild.id)
        if state is None:
            vc = await voice_channel.connect()
            state = self.state.ensure_voice_state(ctx.guild.id, vc)
            user_settings = self.get_user_settings(ctx.author.id)
            if isinstance(user_settings.get("sound"), (int, float)):
                state["sound"] = float(user_settings["sound"])
            if isinstance(user_settings.get("loop"), bool):
                state["loop"] = user_settings["loop"]
            if isinstance(user_settings.get("shuffle"), bool):
                state["random"] = user_settings["shuffle"]
            await ctx.send(f"Joined {voice_channel.name}")
        return state

    async def check_playlist(self, vc_state):
        slist = vc_state["list"]
        if vc_state["song"] == len(slist) or not slist:
            if vc_state["loop"] and slist:
                if vc_state["random"]:
                    random.shuffle(slist)
                vc_state["song"] = 0
            else:
                vc_state["list"] = []
                vc_state["song"] = 0
                return False, "No more songs in queue.\nthe queue is now empty."
        return True, None

    async def play_online_song(self, ctx, vc, song, ffmpeg_options):
        def _extract():
            with yt_dlp.YoutubeDL(ytdl_format_options) as ydl:
                return ydl.extract_info(song[1], download=False)

        info = await asyncio.to_thread(_extract)
        song_name = info["title"]
        song_url = info["url"]
        loop = asyncio.get_running_loop()
        vc.play(
            FFmpegPCMAudio(song_url, **ffmpeg_options),
            after=lambda e: loop.create_task(self.next_song(ctx, vc)),
        )
        return song_name

    async def play_local_song(self, ctx, vc, song, ffmpeg_options):
        song_path = song[1]
        song_name = os.path.basename(song_path)
        loop = asyncio.get_running_loop()
        vc.play(
            FFmpegPCMAudio(song_path, **ffmpeg_options),
            after=lambda e: loop.create_task(self.next_song(ctx, vc)),
        )
        return song_name

    async def start_playback(self, ctx, vc):
        guild_id = ctx.guild.id
        vc_state = self.state.get_voice_state(guild_id)
        if not vc_state:
            return

        ok, message = await self.check_playlist(vc_state)
        if not ok:
            if message:
                await ctx.send(message)
            return

        song = self.clean_song_url(vc_state["list"][vc_state["song"]])
        vc_state["song"] += 1
        ffmpeg_base_options = self.get_ffmpeg_options(vc_state["sound"])

        if song[0] == 0:
            song_name = await self.play_local_song(ctx, vc, song, {"options": ffmpeg_base_options})
        else:
            song_name = await self.play_online_song(
                ctx,
                vc,
                song,
                {
                    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
                    "options": ffmpeg_base_options,
                },
            )
        await ctx.send(f"Now playing: {song_name}")

    async def next_song(self, ctx, vc, force_skip=False):
        vc_state = self.state.get_voice_state(ctx.guild.id)
        if not vc_state:
            return
        if vc_state["stop"]:
            vc_state["stop"] = False
            return
        if force_skip:
            vc_state["stop"] = True
        vc.stop()
        await self.start_playback(ctx, vc)

    async def extract_playlist_entries(self, url: str):
        def _extract_playlist():
            with yt_dlp.YoutubeDL(ytdl_list_format_options) as ydl:
                return ydl.extract_info(url, download=False)

        info = await asyncio.to_thread(_extract_playlist)
        entries = [i for i in info.get("entries", []) if i]
        return info, [[1, item["url"]] for item in entries if item.get("url")]

    async def queue_youtube(self, ctx, vc_state, url: str):
        vc_state["list"].append([1, url])
        if "youtube.com/playlist?list=" in url:
            vc_state["list"].remove([1, url])
            info, entries = await self.extract_playlist_entries(url)
            await ctx.send(f"新增 {info['title']} 至播放清單")
            if vc_state["random"]:
                random.shuffle(entries)
            vc_state["list"].extend(entries)

    @commands.command()
    async def join(self, ctx):
        await self.ensure_joined(ctx)

    @commands.command()
    async def leave(self, ctx):
        state = self.state.get_voice_state(ctx.guild.id)
        if not state:
            await ctx.send("Not in a voice channel.")
            return
        vc = state["vc"]
        if vc.is_playing():
            state["stop"] = True
            vc.stop()
        await vc.disconnect()
        self.state.remove_voice_state(ctx.guild.id)
        await ctx.send("Left the voice channel.")

    @commands.command()
    async def play(self, ctx, mod: str = None, arg: str = None):
        state = await self.ensure_joined(ctx)
        if not state:
            return
        vc = state["vc"]

        if mod is None:
            if state["random"]:
                random.shuffle(state["list"])
            await self.start_playback(ctx, vc)
            return

        if mod in ("random", "r"):
            if arg == "on":
                state["random"] = True
                self.get_user_settings(ctx.author.id)["shuffle"] = True
                await self.state.save_data()
                await ctx.send("已開啟隨機播放")
            elif arg == "off":
                state["random"] = False
                self.get_user_settings(ctx.author.id)["shuffle"] = False
                await self.state.save_data()
                await ctx.send("已關閉隨機播放")
            else:
                await ctx.send("輸入錯誤(on or off)")
            return

        if mod in ("loop", "l"):
            if arg == "on":
                state["loop"] = True
                self.get_user_settings(ctx.author.id)["loop"] = True
                await self.state.save_data()
                await ctx.send("已開啟重複播放")
            elif arg == "off":
                state["loop"] = False
                self.get_user_settings(ctx.author.id)["loop"] = False
                await self.state.save_data()
                await ctx.send("已關閉重複播放")
            else:
                await ctx.send("輸入錯誤(on or off)")
            return

        if mod in ("youtube", "y") and arg:
            await self.queue_youtube(ctx, state, arg)
            if state["song"] > 0:
                await ctx.send("已加入播放清單，等待播放")
            else:
                if state["random"]:
                    random.shuffle(state["list"])
                await self.start_playback(ctx, vc)
            return

        if ("youtube.com" in mod or "youtu.be" in mod) and arg is None:
            await self.play(ctx, "y", mod)
            return

        if mod in ("defult", "d"):
            songs = list(self.state.music_catalog)
            if state["random"]:
                random.shuffle(songs)
            state["list"].extend(songs)
            await ctx.send("已加入播放清單")
            if state["song"] == 0:
                await self.start_playback(ctx, vc)
            return

        if mod in ("next", "n"):
            if not state["list"]:
                await ctx.send("播放清單為空")
                return
            start = max(state["song"] - 1, 0)
            preview = state["list"][start:start + 3]
            names = []
            for item in preview:
                if item[0] == 0:
                    names.append(os.path.basename(item[1]))
                else:
                    names.append(item[1])
            await ctx.send("接下來的歌為\n" + "\n".join(names))
            return

        await ctx.send("輸入錯誤")

    @commands.command()
    async def pause(self, ctx):
        state = self.state.get_voice_state(ctx.guild.id)
        if state and state["vc"].is_playing():
            state["vc"].pause()

    @commands.command()
    async def resume(self, ctx):
        state = self.state.get_voice_state(ctx.guild.id)
        if state and state["vc"].is_paused():
            state["vc"].resume()

    @commands.command(name="next")
    async def next_track(self, ctx):
        state = self.state.get_voice_state(ctx.guild.id)
        if state:
            await self.next_song(ctx, state["vc"], True)

    @commands.command()
    async def stop(self, ctx):
        state = self.state.get_voice_state(ctx.guild.id)
        if state:
            state["list"] = []
            state["song"] = 0
            state["stop"] = True
            state["vc"].stop()

    @commands.command()
    async def scan(self, ctx):
        await ctx.send("已停用本地音樂資料夾掃描功能")

    @commands.command()
    async def special(self, ctx):
        await ctx.send("已停用本地 Steam 音樂功能")

    @commands.command()
    async def thpynno(self, ctx):
        await self.play(ctx, "y", "https://www.youtube.com/watch?v=xAjvjVd6Xnk")

    @commands.command(name="list")
    async def playlist_command(self, ctx, command=None, name: str = None, url: str = None):
        if command in ("list", "ls"):
            await self.list_saved_playlists(ctx)
            return
        if command in ("play", "p"):
            await self.play_saved_playlist(ctx, name)
            return
        if command in ("create", "c"):
            await self.create_saved_playlist(ctx, name, url)
            return
        await ctx.send("輸入錯誤")

    async def get_user_music_lists(self, user_id: int):
        music_list = self.state.data.setdefault("music_list", {})
        return music_list.setdefault(str(user_id), {})

    async def create_saved_playlist(self, ctx, name: str, url: str):
        if not name or not url:
            await ctx.send("輸入錯誤")
            return
        if "youtube.com/playlist?list=" not in url:
            await ctx.send("只支援 YouTube 播放清單")
            return
        try:
            info, entries = await self.extract_playlist_entries(url)
        except Exception:
            await ctx.send("發生錯誤,也有可能是你沒有把歌單設定成非公開或公開")
            return
        playlists = await self.get_user_music_lists(ctx.author.id)
        playlists[name] = entries
        await self.state.save_data()
        await ctx.send(f"創建播放清單完成: {info['title']}")

    async def play_saved_playlist(self, ctx, name: str):
        if not name:
            await ctx.send("輸入錯誤")
            return
        playlists = await self.get_user_music_lists(ctx.author.id)
        items = playlists.get(name)
        if not items:
            await ctx.send("輸入錯誤")
            return
        state = await self.ensure_joined(ctx)
        if not state:
            return
        state["list"].extend(items)
        await ctx.send(f"已加入播放清單 `{name}`")
        if state["song"] == 0:
            await self.start_playback(ctx, state["vc"])

    async def list_saved_playlists(self, ctx):
        playlists = await self.get_user_music_lists(ctx.author.id)
        if not playlists:
            await ctx.send("這是您的列表\n(空)")
            return
        out = "這是您的列表"
        for key in playlists:
            out += f"\n{key}"
        await ctx.send(out)


async def setup(bot: commands.Bot):
    await bot.add_cog(ForgetMusic(bot))
