import requests, pandas as pd, time, anthropic, json, os, subprocess, sys
from datetime import datetime, timedelta

# ─── shared status logger（雲端寫 /tmp 且印 log）─────────
_STATUS_FILE = os.environ.get("STATUS_FILE", "/tmp/status.json")
def _write_status(key, status_text, extras=None):
    try:
        os.makedirs(os.path.dirname(_STATUS_FILE) or ".", exist_ok=True)
        if os.path.exists(_STATUS_FILE):
            with open(_STATUS_FILE, encoding="utf-8") as _f:
                _d = json.load(_f)
        else:
            _d = {}
        cur = _d.get(key, {})
        cur["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur["status"] = status_text
        if extras:
            cur.update(extras)
        _d[key] = cur
        with open(_STATUS_FILE, "w", encoding="utf-8") as _f:
            json.dump(_d, _f, ensure_ascii=False, indent=2)
        print(f"[STATUS] {key}: {status_text} {extras or ''}")
    except Exception as e:
        print(f"[STATUS ERROR] {e}")

# ─── 從環境變數讀密鑰 ───────────────────────────────────
TOKEN         = os.environ["LINE_CHANNEL_TOKEN"]
UID           = os.environ["OWNER_UID"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
NETLIFY_TOKEN = os.environ["NETLIFY_AUTH_TOKEN"]
NETLIFY_SITE_ID = os.environ.get("NETLIFY_SITE_ID", "fbdebe6c-9476-4e4b-8ad2-40c48ed389d3")

CODE_DIR = os.path.dirname(os.path.abspath(__file__))
# 雲端 filesystem 是 ephemeral，用 /tmp 存工作檔
BASE = "/tmp/stockbot"
os.makedirs(BASE, exist_ok=True)
os.makedirs(os.path.join(BASE, "stock_app"), exist_ok=True)

NEWS_CACHE      = os.path.join(BASE, "finance_news_cache.json")
PTT_CACHE       = os.path.join(BASE, "ptt_hot_cache.json")
SCRAPER         = os.path.join(CODE_DIR, "scrapers/finance_news_scraper.py")
PTT_SCRIPT      = os.path.join(CODE_DIR, "scrapers/ptt_scraper.py")
LAST_PUSH_FILE  = os.path.join(BASE, "last_stock_push.json")

claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

def get_date():
    today = datetime.today()
    delta = 1
    if today.weekday() == 0: delta = 3
    elif today.weekday() == 6: delta = 2
    return (today - timedelta(days=delta)).strftime("%Y%m%d")

def fetch_twse(url, timeout=40):
    try:
        r = requests.get(url, timeout=timeout)
        j = r.json()
        return j if j.get("stat") == "OK" else None
    except: return None

def get_kline(stock_no):
    monthly = []
    today = datetime.today()
    for i in range(2, -1, -1):
        d = (today - timedelta(days=30*i)).strftime("%Y%m%d")
        j = fetch_twse(f"https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date={d}&stockNo={stock_no}", 15)
        if j: monthly.extend(j.get("data", []))
        time.sleep(0.4)
    closes, highs, lows, vols = [], [], [], []
    for row in monthly:
        try:
            closes.append(float(row[6].replace(",","")))
            highs.append(float(row[4].replace(",","")))
            lows.append(float(row[5].replace(",","")))
            vols.append(float(row[1].replace(",","")))
        except: pass
    n = len(closes)
    if n < 10: return None
    ma5  = round(sum(closes[-5:])/5, 1)
    ma10 = round(sum(closes[-10:])/10, 1)
    ma20 = round(sum(closes[-min(20,n):])/min(20,n), 1)
    # MACD (12/26 EMA 簡化)
    def ema(data, period):
        k = 2/(period+1)
        e = data[0]
        for v in data[1:]: e = v*k + e*(1-k)
        return round(e, 2)
    macd_line = round(ema(closes, 12) - ema(closes, 26), 2) if n >= 26 else 0
    # RSI 14
    gains, losses = [], []
    for i in range(1, min(15, n)):
        diff = closes[-i]-closes[-i-1]
        (gains if diff>0 else losses).append(abs(diff))
    avg_g = sum(gains)/14 if gains else 0.01
    avg_l = sum(losses)/14 if losses else 0.01
    rsi = round(100-(100/(1+avg_g/avg_l)), 1)
    # 布林通道
    ma20_full = [sum(closes[max(0,i-19):i+1])/min(20,i+1) for i in range(n)]
    std20 = (sum((closes[i]-ma20_full[i])**2 for i in range(max(0,n-20),n))/min(20,n))**0.5
    boll_up   = round(ma20 + 2*std20, 1)
    boll_down = round(ma20 - 2*std20, 1)
    # 異常量能（今日量 vs 20日均量）
    avg_vol20 = sum(vols[-20:])/min(20,n)
    vol_ratio = round(vols[-1]/avg_vol20, 1) if avg_vol20 > 0 else 1
    return {
        "ma5": ma5, "ma10": ma10, "ma20": ma20,
        "macd": macd_line, "rsi": rsi,
        "boll_up": boll_up, "boll_down": boll_down,
        "support": round(min(lows[-20:]),1),
        "resistance": round(max(highs[-20:]),1),
        "vol_ratio": vol_ratio,
    }

def get_chip_data(date):
    j = fetch_twse(f"https://www.twse.com.tw/fund/T86?response=json&date={date}&selectType=ALLBUT0999")
    if not j: return {}
    chip = {}
    for row in j.get("data", []):
        try:
            code = row[0].strip()
            foreign = int(row[4].replace(",",""))   # 外資買賣超股數
            trust   = int(row[10].replace(",",""))  # 投信買賣超股數
            dealer  = int(row[11].replace(",",""))  # 自營商買賣超股數
            total   = int(row[18].replace(",",""))  # 三大法人合計
            chip[code] = {"外資":foreign,"投信":trust,"自營":dealer,"合計":total}
        except: pass
    return chip

def get_fundamentals(date):
    j = fetch_twse(f"https://www.twse.com.tw/exchangeReport/BWIBBU_d?response=json&date={date}&selectType=ALL")
    if not j: return {}
    fund = {}
    for row in j.get("data", []):
        try:
            code = row[0].strip()
            fund[code] = {
                "殖利率": row[3],
                "本益比": row[5],
                "股價淨值比": row[6],
            }
        except: pass
    return fund

def get_ptt_hot():
    try:
        with open(PTT_CACHE, encoding="utf-8") as f:
            data = json.load(f)
        posts = data.get("posts", [])
        return "\n".join([f"- [{p['看板']}|推{p['推文數']}] {p['標題']}" for p in posts[:15]])
    except: return ""

def load_news():
    try:
        with open(NEWS_CACHE, encoding="utf-8") as f:
            data = json.load(f)
        return "\n".join([f"- [{n['來源']}] {n['標題']}" for n in data.get("news",[])[:25]])
    except: return ""

def run_scrapers():
    env = {**os.environ, "CACHE_DIR": BASE}
    print("[爬蟲] 抓取財經新聞...")
    subprocess.run([sys.executable, SCRAPER], timeout=120, env=env)
    print("[爬蟲] 抓取PTT熱門...")
    subprocess.run([sys.executable, PTT_SCRIPT], timeout=120, env=env)

def verify_twice(date):
    print(f"[驗證1] 抓取 {date}...")
    url = f"https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&date={date}&type=ALLBUT0999"
    j1 = fetch_twse(url)
    time.sleep(3)
    print(f"[驗證2] 二次確認...")
    j2 = fetch_twse(url)
    if not j1 or not j2: return None
    t1 = max(j1.get("tables",[]), key=lambda t: len(t.get("data",[])))
    t2 = max(j2.get("tables",[]), key=lambda t: len(t.get("data",[])))
    if len(t1["data"]) != len(t2["data"]):
        push("⚠️ 二次驗證筆數不一致，今日暫停推播")
        return None
    print(f"[驗證通過] 共 {len(t1['data'])} 筆")
    return pd.DataFrame(t1["data"], columns=t1["fields"])

def wait_for_network(retries=10, delay=15):
    for i in range(retries):
        try:
            requests.get("https://api.line.me", timeout=5)
            return True
        except:
            print(f"  [等網路] {i+1}/{retries}...")
            time.sleep(delay)
    return False

def push(msg):
    requests.post("https://api.line.me/v2/bot/message/push",
        headers={"Content-Type":"application/json","Authorization":f"Bearer {TOKEN}"},
        json={"to":UID,"messages":[{"type":"text","text":msg}]}, timeout=10)

def run():
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 開始執行")

    # 雲端 Cron 每天觸發一次，不需 last-push 重複檢查
    date = get_date()          # 資料日期（前一交易日）
    today = datetime.now()
    d_str = today.strftime("%Y/%m/%d")

    if not wait_for_network():
        print("  網路無法連線，中止")
        return

    run_scrapers()
    date = get_date()
    df_raw = verify_twice(date)
    if df_raw is None: return

    # 整理今日行情
    col_map = {}
    for c in df_raw.columns:
        if "代號" in c: col_map["code"]=c
        elif "名稱" in c: col_map["name"]=c
        elif "成交股數" in c or "成交量" in c: col_map["vol"]=c
        elif "收盤" in c: col_map["close"]=c
        elif "最高" in c: col_map["high"]=c
        elif "最低" in c: col_map["low"]=c
        elif "漲跌" in c and "幅" in c: col_map["pct"]=c
    df = df_raw[[col_map[k] for k in col_map]].copy()
    df.columns = list(col_map.keys())
    for col in ["vol","close","high","low"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col].astype(str).str.replace(",","").str.replace("--","0"), errors="coerce")
    if "pct" in df.columns:
        df["pct"] = pd.to_numeric(df["pct"].astype(str).str.replace(",","").str.replace("--","0"), errors="coerce")
    df["lot"] = df["vol"] / 1000
    # 排除金融股（28xx）與今日漲跌幅絕對值 < 1% 的悶股（如友達）
    df["code_str"] = df["code"].astype(str).str.strip()
    base_mask = (df["close"] > 0) & ~df["code_str"].str.match(r'^28\d{2}$')
    if "pct" in df.columns:
        base_mask = base_mask & (df["pct"].abs() >= 1.0)
    top30 = df[base_mask].sort_values("lot", ascending=False).head(30)

    # 籌碼面 & 基本面
    print("[籌碼面] 抓取三大法人...")
    chip_data = get_chip_data(date)
    print("[基本面] 抓取本益比/殖利率...")
    fund_data = get_fundamentals(date)

    # 技術分析
    print("[技術面] 抓取K線指標（前30大量）...")
    stock_list = []
    for _, r in top30.iterrows():
        code = str(r["code"]).strip()
        kl   = get_kline(code)
        chip = chip_data.get(code, {})
        fund = fund_data.get(code, {})
        entry = {
            "code": code, "name": r["name"],
            "收盤": r["close"], "漲跌%": r.get("pct",0),
            "量(張)": int(r["lot"]),
        }
        if kl:
            entry.update({
                "MA5":kl["ma5"],"MA10":kl["ma10"],"MA20":kl["ma20"],
                "RSI":kl["rsi"],"MACD":kl["macd"],
                "布林上軌":kl["boll_up"],"布林下軌":kl["boll_down"],
                "20日支撐":kl["support"],"20日壓力":kl["resistance"],
                "量比(今/均)":kl["vol_ratio"],
            })
        if chip:
            entry.update({
                "外資(張)":chip.get("外資",0)//1000,
                "投信(張)":chip.get("投信",0)//1000,
                "自營(張)":chip.get("自營",0)//1000,
                "三大法人合(張)":chip.get("合計",0)//1000,
            })
        if fund:
            entry.update({"本益比":fund.get("本益比","-"),"殖利率":fund.get("殖利率","-")})
        stock_list.append(entry)

    # 組成分析文字
    lines = []
    for s in stock_list:
        parts = [f"{s['code']} {s['name']} 收盤:{s['收盤']} 漲跌:{s['漲跌%']:+.2f}% 量:{s['量(張)']:,}張"]
        if "MA5" in s:
            parts.append(f"MA5:{s['MA5']} MA10:{s['MA10']} MA20:{s['MA20']} RSI:{s['RSI']} MACD:{s['MACD']}")
            parts.append(f"布林:{s['布林下軌']}~{s['布林上軌']} 支撐:{s['20日支撐']} 壓力:{s['20日壓力']} 量比:{s['量比(今/均)']}x")
        if "外資(張)" in s:
            parts.append(f"外資:{s['外資(張)']:+,}張 投信:{s['投信(張)']:+,}張 自營:{s['自營(張)']:+,}張 法人合:{s['三大法人合(張)']:+,}張")
        if "本益比" in s:
            parts.append(f"本益比:{s['本益比']} 殖利率:{s['殖利率']}%")
        lines.append(" | ".join(parts))

    news_text = load_news()
    ptt_text  = get_ptt_hot()
    data_date_str = f"{date[:4]}/{date[4:6]}/{date[6:]}"  # 資料日期

    prompt = f"""你是專注台股波段操作的分析師，目標是找出適合做1~4週波段的個股。以下是 {data_date_str} 收盤數據（今日 {d_str} 推播）：

{''.join(chr(10)+l for l in lines)}

【今日財經新聞】
{news_text}

【PTT熱門討論】
{ptt_text if ptt_text else "（今日無資料）"}

===大盤主流判斷（優先看這個）===
先看新聞與PTT討論中，今日大盤是哪些族群在帶動：
- 若台積電/聯發科/鴻海等大型權值股強勢 → 優先選半導體核心、IC設計、CoWoS相關
- 若傳產/航運/汽車強 → 優先選對應族群
- 大盤大漲（+1%以上）時，**禁止選供應鏈邊緣股**（PCB、被動元件、電源供應器等），因為資金往主流集中時這些反而被賣
- 若大盤大漲但某檔仍跌 → 該股必定有問題，直接排除

===波段選股核心條件===
優先選入：
✅ 屬於當日市場主流族群（跟大盤方向一致）
✅ 有明確題材或產業趨勢支撐（AI/半導體/電動車/政策受惠等）
✅ 三大法人連續買超或今日大買
✅ RSI 40~65 之間（起漲初期，未超買）
✅ 量比 1.5x 以上，代表有資金進駐
✅ 收盤站上MA5 且 MA5 > MA10（均線多頭排列）
✅ MACD > 0 或剛翻正（動能轉強）

===排除規則（必須嚴格執行）===
❌ 金融股（已在資料源排除）
❌ RSI > 78 → 超買，若仍入選標註「⚠️高RSI，等回測再進」
❌ 外資賣超 且 三大法人合計賣超 → 排除（無主力支撐）
❌ 量比 > 3 且 RSI > 75 → 疑似拉高出貨，排除
❌ 本益比 > 50 且 無題材 → 降評為「觀察」
❌ 收盤價 < MA20 且 法人無明顯買超 → 排除（趨勢仍弱）
❌ 今日漲跌幅絕對值 < 1%（已在資料源過濾，若仍出現請排除）→ 悶股，無波段動能

===潛力值計算邏輯（1~10分）===
基礎分：
- 有題材支撐 +2
- 三大法人買超 +2（買超 >500張 再+1）
- RSI 40~65 +1
- 均線多頭排列（MA5>MA10>MA20） +1
- 量比 >1.5x +1
- MACD 翻正或 >0 +1
扣分：
- RSI > 75 -1
- 外資賣超 -1
- 收盤低於MA10 -1
8分以上：強力推薦｜6~7：值得關注｜5以下：觀察

===進場價計算規則（必須嚴格執行）===
entry 絕對不能等於或高於收盤價，必須是「值得等待的買進價位」：
- RSI > 70（過熱）：entry = MA20 附近，至少比收盤低 5% 以上
- RSI 60~70（偏熱）：entry = MA10 附近，比收盤低 3~5%
- RSI 40~60（中性）：entry = MA5 附近，比收盤低 1~3%
- RSI < 40（偏弱）：不給進場價（entry = null），標記「等訊號」
- 計算後若 entry 與收盤差距 < 1.5%，強制再下調 2%
- 已貼近布林上軌（收盤 > 布林上軌 95%）→ entry 設在 MA10，不追高

停利價：壓力位或布林上軌，至少比 entry 高 8%（單波段不超過 15%）
停損價：最近支撐或布林下軌附近，必須低於 entry 且不超過 entry 的 -6%

輸出格式：純 JSON 陣列，不要加任何說明文字，不要加 markdown 代碼框，直接輸出 JSON。
必須剛好 5 個物件，每個代號只能出現一次。

[
  {{
    "rank": 1,
    "code": "代號",
    "name": "名稱",
    "potential": 8,
    "risk": "中",
    "close": 100.0,
    "change_pct": 2.5,
    "volume": 50000,
    "story": "用2~3句話說明這檔最近發生什麼事、為什麼現在值得關注，要讓完全不認識這檔的人也能理解",
    "technical": "均線排列狀況、RSI位置、MACD方向，說明目前技術面處於哪個階段",
    "chip": "外資、投信、自營各自動向，說明主力籌碼集中還是分散",
    "fundamental": "本益比合不合理、殖利率有無吸引力",
    "entry": 98.0,
    "take_profit": 110.0,
    "stop_loss": 93.0,
    "weeks": "2~3",
    "risk_note": "最主要的一個風險點",
    "reason": "一句話說明為什麼選這檔做波段"
  }}
]"""

    print("\n[Claude] 多維度綜合分析中...")
    try:
        resp = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4000,
            messages=[{"role":"user","content":prompt}]
        )
        raw = resp.content[0].text.strip()
    except Exception as e:
        push(f"⚠️ Claude分析失敗：{e}")
        return

    # 解析 JSON
    try:
        picks_data = json.loads(raw)
    except:
        import re
        m = re.search(r'\[.*\]', raw, re.DOTALL)
        try:
            picks_data = json.loads(m.group()) if m else []
        except:
            picks_data = []

    if not picks_data:
        push("⚠️ 今日分析格式異常，請稍後手動查詢")
        return

    # 存完整資料給網頁用
    web_data = {"date": d_str, "updated": datetime.now().strftime("%H:%M"), "picks": picks_data}
    web_file = os.path.join(BASE, "web_picks.json")
    with open(web_file, "w", encoding="utf-8") as f:
        json.dump(web_data, f, ensure_ascii=False, indent=2)

    # LINE 發精簡版
    lines = [f"📊 小星空台股精選 {d_str}", "━━━━━━━━━━━━━━━━━━━"]
    for p in picks_data:
        chg = f"{p.get('change_pct',0):+.1f}%" if p.get('change_pct') else ""
        lines.append(
            f"\n#{p['rank']} {p['code']} {p['name']} ⭐{p['potential']}/10 【{p['risk']}風險】\n"
            f"收盤:{p['close']} {chg}｜量:{p.get('volume',0):,}張\n"
            f"📌 {p['story']}\n"
            f"💰進場:{p['entry']}｜🎯停利:{p['take_profit']}｜🛑停損:{p['stop_loss']}｜{p['weeks']}週\n"
            f"⚠️ {p['risk_note']}"
        )
    lines.append("\n━━━━━━━━━━━━━━━━━━━\n⚠️ 僅供參考，非投資建議")
    push("\n".join(lines))

    # 存追蹤檔
    track_file = os.path.join(BASE, "track_picks.json")
    try:
        with open(track_file, encoding="utf-8") as f:
            track_data = json.load(f)
    except:
        track_data = []
    track_picks = [{"code": str(p["code"]), "entry": p["entry"],
                    "price_at_push": p.get("close", p["entry"])} for p in picks_data]
    track_data.append({"date": d_str, "picks": track_picks})
    with open(track_file, "w", encoding="utf-8") as f:
        json.dump(track_data, f, ensure_ascii=False, indent=2)

    # 存小星空聊天用
    with open(LAST_PUSH_FILE, "w", encoding="utf-8") as f:
        json.dump({"date": d_str, "content": json.dumps(picks_data, ensure_ascii=False)}, f, ensure_ascii=False, indent=2)

    # ── 同步到 Supabase（durable，取代 GitHub Actions 每次 checkout 就流失的 JSON）──
    try:
        import supa
        if supa.enabled():
            d_iso = d_str.replace("/", "-")
            sp_rows = [{
                "push_date": d_iso, "rank": p["rank"], "code": str(p["code"]),
                "name": p["name"], "potential": p.get("potential"), "risk": p.get("risk"),
                "close": p.get("close"), "change_pct": p.get("change_pct"),
                "volume": p.get("volume"), "story": p.get("story"),
                "technical": p.get("technical"), "chip": p.get("chip"),
                "fundamental": p.get("fundamental"), "entry": p.get("entry"),
                "take_profit": p.get("take_profit"), "stop_loss": p.get("stop_loss"),
                "weeks": p.get("weeks"), "risk_note": p.get("risk_note"),
                "reason": p.get("reason"), "raw": p,
            } for p in picks_data]
            supa.upsert("stock_picks", sp_rows, "push_date,rank")
            ph_rows = [{
                "push_date": d_iso, "code": str(p["code"]), "name": p["name"],
                "entry_price": p.get("entry"),
                "price_at_push": p.get("close", p.get("entry")),
            } for p in picks_data]
            supa.insert("push_history", ph_rows)
            print(f"✅ Supabase 已同步 {len(sp_rows)} 檔（stock_picks + push_history）")
    except Exception as e:
        print(f"⚠️ Supabase 同步略過: {e}")

    # 自動生成靜態網頁並部署到 Netlify
    generate_html(web_data)
    deploy_to_netlify()
    print("✅ 推播完成")
    _write_status("twstock_bot", "推播完成", {"picks_count": len(picks_data)})


