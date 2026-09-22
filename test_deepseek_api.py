"""测试 DeepSeek API 是否可以正常连接。"""

import os

from openai import OpenAI


# 从环境变量读取密钥，不把密钥直接写进源代码。key已经写进环境变量 $env:DEEPSEEK_API_KEY = Read-Host "请输入 DeepSeek API Key" -MaskInput
api_key = os.environ.get("DEEPSEEK_API_KEY")

# 如果没有设置密钥，提前给出容易理解的错误。
if not api_key:
    raise RuntimeError(
        "没有找到 DEEPSEEK_API_KEY，请先在当前终端设置环境变量。"
    )

# openai 只是 API 客户端包；base_url 决定请求实际发送给 DeepSeek。
client = OpenAI(
    api_key=api_key,
    base_url="https://api.deepseek.com",
)

# 发送一次最小请求，只测试模型连接，不涉及 Agent 或工具调用。
response = client.chat.completions.create(
    model="deepseek-v4-flash",
    messages=[
        {
            "role": "user",
            "content": "Reply with exactly: DeepSeek connection successful",
        }
    ],
    stream=False,
)

# 取出并打印模型返回的文本。
print(response.choices[0].message.content)