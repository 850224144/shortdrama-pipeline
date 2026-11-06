# -*- coding: utf-8 -*-
from hooktool.quotes import extract_quotes


def test_extract_strips_timestamp_prefix():
    lines = [
        "[12.3-15.1] 这一巴掌，打你老婆生孩子，你却让别人拿你老婆取乐。",
        "普通叙述句没有爆点。",
        "[40.0-44.2] 直到十分钟前，我怀双胞胎难产，手术医生却变成了别人。",
    ]
    short, long = extract_quotes(lines)
    assert long, "应提取到冲突/悬念句"
    assert all("[" not in x for x in long)


def test_extract_fallback_when_no_keyword():
    lines = ["第一句普通台词。", "第二句普通台词。", "第三句普通台词。"]
    short, long = extract_quotes(lines)
    assert long, "无命中时走中段兜底"
