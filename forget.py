#導入Discord.py
import discord
from discord.ext import commands
from discord import FFmpegPCMAudio
from discord.ext import commands

import random
import json
import asyncio
import yt_dlp
import urllib.request
import ssl
import os


#from . import pycld3
#client是我們與Discord連結的橋樑

#氣象APICWB-07D30AE2-5882-4240-9A5A-372F3F3EA24B
json_url = 'https://opendata.cwa.gov.tw/api/v1/rest/datastore/E-A0015-001?Authorization=CWB-07D30AE2-5882-4240-9A5A-372F3F3EA24B&limit=1&offset=0&format=JSON'
context = ssl._create_unverified_context()

#music 0=本地 1=網址

f = True

#open file music.json
with open('music.json','r') as file:
    data = json.load(file)
#open file music.json
with open('data.json','r') as file:
    data_num = json.load(file)
with open("pwd" , "r") as file:
    pwd = json.load(file)
#open
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.voice_states = True


bot = commands.Bot(command_prefix='!',intents = intents)
bot.remove_command('help')
voice_clients = {}
directory_to_scan = r"D:\家龢用\音樂"
directory_steam = r"D:\SteamLibrary\steamapps\music"

warning_send = bot.get_channel(1233692654210912389)


ytdl_format_options = {
    'extract_flat': True,
    'skip_download': True,
    'format': 'bestaudio/best',
    'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '256',
        }],
    'noplaylist': True,  # 不下載播放列表
    'quiet': True,  # 靜默模式，不打印下載信息
    'no_warnings': True,  # 不打印警告信息
}
ytdl_list_format_options = {
    'extract_flat': True,
    'skip_download': True,
    'format': 'bestaudio/best',
    'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '256',
        }],
    'quiet': True,  # 靜默模式，不打印下載信息
    'no_warnings': True,  # 不打印警告信息
}


#####################################################################################################################################

async def warning():
    """地震報告通知，就只是單純的大型json解碼而已，沒需要改吧"""
    global bot
    send = 1224902159200686110
    channel = bot.get_channel(send)
    id = data_num["id"]
    print("start search")
    while True:
        with urllib.request.urlopen(json_url, context=context) as jsondata:
            #將JSON進行UTF-8的BOM解碼，並把解碼後的資料載入JSON陣列中
            data = json.loads(jsondata.read().decode('utf-8-sig'))
        data = data['records']['Earthquake'][0]
        if data['EarthquakeNo'] != id:
            print(data['EarthquakeNo'])
            embed = discord.Embed()
            embed.set_image(url=data['ReportImageURI'])
            if data["EarthquakeInfo"]["EarthquakeMagnitude"]["MagnitudeValue"] > 7:
                await channel.send(f"<@everyone>\n地震編號：{data['EarthquakeNo']}\n報告內容：{data['ReportContent']}\n",embed=embed)
            else:
                await channel.send(f"地震編號：{data['EarthquakeNo']}\n報告內容：{data['ReportContent']}\n",embed=embed)
            id = data['EarthquakeNo']
            with open('data.json','w') as f:
                data_num["id"] = data['EarthquakeNo']
                json.dump(data_num,f)
        await asyncio.sleep(30)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        # 忽略未定義指令
        return
    # 如果是其他錯誤，則顯示
    raise error


#####################################################################################################################################
#play command

async def set_random(ctx,vc,set):
    """隨機撥放設置"""
    if set == "off":
            voice_clients[ctx.guild.id]["random"] = False
            await ctx.send("已關閉隨機播放")
    elif set == "on":
        voice_clients[ctx.guild.id]["random"] = True
        if vc.is_playing():#如果正在播放，取得目前索引並擷取尚未撥放的歌單
            a = voice_clients[ctx.guild.id]["list"]
            number = voice_clients[ctx.guild.id]["song"]
            a = a[number:]
            random.shuffle(a)#打亂歌單
            for i in range(len(voice_clients[ctx.guild.id]["list"])):#索引後的歌單被隨機化歌單覆蓋
                if i < number:
                    pass
                else:
                    voice_clients[ctx.guild.id]["list"][i] = a[i-number]
        await ctx.send("已開啟隨機播放，將播放隨機順序")
    elif set == None:
        if voice_clients[ctx.guild.id]["random"]:
            await ctx.send("隨機撥放中")
        else:
            await ctx.send("隨機撥放關閉中")
    else:
        await ctx.send("輸入錯誤(on or off)")
                
