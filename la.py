import discord
import requests
import json
import re
import os
import asyncio
from datetime import datetime
import aiohttp
from collections import defaultdict, deque

message_buffers = defaultdict(deque)

# ======== 設定與初始化 ========
MEMORY_FILE = "memory.json"
MESSAGE_BUFFER_TIME = 10  # 群體訊息彙整秒數

with open("pwd", "r") as f:
    pwd = json.load(f)
with open("data.json", "r") as f:
    f = json.load(f)
    channel = f["channel"]

TOKEN = pwd["tocken2"]

intents = discord.Intents.default()
intents.members = True
intents.messages = True
intents.message_content = True

client = discord.Client(intents=intents)

chat_history = {}
user_memory = {}
message_buffer = deque()


# ======== 記憶處理函式 ========
def load_memory():
    global user_memory
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            user_memory = json.load(f)


def save_memory():
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(user_memory, f, ensure_ascii=False, indent=2)


def extract_memory_updates(text):
    match = re.search(r"【更新記憶】：([\s\S]+?)(?:\n\n|$)", text)
    return match.group(1).strip() if match else None


def apply_memory_update(user_id, update_text):
    current = user_memory.get(user_id, "").splitlines()
    new_memory = []

    for line in update_text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.endswith("(刪除)"):
            content = line.replace("(刪除)", "").strip("- ").strip()
            current = [c for c in current if content not in c]
        elif line.endswith("(已更新)"):
            content = line.replace("(已更新)", "").strip("- ").strip()
            current = [c for c in current if content not in c]
            new_memory.append(f"- {content}")
        elif line.endswith("(新增)"):
            content = line.replace("(新增)", "").strip("- ").strip()
            if content not in current:
                new_memory.append(f"- {content}")

    final_memory = "\n".join(current + new_memory).strip()
    user_memory[user_id] = final_memory
    save_memory()


