# -*- coding: utf-8 -*-
"""发表确认与成功验证单测（2026-11-10 P0 防假成功；2026-11-14 修正确认弹窗误判）。

核心：
1. _handle_publish_confirm 只点「真实渲染」的弹窗按钮（隐藏弹窗 0x0 由 JS 侧按
   盒子尺寸过滤），等不到可见弹窗必须返回 None，不能返回假成功；
2. _verify_publish_success 只认明确信号（成功/失败提示、离开发布页），不再把
   「发表按钮消失」当成功——定时发表模式下按钮本来就不会消失。
"""
import unittest
from unittest import mock

from wechat_uploader import WeChatChannelsUploader

CREATE_URL = 'https://channels.weixin.qq.com/platform/post/create'
LIST_URL = 'https://channels.weixin.qq.com/platform/post/list'


def _make_uploader(js_result=None, url=CREATE_URL):
    u = WeChatChannelsUploader('测试账号')
    page = mock.MagicMock()
    page.run_js.return_value = js_result
    page.url = url
    u.page = page
    return u, page


class TestPublishConfirm(unittest.TestCase):
    def test_confirm_returns_clicked_text(self):
        u, _ = _make_uploader('确定')
        self.assertEqual(u._handle_publish_confirm(timeout=0.5), '确定')

    def test_confirm_returns_none_when_no_visible_dialog(self):
        # 页面上只有预渲染的 0x0 隐藏弹窗时，JS 返回 None → 不能当已确认
        u, _ = _make_uploader(None)
        self.assertIsNone(u._handle_publish_confirm(timeout=0.4))

    def test_confirm_survives_js_exception(self):
        u, page = _make_uploader('确定')
        page.run_js.side_effect = RuntimeError('js broken')
        self.assertIsNone(u._handle_publish_confirm(timeout=0.4))


class TestPublishVerification(unittest.TestCase):
    def test_success_text_returns_true(self):
        u, _ = _make_uploader('发表成功，等待审核')
        self.assertTrue(u._verify_publish_success(timeout=1, interval=0.1))

    def test_failure_text_returns_false(self):
        u, _ = _make_uploader('发表失败，请稍后再试')
        self.assertFalse(u._verify_publish_success(timeout=1, interval=0.1))

    def test_no_signal_timeout_returns_none(self):
        # 定时发表点完「发表」按钮仍在，也没有成功/失败提示 → 超时 None
        u, _ = _make_uploader('发表')
        self.assertIsNone(u._verify_publish_success(timeout=0.5, interval=0.1))

    def test_url_left_create_page_returns_true(self):
        u, _ = _make_uploader('', url=LIST_URL)
        self.assertTrue(u._verify_publish_success(timeout=1, interval=0.1))

    def test_url_to_draft_list_is_not_success(self):
        # 点发表后被导到草稿箱（/platform/post/draftListManager）不得记成发表成功
        u, _ = _make_uploader(
            '', url='https://channels.weixin.qq.com/platform/post/draftListManager')
        self.assertIsNone(u._verify_publish_success(timeout=0.5, interval=0.1))

    def test_switch_to_immediate_returns_true(self):
        u, _ = _make_uploader(True)
        self.assertTrue(u._switch_to_immediate_publish())

    def test_switch_to_immediate_returns_false_on_missing_radio(self):
        u, _ = _make_uploader(False)
        self.assertFalse(u._switch_to_immediate_publish())

    def test_switch_to_immediate_survives_exception(self):
        u, page = _make_uploader('x')
        page.run_js.side_effect = RuntimeError('js broken')
        self.assertFalse(u._switch_to_immediate_publish())