async def loop(ctx,vc,set):
    """重複播放音樂設置"""
    if set == "off":
        voice_clients[ctx.guild.id]["loop"] = False
        await ctx.send("已關閉重複播放")
    elif set == "on":
        print(ctx.author.id)
        if ctx.author.id == 414971247944794112:
            voice_clients[ctx.guild.id]["loop"] = False
            await ctx.send("已關閉重複播放")
            return
        voice_clients[ctx.guild.id]["loop"] = True
        await ctx.send("已開啟重複播放")
    elif set == None:
        if voice_clients[ctx.guild.id]["loop"]:
            await ctx.send("重複撥放中")
        else:
            await ctx.send("重複撥放已關閉")
    else:
        await ctx.send("輸入錯誤(on or off)")


async def youtube(ctx,vc,url):
    """撥放網路上的音樂"""
    if url == None:
        await ctx.send("輸入錯誤")
        return
    #如果url是youtube的直播，直接播放
#    if "youtube.com/watch?v=" in url:
#        loop = asyncio.get_event_loop()
#        vc.play(discord.FFmpegPCMAudio(url), after=lambda e:loop.create_task(next_song(ctx, vc)))
#        return
    voice_clients[ctx.guild.id]["list"].append([1,url])
    #如果url是youtube的1ist，將list所有音樂加入list
    if "youtube.com/playlist?list=" in url:
        voice_clients[ctx.guild.id]["list"].remove([1,url])
        print("[debug] start list")
        with yt_dlp.YoutubeDL(ytdl_list_format_options) as ydl:
            print("[debug] open list")
            info = ydl.extract_info(url, download=False)
            print("[debug] add list")
            await ctx.send(f"新增 {info['title']} 至播放清單")
            if voice_clients[ctx.guild.id]["random"]:
                random.shuffle(info["entries"])
            for i in info["entries"]:
                voice_clients[ctx.guild.id]["list"].append([1,i['url']])
                print(f"[debug] {i['url']}")
    
    if voice_clients[ctx.guild.id]["song"] > 0:
        await ctx.send("已加入播放清單，等待播放")
    else:
        if voice_clients[ctx.guild.id]["random"]:
            random.shuffle(voice_clients[ctx.guild.id]["list"])
        await playing_music(ctx,vc)

async def show_steam_music(ctx,vc,set):
    """顯示steam上的音樂"""
    music = scan_music_files(r"D:\SteamLibrary\steamapps\music")
    #輸出music路徑內的資料夾名稱
    i = os.listdir(music)
    

@bot.command()
async def thpynno(ctx):
    url = "https://www.youtube.com/watch?v=xAjvjVd6Xnk"
    await play(ctx,"y",url)


@bot.command()
async def special(ctx):
    song = r"D:\SteamLibrary\steamapps\music\100% Orange Juice - Character Song Pack Ultimate Weapon Girl\Ultimate Weapon Girl - Character Song Pack OST\Track 5 - Ultimate Weapon Girl (Bonus Track).mp3"
    if ctx.guild.id in voice_clients:
        vc = voice_clients[ctx.guild.id]["vc"]
        vc.play(discord.FFmpegPCMAudio(song))
        song_name = os.path.basename(song)
        await ctx.send(f'Now playing: ??????????????????????????????????????????')
    else:
        await ctx.send('Not in a voice channel.')

async def play_steam_music(ctx,vc,set):
    """播放steam上的音樂"""
    music = scan_music_files(r"D:\SteamLibrary\steamapps\music")
    #掃描所有音樂



