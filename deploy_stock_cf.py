#!/usr/bin/env python3
"""把股票網鏡像到 Cloudflare(ferryman-stock)，並補上 AI 供應鏈題材地圖。

在 GitHub Actions 裡接在 twstock_bot.py 之後跑。bot 剛把新頁面部署到 Netlify，
但 Netlify 的 CDN 要幾十秒才吐得出新版 —— 所以這裡會等到頁面上的日期真的是今天
才鏡像，不然會把昨天的頁面推上 Cloudflare 還以為成功了。

（原本這支只能在 Stanley 的 Mac 上跑，因為 wrangler 走本機 OAuth。
  2026-07-12 拿到 CLOUDFLARE_API_TOKEN 之後才搬上雲。）
"""
import os
import sys
import time
from datetime import datetime

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
SRC = "https://iridescent-fox-2b8e7e.netlify.app/"
OUT = os.path.join(BASE, "cf_stock_site")

today = datetime.now().strftime("%Y/%m/%d")

html = None
for i in range(1, 13):                      # 最多等 ~3 分鐘
    try:
        r = requests.get(SRC, timeout=30, headers={"Cache-Control": "no-cache"})
        h = r.text
        if "自選股即時分析" not in h:
            print(f"  [{i}/12] 抓到的 HTML 不對（找不到分析區）")
        elif today not in h:
            print(f"  [{i}/12] Netlify 還是舊版（頁面上沒有 {today}），等 CDN…")
        else:
            html = h
            break
    except Exception as e:
        print(f"  [{i}/12] {type(e).__name__}: {e}")
    time.sleep(15)

if html is None:
    print(f"[deploy_stock_cf] ❌ 等不到 Netlify 更新成 {today}，不鏡像（避免把舊頁面推上 Cloudflare）")
    sys.exit(1)

snippet = open(os.path.join(BASE, "_chain_snippet.html"), encoding="utf-8").read()
if "AI 供應鏈題材地圖" not in html:
    html = html.replace('<div id="searchResult"></div>',
                        '<div id="searchResult"></div>' + snippet, 1)

os.makedirs(OUT, exist_ok=True)
open(os.path.join(OUT, "index.html"), "w", encoding="utf-8").write(html)
print(f"[deploy_stock_cf] ✅ 已生成 cf_stock_site/index.html（{len(html)} bytes，含供應鏈，日期 {today}）")
