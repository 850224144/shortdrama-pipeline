# -*- coding: utf-8 -*-
from hooktool.normalize import (
    normalize_name, is_fuzzy_duplicate, parse_episode_number, sanitize_short_title,
)


def test_normalize_name_strips_symbols():
    assert normalize_name("娇妻婆婆为我挡风雨-sph") == "娇妻婆婆为我挡风雨sph"
    assert normalize_name("  陆时衍 1 ") == "陆时衍1"


def test_fuzzy_duplicate_exact_and_space():
    assert is_fuzzy_duplicate("陆时衍", "陆时衍 ") is True
    assert is_fuzzy_duplicate("白玉瑶", "秦露") is False


def test_parse_episode_number():
    assert parse_episode_number("1.mp4") == 1
    assert parse_episode_number("第12集.mp4") == 12
    assert parse_episode_number("cover-sph.jpg") is None


def test_sanitize_short_title():
    assert sanitize_short_title("这一巴掌，打你老婆！") == "这一巴掌打你老婆"
    assert sanitize_short_title("啊") == ""
    assert len(sanitize_short_title("今天天气不错适合出门走走看看")) <= 16
