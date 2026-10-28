import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


try:
    import DrissionPage  # noqa: F401
except ModuleNotFoundError:
    drission_page = types.ModuleType('DrissionPage')
    drission_page.__path__ = []

    class FakeChromiumOptions:
        def __init__(self):
            self.arguments = []

        def set_paths(self, **kwargs):
            self.paths = kwargs

        def set_argument(self, value):
            self.arguments.append(value)

        def set_user_agent(self, value):
            self.user_agent = value

    drission_page.ChromiumOptions = FakeChromiumOptions
    drission_page.ChromiumPage = object
    common = types.ModuleType('DrissionPage.common')
    common.Keys = types.SimpleNamespace(ENTER='ENTER')
    sys.modules['DrissionPage'] = drission_page
    sys.modules['DrissionPage.common'] = common


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wechat_uploader import WeChatChannelsUploader


class FakeElement:
    def __init__(self, text='', visible=True, attrs=None):
        self.text = text
        self.visible = visible
        self.attrs = attrs or {}
        self.click_count = 0
        self.input_value = None
        self.states = types.SimpleNamespace(
            is_displayed=visible,
            is_enabled='disabled' not in (self.attrs.get('class') or '').lower(),
        )

    def is_displayed(self):
        return self.visible

    def attr(self, name):
        return self.attrs.get(name)

    def click(self):
        self.click_count += 1

    def input(self, value):
        self.input_value = value


class FakePage:
    def __init__(self, matches=None, selectors=None, js_result=None):
        self.matches = matches or {}
        self.selectors = selectors or {}
        self.js_result = js_result
        self.js_calls = []
        self.url = 'https://channels.weixin.qq.com/platform/post/create'

    def eles(self, selector):
        return self.selectors.get(selector, self.matches.get(selector, []))

    def ele(self, selector, timeout=None):
        return self.selectors.get(selector)

    def get(self, url):
        self.url = url

    def run_js(self, script, *args):
        self.js_calls.append(script)
        return self.js_result


