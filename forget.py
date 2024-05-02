#導入Discord.py
import discord
from discord import channel
from discord.ext import commands
from discord.flags import Intents
from discord.voice_client import VoiceClient
from discord import app_commands
import random,os
from threading import Thread
from discord import FFmpegPCMAudio
import os
import json
import asyncio
import yt_dlp
import urllib.request
import ssl
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
with open('orange.json','r') as file:
    data_orange = json.load(file)
with open('data.json','r') as file:
    data_num = json.load(file)
#open
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.voice_states = True


bot = commands.Bot(command_prefix='!',intents = intents)
bot.remove_command('help')
voice_clients = {}
directory_to_scan = r"D:\家龢用\音樂"
directory_orange = r"D:\家龢用\音樂\100"

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

ffmpeg_options = {
        'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
        'options': '-vn'
    }
#####################################################################################################################################

async def warning():
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





#####################################################################################################################################
#play command

async def set_random(ctx,vc,set):
    if set == "off":
            voice_clients[ctx.guild.id]["random"] = False
            await ctx.send("已關閉隨機播放")
    elif set == "on":
        voice_clients[ctx.guild.id]["random"] = True
        if vc.is_playing():#如果正在播放
            a = voice_clients[ctx.guild.id]["list"]
            number = voice_clients[ctx.guild.id]["song"]
            a = a[number:]
            random.shuffle(a)
            for i in range(len(voice_clients[ctx.guild.id]["list"])):#將下一首開始的順序換成隨機
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
    if set == "off":
        voice_clients[ctx.guild.id]["loop"] = False
        await ctx.send("已關閉重複播放")
    elif set == "on":
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
    if set == None:
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
    
async def touhou(ctx,vc,msg):
    music = scan_music_files(r"D:\SteamLibrary\steamapps\common\东方冰之勇者记 ~ Touhou Hero of Ice Fairy\DLC - Supporter Pack\~ 原声集 - OST ~")
    if voice_clients[ctx.guild.id]["random"]:
        random.shuffle(music)
    for i in music:
        voice_clients[ctx.guild.id]["list"].append(i)
    if voice_clients[ctx.guild.id]["song"] > 0:
        await ctx.send("已加入播放清單，等待播放")
    else:
        await playing_music(ctx,vc)
async def next(ctx,vc,set):#
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
async def orange(ctx,vc,set):
    a = data_orange
    if voice_clients[ctx.guild.id]["random"]:
        random.shuffle(a)
    for i in a:
        voice_clients[ctx.guild.id]["list"].append(i)
    await ctx.send("orange已加入播放清單")
    if voice_clients[ctx.guild.id]["song"] > 0:
        await ctx.send("已加入播放清單，等待播放")
    else:
        await playing_music(ctx,vc)