async def next(ctx,vc,set):
    """下三首歌的名稱"""
    if len(voice_clients[ctx.guild.id]["list"]) == 0:
        await ctx.send("播放清單為空")
        return
    
    song = voice_clients[ctx.guild.id]["list"][voice_clients[ctx.guild.id]["song"]-1]
    if song[0] == 0:
        await ctx.send("接下來的歌為")
        song_name = os.path.basename(song[1])
        await ctx.send(song_name)
    elif song[0] == 1:
        song_name = song[1]
        str = "```" 
        str += f'接下來的歌為\n'
        with yt_dlp.YoutubeDL(ytdl_format_options) as ydl:
            try:
                for i in range(voice_clients[ctx.guild.id]["song"]-1, voice_clients[ctx.guild.id]["song"]+2):        
                    info = ydl.extract_info(voice_clients[ctx.guild.id]["list"][i][1], download=False)
                    song_name = info['title']
                    str += f'{i}: {song_name}\n'
            except:
                    pass
        str += "```" 
        await ctx.send(str)
async def defult(ctx,vc,set):
    """預設歌單"""
    a = data
    if voice_clients[ctx.guild.id]["random"]:
        random.shuffle(a)
    for i in a:
        voice_clients[ctx.guild.id]["list"].append(i)
    await ctx.send("已加入播放清單")
    if voice_clients[ctx.guild.id]["song"] > 0:
        await ctx.send("已加入播放清單，等待播放")
    else:
        await playing_music(ctx,vc)


async def fix(ctx,vc,set):
    """語音區域切換"""
    voice_channel = ctx.author.voice.channel
#    a = False
#    if voice_channel == voice_clients[ctx.guild.id]["vc"]:
#        voice_clients[ctx.guild.id]["reconnect"] = True
    if voice_channel:
#        print(f"[debug] {voice_channel.rtc_region}")
#        if voice_clients[ctx.guild.id]["reconnect"]:
#            vc.pause()
        if set == "auto" or set == "a":
            await voice_channel.edit(rtc_region=None)
            await ctx.send("已切換至自動語音區域")
            return
        if voice_channel.rtc_region != "japan":
            await voice_channel.edit(rtc_region="japan")
            await ctx.send("已切換至語音區域為日本")
        else:
            await voice_channel.edit(rtc_region="hongkong")
            await ctx.send("已切換至語音區域為香港")

    else:
        await ctx.send('You are not connected to a voice channel.')

async def set_sound(ctx,vc,set):
    """調整音量"""
    if set == None:
        await ctx.send("請輸入範圍")
    if set == "d" or set == "default":
        voice_clients[ctx.guild.id]["sound"] = 0.005
        await ctx.send("已切換至預設音量，目前音量為0.5%，將從下一首開始生效")
        return
    try:
        voice_clients[ctx.guild.id]["sound"] = float(set)
        await ctx.send(f"已切換至音量為{float(set)*100}%，將從下一首開始生效")
    except:
        await ctx.send("輸入錯誤")
        return

play_command = {
    "random":set_random,
    "loop":loop,
    "youtube":youtube,
    "next":next,
    "defult":defult,
    "d":defult,
    "y":youtube,
    "r":set_random,
    "l":loop,
    "n":next,
    "fix":fix,
    "f":fix,
    "sound":set_sound,
    "s":set_sound
}
#####################################################################################################################################
"""user save command"""
@bot.command()
async def save(ctx):
    """save"""
#####################################################################################################################################
@bot.command()
async def list(ctx,command,set:str=None,url:str=None):
    """播放清單設置"""
    if command in list_command:
        await list_command[command](ctx,command,set)
    else:
        await ctx.send("輸入錯誤")
async def list_play(ctx,name,url):
    """播放清單播放"""
    if name in data_num["music_list"][ctx.auther.id]:
        await play(ctx,"y",data_num["music_list"][ctx.auther.id][name])
    else:
        await ctx.send("輸入錯誤")
async def list_create_list(ctx,name,url):
    """創建播放清單"""
    try:
        if "youtube.com/playlist?list=" in url:
            with yt_dlp.YoutubeDL(ytdl_format_options) as ydl:
                info = ydl.extract_info(url, download=False)
                data_num["music_list"][ctx.auther.id][name] = []
                for i in info["entries"]:
                    if i != None:
                        data_num["music_list"][ctx.auther.id][name].append([1,i["url"]])
                await ctx.send("創建播放清單完成")
    except:
        await ctx.send("發生錯誤,也有可能是你沒有把歌單設定成非公開或公開")
