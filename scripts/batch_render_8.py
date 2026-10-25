"""批量渲染8部短剧 - 一个接一个直到全部完成"""
import subprocess, os, sys, time
from datetime import datetime

PYTHON = r"C:\Users\86159\AppData\Local\Programs\Python\Python311\python.exe"
RENDER = r"D:\短剧工坊\scripts\render_generic.py"
FFMPEG = r"D:\ffmpeg-master-latest-win64-gpl-shared\bin\ffmpeg.exe"
FONT = r"C:\Windows\Fonts\msyhbd.ttc"
CONFIG_DIR = r"D:\短剧工坊\configs"

# 8部剧配置
DRAMAS = [
    {"name": "迟爱换不回旧人", "cover_src": r"D:\短剧素材\迟爱换不回旧人\sph.png", "badge_list": ["第1集","第2集","第3集","第4集","完整版","大结局"]},
    {"name": "乞丐说自己才是真女儿", "cover_src": r"D:\短剧素材\乞丐说自己才是真女儿\sph乞丐说自己才是真女儿.png", "badge_list": ["第1集","第2集","第3集","第4集","完整版","大结局"]},
    {"name": "他误签协议我怀崽跑路", "cover_src": r"D:\短剧素材\他误签协议我怀崽跑路\sph他误签协议我怀崽跑路.png", "badge_list": ["第1集","第2集","第3集","第4集","完整版","大结局"]},
    {"name": "我的妻女我来守护", "cover_src": r"D:\短剧素材\我的妻女我来守护\视频号1.png", "badge_list": ["第1集","第2集","第3集","第4集","完整版","大结局"]},
    {"name": "小公主滚开老大回来了", "cover_src": r"D:\短剧素材\小公主滚开老大回来了\小公主滚开老大回来了-视频号-竖.png", "badge_list": ["第1集","第2集","第3集","第4集","完整版","大结局"]},
    {"name": "一张破山图，我成真山王", "cover_src": r"D:\短剧素材\一张破山图，我成真山王\一张破山图，我成真山王（sph）.png", "badge_list": ["第1集","第2集","第3集","第4集","完整版","大结局"]},
    {"name": "这一世，我的命我自己保", "cover_src": r"D:\短剧素材\这一世，我的命我自己保\视频号-这一世，我的命我自己保.png", "badge_list": ["第1集","第2集","第3集","第4集","完整版","大结局"]},
    {"name": "红酒覆纱仇", "cover_src": r"D:\短剧工坊\output\红酒覆纱仇\covers\红酒覆纱仇-原始封面.png", "badge_list": ["第1集","第2集","第3集","第4集","完整版","大结局"]},
]

def gen_covers(drama_name, cover_src, badge_list):
    """生成6张封面"""
    out_dir = os.path.join(r"D:\短剧工坊\output", drama_name, "covers")
    os.makedirs(out_dir, exist_ok=True)
    for i, badge in enumerate(badge_list):
        out_path = os.path.join(out_dir, f"{drama_name}_{i+1}_封面.jpg")
        cmd = [
            FFMPEG, "-y", "-i", cover_src,
            "-vf", f"drawtext=fontfile='{FONT}':text='{badge}':fontsize=56:fontcolor=white:borderw=3:bordercolor=black:x=w-text_w-30:y=h-text_h-30",
            out_path
        ]
        subprocess.run(cmd, capture_output=True)
        print(f"  封面 {i+1}: {badge} ✓")
    print(f"  6张封面完成")

def render_drama(drama_name, config_path):
    """渲染一部剧"""
    print(f"\n{'='*60}")
    print(f"🎬 开始渲染: {drama_name}")
    print(f"{'='*60}")
    
    result = subprocess.run(
        [PYTHON, RENDER, config_path],
        capture_output=True, text=True, encoding='utf-8', errors='replace'
    )
    
    # 打印关键输出
    output = result.stdout
    # 找成功/失败统计
    for line in output.split('\n'):
        if any(k in line for k in ['✅', '❌', '成功', '失败', '总计', '耗时', '报告']):
            print(f"  {line.strip()}")
    
    print(f"  退出码: {result.returncode}")
    return result.returncode == 0

def main():
    start_time = time.time()
    results = []
    
    for idx, drama in enumerate(DRAMAS, 1):
        name = drama["name"]
        config_path = os.path.join(CONFIG_DIR, f"{name}.json")
        
        if not os.path.exists(config_path):
            print(f"\n⚠️  配置不存在: {config_path}, 跳过 {name}")
            results.append((name, False, "配置不存在"))
            continue
        
        print(f"\n{'#'*60}")
        print(f"# [{idx}/8] {name}")
        print(f"{'#'*60}")
        
        success = render_drama(name, config_path)
        results.append((name, success, "OK" if success else "失败"))
        
        if success:
            # 生成封面
            print(f"\n  🎨 生成封面...")
            gen_covers(name, drama["cover_src"], drama["badge_list"])
        
        # 打印进度
        done = sum(1 for _, s, _ in results if s)
        print(f"\n  📊 进度: {done}/{len(DRAMAS)} 完成")
    
    # 最终报告
    elapsed = (time.time() - start_time) / 60
    print(f"\n{'='*60}")
    print(f"📊 批量渲染完成")
    print(f"{'='*60}")
    print(f"  总耗时: {elapsed:.1f} 分钟")
    print(f"  成功: {sum(1 for _,s,_ in results if s)}/8")
    print(f"  失败: {sum(1 for _,s,_ in results if not s)}/8")
    
    for name, success, status in results:
        icon = "✅" if success else "❌"
        print(f"  {icon} {name} ({status})")
    
    # 写汇总报告
    report_path = r"D:\短剧工坊\output\批量渲染汇总报告.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("# 批量渲染汇总报告\n\n")
        f.write(f"- 时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
        f.write(f"- 总耗时: {elapsed:.1f} 分钟\n")
        f.write(f"- 成功: {sum(1 for _,s,_ in results if s)}/8\n\n")
        f.write("| 状态 | 剧名 |\n")
        f.write("|------|------|\n")
        for name, success, status in results:
            icon = "✅" if success else "❌"
            f.write(f"| {icon} | {name} |\n")
    print(f"\n  报告: {report_path}")

if __name__ == "__main__":
    main()