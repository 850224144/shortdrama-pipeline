# -*- coding: utf-8 -*-
"""
短剧流水线主调度脚本
自动扫描E盘新素材→校验ID→分配账号→触发切片→更新状态
"""
import os
import json
import re
import time
import shutil
from datetime import datetime

E_SRC = r"E:\短剧素材"
OUTPUT_ROOT = r"D:\短剧工坊\output"
STATE_FILE = r"D:\短剧工坊\data\pipeline_state.json"
CACHE_FILE = r"D:\短剧工坊\data\dramaid_cache.json"
TRASH = r"D:\短剧工坊\回收站"

# 题材分类规则
GENRE_RULES = [
    (["萌宝", "三宝", "崽", "双胞胎", "找爹", "三岁"], "小桃追剧中"),
    (["糙汉", "宠妻", "甜宠", "二婚", "闪婚", "先婚后爱"], "小桃追剧中"),
    (["婆婆", "妈", "娘家", "养母", "孝道", "亲情"], "小桃追剧中"),
    (["年代", "七零", "八零", "种田", "山村", "农村"], "小桃追剧中"),
    (["真千金", "假千金", "抱错", "认亲"], "福桃追剧"),
    (["打脸", "逆袭", "复仇", "重生", "离婚"], "福桃追剧"),
    (["前夫", "追妻", "后悔", "复婚", "前妻"], "福桃追剧"),
    (["总裁", "豪门", "陆总", "傅少"], "福桃追剧"),
    (["战神", "兵王", "赘婿", "龙王", "回归"], "福桃追剧"),
]


def load_state():
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def normalize_name(name):
    """标准化剧名，去掉空格标点"""
    name = re.sub(r'[^\u4e00-\u9fa5a-zA-Z]', '', name)
    return name


def assign_account(drama_name):
    """按题材自动分配账号"""
    for keywords, account in GENRE_RULES:
        for kw in keywords:
            if kw in drama_name:
                return account
    return "小桃追剧中"


def is_duplicate(name, completed_list):
    """模糊去重：名字差1-2个字也算重复"""
    norm = normalize_name(name)
    for item in completed_list:
        old_norm = normalize_name(item["name"])
        # 编辑距离≤2认为是同剧换皮
        if abs(len(norm) - len(old_norm)) <= 2 and (norm in old_norm or old_norm in norm):
            return True
        if abs(len(norm) - len(old_norm)) <= 1:
            match = sum(1 for a, b in zip(norm, old_norm) if a == b)
            if match >= len(norm) - 2:
                return True
    return False


def scan_new_dramas():
    """扫描E盘新素材，没切过的"""
    state = load_state()
    new_dramas = []
    for d in os.listdir(E_SRC):
        path = os.path.join(E_SRC, d)
        if not os.path.isdir(path):
            continue
        # 已经有已切完.txt跳过
        if os.path.exists(os.path.join(path, "已切完.txt")):
            continue
        mp4s = [f for f in os.listdir(path) if f.endswith(".mp4")]
        if not mp4s:
            continue
        # 模糊去重
        if is_duplicate(d, state["completed"]):
            continue
        new_dramas.append(d)
    return new_dramas


def check_drama_id(drama_name):
    """查CPS库有没有这个剧ID"""
    if not os.path.exists(CACHE_FILE):
        return False, None
    with open(CACHE_FILE, "r", encoding="utf-8") as f:
        cache = json.load(f)
    norm = normalize_name(drama_name)
    for key, entries in cache.get("dramas", {}).items():
        if norm in key or key in norm:
            for e in entries:
                if e.get("is_published") == 1:
                    return True, e.get("drama_id")
    return False, None


def delete_drama(drama_name):
    """删除素材+成品，先移回收站"""
    os.makedirs(TRASH, exist_ok=True)
    now = datetime.now().strftime("%Y%m%d_%H%M%S")
    # 删素材
    src = os.path.join(E_SRC, drama_name)
    if os.path.exists(src):
        shutil.move(src, os.path.join(TRASH, f"{drama_name}_{now}_src"))
    # 删成品
    for account in ["小桃追剧中", "福桃追剧"]:
        out = os.path.join(OUTPUT_ROOT, account, drama_name)
        if os.path.exists(out):
            shutil.move(out, os.path.join(TRASH, f"{drama_name}_{now}_out"))
    # 更新状态
    state = load_state()
    state["completed"] = [c for c in state["completed"] if c["name"] != drama_name]
    save_state(state)
    print(f"已删除：{drama_name}（移入回收站）")


def main():
    print("扫描新素材...")
    new_dramas = scan_new_dramas()
    print(f"发现 {len(new_dramas)} 部未切剧：{new_dramas}")

    for drama in new_dramas:
        print(f"\n处理：{drama}")
        has_id, drama_id = check_drama_id(drama)
        if not has_id:
            print(f"  未找到CPS ID，加入待确认列表")
            state = load_state()
            state["pending_confirm"].append({
                "name": drama,
                "reason": "未找到CPS ID",
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
            save_state(state)
            continue
        account = assign_account(drama)
        print(f"  分配账号：{account}，ID：{drama_id}")
        # 更新状态
        state = load_state()
        state["current_task"] = {
            "status": "切片中",
            "drama_name": drama,
            "progress": 0,
            "message": f"开始切《{drama}》"
        }
        save_state(state)

        # 调用自动切片
        from auto_cut import cut_drama
        src_dir = os.path.join(E_SRC, drama)
        ok = cut_drama(drama, src_dir, account)

        if ok:
            print(f"  切片完成")
            state = load_state()
            state["completed"].append({
                "name": drama,
                "account": account,
                "count": 6,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "path": f"{OUTPUT_ROOT}\\{account}\\{drama}"
            })
            state["current_task"] = {"status": "空闲", "drama_name": "", "progress": 0, "message": ""}
            save_state(state)
        else:
            print(f"  切片失败")
            state = load_state()
            state["failed"].append({
                "name": drama,
                "reason": "切片失败",
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
            save_state(state)


if __name__ == "__main__":
    main()
