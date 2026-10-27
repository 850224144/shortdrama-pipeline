# -*- coding: utf-8 -*-
"""
视频号自动上传器 - 支持多账号、保存草稿
基于 DrissionPage (CDP协议，规避自动化检测)
"""

import os
import sys
import time
import ctypes
import hashlib
import logging
import random
from DrissionPage import ChromiumPage, ChromiumOptions
from DrissionPage.common import Keys

# 配置
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USER_DATA_BASE = os.path.join(BASE_DIR, 'wechat_user_data')

# 日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger('WeChatUploader')


class WeChatChannelsUploader:
    """视频号自动上传器"""

    COVER_EDITOR_SCRIPT = r"""
(() => {
    const normalize = value => (value || '').replace(/\s+/g, ' ').trim();
    const visible = element => {
        const rect = element.getBoundingClientRect();
        const style = getComputedStyle(element);
        return rect.width > 0 && rect.height > 0 &&
            style.display !== 'none' && style.visibility !== 'hidden' &&
            Number(style.opacity) !== 0;
    };
    const labels = ['编辑封面', '更换封面', '选择封面', '设置封面', '编辑'];
    const nodes = [...document.querySelectorAll(
        'button,[role="button"],a,[tabindex],div,span'
    )].filter(element => labels.includes(normalize(element.innerText || element.textContent)) &&
        visible(element));
    const candidates = nodes.map(element => {
        let cardRank = 100;
        let parent = element;
        for (let depth = 0; parent && depth <= 8; depth++, parent = parent.parentElement) {
            const text = normalize(parent.innerText || parent.textContent);
            if (parent.querySelector('img,video,canvas') && /封面预览/.test(text)) {
                cardRank = depth;
                break;
            }
            if (parent.querySelector('img,video,canvas') && /个人主页和分享卡片/.test(text)) {
                cardRank = Math.min(cardRank, depth + 10);
            }
        }
        const target = element.closest('button,[role="button"],a,[tabindex]') || element;
        return {element, target, cardRank, text: normalize(element.innerText || element.textContent)};
    });
    candidates.sort((left, right) =>
        left.cardRank - right.cardRank ||
        labels.indexOf(left.text) - labels.indexOf(right.text)
    );
    const candidate = candidates.find(item => item.cardRank < 100) ||
        candidates.find(item => item.text === '编辑') ||
        candidates[0];
    if (!candidate) return {found: false};
    candidate.target.setAttribute('data-codex-cover-editor', 'true');
    return {found: true, text: candidate.text, cardRank: candidate.cardRank};
})()
"""
    
    def __init__(self, account_name='default'):
        """
        初始化上传器
        
        Args:
            account_name: 账号名称（用于区分不同账号的登录数据）
        """
        self.account_name = account_name
        self.user_data_path = os.path.join(USER_DATA_BASE, account_name)
        self.page = None
    
    def _get_browser_options(self, headless=False):
        """配置浏览器选项"""
        co = ChromiumOptions()
        
        # 使用持久化用户配置目录
        co.set_paths(user_data_path=self.user_data_path)
        
        # 规避自动化检测
        co.set_argument('--disable-blink-features=AutomationControlled')
        co.set_argument('--no-sandbox')
        co.set_argument('--disable-gpu')
        
        if headless:
            co.set_argument('--headless=new')
        else:
            co.set_argument('--start-maximized')
        
        co.set_user_agent(
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/120.0.0.0 Safari/537.36'
        )
        
        return co
    
    def _human_type(self, element, text):
        """模拟人类打字"""
        for char in text:
            try:
                element.input(char)
                time.sleep(random.uniform(0.04, 0.12))
            except Exception as e:
                if "ElementLostError" in str(e) or "元素对象已失效" in str(e):
                    # 元素失效，重新获取
                    logger.warning("元素失效，重新获取输入框...")
                    time.sleep(1)
                    try:
                        element = self.page.ele('css:div.input-editor', timeout=5)
                        element.click()
                        time.sleep(0.5)
                        element.input(char)
                    except Exception as e2:
                        logger.error(f"重新获取元素失败: {e2}")
                        raise
                else:
                    raise

    def _find_visible_text_element(self, labels):
        """按可见文字找控件，忽略隐藏节点并优先返回精确匹配。"""
        for label in labels:
            try:
                candidates = self.page.eles(f'text:{label}')
            except Exception:
                continue

            visible = []
            for candidate in candidates:
                try:
                    if candidate and candidate.states.is_displayed:
                        text = (candidate.text or '').strip()
                        if label in text:
                            visible.append((text != label, candidate))
                except Exception:
                    continue
            if visible:
                visible.sort(key=lambda item: item[0])
                return visible[0][1]
        return None

    @staticmethod
    def _element_is_disabled(element):
        # 优先使用 DrissionPage 原生 states.is_enabled（4.1.x 推荐）
        try:
            states = getattr(element, 'states', None)
            if states is not None and hasattr(states, 'is_enabled'):
                return not bool(states.is_enabled)
        except Exception:
            pass
        # 回退到 HTML 属性检查
        try:
            classes = (element.attr('class') or '').lower()
            aria_disabled = (element.attr('aria-disabled') or '').lower()
            disabled = element.attr('disabled')
            return (
                disabled is not None and str(disabled).lower() != 'false'
            ) or aria_disabled == 'true' or 'disabled' in classes
        except Exception:
            return False

    def _find_publish_button(self):
        labels = ('发表', '发表视频', '发布', '发布视频', '发布作品')
        try:
            buttons = self.page.eles('tag:button')
        except Exception:
            buttons = []

        for label in labels:
            for button in buttons:
                try:
                    text = (button.text or '').strip()
                    if text == label:
                        return button
                except Exception:
                    continue
        return self._find_visible_text_element(labels)

    def _scroll_page_to_bottom(self):
        """滚动页面及内部面板，并把底部保存操作滚入视口。"""
        try:
            self.page.scroll.to_bottom()
        except Exception as e:
            logger.debug(f'DrissionPage 页面滚动失败: {e}')
        try:
            self.page.run_js(r"""
return (() => {
    const elements = [document.scrollingElement, document.body, document.documentElement,
        ...document.querySelectorAll('*')];
    for (const element of elements) {
        if (element && element.scrollHeight > element.clientHeight + 2) {
            element.scrollTop = element.scrollHeight;
        }
    }
    window.scrollTo(0, document.documentElement.scrollHeight);
    const labels = ['保存草稿', '保存为草稿', '存为草稿', '保存到草稿箱', '保存并退出', '存草稿'];
    const elements = [...document.querySelectorAll('button,[role="button"],a,[tabindex],div,span')];
    const draft = elements.find(element => labels.includes((element.innerText || element.textContent || '')
        .replace(/\s+/g, ' ').trim()) && element.getBoundingClientRect().width > 0);
    if (draft) draft.scrollIntoView({block: 'center', inline: 'nearest'});
    return {viewport: [window.innerWidth, window.innerHeight], draftFound: Boolean(draft)};
})()
""")
        except Exception as e:
            logger.debug(f'滚动草稿按钮入视口失败: {e}')

    def _save_debug_state(self, stage):
        if not self.page:
            return
        try:
            screenshot_path = os.path.join(self.user_data_path, f'debug_{stage}.png')
            self.page.get_screenshot(path=screenshot_path)
            logger.warning(f'调试截图已保存: {screenshot_path}')
        except Exception as e:
            logger.warning(f'保存调试截图失败: {e}')
        try:
            state = self.page.run_js(r"""
return (() => ({
    url: location.href,
    viewport: [window.innerWidth, window.innerHeight],
    controls: [...document.querySelectorAll('button,[role="button"],a')]
        .filter(element => {
            const rect = element.getBoundingClientRect();
            const style = getComputedStyle(element);
            return rect.width > 0 && rect.height > 0 &&
                style.display !== 'none' && style.visibility !== 'hidden';
        })
        .map(element => ({
            text: (element.innerText || element.textContent || '').trim().slice(0, 80),
            rect: (() => {
                const r = element.getBoundingClientRect();
                return [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)];
            })(),
            disabled: element.disabled || element.getAttribute('aria-disabled') === 'true'
        }))
        .slice(-40)
}))()
""")
            logger.warning(f'页面诊断信息: {state}')
        except Exception as e:
            logger.debug(f'读取页面诊断信息失败: {e}')

    def _wait_for_video_upload(self, timeout=300):
        """等待视频上传完成。优先等待发布按钮出现，只有长时间不出现时才检查保存草稿按钮。"""
        self._scroll_page_to_bottom()
        started = time.time()
        last_log = 0
        last_draft_check = 60  # 至少等待60秒才检查保存草稿按钮
        debug_count = 0
        last_debug_dump = 0
        
        while time.time() - started < timeout:
            # 1. 优先检查发布按钮（最可靠的判断）
            button = self._find_publish_button()
            if button:
                if not self._element_is_disabled(button):
                    logger.info('视频上传完成（发布按钮可用）')
                    return button
                else:
                    if time.time() - last_log >= 10:
                        logger.info('视频正在上传（发布按钮禁用）...')
                        last_log = time.time()
            
            # 2. 等待60秒后，才检查保存草稿按钮（备用判断）
            elapsed = time.time() - started
            if elapsed >= last_draft_check:
                self._scroll_page_to_bottom()
                draft_button = self._find_draft_button()
                if draft_button:
                    logger.info('视频发布表单已就绪（保存草稿按钮可用）')
                    return draft_button
            
            # 3. 日志
            if elapsed >= 30 and time.time() - last_log >= 10:
                logger.info(f'等待发布操作区加载... ({int(elapsed)}s)')
                last_log = time.time()
            
            # 4. 每120秒dump一次按钮文本（调试用）
            if elapsed >= 120 and time.time() - last_debug_dump >= 120:
                try:
                    buttons = self.page.eles('css:button')
                    texts = []
                    for b in buttons:
                        try:
                            t = (b.text or '').strip()
                            if t and len(texts) < 30:
                                texts.append(t)
                        except:
                            pass
                    logger.info(f'页面按钮文本采样: {texts[:20]}')
                    last_debug_dump = time.time()
                except Exception as e:
                    logger.debug(f'按钮文本dump失败: {e}')
            
            # 5. 每60秒保存一次调试截图
            if time.time() - last_log >= 60 and debug_count < 5:
                try:
                    screenshot_path = os.path.join(self.user_data_path, f'debug_waiting_{debug_count}.png')
                    self.page.get_screenshot(path=screenshot_path)
                    logger.info(f'调试截图已保存: {screenshot_path}')
                    debug_count += 1
                except Exception as e:
                    logger.error(f'保存调试截图失败: {e}')
            
            time.sleep(1)
        
        logger.error(f'等待视频上传完成超时（{timeout}秒）')
        self._save_debug_state('video_upload_timeout')
        return None

    def _find_cover_edit_button(self):
        logger.info('开始查找封面编辑按钮...')
        
        # 方法1：直接查找所有按钮，查找文本为"编辑"的按钮
        logger.info('尝试直接查找"编辑"按钮...')
        try:
            buttons = self.page.eles('tag:button')
            logger.info(f'找到 {len(buttons)} 个按钮')
            for button in buttons:
                try:
                    text = (button.text or '').strip()
                    logger.debug(f'检查按钮: {text}')
                    if text == '编辑':
                        logger.info(f'直接找到编辑按钮: {text}')
                        return button
                except Exception:
                    continue
        except Exception as e:
            logger.debug(f'直接查找编辑按钮失败: {e}')

        # 方法2：使用 CSS 选择器查找
        logger.info('尝试使用CSS选择器查找...')
        try:
            # 尝试查找包含"编辑"文本的元素
            elements = self.page.eles('css:span, div, button')
            for element in elements:
                try:
                    text = (element.text or '').strip()
                    if text == '编辑':
                        logger.info(f'通过CSS找到编辑元素: {text}')
                        return element
                except Exception:
                    continue
        except Exception as e:
            logger.debug(f'CSS查找失败: {e}')

        # 方法3：使用 JavaScript 简化脚本
        logger.info('尝试使用简化JavaScript脚本...')
        try:
            result = self.page.run_js('''
                (() => {
                    const buttons = [...document.querySelectorAll('button')];
                    const editButton = buttons.find(b => b.textContent.trim() === '编辑');
                    if (editButton) {
                        editButton.setAttribute('data-codex-cover-editor', 'true');
                        return {found: true, text: editButton.textContent.trim()};
                    }
                    return {found: false};
                })()
            ''')
            logger.info(f'简化JavaScript脚本结果: {result}')
            if isinstance(result, dict) and result.get('found'):
                button = self.page.ele('css:[data-codex-cover-editor="true"]', timeout=2)
                if button:
                    logger.info(f'通过JavaScript找到编辑按钮: {button.text}')
                    return button
        except Exception as e:
            logger.debug(f'简化JavaScript脚本失败: {e}')

        logger.info('尝试使用文本查找...')
        result = self._find_visible_text_element(
            ('更换封面', '编辑封面', '选择封面', '设置封面', '编辑')
        )
        if result:
            logger.info(f'通过文本查找找到按钮: {result.text}')
        return result

    def _find_cover_file_input(self):
        for _ in range(16):
            try:
                inputs = self.page.eles('css:input[type="file"]')
            except Exception:
                inputs = []

            image_inputs = []
            for file_input in inputs:
                try:
                    accept = (file_input.attr('accept') or '').lower()
                    if 'image' in accept or any(
                        extension in accept for extension in ('.jpg', '.jpeg', '.png', '.webp')
                    ):
                        image_inputs.append(file_input)
                except Exception:
                    continue

            if image_inputs:
                return image_inputs[-1]
            if len(inputs) > 1:
                return inputs[-1]
            if len(inputs) == 1:
                try:
                    accept = (inputs[0].attr('accept') or '').lower()
                    if 'video' not in accept and not any(
                        extension in accept for extension in ('.mp4', '.mov', '.m4v')
                    ):
                        return inputs[0]
                except Exception:
                    pass
            time.sleep(0.5)
        return None

    def _upload_cover(self, cover_path):
        cover_edit_button = self._find_cover_edit_button()
        if not cover_edit_button:
            logger.warning('封面预览卡片中未找到“编辑”入口')
            self._save_debug_state('cover_editor_missing')
            return False

        try:
            cover_edit_button.click()
            time.sleep(1)
            cover_input = self._find_cover_file_input()
            if not cover_input:
                upload_trigger = self._find_visible_text_element(
                    ('上传封面', '上传图片', '本地上传', '更换封面')
                )
                if upload_trigger:
                    upload_trigger.click()
                    time.sleep(0.5)
                    cover_input = self._find_cover_file_input()
            if not cover_input:
                logger.warning('封面编辑界面中未找到图片文件控件')
                self._save_debug_state('cover_input_missing')
                return False

            cover_input.input(os.path.abspath(cover_path))
            time.sleep(2)
            
            # 查找并点击"确认"或"完成"按钮关闭对话框
            confirm_button = self._find_visible_text_element(('确认', '完成', '确定'))
            if confirm_button:
                confirm_button.click()
                time.sleep(2)
                logger.info('已点击确认按钮关闭封面编辑对话框')
            else:
                # 尝试使用 JavaScript 关闭对话框
                try:
                    self.page.run_js('''
                        (() => {
                            const buttons = [...document.querySelectorAll('button')];
                            for (const button of buttons) {
                                const text = button.textContent.trim();
                                if (text === '确认' || text === '完成' || text === '确定') {
                                    button.click();
                                    return {clicked: true, text: text};
                                }
                            }
                            // 尝试关闭对话框
                            const closeButtons = [...document.querySelectorAll('[class*="close"], [class*="Close"], .close')];
                            if (closeButtons.length > 0) {
                                closeButtons[0].click();
                                return {clicked: true, type: 'close'};
                            }
                            return {clicked: false};
                        })()
                    ''')
                    time.sleep(1)
                    logger.info('已使用JavaScript关闭封面编辑对话框')
                except Exception as e:
                    logger.warning(f'关闭对话框失败: {e}')
            
            logger.info('封面图片已提交到预览')
            return True
        except Exception as e:
            logger.warning(f'封面上传失败: {e}')
            self._save_debug_state('cover_upload_failed')
            return False

    def _find_draft_button(self):
        """查找可见且可用的草稿按钮，不要点到隐藏模板节点。"""
        labels = (
            '保存草稿',
            '保存为草稿',
            '存为草稿',
            '保存到草稿箱',
            '保存并退出',
            '存草稿',
            '保存草稿箱',
            '保存至草稿箱',
            '存入草稿箱',
            '草稿',
        )
        
        # 方法1：使用 CSS 选择器遍历所有按钮
        logger.info('使用 CSS 选择器查找保存草稿按钮...')
        try:
            buttons = self.page.eles('css:button')
            logger.info(f'找到 {len(buttons)} 个按钮')
            for i, button in enumerate(buttons):
                try:
                    text = (button.text or '').strip()
                    logger.debug(f'按钮 {i}: "{text}"')
                    # 必须可见且未禁用：隐藏按钮点不到；禁用按钮说明视频还没
                    # 上传完（调用方会等待重试），此时点保存草稿没有意义。
                    if text in labels \
                            and button.states.is_displayed \
                            and not self._element_is_disabled(button):
                        logger.info(f'找到保存草稿按钮: "{text}" (索引 {i})')
                        return button
                except Exception:
                    continue
        except Exception as e:
            logger.debug(f'CSS 选择器查找失败: {e}')

        # 方法2：text 选择器（_find_visible_text_element 只查可见性，这里补禁用检查）
        button = self._find_visible_text_element(labels)
        if button and not self._element_is_disabled(button):
            return button

        # 方法3：使用 JavaScript 查找
        logger.debug('尝试通过可见按钮状态查找保存草稿操作...')
        try:
            result = self.page.run_js(r'''
                return (() => {
                    const labels = ['保存草稿', '保存为草稿', '存为草稿', '保存到草稿箱', '保存并退出', '存草稿'];
                    const nodes = [...document.querySelectorAll('button,[role="button"],a,[tabindex],div,span')];
                    for (const node of nodes) {
                        const text = (node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim();
                        const rect = node.getBoundingClientRect();
                        const style = getComputedStyle(node);
                        const visible = rect.width > 0 && rect.height > 0 &&
                            style.display !== 'none' && style.visibility !== 'hidden' &&
                            Number(style.opacity) !== 0;
                        const target = node.closest('button,[role="button"],a,[tabindex]') || node;
                        const targetStyle = getComputedStyle(target);
                        const disabled = target.disabled || target.getAttribute('aria-disabled') === 'true' ||
                            /disabled/i.test(target.className || '') || targetStyle.pointerEvents === 'none';
                        if (labels.includes(text) && visible && !disabled) {
                            target.setAttribute('data-codex-draft-button', 'true');
                            return {found: true, text: text};
                        }
                    }
                    return {found: false};
                })()
            ''')
            logger.debug(f'JavaScript查找结果: {result}')
            button = self.page.ele('css:[data-codex-draft-button="true"]', timeout=2)
            if button and not self._element_is_disabled(button):
                logger.info(f'通过JavaScript找到保存草稿按钮: {button.text}')
                return button
        except Exception as e:
            logger.debug(f'JavaScript查找保存草稿按钮失败: {e}')

        # 方法4：dump 页面全部按钮文本（调试用，仅当找不到时）
        try:
            buttons = self.page.eles('css:button')
            texts = []
            for b in buttons:
                try:
                    t = (b.text or '').strip()
                    if t and len(texts) < 30:
                        texts.append(t)
                except:
                    pass
            if texts:
                logger.info(f'未找到保存草稿按钮，当前页面按钮文本: {texts}')
        except Exception:
            pass

        return None

    def _wait_for_draft_saved(self, previous_url, title, timeout=20):
        """点击保存草稿后，导航到草稿箱检查标题是否存在——最可靠的验证。

        此前用「成功提示文案 / URL 跳转 / 保存按钮消失」三个信号判成功，
        但视频号是 wujie 微前端 SPA：保存草稿后发布页不跳转、无固定提示、
        按钮也未必消失，导致真实保存成功却被误判为失败（实际草稿已入箱）。
        现在改为到草稿箱 /platform/post/draftListManager 读取列表，确认标题
        真的出现才算保存成功。
        """
        draft_url = 'https://channels.weixin.qq.com/platform/post/draftListManager'
        started = time.time()
        # 给平台一点时间落库
        time.sleep(2)
        last_nav = 0
        while time.time() - started < timeout:
            try:
                # 若发布页已自己跳到草稿箱（SPA 路由），无需重复导航
                current_url = self.page.url or ''
                if 'draftlist' not in current_url.lower() and 'draft' not in current_url.lower():
                    if time.time() - last_nav >= 4:
                        self.page.get(draft_url)
                        last_nav = time.time()
                        time.sleep(4)
                # 递归读取草稿列表文本（含 iframe）
                body_text = self.page.run_js(r'''
                    return (() => {
                        const getText = (doc) => {
                            let t = doc.body ? doc.body.innerText : '';
                            for (const f of doc.querySelectorAll('iframe')) {
                                try {
                                    if (f.contentDocument) t += '\n' + getText(f.contentDocument);
                                } catch(e){}
                            }
                            return t;
                        };
                        return getText(document);
                    })()
                ''') or ''
                if title in body_text:
                    logger.info(f'草稿箱已找到标题: {title}')
                    return True
                # 没找到：草稿可能还在处理，等一会重新导航刷新列表
                time.sleep(3)
            except Exception as e:
                logger.debug(f'检查草稿箱失败: {e}')
                time.sleep(2)
        logger.warning(f'草稿箱未找到标题（timeout={timeout}s）: {title}')
        return False

    def _save_draft(self, title):
        logger.info('开始保存草稿...')
        
        # 步骤1: 关闭可能的通知弹窗
        try:
            notice = self._find_visible_text_element(('我知道了',))
            if notice:
                notice.click()
                logger.info('已关闭通知弹窗')
                time.sleep(0.5)
        except Exception:
            pass

        # 步骤2: 滚动到页面底部
        self._scroll_page_to_bottom()
        
        # 步骤3: 查找保存草稿按钮
        draft_button = self._find_draft_button()
        if not draft_button:
            logger.error('未找到可见的保存草稿按钮')
            self._save_debug_state('draft_button_missing')
            # 截图查看页面状态
            try:
                self.page.get_screenshot(path=os.path.join(self.user_data_path, 'debug_draft_button_missing.png'))
                logger.info('调试截图已保存: debug_draft_button_missing.png')
            except Exception as e:
                logger.error(f'保存调试截图失败: {e}')
            # 尝试通过 JavaScript 查找按钮
            logger.info('尝试通过 JavaScript 查找保存草稿按钮...')
            try:
                result = self.page.run_js(r'''
                    return (() => {
                        const labels = ['保存草稿', '保存为草稿', '存为草稿', '保存到草稿箱', '保存并退出', '存草稿'];
                        const nodes = [...document.querySelectorAll('button,[role="button"],a,[tabindex],div,span')];
                        for (const node of nodes) {
                            const text = (node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim();
                            if (labels.includes(text)) {
                                node.setAttribute('data-draft-btn', 'true');
                                return {found: true, text: text, tagName: node.tagName};
                            }
                        }
                        return {found: false};
                    })()
                ''')
                logger.info(f'JavaScript查找结果: {result}')
                if result and result.get('found'):
                    button = self.page.ele('css:[data-draft-btn="true"]', timeout=2)
                    if button:
                        logger.info(f'通过JavaScript找到按钮: {button.text}')
                        draft_button = button
            except Exception as e:
                logger.error(f'JavaScript查找失败: {e}')
            return False
        
        logger.info(f'找到保存草稿按钮: {draft_button.text}')
        
        # 步骤4: 点击保存草稿按钮
        # 页面是 wujie 微前端架构：发布表单渲染在 iframe[0]（empty.html）内，
        # 该 iframe 的 getBoundingClientRect 尺寸为 0，DrissionPage 的坐标点击
        # （element.click()）会算错坐标而静默失效。改为用 JS 在 iframe 内直接
        # 触发 button.click()，坐标点击仅作回退。
        try:
            previous_url = self.page.url
            js_clicked = self.page.run_js(r'''
                return (() => {
                    const labels = ['保存草稿','保存为草稿','存为草稿','保存到草稿箱','保存并退出','存草稿','保存草稿箱','保存至草稿箱','存入草稿箱'];
                    const findBtn = (doc) => {
                        for (const b of doc.querySelectorAll('button')) {
                            if (labels.includes((b.innerText||b.textContent||'').replace(/\s+/g,' ').trim())) {
                                return b;
                            }
                        }
                        for (const f of doc.querySelectorAll('iframe')) {
                            try {
                                if (f.contentDocument) {
                                    const r = findBtn(f.contentDocument);
                                    if (r) return r;
                                }
                            } catch(e){}
                        }
                        return null;
                    };
                    const btn = findBtn(document);
                    if (!btn) return false;
                    btn.click();
                    return true;
                })()
            ''')
            if js_clicked:
                logger.info(f'通过 JS 在 iframe 内点击保存草稿按钮，当前URL: {previous_url}')
            else:
                logger.warning('JS 未找到保存草稿按钮，回退到坐标点击')
                draft_button.click()
                logger.info(f'已用坐标点击保存草稿按钮，当前URL: {previous_url}')
        except Exception as e:
            logger.error(f'点击保存草稿按钮失败: {e}')
            self._save_debug_state('draft_button_click_failed')
            return False

        # 步骤5: 校验草稿真的保存成功
        # P1-D1：原实现点击后立即 return True——点击事件派发成功 ≠ 平台接受了
        # 草稿（标题超长/表单未就绪/内容校验失败都会静默失败），而 previous_url
        # 捕获了却从未使用。校验函数 _wait_for_draft_saved 此前零调用（只被测试
        # 引用），等于一直空转。现改为调用它：等成功提示文案、URL 跳转（draft/
        # 草稿列表且正文含本条标题）、或保存按钮消失，都没有才判失败。
        if not self._wait_for_draft_saved(previous_url, title):
            logger.error('草稿保存未确认：平台无成功提示/URL 未跳转/按钮未消失')
            self._save_debug_state('draft_save_unverified')
            return False

        logger.info(f'[{self.account_name}] ✅ 草稿保存成功')
        return True
    
    def _login_state(self):
        """读取当前页面的登录态，返回 'logged_in' / 'logged_out' / 'unknown'。

        不能只看 URL。channels.weixin.qq.com 的跳转链是：
            / → /platform/post/create（瞬态，<6s）→ /login.html（未登录最终态）
        只看 '/platform' in url 会把重定向链里的瞬态当成登录成功，二维码窗口
        几秒就被关掉——用户根本来不及扫码。
        两个权威信号：URL 含 'login'，或页面里挂着微信扫码 iframe（qrconnect）。

        注意：qrconnect iframe 检查必须放在 URL 判断之后——已登录后页面可能
        残留隐藏的 qrconnect iframe（被误判为未登录，如 15:40 那次）。URL 已
        在 /platform 内且不含 login 时直接判 logged_in，不再看 iframe。
        """
        try:
            url = (self.page.url or '').lower()
            if 'login' in url:
                return 'logged_out'
            if '/platform' in url:
                return 'logged_in'
            n = self.page.run_js(
                "return document.querySelectorAll('iframe[src*=qrconnect]').length")
            if n and int(n) > 0:
                return 'logged_out'
        except Exception as e:
            logger.warning(f"读取登录态失败: {e}")
        return 'unknown'

    @staticmethod
    def _pid_alive(pid):
        """进程是否还活着（Windows: OpenProcess + GetExitCodeProcess）。"""
        if not pid or pid <= 0:
            return False
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        h = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        code = ctypes.c_ulong()
        try:
            ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            return bool(ok and code.value == STILL_ACTIVE)
        finally:
            ctypes.windll.kernel32.CloseHandle(h)

    @staticmethod
    def _lock_holder(lock_path):
        """返回持有锁的存活 PID；锁不存在或持有进程已死返回 None。"""
        try:
            with open(lock_path, encoding='utf-8') as f:
                pid = int((f.read() or '').strip() or 0)
        except (OSError, ValueError):
            return None
        return pid if WeChatChannelsUploader._pid_alive(pid) else None

    def _acquire_profile_lock(self):
        """跨进程独占账号 profile 目录，成功返回锁文件路径；拿不到返回 None。

        两次 login() 共用同一 user_data_path 时，两个 Chrome 会互相踢掉对方的
        profile（表现为「再发起登录，登录页面没被打开」）。用 O_EXCL 原子创建
        的哨兵文件做硬互斥——比 msvcrt.locking 可靠（后者在 append 模式下
        seek 被 O_APPEND 忽略，实测加锁成功但解锁 PermissionError）。
        持有进程崩溃留下的死锁也能自动接管。
        """
        os.makedirs(self.user_data_path, exist_ok=True)
        p = os.path.join(self.user_data_path, 'profile.lock')
        for _ in range(4):
            try:
                fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    f.write(str(os.getpid()))
                return p
            except FileExistsError:
                if self._lock_holder(p) is None:     # 持有者已死，接管
                    try:
                        os.remove(p)
                    except OSError:
                        pass
                    continue
                return None
            except OSError:
                return None
        return None

    def _release_profile_lock(self, lock_path):
        if not lock_path:
            return
        try:
            os.remove(lock_path)
        except OSError:
            pass

    @staticmethod
    def is_login_in_progress(account_name):
        """该账号是否已有登录进程在用 profile（供 Web 端前置检查）。"""
        return WeChatChannelsUploader._lock_holder(
            os.path.join(USER_DATA_BASE, account_name, 'profile.lock')) is not None

    def _quit_page(self):
        """安全关闭浏览器页面。"""
        if self.page:
            try:
                self.page.quit()
            except Exception as e:
                logger.warning(f"关闭浏览器时出错: {e}")
            self.page = None

    def login(self):
        """扫码登录。窗口会一直开着直到真的扫上码或超时。"""
        logger.info(f"[{self.account_name}] 启动登录模式...")
        os.makedirs(self.user_data_path, exist_ok=True)

        lock = self._acquire_profile_lock()
        if lock is None:
            logger.error(f"[{self.account_name}] 已有登录进程在使用该账号，本次放弃")
            return False

        try:
            co = self._get_browser_options(headless=False)
            try:
                self.page = ChromiumPage(co)
            except Exception as e:
                logger.error(f"浏览器启动失败: {e}")
                return False

            logger.info("正在打开微信视频号助手首页...")
            try:
                self.page.get("https://channels.weixin.qq.com")
            except Exception as e:
                logger.error(f"打开首页失败: {e}")
                return False

            logger.info("请在弹出的浏览器窗口中完成微信扫码登录。等待扫码...")

            # 等真登录：状态需连续 3 秒稳定为 logged_in。
            # 单次读到 /platform 不算——那可能是重定向链里的瞬态。
            timeout_seconds = 300
            stable = 0
            for i in range(timeout_seconds):
                st = self._login_state()
                if st == 'logged_in':
                    stable += 1
                    if stable >= 3:
                        logger.info(f"[{self.account_name}] 登录成功！")
                        break
                else:
                    stable = 0
                if st != 'unknown':
                    logger.info(f"[{self.account_name}] 当前状态: {st} | {self.page.url}")
                time.sleep(1)
            else:
                logger.error("登录超时：未在窗口内检测到登录完成。")
                return False

            # 登录态要落盘（cookie / localStorage 写回 profile），留足时间。
            time.sleep(5)
            logger.info(f"[{self.account_name}] 登录数据已保存")
            return True
        finally:
            self._quit_page()
            self._release_profile_lock(lock)

    def check_login(self):
        """检查登录状态（headless，不弹窗口）。"""
        if not os.path.exists(self.user_data_path):
            logger.error(f"未找到登录目录，请先登录: {self.user_data_path}")
            return False

        # 有登录进程在用 profile 时，headless 去抢会失败/读到半成品状态。
        if self.is_login_in_progress(self.account_name):
            logger.warning(f"[{self.account_name}] 登录进行中，暂无法校验")
            return False

        logger.info(f"[{self.account_name}] 验证登录状态...")
        lock = self._acquire_profile_lock()
        if lock is None:
            logger.warning(f"[{self.account_name}] 登录进行中，暂无法校验")
            return False

        try:
            co = self._get_browser_options(headless=True)
            try:
                self.page = ChromiumPage(co)
            except Exception as e:
                logger.error(f"浏览器启动失败: {e}")
                return False

            try:
                self.page.get("https://channels.weixin.qq.com")
                self._handle_alerts()
            except Exception as e:
                logger.warning(f"处理启动弹窗时出错: {e}")

            # 等跳转链走到底：未登录最终态是 /login.html（可能经数秒瞬态）。
            # 只看最终态判断，避免把瞬态 /platform 误判为登录成功。
            last = ''
            for _ in range(12):
                try:
                    cur = self.page.url or ''
                except Exception:
                    cur = ''
                if cur == last:
                    break
                last = cur
                time.sleep(1)

            st = self._login_state()
            logger.info(f"[{self.account_name}] 登录态={st} | {last}")
            if st == 'logged_in':
                logger.info(f"[{self.account_name}] ✅ 登录有效")
                return True

            # 二次确认：未登录态再等一轮，排除跳转未完成
            time.sleep(3)
            st2 = self._login_state()
            if st2 == 'logged_in':
                logger.info(f"[{self.account_name}] ✅ 登录有效（二次确认）")
                return True

            logger.error(f"[{self.account_name}] ❌ 登录失效（状态={st2}）")
            return False
        finally:
            self._quit_page()
            self._release_profile_lock(lock)
    
    def _handle_alerts(self):
        """处理浏览器弹窗（包括原生弹窗和页面弹窗）"""
        try:
            # 处理浏览器原生弹窗（alert, confirm, prompt）
            while True:
                try:
                    # 检查是否有原生弹窗
                    if self.page.alert:
                        self.page.alert.accept()
                        logger.info('已接受原生弹窗')
                        time.sleep(0.5)
                        continue
                    
                    if self.page.confirm:
                        self.page.confirm.accept()
                        logger.info('已接受确认弹窗')
                        time.sleep(0.5)
                        continue
                except Exception:
                    pass
                
                # 处理页面内的弹窗（按钮点击）
                # 尝试获取"我知道了"按钮
                alert = self.page.ele('text:我知道了', timeout=0.5)
                if alert:
                    alert.click()
                    logger.info('已关闭弹窗: 我知道了')
                    time.sleep(0.5)
                    continue
                
                # 尝试获取"知道了"按钮
                alert = self.page.ele('text:知道了', timeout=0.5)
                if alert:
                    alert.click()
                    logger.info('已关闭弹窗: 知道了')
                    time.sleep(0.5)
                    continue
                
                # 检查是否有对话框
                dialog = self.page.ele('css:.ant-modal', timeout=0.5)
                if dialog:
                    close_btn = self.page.ele('css:.ant-modal-close', timeout=0.5)
                    if close_btn:
                        close_btn.click()
                        logger.info('已关闭对话框')
                        time.sleep(0.5)
                        continue
                
                # 没有弹窗了，退出循环
                break
        except Exception as e:
            logger.debug(f'处理弹窗时出错: {e}')
    
    def upload_to_draft(self, video_path, title, tags, description, cover_path=None):
        """
        上传视频到草稿箱
        
        Args:
            video_path: 视频文件路径
            title: 标题（6-16字，无标点）
            tags: 话题标签列表
            description: 描述
            cover_path: 封面图路径（可选）
        
        Returns:
            True/False
        """
        logger.info(f"[{self.account_name}] 开始上传到草稿箱...")
        
        # 检查文件
        abs_file_path = os.path.abspath(video_path)
        if not os.path.exists(abs_file_path):
            logger.error(f"找不到视频文件: {abs_file_path}")
            return False
        
        # 检查登录
        if not self.check_login():
            logger.error("登录失效，请先登录")
            return False
        
        # 启动浏览器
        co = self._get_browser_options(headless=False)
        try:
            self.page = ChromiumPage(co)
        except Exception as e:
            logger.error(f"浏览器启动失败: {e}")
            return False
        
        try:
            # 打开发布页面
            publish_url = "https://channels.weixin.qq.com/platform/post/create"
            logger.info(f"打开发布页面: {publish_url}")
            self.page.get(publish_url)
            
            time.sleep(5)  # 增加等待时间
            
            # 验证登录
            current_url = self.page.url
            if "login" in current_url or "/platform" not in current_url:
                logger.error("登录状态已失效")
                return False
            
            # 1. 上传视频
            logger.info("定位文件上传控件...")
            file_input = None
            
            # 直接查找文件上传控件，不点击"发表视频"按钮
            # 等待文件上传控件出现
            for attempt in range(10):
                try:
                    file_input = self.page.ele('xpath://input[@type="file"]', timeout=3)
                    if file_input:
                        logger.info(f"第 {attempt+1} 次尝试找到文件上传控件")
                        break
                except Exception:
                    time.sleep(1)
            
            if not file_input:
                logger.error("未找到文件上传控件，尝试CSS选择器...")
                try:
                    file_input = self.page.ele('css:input[type="file"]', timeout=10)
                    if file_input:
                        logger.info("通过CSS选择器找到文件上传控件")
                except Exception as e:
                    logger.error(f"CSS选择器也失败: {e}")
            
            if not file_input:
                # 尝试查找所有input元素
                try:
                    inputs = self.page.eles('css:input')
                    logger.info(f"找到 {len(inputs)} 个input元素")
                    for i, inp in enumerate(inputs):
                        try:
                            inp_type = inp.attr('type') or ''
                            logger.info(f"  input[{i}]: type={inp_type}")
                            if inp_type == 'file':
                                file_input = inp
                                logger.info(f"找到文件上传控件: input[{i}]")
                                break
                        except Exception:
                            continue
                except Exception as e:
                    logger.error(f"查找input元素失败: {e}")
            
            if not file_input:
                logger.error("未找到文件上传控件")
                # 截图调试
                try:
                    screenshot_path = os.path.join(self.user_data_path, 'debug_upload_page.png')
                    self.page.get_screenshot(path=screenshot_path)
                    logger.info(f"调试截图已保存: {screenshot_path}")
                except Exception:
                    pass
                return False
            
            logger.info(f"上传视频: {abs_file_path}")
            file_input.input(abs_file_path)
            time.sleep(3)
            
            # 2. 填写文案
            logger.info("填写文案...")
            try:
                editor = self.page.ele('css:div.input-editor', timeout=30)
            except Exception:
                logger.error("未找到富文本输入框")
                return False
            
            # 聚焦并清空
            editor.click()
            self.page.run_js("arguments[0].innerText = '';", editor)
            time.sleep(1)
            editor.click()
            time.sleep(0.5)
            
            # 1. 输入短标题
            logger.info(f"输入短标题: {title}")
            self._human_type(editor, title)
            editor.input(Keys.ENTER)
            time.sleep(0.5)
            
            # 2. 输入长描述
            if description:
                logger.info(f"输入描述: {description[:50]}...")
                self._human_type(editor, description)
                editor.input(Keys.ENTER)
                time.sleep(0.5)
            
            # 3. 输入话题（确保tags是list类型）
            if tags and isinstance(tags, list):
                logger.info(f"输入话题: {tags}")
                for tag in tags:
                    clean_tag = tag if tag.startswith('#') else f'#{tag}'
                    self._human_type(editor, f'{clean_tag} ')
                    time.sleep(0.8)
                editor.input(Keys.ENTER)
                time.sleep(0.5)
            
            # 4. 等待视频上传完成（超时基于文件大小动态计算）
            file_size_mb = os.path.getsize(abs_file_path) / (1024 * 1024)
            # 预估：每秒 0.5MB 上传速度，加 120 秒余量，最小 300 秒，最大 900 秒
            upload_timeout = max(300, min(900, int(file_size_mb * 2) + 120))
            logger.info(f'视频 {file_size_mb:.0f}MB，等待超时 {upload_timeout} 秒')
            logger.info('等待视频上传完成...')
            video_uploaded = self._wait_for_video_upload(timeout=upload_timeout)
            if not video_uploaded:
                logger.error('等待视频上传完成超时，终止上传')
                # 截图调试
                try:
                    screenshot_path = os.path.join(self.user_data_path, 'debug_video_upload_timeout.png')
                    self.page.get_screenshot(path=screenshot_path)
                    logger.info(f'调试截图已保存: {screenshot_path}')
                except Exception as e:
                    logger.error(f'保存调试截图失败: {e}')
                # 不继续上传，直接返回失败
                return False
            
            # 再次检查视频是否上传完成
            time.sleep(3)
            current_url = self.page.url
            logger.info(f'视频上传后URL: {current_url}')
            
            # 检查页面上是否有保存草稿按钮
            draft_button = self._find_draft_button()
            if draft_button:
                logger.info('找到保存草稿按钮，视频可能已上传完成')
            else:
                logger.warning('未找到保存草稿按钮，视频可能还在上传中')
                # 再等待一段时间
                time.sleep(30)
                draft_button = self._find_draft_button()
                if not draft_button:
                    logger.error('保存草稿按钮仍未找到，视频可能未上传完成')
                    self._save_debug_state('draft_button_missing')
                    return False

            # 5. 上传封面（可选）- 暂时跳过，专注于保存草稿
            cover_uploaded = False
            # 暂时跳过封面上传，专注于保存草稿
            # if cover_path:
            #     abs_cover_path = os.path.abspath(cover_path)
            #     if os.path.isfile(abs_cover_path):
            #         logger.info(f'上传封面: {abs_cover_path}')
            #         cover_uploaded = self._upload_cover(abs_cover_path)
            #     else:
            #         logger.warning(f'找不到封面文件: {abs_cover_path}')
            logger.info('暂时跳过封面上传，专注于保存草稿')

            # 6. 保存草稿
            logger.info('保存草稿...')
            saved = self._save_draft(title)
            
            if saved:
                logger.info(f'[{self.account_name}] ✅ 草稿保存成功')
                return True
            else:
                logger.error(f'[{self.account_name}] ❌ 草稿保存失败')
                # 保存失败时不关闭页面，方便调试
                logger.warning('页面保持打开，方便检查问题')
                self._save_debug_state('draft_save_failed')
                return False
        finally:
            # 确保浏览器实例被正确清理
            if self.page:
                try:
                    self.page.quit()
                except Exception as e:
                    logger.warning(f'关闭浏览器时出错: {e}')
                self.page = None
    
    def get_next_account(self, accounts, drama_name):
        """
        获取下一个账号（简单轮换）
        
        Args:
            accounts: 账号列表
            drama_name: 剧名
        
        Returns:
            账号名称
        """
        if not accounts:
            return 'default'

        # P1-D2：hash() 对 str 有 PYTHONHASHSEED 盐值随机化，同一部剧在不同进程
        # 里会选到不同账号（进程一重启分配就变），跨进程完全不稳定。
        # 改用稳定摘要：同一剧名在任何进程、任何平台上都固定选到同一账号。
        idx = int.from_bytes(
            hashlib.md5(drama_name.encode('utf-8')).digest()[:4], 'big') % len(accounts)
        return accounts[idx]


# 导出
__all__ = ['WeChatChannelsUploader']
