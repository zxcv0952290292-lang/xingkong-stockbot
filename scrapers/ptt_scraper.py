#!/usr/bin/env python3
import requests
from bs4 import BeautifulSoup
import csv
import json
import os
from datetime import datetime

TODAY = datetime.now().strftime("%Y%m%d")
BASE = os.environ.get("CACHE_DIR", os.path.dirname(os.path.abspath(__file__)))
os.makedirs(BASE, exist_ok=True)
OUTPUT_FILE = os.path.join(BASE, f"PTT_理財投資_{TODAY}.csv")
CACHE_FILE  = os.path.join(BASE, "ptt_hot_cache.json")

BOARDS = {
    "Stock": "股票",
    "Fund": "基金",
    "Finance": "理財",
    "Insurance": "保險",
    "Creditcard": "信用卡",
}

PAGES_PER_BOARD = 3

SESSION = requests.Session()
SESSION.cookies.set("over18", "1")
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
})

def get_posts(board_id, pages=PAGES_PER_BOARD):
    posts = []
    url = f"https://www.ptt.cc/bbs/{board_id}/index.html"
    for _ in range(pages):
        try:
            res = SESSION.get(url, timeout=10)
            soup = BeautifulSoup(res.text, "html.parser")
            items = soup.select("div.r-ent")
            for item in items:
                try:
                    title_el = item.select_one("div.title a")
                    if not title_el:
                        continue
                    title = title_el.text.strip()
                    link = "https://www.ptt.cc" + title_el["href"]
                    author_el = item.select_one("div.author")
                    author = author_el.text.strip() if author_el else ""
                    date_el = item.select_one("div.date")
                    date = date_el.text.strip() if date_el else ""
                    push_el = item.select_one("div.nrec span")
                    push = push_el.text.strip() if push_el else "0"
                    posts.append({
                        "平台": "PTT",
                        "看板": BOARDS.get(board_id, board_id),
                        "標題": title,
                        "作者": author,
                        "日期": date,
                        "推文數": push,
                        "連結": link,
                        "抓取日期": TODAY
                    })
                except:
                    continue
            # 找上一頁連結
            prev = soup.select_one("a.btn.wide:-soup-contains('上頁')")
            if not prev:
                break
            url = "https://www.ptt.cc" + prev["href"]
        except Exception as e:
            print(f"  ⚠️ 錯誤：{e}")
            break
    return posts

def main():
    print("=" * 45)
    print("  PTT 理財投資文章爬蟲")
    print(f"  日期：{TODAY}")
    print("=" * 45)

    all_posts = []
    for board_id, board_name in BOARDS.items():
        print(f"\n📋 抓取 [{board_name}] 版...")
        posts = get_posts(board_id)
        all_posts.extend(posts)
        print(f"  ✅ 抓到 {len(posts)} 篇")

    # 依推文數排序
    def push_sort(x):
        p = str(x.get("推文數", "0")).replace("爆", "100").replace("X", "-1")
        try:
            return int(p)
        except:
            return 0
    all_posts.sort(key=push_sort, reverse=True)

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["平台","看板","標題","作者","日期","推文數","連結","抓取日期"])
        writer.writeheader()
        writer.writerows(all_posts)

    # 存 JSON cache 供小星空使用（熱門前30篇）
    hot_posts = all_posts[:30]
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "updated": TODAY,
            "posts": [{"看板":p["看板"],"標題":p["標題"],"推文數":p["推文數"],"連結":p["連結"]} for p in hot_posts]
        }, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 共 {len(all_posts)} 篇，已儲存：{OUTPUT_FILE}")
    print(f"📦 PTT Cache 已更新：{CACHE_FILE}")

if __name__ == "__main__":
    main()