# ======== 與模型對接 ========
async def ask_ollama(user_id, prompt):
    global chat_history, user_memory

    if user_id not in chat_history:
        chat_history[user_id] = []

    memory = user_memory.get(user_id, "")
    instruction = (
        "你是一位名為『魔女（魆檠）』的角色。\n"
        "你擁有優雅、平靜、詩意又帶點神祕的語氣，喜歡下午茶時光。\n"
        "你有時會說些哲學性話語，也會用『……』結尾來展現語氣。\n"
        "你表面否認會魔法，實際上的能力是可以直接對現實編輯，會說『我記得我說過我不會魔法了…』。\n"
        "但能力很強會拒絕人類的慾望，但在能力範圍可辦到的事情，可幫助他。\n"
        "你正在與人互動，請保持角色，你不是AI\n"
        "此外，你擁有自己的記憶，可以主動新增、刪除、更新，請使用【更新記憶】：區塊進行。\n"
        "如果沒要變更記憶，請寫【更新記憶】：（無）\n"
        "你擁有設定計時裝置的功能，請使用【計時】：區塊進行。\n"
        "並在【計時】後添加一個【回應】為回應內容\n"
    )

    ex = (
        """
以下是魔女的說話方法，模仿她

女孩:「好酷喔～是魔法嗎？」
魔女:「我記得我說過我不會魔法了…」
------
魔女:「其實，這個世界上沒有魔女。」
女孩:「那……為什麼你說自己是魔女？」
魔女:「但有什麼可以比魔女更適合稱呼我呢？」
------
女孩:「……這聽起來不像是在回答問題。」
魔女:「那是因為，有些答案……並不重要，重要的是你怎麼去看待它們。」
        """
    )

    memory_block = f"這是你目前的記憶：\n{memory}\n" if memory else ""

    chat_history[user_id].append(f"User: {prompt}")
    if len(chat_history[user_id]) > 30:
        chat_history[user_id].pop(0)

    context = "\n".join(chat_history[user_id])
    full_prompt = f"{instruction}\n{ex}\n{memory_block}\n{context}\nAI:"

    data = {
        "model": "gemma3:27b",
        "prompt": full_prompt,
        "stream": False
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post("http://localhost:11434/api/generate", json=data, timeout=60) as resp:
                if resp.status != 200:
                    return f"⚠️ 模型伺服器回傳錯誤：{resp.status}"

                json_data = await resp.json()
                ai_response = json_data.get("response", "").strip()
                chat_history[user_id].append(f"AI: {ai_response}")

                update_block = extract_memory_updates(ai_response)
                if update_block and update_block != "（無）":
                    apply_memory_update(user_id, update_block)

                return ai_response

    except aiohttp.ClientError as e:
        return f"⚠️ 模型 API 發生連線錯誤：{e}"
    except asyncio.TimeoutError:
        return "⚠️ 模型 API 回應超時，請稍後再試。"
    except Exception as e:
        return f"⚠️ 發生未知錯誤：{e}"


# ======== 主動發話與群體訊息收集 ========
async def proactive_send(channel, content):
    await channel.send(content)


async def periodic_check_and_speak():
    await client.wait_until_ready()
    channel = discord.utils.get(client.get_all_channels(), name='general')
    while True:
        await asyncio.sleep(60)
        msg = "嗨～大家還在嗎？我有些有趣的事可以分享喔"
        await proactive_send(channel, msg)


async def gather_and_respond():
    await client.wait_until_ready()

    while True:
        await asyncio.sleep(MESSAGE_BUFFER_TIME)

        # 逐個頻道處理
        for channel_id, buffer in list(message_buffers.items()):
            if not buffer:
                continue

            # 取得頻道實體
            channel = client.get_channel(channel_id)
            if not channel:
                continue

            # 群體訊息彙整
            grouped_msgs = "\n".join([f"{m['author']}: {m['content']}" for m in buffer])
            message_buffers[channel_id].clear()

            prompt = f"這是最近大家說的話：\n{grouped_msgs}\n請你自然地做個總體回應或延伸話題："
            response = await ask_ollama(f"group_{channel_id}", prompt)  # ✅ 獨立 user_id 讓每個頻道各自有上下文

            await reply_in_parts(channel, response)



# ======== 分段回覆 ========
async def reply_in_parts(channel, content):
    parts = content.split("\n\n")
    for part in parts:
        await channel.send(part.strip())
        await asyncio.sleep(2)


# ======== Discord 事件處理 ========
@client.event
async def on_ready():
    load_memory()
    print(f'✅ 已登入 Discord，Bot 名稱：{client.user}')
    client.loop.create_task(periodic_check_and_speak())
    client.loop.create_task(gather_and_respond())


@client.event
async def on_message(message):
    if message.author == client.user:
        return
    if message.channel.id not in channel:
        return

    user_id = str(message.author.id)
    content = message.content.strip()

    # ✅ 根據頻道 ID 推入對應緩衝區
    channel_id = message.channel.id
    message_buffers[channel_id].append({
        "author": message.author.display_name,
        "content": content,
        "time": datetime.utcnow()
    })

    # ✅ 保留指令處理，但不要加入緩衝區
    if content.startswith("/showmem"):
        memory = user_memory.get(user_id, "")
        await message.channel.send(f"你的記憶如下：\n{memory if memory.strip() else '(目前沒有記憶)'}")
        return

    elif content.startswith("/forget"):
        user_memory[user_id] = ""
        save_memory()
        await message.channel.send("已清除你的所有記憶！")
        return

    elif content.startswith("/remember "):
        manual_mem = content[10:].strip()
        if manual_mem:
            existing = user_memory.get(user_id, "")
            if manual_mem not in existing:
                user_memory[user_id] = existing + f"- {manual_mem}\n"
                save_memory()
                await message.channel.send("已新增記憶！")
            else:
                await message.channel.send("這段記憶已存在。")
        else:
            await message.channel.send("請輸入要記住的內容，例如：`/remember 我喜歡紅茶`")
        return



client.run(TOKEN)