async def list_list(ctx,name,url):
    if ctx.auther.id in data_num["music_list"]:
        if data_num["music_list"][ctx.auther.id] != {}:
            out = "這是您的列表"
            for i in data_num["music_list"][ctx.auther.id]:
                out += "\n" + i
            await ctx.send(out)
list_command = {
    "play":list_play,
    "p":list_play,
    "create":list_create_list,
    "c":list_create_list
}    
#####################################################################################################################################
#function
def scan_music_files(directory):
    music_extensions = ['.m4a', '.mp3', '.wav', '.flac', '.aac', '.ogg']

    music_files = []

    for root, dirs, files in os.walk(directory):
        for file in files:
            if any(file.endswith(ext) for ext in music_extensions):
                music_files.append([0,os.path.join(root, file)])
    return music_files

def save_json(data,a=True):
    if a:
        with open('music.json','w') as f:
            json.dump(data,f)


async def check_playlist(vc_client):
    """檢查播放清單並處理迴圈邏輯
    Args:
        vc_client (dict): 包含當前伺服器的音樂播放相關資訊的字典。

    Returns:
        tuple: 第一個值是布林值，表示清單是否有效；第二個值是訊息，當清單無效時返回。
    """
    slist = vc_client["list"]

    if vc_client["song"] == len(slist) or not slist:
        if vc_client["loop"] and slist:
            if vc_client["random"]:
                random.shuffle(slist)  # 隨機打亂播放清單
            vc_client["song"] = 0  # 重置播放索引
        else:
            vc_client["list"] = []  # 清空播放清單
            vc_client["song"] = 0
            return False, 'No more songs in queue.\nthe queue is now empty.'
    return True, None


def clean_song_url(song):
    """清理歌曲的多餘網址部分
    Args:
        song (list): 包含歌曲資訊的清單，第二個元素是 URL。

    Returns:
        list: 清理後的歌曲清單。
    """
    if "&list=" in song[1]:
        song[1] = song[1].split("&list=")[0]
    return song


def get_ffmpeg_options(volume):
    """生成 FFmpeg 的選項
    Args:
        volume (float): 音量大小。

    Returns:
        str: FFmpeg 的選項字串。
    """
    return f'-vn -filter:a "volume={volume}"'


def play_local_song(vc, song, ffmpeg_options, loop, ctx):
    """播放本地音樂
    Args:
        vc (VoiceClient): Discord 的語音客戶端。
        song (list): 包含歌曲資訊的清單。
        ffmpeg_options (dict): FFmpeg 的選項。
        loop (asyncio.AbstractEventLoop): 異步事件迴圈。
        ctx (Context): Discord 的指令上下文。

    Returns:
        str: 歌曲名稱。
    """
    song_path = song[1]
    song_name = os.path.basename(song_path)
    vc.play(FFmpegPCMAudio(song_path, **ffmpeg_options), after=lambda e: loop.create_task(next_song(ctx, vc)))
    return song_name


def play_online_song(vc, song, ffmpeg_options, loop, ctx):
    """播放線上音樂
    Args:
        vc (VoiceClient): Discord 的語音客戶端。
        song (list): 包含歌曲資訊的清單。
        ffmpeg_options (dict): FFmpeg 的選項。
        loop (asyncio.AbstractEventLoop): 異步事件迴圈。
        ctx (Context): Discord 的指令上下文。

    Returns:
        str: 歌曲名稱，若播放失敗返回 None。
    """
    with yt_dlp.YoutubeDL(ytdl_format_options) as ydl:
        try:
            info = ydl.extract_info(song[1], download=False)  # 提取線上歌曲資訊
            song_name = info['title']
            song_url = info['url']
            del info  # 釋放記憶體

            vc.play(
                FFmpegPCMAudio(song_url, **ffmpeg_options),
                after=lambda e: loop.create_task(next_song(ctx, vc))
            )
            return song_name

        except Exception as e:
            loop.create_task(ctx.send("無法播放該歌曲"))
            loop.create_task(next_song(ctx, vc))
            print(f"播放錯誤: {e}")  # 可以用於 debug
            return None


