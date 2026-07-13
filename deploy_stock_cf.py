#!/usr/bin/env python3
"""把股票網鏡像到 Cloudflare(ferryman-stock)，並補上 AI 供應鏈題材地圖。

在 GitHub Actions 裡接在 twstock_bot.py 之後跑。bot 剛把新頁面部署到 Netlify，
但 Netlify 的 CDN 要幾十秒才吐得出新版——所以這裡要等，等不到就不鏡像，
不然會把舊頁面推上 Cloudflare 還回報成功。

⚠️ 判斷「新不新」不能只看頁面上的日期：一天內重跑第二次時，日期本來就已經是今天了，
   檢查會直接通過，結果鏡像到還沒更新的舊頁面（2026-07-13 移除 LINE QR 時就踩到）。
   所以改成比對「內容有沒有真的變」——拿目前 Cloudflare 上那份的雜湊來比。

（原本這支只能在 Stanley 的 Mac 上跑，因為 wrangler 走本機 OAuth。
  2026-07-12 拿到 CLOUDFLARE_API_TOKEN 之後才搬上雲。）
"""
import hashlib
import os
import sys
import time
from datetime import datetime

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
SRC = "https://iridescent-fox-2b8e7e.netlify.app/"
LIVE = "https://ferryman-stock.pages.dev/"
OUT = os.path.join(BASE, "cf_stock_site")

today = datetime.now().strftime("%Y/%m/%d")


def body_hash(h):
    """只比對推薦區之後的內容，避開每次都會變的時間戳。"""
    i = h.find("自選股即時分析")
    return hashlib.md5(h[i:].encode("utf-8", "ignore")).hexdigest() if i >= 0 else None


# 目前線上那份長什麼樣（拿不到就算了，退回只檢查日期）
try:
    prev = body_hash(requests.get(LIVE, timeout=20, headers={"Cache-Control": "no-cache"}).text)
except Exception:
    prev = None

html = None
for i in range(1, 13):                      # 最多等 ~3 分鐘
    try:
        r = requests.get(SRC, timeout=30, headers={"Cache-Control": "no-cache"})
        h = r.text
        if "自選股即時分析" not in h:
            print(f"  [{i}/12] 抓到的 HTML 不對（找不到分析區）")
        elif today not in h:
            print(f"  [{i}/12] Netlify 還是舊版（頁面上沒有 {today}），等 CDN…")
        elif prev and body_hash(h) == prev:
            print(f"  [{i}/12] Netlify 內容跟 Cloudflare 現有的一模一樣，CDN 可能還沒吐新版，等…")
        else:
            html = h
            break
    except Exception as e:
        print(f"  [{i}/12] {type(e).__name__}: {e}")
    time.sleep(15)

if html is None:
    # 內容真的沒變（例如非交易日重跑）也會走到這裡——這是對的，本來就不用重推。
    print(f"[deploy_stock_cf] ⏭ Netlify 沒有比線上更新的內容，不鏡像")
    sys.exit(0)

snippet = open(os.path.join(BASE, "_chain_snippet.html"), encoding="utf-8").read()
if "AI 供應鏈題材地圖" not in html:
    html = html.replace('<div id="searchResult"></div>',
                        '<div id="searchResult"></div>' + snippet, 1)

os.makedirs(OUT, exist_ok=True)
open(os.path.join(OUT, "index.html"), "w", encoding="utf-8").write(html)
print(f"[deploy_stock_cf] ✅ 已生成 cf_stock_site/index.html（{len(html)} bytes，含供應鏈，日期 {today}）")
