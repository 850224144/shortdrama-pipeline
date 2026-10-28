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

COVER_EDITOR_OPEN_JS = r'''
(() => {
    const normalize = v => (v || '').replace(/\s+/g, ' ').trim();
    const seen = new WeakSet();
    const walk = root => {
        if (!root || seen.has(root)) return [];
        seen.add(root);
        let out = [];
        let nodes = [];
        try { nodes = [...root.querySelectorAll('*')]; } catch (e) {}
        out = out.concat(nodes);
        for (const n of nodes) { if (n.shadowRoot) out = out.concat(walk(n.shadowRoot)); }
        for (const f of root.querySelectorAll('iframe,frame')) {
            try { if (f.contentDocument) out = out.concat(walk(f.contentDocument)); } catch (e) {}
        }
        return out;
    };
    const all = walk(document);
    const visible = n => {
        if (!n || !n.getBoundingClientRect) return false;
        const r = n.getBoundingClientRect();
        const st = n.ownerDocument ? n.ownerDocument.defaultView.getComputedStyle(n) : null;
        return r.width > 0 && r.height > 0 && st && st.display !== 'none' &&
            st.visibility !== 'hidden' && Number(st.opacity || 1) > 0;
    };
    const labels = ['编辑封面', '更换封面', '选择封面', '设置封面'];
    // 1) 文本精确命中且可见的打开按钮
    for (const n of all) {
        const t = normalize(n.innerText || n.textContent);
        if (labels.includes(t) && visible(n)) {
            const btn = n.closest('button,[role="button"],a,[tabindex]') || n;
            btn.click();
            return {found: true, text: t};
        }
    }
    // 2) 回退：纯“编辑”文本且靠近封面预览卡片的元素
    for (const n of all) {
        const t = normalize(n.innerText || n.textContent);
        if (t === '编辑' && visible(n)) {
            let p = n, ok = false;
            for (let d = 0; p && d <= 6; d++, p = p.parentElement) {
                if (p.querySelector && p.querySelector('img,video,canvas') &&
                    /封面|分享卡片/.test(normalize(p.innerText || ''))) { ok = true; break; }
            }
            if (ok) {
                const btn = n.closest('button,[role="button"],a,[tabindex]') || n;
                btn.click();
                return {found: true, text: '编辑'};
            }
        }
    }
    return {found: false};
})()
'''

COVER_EDITOR_STATE_JS = r'''
(() => {
    const normalize = v => (v || '').replace(/\s+/g, ' ').trim();
    const seen = new WeakSet();
    const walk = root => {
        if (!root || seen.has(root)) return [];
        seen.add(root);
        let out = [];
        let nodes = [];
        try { nodes = [...root.querySelectorAll('*')]; } catch (e) {}
        out = out.concat(nodes);
        for (const n of nodes) { if (n.shadowRoot) out = out.concat(walk(n.shadowRoot)); }
        for (const f of root.querySelectorAll('iframe,frame')) {
            try { if (f.contentDocument) out = out.concat(walk(f.contentDocument)); } catch (e) {}
        }
        return out;
    };
    const all = walk(document);
    const images = all
        .filter(n => n.tagName === 'IMG')
        .map(n => ({src: n.src || '', cls: String(n.className || ''), natural: n.naturalWidth || 0}));
    const dialogs = all.filter(n => {
        const t = normalize(n.innerText || n.textContent);
        if (!t || t.length > 400) return false;
        const role = (n.getAttribute && n.getAttribute('role')) || '';
        const cls = String(n.className || '');
        const looksDialog = role === 'dialog' || /dialog|modal|popup|popover/i.test(cls);
        return looksDialog && /编辑封面|更换封面|上传封面|选择封面|设置封面/.test(t);
    });
    dialogs.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
    const dialog = dialogs[0];
    const dialogText = dialog ? normalize(dialog.innerText || '') : '';
    const dialogImgs = dialog
        ? [...dialog.querySelectorAll('img')].map(n => ({src: n.src || '', natural: n.naturalWidth || 0}))
        : [];
    let confirm = null;
    if (dialog) {
        const nodes = [...dialog.querySelectorAll('button,[role="button"],a,span,div')];
        confirm = nodes.find(b => normalize(b.innerText || b.textContent) === '确认') || null;
    }
    const rect = confirm ? confirm.getBoundingClientRect() : null;
    return {
        dialog_open: !!dialog,
        dialog_text: dialogText.slice(0, 160),
        dialog_image_count: dialogImgs.length,
        dialog_images: dialogImgs.slice(0, 4),
        image_count: images.length,
        images: images.slice(0, 8),
        confirm_visible: !!(rect && rect.width > 0 && rect.height > 0),
        confirm_disabled: !!(confirm && (confirm.disabled ||
            (confirm.getAttribute && confirm.getAttribute('aria-disabled') === 'true'))),
        has_error: /失败|错误|请选择|重新上传/.test(dialogText),
    };
})()
'''

COVER_EDITOR_APPLY_JS = r'''
(() => {
    const normalize = v => (v || '').replace(/\s+/g, ' ').trim();
    const seen = new WeakSet();
    const walk = root => {
        if (!root || seen.has(root)) return [];
        seen.add(root);
        let out = [];
        let nodes = [];
        try { nodes = [...root.querySelectorAll('*')]; } catch (e) {}
        out = out.concat(nodes);
        for (const n of nodes) { if (n.shadowRoot) out = out.concat(walk(n.shadowRoot)); }
        for (const f of root.querySelectorAll('iframe,frame')) {
            try { if (f.contentDocument) out = out.concat(walk(f.contentDocument)); } catch (e) {}
        }
        return out;
    };
    const all = walk(document);
    const dialogs = all.filter(n => {
        const t = normalize(n.innerText || n.textContent);
        if (!t || t.length > 400) return false;
        const role = (n.getAttribute && n.getAttribute('role')) || '';
        const cls = String(n.className || '');
        const looksDialog = role === 'dialog' || /dialog|modal|popup|popover/i.test(cls);
        return looksDialog && /编辑封面|更换封面|上传封面|选择封面|设置封面/.test(t);
    });
    dialogs.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
    const dialog = dialogs[0];
    if (!dialog) return {applied: false, reason: 'dialog_not_found'};
    const applyLabels = ['确认', '完成', '确定', '保存', '使用封面', '应用'];
    const buttons = [...dialog.querySelectorAll('button,[role="button"],a,span,div')];
    for (const label of applyLabels) {
        const btn = buttons.find(b => normalize(b.innerText || b.textContent) === label && !b.disabled);
        if (btn) {
            btn.click();
            return {applied: true, text: label};
        }
    }
    return {applied: false, reason: 'apply_not_found',
            dialog_text: normalize(dialog.innerText || '').slice(0, 160)};
})()
'''