async def playing_music(ctx, vc):
    """撥放音樂
    Args:
        ctx (Context): Discord 的指令上下文。
        vc (VoiceClient): Discord 的語音客戶端。
    """
    guild_id = ctx.guild.id
    vc_client = voice_clients[guild_id]

    is_playlist_valid, message = await check_playlist(vc_client)
    if not is_playlist_valid:
        if message:
            await ctx.send(message)
        return

    slist = vc_client["list"]
    loop = asyncio.get_event_loop()
    
    song = clean_song_url(slist[vc_client["song"]])
    vc_client["song"] += 1

    ffmpeg_base_options = get_ffmpeg_options(vc_client["sound"])

    if song[0] == 0:
        song_name = play_local_song(vc, song, {'options': ffmpeg_base_options}, loop, ctx)
    elif song[0] == 1:
        song_name = play_online_song(vc, song, {
            'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
            'options': ffmpeg_base_options
        }, loop, ctx)

    if song_name:
        await ctx.send(f'Now playing: {song_name}')


async def next_song(ctx, vc, c=False):
    """設置播放下一首歌的控制器，判斷是否形成迴圈
    Args:
        ctx (Context): Discord 的指令上下文。
        vc (VoiceClient): Discord 的語音客戶端。
        c (bool): 是否插播歌曲。
    """
    guild_id = ctx.guild.id
    if guild_id not in voice_clients:
        return

    vc_client = voice_clients[guild_id]
    if vc_client["stop"]:
        vc_client["stop"] = False
        return

    if c:  # 插播判定
        vc_client["stop"] = True
    vc.stop()
    await playing_music(ctx, vc)



async def next_song(ctx, vc, c = False):
    """設置播放下一首歌的控制器，判斷是有形成迴圈"""
    if ctx.guild.id not in voice_clients:
        return
    if voice_clients[ctx.guild.id]["stop"]:
        voice_clients[ctx.guild.id]["stop"] = False
        return
    if c: #插播判定
        voice_clients[ctx.guild.id]["stop"] = True
    vc.stop()
    await playing_music(ctx,vc)
    
    
@bot.command()
async def r(ctx,url):
    if url == None:
        await ctx.send("輸入錯誤")
        return
    if ctx.guild.id not in voice_clients:
        await ctx.send('Not in a voice channel.')
        return
    vc = voice_clients[ctx.guild.id]["vc"]
    if voice_clients[ctx.guild.id]["r"]:
        voice_clients[ctx.guild.id]["song"] -= 1
        voice_clients[ctx.guild.id]["r"] = True
    vc.pause()
    if "&list=" in url:
        url = url.split("&list=")[0]
    ffmpeg_options = {
        'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
        'options': '-vn -filter:a "volume=0.01"'
    }
    with yt_dlp.YoutubeDL(ytdl_format_options) as ydl:
        info = ydl.extract_info( url, download=False)
        song_name = info['title']
        song = info['url']
        loop = asyncio.get_event_loop()
        await ctx.send(f'Now playing: {song_name}')
        vc.play(FFmpegPCMAudio(song, **ffmpeg_options), after=lambda e:loop.create_task(next_song(ctx, vc)))
    
@bot.command()
async def p(ctx):
    if len(ctx.message.mentions) > 0:
        member = ctx.message.mentions[0]
        await ctx.send(f"{member.name}'s avatar: {member.display_avatar.url}")
    else:
        member = ctx.author
        await ctx.send(f"{member.name}'s avatar: {member.display_avatar.url}")


@bot.command()
async def owner(ctx,msg,c = None):
    print(f"[debug] msg:{msg} c:{c}")
    if ctx.author.id != 649969607406387200:
        await ctx.send("沒有權限")
        return
    if msg == "restart":
        await ctx.send("三秒後開始嘗試執行")
        os.system("shutdown -r -t 3")
        asyncio.sleep(3)
        await ctx.send("開始重啟")
        return

    if msg == "nh":
        if c == None:
            if ctx.channel.id in data_num["nh"]:
                await ctx.send("已經開啟")
                return
            else:
                await ctx.send("尚未開啟")
        if c == "on":
            data_num["nh"].append(ctx.channel.id)
            with open("data.json","w") as f:
                json.dump(data_num,f)
            await ctx.send("開始在這個頻道作用")
        if c == "off":
            data_num["nh"].remove(ctx.channel.id)
            with open("data.json","w") as f:
                json.dump(data_num,f)
            await ctx.send("取消在這個頻道作用")


