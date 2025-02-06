import discord
from discord.ext import commands
import random
import asyncio
from discord.flags import Intents
from discord.ext import commands
from datetime import timedelta
from datetime import datetime
from discord.utils import utcnow
import pandas as pd
import numpy as np
import json
import random


with open("quz.json","r",encoding="utf-8") as f:
    quz = json.load(f)
with open("pwd","r",encoding="utf-8") as f:
    pwd = json.load(f)

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix='!',intents = intents)

user_data = {}










############################################################
async def give_quz(id:int,user_id:int):
    global user_data,quz
    s = bot.get_channel(id)
    time = user_data[user_id]['time']
    await s.send(f"第{time+1}題:")
    await s.send(f"{quz[user_data[user_id]['quz_num'][time]][0]}")
    await s.send(f"<@{user_id}> 請回答:")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        # 忽略未定義指令
        return
    # 如果是其他錯誤，則顯示
    raise error
############################################################



@bot.event
async def on_ready():
    """
    When the bot is ready, this function is called.

    It sets the bot's status to online and sets the activity to "Escape From Arknight".
    """
    print('目前登入身份：',bot.user)
    game = discord.Game("Escape From Arknight")
    #discord.Status.<狀態>，可以是online,offline,idle,dnd,invisible
    await bot.change_presence(status=discord.Status.online, activity=game)
    await bot.wait_until_ready()

@bot.command()
async def update(ctx):
    """
    This function is called when the !update command is used.

    It reads the "quz.json" file and updates the global variable "quz" with the new data.
    """
    global quz
    with open("quz.json","r",encoding="utf-8") as f:
        quz = json.load(f)
    await ctx.send("更新題庫完成")

@bot.command()
async def start(ctx):
    """
    This function is called when the !start command is used.

    It starts the game by generating a new set of questions for the user.
    """
    global user_data
    user_data[ctx.author.id] = {"quz_num":random.sample(range(len(quz)),5),
                                "score":0,
                                "time":-1}
    await ctx.send("初始化資料完成")
    await ctx.send("精通萬物的博士呀\n你是否準備好了挑戰泰拉最高難度的題目?(y/n)")


    
    

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    await bot.process_commands(message)
    if "教主"  in message.content and "拜託" in message.content and message.author.id == 649969607406387200:
        await message.channel.send("https://media.discordapp.net/attachments/1307536536518000712/1316094967256580227/GbjQ7YHa4AERqA8.jpg?ex=6759cc74&is=67587af4&hm=2de4c4a561059a14853512d564942582b73419f4c4d92010320895aeee526dfb&=&format=webp&width=905&height=905")
        return
    global user_data
    if message.content.lower() == "y" or message.content == "n" and message.author.id in user_data:
        if message.content.lower() == "y" and user_data[message.author.id]['time'] == -1:
            await message.channel.send("那我們開始吧")
            user_data[message.author.id]['time'] = 0
            await give_quz(message.channel.id,message.author.id)
            return
        if message.content.lower() == "n" and user_data[message.author.id]['time'] == -1:
            await message.channel.send("那下次再來吧")
            del user_data[message.author.id]
            return
    if message.author.id in user_data:
        """ This function is called when the bot receives a message.

        If the message is from a user who is currently playing the game, it checks if the message is a correct answer to the current question. If it is, it increments the user's score and checks if the user has finished the game. If the user has finished, it sends the user's score and removes the user from the game.
        """
        if user_data[message.author.id]['time'] == -1:
            return
        time = user_data[message.author.id]['time']
        #題目所有答案都要小寫
        if message.content.lower() in quz[user_data[message.author.id]['quz_num'][time]][1]:
            user_data[message.author.id]["score"] += 1
            if time > 3:#5次
                await message.channel.send("你已經完成挑戰")
                await message.channel.send(f"你的成績為{user_data[message.author.id]['score']}/5分")
                await message.channel.send("你的題目編號為"+str(user_data[message.author.id]['quz_num']))
                del user_data[message.author.id]
                return
            user_data[message.author.id]["time"] += 1#(time)
            await give_quz(message.channel.id,message.author.id)
            return
        else:
            await message.channel.send("你的答案錯誤")
            await give_quz(message.channel.id,message.author.id)
            return
    
    




bot.run(pwd["tocken2"])