class WeChatUploaderTests(unittest.TestCase):
    def make_uploader(self):
        uploader = object.__new__(WeChatChannelsUploader)
        uploader.account_name = 'test'
        uploader.user_data_path = str(Path(__file__).parent)
        uploader.page = FakePage()
        return uploader

    def test_headful_browser_starts_maximized(self):
        uploader = self.make_uploader()
        options = uploader._get_browser_options(headless=False)
        self.assertIn('--start-maximized', options.arguments)
        self.assertNotIn('--headless=new', options.arguments)

    def test_headless_login_check_falls_back_to_headed(self):
        """Chrome 155 与 DrissionPage 4.1.1.4 的 headless 不兼容，回退有头模式。

        headless=True 也不再设置 --headless=new（实测启动后 CDP 404），
        改为 --start-maximized（短暂弹窗可接受）。
        """
        uploader = self.make_uploader()
        options = uploader._get_browser_options(headless=True)
        self.assertNotIn('--headless=new', options.arguments)
        self.assertIn('--start-maximized', options.arguments)

    def test_visible_text_lookup_skips_hidden_matches_and_prefers_exact(self):
        hidden = FakeElement('保存草稿', visible=False)
        partial = FakeElement('保存草稿并退出')
        exact = FakeElement('保存草稿')
        page = FakePage(matches={'text:保存草稿': [hidden, partial, exact]})
        uploader = self.make_uploader()
        uploader.page = page

        self.assertIs(uploader._find_visible_text_element(('保存草稿',)), exact)

    def test_validate_cover_file_rejects_bad_inputs_and_accepts_valid(self):
        """回归：_validate_cover_file 必须是类方法（曾误缩进成死代码导致 AttributeError）。"""
        import os
        import tempfile
        import shutil
        from PIL import Image
        uploader = self.make_uploader()
        tmp = tempfile.mkdtemp(prefix='cover_val_')
        try:
            # 不存在
            self.assertIsNotNone(uploader._validate_cover_file(
                os.path.join(tmp, 'nope.jpg')))
            # 非法扩展名
            bad = os.path.join(tmp, 'c.txt')
            open(bad, 'w').write('x' * 2000)
            err = uploader._validate_cover_file(bad)
            self.assertIsNotNone(err)
            self.assertIn('扩展名不支持', err)
            # 过小（<1KB）
            tiny = os.path.join(tmp, 'tiny.jpg')
            Image.new('RGB', (10, 10)).save(tiny)
            err = uploader._validate_cover_file(tiny)
            self.assertIsNotNone(err)
            self.assertIn('过小', err)
            # 合法（>1KB 且可解码）
            ok = os.path.join(tmp, 'ok.jpg')
            Image.new('RGB', (200, 200)).save(ok)
            self.assertIsNone(uploader._validate_cover_file(ok))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_cover_card_changed_semantics(self):
        """#2：封面卡片指纹变化判定——before 无图→after 有图 或 src 变化 才算成功。"""
        U = WeChatChannelsUploader
        # after 未找到卡片 → 不变（保守）
        self.assertFalse(U._cover_card_changed({}, {'found': False}))
        # before 无图 → after 有图 → 变化（首次上传封面）
        self.assertTrue(U._cover_card_changed(
            {'found': True, 'main': None},
            {'found': True, 'main': {'src': 'http://x/1.jpg', 'w': 100, 'h': 100}}))
        # src 变化 → 变化（更换封面）
        self.assertTrue(U._cover_card_changed(
            {'found': True, 'main': {'src': 'http://x/old.jpg', 'w': 100}},
            {'found': True, 'main': {'src': 'http://x/new.jpg', 'w': 100}}))
        # src 相同 → 未变化（假阳性防护：弹窗开关不影响卡片）
        self.assertFalse(U._cover_card_changed(
            {'found': True, 'main': {'src': 'http://x/same.jpg', 'w': 100}},
            {'found': True, 'main': {'src': 'http://x/same.jpg', 'w': 100}}))
        # 前后都无图 → 未变化
        self.assertFalse(U._cover_card_changed(
            {'found': True, 'main': None}, {'found': True, 'main': None}))

    def test_upload_readiness_rejects_active_upload_with_enabled_draft_button(self):
        state = {
            'cancel_upload_visible': True,
            'progress_visible': True,
            'progress_percentage': 41,
            'publish_button': {'found': True, 'disabled': True},
            'draft_button': {'found': True, 'disabled': False},
            'preview_delete_visible': False,
            'video_count': 1,
            'video_remote_ready_count': 0,
        }

        readiness = WeChatChannelsUploader._upload_readiness(state)

        self.assertTrue(readiness['upload_active'])
        self.assertFalse(readiness['positive_ready'])

    def test_upload_readiness_accepts_completed_preview(self):
        state = {
            'cancel_upload_visible': False,
            'progress_visible': False,
            'publish_button': {'found': False, 'disabled': True},
            'draft_button': {'found': True, 'disabled': False},
            'preview_delete_visible': True,
            'video_count': 1,
            'video_duration': 180,
            'video_remote_ready_count': 0,
        }

        readiness = WeChatChannelsUploader._upload_readiness(state)

        self.assertFalse(readiness['upload_active'])
        self.assertTrue(readiness['positive_ready'])
        self.assertIn('视频预览已就绪', readiness['evidence'])

    def test_wait_for_upload_requires_stable_ready_state(self):
        active = {
            'cancel_upload_visible': True,
            'progress_visible': True,
            'progress_percentage': 41,
            'publish_button': {'found': False, 'disabled': True},
            'draft_button': {'found': True, 'disabled': False},
            'preview_delete_visible': False,
            'video_count': 1,
            'video_remote_ready_count': 0,
        }
        ready = {
            'cancel_upload_visible': False,
            'progress_visible': False,
            'publish_button': {'found': True, 'disabled': False},
            'draft_button': {'found': True, 'disabled': False},
            'preview_delete_visible': True,
            'video_count': 1,
            'video_duration': 181,
            'video_remote_ready_count': 1,
        }
        uploader = self.make_uploader()
        uploader._scroll_page_to_bottom = lambda: None
        uploader._read_upload_runtime_state = Mock(
            side_effect=[active, ready, ready, ready]
        )

        with patch('wechat_uploader.time.sleep'):
            self.assertIs(
                uploader._wait_for_video_upload(timeout=2, settle_seconds=0),
                ready,
            )

    def test_scroll_uses_native_api_and_brings_draft_action_into_view(self):
        uploader = self.make_uploader()
        calls = []
        uploader.page.scroll = types.SimpleNamespace(to_bottom=lambda: calls.append('bottom'))
        uploader._scroll_page_to_bottom()

        script = uploader.page.js_calls[0]
        self.assertEqual(calls, ['bottom'])
        self.assertIn("draft.scrollIntoView({block: 'center'", script)

    def test_draft_button_lookup_ignores_hidden_and_disabled_buttons(self):
        hidden = FakeElement('保存草稿', visible=False)
        disabled = FakeElement('保存草稿', attrs={'class': 'weui-desktop-btn_disabled'})
        page = FakePage(
            matches={'text:保存草稿': [hidden, disabled]},
            selectors={'tag:button': [hidden, disabled]},
        )
        uploader = self.make_uploader()
        uploader.page = page

        self.assertIsNone(uploader._find_draft_button())

    def test_draft_button_lookup_returns_visible_enabled_button(self):
        hidden = FakeElement('保存草稿', visible=False)
        visible = FakeElement('保存草稿')
        page = FakePage(
            matches={'text:保存草稿': [hidden, visible]},
            selectors={'tag:button': [hidden, visible]},
        )
        uploader = self.make_uploader()
        uploader.page = page

        self.assertIs(uploader._find_draft_button(), visible)

    def test_draft_button_lookup_uses_marked_visible_dom_button(self):
        visible = FakeElement('保存草稿')
        page = FakePage(
            selectors={'css:[data-codex-draft-button="true"]': visible},
        )
        uploader = self.make_uploader()
        uploader.page = page

        self.assertIs(uploader._find_draft_button(), visible)

    def test_save_draft_requires_platform_confirmation(self):
        button = FakeElement('保存草稿')
        uploader = self.make_uploader()
        uploader._find_visible_text_element = lambda labels: None
        uploader._find_draft_button = lambda: button
        uploader._click_draft_button_in_page = lambda: {
            'found': False, 'clicked': False, 'text': ''
        }
        uploader._scroll_page_to_bottom = lambda: None
        uploader._wait_for_draft_saved = lambda previous_url, title, baseline_count=None: False
        uploader._save_debug_state = lambda stage: None

        self.assertFalse(uploader._save_draft('示例标题'))
        self.assertEqual(button.click_count, 1)

    def test_save_draft_succeeds_when_platform_confirms(self):
        button = FakeElement('保存草稿')
        uploader = self.make_uploader()
        uploader._find_visible_text_element = lambda labels: None
        uploader._find_draft_button = lambda: button
        uploader._click_draft_button_in_page = lambda: {
            'found': False, 'clicked': False, 'text': ''
        }
        uploader._scroll_page_to_bottom = lambda: None
        uploader._wait_for_draft_saved = lambda previous_url, title, baseline_count=None: True

        self.assertTrue(uploader._save_draft('示例标题'))
        self.assertEqual(button.click_count, 1)

    def test_save_draft_uses_recursive_js_when_drission_has_no_element(self):
        uploader = self.make_uploader()
        uploader._find_visible_text_element = lambda labels: None
        uploader._find_draft_button = lambda: None
        uploader._click_draft_button_in_page = lambda: {
            'found': True, 'clicked': True, 'text': '保存草稿'
        }
        uploader._scroll_page_to_bottom = lambda: None
        uploader._wait_for_draft_saved = lambda previous_url, title, baseline_count=None: True

        self.assertTrue(uploader._save_draft('示例标题'))


    def test_wait_for_draft_saved_ignores_unrelated_success_text(self):
        """常驻文案不算成功：发布页点保存草稿之前就有"上传成功"，
        草稿页还常驻"自动保存中/已保存"，都不能当作草稿保存成功。"""
        page = FakePage(js_result='视频上传成功，自动保存中，已保存。')
        uploader = self.make_uploader()
        uploader.page = page
        uploader._find_draft_button = lambda: FakeElement('保存草稿')

        with patch('wechat_uploader.time.sleep'):
            self.assertFalse(uploader._wait_for_draft_saved(
                'https://channels.weixin.qq.com/platform/post/create',
                '示例标题', timeout=0.05))

    def test_wait_for_draft_saved_accepts_title_in_draft_list(self):
        page = FakePage(js_result='草稿箱 示例标题')
        uploader = self.make_uploader()
        uploader.page = page

        with patch('wechat_uploader.time.sleep'):
            self.assertTrue(uploader._wait_for_draft_saved(
                'https://channels.weixin.qq.com/platform/post/create',
                '示例标题', timeout=0.05))

    def test_wait_for_draft_saved_url_change_requires_title_in_body(self):
        """URL 跳走后还必须能在正文里看到本条标题，否则可能是别的原因跳走
        （比如被踢回登录页），不能算保存成功。"""
        page = FakePage(
            js_result='创作中心',
            selectors={'tag:button': []},
        )
        page.url = 'https://channels.weixin.qq.com/platform/post/list'
        uploader = self.make_uploader()
        uploader.page = page

        with patch('wechat_uploader.time.sleep'):
            self.assertFalse(uploader._wait_for_draft_saved(
                'https://channels.weixin.qq.com/platform/post/create',
                '示例标题', timeout=0.05))

        page.js_result = '创作中心 示例标题'
        with patch('wechat_uploader.time.sleep'):
            self.assertTrue(uploader._wait_for_draft_saved(
                'https://channels.weixin.qq.com/platform/post/create',
                '示例标题', timeout=0.05))


if __name__ == '__main__':
    unittest.main()