@bot.command()
async def admin(ctx,set,id,emoji,group_id):
    if set == "set_group":
        if ctx.author.guild_permissions.administrator or ctx.author.id == 649969607406387200:
            data_num["role"][id] = {str(emoji):int(group_id)}
            with open("data.json","w") as f:
                json.dump(data_num,f)
            await ctx.send("done")
        else:
            await ctx.send("你沒有權限")
    if set == "del_role":
        if ctx.member.guild_permissions.administrator:
            del data_num["role"][id]
            with open("data.json","w") as f:
                json.dump(data_num,f)
            await ctx.send("done")
        else:
            await ctx.send("你沒有權限")

    

#@bot.command()
#async def q(ctx):
#    str = "```" 
#    str += f'接下來的歌為\n'
#    with yt_dlp.YoutubeDL(ytdl_format_options) as ydl:
#        for i in range(voice_clients[ctx.guild.id]["song"]+1, voice_clients[ctx.guild.id]["song"]+51):
#            try:
#                info = ydl.extract_info(voice_clients[ctx.guild.id]["list"][i][1], download=False)
#                song_name = info['title']
#                str += f'{i}: {song_name}\n'
#            except:
#                print("[debug] error")
#    str += "```" 
#    await ctx.send(str)
    


#調用event函式庫
@bot.event
#當機器人完成啟動時
async def on_ready():
    global f
    print('目前登入身份：',bot.user)
    game = discord.Game("夜襲博士模擬器")
    #discord.Status.<狀態>，可以是online,offline,idle,dnd,invisible
    await bot.change_presence(status=discord.Status.online, activity=game)
    await bot.wait_until_ready()
    for filename in os.listdir('./cogs'):
        if filename.endswith('.py'):
            await bot.load_extension(f'cogs.{filename[:-3]}')
            print(f'已載入 {filename[:-3]} 指令')
    if f:
        await warning()
        f = False
        

@bot.event
async def on_voice_state_update(member, before, after):
    if member.guild.id in voice_clients and not before.channel is None and after.channel is None:
            await asyncio.sleep(180)
            if len(before.channel.members) == 1:
                if voice_clients[member.guild.id]["vc"].is_playing():
                    voice_clients[member.guild.id]["vc"].stop()
                await voice_clients[member.guild.id]["vc"].disconnect()
                del voice_clients[member.guild.id]


#######################################################################################################
@bot.command()
async def join(ctx):
    if ctx.author.voice:
        voice_channel = ctx.author.voice.channel
        if ctx.guild.id not in voice_clients:
            voice_clients[ctx.guild.id] = {"vc":await voice_channel.connect(),
                                           "list":[],
                                           "random":False,
                                           "loop":False,
                                           "stop":False,
                                           "song":0,
                                           "sound":0.01,
                                           "r":False}
            await ctx.send(f'Joined {voice_channel.name}')
        else:
            await ctx.send('Already in a voice channel.')
    else:
        await ctx.send('You are not connected to a voice channel.')

@bot.command()
async def leave(ctx):
    if ctx.guild.id in voice_clients:
        vc = voice_clients[ctx.guild.id]["vc"]
        if vc.is_playing():
            voice_clients[ctx.guild.id]["stop"] = True
            vc.stop()  
        await voice_clients[ctx.guild.id]["vc"].disconnect()
        del voice_clients[ctx.guild.id]
        await ctx.send('Left the voice channel.')
    else:
        await ctx.send('Not in a voice channel.')





