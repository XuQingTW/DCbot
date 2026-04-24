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

    async def queue_youtube(self, ctx, vc_state, url: str):
        vc_state["list"].append([1, url])
        if "youtube.com/playlist?list=" in url:
            vc_state["list"].remove([1, url])

            def _extract_playlist():
                with yt_dlp.YoutubeDL(ytdl_list_format_options) as ydl:
                    return ydl.extract_info(url, download=False)

            info = await asyncio.to_thread(_extract_playlist)
            await ctx.send(f"新增 {info['title']} 至播放清單")
            entries = [i for i in info.get("entries", []) if i]
            if vc_state["random"]:
                random.shuffle(entries)
            for item in entries:
                vc_state["list"].append([1, item["url"]])

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
                await ctx.send("已開啟隨機播放")
            elif arg == "off":
                state["random"] = False
                await ctx.send("已關閉隨機播放")
            else:
                await ctx.send("輸入錯誤(on or off)")
            return

        if mod in ("loop", "l"):
            if arg == "on":
                state["loop"] = True
                await ctx.send("已開啟重複播放")
            elif arg == "off":
                state["loop"] = False
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


async def setup(bot: commands.Bot):
    await bot.add_cog(ForgetMusic(bot))
