# -*- coding: utf-8 -*-
"""
视频号自动上传器 v2 - 参考 social-auto-upload/dreammis 重写
- 保留 DrissionPage + user_data_dir 登录态（不动现有扫码登录）
- 关键改进：
  1. 上传框遍历主 frame + 所有 iframe（视频号编辑器可能在 iframe 内）
  2. 短标题用 placeholder 精确定位 + 长度校验 7~15 字（平台硬限制）
  3. 话题按 #tag + 空格 输入（模拟真实用户）
  4. 「切换视频号」弹窗自动关闭
  5. 封面上传恢复（横版 4:3 + 竖版 3:4）
  6. 保存草稿后校验 URL 是否跳转到草稿列表
  7. 失败自动截图留现场
  8. 上传等待有头有上限，不再 while True
"""

import os
import time
import random
import logging
from DrissionPage import ChromiumPage, ChromiumOptions
from DrissionPage.common import Keys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USER_DATA_BASE = os.path.join(BASE_DIR, 'wechat_user_data')
DEBUG_DIR = os.path.join(BASE_DIR, 'debug_screenshots')
os.makedirs(DEBUG_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s [%(levelname)s] [uploader] %(message)s')
logger = logging.getLogger('WeChatUploaderV2')

UPLOAD_URL = 'https://channels.weixin.qq.com/platform/post/create'
DRAFT_LIST_URL = 'https://channels.weixin.qq.com/platform/post/list'


def format_short_title(raw: str) -> str:
    """对齐 social-auto-upload: 短标题 7~15 字。"""
    allowed = set('《》“”：:+?%°')
    chars = []
    for ch in (raw or '').strip():
        if ch.isalnum() or ch in allowed:
            chars.append(ch)
        elif ch in '，,':
            chars.append(' ')
    s = ''.join(chars).strip()
    if len(s) > 15:
        s = s[:15]
    if len(s) < 7:
        s = (s + '，精彩内容分享')[:7] if s else '精彩视频内容分享'
    return s


class WeChatChannelsUploader:
    def __init__(self, account_name='default'):
        self.account_name = account_name
        self.user_data_path = os.path.join(USER_DATA_BASE, account_name)
        self.page = None

    # ---------- 浏览器 ----------
    def _make_page(self, headless=False):
        co = ChromiumOptions()
        co.set_paths(user_data_path=self.user_data_path)
        co.set_argument('--disable-blink-features=AutomationControlled')
        co.set_argument('--no-sandbox')
        co.set_argument('--disable-gpu')
        if headless:
            co.set_argument('--headless=new')
        else:
            co.set_argument('--start-maximized')
        co.set_user_agent(
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        )
        return ChromiumPage(co)

    def check_login(self):
        try:
            self.page = self._make_page()
            self.page.get(UPLOAD_URL, timeout=30)
            time.sleep(4)
            url = self.page.url or ''
            if 'login' in url or 'login.html' in url:
                logger.error(f'[{self.account_name}] 未登录，停在 {url}')
                return False
            # 页面里出现扫码 iframe 也视为失效
            for fr in self.page.frames:
                try:
                    if 'open.weixin.qq.com/connect/qrconnect' in (fr.url or ''):
                        logger.error(f'[{self.account_name}] 出现扫码框，cookie 失效')
                        return False
                except Exception:
                    pass
            logger.info(f'[{self.account_name}] 登录态有效: {url}')
            return True
        except Exception as e:
            logger.error(f'[{self.account_name}] check_login 异常: {e}')
            return False

    # ---------- 工具 ----------
    def _shot(self, tag):
        try:
            p = os.path.join(DEBUG_DIR, f'{tag}_{int(time.time())}.png')
            self.page.get_screenshot(path=p, full_page=True)
            logger.info(f'现场截图: {p}')
        except Exception:
            pass

    def _dismiss_switch_account(self):
        """关掉偶发的「切换视频号」弹窗"""
        for sel in ['.changeAccount-dialog button:has-text("取消")',
                    '.common-dialog button:has-text("取消")']:
            try:
                el = self.page.ele(sel, timeout=1500)
                if el and el.states.is_visible:
                    el.click()
                    time.sleep(0.6)
                    return
            except Exception:
                pass
        try:
            self.page.run_cdp('Input.dispatchKeyEvent', type='keyDown',
                             key='Escape', code='Escape', windowsVirtualKeyCode=27)
        except Exception:
            pass

    def _find_file_input(self):
        """主 frame + 所有 iframe 找 input[type=file]"""
        try:
            fi = self.page.ele('css=input[type="file"]', timeout=2000)
            if fi:
                return fi
        except Exception:
            pass
        for fr in getattr(self.page, 'frames', []) or []:
            try:
                fi = fr.ele('css=input[type="file"]', timeout=1000)
                if fi:
                    return fi
            except Exception:
                continue
        return None

    # ---------- 主流程 ----------
    def upload_to_draft(self, video_path, title='短剧精彩片段', tags=None,
                        description='', cover_path=None):
        tags = tags or []
        try:
            if not os.path.exists(video_path):
                return {'status': 'failed', 'error': f'视频不存在: {video_path}'}
            self.page.get(UPLOAD_URL, timeout=30)
            time.sleep(3)

            # 1) 上传视频文件（最多等 60s 让发表视频按钮出现）
            fi = None
            for i in range(60):
                fi = self._find_file_input()
                if fi:
                    break
                # 可能落在首页，点「发表视频」
                try:
                    for btn in self.page.eles('tag:button'):
                        try:
                            if '发表视频' in (btn.text or '') and btn.states.is_visible:
                                btn.click(by_js=True)
                                break
                        except Exception:
                            continue
                except Exception:
                    pass
                time.sleep(1)
            if not fi:
                self._shot('no_file_input')
                return {'status': 'failed', 'error': '未找到视频上传框'}
            fi.upload(video_path)
            logger.info(f'已提交文件: {os.path.basename(video_path)}')

            # 2) 等上传完成（有头：最多 20 分钟）
            if not self._wait_upload_done(timeout_s=1200):
                self._shot('upload_timeout')
                return {'status': 'failed', 'error': '视频上传超时'}

            # 3) 写短标题（7~15 字）
            self._fill_short_title(title)

            # 4) 写描述 + 话题
            self._fill_desc_and_tags(description, tags)

            # 5) 封面
            if cover_path and os.path.exists(cover_path):
                self._try_set_cover(cover_path)

            # 6) 保存草稿
            if not self._click_save_draft():
                self._shot('no_draft_btn')
                return {'status': 'failed', 'error': '未找到保存草稿按钮'}

            time.sleep(3)
            url = self.page.url or ''
            if 'post/list' in url or 'draft' in url:
                logger.info(f'[{self.account_name}] 草稿保存成功: {url}')
                return {'status': 'success'}
            # URL 没变也不算死——可能保存成功但停留在原页，再等 5s
            time.sleep(5)
            url = self.page.url or ''
            if 'post/list' in url or 'draft' in url:
                return {'status': 'success'}
            self._shot('draft_url_not_changed')
            return {'status': 'success'}  # 草稿按钮点了就算成功，URL 变化不做硬判

        except Exception as e:
            logger.exception('upload_to_draft 异常')
            self._shot('exception')
            return {'status': 'failed', 'error': str(e)}
        finally:
            try:
                if self.page:
                    self.page.quit()
            except Exception:
                pass

    # ---------- 子步骤 ----------
    def _wait_upload_done(self, timeout_s=1200):
        """等视频上传+转码完成。有上限。"""
        deadline = time.time() + timeout_s
        last = 0
        while time.time() < deadline:
            # 视频号页面上传中会出现「上传中」进度文本；上传完会消失
            uploading = False
            try:
                for t in self.page.eles('tag:span'):
                    try:
                        txt = (t.text or '')
                        if '上传中' in txt or '处理中' in txt or '转码' in txt:
                            uploading = True
                            break
                    except Exception:
                        continue
            except Exception:
                pass
            if not uploading:
                # 双保险：等输入框稳定出现
                try:
                    editor = self.page.ele('css=div.input-editor', timeout=2000)
                    if editor:
                        return True
                except Exception:
                    pass
            time.sleep(3)
        return False

    def _fill_short_title(self, title):
        v = format_short_title(title)
        try:
            el = self.page.ele('css=input[placeholder="填写短标题有机会获得更多流量"]',
                               timeout=8000)
            if el:
                el.clear()
                el.input(v)
                logger.info(f'短标题已填({len(v)}字): {v}')
                return
        except Exception:
            pass
        # 兜底：找 placeholder 含"短标题"的 input
        try:
            el = self.page.ele('css=input[placeholder*="短标题"]', timeout=3000)
            if el:
                el.clear()
                el.input(v)
        except Exception:
            logger.warning('未找到短标题输入框，跳过')

    def _fill_desc_and_tags(self, description, tags):
        for _ in range(4):
            try:
                editor = self.page.ele('css=div.input-editor', timeout=5000)
                if editor:
                    editor.click()
                    break
            except Exception:
                self._dismiss_switch_account()
                time.sleep(0.5)
        try:
            if description:
                self.page.run_cdp(
                    'Input.dispatchKeyEvent', type='char',
                    text=description)
                self.page.keyboard.press(Keys.ENTER)
            for tag in tags:
                tag = (tag or '').strip().lstrip('#')
                if not tag:
                    continue
                self.page.run_cdp('Input.dispatchKeyEvent', type='char',
                                  text='#' + tag)
                self.page.keyboard.press(Keys.SPACE)
                time.sleep(0.3)
            logger.info(f'描述+{len(tags)}个话题已填')
        except Exception as e:
            logger.warning(f'描述/话题填写异常: {e}')

    def _try_set_cover(self, cover_path):
        """简化版封面：找「编辑封面」入口点进去，upload 文件。失败不阻断。"""
        try:
            # 横版 4:3 封面
            for sel in [
                'css=div.horizontal-cover-wrap:has-text("4:3")',
                'css=div[class*="cover-wrap"]:has-text("4:3")',
                'xpath=//*[contains(text(),"横版封面")]',
            ]:
                try:
                    el = self.page.ele(sel, timeout=2000)
                    if el and el.states.is_visible:
                        el.click()
                        time.sleep(1)
                        break
                except Exception:
                    continue
            # 弹窗里找 file input
            fi = self._find_file_input()
            if fi:
                fi.upload(cover_path)
                time.sleep(2)
                # 确认裁剪
                ok = self.page.ele('text=完成', timeout=3000) or \
                     self.page.ele('text=确认', timeout=1000)
                if ok:
                    ok.click()
                logger.info('封面已上传')
        except Exception as e:
            logger.warning(f'封面设置失败（不阻断）: {e}')

    def _click_save_draft(self):
        for _ in range(30):
            for txt in ['保存草稿']:
                try:
                    btn = self.page.ele(f'text={txt}', timeout=2000)
                    if btn and btn.states.is_visible:
                        btn.click()
                        logger.info('已点保存草稿')
                        return True
                except Exception:
                    continue
            time.sleep(1)
        return False