COVER_EDITOR_IMAGE_CHANGED_JS = r'''
(() => {
    const normalize = v => (v || '').replace(/\s+/g, ' ').trim();
    const seen = new WeakSet();
    const walk = root => {
        if (!root || seen.has(root)) return [];
        seen.add(root);
        let out = [];
        let nodes = [];
        try { nodes = [...root.querySelectorAll('*')]; } catch (e) {}
        out = out.concat(nodes);
        for (const n of nodes) { if (n.shadowRoot) out = out.concat(walk(n.shadowRoot)); }
        for (const f of root.querySelectorAll('iframe,frame')) {
            try { if (f.contentDocument) out = out.concat(walk(f.contentDocument)); } catch (e) {}
        }
        return out;
    };
    const all = walk(document);
    const dialogs = all.filter(n => {
        const t = normalize(n.innerText || n.textContent);
        if (!t || t.length > 400) return false;
        const role = (n.getAttribute && n.getAttribute('role')) || '';
        const cls = String(n.className || '');
        const looksDialog = role === 'dialog' || /dialog|modal|popup|popover/i.test(cls);
        return looksDialog && /编辑封面|更换封面|上传封面|选择封面|设置封面/.test(t);
    });
    dialogs.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
    const dialog = dialogs[0];
    // 封面预览主图：弹窗内自然尺寸最大的 img，或全局第一个尺寸合理的 img
    let best = null, bestArea = 0;
    for (const img of all) {
        if (img.tagName !== 'IMG') continue;
        const r = img.getBoundingClientRect();
        const area = r.width * r.height;
        if (area > bestArea && r.width > 0) { best = img; bestArea = area; }
    }
    const main = best ? {src: best.src || '', w: best.naturalWidth || 0, cls: String(best.className || '')} : null;
    return {dialog_open: !!dialog, main: main, dialog_image_count: dialog ? [...dialog.querySelectorAll('img')].length : 0};
})()
'''

