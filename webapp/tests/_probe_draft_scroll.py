import sys
import time

sys.path.insert(0, r'D:\短剧工坊\webapp')
from wechat_uploader import WeChatChannelsUploader
from DrissionPage import ChromiumPage

uploader = WeChatChannelsUploader('福桃说剧')
page = ChromiumPage(uploader._get_browser_options(headless=False))
page.get('https://channels.weixin.qq.com/platform/post/create')
time.sleep(4)
file_input = page.ele('css:input[type="file"]', timeout=10)
file_input.input(r'D:\短剧工坊\output\为妻女撑腰的男人\为妻女撑腰的男人_V6_往上奶奶那份加一勺_去重版.mp4')
time.sleep(30)
print(page.run_js(r'''
return (() => ({
    viewport: [innerWidth, innerHeight],
    page: [document.documentElement.scrollHeight, document.documentElement.clientHeight,
        document.documentElement.scrollTop, document.body.scrollHeight, document.body.clientHeight,
        document.body.scrollTop],
    scrollers: [...document.querySelectorAll('*')]
        .filter(e => e.scrollHeight > e.clientHeight + 5)
        .map(e => ({tag:e.tagName, cls:String(e.className).slice(0,100),
            sh:e.scrollHeight,ch:e.clientHeight,top:e.scrollTop,overflow:getComputedStyle(e).overflow,
            rect:[e.getBoundingClientRect().x,e.getBoundingClientRect().y,
                e.getBoundingClientRect().width,e.getBoundingClientRect().height]}))
        .slice(0,40),
    draft: [...document.querySelectorAll('button,[role="button"],a,[tabindex],div,span')]
        .filter(e => ['保存草稿','保存为草稿','存为草稿','保存到草稿箱','保存并退出','存草稿']
            .includes((e.innerText||e.textContent||'').replace(/\s+/g,' ').trim()))
        .map(e=>({tag:e.tagName,cls:String(e.className).slice(0,100),
            text:(e.innerText||e.textContent||'').trim(),
            rect:[e.getBoundingClientRect().x,e.getBoundingClientRect().y,
                e.getBoundingClientRect().width,e.getBoundingClientRect().height],
            visibility:getComputedStyle(e).visibility,display:getComputedStyle(e).display})),
    text: document.body.innerText.slice(-1500)
}))()
'''))
page.quit()
