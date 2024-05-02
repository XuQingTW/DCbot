import requests
import sys

# Discord 頻道的 Webhook URL
webhook_url = "https://discord.com/api/webhooks/1234192343113142272/rulOhk_bMyjWEGtIRugbHSpkbPLvjqjcxKVM_qNb4sPqW8lFtuoep4MnZWzf3fjVUL7Y"

# 要傳送的變數
variable1 = sys.argv[1]
variable2 = sys.argv[2]

# 構建要發送的消息內容
message = f"*地震警報* <@&1233693554023334011>\n震度：{variable1}\n將在{variable2}秒後到達"

# 發送 POST 請求到 Webhook URL
response = requests.post(webhook_url, json={"content": message})

# 檢查請求是否成功
if response.status_code == 200:
    print("消息發送成功")
else:
    print("消息發送失敗，錯誤碼：", response.status_code)