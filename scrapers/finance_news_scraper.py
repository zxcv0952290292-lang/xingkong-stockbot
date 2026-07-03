#!/usr/bin/env python3
import requests
import feedparser
import csv
import os
import json
from datetime import datetime
from bs4 import BeautifulSoup

TODAY = datetime.now().strftime("%Y%m%d_%H%M")
_CACHE_DIR = os.environ.get("CACHE_DIR", os.path.dirname(os.path.abspath(__file__)))
os.makedirs(_CACHE_DIR, exist_ok=True)
OUTPUT_FILE = os.path.join(_CACHE_DIR, f"財經新聞_{TODAY}.csv")
CACHE_FILE = os.path.join(_CACHE_DIR, "finance_news_cache.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

def scrape_yahoo_finance():
    print("📰 Yahoo 股市新聞...")
    results = []
    try:
        feed = feedparser.parse("https://tw.stock.yahoo.com/rss")
        for entry in feed.entries[:15]:
            results.append({
                "來源": "Yahoo股市",
                "標題": entry.get("title", ""),
                "摘要": entry.get("summary", "")[:150].replace("\n", " "),
                "連結": entry.get("link", ""),
                "時間": entry.get("published", ""),
            })
        print(f"  ✅ {len(results)} 篇")
    except Exception as e:
        print(f"  ⚠️ {e}")
    return results

def scrape_bbc_chinese():
    print("📰 BBC 中文財經...")
    results = []
    try:
        feed = feedparser.parse("https://feeds.bbci.co.uk/zhongwen/trad/rss.xml")
        keywords = ["經濟","股","投資","財","市場","貿易","通膨","利率","央行","美元","關稅","科技"]
        for entry in feed.entries[:30]:
            title = entry.get("title", "")
            if any(k in title for k in keywords):
                results.append({
                    "來源": "BBC中文",
                    "標題": title,
                    "摘要": entry.get("summary", "")[:150].replace("\n", " "),
                    "連結": entry.get("link", ""),
                    "時間": entry.get("published", ""),
                })
        print(f"  ✅ {len(results)} 篇")
    except Exception as e:
        print(f"  ⚠️ {e}")
    return results

def scrape_yahoo_finance_news():
    print("📰 Yahoo 財經新聞（完整版）...")
    results = []
    try:
        r = requests.get("https://tw.stock.yahoo.com/news/", headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.select("a[href*='/news/']")[:20]:
            title = a.get_text(strip=True)
            link = a.get("href", "")
            if len(title) < 10: continue
            if not link.startswith("http"): link = "https://tw.stock.yahoo.com" + link
            results.append({
                "來源": "Yahoo財經",
                "標題": title,
                "摘要": "",
                "連結": link,
                "時間": TODAY,
            })
        results = list({r["標題"]: r for r in results if r["標題"]}.values())[:15]
        print(f"  ✅ {len(results)} 篇")
    except Exception as e:
        print(f"  ⚠️ {e}")
    return results

def scrape_moneydj():
    print("📰 MoneyDJ 台股新聞...")
    results = []
    try:
        r = requests.get("https://www.moneydj.com/KMDJ/News/NewsViewer.aspx?a=mb010000", headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.select("a[href*='NewsViewer']")[:15]:
            title = a.get_text(strip=True)
            href = a.get("href", "")
            if len(title) < 5: continue
            link = f"https://www.moneydj.com{href}" if href.startswith("/") else href
            results.append({
                "來源": "MoneyDJ",
                "標題": title,
                "摘要": "",
                "連結": link,
                "時間": TODAY,
            })
        results = list({r["標題"]: r for r in results if r["標題"]}.values())[:15]
        print(f"  ✅ {len(results)} 篇")
    except Exception as e:
        print(f"  ⚠️ {e}")
    return results

def scrape_twse_news():
    print("📰 證交所最新消息...")
    results = []
    try:
        r = requests.get("https://www.twse.com.tw/zh/page/news/announcement.html", headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.select("a[href]")[:15]:
            title = a.get_text(strip=True)
            href = a.get("href", "")
            if len(title) < 5: continue
            link = f"https://www.twse.com.tw{href}" if href.startswith("/") else href
            results.append({
                "來源": "證交所",
                "標題": title,
                "摘要": "",
                "連結": link,
                "時間": TODAY,
            })
        results = list({r["標題"]: r for r in results if r["標題"]}.values())[:10]
        print(f"  ✅ {len(results)} 篇")
    except Exception as e:
        print(f"  ⚠️ {e}")
    return results

def main():
    print("=" * 45)
    print("  多來源財經新聞爬蟲")
    print(f"  時間：{TODAY}")
    print("=" * 45)

    all_news = []
    all_news.extend(scrape_yahoo_finance())
    all_news.extend(scrape_yahoo_finance_news())
    all_news.extend(scrape_bbc_chinese())
    all_news.extend(scrape_moneydj())
    all_news.extend(scrape_twse_news())

    # 存 CSV
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["來源","標題","摘要","連結","時間"])
        writer.writeheader()
        writer.writerows(all_news)

    # 存 JSON cache 供小星空即時使用
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "updated": TODAY,
            "news": all_news
        }, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 共 {len(all_news)} 篇，已儲存：{OUTPUT_FILE}")
    print(f"📦 Cache 已更新：{CACHE_FILE}")

if __name__ == "__main__":
    main()
