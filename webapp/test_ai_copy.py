# -*- coding: utf-8 -*-
"""测试 AI 文案生成器"""

import sys
sys.path.insert(0, r'D:\短剧工坊\webapp')

from ai_copy_generator import AICopyGenerator

# 初始化
ai = AICopyGenerator()

# 测试 API 连接
print("=" * 60)
print("测试 1: API 连接")
print("=" * 60)
if ai.test_api():
    print("✅ API 连接成功")
else:
    print("❌ API 连接失败")
    sys.exit(1)

# 测试文案生成
print("\n" + "=" * 60)
print("测试 2: 文案生成 - 恶霸占我橘山")
print("=" * 60)

copy_v1 = ai.generate_copy(
    drama_name='恶霸占我橘山',
    version_idx=1,
    version_hook='逆袭',
    plot_summary='恶霸强占橘子山，隐藏千金身份即将曝光',
    drama_genre='豪门逆袭'
)
print(f"\nV1 (逆袭):")
print(f"  标题: {copy_v1['title']} ({len(copy_v1['title'])}字)")
print(f"  描述: {copy_v1['description']} ({len(copy_v1['description'])}字)")
print(f"  话题: {', '.join(copy_v1['hashtags'])}")

copy_v2 = ai.generate_copy(
    drama_name='恶霸占我橘山',
    version_idx=2,
    version_hook='打脸',
    plot_summary='恶霸打脸瞬间，豪门千金复仇开始',
    drama_genre='豪门逆袭'
)
print(f"\nV2 (打脸):")
print(f"  标题: {copy_v2['title']} ({len(copy_v2['title'])}字)")
print(f"  描述: {copy_v2['description']} ({len(copy_v2['description'])}字)")
print(f"  话题: {', '.join(copy_v2['hashtags'])}")

copy_v3 = ai.generate_copy(
    drama_name='恶霸占我橘山',
    version_idx=3,
    version_hook='真相',
    plot_summary='真相大白，隐藏身份即将曝光',
    drama_genre='豪门逆袭'
)
print(f"\nV3 (真相):")
print(f"  标题: {copy_v3['title']} ({len(copy_v3['title'])}字)")
print(f"  描述: {copy_v3['description']} ({len(copy_v3['description'])}字)")
print(f"  话题: {', '.join(copy_v3['hashtags'])}")

# 验证
print("\n" + "=" * 60)
print("验证结果")
print("=" * 60)

all_valid = True
for i, (copy, hook) in enumerate([(copy_v1, '逆袭'), (copy_v2, '打脸'), (copy_v3, '真相')], 1):
    title = copy['title']
    desc = copy['description']
    tags = copy['hashtags']
    
    # 检查标题
    if len(title) < 6 or len(title) > 16:
        print(f"❌ V{i} 标题字数不符合要求: {len(title)}字 ({title})")
        all_valid = False
    elif any(p in title for p in [',', '，', '。', '！', '？', '：', '；', '、', '"', "'", '[', ']', '【', '】']):
        print(f"❌ V{i} 标题包含标点符号: {title}")
        all_valid = False
    else:
        print(f"✅ V{i} 标题有效: {title} ({len(title)}字)")
    
    # 检查描述
    if len(desc) < 30:
        print(f"❌ V{i} 描述太短: {len(desc)}字")
        all_valid = False
    else:
        print(f"✅ V{i} 描述有效: {len(desc)}字")
    
    # 检查话题
    if len(tags) < 3:
        print(f"❌ V{i} 话题数量不足: {len(tags)}个")
        all_valid = False
    else:
        print(f"✅ V{i} 话题有效: {len(tags)}个")

if all_valid:
    print("\n✅ 所有验证通过！")
else:
    print("\n❌ 有验证未通过，需要调整")

print("\n" + "=" * 60)
print("测试完成")
print("=" * 60)