async def fix(ctx,vc,set):
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
play_command = {
    "random":set_random,
    "loop":loop,
    "youtube":youtube,
    "next":next,
    "defult":defult,
    "orange":orange,
    "d":defult,
    "y":youtube,
    "r":set_random,
    "l":loop,
    "n":next,
    "o":orange,
    "touhou":touhou,
    "t":touhou,
    "fix":fix,
    "f":fix
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
    else:
        with open('orange.json','w') as f:
            json.dump(data,f)


async def playing_music(ctx, vc):
    slist = voice_clients[ctx.guild.id]["list"]
    if voice_clients[ctx.guild.id]["song"] == len(slist):#如果播完
        if voice_clients[ctx.guild.id]["loop"]:
            if voice_clients[ctx.guild.id]["random"]:
                random.shuffle(slist)
            voice_clients[ctx.guild.id]["song"] = 0
        else:
            voice_clients[ctx.guild.id]["list"] = []
            voice_clients[ctx.guild.id]["song"] = 0
            await ctx.send('No more songs in queue.')
            return

    loop = asyncio.get_event_loop()
    song = slist[voice_clients[ctx.guild.id]["song"]]##要改這裡
    voice_clients[ctx.guild.id]["song"] += 1
    if "&list=" in song[1]:
        song[1] = song[1].split("&list=")[0]

    if song[0] == 0:
        song = song[1]
        song_name = os.path.basename(song)
        vc.play(discord.FFmpegPCMAudio(song), after=lambda e:loop.create_task(next_song(ctx, vc)))
    elif song[0] == 1:
        with yt_dlp.YoutubeDL(ytdl_format_options) as ydl:
            try:
                info = ydl.extract_info(song[1], download=False)
                song_name = info['title']
                song = info['url']
                del info
                vc.play(discord.FFmpegPCMAudio(song, **ffmpeg_options), after=lambda e:loop.create_task(next_song(ctx, vc)))
            except:
                await ctx.send("無法一首該歌曲")
                await next_song(ctx,vc)
                return
    
    await ctx.send(f'Now playing: {song_name}')


async def next_song(ctx, vc, c = False):
    if ctx.guild.id not in voice_clients:
        return
    if voice_clients[ctx.guild.id]["stop"]:
        voice_clients[ctx.guild.id]["stop"] = False
        return
    if c:
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
    with yt_dlp.YoutubeDL(ytdl_format_options) as ydl:
        info = ydl.extract_info( url, download=False)
        song_name = info['title']
        song = info['url']
        loop = asyncio.get_event_loop()
        await ctx.send(f'Now playing: {song_name}')
        vc.play(discord.FFmpegPCMAudio(song, **ffmpeg_options), after=lambda e:loop.create_task(next_song(ctx, vc)))
    

@bot.command()
async def owner(ctx,msg):
    if msg == "restart":
        await ctx.send("三秒後開始嘗試執行")
        os.system("shutdown -r -t 3")
        asyncio.sleep(3)
        await ctx.send("開始重啟")

    

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
    game = discord.Game('成為白癡打工仔吧')
    #discord.Status.<狀態>，可以是online,offline,idle,dnd,invisible
    await bot.change_presence(status=discord.Status.online, activity=game)
    await bot.wait_until_ready()
    if f:
        await warning()
        f = False
        

@bot.event
async def on_voice_state_update(member, before, after):
    if member.guild.id in voice_clients:
        if not before.channel is None and after.channel is None:
            asyncio.sleep(180)
            if len(before.channel.members) == 1:
                if voice_clients[member.guild.id]["vc"].is_playing():
                    voice_clients[member.guild.id]["vc"].stop()
                voice_clients[member.guild.id]["vc"].disconnect()
                del voice_clients[member.guild.id]
#    if member.guild.id in voice_clients:
#        if member.id == bot.user.id and voice_clients[member.guild.id]["reconnect"]:
#            voice_clients[member.guild.id]["vc"].wait_until_connected()
#            voice_clients[member.guild.id]["vc"].resume()
#            voice_clients[member.guild.id]["reconnect"] = False
#            print("[debug] resume")


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
                                           "song":0,
                                           "stop":False,
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
    save_json(music)
    await ctx.send(f"在目標資料夾尋找到{len(music)}首歌曲")

@bot.command()
async def orange_scan(ctx):
    music = scan_music_files(directory_orange)
    save_json(music,False)
    await ctx.send(f"在目標資料夾尋找到{len(music)}首歌曲")

@bot.command()
async def next(ctx):
    if ctx.guild.id in voice_clients:
        vc = voice_clients[ctx.guild.id]["vc"]
        await next_song(ctx,vc,True)
@bot.command()
async def stop(ctx):
    if ctx.guild.id in voice_clients:
        vc = voice_clients[ctx.guild.id]["vc"]
        voice_clients[ctx.guild.id]["list"] = []
        voice_clients[ctx.guild.id]["song"] = 0
        voice_clients[ctx.guild.id]["stop"] = True
        vc.stop()
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

@bot.command()
async def special1(ctx):
    song = r"C:\Users\ASUS\Desktop\【神威鬼鸣】千本幼女_(法律已经阻止不了蘿莉控了)_2.mp4"
    if ctx.guild.id in voice_clients:
        vc = voice_clients[ctx.guild.id]["vc"]
        vc.play(discord.FFmpegPCMAudio(song))
        song_name = os.path.basename(song)
        await ctx.send(f'Now playing: ??????????????????????????????????????????')
    else:
        await ctx.send('Not in a voice channel.')

@bot.command()
async def CNM(ctx):
    song = r"C:\Users\ASUS\Downloads\【戰地風雲4】戰地4中文神配音 - 友軍之圍  笑死了！我中彈了....wav"
    if ctx.guild.id in voice_clients:
        vc = voice_clients[ctx.guild.id]["vc"]
        vc.play(discord.FFmpegPCMAudio(song))
        song_name = os.path.basename(song)
        await ctx.send(f'Now playing: ??????????????????????????????????????????')
    else:
        await ctx.send('Not in a voice channel.')
@bot.command()
async def c(ctx):
    await ctx.send(voice_clients)

@bot.command()
async def ban(ctx):
    if ctx.author.id == 649969607406387200 and ctx.message.mentions:
        await ctx.send("Done")
        with open('data.json','w') as f:
            data_num["ban"].append(ctx.message.mentions[0].id) 
            json.dump(data_num,f)

@bot.command()
async def unban(ctx):
    if ctx.author.id == 649969607406387200 and ctx.message.mentions:
        await ctx.send("Done")
        with open('data.json','w') as f:
            data_num["ban"].remove(ctx.message.mentions[0].id) 
            json.dump(data_num,f)


@bot.command()
async def help(ctx):
    embed = discord.Embed(title="Custom Help", description="This is a custom help command.")
    embed.add_field(name="!join", value="加入語音頻道")
    embed.add_field(name="!leave", value="離開語音頻道")
    embed.add_field(name="!play", value="播放")
    embed.add_field(name="!stop", value="停止")
    embed.add_field(name="!pause", value="暫停")
    embed.add_field(name="!resume", value="繼續")
    embed.add_field(name="!next", value="跳到下一首")
    embed.add_field(name="!r", value="插播")

    await ctx.send(embed=embed)
@bot.command()
async def chelp(ctx):
    await ctx.send("""```可憐打工仔的指令:
    !join - 加入語音頻道
    !leave - 離開語音頻道
    !play - 播放
           r/random (on/off/空氣)- 隨機播放
           l/loop (on/off/空氣) - 重複播放
           y/youtube (URL) - 播放youtube歌曲(雖然這樣說但twitch也可以撥放(抖音好像也可以)
           n/next - 顯示下三首歌曲
           d/defult - 播放預設歌曲(我電腦的所有歌)
           o/orange - 播放100% orange juice歌曲
           t/touhou - 播放⑨歌曲
    !r (URL) - 插播
    !pause - 暫停
    !resume - 繼續
    !stop - 停止
    !next - 跳到下一首
    !scan - 更新音樂資料夾
    !chelp - 顯示指令説明
    !special - ??? #非正常指令可能會有問題
    !CNM - 戰地風雲4中文神配音 #非正常指令可能會有問題```""")
#########################################################################################################




@bot.event
async def on_message(message):
    
    if message.author == bot.user or message.author.id in data_num["ban"]:
        return
    await bot.process_commands(message)
    if message.channel.id == 1216603222215491594:
        image = []
        try:
            for i in message.attachments:
                 image.append(i.url)
        except:
            pass
        send = 1216430186917003428
        channel = bot.get_channel(send)
        if len(image) == 0:
            await channel.send(message.content)
        else:
            for i in image:
                await channel.send(i)
        return
    elif message.channel.id == 1216602901695430676:
        image = []
        try:
            for i in message.attachments:
                image.append(i.url)
        except:
            pass
        send = 1216430327585833041
        channel = bot.get_channel(send)
        if len(image) == 0:
            await channel.send(message.content)
        else:
            for i in image:
                await channel.send(i)


                
@bot.event
async def on_message_edit(before, after):

    if before.content != after.content:
        if before.channel.id == 1216603222215491594:
            send = 1216430186917003428
            channel = bot.get_channel(send)
            await channel.send(after.content)
        elif before.channel.id == 1216602901695430676:
            send = 1216430327585833041
            channel = bot.get_channel(send)
            await channel.send("編輯後:"+after.content)
        

#分群身分組一般人跟怪人
@bot.event
async def on_raw_reaction_add(payload):
    print (payload.emoji)
    guild = bot.get_guild(payload.guild_id)
    if payload.message_id == 1216564236487229562:#分群身分組一般人跟怪人
        if str(payload.emoji) == '<:miku:1216434199675015188>':
            role = 1216560881719050260
            role = guild.get_role(role)
            await payload.member.add_roles(role)
        elif str(payload.emoji) == '<:emoji_2:1216564009692692491>':
            role = 1216560795261861939
            role = guild.get_role(role)
            await payload.member.add_roles(role)
    elif payload.message_id == 1216688099036495902:#主群身分組一般人跟怪人
        if str(payload.emoji) == '<:blue_archive:1197924218160025660>':
            role = 1216400867356573706
            role = guild.get_role(role)
            await payload.member.add_roles(role)
        elif str(payload.emoji) == '<:more18:1162345474586591263>':
            role = 1216400764033957888
            role = guild.get_role(role)
            await payload.member.add_roles(role)
    elif payload.message_id == 1233694947568390144:#國家級邊緣人
        if str(payload.emoji) == '<:emoji_21:1224632456305442837>':
            role = 1233693554023334011
            role = guild.get_role(role)
            await payload.member.add_roles(role)
@bot.event
async def on_raw_reaction_remove(payload):
    guild = bot.get_guild(payload.guild_id)
    member = guild.get_member(payload.user_id)
    if payload.message_id == 1216564236487229562:#分群身分組一般人跟怪人
        if str(payload.emoji) == '<:miku:1216434199675015188>':
            role = 1216560881719050260
            role = guild.get_role(role)
            await member.remove_roles(role)
        elif str(payload.emoji) == '<:emoji_2:1216564009692692491>':
            role = 1216560795261861939
            role = guild.get_role(role)
            await member.remove_roles(role)
    elif payload.message_id == 1216688099036495902:#主群身分組一般人跟怪人
        if str(payload.emoji) == '<:blue_archive:1197924218160025660>':
            role = 1216400867356573706
            role = guild.get_role(role)
            await member.remove_roles(role)
        elif str(payload.emoji) == '<:more18:1162345474586591263>':
            role = 1216400764033957888
            role = guild.get_role(role)
            await member.remove_roles(role)
    elif payload.message_id == 1233694947568390144:#國家級邊緣人
        if str(payload.emoji) == '<:emoji_21:1224632456305442837>':
            role = 1233693554023334011
            role = guild.get_role(role)
            await member.remove_roles(role)

bot.run('OTEyNzM4NTUyNzI5NzAyNDcw.GmniJ0.p5Lt5ZjjNgj_IegxHv5NpLJEJNBQk091junzp8') 