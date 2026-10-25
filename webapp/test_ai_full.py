# -*- coding: utf-8 -*-
"""测试完整文案生成"""

import sys
import json
import requests

API_BASE = 'https://token.sensenova.cn/v1'
API_KEY = 'sk-qA5e29R5Uo5d8ylCyioHA3wC88U5YunO'
MODEL = 'sensenova-6.8-flash-lite'

url = f"{API_BASE}/chat/completions"
headers = {
    'Authorization': f'Bearer {API_KEY}',
    'Content-Type': 'application/json'
}

# 测试提示词
prompt = """你是一个专业的短剧文案策划师。请为以下短剧生成吸引人的文案。

【剧集信息】
- 剧名: 恶霸占我橘山
- 题材: 豪门逆袭
- 本版钩子: 逆袭
- 剧情概要: 恶霸强占橘子山，隐藏千金身份即将曝光

【生成要求】
1. 标题（title）: 6-16个字，不能有标点符号
2. 描述（description）: 50-100个字，剧情概要，制造悬念
3. 话题（hashtags）: 3-5个话题，每个以#开头

【输出格式】
请严格按照以下JSON格式输出，不要包含任何其他内容：
{
    "title": "标题文字",
    "description": "描述文字",
    "hashtags": ["#话题1", "#话题2", "#话题3"]
}"""

data = {
    'model': MODEL,
    'messages': [
        {
            'role': 'user',
            'content': prompt
        }
    ],
    'temperature': 0.8,
    'max_tokens': 1000,
    'reasoning_effort': 'none'
}

print("发送请求...")
response = requests.post(url, headers=headers, json=data, timeout=30)
print(f"状态码: {response.status_code}")

result = response.json()
print(f"\n完整响应:")
print(json.dumps(result, ensure_ascii=False, indent=2))

# 尝试解析
print("\n\n尝试解析...")
content = result['choices'][0]['message']['content']
print(f"内容: {content}")

# 提取 JSON
if '```json' in content:
    content = content.split('```json')[1].split('```')[0].strip()
elif '```' in content:
    content = content.split('```')[1].split('```')[0].strip()

print(f"\n提取后的 JSON:")
print(content)

# 解析 JSON
try:
    parsed = json.loads(content)
    print(f"\n解析成功:")
    print(f"  标题: {parsed.get('title')}")
    print(f"  描述: {parsed.get('description')}")
    print(f"  话题: {parsed.get('hashtags')}")
except json.JSONDecodeError as e:
    print(f"解析失败: {e}")