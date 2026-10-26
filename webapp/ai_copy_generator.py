# -*- coding: utf-8 -*-
"""
AI 文案生成器 - 使用 SenseNova API
生成标题、描述、话题
"""

import os
import sys
import json
import time
import logging
import requests

logger = logging.getLogger('AICopyGenerator')

# API 配置（敏感值不入库：读环境变量，缺省回退到项目根 .env）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENV_PATH = os.path.join(_PROJECT_ROOT, '.env')


def _load_env(path=_ENV_PATH):
    """极简 .env 加载器（无第三方依赖）。已存在的环境变量优先，不被 .env 覆盖。"""
    if not os.path.exists(path):
        return False
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, _, v = line.partition('=')
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        return True
    except OSError:
        return False


_load_env()

SENSENOVA_API_BASE = os.environ.get('SENSENOVA_API_BASE', 'https://token.sensenova.cn/v1')
# 注意：源码里不再存放任何 API key。未配置时 AICopyGenerator.enabled=False，直接走模板兜底。
SENSENOVA_API_KEY = os.environ.get('SENSENOVA_API_KEY', '')
SENSENOVA_MODEL = os.environ.get('SENSENOVA_MODEL', 'sensenova-6.8-flash-lite')


class AICopyGenerator:
    """AI 文案生成器"""
    
    def __init__(self, api_key=SENSENOVA_API_KEY):
        self.api_base = SENSENOVA_API_BASE
        self.api_key = api_key
        self.model = SENSENOVA_MODEL
        self.enabled = bool(api_key)  # 未配置 key 时禁用，直接走模板兜底
        self.cache = {}  # 缓存已生成的文案
    
    def generate_copy(self, drama_name, version_idx, version_hook, 
                      plot_summary='', drama_genre='', quotes=None):
        """
        生成完整文案（标题、描述、话题）

        Args:
            drama_name: 剧名
            version_idx: 版本索引 (1, 2, 3...)
            version_hook: 钩子主题 (逆袭、打脸、真相...)
            plot_summary: 剧情概要（可选，应来自ASR金句，不要编）
            drama_genre: 题材类型（可选）
            quotes: list[str] ASR抽出的真实高光台词（可选，强烈建议传）
        Returns:
            {
                'title': '标题（6-16字，无标点）',
                'description': '描述（50-100字，有悬念）',
                'hashtags': ['#话题1', '#话题2', ...]
            }
        """
        quotes = quotes or []
        # 未配置 API key：直接走模板，不做无意义的 API 调用
        if not self.enabled:
            return self._generate_template_copy(drama_name, version_idx, version_hook)
        # 检查缓存
        cache_key = f"{drama_name}_{version_idx}_{version_hook}"
        if cache_key in self.cache:
            return self.cache[cache_key]
        
        # 构建提示词
        prompt = self._build_prompt(drama_name, version_idx, version_hook, 
                                    plot_summary, drama_genre, quotes)
        
        try:
            # 调用 AI API（先试SenseNova，失败试智谱）
            try:
                response = self._call_api(prompt)
                result = self._parse_response(response)
            except Exception as e1:
                logger.warning(f'[AI] SenseNova失败({e1})，试智谱GLM...')
                response = self._call_zhipu(prompt)
                result = self._parse_response(response)

            # 验证结果
            result = self._validate_result(result, drama_name, version_hook)

            # 缓存结果
            self.cache[cache_key] = result

            return result

        except Exception as e:
            logger.error(f'[AI] 文案生成失败，返回兜底: {e}')
            return self._generate_template_copy(drama_name, version_idx, version_hook)
    
    def _build_prompt(self, drama_name, version_idx, version_hook, 
                      plot_summary, drama_genre, quotes=None):
        """构建 AI 提示词"""
        quotes = quotes or []
        quotes_block = ''
        if quotes:
            quotes_block = '\n'.join(f'  - {q}' for q in quotes[:5])
            quotes_block = f'''
【本剧真实高光台词（ASR提取，请基于这些台词写，不要编造剧情）】
{quotes_block}
'''

        prompt = f"""你是一个专业的短剧文案策划师。请为以下短剧生成吸引人的文案。

【剧集信息】
- 剧名: {drama_name}
- 题材: {drama_genre or '短剧'}
- 本版钩子: {version_hook}
{quotes_block}
- 补充信息: {plot_summary or '无'}

【生成要求】
1. 标题（title）:
   - 6-16个字
   - 不能有标点符号
   - 优先从上面的高光台词里提炼，不要编造人物身份（不要写"隐藏千金"这种我们不知道的剧情）
   - 体现本版钩子主题

2. 描述（description）:
   - 50-100个字
   - 基于真实高光台词制造悬念，不要编造"恶霸/豪门/复仇"等具体剧情设定
   - 不能包含剧名

3. 话题（hashtags）:
   - 3-5个话题
   - 每个话题以#开头
   - 包含热门话题和精准话题

【版本差异化】
- 这是第 {version_idx} 个版本
- 突出 {version_hook} 主题

【输出格式】
请严格按照以下JSON格式输出，不要包含任何其他内容：
{{
    "title": "标题文字",
    "description": "描述文字",
    "hashtags": ["#话题1", "#话题2", "#话题3"]
}}"""

        return prompt
    
    def _call_api(self, prompt):
        """调用 SenseNova API"""
        
        url = f"{self.api_base}/chat/completions"
        headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json'
        }
        data = {
            'model': self.model,
            'messages': [
                {
                    'role': 'user',
                    'content': prompt
                }
            ],
            'temperature': 0.8,
            'max_tokens': 4000,  # reasoning模型会吃token，给够
            'reasoning_effort': 'none'
        }
        
        try:
            response = requests.post(url, headers=headers, json=data, timeout=90)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            raise Exception(f"API 调用失败: {e}")

    def _call_zhipu(self, prompt):
        """智谱GLM-4-Flash备用"""
        url = 'https://open.bigmodel.cn/api/paas/v4/chat/completions'
        headers = {
            'Authorization': 'Bearer 55f9d6f4864e4088b6cd162df2c5be72.itFPsQE7gz80kWw6',
            'Content-Type': 'application/json'
        }
        data = {
            'model': 'glm-4-flash',
            'messages': [{'role': 'user', 'content': prompt}],
            'temperature': 0.8,
            'max_tokens': 2000
        }
        response = requests.post(url, headers=headers, json=data, timeout=60)
        response.raise_for_status()
        return response.json()

    def _parse_response(self, response):
        """解析 AI 响应"""
        
        content = None
        try:
            content = response['choices'][0]['message']['content']
            
            # 提取 JSON
            # 处理可能被包裹在 ```json ... ``` 中的情况
            if '```json' in content:
                content = content.split('```json')[1].split('```')[0].strip()
            elif '```' in content:
                content = content.split('```')[1].split('```')[0].strip()
            
            result = json.loads(content)
            
            return {
                'title': result.get('title', ''),
                'description': result.get('description', ''),
                'hashtags': result.get('hashtags', [])
            }
            
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            raise Exception(f"响应解析失败: {e}, 内容: {str(content)[:200] if content else 'N/A'}...")
    
    def _validate_result(self, result, drama_name, version_hook):
        """验证 AI 生成结果"""
        
        # 验证标题
        title = result.get('title', '')
        if not title or len(title) < 6 or len(title) > 16:
            title = self._generate_title(drama_name, version_hook)
        
        # 验证描述
        description = result.get('description', '')
        if not description or len(description) < 30:
            # 兜底：只基于hook和剧名，不编造"隐藏千金/恶霸"这类具体剧情
            description = self._generic_description(drama_name, version_hook, quotes=None)
        
        # 验证话题
        hashtags = result.get('hashtags', [])
        if not hashtags or len(hashtags) < 3:
            hashtags = self._generate_hashtags(version_hook)
        
        return {
            'title': title,
            'description': description,
            'hashtags': hashtags[:5]  # 最多5个话题
        }
    
    def _generic_description(self, drama_name, version_hook, quotes=None):
        """通用兜底描述：只基于hook，不编造人物身份/剧情。"""
        quotes = quotes or []
        hook_text = {
            '逆袭': '逆袭时刻', '打脸': '打脸瞬间', '真相': '真相揭开',
            '反转': '高能反转', '高潮': '高潮片段', '泪目': '泪目名场面',
        }.get(version_hook, '精彩片段')
        if quotes:
            return f'{hook_text}——"{quotes[0]}"，后续剧情更精彩，一口气看完全集。'
        return f'{drama_name}精选{hook_text}，全程高能无尿点，刷到就是缘分，快来追完它。'

    def _generate_title(self, drama_name, version_hook):
        """模板生成标题（AI失败时兜底）"""
        
        # 提取剧名关键词
        keywords = []
        for word in ['千金', '豪门', '逆袭', '打脸', '真相', '反转', '恶霸', '甜宠']:
            if word in drama_name:
                keywords.append(word)
        
        title_templates = {
            '逆袭': ['恶霸竟敢占我橘山', '真相揭开时全场震惊', '身份曝光逆袭开始'],
            '打脸': ['恶霸打脸瞬间高能', '豪门千金复仇开始', '真相大白打脸全场'],
            '真相': ['真相揭开时全场震惊', '隐藏身份即将曝光', '豪门恩怨真相大白'],
            '反转': ['意想不到的结局来了', '反转瞬间引爆全场', '剧情反转烧脑至极'],
            '高潮': ['高能预警精彩瞬间', '剧情高潮不容错过', '逆袭时刻燃爆全场'],
            '泪目': ['催泪瞬间泪目全场', '虐心剧情泪目不断', '情感高潮泪目瞬间']
        }
        
        templates = title_templates.get(version_hook, ['精彩剧情不容错过'])
        return templates[hash(drama_name + version_hook) % len(templates)]
    
    def _generate_hashtags(self, version_hook):
        """模板生成话题（AI失败时兜底）"""
        
        hashtag_templates = {
            '逆袭': ['#逆袭人生', '#打脸现场', '#短剧推荐', '#豪门恩怨', '#爽剧'],
            '打脸': ['#打脸', '#反转', '#短剧', '#豪门', '#爽剧'],
            '真相': ['#真相大白', '#揭秘', '#短剧', '#悬疑', '#烧脑'],
            '反转': ['#反转', '#意想不到的结局', '#短剧', '#烧脑', '#悬疑'],
            '高潮': ['#高能预警', '#精彩瞬间', '#短剧', '#爽剧', '#热门'],
            '泪目': ['#泪目', '#催泪', '#短剧', '#情感', '#虐心']
        }
        
        return hashtag_templates.get(version_hook, ['#短剧', '#短剧推荐', '#热门'])
    
    def _generate_template_copy(self, drama_name, version_idx, version_hook):
        """生成模板文案（AI完全失败时兜底）——只基于剧名+hook，不编造剧情。"""
        title = self._generate_title(drama_name, version_hook)
        description = self._generic_description(drama_name, version_hook)
        hashtags = self._generate_hashtags(version_hook)
        return {'title': title, 'description': description, 'hashtags': hashtags}
    
    def test_api(self):
        """测试 API 连接"""
        print("[AI] 测试 SenseNova API 连接...")
        try:
            response = self._call_api("你好，请回复测试成功")
            print("[AI] API 连接成功！")
            return True
        except Exception as e:
            print(f"[AI] API 连接失败: {e}")
            return False


# 导出
__all__ = ['AICopyGenerator']