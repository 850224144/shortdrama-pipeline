"""
只查找保存草稿按钮，不上传视频
"""
import os
import sys
import time
from DrissionPage import ChromiumPage

# 添加路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wechat_uploader import WeChatChannelsUploader

def find_draft_button_only():
    """只查找保存草稿按钮，不上传视频"""
    
    account_name = '福桃说剧'
    user_data_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wechat_user_data', account_name)
    
    # 启动浏览器
    uploader = WeChatChannelsUploader(account_name)
    co = uploader._get_browser_options(headless=False)
    
    try:
        page = ChromiumPage(co)
    except Exception as e:
        print(f"❌ 浏览器启动失败: {e}")
        return
    
    # 打开发布页面
    print("打开发布页面...")
    try:
        page.get("https://channels.weixin.qq.com/platform/post/create")
    except Exception as e:
        print(f"打开发布页面时出错: {e}")
        # 尝试处理弹窗
        try:
            alert = page.alert()
            if alert:
                print(f"处理弹窗: {alert.text}")
                alert.accept()
        except Exception:
            pass
        page.get("https://channels.weixin.qq.com/platform/post/create")
    time.sleep(5)
    
    # 处理可能的弹窗
    try:
        alert = page.alert()
        if alert:
            print(f"处理弹窗: {alert.text}")
            alert.accept()
    except Exception:
        pass
    
    # 验证登录
    current_url = page.url
    print(f"当前URL: {current_url}")
    
    if "login" in current_url:
        print("❌ 登录失效")
        page.quit()
        return
    
    print("✅ 登录有效")
    
    # 查找所有按钮
    print("\n查找所有按钮...")
    buttons = page.eles('tag:button')
    print(f"找到 {len(buttons)} 个按钮")
    
    draft_labels = ('保存草稿', '保存为草稿', '存为草稿', '保存到草稿箱', '保存并退出', '存草稿', '发表', '发布')
    
    for i, button in enumerate(buttons):
        try:
            text = (button.text or '').strip()
            print(f"  按钮[{i}]: '{text}'")
            
            if text in draft_labels:
                print(f"  ✅ 找到目标按钮: '{text}'")
        except Exception as e:
            print(f"  按钮[{i}]: 获取信息失败: {e}")
    
    # 尝试使用 DrissionPage 的 text 选择器
    print("\n尝试使用 text 选择器...")
    try:
        draft_button = page.ele('text:保存草稿', timeout=5)
        if draft_button:
            print("✅ 通过 text 选择器找到'保存草稿'按钮")
            print(f"   文本: '{draft_button.text}'")
            print("   尝试点击...")
            draft_button.click()
            time.sleep(2)
            print("   ✅ 已点击")
        else:
            print("❌ text 选择器未找到'保存草稿'按钮")
    except Exception as e:
        print(f"❌ text 选择器失败: {e}")
    
    # 尝试使用 XPath 选择器
    print("\n尝试使用 XPath 选择器...")
    try:
        draft_button = page.ele('xpath://button[contains(text(), "保存草稿")]', timeout=5)
        if draft_button:
            print("✅ 通过 XPath 选择器找到'保存草稿'按钮")
            print(f"   文本: '{draft_button.text}'")
        else:
            print("❌ XPath 选择器未找到'保存草稿'按钮")
    except Exception as e:
        print(f"❌ XPath 选择器失败: {e}")
    
    # 尝试使用 CSS 选择器查找所有按钮
    print("\n尝试使用 CSS 选择器...")
    try:
        buttons = page.eles('css:button')
        print(f"找到 {len(buttons)} 个按钮")
        for i, button in enumerate(buttons):
            try:
                text = (button.text or '').strip()
                print(f"  按钮[{i}]: '{text}'")
                
                # 如果找到保存草稿按钮，尝试点击
                if text == '保存草稿':
                    print(f"\n✅ 找到'保存草稿'按钮 (索引 {i})")
                    print("   尝试点击...")
                    try:
                        button.click()
                        time.sleep(2)
                        print("   ✅ 已点击")
                    except Exception as e:
                        print(f"   ❌ 点击失败: {e}")
            except Exception:
                print(f"  按钮[{i}]: 获取文本失败")
    except Exception as e:
        print(f"❌ CSS 选择器失败: {e}")
    
    # 使用 JavaScript 查找
    print("\n使用 JavaScript 查找...")
    result = page.run_js('''
        (() => {
            const labels = ['保存草稿', '保存为草稿', '存为草稿', '保存到草稿箱', '保存并退出', '存草稿', '发表', '发布'];
            const buttons = [...document.querySelectorAll('button')];
            const results = [];
            
            for (const button of buttons) {
                const text = button.textContent.trim();
                const rect = button.getBoundingClientRect();
                results.push({
                    text: text,
                    x: Math.round(rect.x),
                    y: Math.round(rect.y),
                    w: Math.round(rect.w),
                    h: Math.round(rect.h),
                    isTarget: labels.includes(text)
                });
            }
            return results;
        })()
    ''')
    
    if result:
        print(f"\nJavaScript 找到 {len(result)} 个按钮:")
        for item in result:
            marker = "✅" if item.get('isTarget') else "  "
            print(f"  {marker} '{item['text']}' @ ({item['x']}, {item['y']}) size: {item['w']}x{item['h']}")
    else:
        print("JavaScript 查找失败或无结果")
    
    # 保存截图
    print("\n保存截图...")
    screenshot_path = os.path.join(user_data_path, 'debug_find_draft_button.png')
    try:
        page.get_screenshot(path=screenshot_path)
        print(f"✅ 截图已保存: {screenshot_path}")
    except Exception as e:
        print(f"❌ 截图失败: {e}")
    
    # 等待用户操作
    print("\n浏览器已打开，您可以手动检查页面...")
    print("按 Enter 关闭浏览器...")
    input()
    
    page.quit()
    print("✅ 浏览器已关闭")

if __name__ == '__main__':
    find_draft_button_only()
