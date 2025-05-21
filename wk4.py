import discord
import json
import os
import asyncio
from datetime import datetime
import aiohttp
import uuid
from collections import defaultdict, deque
from discord.ext import commands
from urllib.parse import quote
from pathlib import Path  # ✅

# ======== 設定與初始化 ========
MESSAGE_BUFFER_TIME = 10
MAX_MESSAGE_HISTORY = 50

with open("pwd", "r") as f:
    pwd = json.load(f)
with open("data.json", "r") as f:
    f = json.load(f)
    channel_list = f.get("channel", [])

TOKEN = pwd["tocken2"]
API_KEY = "Y6TFPYY-GW74S1M-KQ5BGBH-YTR1YRQ"
BASE_URL = "http://localhost:3001/api/v1/workspace"
WORKSPACE_SLUG = "discord"  # ✅ 記得改成你實際的 slug

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)
bot.remove_command('help')

message_buffers = defaultdict(lambda: deque(maxlen=200))
user_histories = defaultdict(lambda: deque(maxlen=MAX_MESSAGE_HISTORY))

# ======== thread map 永久儲存 ========
thread_map_path = Path("thread_map.json")

def load_thread_map():
    if thread_map_path.exists():
        with open(thread_map_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_thread_map(data):
    with open(thread_map_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

channel_thread_map = load_thread_map()  # ✅ channel_id -> thread_id

# ======== 建立 Thread ========
async def create_thread(workspace_slug, thread_slug, user_id=1):
    url = f"http://localhost:3001/api/v1/workspace/{quote(workspace_slug)}/thread/new"
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "userId": user_id,
        "name": f"Thread for {thread_slug}",
        "slug": thread_slug
    }

    print("🧪 建立 Thread payload:", json.dumps(payload))

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status == 200:
                    thread_info = await resp.json()
                    return thread_info["thread"]["id"]
                else:
                    error_text = await resp.text()
                    print(f"⚠️ 建立 Thread 失敗 ({resp.status}):\n{error_text}")
                    return None
    except Exception as e:
        print(f"⚠️ 建立 Thread 時發生錯誤：{e}")
        return None

# ======== 發送對話 ========
async def ask_anythingllm(workspace_slug, prompt, user_id=1, history=None, channel_id=None, thread_slug=None):
    channel_key = str(channel_id)

    if channel_key not in channel_thread_map:
        thread_slug = thread_slug or f"default-{uuid.uuid4().hex[:8]}"
        thread_id = await create_thread(workspace_slug, thread_slug, user_id=user_id)
        if thread_id is None:
            return None
        channel_thread_map[channel_key] = thread_id
        save_thread_map(channel_thread_map)
    else:
        thread_id = channel_thread_map[channel_key]

    API_URL = f"{BASE_URL}/{quote(workspace_slug)}/chat"
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "mode": "chat",
        "message": prompt,
        "threadId": thread_id
    }

    if history:
        payload["history"] = history

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(API_URL, headers=headers, json=payload, timeout=60) as resp:
                if resp.status != 200:
                    print(f"⚠️ AnythingLLM 回傳錯誤：{resp.status}")
                    return None
                result = await resp.json()
                return result.get("response", "").strip() or None
    except Exception as e:
        print(f"⚠️ AnythingLLM 發生錯誤：{e}")
        return None

# ======== 分段回覆 ========
async def reply_in_parts(channel, content):
    if not content:
        return
    parts = content.split("\n")
    for part in parts:
        if part.strip():
            await channel.send(part.strip())
            await asyncio.sleep(1)

# ======== 群體訊息彙整處理 ========
async def gather_and_respond():
    await bot.wait_until_ready()
    while True:
        await asyncio.sleep(MESSAGE_BUFFER_TIME)

        for channel_id, buffer in list(message_buffers.items()):
            try:
                if not buffer:
                    continue

                channel = bot.get_channel(channel_id)
                if not channel:
                    continue

                limited_msgs = list(buffer)[-MAX_MESSAGE_HISTORY:]
                grouped_msgs = "\n".join([f"{m['author']}: {m['content']}" for m in limited_msgs])
                message_buffers[channel_id].clear()

                prompt = (
                    f"這是最近大家說的話：\n{grouped_msgs}\n"
                    f"如果這些對話沒有明確的問題或主題，你可以選擇不回應；"
                    f"否則請自然地回應或延伸話題："
                )

                thread_slug = f"group-{channel_id}-{uuid.uuid4().hex[:8]}"
                response = await ask_anythingllm(
                    WORKSPACE_SLUG, prompt, user_id=0, channel_id=channel_id, thread_slug=thread_slug
                )
                if response:
                    # 檢查是否包含靜默控制詞
                    silent_keywords = ["[沉默]"]
                    if any(kw in response for kw in silent_keywords):
                        print(f"🤖 AI 選擇靜默，不輸出 channel {channel_id}")
                        continue
                    
                    # 若內容有意義則輸出
                    if any(c.isalnum() for c in response):
                        await reply_in_parts(channel, response)
                    else:
                        print(f"🤖 AI 回應內容無實質文字，選擇不輸出 channel {channel_id}")

            except Exception as e:
                print(f"處理頻道 {channel_id} 發生錯誤: {e}")

# ======== 驗證 API 金鑰 ========
async def verify_api_key():
    url = "http://localhost:3001/api/v1/auth"
    headers = {
        "Authorization": f"Bearer {API_KEY}"
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("authenticated") is True:
                        print("✅ API 金鑰驗證成功")
                        return True
                print(f"❌ API 金鑰驗證失敗: {resp.status}")
                return False
    except Exception as e:
        print(f"驗證 API 金鑰時發生錯誤: {e}")
        return False

# ======== Discord Bot 啟動 ========
@bot.event
async def on_ready():
    print(f'✅ 已登入 Discord，Bot 名稱：{bot.user}')
    auth_ok = await verify_api_key()
    if not auth_ok:
        print("🚫 請確認您的 API 金鑰正確，Bot 將不啟動與 AnythingLLM 的對話功能")
        return
    bot.loop.create_task(gather_and_respond())

# ======== 接收訊息事件 ========
@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if message.channel.id not in channel_list and bot.user not in message.mentions:
        return

    content = message.content.strip()
    channel_id = message.channel.id

    # ✅ BOT 被提及時
    if bot.user in message.mentions:
        user_name = message.author.display_name
        prompt = content.replace(f"<@{bot.user.id}>", "").strip()
        prompt = f"{user_name}: {prompt}"

        thread_slug = f"direct-{channel_id}-{uuid.uuid4().hex[:8]}"

        history = None
        if channel_id not in channel_list:
            user_histories[message.author.id].append({"role": "user", "content": prompt})
            history = list(user_histories[message.author.id])

        response = await ask_anythingllm(
            WORKSPACE_SLUG, prompt,
            user_id=message.author.id,
            channel_id=channel_id,
            thread_slug=thread_slug,
            history=history
        )

        print(f"AI 回應 channel {channel_id}: {response}")
        if response and any(c.isalnum() for c in response):
            if channel_id not in channel_list:
                user_histories[message.author.id].append({"role": "assistant", "content": response})
            await reply_in_parts(message.channel, response)
        else:
            print(f"AI 選擇不回應 channel {channel_id}")
        return

    # ✅ 群體訊息儲存
    message_buffers[channel_id].append({
        "author": message.author.display_name,
        "content": content,
        "time": datetime.now()
    })

# ======== 啟動 Bot ========
bot.run(TOKEN)