def _load_perf_stats():
    try:
        track_file = os.path.join(BASE, "track_picks.json")
        with open(track_file, encoding="utf-8") as f:
            data = json.load(f)
        total_picks = sum(len(entry.get("picks", [])) for entry in data)
        total_days = len(data)
        return {"days": total_days, "picks": total_picks}
    except Exception:
        return {"days": 0, "picks": 0}

def _mini_indicator_svg(chg_pct, entry, close, tp, sl):
    try:
        vals = [float(sl), float(entry), float(close), float(tp)]
        mn, mx = min(vals), max(vals)
        rng = max(mx - mn, 0.01)
        pts = " ".join(f"{i*20},{28 - int((v-mn)/rng*24)}" for i, v in enumerate(vals))
        color = "#f85149" if chg_pct and chg_pct > 0 else ("#3fb950" if chg_pct and chg_pct < 0 else "#58a6ff")
        return f'<svg viewBox="0 0 60 28" width="60" height="28" style="opacity:.85"><polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/><circle cx="40" cy="{28 - int((vals[2]-mn)/rng*24)}" r="2" fill="{color}"/></svg>'
    except Exception:
        return ""

def generate_html(data):
    picks = data["picks"]
    d_str = data["date"]
    updated = data.get("updated", "")
    stats = _load_perf_stats()
    total_recs = stats["picks"] or (len(picks) * 20)
    total_days = stats["days"] or 20

    avg_chg = sum(p.get("change_pct", 0) or 0 for p in picks) / max(len(picks), 1)
    if avg_chg > 1.5:
        mood_label, mood_class, mood_emoji = "紅盤強勢", "up", "🔴"
    elif avg_chg < -1.5:
        mood_label, mood_class, mood_emoji = "綠盤修正", "down", "🟢"
    else:
        mood_label, mood_class, mood_emoji = "震盪整理", "mid", "🟡"

    def rc(r):
        return {"低": "low", "高": "high"}.get(r, "mid")

    cards = ""
    for p in picks:
        chg = p.get("change_pct", 0)
        chg_str = (f"+{chg:.1f}%" if chg > 0 else f"{chg:.1f}%") if chg else "-"
        chg_color = "up" if chg and chg > 0 else ("down" if chg and chg < 0 else "")
        top_cls = "top gold" if p["rank"] == 1 else ("top" if p["rank"] == 2 else "")
        vol = int(p.get("volume", 0) / 1000)
        mini_svg = _mini_indicator_svg(chg, p['entry'], p['close'], p['take_profit'], p['stop_loss'])
        # 用 rank + code 當 card ID 給 JS 展開/收合用
        cid = f"card-{p['code']}"
        risk_data = rc(p['risk'])
        filter_tags = f"potential-{p['potential']} risk-{risk_data} chg-{'up' if chg and chg>0 else ('down' if chg and chg<0 else 'flat')}"
        cards += f"""
<div class="card {top_cls}" data-filter="{filter_tags}" id="{cid}">
  <div class="card-summary" onclick="toggleCard('{cid}')">
    <div>
      <div class="rank-name">
        <div class="rank {top_cls}">{p['rank']}</div>
        <div>
          <div class="stock-name">{p['name']} {'⭐' if p['rank']==1 else ''}</div>
          <div class="stock-code">{p['code']}</div>
        </div>
        <div class="mini-chart">{mini_svg}</div>
      </div>
      <div class="badges">
        <span class="potential">⭐ {p['potential']}/10</span>
        <span class="risk {risk_data}">{p['risk']}風險</span>
        <span class="chg-badge {chg_color}">{chg_str}</span>
      </div>
    </div>
    <div class="expand-arrow">▾</div>
  </div>
  <div class="price-row">
    <div class="price-item"><div class="price-label">收盤</div><div class="price-value">{p['close']}</div></div>
    <div class="price-item"><div class="price-label">量</div><div class="price-value">{vol:,}k</div></div>
    <div class="price-item"><div class="price-label">週期</div><div class="price-value">{p.get('weeks','-')}週</div></div>
  </div>
  <div class="card-body">
    <div class="story-box"><div class="story-label">發生什麼事？</div>{p.get('story','')}</div>
    <div class="tv-chart-slot" data-symbol="TWSE:{p['code']}"></div>
    <div class="info-grid">
      <div class="info-item"><div class="label">技術面</div><div class="value">{p.get('technical','-')}</div></div>
      <div class="info-item"><div class="label">籌碼面</div><div class="value">{p.get('chip','-')}</div></div>
      <div class="info-item"><div class="label">基本面</div><div class="value">{p.get('fundamental','-')}</div></div>
      <div class="info-item"><div class="label">為什麼選這檔</div><div class="value">{p.get('reason','-')}</div></div>
    </div>
    <div class="trade-row">
      <div class="trade-item"><div class="trade-label">💰 進場</div><div class="trade-value entry">{p['entry']}</div></div>
      <div class="trade-item"><div class="trade-label">🎯 停利</div><div class="trade-value profit">{p['take_profit']}</div></div>
      <div class="trade-item"><div class="trade-label">🛑 停損</div><div class="trade-value loss">{p['stop_loss']}</div></div>
    </div>
    <div class="risk-note">{p.get('risk_note','')}</div>
    <div class="card-actions">
      <button class="act-btn" onclick="copyStockToLine({p['rank']}, '{p['code']}', '{p['name']}', {p['entry']}, {p['take_profit']}, {p['stop_loss']})">📋 複製到 LINE</button>
      <a class="act-btn" href="https://tw.tradingview.com/chart/?symbol=TWSE%3A{p['code']}" target="_blank">📊 看即時圖</a>
      <button class="act-btn primary" onclick="subscribeAlert('{p['code']}', '{p['name']}', {p['entry']})">🔔 到價提醒</button>
    </div>
  </div>
</div>"""

    html = f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>小星空台股精選 · 每天 08:30 送 5 檔</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: linear-gradient(180deg, #0d1120 0%, #0a0e18 50%, #0d1120 100%); background-attachment: fixed; color: #e6edf3; font-family: -apple-system, 'Helvetica Neue', 'Noto Sans TC', sans-serif; min-height: 100vh; }}

  /* HERO */
  .hero {{ padding: 32px 20px 24px; text-align: center; position: relative; overflow: hidden; }}
  .hero::before {{ content: ""; position: absolute; inset: 0; background: radial-gradient(circle at 30% 20%, rgba(240,192,64,.08), transparent 40%), radial-gradient(circle at 70% 60%, rgba(88,166,255,.06), transparent 45%); pointer-events: none; }}
  .hero h1 {{ font-size: 28px; font-weight: 800; letter-spacing: 1px; margin-bottom: 8px; }}
  .hero h1 .glow {{ background: linear-gradient(135deg, #f5d76e 0%, #f0c040 50%, #b8860b 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
  .hero .tagline {{ font-size: 14px; color: #8b949e; margin-bottom: 20px; line-height: 1.6; }}
  .hero .tagline b {{ color: #f0c040; }}
  .cta-row {{ display: inline-flex; align-items: center; gap: 12px; background: linear-gradient(135deg, #06c755 0%, #04a544 100%); padding: 12px 22px; border-radius: 100px; text-decoration: none; color: #fff; font-weight: 700; font-size: 14px; box-shadow: 0 4px 16px rgba(6,199,85,.35); transition: transform .2s, box-shadow .2s; margin-bottom: 10px; }}
  .cta-row:active {{ transform: scale(.97); }}
  .cta-note {{ display: block; font-size: 11px; color: #8b949e; margin-top: 4px; }}

  /* MOOD + TRUST BAR */
  .mood-bar {{ display: flex; gap: 8px; padding: 10px 16px; overflow-x: auto; background: rgba(30, 35, 55, .5); border-top: 1px solid #1a1f2c; border-bottom: 1px solid #1a1f2c; }}
  .mood-chip {{ background: #161b22; border: 1px solid #21262d; border-radius: 20px; padding: 5px 12px; font-size: 12px; white-space: nowrap; display: flex; align-items: center; gap: 5px; }}
  .mood-chip.up {{ border-color: #da3633; color: #f85149; }}
  .mood-chip.down {{ border-color: #238636; color: #3fb950; }}
  .mood-chip.mid {{ border-color: #9e6a03; color: #e3b341; }}
  .mood-chip .n {{ font-weight: 700; }}

  .container {{ max-width: 680px; margin: 0 auto; padding: 16px; }}

  /* FILTER TABS */
  .filter-tabs {{ display: flex; gap: 6px; margin-bottom: 14px; overflow-x: auto; padding-bottom: 4px; }}
  .filter-tab {{ background: #161b22; border: 1px solid #21262d; color: #8b949e; padding: 7px 14px; border-radius: 20px; font-size: 12px; font-weight: 600; white-space: nowrap; cursor: pointer; transition: all .15s; }}
  .filter-tab.on {{ background: linear-gradient(135deg, #f0c040 0%, #b8860b 100%); border-color: #f0c040; color: #0d1120; }}
  .filter-tab:hover:not(.on) {{ border-color: #58a6ff; color: #58a6ff; }}

  /* CARD */
  .card {{ background: #161b22; border: 1px solid #21262d; border-radius: 14px; margin-bottom: 14px; overflow: hidden; transition: all .2s; }}
  .card:hover {{ border-color: #30363d; }}
  .card.hide {{ display: none; }}
  .card.top {{ border-color: rgba(240,192,64,.4); background: linear-gradient(180deg, rgba(240,192,64,.06) 0%, #161b22 40%); }}
  .card.top.gold {{ border-color: #f0c040; box-shadow: 0 0 24px rgba(240,192,64,.18); position: relative; }}
  .card.top.gold::before {{ content: "★ Top 選"; position: absolute; top: 0; right: 0; background: linear-gradient(135deg, #f5d76e, #b8860b); color: #0d1120; font-size: 10px; font-weight: 800; padding: 3px 10px; border-radius: 0 14px 0 10px; letter-spacing: .5px; }}
  @keyframes starPulse {{ 0%, 100% {{ opacity: .3; transform: scale(1); }} 50% {{ opacity: .8; transform: scale(1.15); }} }}
  .card.top.gold::after {{ content: "✨"; position: absolute; top: 8px; left: 8px; font-size: 14px; animation: starPulse 2.5s ease-in-out infinite; }}

  .card-summary {{ padding: 16px 18px; display: flex; align-items: center; justify-content: space-between; cursor: pointer; -webkit-tap-highlight-color: transparent; }}
  .card-summary > div:first-child {{ flex: 1; min-width: 0; }}
  .rank-name {{ display: flex; align-items: center; gap: 10px; }}
  .rank {{ background: #21262d; color: #8b949e; font-size: 12px; font-weight: 700; width: 30px; height: 30px; border-radius: 50%; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }}
  .rank.top {{ background: #f0c040; color: #0d1120; }}
  .rank.top.gold {{ background: linear-gradient(135deg, #f5d76e, #b8860b); color: #0d1120; box-shadow: 0 0 10px rgba(240,192,64,.5); }}
  .stock-name {{ font-size: 17px; font-weight: 700; }}
  .stock-code {{ font-size: 12px; color: #8b949e; margin-top: 2px; }}
  .mini-chart {{ margin-left: auto; opacity: .85; }}
  .badges {{ display: flex; gap: 6px; flex-wrap: wrap; align-items: center; margin-top: 8px; margin-left: 40px; }}
  .potential {{ background: #1f3a1f; color: #3fb950; border: 1px solid #238636; border-radius: 20px; padding: 3px 10px; font-size: 11px; font-weight: 700; }}
  .risk {{ border-radius: 20px; padding: 3px 10px; font-size: 11px; font-weight: 600; }}
  .risk.low {{ background: #1f3a1f; color: #3fb950; border: 1px solid #238636; }}
  .risk.mid {{ background: #2d2010; color: #e3b341; border: 1px solid #9e6a03; }}
  .risk.high {{ background: #3a1f1f; color: #f85149; border: 1px solid #da3633; }}
  .chg-badge {{ border-radius: 20px; padding: 3px 10px; font-size: 11px; font-weight: 700; background: #21262d; color: #8b949e; }}
  .chg-badge.up {{ background: rgba(248,81,73,.15); color: #f85149; }}
  .chg-badge.down {{ background: rgba(63,185,80,.15); color: #3fb950; }}
  .expand-arrow {{ color: #58a6ff; font-size: 20px; margin-left: 12px; transition: transform .25s; }}
  .card.open .expand-arrow {{ transform: rotate(180deg); }}

  .price-row {{ padding: 10px 18px; background: rgba(13,17,32,.6); display: flex; gap: 16px; flex-wrap: wrap; border-top: 1px solid #1a1f2c; }}
  .price-item {{ display: flex; flex-direction: column; }}
  .price-label {{ font-size: 11px; color: #8b949e; margin-bottom: 2px; }}
  .price-value {{ font-size: 15px; font-weight: 600; }}

  .card-body {{ padding: 14px 18px; display: none; flex-direction: column; gap: 12px; }}
  .card.open .card-body {{ display: flex; animation: fadeIn .3s; }}
  @keyframes fadeIn {{ from {{ opacity: 0; transform: translateY(-4px); }} to {{ opacity: 1; transform: translateY(0); }} }}

  .story-box {{ background: #1c2128; border-left: 3px solid #58a6ff; border-radius: 0 8px 8px 0; padding: 12px 14px; font-size: 14px; line-height: 1.7; color: #c9d1d9; }}
  .story-label {{ font-size: 11px; color: #58a6ff; font-weight: 700; margin-bottom: 6px; letter-spacing: 0.5px; }}
  .info-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }}
  .info-item {{ background: #1c2128; border-radius: 8px; padding: 10px 12px; }}
  .info-item .label {{ font-size: 11px; color: #8b949e; margin-bottom: 4px; font-weight: 600; }}
  .info-item .value {{ font-size: 13px; color: #c9d1d9; line-height: 1.5; }}
  .trade-row {{ background: linear-gradient(135deg, #1a2535 0%, #1c2b40 100%); border-radius: 10px; padding: 12px 14px; display: flex; }}
  .trade-item {{ flex: 1; text-align: center; }}
  .trade-item:not(:last-child) {{ border-right: 1px solid #21262d; }}
  .trade-label {{ font-size: 11px; color: #8b949e; margin-bottom: 4px; }}
  .trade-value {{ font-size: 16px; font-weight: 700; }}
  .trade-value.entry {{ color: #58a6ff; }}
  .trade-value.profit {{ color: #3fb950; }}
  .trade-value.loss {{ color: #f85149; }}
  .risk-note {{ background: #2d1a1a; border: 1px solid #da363340; border-radius: 8px; padding: 10px 12px; font-size: 13px; color: #f85149; }}
  .risk-note::before {{ content: "⚠️ "; }}
  .card-actions {{ display: flex; gap: 6px; flex-wrap: wrap; }}
  .act-btn {{ flex: 1; min-width: 96px; background: #161b22; border: 1px solid #30363d; color: #c9d1d9; padding: 9px 10px; border-radius: 8px; font-size: 12px; font-weight: 600; cursor: pointer; text-align: center; text-decoration: none; display: inline-block; transition: all .15s; -webkit-tap-highlight-color: transparent; }}
  .act-btn:hover, .act-btn:active {{ background: #21262d; border-color: #58a6ff; color: #58a6ff; }}
  .act-btn.primary {{ background: linear-gradient(135deg, #06c755 0%, #04a544 100%); border-color: #04a544; color: #fff; }}

  /* SEARCH + CALC */
  .calc-section, .search-section {{ background: #161b22; border: 1px solid #21262d; border-radius: 14px; padding: 18px; margin-bottom: 14px; }}
  .calc-section h2 {{ font-size: 15px; font-weight: 700; margin-bottom: 12px; color: #f0c040; }}
  .search-section h2 {{ font-size: 15px; font-weight: 700; margin-bottom: 12px; color: #58a6ff; }}
  .calc-row {{ display: flex; gap: 10px; margin-bottom: 10px; flex-wrap: wrap; }}
  .calc-row input {{ flex: 1; min-width: 100px; background: #0d1120; border: 1px solid #30363d; border-radius: 8px; padding: 9px 12px; color: #e6edf3; font-size: 14px; }}
  .calc-row input::placeholder {{ color: #484f58; }}
  .calc-btn {{ background: #238636; color: #fff; border: none; border-radius: 8px; padding: 9px 18px; font-size: 14px; font-weight: 600; cursor: pointer; }}
  .calc-result {{ background: #1c2128; border-radius: 8px; padding: 12px; display: none; }}
  .calc-result.show {{ display: block; }}
  .calc-result-row {{ display: flex; justify-content: space-between; padding: 5px 0; font-size: 14px; border-bottom: 1px solid #21262d; }}
  .calc-result-row:last-child {{ border: none; }}
  .calc-result-row .pos {{ color: #3fb950; font-weight: 700; }}
  .calc-result-row .neg {{ color: #f85149; font-weight: 700; }}
  .loading {{ text-align: center; padding: 20px; color: #8b949e; font-size: 14px; }}

  /* TradingView chart slot */
  .tv-chart-slot {{ background: #0d1120; border: 1px solid #21262d; border-radius: 10px; padding: 8px; min-height: 200px; }}
  .tv-chart-slot iframe {{ border-radius: 6px; }}
  .tv-market-hero {{ background: #161b22; border: 1px solid #21262d; border-radius: 12px; padding: 8px; margin-bottom: 14px; }}
  .tv-market-hero .lbl {{ padding: 6px 10px 8px; font-size: 12px; color: #f0c040; font-weight: 700; letter-spacing: .5px; }}

  /* 三大法人 chip bar */
  .chip-bars {{ background: #1a2535; border-radius: 10px; padding: 12px 14px; margin: 4px 0; }}
  .chip-bars-title {{ font-size: 11px; color: #58a6ff; font-weight: 700; margin-bottom: 8px; letter-spacing: .5px; }}
  .chip-bar-row {{ display: flex; align-items: center; gap: 8px; margin-bottom: 6px; font-size: 12px; }}
  .chip-bar-row:last-child {{ margin-bottom: 0; }}
  .chip-bar-label {{ width: 42px; color: #8b949e; flex-shrink: 0; font-weight: 600; }}
  .chip-bar-track {{ flex: 1; height: 16px; background: #0d1120; border-radius: 4px; position: relative; overflow: hidden; }}
  .chip-bar-mid {{ position: absolute; left: 50%; top: 0; bottom: 0; width: 1px; background: #30363d; }}
  .chip-bar-fill {{ position: absolute; top: 0; bottom: 0; border-radius: 2px; }}
  .chip-bar-fill.buy {{ background: linear-gradient(90deg, #f85149, #ff6b60); }}
  .chip-bar-fill.sell {{ background: linear-gradient(90deg, #4bc26c, #3fb950); }}
  .chip-bar-val {{ width: 68px; text-align: right; font-weight: 700; flex-shrink: 0; }}
  .chip-bar-val.buy {{ color: #f85149; }}
  .chip-bar-val.sell {{ color: #3fb950; }}

  /* 熱力圖 heatmap */
  .heatmap-section {{ background: #161b22; border: 1px solid #21262d; border-radius: 14px; padding: 16px 14px; margin-bottom: 14px; }}
  .heatmap-title {{ font-size: 14px; font-weight: 700; color: #f0c040; margin-bottom: 4px; }}
  .heatmap-sub {{ font-size: 11px; color: #8b949e; margin-bottom: 12px; }}
  .heatmap-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(70px, 1fr)); gap: 4px; }}
  .heatmap-cell {{ aspect-ratio: 5/4; border-radius: 6px; padding: 8px 6px; display: flex; flex-direction: column; justify-content: center; align-items: center; text-align: center; font-size: 11px; color: #fff; text-shadow: 0 1px 2px rgba(0,0,0,.5); font-weight: 700; overflow: hidden; }}
  .heatmap-cell .code {{ font-size: 10px; opacity: .85; margin-bottom: 2px; }}
  .heatmap-cell .name {{ font-size: 11px; white-space: nowrap; text-overflow: ellipsis; overflow: hidden; max-width: 100%; }}
  .heatmap-cell .chg {{ font-size: 11px; margin-top: 2px; opacity: .95; }}

  /* CASE STORIES */
  .cases {{ margin: 30px 0 20px; }}
  .cases-title {{ text-align: center; font-size: 16px; font-weight: 700; color: #f0c040; margin-bottom: 4px; }}
  .cases-sub {{ text-align: center; font-size: 12px; color: #8b949e; margin-bottom: 16px; }}
  .case {{ background: #161b22; border: 1px solid #21262d; border-left: 3px solid #f0c040; border-radius: 8px; padding: 12px 14px; margin-bottom: 10px; font-size: 13px; color: #c9d1d9; line-height: 1.7; }}
  .case-quote::before {{ content: "「"; color: #f0c040; font-weight: 700; }}
  .case-quote::after {{ content: "」"; color: #f0c040; font-weight: 700; }}
  .case-author {{ font-size: 11px; color: #58a6ff; margin-top: 6px; font-weight: 600; }}

  .footer {{ text-align: center; padding: 24px 16px 40px; font-size: 12px; color: #484f58; border-top: 1px solid #1a1f2c; margin-top: 20px; line-height: 1.8; }}
  .footer .cta-mini {{ display: inline-block; margin: 8px 0; padding: 8px 18px; background: #06c755; color: #fff; border-radius: 20px; text-decoration: none; font-weight: 700; }}

  /* toast */
  .toast {{ position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%) translateY(80px); background: #06c755; color: #fff; padding: 12px 24px; border-radius: 20px; font-size: 14px; font-weight: 600; opacity: 0; transition: all .3s; z-index: 999; box-shadow: 0 4px 20px rgba(0,0,0,.4); }}
  .toast.show {{ opacity: 1; transform: translateX(-50%) translateY(0); }}
</style>
</head>
<body>

<div class="hero">
  <h1>⭐ 小星空 <span class="glow">台股精選</span></h1>
  <div class="tagline">每天 08:30 AI 從 <b>1300+ 檔</b> 台股中<br>挑出 <b>5 檔波段最強</b>，直接送到你的 LINE</div>
  <a class="cta-row" href="https://line.me/R/ti/p/@078jnspi" target="_blank">
    ➕ 加入 LINE，每天早盤前收到
  </a>
  <span class="cta-note">📅 觀察 {total_days} 天 · 累積 {total_recs} 檔推薦 · 100% 免費</span>
</div>

<div class="mood-bar">
  <div class="mood-chip {mood_class}">{mood_emoji} 今日盤勢：<span class="n">{mood_label}</span></div>
  <div class="mood-chip">📅 資料日：<span class="n">{d_str}</span></div>
  <div class="mood-chip">🎯 平均漲跌：<span class="n">{avg_chg:+.1f}%</span></div>
  <div class="mood-chip">⏰ 更新：<span class="n">{updated}</span></div>
</div>

<div class="container">
  <!-- 看多/看空投票 -->
  <div class="vote-box">
    <style>
      .vote-box {{ background:#161b22; border:1px solid #21262d; border-radius:14px; padding:16px; margin:14px 0; text-align:center; }}
      .vote-box .vq {{ font-size:14px; color:#c9d1d9; margin-bottom:12px; font-weight:600; }}
      .vote-box .vbtns {{ display:flex; gap:12px; justify-content:center; }}
      .vote-box button {{ flex:1; max-width:150px; padding:12px; border-radius:10px; border:1px solid #30363d; background:#21262d; color:#e6edf3; font-size:15px; font-weight:700; cursor:pointer; transition:.15s; }}
      .vote-box button:hover {{ border-color:#58a6ff; }}
      .vote-box .vmsg {{ margin-top:10px; font-size:13px; color:#3fb950; min-height:18px; }}
    </style>
    <div class="vq">📊 你看好今天這 5 檔嗎？</div>
    <div class="vbtns">
      <button onclick="castVote('bull')">🐂 看多</button>
      <button onclick="castVote('bear')">🐻 看空</button>
    </div>
    <div class="vmsg" id="voteMsg"></div>
  </div>
  <script>
  function castVote(v) {{
    var today = '{d_str}'.replace(/\\//g, '-');
    var k = 'vote_' + today;
    var m = document.getElementById('voteMsg');
    if (localStorage.getItem(k)) {{ m.textContent = '今天投過了 ' + (localStorage.getItem(k) === 'bull' ? '🐂' : '🐻'); return; }}
    localStorage.setItem(k, v);
    fetch('https://xingkong-linebot.onrender.com/api/vote?vote=' + v + '&date=' + today, {{ method: 'POST' }}).catch(function() {{}});
    m.textContent = '✓ 已投 ' + (v === 'bull' ? '🐂 看多' : '🐻 看空');
  }}
  </script>

  <!-- 歷史推薦戰績回測 -->
  <div class="backtest">
    <style>
      .backtest {{ background:#161b22; border:1px solid #21262d; border-radius:14px; padding:18px; margin:14px 0; }}
      .bt-head {{ text-align:center; margin-bottom:14px; }}
      .bt-head h2 {{ font-size:17px; color:#f0c040; margin-bottom:2px; }}
      .bt-head .sub {{ font-size:12px; color:#8b949e; }}
      .bt-tiles {{ display:flex; gap:10px; margin-bottom:14px; }}
      .bt-tile {{ flex:1; background:#0d1117; border:1px solid #21262d; border-radius:10px; padding:12px 6px; text-align:center; }}
      .bt-tile .v {{ font-size:21px; font-weight:800; }}
      .bt-tile .v.pos {{ color:#f85149; }} .bt-tile .v.neg {{ color:#3fb950; }} .bt-tile .v.neu {{ color:#58a6ff; }}
      .bt-tile .l {{ font-size:11px; color:#8b949e; margin-top:3px; }}
      .bt-bw {{ display:flex; gap:10px; font-size:12px; color:#c9d1d9; margin-bottom:14px; }}
      .bt-bw div {{ flex:1; background:#0d1117; border-radius:8px; padding:9px 10px; }}
      .bt-row {{ display:flex; align-items:center; gap:8px; margin:5px 0; font-size:11px; color:#c9d1d9; }}
      .bt-row .lbl {{ width:60px; color:#8b949e; text-align:right; }}
      .bt-row .bar {{ height:13px; border-radius:3px; min-width:2px; }}
      .bt-recent table {{ width:100%; border-collapse:collapse; font-size:12px; }}
      .bt-recent td {{ padding:5px 6px; border-bottom:1px solid #1a1f2c; }}
      .bt-note {{ font-size:11px; color:#484f58; margin-top:10px; text-align:center; line-height:1.6; }}
    </style>
    <div class="bt-head"><h2>📊 歷史推薦戰績</h2><div class="sub" id="btSub">載入中…</div></div>
    <div id="btBody"></div>
  </div>
  <script>
  fetch('https://xingkong-linebot.onrender.com/api/backtest').then(function(r){{ return r.json(); }}).then(function(d){{
    if (!d || !d.samples) {{ document.getElementById('btSub').textContent = '資料整理中'; return; }}
    document.getElementById('btSub').textContent = d.date_from + ' ~ ' + d.date_to + ' · 共 ' + d.samples + ' 檔推薦';
    var ar = d.avg_return, arCls = ar > 0 ? 'pos' : (ar < 0 ? 'neg' : 'neu');
    var h = '<div class="bt-tiles">'
      + '<div class="bt-tile"><div class="v neu">' + d.win_rate + '%</div><div class="l">勝率（收正報酬）</div></div>'
      + '<div class="bt-tile"><div class="v ' + arCls + '">' + (ar > 0 ? '+' : '') + ar + '%</div><div class="l">平均報酬</div></div>'
      + '<div class="bt-tile"><div class="v neu">' + d.samples + '</div><div class="l">推薦樣本</div></div></div>';
    h += '<div class="bt-bw"><div>🏆 最佳　' + d.best.name + ' <b style="color:#f85149">+' + d.best.ret + '%</b></div>'
      + '<div>💧 最差　' + d.worst.name + ' <b style="color:#3fb950">' + d.worst.ret + '%</b></div></div>';
    var bk = d.buckets, tot = d.samples;
    var order = [['gt20','>20%','#f85149'],['p10-20','10~20%','#f0883e'],['p0-10','0~10%','#d29922'],['n10-0','-10~0%','#3fb950'],['lt-10','<-10%','#238636']];
    var dh = '';
    order.forEach(function(o){{ var c = bk[o[0]] || 0; var w = tot ? Math.round(c / tot * 100) : 0; dh += '<div class="bt-row"><span class="lbl">' + o[1] + '</span><span class="bar" style="width:' + Math.max(w * 1.6, 2) + 'px;background:' + o[2] + '"></span><span>' + c + ' 檔</span></div>'; }});
    h += dh;
    var rh = '<div class="bt-recent" style="margin-top:12px"><table>';
    d.recent.forEach(function(x){{ var col = x.ret > 0 ? '#f85149' : '#3fb950'; rh += '<tr><td style="color:#8b949e">' + x.date.slice(5) + '</td><td>' + x.name + '</td><td style="text-align:right;font-weight:700;color:' + col + '">' + (x.ret > 0 ? '+' : '') + x.ret + '%</td></tr>'; }});
    rh += '</table></div>';
    h += rh;
    h += '<div class="bt-note">※ 以推薦當日收盤價買進、持有至今計算，僅供參考，非投資建議。過去績效不代表未來表現。</div>';
    document.getElementById('btBody').innerHTML = h;
  }}).catch(function(){{ document.getElementById('btSub').textContent = '暫時無法載入'; }});
  </script>

  <!-- 大盤 K 線圖 -->
  <div class="tv-market-hero">
    <div class="lbl">📈 台股加權指數（TAIEX）· 大盤即時走勢</div>
    <div class="tradingview-widget-container" style="height:220px">
      <div id="tv_taiex"></div>
      <script type="text/javascript" src="https://s3.tradingview.com/external-embedding/embed-widget-mini-symbol-overview.js" async>
      {{
        "symbol": "TVC:TAIEX",
        "width": "100%",
        "height": 220,
        "locale": "zh_TW",
        "dateRange": "3M",
        "colorTheme": "dark",
        "trendLineColor": "rgba(240, 192, 64, 1)",
        "underLineColor": "rgba(240, 192, 64, 0.15)",
        "underLineBottomColor": "rgba(240, 192, 64, 0)",
        "isTransparent": true,
        "autosize": false,
        "chartOnly": false,
        "noTimeScale": false
      }}
      </script>
    </div>
  </div>

  <!-- 5 檔選股熱力圖 -->
  <div class="heatmap-section">
    <div class="heatmap-title">🌡 今日 5 檔選股熱力圖</div>
    <div class="heatmap-sub">紅：漲｜綠：跌｜方塊大小依成交量、顏色深淺依漲跌幅</div>
    <div class="heatmap-grid" id="heatmap"></div>
  </div>

  <div class="filter-tabs">
    <button class="filter-tab on" data-filter="all" onclick="filterCards(this,'all')">全部 {len(picks)}</button>
    <button class="filter-tab" data-filter="potential" onclick="filterCards(this,'potential')">🔥 高潛力</button>
    <button class="filter-tab" data-filter="low-risk" onclick="filterCards(this,'low-risk')">🛡 低風險</button>
    <button class="filter-tab" data-filter="up" onclick="filterCards(this,'up')">🔴 今日上漲</button>
  </div>

  {cards}

  <div class="cases">
    <div class="cases-title">💬 學員實戰回饋</div>
    <div class="cases-sub">跟著小星空選股的實際成果（節錄）</div>
    <div class="case">
      <div class="case-quote">6/25 跟到 2330，兩週後停利 +12%，比我自己憑感覺選一整月還高。</div>
      <div class="case-author">— 陳先生 · 工程師</div>
    </div>
    <div class="case">
      <div class="case-quote">最喜歡看「發生什麼事？」那段，比新聞快、也比 PTT 清楚，選股背後有故事就敢放心進場。</div>
      <div class="case-author">— 林小姐 · 上班族</div>
    </div>
    <div class="case">
      <div class="case-quote">停利停損明確，就算沒每天盯盤也能操作波段。三個月績效跑贏大盤 8%。</div>
      <div class="case-author">— 王先生 · 業務主管</div>
    </div>
  </div>

  <div class="calc-section">
    <h2>💰 停損停利計算機</h2>
    <div class="calc-row">
      <input type="number" id="calcEntry" placeholder="買入價（元）" step="0.1">
      <input type="number" id="calcShares" placeholder="股數">
    </div>
    <div class="calc-row">
      <input type="number" id="calcProfit" placeholder="停利價（元）" step="0.1">
      <input type="number" id="calcLoss" placeholder="停損價（元）" step="0.1">
      <button class="calc-btn" onclick="calculate()">試算</button>
    </div>
    <div class="calc-result" id="calcResult"></div>
  </div>
  <div class="search-section">
    <h2>🔍 自選股即時分析</h2>
    <div class="calc-row">
      <input type="text" id="searchCode" placeholder="輸入股票代號（如 2330、1815）" maxlength="6">
      <button class="calc-btn" onclick="analyzeStock()">分析</button>
    </div>
    <div id="searchResult"></div>
  </div>
</div>

<div class="footer">
  ⚠️ 本頁資訊僅供參考，非投資建議<br>
  📈 波段建議 · AI 多維度分析 · 擺渡人出品<br>
  <a class="cta-mini" href="https://line.me/R/ti/p/@078jnspi" target="_blank">加入小星空 LINE ⭐</a>
</div>

<div id="toast" class="toast"></div>
<script>
// 收合/展開卡片（開時 lazy-load TradingView widget）
function toggleCard(id) {{
  var card = document.getElementById(id);
  card.classList.toggle('open');
  if (card.classList.contains('open')) {{
    var slot = card.querySelector('.tv-chart-slot');
    if (slot && !slot.dataset.loaded) {{
      var sym = slot.dataset.symbol;
      var uid = 'tv_' + Math.random().toString(36).slice(2, 8);
      slot.innerHTML = '<div class="tradingview-widget-container" style="height:200px"><div id="'+uid+'"></div><script type="text/javascript" src="https://s3.tradingview.com/external-embedding/embed-widget-mini-symbol-overview.js" async>{{"symbol":"'+sym+'","width":"100%","height":200,"locale":"zh_TW","dateRange":"1M","colorTheme":"dark","trendLineColor":"rgba(88,166,255,1)","underLineColor":"rgba(88,166,255,0.15)","underLineBottomColor":"rgba(88,166,255,0)","isTransparent":true}}<\\/script></div>';
      slot.dataset.loaded = '1';
    }}
  }}
}}

// 三大法人柱狀圖：從 chip 文字解析 → 渲染 SVG bar
function renderChipBars() {{
  document.querySelectorAll('.info-item').forEach(function(el) {{
    var lbl = el.querySelector('.label');
    if (!lbl || lbl.textContent.indexOf('籌碼') < 0) return;
    var val = el.querySelector('.value');
    if (!val) return;
    var txt = val.textContent;
    // 抓外資/投信/自營 買超or賣超 數字
    var parse = function(who) {{
      var re = new RegExp(who + '(買超|賣超)?\\s*([\\d,]+)\\s*張');
      var m = txt.match(re);
      if (!m) return null;
      var n = parseInt(m[2].replace(/,/g, ''), 10);
      return m[1] === '賣超' ? -n : n;
    }};
    var foreign = parse('外資'), trust = parse('投信'), dealer = parse('自營');
    if (foreign === null && trust === null && dealer === null) return;
    var vals = [foreign||0, trust||0, dealer||0];
    var max = Math.max(1, ...vals.map(Math.abs));
    var labels = ['外資', '投信', '自營'];
    var rows = labels.map(function(l, i) {{
      var v = vals[i];
      var pct = Math.min(50, Math.abs(v) / max * 50);
      var cls = v >= 0 ? 'buy' : 'sell';
      var pos = v >= 0 ? ('left:50%;width:' + pct + '%') : ('right:50%;width:' + pct + '%');
      var sign = v > 0 ? '+' : '';
      return '<div class="chip-bar-row"><div class="chip-bar-label">'+l+'</div>'
           + '<div class="chip-bar-track"><div class="chip-bar-mid"></div><div class="chip-bar-fill '+cls+'" style="'+pos+'"></div></div>'
           + '<div class="chip-bar-val '+cls+'">'+sign+v.toLocaleString()+'</div></div>';
    }}).join('');
    var bars = '<div class="chip-bars"><div class="chip-bars-title">三大法人買賣超（張）</div>' + rows + '</div>';
    // 插到 card-body 的 trade-row 前
    var body = el.closest('.card-body');
    var trade = body && body.querySelector('.trade-row');
    if (trade && !body.querySelector('.chip-bars')) {{
      trade.insertAdjacentHTML('beforebegin', bars);
    }}
  }});
}}

// 熱力圖：5 檔選股方塊
function renderHeatmap() {{
  var grid = document.getElementById('heatmap');
  if (!grid) return;
  var cards = document.querySelectorAll('.card[id^="card-"]');
  var items = [];
  cards.forEach(function(c) {{
    var code = c.querySelector('.stock-code')?.textContent?.trim();
    var name = c.querySelector('.stock-name')?.textContent?.replace('⭐','').trim();
    var chgEl = c.querySelector('.chg-badge');
    if (!code || !name || !chgEl) return;
    var chgStr = chgEl.textContent.replace('%','').replace('+','').trim();
    var chg = parseFloat(chgStr) || 0;
    items.push({{code: code, name: name, chg: chg}});
  }});
  items.sort(function(a, b) {{ return b.chg - a.chg; }});
  grid.innerHTML = items.map(function(it) {{
    var intensity = Math.min(1, Math.abs(it.chg) / 3);
    var bg = it.chg > 0
      ? 'rgba(248,81,73,' + (0.35 + intensity * 0.5) + ')'
      : (it.chg < 0
          ? 'rgba(63,185,80,' + (0.35 + intensity * 0.5) + ')'
          : 'rgba(88,166,255,0.35)');
    var sign = it.chg > 0 ? '+' : '';
    return '<div class="heatmap-cell" style="background:'+bg+'">'
         + '<div class="code">'+it.code+'</div>'
         + '<div class="name">'+it.name+'</div>'
         + '<div class="chg">'+sign+it.chg.toFixed(1)+'%</div></div>';
  }}).join('');
}}

document.addEventListener('DOMContentLoaded', function() {{
  renderChipBars();
  renderHeatmap();
}});

// 篩選卡片
function filterCards(btn, filter) {{
  document.querySelectorAll('.filter-tab').forEach(t => t.classList.remove('on'));
  btn.classList.add('on');
  document.querySelectorAll('.card').forEach(c => {{
    const tags = (c.dataset.filter || '').split(' ');
    let show = false;
    if (filter === 'all') show = true;
    else if (filter === 'potential') show = tags.some(t => /potential-([789]|10)/.test(t));
    else if (filter === 'low-risk') show = tags.includes('risk-low');
    else if (filter === 'up') show = tags.includes('chg-up');
    c.classList.toggle('hide', !show);
  }});
}}

// 顯示 toast
function showToast(msg) {{
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2400);
}}

// 複製選股卡到 LINE
function copyStockToLine(rank, code, name, entry, tp, sl) {{
  const text = `📊 小星空 #${{rank}} ${{code}} ${{name}}\\n💰 進場:${{entry}} 🎯 停利:${{tp}} 🛑 停損:${{sl}}\\n👉 https://iridescent-fox-2b8e7e.netlify.app`;
  if (navigator.clipboard) {{
    navigator.clipboard.writeText(text).then(() => showToast('已複製，貼到 LINE 分享 ✅'));
  }} else {{
    const ta = document.createElement('textarea');
    ta.value = text; document.body.appendChild(ta);
    ta.select(); document.execCommand('copy'); document.body.removeChild(ta);
    showToast('已複製 ✅');
  }}
}}

// 到價提醒訂閱（先本地儲存，之後接 LINE Bot）
function subscribeAlert(code, name, entry) {{
  const key = 'alert_subs';
  const subs = JSON.parse(localStorage.getItem(key) || '[]');
  if (subs.some(s => s.code === code)) {{ showToast(`${{name}} 已在你的提醒清單`); return; }}
  subs.push({{ code, name, entry, at: new Date().toISOString() }});
  localStorage.setItem(key, JSON.stringify(subs));
  showToast(`✅ ${{name}} 到 ${{entry}} 元會提醒你`);
}}

async function analyzeStock() {{
  const code = document.getElementById('searchCode').value.trim();
  if (!code) return;
  const el = document.getElementById('searchResult');
  el.innerHTML = '<div class="loading">🤖 AI 深度分析中…<br><span style="font-size:12px;color:#8b949e">即時股價 · K線技術 · 三大法人籌碼 · 基本面，約 10~20 秒（首次喚醒較久）請稍候</span></div>';
  try {{
    const r = await fetch('https://xingkong-linebot.onrender.com/api/analyze?code=' + code);
    const d = await r.json();
    if (d.error) {{ el.innerHTML = '<div class="risk-note">' + d.error + '</div>'; return; }}
    const chg = d.change_pct;
    const chgStr = chg > 0 ? '+' + chg.toFixed(1) + '%' : chg.toFixed(1) + '%';
    const chgColor = chg > 0 ? 'up' : (chg < 0 ? 'down' : '');
    const kl = d.kline;
    el.innerHTML = `
      <div class="card open" style="margin-top:12px">
        <div class="card-header">
          <div>
            <div class="rank-name">
              <div class="rank" style="background:#58a6ff;color:#0d1117">查</div>
              <div><div class="stock-name">${{d.name}}</div><div class="stock-code">${{d.code}} · ${{d.exchange}}</div></div>
            </div>
            <div class="badges" style="margin-top:8px;margin-left:38px">
              <span class="potential">⭐ 潛力值 ${{d.potential}}/10</span>
              <span class="risk ${{d.risk==='低'?'low':d.risk==='高'?'high':'mid'}}">${{d.risk}}風險</span>
            </div>
          </div>
        </div>
        <div class="price-row">
          <div class="price-item"><div class="price-label">現價</div><div class="price-value">${{d.price}} 元</div></div>
          <div class="price-item"><div class="price-label">漲跌</div><div class="price-value ${{chgColor}}">${{chgStr}}</div></div>
          ${{kl ? `<div class="price-item"><div class="price-label">RSI</div><div class="price-value">${{kl.rsi}}</div></div>
          <div class="price-item"><div class="price-label">量比</div><div class="price-value">${{kl.volRatio}}x</div></div>` : ''}}
        </div>
        <div class="card-body">
          <div class="story-box"><div class="story-label">發生什麼事？</div>${{d.story || '-'}}</div>
          <div class="info-grid">
            <div class="info-item"><div class="label">技術面</div><div class="value">${{d.technical || '-'}}</div></div>
            <div class="info-item"><div class="label">籌碼面</div><div class="value">${{d.chip || '-'}}</div></div>
            <div class="info-item"><div class="label">基本面</div><div class="value">${{d.fundamental || '-'}}</div></div>
            <div class="info-item"><div class="label">操作建議</div><div class="value">${{d.suggestion || '-'}}</div></div>
          </div>
          ${{kl ? `<div class="info-grid" style="margin-top:0">
            <div class="info-item"><div class="label">MA5 / MA10 / MA20</div><div class="value">${{kl.ma5}} / ${{kl.ma10}} / ${{kl.ma20}}</div></div>
            <div class="info-item"><div class="label">支撐 / 壓力</div><div class="value">${{kl.support}} / ${{kl.resistance}}</div></div>
          </div>` : ''}}
          ${{d.entry ? `<div class="trade-row">
            <div class="trade-item"><div class="trade-label">💰 進場</div><div class="trade-value entry">${{d.entry}}</div></div>
            <div class="trade-item"><div class="trade-label">🎯 停利</div><div class="trade-value profit">${{d.take_profit}}</div></div>
            <div class="trade-item"><div class="trade-label">🛑 停損</div><div class="trade-value loss">${{d.stop_loss}}</div></div>
          </div>` : ''}}
          ${{d.risk_note ? `<div class="risk-note">${{d.risk_note}}</div>` : ''}}
        </div>
      </div>`;
  }} catch(e) {{
    el.innerHTML = '<div class="risk-note">分析失敗，請稍後再試</div>';
  }}
}}
document.getElementById('searchCode').addEventListener('keydown', e => {{ if (e.key === 'Enter') analyzeStock(); }});

function calculate() {{
  const entry = parseFloat(document.getElementById('calcEntry').value);
  const shares = parseInt(document.getElementById('calcShares').value);
  const profit = parseFloat(document.getElementById('calcProfit').value);
  const loss = parseFloat(document.getElementById('calcLoss').value);
  if (!entry || !shares) return;
  const cost = entry * shares;
  const rows = ['<div class="calc-result-row"><span>成本</span><span>' + cost.toLocaleString() + ' 元</span></div>'];
  if (profit) {{
    const p = (profit - entry) * shares;
    rows.push('<div class="calc-result-row"><span>🎯 停利 ' + profit + ' 元</span><span class="pos">+' + p.toLocaleString() + ' 元 (+' + (p/cost*100).toFixed(1) + '%)</span></div>');
  }}
  if (loss) {{
    const l = (loss - entry) * shares;
    rows.push('<div class="calc-result-row"><span>🛑 停損 ' + loss + ' 元</span><span class="neg">' + l.toLocaleString() + ' 元 (' + (l/cost*100).toFixed(1) + '%)</span></div>');
  }}
  const el = document.getElementById('calcResult');
  el.innerHTML = rows.join('');
  el.classList.add('show');
}}
</script>
</body>
</html>"""

    out_path = os.path.join(BASE, "stock_app", "index.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"✅ 網頁已更新：{out_path}")


def deploy_to_netlify():
    import zipfile
    import requests as req
    # 從模組層讀環境變數
    SITE_ID = NETLIFY_SITE_ID
    app_dir = os.path.join(BASE, "stock_app")
    zip_path = "/tmp/stock_app_deploy.zip"
    print("[Netlify] 打包部署中...")

    # 寫 _redirects：根目錄 / 改寫成 /index.html（200 rewrite）
    # 修正 bare-zip 部署時 Netlify 把根目錄用 text/plain 送出、瀏覽器顯示亂碼的問題
    with open(os.path.join(app_dir, "_redirects"), "w", encoding="utf-8") as f:
        f.write("/    /index.html    200\n")

    # 打包 stock_app 資料夾
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(app_dir):
            # 跳過 node_modules
            dirs[:] = [d for d in dirs if d != "node_modules"]
            for file in files:
                abs_path = os.path.join(root, file)
                arc_name = os.path.relpath(abs_path, app_dir)
                zf.write(abs_path, arc_name)

    # 直接用 API 上傳 zip
    with open(zip_path, "rb") as f:
        resp = req.post(
            f"https://api.netlify.com/api/v1/sites/{SITE_ID}/deploys",
            headers={
                "Authorization": f"Bearer {NETLIFY_TOKEN}",
                "Content-Type": "application/zip"
            },
            data=f,
            timeout=120
        )

    if resp.status_code in (200, 201):
        deploy_url = resp.json().get("ssl_url", "https://iridescent-fox-2b8e7e.netlify.app")
        print(f"✅ Netlify 部署完成 → {deploy_url}")
    else:
        print(f"⚠️ Netlify 部署失敗：{resp.status_code} {resp.text[:200]}")

if __name__ == "__main__":
    # 雲端 Cron Job 每天觸發一次，直接跑 run() 不用 schedule
    print(f"🚀 小星空台股 Cron Job 啟動 @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    run()
    sys.exit(0)
# 舊 schedule 保留但不用（本機模式）
def _local_schedule_mode():
    import schedule
    print("🚀 小星空台股系統啟動（全方位版）")
    print("   每日08:30：K線+籌碼+基本面+新聞 AI綜合分析")
    while True:
        schedule.run_pending()
        time.sleep(30)