@bot.command()
async def play(ctx,mod:str=None,set:str=None):
    """撥放音樂前置指令"""
    if ctx.guild.id not in voice_clients and set != "f" and set != "fix":
        await join(ctx)
        
    if ctx.guild.id in voice_clients:
        vc = voice_clients[ctx.guild.id]["vc"]
        if mod==None:
            if voice_clients[ctx.guild.id]["random"]:
                random.shuffle(voice_clients[ctx.guild.id]["list"])
            await playing_music(ctx,vc)
        elif mod in play_command:
            await play_command[mod](ctx,vc,set)
        elif ("youtube.com" in mod and set == None) or ("youtu.be" in mod and set == None):
            await play(ctx,"y",mod)
        else:
            await ctx.send("輸入錯誤")



@bot.command()
async def pause(ctx):
    if ctx.guild.id in voice_clients:
        vc = voice_clients[ctx.guild.id]["vc"]
        if vc.is_playing():
            vc.pause()

@bot.command()
async def resume(ctx):
    if ctx.guild.id in voice_clients:
        vc = voice_clients[ctx.guild.id]["vc"]
        if vc.is_paused():
            vc.resume()


@bot.command()
async def scan(ctx):
    music = scan_music_files(directory_to_scan)
    music += scan_music_files(directory_steam)
    save_json(music)
    await ctx.send(f"在目標資料夾尋找到{len(music)}首歌曲")


@bot.command()
async def next(ctx):
    """播放下一首歌"""
    if ctx.guild.id in voice_clients:
        vc = voice_clients[ctx.guild.id]["vc"]
        await next_song(ctx,vc,True)
@bot.command()
async def stop(ctx):
    """停止播放音樂，並重製播放清單"""
    if ctx.guild.id in voice_clients:
        vc = voice_clients[ctx.guild.id]["vc"]
        voice_clients[ctx.guild.id]["list"] = []
        voice_clients[ctx.guild.id]["song"] = 0
        voice_clients[ctx.guild.id]["stop"] = True
        vc.stop()

@bot.command()
async def c(ctx):
    """Custom 輸出檢查voice_clients用"""
    await ctx.send(voice_clients)

@bot.command()
async def ban(ctx):
    """ban"""
    if ctx.author.id == 649969607406387200 and ctx.message.mentions:
        await ctx.send("Done")
        with open('data.json','w') as f:
            data_num["ban"].append(ctx.message.mentions[0].id) 
            json.dump(data_num,f)

@bot.command()
async def unban(ctx):
    """解除ban"""
    if ctx.author.id == 649969607406387200 and ctx.message.mentions:
        await ctx.send("Done")
        with open('data.json','w') as f:
            data_num["ban"].remove(ctx.message.mentions[0].id) 
            json.dump(data_num,f)


@bot.command()
async def chelp(ctx):
    """自製的的指令"""
    await ctx.send("""```可憐打工仔的指令:
    !join - 加入語音頻道
    !leave - 離開語音頻道
    !play - 播放
           r/random (on/off/空氣)- 隨機播放
           l/loop (on/off/空氣) - 重複播放
           y/youtube (URL) - 播放youtube歌曲(雖然這樣說但twitch也可以撥放(抖音好像也可以)
                             #如果含有list的歌曲只會撥放歌曲
           n/next - 顯示下三首歌曲
           d/defult - 播放預設歌曲(我電腦的所有歌)
           (youtube 的 URL) - 可以直接播放
    !r (URL) - 插播
    !pause - 暫停
    !resume - 繼續
    !stop - 停止
    !next - 跳到下一首
    !scan - 更新音樂資料夾
    !chelp - 顯示指令説明
    !help - 未完成的指令説明```""")


@bot.command()
async def news(ctx):
    await ctx.send(
"""```各位聖誕快樂，我不快樂

常駐訊息
!chelp可以看到這台機器人的指令表
如果有任何問題請私訊魆檠

2024/12/25 04:11更新資訊:
    1.play youtube 指令被簡化成可以直接play接上網址，但僅限youtube的網址
    2.youtube music的個人推薦歌單是沒辦法直接撥放的
    3.承接上一個，如果歌單沒辦法撥放你要把歌單轉乘非公開或公開，私人的機器人無法播放
    4.聲音控制系統沒意外應該可能是好了吧
    5.教主養窩
未來更新
    撥放steam的原聲帶功能，目前搜尋系統完成但本地撥放炸掉了
    音量倍率可調整效果(目前0.5%)```""")