class TestPublishedListMatch(unittest.TestCase):
    DESC = '她回乡竟引轰动，真相背后谁在操控？一句“娘镇上传疯了”，反转连连，谁是真相的隐藏者？'
    TITLE = '归夫谜案传镇疯'
    # 列表卡片里出现的同一条描述（首行前缀命中）
    CARD = '她回乡竟引轰动，真相背后谁在操控？一句“娘镇上传疯了”，反转连连 #热搜'

    def test_desc_prefix_new_entry_confirms(self):
        # 列表卡片只展示描述、不展示短标题，必须用描述首行匹配；
        # L2：只有比发表前基线多出一条才算确认
        u, _ = _make_uploader('', url=LIST_URL)
        u._read_draft_list_text = mock.Mock(return_value=self.CARD)
        self.assertTrue(u._verify_published_list_has(
            self.TITLE, self.DESC, timeout=1, interval=0.1,
            baseline={'desc': 0, 'title': 0}))

    def test_same_desc_existing_entry_not_confirmed(self):
        # 跨剧同名碰撞：列表里那条本来就存在（基线已计 1），不得当成本次发表
        u, _ = _make_uploader('', url=LIST_URL)
        u._read_draft_list_text = mock.Mock(return_value=self.CARD)
        base = u._count_publish_keys(
            u._norm_publish_text(self.CARD),
            u._publish_match_keys(self.TITLE, self.DESC))
        self.assertEqual(base['desc'], 1)
        self.assertFalse(u._verify_published_list_has(
            self.TITLE, self.DESC, timeout=0.5, interval=0.1, baseline=base))

    def test_collision_count_delta_confirms(self):
        # 基线已有 1 条同首行，发表后变 2 条 → 确认本次新增
        u, _ = _make_uploader('', url=LIST_URL)
        u._read_draft_list_text = mock.Mock(
            return_value=self.CARD + '\n' + self.CARD)
        self.assertTrue(u._verify_published_list_has(
            self.TITLE, self.DESC, timeout=1, interval=0.1,
            baseline={'desc': 1, 'title': 0}))

    def test_absent_entry_returns_false(self):
        u, _ = _make_uploader('', url=LIST_URL)
        u._read_draft_list_text = mock.Mock(return_value='别人的视频描述 #别的剧')
        self.assertFalse(u._verify_published_list_has(
            self.TITLE, self.DESC, timeout=0.5, interval=0.1,
            baseline={'desc': 0, 'title': 0}))

    def test_missing_baseline_never_claims_success(self):
        # 无基线（旧调用/基线捕获失败）：不按子串断言成功，转人工复核
        u, _ = _make_uploader('', url=LIST_URL)
        u._read_draft_list_text = mock.Mock(return_value=self.CARD)
        self.assertFalse(u._verify_published_list_has(
            self.TITLE, self.DESC, timeout=0.5, interval=0.1))

    def test_short_keys_are_disabled(self):
        # 过短的描述首行/标题不参与匹配（防长列表天然命中）
        u = WeChatChannelsUploader('测试账号')
        self.assertEqual(u._publish_match_keys('短标题', '太短了'), {'desc': '', 'title': ''})

    def test_capture_baseline_counts_current_list(self):
        u, page = _make_uploader('', url=LIST_URL)
        u._read_draft_list_text = mock.Mock(return_value=self.CARD)
        with mock.patch('wechat_uploader.time.sleep'):
            base = u._capture_published_baseline(self.TITLE, CREATE_URL, description=self.DESC)
        self.assertEqual(base, {'desc': 1, 'title': 0})
        self.assertEqual(page.get.call_count, 2)  # 列表 → 回发布页


class TestPublishTimeDefaults(unittest.TestCase):
    def test_default_publish_time_reads_runtime_config(self):
        # L4：与入队算点同源，读运行时 CONFIG（不再单读 DEFAULT_CONFIG）
        import config as _cfg
        from datetime import datetime, timedelta
        with mock.patch.object(_cfg, 'CONFIG', {
                'upload': {'publish_schedule': {'day_offset': 2, 'times': ['07:00', '09:00']}}}):
            t = WeChatChannelsUploader._default_publish_time()
        expect = (datetime.now() + timedelta(days=2)).strftime('%Y-%m-%d') + ' 07:00'
        self.assertEqual(t, expect)

    def test_default_publish_time_falls_back_to_default_config(self):
        # 运行时配置缺失 → 回退 DEFAULT_CONFIG 的 9 档（与 _compute_publish_time 一致）
        import config as _cfg
        from datetime import datetime, timedelta
        with mock.patch.object(_cfg, 'CONFIG', {'upload': None}):
            t = WeChatChannelsUploader._default_publish_time()
        expect = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d') + ' 07:00'
        self.assertEqual(t, expect)


if __name__ == '__main__':
    unittest.main()
