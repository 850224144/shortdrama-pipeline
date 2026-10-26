# -*- coding: utf-8 -*-
"""
AI 文案生成器 - 主用智谱 GLM-4-Flash（免费），备用 SenseNova，都失败走模板兜底
生成标题、描述、话题
"""

import os
import sys
import json
import time
import hashlib
import logging
import requests

def _setup_logger():
    """把 AI 文案日志落盘。

    原先用 logging 但全项目无 handler，[AI] 的 warning/error 只进 stderr，
    导致 AI 文案失败（历史 4/34）的原因永久查不到。
    整段包 try/except：日志调用自身抛错绝不能炸掉文案兜底路径。
    """
    lg = logging.getLogger('AICopyGenerator')
    if lg.handlers:
        return lg
    lg.setLevel(logging.INFO)
    lg.propagate = False
    try:
        log_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, f'ai_copy_{time.strftime("%Y%m%d")}.log')
        h = logging.FileHandler(log_file, encoding='utf-8')
        h.setFormatter(logging.Formatter(
            '[%(asctime)s] [%(levelname)s] [AICopy] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'))
        lg.addHandler(h)
    except OSError:
        pass
    return lg


logger = _setup_logger()

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

# 主文案模型（智谱 GLM-4-Flash，免费）：实测单次约 6s、70-86 token、零推理浪费。
# SenseNova 为备用（reasoning 模型，6 版并行约 40s，token 大部分花在推理上）。
# 未配置 GLM 则直接切 SenseNova；两个都没有走模板兜底。
ZHIPU_API_KEY = os.environ.get('ZHIPU_API_KEY', '')


