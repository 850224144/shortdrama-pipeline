"""批量渲染全部15部短剧 - 自动连续执行"""
import subprocess, os, time, glob
from datetime import datetime

PYTHON = r"C:\Users\86159\AppData\Local\Programs\Python\Python311\python.exe"
RENDER = r"D:\短剧工坊\scripts\render_generic.py"
FFMPEG = r"D:\ffmpeg-master-latest-win64-gpl-shared\bin\ffmpeg.exe"
FONT = r"C:\Windows\Fonts\msyhbd.ttc"
CONFIG_DIR = r"D:\短剧工坊\configs"

BADGES = ["第1集","第2集","第3集","第4集","完整版","大结局"]

# All 15 dramas with cover info
DRAMAS = [
    ("迟爱换不回旧人", r"D:\短剧素材\迟爱换不回旧人\sph.png", True),
    ("乞丐说自己才是真女儿", r"D:\短剧素材\乞丐说自己才是真女儿\sph乞丐说自己才是真女儿.png", True),
    ("他误签协议我怀崽跑路", r"D:\短剧素材\他误签协议我怀崽跑路\sph他误签协议我怀崽跑路.png", True),
    ("我的妻女我来守护", r"D:\短剧素材\我的qizi\视频号1.png", True),
    ("小公主滚开老大回来了", r"D:\短剧素材\小公主滚开老大回来了\小公主滚开老大回来了-视频号-竖.png", True),
    ("一张破山图，我成真山王", r"D:\短剧素材\一张破山图，我成真山王\一张破山图，我成真山王（sph）.png", True),
    ("这一世，我的命我自己保", r"D:\短剧素材\这一世，我的命我自己保\视频号-这一世，我的命我自己保.png", True),
    ("红酒覆纱仇", None, False),
    ("前夫竟不知我是谁", None, False),
    ("亲情止损", None, False),
    ("百万买断四年情深", None, False),
    ("豪门小少爷的哄睡师", None, False),
    ("悔赶持家婆", None, False),
    ("卤味夫妻甜日子", None, False),
    ("杀鸡媳妇不好惹", None, False),
]

def extract_cover(drama):
    """从Ep1截帧生成封面"""
    cover_dir = os.path.join(r"D:\短剧工坊\output", drama, "covers")
    os.makedirs(cover_dir, exist_ok=True)
    cover_path = os.path.join(cover_dir, f"{drama}-原始封面.png")
    if os.path.exists(cover_path): return cover_path
    
    # Find episode 1
    patterns = [f"D:\\短剧素材\\{drama}\\*1*mp4"]
    for p in patterns:
        files = glob.glob(p)
        if files:
            subprocess.run([FFMPEG, "-y", "-ss", "10", "-i", files[0], "-frames:v", "1", cover_path], capture_output=True)
            return cover_path
    return None

def gen_covers(drama, cover_src):
    out_dir = os.path.join(r"D:\短剧工坊\output", drama, "covers")
    os.makedirs(out_dir, exist_ok=True)
    for i, badge in enumerate(BADGES):
        out_path = os.path.join(out_dir, f"{drama}_{i+1}_封面.jpg")
        if os.path.exists(out_path): continue
        subprocess.run([FFMPEG, "-y", "-i", cover_src,
            "-vf", f"drawtext=fontfile='{FONT}':text='{badge}':fontsize=56:fontcolor=white:borderw=3:bordercolor=black:x=w-text_w-30:y=h-text_h-30",
            out_path], capture_output=True)

def main():
    start = time.time()
    results = []
    
    for idx, (name, cover_src, has_cover) in enumerate(DRAMAS, 1):
        config = os.path.join(CONFIG_DIR, f"{name}.json")
        if not os.path.exists(config):
            print(f"\n⚠️  [{idx}/15] {name} 配置不存在")
            results.append((name, False))
            continue
        
        print(f"\n{'#'*50}")
        print(f"# [{idx}/15] {name}")
        print(f"{'#'*50}", flush=True)
        
        r = subprocess.run([PYTHON, RENDER, config], capture_output=True, text=True, encoding='utf-8', errors='replace')
        ok = r.returncode == 0
        
        for line in r.stdout.split('\n'):
            if any(k in line for k in ['✅','❌','总计','耗时','报告']):
                print(f"  {line.strip()}")
        
        if ok:
            # Cover
            if not has_cover:
                cover_src = extract_cover(name)
            if cover_src:
                gen_covers(name, cover_src)
                print(f"  🎨 6张封面 ✓")
        
        results.append((name, ok))
        done = sum(1 for _,s in results if s)
        print(f"\n  📊 {done}/{len(DRAMAS)} 完成")
    
    elapsed = (time.time()-start)/60
    print(f"\n{'='*50}")
    print(f"📊 全部完成: {sum(1 for _,s in results if s)}/15, 耗时 {elapsed:.1f} 分钟")
    for name, ok in results:
        print(f"  {'✅' if ok else '❌'} {name}")

if __name__ == "__main__":
    main()