#########################################################################################################




@bot.event
async def on_message(message):
    
    if message.author == bot.user or message.author.id in data_num["ban"]:
        return
    await bot.process_commands(message)
    if message.content == "我要開門":
        await message.channel.send(f"<@{message.author.id}> 這個門只能從另外一側開啟")
        return
    if message.channel.id in data_num["nh"]:#自動補網址
        #如果訊息是6個數字
        if len(message.content) == 6 and message.content.isdigit():
            await message.channel.send(f"https://nhentai.net/g/{message.content}/")
        return    
    if message.channel.id == 1216603222215491594:#指定頻道搬運到複製頻道
        send = 1216430186917003428
        send2= 1308741359989162044
        channel = bot.get_channel(send)
        channel2 = bot.get_channel(send2)
        try:
            if len(message.attachments) == 0:
                await channel.send(message.content)
                await channel2.send(message.content)    
            else:
                for i in message.attachments:
                    await channel.send(i)
                    await channel2.send(i)
        except:
            for i in message.attachments:
                await channel.send(i)
                await channel2.send(i)
        return
    elif message.channel.id == 1216602901695430676:
        send = 1216430327585833041
        channel = bot.get_channel(send)
        try:
            if len(message.attachments) == 0:
                await channel.send(message.content)
            else:
                for i in message.attachments:
                    await channel.send(i)
        except:
            for i in message.attachments:
                await channel.send(i)
        return


                
@bot.event
async def on_message_edit(before, after):
    """編輯訊息檢查"""
    if before.content != after.content:
        if before.channel.id == 1216603222215491594:
            send = 1216430186917003428
            channel = bot.get_channel(send)
            await channel.send(after.content)
        elif before.channel.id == 1216602901695430676:
            send = 1216430327585833041
            channel = bot.get_channel(send)
            await channel.send("編輯後:"+after.content)
        


@bot.event
async def on_raw_reaction_add(payload):
    """加入身分組
    當用戶在訊息上添加表情符號時觸發，檢查該表情符號是否對應到特定的身分組，
    如果是，則將對應的身分組加入到用戶。

    Args:
        payload (RawReactionActionEvent): 包含觸發事件的詳細資訊，例如伺服器、訊息 ID 和表情符號等。
    """
    guild = bot.get_guild(payload.guild_id)  # 獲取事件所屬的伺服器
    id = str(payload.message_id)  # 將訊息 ID 轉為字串
    emoji = str(payload.emoji)  # 將表情符號轉為字串
    print(id)
    print(emoji)
    if id in data_num["role"] and emoji in data_num["role"][id]:  # 檢查訊息 ID 和表情是否存在於設定中
        print("[debug] add role start")
        role = data_num["role"][id][emoji]  # 獲取對應的身分組 ID
        member = guild.get_member(payload.user_id)  # 獲取用戶物件
        role = guild.get_role(role)  # 獲取身分組物件
        await member.add_roles(role)  # 添加身分組給用戶
        print("[debug] add role")


@bot.event
async def on_raw_reaction_remove(payload):
    """移除身分組
    當用戶在訊息上移除表情符號時觸發，檢查該表情符號是否對應到特定的身分組，
    如果是，則將對應的身分組從用戶移除。

    Args:
        payload (RawReactionActionEvent): 包含觸發事件的詳細資訊，例如伺服器、訊息 ID 和表情符號等。
    """
    guild = bot.get_guild(payload.guild_id)  # 獲取事件所屬的伺服器
    id = str(payload.message_id)  # 將訊息 ID 轉為字串
    emoji = str(payload.emoji)  # 將表情符號轉為字串
    if id in data_num["role"] and emoji in data_num["role"][id]:  # 檢查訊息 ID 和表情是否存在於設定中
        print("[debug] remove role start")
        role = data_num["role"][id][emoji]  # 獲取對應的身分組 ID
        member = guild.get_member(payload.user_id)  # 獲取用戶物件
        role = guild.get_role(role)  # 獲取身分組物件
        await member.remove_roles(role)  # 從用戶移除身分組
        print("[debug] remove role")

bot.run(pwd["tocken"]) 