class AICopyGenerator:
    """AI 文案生成器"""
    
    def __init__(self, api_key=SENSENOVA_API_KEY):
        self.api_base = SENSENOVA_API_BASE
        self.api_key = api_key
        self.model = SENSENOVA_MODEL
        # 任一模型可用就算启用；两个都没有才走纯模板兜底
        self.enabled = bool(SENSENOVA_API_KEY or ZHIPU_API_KEY)
        self.cache = {}  # 缓存已生成的文案
        # 智谱熔断：不可用（额度/限流/鉴权失效）后本会话内置 True，跳过智谱
        # 直接走 SenseNova 备用。到 cooldown 后半开自动再试一次，所以额度恢复后
        # 无需改代码、无需重启 webapp。
        self._zhipu_disabled = False
        self._zhipu_disabled_at = 0.0
        self._zhipu_fuse_cooldown = 900  # 秒
    
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
            # 主模型：智谱 GLM-4-Flash（免费）。实测单次约 6s、70-86 个 token、
            # reasoning_tokens=0，不需要重试；标题过短的问题由 _call_zhipu_copy
            # 内部的修正提示重试处理。SenseNova 已降为备用（见下方注释）。
            try:
                if not ZHIPU_API_KEY:
                    raise Exception('智谱 GLM 未配置')
                if not self._zhipu_available():
                    raise Exception('智谱熔断中（未到冷却期）')
                result = self._call_zhipu_copy(prompt)
            except Exception as e1:
                # SenseNova 备用（reasoning 模型：reasoning_effort='none' 被忽略，
                # 实测 1940 个 completion token 里 1841 个花在推理上，content 只剩
                # 186 字，经常 JSON 被截断，偶尔 content 字段整个缺失表现为
                # KeyError 'content'，所以自带重试一次）
                def _try_sensenova(p):
                    last = None
                    for attempt in (1, 2):
                        try:
                            return self._parse_response(self._call_api(p))
                        except Exception as e:
                            last = e
                            if attempt == 1:
                                logger.warning(f'[AI] SenseNova 第1次失败({e})，重试...')
                    raise last

                msg = str(e1)
                if self._trip_zhipu_fuse(msg):
                    logger.warning(
                        f'[AI] 智谱不可用({msg[:80]})，'
                        f'{self._zhipu_fuse_cooldown // 60}分钟内跳过智谱直接走 SenseNova')
                logger.warning(f'[AI] 智谱失败({msg[:80]})，试 SenseNova...')
                if not self.api_key:
                    raise  # 备用模型未配置，直接走模板兜底，不发无意义请求
                try:
                    result = _try_sensenova(prompt)
                except Exception as e2:
                    logger.warning(f'[AI] SenseNova 也失败({str(e2)[:80]})')
                    raise

            # 验证结果（GLM 成功和备用切换成功两条路径都会走到这里）
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

    # 熔断关键字：限流/额度类 + 鉴权类。401 实测错误体为
    # {'error': {'code': '401', 'message': '令牌已过期或验证不正确'}}（HTTP 401）
    _ZHIPU_FUSE_KEYWORDS = ('429', '1113', '1302', '1001', '401',
                            '余额', '额度', '令牌', 'quota', 'balance')

    def _zhipu_available(self):
        """智谱熔断状态：熔断后过冷却期自动半开，重新允许一次尝试。"""
        if not self._zhipu_disabled:
            return True
        if time.time() - self._zhipu_disabled_at >= self._zhipu_fuse_cooldown:
            self._zhipu_disabled = False
            logger.warning('[AI] 智谱熔断已过冷却期，重新允许尝试')
            return True
        return False

    def _trip_zhipu_fuse(self, msg):
        """错误属于限流/额度/鉴权类则熔断；返回 True 表示本次触发了熔断。"""
        if any(k in msg for k in self._ZHIPU_FUSE_KEYWORDS):
            self._zhipu_disabled = True
            self._zhipu_disabled_at = time.time()
            return True
        return False

    # 标题里不允许出现的标点（发布平台标题不接受标点）
    _TITLE_PUNCT = '，。！？、；：,.!?;:"\'“”‘’（）()《》<>【】[]…—·-~@#$%^&*+='

    @classmethod
    def _clean_title(cls, title):
        """去掉标题里的标点并去首尾空白。"""
        return ''.join(ch for ch in (title or '') if ch not in cls._TITLE_PUNCT).strip()

    @classmethod
    def _title_ok(cls, title):
        """标题合规：6-16 字且不含标点。"""
        return 6 <= len(cls._clean_title(title)) <= 16

    def _call_zhipu_copy(self, prompt, max_attempts=3):
        """调智谱，标题不合规（不足 6 字 / 超长 / 带标点）就追加修正提示重试。

        glm-4-flash 单次输出质量很差：实测 18 条里 17 条标题不足 6 字、7 条带
        问号等标点（"好惹？等着"、"新娘怒吼"、"等真相"）。放任的话
        _validate_result 只会静默换成模板标题，而模板是"恶霸/豪门千金"这类
        提示词明令禁止的编造剧情——等于 AI 白跑还更差。
        """
        base = prompt
        result = None
        for attempt in range(1, max_attempts + 1):
            result = self._parse_response(self._call_zhipu(prompt))
            title = self._clean_title(result.get('title'))
            if self._title_ok(title):
                return result
            raw = (result.get('title') or '').strip()
            issues = []
            if not 6 <= len(title) <= 16:
                issues.append(f'标题只有 {len(title)} 个字，必须写到 6-16 个字')
            if raw != title:
                issues.append(f'标题「{raw}」带了标点符号，标题里绝对不能有标点')
            logger.warning(f'[AI] 智谱标题不合规（{"，".join(issues)}）：第{attempt}/{max_attempts}次')
            if attempt < max_attempts:
                prompt = (base
                          + '\n\n【必须修正】你上次的输出不合格：' + '；'.join(issues)
                          + '。请严格重做：标题必须是 6 到 16 个汉字、完全不含任何'
                            '标点符号，并且要包含剧名或钩子主题，不要写这种通用碎片。')
        return result

    def _call_zhipu(self, prompt):
        """智谱GLM-4-Flash备用"""
        url = 'https://open.bigmodel.cn/api/paas/v4/chat/completions'
        headers = {
            'Authorization': f'Bearer {ZHIPU_API_KEY}',
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
        
        # 验证标题：先去掉标点（发布平台标题不接受标点），长度不合规再走模板
        title = self._clean_title(result.get('title'))
        if not title or not 6 <= len(title) <= 16:
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
        # 与 P1-D2 同类：hash() 有 PYTHONHASHSEED 盐值随机化，同一部剧在同一
        # 进程重启后可能拿到不同模板标题。改用稳定摘要保证重切结果一致。
        seed = int.from_bytes(
            hashlib.md5((drama_name + version_hook).encode('utf-8')).digest()[:4], 'big')
        return templates[seed % len(templates)]
    
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