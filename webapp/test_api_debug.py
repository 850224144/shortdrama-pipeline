# -*- coding: utf-8 -*-
"""测试 AI API 响应"""

import requests
import json

API_BASE = 'https://token.sensenova.cn/v1'
API_KEY = 'sk-qA5e29R5Uo5d8ylCyioHA3wC88U5YunO'
MODEL = 'sensenova-6.8-flash-lite'

url = f"{API_BASE}/chat/completions"
headers = {
    'Authorization': f'Bearer {API_KEY}',
    'Content-Type': 'application/json'
}
data = {
    'model': MODEL,
    'messages': [
        {
            'role': 'user',
            'content': '请回复：测试成功'
        }
    ],
    'temperature': 0.8,
    'max_tokens': 100,
    'reasoning_effort': 'none'
}

print("发送请求...")
response = requests.post(url, headers=headers, json=data, timeout=30)
print(f"状态码: {response.status_code}")
print(f"\n完整响应:")
result = response.json()
print(json.dumps(result, ensure_ascii=False, indent=2))