# 封面预览卡片指纹（不依赖弹窗）：锁定“封面预览/分享卡片”容器内的主图。
# 应用成功后该卡片主图的 src 会变化；用它做 before/after 比较，
# 避免之前“取全页面最大图”在弹窗开关时产生假阳性 applied。
COVER_CARD_FP_JS = r'''
(() => {
    const normalize = v => (v || '').replace(/\s+/g, ' ').trim();
    const seen = new WeakSet();
    const walk = root => {
        if (!root || seen.has(root)) return [];
        seen.add(root);
        let out = [];
        let nodes = [];
        try { nodes = [...root.querySelectorAll('*')]; } catch (e) {}
        out = out.concat(nodes);
        for (const n of nodes) { if (n.shadowRoot) out = out.concat(walk(n.shadowRoot)); }
        for (const f of root.querySelectorAll('iframe,frame')) {
            try { if (f.contentDocument) out = out.concat(walk(f.contentDocument)); } catch (e) {}
        }
        return out;
    };
    const all = walk(document);
    const cards = all.filter(n => {
        const t = normalize(n.innerText || n.textContent);
        if (!t || t.length > 400) return false;
        if (!/封面预览|个人主页和分享卡片|分享卡片/.test(t)) return false;
        return !!(n.querySelector && n.querySelector('img'));
    });
    // 取最具体的卡片（文本最短）
    cards.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
    const card = cards[0];
    if (!card) return {found: false};
    const imgs = [...card.querySelectorAll('img')].filter(i => {
        const r = i.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    });
    let best = null, bestArea = 0;
    for (const img of imgs) {
        const r = img.getBoundingClientRect();
        const area = r.width * r.height;
        if (area > bestArea) { best = img; bestArea = area; }
    }
    return {
        found: true,
        img_count: imgs.length,
        main: best ? {src: best.src || '', w: best.naturalWidth || 0,
                      h: best.naturalHeight || 0} : null,
    };
})()
'''

    

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
            # Chrome 155 的 headless 模式与 DrissionPage 4.1.1.4 不兼容：
            # --headless=new 启动后 CDP 连接 404，co.headless()/旧版 --headless 同样失败。
            # 回退为有头模式（实测能正常启动），check_login 预检会短暂弹窗（10-20s），可接受。
            # 若要恢复真 headless：升级 DrissionPage 或降级 Chrome，且需实测通过。
            co.set_argument('--start-maximized')
        else:
            co.set_argument('--start-maximized')
        
        co.set_user_agent(
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/120.0.0.0 Safari/537.36'
        )
        
        return co
    
    def _new_page(self, co):
        """创建页面并启用自动处理原生提示框（alert/confirm）。
        accept=True：原生 confirm 自动点"确定"（微信"将此次编辑保留? 不保存保存"的
        确定=保存草稿），避免 DrissionPage AlertExistsError 拦截所有 JS 操作。"""
        page = ChromiumPage(co)
        try:
            page.set.auto_handle_alert(on_off=True, accept=True)
        except Exception as e:
            logger.debug(f'设置自动处理提示框失败: {e}')
        return page
    
    def _handle_draft_popup(self):
        """处理"将此次编辑保留? 不保存保存"弹窗——点"保存"（=保存草稿）。
        原生 confirm 由 auto_handle_alert accept 自动处理；此处兜底点 DOM 弹窗的"保存"。"""
        try:
            result = self.page.run_js(r'''
            return (() => {
                const normalize = v => (v||'').replace(/\s+/g,' ').trim();
                const popups = [...document.querySelectorAll(
                    '.weui-desktop-dialog,[role="dialog"],.weui-desktop-popover,.weui-desktop-dialog__wrp'
                )];
                for (const popup of popups) {
                    const text = normalize(popup.innerText||popup.textContent);
                    if (text.includes('将此次编辑保留') || text.includes('使用定时发表')) {
                        const btns = [...popup.querySelectorAll('button,[role="button"],a,span')];
                        const saveBtn = btns.find(b => normalize(b.innerText||b.textContent)==='保存');
                        if (saveBtn) { saveBtn.click(); return 'saved'; }
                    }
                }
                return 'none';
            })()
            ''')
            if result == 'saved':
                logger.info('离开确认弹窗已点"保存"（保存草稿）')
            return result
        except Exception as e:
            logger.debug(f'处理离开确认弹窗失败: {e}')
            return None
    
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
    const scrollingElements = [document.scrollingElement, document.body, document.documentElement,
        ...document.querySelectorAll('*')];
    for (const element of scrollingElements) {
        if (element && element.scrollHeight > element.clientHeight + 2) {
            element.scrollTop = element.scrollHeight;
        }
    }
    window.scrollTo(0, document.documentElement.scrollHeight);
    const labels = ['保存草稿', '保存为草稿', '存为草稿', '保存到草稿箱', '保存并退出', '存草稿'];
    const candidateNodes = [...document.querySelectorAll('button,[role="button"],a,[tabindex],div,span')];
    const draft = candidateNodes.find(element => labels.includes((element.innerText || element.textContent || '')
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

    def _read_upload_runtime_state(self):
        """递归读取发布页运行时状态，不依赖 iframe 外层的可见尺寸。

        wujie 会把业务 DOM 放在 0x0 iframe 或 open shadow root 中，
        DrissionPage 对这类节点做坐标/可见性判断会失真。这里直接在页面内
        合并所有 same-origin document 和 shadow root，返回稳定可序列化状态。
        """
        return self.page.run_js(r'''
return (() => {
    const normalize = value => (value || '').replace(/\s+/g, ' ').trim();
    const roots = [];
    const seenRoots = new WeakSet();
    let frameCount = 0;
    let shadowRootCount = 0;

    const addRoot = (root, depth = 0) => {
        if (!root || seenRoots.has(root) || depth > 8) return;
        seenRoots.add(root);
        roots.push(root);

        let nodes = [];
        try {
            nodes = [...root.querySelectorAll('*')];
        } catch (e) {
            return;
        }

        for (const element of nodes) {
            if (element.shadowRoot) {
                shadowRootCount += 1;
                addRoot(element.shadowRoot, depth + 1);
            }
        }
        for (const frame of root.querySelectorAll('iframe,frame')) {
            try {
                const frameDocument = frame.contentDocument;
                if (frameDocument) {
                    frameCount += 1;
                    addRoot(frameDocument, depth + 1);
                }
            } catch (e) {}
        }
    };
    addRoot(document);

    const elements = [];
    const seenElements = new Set();
    for (const root of roots) {
        const scope = root.documentElement || root;
        let nodes = [];
        try {
            nodes = [...scope.querySelectorAll('*')];
        } catch (e) {
            continue;
        }
        for (const element of nodes) {
            if (!seenElements.has(element)) {
                seenElements.add(element);
                elements.push(element);
            }
        }
    }

    const visible = element => {
        try {
            if (!element || !element.isConnected) return false;
            if (element.closest('[aria-hidden="true"],[hidden]')) return false;
            const ownerWindow = element.ownerDocument && element.ownerDocument.defaultView;
            if (!ownerWindow) return false;
            const style = ownerWindow.getComputedStyle(element);
            return style.display !== 'none' &&
                style.visibility !== 'hidden' &&
                Number(style.opacity || 1) > 0;
        } catch (e) {
            return false;
        }
    };

    const controlFor = element => {
        // 优先取元素内部的实际 <button>（保存草稿/发表是 <button>，disabled
        // 在 button 上；外层 DIV 检测不到 disabled，会误判按钮可用）。
        const innerButton = element.querySelector && element.querySelector('button');
        if (innerButton) return innerButton;
        return element.closest('button,[role="button"],a,[tabindex]') || element;
    };
    const disabled = element => {
        try {
            const ownerWindow = element.ownerDocument && element.ownerDocument.defaultView;
            const style = ownerWindow ? ownerWindow.getComputedStyle(element) : null;
            return element.disabled === true ||
                element.getAttribute('aria-disabled') === 'true' ||
                /disabled|is-disabled/i.test(element.className || '') ||
                (style && style.pointerEvents === 'none');
        } catch (e) {
            return false;
        }
    };

    const actionByText = labels => {
        const seen = new Set();
        const candidates = [];
        for (const element of elements) {
            if (!visible(element)) continue;
            const text = normalize(element.innerText || element.textContent);
            if (!labels.includes(text)) continue;
            const control = controlFor(element);
            if (seen.has(control)) continue;
            seen.add(control);
            candidates.push({
                found: true,
                text,
                disabled: disabled(control),
                tag: control.tagName || '',
            });
        }
        candidates.sort((left, right) => Number(left.disabled) - Number(right.disabled));
        return candidates[0] || {found: false, text: '', disabled: true, tag: ''};
    };

    const publishLabels = ['发表', '发表视频', '发布', '发布视频', '发布作品'];
    const draftLabels = [
        '保存草稿', '保存为草稿', '存为草稿', '保存到草稿箱', '保存并退出',
        '存草稿', '保存草稿箱', '保存至草稿箱', '存入草稿箱',
    ];
    const publishButton = actionByText(publishLabels);
    const draftButton = actionByText(draftLabels);
    const cancelButton = actionByText(['取消上传']);
    const deleteButton = actionByText(['删除']);

    const progressElements = elements.filter(element => {
        if (!visible(element)) return false;
        const role = element.getAttribute('role');
        return role === 'progressbar' || element.hasAttribute('aria-valuenow');
    });

    const percentages = [];
    for (const element of elements) {
        if (!visible(element)) continue;
        const text = normalize(element.innerText || element.textContent);
        if (!text || text.length > 18) continue;
        const match = text.match(/^(?:上传(?:进度)?\s*)?(\d{1,3})\s*%$/);
        if (!match) continue;
        const value = Number(match[1]);
        if (value >= 0 && value <= 100) percentages.push(value);
    }
    const progressPercentage = percentages.length ? Math.max(...percentages) : null;

    const failureNodes = elements.filter(element => {
        if (!visible(element)) return false;
        const text = normalize(element.innerText || element.textContent);
        return text.length > 0 && text.length <= 100 &&
            /(?:视频)?上传失败|视频处理失败|上传出错/.test(text);
    });

    const videos = elements
        .filter(element => element.tagName === 'VIDEO')
        .map(video => {
            const duration = Number.isFinite(video.duration) ? video.duration : null;
            const source = video.currentSrc || video.src || '';
            return {
                duration,
                readyState: Number(video.readyState || 0),
                width: Number(video.videoWidth || 0),
                sourceKind: /^https?:/i.test(source) ? 'remote' :
                    (/^blob:/i.test(source) ? 'blob' : (source ? 'other' : 'none')),
            };
        });
    const readyVideos = videos.filter(video =>
        video.readyState >= 1 && video.duration !== null && video.duration > 0
    );
    const remoteReadyVideos = readyVideos.filter(video => video.sourceKind === 'remote');

    const visibleControlTexts = [];
    const seenControlTexts = new Set();
    for (const element of elements) {
        if (visibleControlTexts.length >= 30 || !visible(element)) continue;
        const tag = element.tagName || '';
        const role = element.getAttribute('role') || '';
        if (!['BUTTON', 'A'].includes(tag) && role !== 'button') continue;
        const text = normalize(element.innerText || element.textContent);
        if (text && !seenControlTexts.has(text)) {
            seenControlTexts.add(text);
            visibleControlTexts.push(text.slice(0, 60));
        }
    }

    return {
        root_count: roots.length,
        frame_count: frameCount,
        shadow_root_count: shadowRootCount,
        element_count: elements.length,
        publish_button: publishButton,
        draft_button: draftButton,
        cancel_upload_visible: cancelButton.found,
        preview_delete_visible: deleteButton.found,
        progress_visible: percentages.length > 0,
        progress_percentage: progressPercentage,
        video_count: videos.length,
        video_ready_count: readyVideos.length,
        video_remote_ready_count: remoteReadyVideos.length,
        video_duration: readyVideos.length
            ? Math.max(...readyVideos.map(video => video.duration))
            : null,
        video_ready_state: videos.length
            ? Math.max(...videos.map(video => video.readyState))
            : 0,
        upload_failed: failureNodes.length > 0,
        visible_controls: visibleControlTexts,
    };
})()
''')

    @staticmethod
    def _upload_readiness(state):
        publish_button = state.get('publish_button') or {}
        draft_button = state.get('draft_button') or {}
        publish_ready = bool(publish_button.get('found') and not publish_button.get('disabled'))
        preview_ready = bool(
            state.get('preview_delete_visible')
            and (state.get('video_count', 0) > 0 or draft_button.get('found'))
        )
        remote_media_ready = state.get('video_remote_ready_count', 0) > 0
        # 保存草稿按钮可用（enabled）= 视频上传完成（实测：发布页初始/上传中
        # 按钮带 weui-desktop-btn_disabled，视频就绪后才 enabled）——可靠完成信号。
        draft_ready = bool(draft_button.get('found') and not draft_button.get('disabled'))
        # 上传活跃（cancel/progress 可见）期间绝不判定 ready：草稿按钮在上传中也可能
        # enabled，不能作为完成信号。完成门槛（not upload_active）折叠进 positive_ready，
        # 与 _wait_for_video_upload 的「活跃信号消失才算完成」一致。
        upload_active = bool(state.get('cancel_upload_visible')) or bool(state.get('progress_visible'))
        positive_ready = (publish_ready or preview_ready or remote_media_ready or draft_ready) and not upload_active
        cancel_visible = bool(state.get('cancel_upload_visible'))
        progress_visible = bool(state.get('progress_visible'))

        evidence = []
        if publish_ready:
            evidence.append('发表按钮可用')
        if preview_ready:
            evidence.append('视频预览已就绪')
        if remote_media_ready:
            evidence.append('远程视频元数据可用')
        if draft_ready:
            evidence.append('保存草稿按钮可用')
        return {
            'positive_ready': positive_ready,
            'upload_active': upload_active,
            'publish_ready': publish_ready,
            'preview_ready': preview_ready,
            'remote_media_ready': remote_media_ready,
            'draft_ready': draft_ready,
            'cancel_visible': cancel_visible,
            'progress_visible': progress_visible,
            'evidence': evidence,
        }

    def _wait_for_video_upload(self, timeout=300, settle_seconds=10):
        """等待视频真正上传完成。

        保存草稿按钮在上传中也可能可见且 enabled，因此绝不能作为完成信号。
        完成必须同时满足：上传活跃信号消失、存在完成证据、状态连续稳定。
        """
        self._scroll_page_to_bottom()
        started = time.monotonic()
        effective_deadline = started + max(30, timeout)
        hard_deadline = started + max(timeout, min(1800, timeout + 300))
        ready_since = None
        ready_polls = 0
        saw_upload_active = False
        last_progress = None
        last_progress_at = started
        last_log_at = 0
        last_state = {}

        while time.monotonic() < effective_deadline:
            now = time.monotonic()
            elapsed = now - started
            try:
                state = self._read_upload_runtime_state() or {}
            except Exception as e:
                logger.debug(f'读取上传运行时状态失败: {e}')
                state = {}
            last_state = state

            readiness = self._upload_readiness(state)
            positive_ready = readiness['positive_ready']
            upload_active = readiness['upload_active']

            if upload_active:
                saw_upload_active = True

            if state.get('upload_failed'):
                logger.error(f'平台报告视频上传失败，停止等待: {state}')
                return None

            if positive_ready and not upload_active and saw_upload_active:
                if ready_since is None:
                    ready_since = now
                    ready_polls = 1
                else:
                    ready_polls += 1

                stable_for = now - ready_since
                if stable_for >= settle_seconds and ready_polls >= 3:
                    evidence = list(readiness['evidence'])
                    if state.get('video_duration'):
                        evidence.append(f"duration={state['video_duration']:.1f}s")
                    logger.info(
                        f'视频上传完成，完成证据: {"、".join(evidence)}；'
                        f'稳定 {stable_for:.1f}s'
                    )
                    return state
            else:
                ready_since = None
                ready_polls = 0

            progress = state.get('progress_percentage')
            if progress is not None and progress != last_progress:
                last_progress = progress
                last_progress_at = now
                if now >= effective_deadline - 60 and now < hard_deadline:
                    effective_deadline = min(hard_deadline, now + 120)

            # 无进度UI兜底：30s 内未观察到上传活跃信号（cancel/progress），
            # 说明走的是无进度条的上传路径（实测V2重传、小视频），
            # 提前交给保存重试验证（视频就绪则保存成功，未就绪则重试）。
            if now >= 30 and not saw_upload_active:
                logger.warning(
                    f'30s 内未观察到上传进度UI（cancel/progress），'
                    f'可能无进度信号上传路径，提前交给保存重试验证'
                )
                return None

            if now - last_log_at >= 10:
                logger.info(
                    f'等待视频上传 ({int(elapsed)}s): '
                    f'progress={progress}%, active={upload_active}, '
                    f'cancel={readiness["cancel_visible"]}, duration={state.get("video_duration")}, '
                    f'publish={state.get("publish_button", {})}, '
                    f'preview_delete={state.get("preview_delete_visible", False)}, '
                    f'roots={state.get("root_count")}/frames={state.get("frame_count")}/'
                    f'shadows={state.get("shadow_root_count")}'
                )
                last_log_at = now

            if now >= effective_deadline:
                if (progress is not None and now - last_progress_at < 60
                        and now < hard_deadline):
                    effective_deadline = min(hard_deadline, now + 120)
                    continue
                break
            time.sleep(2)

        logger.error(f'等待视频上传完成超时（{timeout}秒）: {last_state}')
        return None

    def _validate_cover_file(self, cover_path):
        """校验封面文件。通过返回 None，否则返回错误字符串。"""
        if not cover_path:
            return '未提供封面路径'
        abs_path = os.path.abspath(cover_path)
        if not os.path.isfile(abs_path):
            return f'封面文件不存在: {abs_path}'
        ext = os.path.splitext(abs_path)[1].lower()
        if ext not in ('.jpg', '.jpeg', '.png', '.webp', '.bmp'):
            return f'封面扩展名不支持: {ext}'
        try:
            size = os.path.getsize(abs_path)
            if size < 1024:
                return f'封面文件过小 ({size} bytes)'
            if size > 50 * 1024 * 1024:
                return f'封面文件过大 ({size // (1024 * 1024)}MB)'
        except OSError as e:
            return f'读取封面大小失败: {e}'
        try:
            from PIL import Image
            with Image.open(abs_path) as img:
                w, h = img.size
                if w <= 0 or h <= 0:
                    return f'封面尺寸异常: {w}x{h}'
        except Exception as e:
            return f'封面解码失败: {e}'
        return None

    def _open_cover_editor(self):
        """打开封面编辑器（弹窗）。成功返回 True。"""
        try:
            result = self.page.run_js(COVER_EDITOR_OPEN_JS)
            logger.info(f'打开封面编辑器结果: {result}')
            return bool(isinstance(result, dict) and result.get('found'))
        except Exception as e:
            logger.warning(f'打开封面编辑器失败: {e}')
            return False

    def _set_cover_file(self, cover_path):
        """用 DrissionPage input() 设置封面文件控件（浏览器安全策略禁止 JS 设值）。"""
        cover_input = self._find_cover_file_input()
        if not cover_input:
            logger.warning('封面编辑界面中未找到图片文件控件')
            return False
        try:
            cover_input.input(os.path.abspath(cover_path))
            return True
        except Exception as e:
            logger.warning(f'设置封面文件失败: {e}')
            return False

    def _cover_state(self):
        """读取封面编辑器当前状态（弹窗、确认按钮、图片）。"""
        try:
            return self.page.run_js(COVER_EDITOR_STATE_JS) or {}
        except Exception as e:
            logger.debug(f'读取封面编辑器状态失败: {e}')
            return {}

    def _wait_cover_preview(self, timeout=20):
        """轮询：弹窗内图片已加载、确认按钮可见且未禁用。成功返回状态 dict。"""
        started = time.time()
        last = {}
        while time.time() - started < timeout:
            last = self._cover_state()
            if last.get('dialog_open') and last.get('dialog_image_count', 0) > 0 \
                    and last.get('confirm_visible') and not last.get('confirm_disabled'):
                logger.info(
                    f'封面编辑器就绪: 弹窗={last.get("dialog_open")}, '
                    f'图片={last.get("dialog_image_count")}, '
                    f'确认可见={last.get("confirm_visible")}, '
                    f'禁用={last.get("confirm_disabled")}'
                )
                return last
            if last.get('has_error'):
                logger.warning(f'封面编辑器出现错误提示: {last.get("dialog_text")}')
            time.sleep(0.5)
        logger.warning(f'等待封面编辑器就绪超时: {last}')
        return last

    def _apply_cover_confirm(self):
        """在封面编辑器弹窗内点击“确认”。返回 (applied, detail)。"""
        try:
            result = self.page.run_js(COVER_EDITOR_APPLY_JS) or {}
            applied = bool(result.get('applied'))
            logger.info(f'点击封面确认: {result}')
            return applied, result
        except Exception as e:
            logger.warning(f'点击封面确认失败: {e}')
            return False, {'reason': 'js_error', 'error': str(e)}

    def _capture_cover_fingerprint(self):
        """（调试用）捕获全页面最大图指纹。已不用于应用验证——改用 _capture_cover_card。"""
        try:
            state = self.page.run_js(COVER_EDITOR_IMAGE_CHANGED_JS) or {}
            main = state.get('main') or {}
            return {
                'src': main.get('src', ''),
                'w': main.get('w', 0),
                'dialog_open': state.get('dialog_open'),
            }
        except Exception:
            return {}

    def _capture_cover_card(self):
        """捕获封面预览卡片指纹（锁定“封面预览/分享卡片”容器内主图）。"""
        try:
            return self.page.run_js(COVER_CARD_FP_JS) or {}
        except Exception as e:
            logger.debug(f'捕获封面卡片指纹失败: {e}')
            return {}

    @staticmethod
    def _cover_card_changed(before, after):
        """封面卡片指纹是否变化（应用成功信号）。

        规则：应用后卡片出现主图（before 无图 → after 有图）或主图 src 变化。
        二者都不满足 → 未变化（保守判失败，绝不假阳性）。
        """
        if not after or not after.get('found'):
            return False
        bm = (before or {}).get('main')
        am = after.get('main')
        if bm is None and am is not None:
            return True
        if bm and am:
            return bm.get('src') != am.get('src')
        return False

    def _verify_cover_applied(self, before_card):
        """点击确认后必须同时看到：弹窗关闭 + 封面预览卡片变化 + 无错误。"""
        started = time.time()
        last_state = {}
        while time.time() - started < 15:
            last_state = self._cover_state()
            after_card = self._capture_cover_card()
            dialog_closed = not last_state.get('dialog_open')
            if last_state.get('has_error'):
                logger.warning(f'封面应用出现错误: {last_state.get("dialog_text")}')
                return False, last_state
            changed = self._cover_card_changed(before_card, after_card)
            if dialog_closed and changed:
                logger.info(f'封面应用已确认: 弹窗关闭, 卡片指纹变化={changed}')
                return True, last_state
            time.sleep(0.5)
        logger.warning(f'封面应用验证超时: {last_state}')
        return False, last_state

    def _upload_cover(self, cover_path):
        """上传封面（状态机），返回结构化结果：
        {'status': 'not_requested'|'uploading'|'applied'|'failed',
         'error': str|None, 'cover_path': str}
        """
        result = {'status': 'uploading', 'error': None, 'cover_path': cover_path}
        if not cover_path:
            result['status'] = 'not_requested'
            return result
        err = self._validate_cover_file(cover_path)
        if err:
            result['status'] = 'failed'
            result['error'] = err
            logger.warning(f'封面上传校验失败: {err}')
            return result
        try:
            # 打开编辑器前锁定封面预览卡片指纹（不能等弹窗打开再捕获——
            # 弹窗本身会改变全页面最大图，导致假阳性 applied）
            card_before = self._capture_cover_card()
            if not self._open_cover_editor():
                result['status'] = 'failed'
                result['error'] = '未找到封面编辑入口'
                self._save_debug_state('cover_editor_missing')
                return result
            time.sleep(1)
            if not self._set_cover_file(cover_path):
                result['status'] = 'failed'
                result['error'] = '未找到封面图片文件控件'
                self._save_debug_state('cover_input_missing')
                return result
            ready = self._wait_cover_preview()
            if not ready.get('dialog_open'):
                result['status'] = 'failed'
                result['error'] = '封面编辑器未打开'
                self._save_debug_state('cover_editor_not_open')
                return result
            if ready.get('has_error'):
                result['status'] = 'failed'
                result['error'] = f"封面编辑器报错: {ready.get('dialog_text')}"
                self._save_debug_state('cover_editor_error')
                return result
            applied, detail = self._apply_cover_confirm()
            if not applied:
                result['status'] = 'failed'
                result['error'] = f"未找到封面确认按钮: {detail}"
                self._save_debug_state('cover_apply_not_found')
                return result
            verified, state = self._verify_cover_applied(card_before)
            if verified:
                result['status'] = 'applied'
                logger.info(f'封面应用成功: {os.path.abspath(cover_path)}')
            else:
                result['status'] = 'failed'
                result['error'] = '点击确认后封面预览未变化或弹窗未关闭'
                self._save_debug_state('cover_apply_unverified')
            return result
        except Exception as e:
            logger.warning(f'封面上传失败: {e}')
            self._save_debug_state('cover_upload_failed')
            result['status'] = 'failed'
            result['error'] = str(e)
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

    def _click_draft_button_in_page(self):
        """在顶层、iframe 和 shadow root 中直接触发保存草稿按钮。"""
        return self.page.run_js(r'''
return (() => {
    const normalize = value => (value || '').replace(/\s+/g, ' ').trim();
    const labels = [
        '保存草稿', '保存为草稿', '存为草稿', '保存到草稿箱', '保存并退出',
        '存草稿', '保存草稿箱', '保存至草稿箱', '存入草稿箱',
    ];
    const seenRoots = new WeakSet();

    const visible = element => {
        try {
            if (!element || !element.isConnected) return false;
            if (element.closest('[aria-hidden="true"],[hidden]')) return false;
            const ownerWindow = element.ownerDocument && element.ownerDocument.defaultView;
            if (!ownerWindow) return false;
            const style = ownerWindow.getComputedStyle(element);
            return style.display !== 'none' &&
                style.visibility !== 'hidden' &&
                Number(style.opacity || 1) > 0;
        } catch (e) {
            return false;
        }
    };

    const isDisabled = element => {
        try {
            const ownerWindow = element.ownerDocument && element.ownerDocument.defaultView;
            const style = ownerWindow ? ownerWindow.getComputedStyle(element) : null;
            return element.disabled === true ||
                element.getAttribute('aria-disabled') === 'true' ||
                /disabled|is-disabled/i.test(element.className || '') ||
                (style && style.pointerEvents === 'none');
        } catch (e) {
            return false;
        }
    };

    const findButton = root => {
        if (!root || seenRoots.has(root)) return null;
        seenRoots.add(root);

        let nodes = [];
        try {
            nodes = [...root.querySelectorAll(
                'button,[role="button"],a,[tabindex],div,span'
            )];
        } catch (e) {}

        for (const node of nodes) {
            const text = normalize(node.innerText || node.textContent);
            if (!labels.includes(text) || !visible(node)) continue;
            const button = (node.querySelector && node.querySelector('button'))
                || node.closest('button,[role="button"],a,[tabindex]') || node;
            if (!visible(button) || isDisabled(button)) continue;
            return {button, text};
        }

        for (const node of nodes) {
            if (node.shadowRoot) {
                const result = findButton(node.shadowRoot);
                if (result) return result;
            }
        }
        for (const frame of root.querySelectorAll('iframe,frame')) {
            try {
                if (frame.contentDocument) {
                    const result = findButton(frame.contentDocument);
                    if (result) return result;
                }
            } catch (e) {}
        }
        return null;
    };

    const result = findButton(document);
    if (!result) return {found: false, clicked: false, text: ''};
    result.button.click();
    return {found: true, clicked: true, text: result.text};
})()
''')

    def _read_draft_list_text(self):
        """递归读取草稿列表正文（含 iframe/shadow root），返回原始文本。"""
        try:
            return self.page.run_js(r'''
                return (() => {
                    const seen = new WeakSet();
                    const getText = root => {
                        if (!root || seen.has(root)) return '';
                        seen.add(root);
                        let text = root.body ? root.body.innerText : '';
                        for (const element of root.querySelectorAll('*')) {
                            if (element.shadowRoot) {
                                text += '\n' + getText(element.shadowRoot);
                            }
                        }
                        for (const frame of root.querySelectorAll('iframe,frame')) {
                            try {
                                if (frame.contentDocument) {
                                    text += '\n' + getText(frame.contentDocument);
                                }
                            } catch(e){}
                        }
                        return text;
                    };
                    return getText(document);
                })()
            ''') or ''
        except Exception as e:
            logger.debug(f'读取草稿列表失败: {e}')
            return ''

    @staticmethod
    def _compact(s):
        """去空白，用于标题/正文的容错比较。"""
        return ''.join((s or '').split())

    def _count_title_in_draft_box(self, title):
        """当前草稿列表里标题出现次数（调用前须已在草稿箱页面）。

        用于“本次新增草稿”判定：保存后计数必须大于保存前基线。
        """
        t = self._compact(title)
        if not t:
            return 0
        return self._compact(self._read_draft_list_text()).count(t)

    def _capture_draft_baseline(self, title, publish_url):
        """保存前统计草稿箱里该标题的出现次数（基线），随后返回发布页。

        返回基线计数；捕获失败返回 None（调用方不得把它当 0，否则重复标题
        的旧草稿会构成假阳性）。「本次新增草稿」判定依赖 count_after > 基线。
        """
        draft_url = 'https://channels.weixin.qq.com/platform/post/draftListManager'
        try:
            self.page.get(draft_url)
            time.sleep(4)
            base = self._count_title_in_draft_box(title)
            logger.info(f'草稿箱基线（标题出现次数）: {base}')
            self.page.get(publish_url)
            time.sleep(5)
            return base
        except Exception as e:
            logger.warning(f'捕获草稿基线失败，退回子串校验（可能误判）: {e}')
            return None

    def _wait_for_draft_saved(self, previous_url, title, timeout=90, baseline_count=None):
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
        time.sleep(3)
        last_nav = 0
        while time.time() - started < timeout:
            try:
                # 处理"将此次编辑保留? 不保存保存"离开确认弹窗——点"保存"（=保存草稿）。
                # 视频未就绪时保存无效会弹此窗，点"保存"重试；视频就绪后点"保存"即成功。
                self._handle_draft_popup()
                # 若发布页已自己跳到草稿箱（SPA 路由），无需重复导航
                current_url = self.page.url or ''
                if 'draftlist' not in current_url.lower() and 'draft' not in current_url.lower():
                    if time.time() - last_nav >= 4:
                        self.page.get(draft_url)
                        last_nav = time.time()
                        time.sleep(4)
                # 递归读取草稿列表文本（含 iframe）
                body_text = self._read_draft_list_text()
                expected_title = ''.join(title.split())
                compact_body = ''.join(body_text.split())
                # 本次新增草稿判定：提供基线时要求计数严格大于基线（重复标题不误判）；
                # 未提供基线（向后兼容/测试）退回标题子串匹配。
                if baseline_count is not None:
                    count_now = compact_body.count(expected_title)
                    if count_now > baseline_count:
                        logger.info(
                            f'草稿箱已确认本次新增: 标题出现次数 {baseline_count}→{count_now}'
                        )
                        return True
                elif expected_title and expected_title in compact_body:
                    logger.info(f'草稿箱已找到标题: {title}')
                    return True
                # 没找到：草稿可能还在处理，等一会重新导航刷新列表
                time.sleep(3)
            except Exception as e:
                logger.debug(f'检查草稿箱失败: {e}')
                time.sleep(2)
        logger.warning(f'草稿箱未找到标题（timeout={timeout}s）: {title}')
        return False

    def _save_draft(self, title, baseline_count=None):
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
        
        # 步骤3: 查找保存草稿按钮。DrissionPage 找不到不代表按钮不存在，
        # wujie 的 0x0 iframe 仍可直接通过页面内 JS 点击。
        draft_button = self._find_draft_button()
        if draft_button:
            logger.info(f'找到保存草稿按钮: {draft_button.text}')
        else:
            logger.info('DrissionPage 未直接定位保存草稿按钮，改用递归 JS 查找')
        
        # 步骤4: 点击保存草稿按钮
        # 页面是 wujie 微前端架构：发布表单渲染在 iframe[0]（empty.html）内，
        # 该 iframe 的 getBoundingClientRect 尺寸为 0，DrissionPage 的坐标点击
        # （element.click()）会算错坐标而静默失效。改为用 JS 在 iframe 内直接
        # 触发 button.click()，坐标点击仅作回退。
        try:
            previous_url = self.page.url
            click_result = self._click_draft_button_in_page() or {}
            if click_result.get('clicked'):
                logger.info(
                    f'通过 JS 点击保存草稿按钮: {click_result.get("text")}, '
                    f'当前URL: {previous_url}'
                )
                # 点击后可能弹出"将此次编辑保留? 不保存保存"离开确认弹窗
                # （视频未就绪时保存无效），点"保存"（=保存草稿）。
                self._handle_draft_popup()
            elif draft_button:
                logger.warning('JS 未点击保存草稿按钮，回退到 DrissionPage 点击')
                draft_button.click()
                logger.info(f'已用 DrissionPage 点击保存草稿按钮，当前URL: {previous_url}')
            else:
                logger.error('顶层、iframe 和 shadow root 中均未找到保存草稿按钮')
                return False
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
        if not self._wait_for_draft_saved(previous_url, title, baseline_count=baseline_count):
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
                self.page = self._new_page(co)
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
                self.page = self._new_page(co)
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
    
    @staticmethod
    def _upload_failure(error=None):
        """构造 upload_to_draft 的结构化失败结果。"""
        return {
            'status': 'failed',
            'draft_saved': False,
            'cover': {'status': 'not_requested', 'error': None, 'cover_path': None},
            'error': error or '上传失败',
        }

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
            dict: {'status': 'success'|'failed',
                   'draft_saved': bool,
                   'cover': {'status': 'not_requested'|'uploading'|'applied'|'failed',
                             'error': str|None, 'cover_path': str|None},
                   'error': str|None}
        """
        logger.info(f"[{self.account_name}] 开始上传到草稿箱...")
        
        # 检查文件
        abs_file_path = os.path.abspath(video_path)
        if not os.path.exists(abs_file_path):
            logger.error(f"找不到视频文件: {abs_file_path}")
            return self._upload_failure("找不到视频文件: {abs_file_path}")
        
        # 检查登录（复用：10分钟内已验证则跳过重复校验，避免每版重复验证）
        _now = time.time()
        if getattr(self, '_login_verified_at', 0) and _now - self._login_verified_at < 600:
            pass
        elif not self.check_login():
            logger.error("登录失效，请先登录")
            return self._upload_failure("登录失效，请先登录")
        else:
            self._login_verified_at = time.time()
        
        # 启动浏览器
        co = self._get_browser_options(headless=False)
        try:
            self.page = self._new_page(co)
        except Exception as e:
            logger.error(f"浏览器启动失败: {e}")
            return self._upload_failure("浏览器启动失败: {e}")
        
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
                return self._upload_failure("登录状态已失效")

            # 保存前捕获草稿箱基线（该标题当前出现次数）——「本次新增草稿」判定依据。
            # 必须在视频上传前导航，避免离开发布页丢已上传视频。
            draft_baseline = self._capture_draft_baseline(title, publish_url)

            # 1. 上传视频
            logger.info("定位文件上传控件...")
            file_input = None
            
            # 直接查找文件上传控件，不点击"发表视频"按钮
            # CSS 选择器实测可靠（xpath 每版失败 30s 才回退），改为主选择器
            for attempt in range(3):
                try:
                    file_input = self.page.ele('css:input[type="file"]', timeout=3)
                    if file_input:
                        logger.info(f"第 {attempt+1} 次尝试找到文件上传控件（CSS）")
                        break
                except Exception:
                    time.sleep(1)
            
            if not file_input:
                logger.error("未找到文件上传控件（CSS），尝试XPath选择器...")
                try:
                    file_input = self.page.ele('xpath://input[@type="file"]', timeout=5)
                    if file_input:
                        logger.info("通过XPath选择器找到文件上传控件")
                except Exception as e:
                    logger.error(f"XPath选择器也失败: {e}")
            
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
                return self._upload_failure("未找到文件上传控件")
            
            logger.info(f"上传视频: {abs_file_path}")
            file_input.input(abs_file_path)
            time.sleep(3)
            
            # 2. 填写文案
            logger.info("填写文案...")
            try:
                editor = self.page.ele('css:div.input-editor', timeout=30)
            except Exception:
                logger.error("未找到富文本输入框")
                return self._upload_failure("未找到富文本输入框")
            
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
            upload_state = self._wait_for_video_upload(timeout=upload_timeout)
            if not upload_state:
                logger.warning(
                    '等待视频上传未得到完成信号（可能无进度UI），'
                    '继续尝试保存草稿，由保存重试验证视频是否就绪'
                )

            upload_state = upload_state or {}
            logger.info(
                f'视频上传状态已稳定: duration={upload_state.get("video_duration")}, '
                f'publish={upload_state.get("publish_button")}, '
                f'preview_delete={upload_state.get("preview_delete_visible")}'
            )
            current_url = self.page.url
            logger.info(f'视频上传后URL: {current_url}')

            # 5. 上传封面（状态机）- 失败不阻断保存草稿，但状态回传给调用方
            time.sleep(1)  # 等封面预览卡片渲染
            abs_cover_path = os.path.abspath(cover_path) if cover_path else None
            cover_result = {'status': 'not_requested', 'error': None, 'cover_path': abs_cover_path}
            if cover_path:
                cover_result = self._upload_cover(abs_cover_path)
                if cover_result.get('status') == 'failed':
                    logger.warning(f"封面上传未成功（不阻断）: {cover_result.get('error')}")
            else:
                logger.info('无封面，跳过封面上传')

            # 6. 保存草稿（重试：视频可能仍在上传，保存被拒则等待重试）
            logger.info('保存草稿...')
            saved = False
            for attempt in range(5):
                logger.info(f'保存草稿（第{attempt+1}次尝试）...')
                saved = self._save_draft(title, baseline_count=draft_baseline)
                if saved:
                    break
                logger.warning(
                    f'草稿保存未确认（第{attempt+1}次），'
                    f'视频可能仍在上传，等待30s后重试'
                )
                time.sleep(30)
                # 回发布页重试（_save_draft 失败时可能已导航到草稿箱）。
                # 已应用的封面会随导航丢失，必须重新应用，否则草稿保存成功后封面仍是默认的。
                try:
                    current_url_after = self.page.url or ''
                    if 'create' not in current_url_after:
                        self.page.get(publish_url)
                        time.sleep(5)
                    if cover_result.get('status') == 'applied' and abs_cover_path:
                        logger.info('草稿重试前重新应用封面（上次应用随导航丢失）')
                        cover_result = self._upload_cover(abs_cover_path)
                except Exception as e:
                    logger.debug(f'返回发布页失败: {e}')

            if saved:
                logger.info(f'[{self.account_name}] ✅ 草稿保存成功')
                return {
                    'status': 'success',
                    'draft_saved': True,
                    'cover': cover_result,
                    'error': None,
                }
            else:
                logger.error(f'[{self.account_name}] ❌ 草稿保存失败')
                # 保存失败时不关闭页面，方便调试
                logger.warning('页面保持打开，方便检查问题')
                self._save_debug_state('draft_save_failed')
                return {
                    'status': 'failed',
                    'draft_saved': False,
                    'cover': cover_result,
                    'error': '草稿保存失败',
                }
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
