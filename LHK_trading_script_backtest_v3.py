# =============================================================================
# ⚙️ UAT 時光機模式 (The Ultimate Edition - Full Integration)
# 核心功能：模擬過去交易日 / 波段與短線雙引擎 / 大盤 FTD 偵測 / 部位計算機
# =============================================================================

import pandas as pd, numpy as np, yfinance as yf, matplotlib
matplotlib.use('Agg') # 伺服器端繪圖必須加上這行
import matplotlib.pyplot as plt, matplotlib.dates as mdates, concurrent.futures
import warnings, os, datetime, json, logging, time, requests
from io import StringIO
from fake_useragent import UserAgent

# 關閉不必要嘅警告，保持 Terminal 乾淨
logging.getLogger('yfinance').setLevel(logging.CRITICAL)
warnings.filterwarnings('ignore')
plt.style.use('dark_background')
plt.ioff()

# =============================================================================
# 系統環境設定 (路徑與 Webhook - UAT 專用)
# =============================================================================
# 寫入 UAT 子資料夾，避免覆蓋正式版
OUTPUT_DIR = "docs/UAT"
CHARTS_DIR = os.path.join(OUTPUT_DIR, "charts")
os.makedirs(CHARTS_DIR, exist_ok=True)

# 讀取 GitHub Secrets (UAT 專用的 Webhook)
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_BACKTEST_WEBHOOK_URL", "")
DISCORD_SUMMARY_WEBHOOK = os.environ.get("DISCORD_BACKTEST_SUMMARY_WEBHOOK", "")
HISTORY_FILE = os.path.join(OUTPUT_DIR, "uat_trade_history.json")

# =============================================================================
# 核心策略與時光機參數 
# =============================================================================
LOOKBACK_YEARS = 10
PQR_SWING_MIN = 75
FTD_VALID_DAYS = 20
MAX_ACCOUNT_RISK_PCT = 0.01 # 每單最多虧損總資金的 1%

# 👇 時光機設定：從 GitHub Actions 讀取要回溯幾多日 (預設回溯 10 日)
# 假設你的腳本內新增一個模式
START_DAYS = 500
END_DAYS = 0

raw_days = os.environ.get("UAT_DAYS_AGO", "10")
SIMULATE_DAYS_AGO = int(raw_days)

# =============================================================================
# 功能函數區
# =============================================================================
STOCK_INFO_CACHE = {} # 👈 新增：智能緩存，避免重複呼叫 yfinance 拖慢速度

def get_stock_info(tk):
    if tk in STOCK_INFO_CACHE: return STOCK_INFO_CACHE[tk]
    try:
        info = yf.Ticker(tk).info
        sector = info.get('sector', 'N/A')
        mcap = info.get('marketCap', 0)
        STOCK_INFO_CACHE[tk] = {'sector': sector, 'mcap': mcap}
        return STOCK_INFO_CACHE[tk]
    except:
        STOCK_INFO_CACHE[tk] = {'sector': 'N/A', 'mcap': 0}
        return STOCK_INFO_CACHE[tk]

def send_discord_alert(ticker, strategy_name, price, sl, tp, is_bullish, sources, tp1_price=None):
    if not DISCORD_WEBHOOK_URL: return
    unit = "¥" if ticker.endswith(".T") else "$"
    
    if sources:
        clean_sources = [f"#{s.replace('&', '').replace(' ', '_')}" for s in sources]
        source_str = " ".join(clean_sources)
    else:
        source_str = "#動態掃描"
        
    color = 65280 if is_bullish else 16711680 
    type_str = "**波段建倉 (Swing)**" if strategy_name in ["🏆 VCP 突破", "💥 BB 擠壓"] else "**短線游擊 (Short Term)**"
    trail_str = "跌穿 5日新低" if "短線" in type_str else "跌穿 20日新低"
    tp1_val = tp1_price if tp1_price else tp
    
    # 👇 改成平倉 75%
    action_text = f"{type_str}\n1️⃣ **TP1:** `{unit}{tp1_val}` (平倉 75% 並保本)\n2️⃣ **TP2 (Trail):** {trail_str}清倉\n3️⃣ **Max TP:** `{unit}{tp}` (全數強制平倉)"
    
    embed_data = {
        "title": f"🚨 系統異動觸發: {ticker}",
        "description": f"**{strategy_name}** 條件已達成！\n🔍 來源: **{source_str}**",
        "color": color,
        "fields": [
            {"name": "💵 當前現價", "value": f"{unit}{price}", "inline": True},
            {"name": "🛑 初始止損", "value": f"{unit}{sl}", "inline": True},
            {"name": "⚙️ 離場策略", "value": action_text, "inline": False}
        ],
        # 👇 加入 UAT 時光機 Footnote
        "footer": {"text": f"V1 Quant Master (UAT 測試) | 時光機回溯 {SIMULATE_DAYS_AGO} 日"}
    }
    try: 
        requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed_data]})
        time.sleep(0.5) 
    except Exception as e: print(f"⚠️ Discord 連線錯誤: {e}")

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f: return json.load(f)
        except: return []
    return []

trade_history = load_history()

# =============================================================================
# MODULE 1 & 2 — 雙市場數據引擎與時光機截斷
# =============================================================================
print(f"⏳ [1-3/7] 正在抓取數據與啟動時光機 (回溯 {SIMULATE_DAYS_AGO} 日)...")

def build_dynamic_watchlist():
    ticker_sources = {}
    # 建立 UA 生成器
    ua = UserAgent()

    def add_to_map(tickers, source_label):
        for t in tickers:
            if not isinstance(t, str) or len(t) < 1: continue
            clean_t = t.strip()
            if not clean_t.endswith('.T'): clean_t = clean_t.replace('.', '-')
            if clean_t not in ticker_sources: ticker_sources[clean_t] = []
            if source_label not in ticker_sources[clean_t]: ticker_sources[clean_t].append(source_label)
    
    # ---------------------------------------------------------
    # 1. 🇺🇸 美股黃金板塊擴充 (超過 1500 隻)
    # ---------------------------------------------------------
    try:
        wiki_us_indexes = [
        ("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", "S&P500_大盤"),
        ("https://en.wikipedia.org/wiki/List_of_S%26P_400_companies", "S&P400_中型"),
        ("https://en.wikipedia.org/wiki/List_of_S%26P_600_companies", "S&P600_小型"),
        ("https://en.wikipedia.org/wiki/Nasdaq-100", "NDX100_科技")]

        for url, label in wiki_us_indexes:
            res = requests.get(url, headers={'User-Agent': ua.random}, timeout=10)
            tables = pd.read_html(StringIO(res.text))
            
            # 自動尋找包含 Symbol 或 Ticker 的表格
            for df in tables:
                target_col = next((col for col in df.columns if 'symbol' in str(col).lower() or 'ticker' in str(col).lower()), None)
                if target_col:
                    add_to_map(df[target_col].dropna().astype(str).tolist(), label)
                    print(f"  ✅ 成功載入 {label}: {len(df)} 隻")
                    break
       
        #csv_url = "https://raw.githubusercontent.com/datasets/s-p-500-companies/master/data/constituents.csv"
        #df_sp = pd.read_csv(csv_url, timeout=10)
        #add_to_map(df_sp['Symbol'].tolist(), "S&P500")
    except:
        print(f"  ⚠️ S&P 500 CSV 載入失敗，啟動超級後備名單: {e}")
        # 超強後備名單 (超過 400 隻美股核心成分股)
        sp500_fallback = [
            "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "BRK-B", "TSLA", "UNH",
            "JPM", "XOM", "V", "MA", "AVGO", "PG", "HD", "JNJ", "LLY", "COST",
            "CVX", "MRK", "ABBV", "PEP", "KO", "TMO", "PFE", "BAC", "ORCL", "MCD",
            "CSCO", "CRM", "ABT", "ACN", "LIN", "NFLX", "AMD", "DIS", "WMT", "TXN",
            "DHR", "PM", "NKE", "NEE", "VZ", "RTX", "UPS", "HON", "QCOM", "AMGN",
            "LOW", "SPGI", "IBM", "INTU", "CAT", "UNP", "COP", "SBUX", "DE", "GS",
            "PLD", "MS", "BLK", "ELV", "GILD", "ISRG", "TJX", "LMT", "SYK", "ADP",
            "MDT", "VRTX", "MMC", "AMT", "GE", "CI", "CB", "NOW", "ADI", "LRCX",
            "MDLZ", "T", "ETN", "REGN", "ZTS", "BSX", "MU", "PANW", "PGR", "FI",
            "SNPS", "C", "KLAC", "VLO", "CDNS", "WM", "EOG", "SHW", "MAR", "MCK",
            "CVS", "MO", "PH", "GD", "ORLY", "APH", "SLB", "ITW", "USB", "FDX",
            "ECL", "ROP", "PXD", "TGT", "BDX", "NXPI", "CMG", "MNST", "MPC", "MCO",
            "CTAS", "AIG", "NSC", "PSX", "ADSK", "AON", "EMR", "MET", "D", "KMB",
            "SRE", "MSI", "MCHP", "AJG", "HCA", "AZO", "F", "WELL", "EW", "DRE",
            "O", "PCAR", "GPN", "ADP", "FIS", "HUM", "PAYX", "TEL", "DOW", "BKR",
            "ADM", "KDP", "STZ", "CNC", "JCI", "SYY", "CTSH", "CARR", "DXCM", "EIX",
            "IDXX", "VRSK", "DLR", "IQV", "A", "GWW", "COR", "ED", "NEM", "CHTR",
            "YUM", "OXY", "MSCI", "KHC", "WFC", "TFC", "PNC", "COF", "DFS", "SYF",
            "KEY", "RF", "HBAN", "FITB", "CFG", "STT", "NTRS", "MTB", "BK", "AMP",
            "IVZ", "BEN", "TROW", "GL", "L", "AIZ", "RE", "TRV", "CBRE", "HST",
            "SPG", "AVB", "EQR", "VTR", "PEAK", "BXP", "MAA", "CPT", "UDR", "ESS",
            "ARE", "VICI", "PSA", "EXR", "SBAC", "CCI", "AWK", "NI", "PNW", "ATO",
            "LNT", "ES", "WEC", "CMS", "XEL", "ETR", "FE", "AEE", "AEP", "PEG",
            "DTE", "PPL", "DUK", "SO", "CNP", "VST", "PARA", "WBD", "NWSA", "NWS",
            "FOXA", "FOX", "LYV", "MTCH", "EA", "TTWO", "OMC", "IPG", "TMUS", "LUMN",
            "FYBR", "AMX", "ROST", "HLT", "DHI", "LEN", "PHM", "NVR", "GRMN", "GM",
            "BBY", "EBAY", "ETSY", "RVTY", "POOL", "HAS", "MAT", "EL", "CL", "K",
            "GIS", "CPB", "HRL", "SJM", "TAP", "KR", "WBA", "DLTR", "DG", "HAL",
            "HES", "DVN", "FANG", "MRO", "APA", "CTRA", "OKE", "TRGP", "KMI", "WMB",
            "SCHW", "RJF", "LPLA", "AXP", "PYPL", "FISV", "JKHY", "WTW", "PRU", "AFL",
            "ALL", "HIG", "CINF", "NDAQ", "CME", "ICE", "BMY", "STE", "WAT", "MTD",
            "CRL", "RMD", "BA", "NOC", "TDG", "HWM", "TXT", "MMM", "AME", "ROK",
            "DOV", "XYL", "FAST", "RSG", "CSX", "INVH", "AMH", "EQIX", "INTC", "AMAT",
            "ANSS", "SAP", "FTNT", "STX", "WDC", "HPQ", "DELL", "NTAP"
        ]
        add_to_map(sp500_fallback, "S&P500")
        print(f"  ✅ 成功載入 S&P 500 後備名單 (共 {len(sp500_fallback)} 隻)")
    # ---------------------------------------------------------
    # 2. 獲取 Finviz 異動股 (Unusual Volume & Top Gainers)
    # ---------------------------------------------------------
    # 呢度係捕捉「當日最熱門」標的關鍵
    finviz_urls = [
        ("https://finviz.com/screener.ashx?v=111&s=ta_topgainers", "Finviz升幅"),
        ("https://finviz.com/screener.ashx?v=111&s=ta_unusualvolume", "Finviz異動")
    ]
    for url, label in finviz_urls:
        try:
            # 每次需要 headers 時，呼叫 ua.random
            headers = {'User-Agent': ua.random}
            res = requests.get(url, headers=headers, timeout=10)
            tables = pd.read_html(res.text)
            # Finviz 的股票代號通常在最後幾個表格中，且長度為 1-5 字符
            for df in tables[-3:]: 
                if 1 in df.columns:
                    found = [str(t) for t in df[1].tolist() if str(t).isupper() and 1 <= len(str(t)) <= 5]
                    if found:
                        add_to_map(found, label)
                        print(f"  🔥 捕捉到 {label}: {len(found)} 隻")
                        break
        except:
            print(f"  ⚠️ {label} 抓取略過")

    # ---------------------------------------------------------
    # 3. 獲取日股動態名單 (Nikkei 225 + 當日熱門)
    # ---------------------------------------------------------
    wiki_jp_indexes = [
        ("https://en.wikipedia.org/wiki/Nikkei_225", "NK225"),
        ("https://en.wikipedia.org/wiki/TOPIX_100", "TOPIX100"),
        ("https://ja.wikipedia.org/wiki/TOPIX_Mid400", "TOPIX_Mid400_中型"),
        ("https://ja.wikipedia.org/wiki/TOPIX_Small500", "TOPIX_Small500_小型")
    ]

    try:
        for url, label in wiki_jp_indexes:
            try:
                res = requests.get(url, headers={'User-Agent': ua.random}, timeout=10)
                tables = pd.read_html(StringIO(res.text))
                    
                import re
                target_col = None
                # 自動尋找包含最多股票代號嘅表格 (日股通常係 4 位數字)
                target_table = max(tables, key=len)
                    
                for col in target_table.columns:
                    col_name = str(col).lower()
                    if 'code' in col_name or 'ticker' in col_name or 'symbol' in col_name or 'コード' in col_name:
                        target_col = col; break
                    
                if target_col is None:
                    for col in target_table.columns:
                        sample_vals = target_table[col].dropna().astype(str).tolist()[:5]
                        if sample_vals and all(re.match(r'^\d{4}$', str(x)) for x in sample_vals):
                            target_col = col; break

                if target_col is not None:
                    found_nk = [f"{str(x)}.T" for x in target_table[target_col] if re.match(r'^\d{4}$', str(x))]
                    add_to_map(list(dict.fromkeys(found_nk)), label)
                    print(f"  ✅ 成功從 Wikipedia 載入 {label} (共 {len(found_nk)} 隻)")
            except Exception as e:
                print(f"  ⚠️ {label} 載入失敗: {e}")
    except Exception as e:
            print(f"  ⚠️ 日股名單載入失敗: {e}")
            # 如果 fail, 手動加入2026/04/05 list
            nk225_tickers = [
            "1332.T", "1605.T", "1721.T", "1801.T", "1802.T", "1803.T", "1812.T", "1925.T", "1928.T", "1963.T",
            "2002.T", "2267.T", "2282.T", "2413.T", "2432.T", "2501.T", "2502.T", "2503.T", "2531.T", "2768.T",
            "2801.T", "2802.T", "2871.T", "2914.T", "3086.T", "3099.T", "3101.T", "3103.T", "3289.T", "3382.T",
            "3401.T", "3402.T", "3405.T", "3407.T", "3436.T", "3659.T", "3861.T", "3863.T", "4004.T", "4005.T",
            "4021.T", "4042.T", "4043.T", "4061.T", "4063.T", "4151.T", "4183.T", "4188.T", "4208.T", "4324.T",
            "4452.T", "4502.T", "4503.T", "4506.T", "4507.T", "4519.T", "4523.T", "4543.T", "4568.T", "4578.T",
            "4661.T", "4689.T", "4704.T", "4751.T", "4755.T", "4901.T", "4911.T", "5019.T", "5020.T", "5101.T",
            "5108.T", "5201.T", "5202.T", "5214.T", "5232.T", "5233.T", "5301.T", "5332.T", "5333.T", "5401.T",
            "5406.T", "5411.T", "5541.T", "5631.T", "5703.T", "5706.T", "5707.T", "5711.T", "5713.T", "5801.T",
            "5802.T", "5803.T", "5901.T", "6098.T", "6103.T", "6113.T", "6178.T", "6301.T", "6302.T", "6305.T",
            "6326.T", "6361.T", "6367.T", "6471.T", "6472.T", "6473.T", "6501.T", "6503.T", "6504.T", "6506.T",
            "6645.T", "6674.T", "6701.T", "6702.T", "6703.T", "6723.T", "6724.T", "6752.T", "6753.T", "6758.T",
            "6762.T", "6770.T", "6841.T", "6857.T", "6902.T", "6920.T", "6952.T", "6954.T", "6971.T", "6976.T",
            "6981.T", "6988.T", "7011.T", "7012.T", "7013.T", "7186.T", "7201.T", "7202.T", "7203.T", "7205.T",
            "7211.T", "7261.T", "7267.T", "7269.T", "7270.T", "7272.T", "7731.T", "7733.T", "7735.T", "7741.T",
            "7751.T", "7752.T", "7832.T", "7911.T", "7912.T", "7951.T", "8001.T", "8002.T", "8015.T", "8031.T",
            "8035.T", "8053.T", "8058.T", "8233.T", "8252.T", "8253.T", "8267.T", "8304.T", "8306.T", "8308.T",
            "8309.T", "8316.T", "8331.T", "8354.T", "8411.T", "8601.T", "8604.T", "8628.T", "8630.T", "8697.T",
            "8725.T", "8750.T", "8766.T", "8795.T", "8801.T", "8802.T", "8804.T", "8830.T", "9001.T", "9005.T",
            "9007.T", "9008.T", "9009.T", "9020.T", "9021.T", "9022.T", "9041.T", "9042.T", "9062.T", "9064.T",
            "9101.T", "9104.T", "9107.T", "9201.T", "9202.T", "9301.T", "9412.T", "9432.T", "9433.T", "9434.T",
            "9501.T", "9502.T", "9503.T", "9531.T", "9532.T", "9602.T", "9613.T", "9681.T", "9735.T", "9766.T",
            "9843.T", "9983.T", "9984.T"
            ]
            # 執行合併
            add_to_map(nk225_tickers, "NK225")

    # B. 捕捉 JP Trending (保持不變)
    try:
        jp_trending_url = "https://query1.finance.yahoo.com/v1/finance/trending/JP?count=20"
        # 每次需要 headers 時，呼叫 ua.random
        headers = {'User-Agent': ua.random}
        res_jp = requests.get(jp_trending_url, headers=headers, timeout=5)
        # 加入 len 檢查，防止 list index out of range
        if res_jp.status_code == 200 and len(res_jp.json().get('finance', {}).get('result', [])) > 0:
            jp_trending = [q['symbol'] for q in res_jp.json()['finance']['result'][0]['quotes']]
            add_to_map(jp_trending, "JP熱門")
            print(f"  🔥 捕捉到日股當日焦點: {len(jp_trending)} 隻")
    except Exception as e:
        print(f"  ⚠️ JP Trending 略過: API 未返回數據")

    add_to_map(['SPY', '^VIX', '^N225'], "基準指數")
    return ticker_sources

TICKER_MAP = build_dynamic_watchlist()
ALL_TICKERS = list(TICKER_MAP.keys())

# --- ⚡ 提速核心：UAT 本地快取邏輯 ---
CACHE_FILE = os.path.join(OUTPUT_DIR, "market_data_cache.pkl")
today_date_str = datetime.datetime.now().strftime('%Y-%m-%d')
load_from_cache = False

if os.path.exists(CACHE_FILE):
    file_time = datetime.datetime.fromtimestamp(os.path.getmtime(CACHE_FILE)).strftime('%Y-%m-%d')
    if file_time == today_date_str:
        load_from_cache = True

if load_from_cache:
    print(f"⚡ [UAT] 發現今日 Cache ({today_date_str})，正在極速讀取本地數據...")
    data_raw = pd.read_pickle(CACHE_FILE)
else:
    print(f"🌐 [UAT] Cache 已過期或不存在，正在從 Yahoo Finance 分批抓取雙市場數據 (防止漏單 Bug)...")
    
    # 👇 終極修復：將名單拆分，避免 Yahoo API 混淆與漏單
    us_list = [t for t in ALL_TICKERS if not t.endswith('.T')]
    jp_list = [t for t in ALL_TICKERS if t.endswith('.T')]
    
    print(f"   👉 正在下載美股數據 ({len(us_list)} 隻)...")
    data_us = yf.download(us_list, period=f"{LOOKBACK_YEARS}y", progress=False, threads=True, timeout=30, group_by='column')
    
    print(f"   👉 正在下載日股數據 ({len(jp_list)} 隻)...")
    data_jp = yf.download(jp_list, period=f"{LOOKBACK_YEARS}y", progress=False, threads=True, timeout=30, group_by='column')
    
    # 安全合併：保留 MultiIndex 並對齊所有日期
    data_raw = pd.concat([data_us, data_jp], axis=1)
    
    if not data_raw.empty:
        data_raw.to_pickle(CACHE_FILE)
        print(f"💾 [UAT] 雙市場數據已合併並寫入快取。")

# =========================================================================
# 🛠️ 終極修復：解決美日雙時區導致的「隔日跳空/零數據」Bug
# =========================================================================
if data_raw is not None and not data_raw.empty:
    # 強制移除 yfinance 帶來的 UTC 時區，統一日線時間為純粹的 YYYY-MM-DD
    data_raw.index = pd.to_datetime(data_raw.index).tz_localize(None).normalize()
    
    # 將同一日的 US 與 JP 數據完美合併成單一行 (無視 NaN 提取真實數據)
    data_raw = data_raw.groupby(data_raw.index).max()

# 數據解構與清洗
if isinstance(data_raw.columns, pd.MultiIndex):
    closes = data_raw['Close'].ffill()
    highs = data_raw['High'].ffill()
    lows = data_raw['Low'].ffill()
    vols = data_raw['Volume'].ffill()
    opens = data_raw['Open'].ffill()
else:
    # 針對單一股票或異常結構的處理
    closes = data_raw[['Close']].ffill()
    highs = data_raw[['High']].ffill()
    lows = data_raw[['Low']].ffill()
    vols = data_raw[['Volume']].ffill()
    opens = data_raw[['Open']].ffill()

# ---------------------------------------------------------------------
# 🕒 【時光機關鍵邏輯】抹除「未來」數據
# ---------------------------------------------------------------------
# 注意：快取存的是完整數據，截斷是發生在記憶體中，因此你可以隨意更改 SIMULATE_DAYS_AGO 而不需重新下載
if SIMULATE_DAYS_AGO > 0:
    print(f"⏰ [時光機] 正在抹除最近 {SIMULATE_DAYS_AGO} 天數據，回溯中...")
    closes = closes.iloc[:-SIMULATE_DAYS_AGO]
    highs = highs.iloc[:-SIMULATE_DAYS_AGO]
    lows = lows.iloc[:-SIMULATE_DAYS_AGO]
    vols = vols.iloc[:-SIMULATE_DAYS_AGO]
    opens = opens.iloc[:-SIMULATE_DAYS_AGO]

# 👇 獲取模擬當日的日期字串
today_str = closes.index[-1].strftime('%Y-%m-%d')
print(f"📅 [UAT] 模擬今日日期：{today_str}")

# =============================================================================
# MODULE 3 — 雙市場宏觀剖析 (FTD, 市寬, 派發日 獨立計算)
# =============================================================================
vix_c = closes['^VIX'].ffill()

jp_tickers = [t for t in closes.columns if str(t).endswith('.T')]
us_tickers = [t for t in closes.columns if not str(t).endswith('.T') and t not in ['SPY', '^VIX', '^N225']]

# 👇 從 TICKER_MAP 智能提取「大盤成份股」名單
us_index_tickers = [tk for tk, sources in TICKER_MAP.items() if any(s in ['S&P500_大盤', 'S&P500'] for s in sources) and tk in closes.columns]
jp_index_tickers = [tk for tk, sources in TICKER_MAP.items() if any(s in ['NK225', 'TOPIX100'] for s in sources) and tk in closes.columns]

# 👇 極速向量化計算矩陣市寬 (Vectorised Breadth Matrix) - 終極防禦版
def calc_matrix(all_tks, idx_tks):
    valid_all = [t for t in all_tks if t in closes.columns]
    valid_idx = [t for t in idx_tks if t in closes.columns]
    
    # 🛡️ 核心修正：如果維基百科大盤名單抓取失敗 (<50隻)，強制用全體股票名單頂上，避免 0% 慘劇！
    if len(valid_idx) < 50:
        valid_idx = valid_all
        
    if not valid_all or len(valid_all) < 50: 
        return {'total_20ma_pct': 0, 'total_50ma_pct': 0, 'index_50ma_pct': 0, 'index_200ma_pct': 0}
    
    # 過濾當日休市 (全 NaN) 的行，自動退回上一個有效交易日
    c_all = closes[valid_all].dropna(how='all')
    c_idx = closes[valid_idx].dropna(how='all')
    
    if c_all.empty or c_idx.empty:
         return {'total_20ma_pct': 0, 'total_50ma_pct': 0, 'index_50ma_pct': 0, 'index_200ma_pct': 0}

    # 取得最後一個「有效交易日」的數據
    last_c_all = c_all.iloc[-1]
    last_c_idx = c_idx.iloc[-1]
    
    # 計算 MA
    ma20_all = c_all.rolling(20, min_periods=10).mean().iloc[-1]
    ma50_all = c_all.rolling(50, min_periods=25).mean().iloc[-1]
    ma50_idx = c_idx.rolling(50, min_periods=25).mean().iloc[-1]
    ma200_idx = c_idx.rolling(200, min_periods=100).mean().iloc[-1]
    
    def safe_pct(price_series, ma_series):
        valid_mask = price_series.notna() & ma_series.notna()
        if valid_mask.sum() < 20: return 0 
        return (price_series[valid_mask] > ma_series[valid_mask]).sum() / valid_mask.sum() * 100

    return {
        'total_20ma_pct': round(float(safe_pct(last_c_all, ma20_all)), 1),
        'total_50ma_pct': round(float(safe_pct(last_c_all, ma50_all)), 1),
        'index_50ma_pct': round(float(safe_pct(last_c_idx, ma50_idx)), 1),
        'index_200ma_pct': round(float(safe_pct(last_c_idx, ma200_idx)), 1)
    }

us_matrix = calc_matrix(us_tickers, us_index_tickers)
jp_matrix = calc_matrix(jp_tickers, jp_index_tickers)

def calc_macro_regime(index_ticker):
    idx_c, idx_v, idx_l = closes[index_ticker], vols[index_ticker], lows[index_ticker]
    ret = idx_c.pct_change()
    dist_mask = (ret < -0.002) & (idx_v > idx_v.shift(1))
    curr_dist_days = int(dist_mask.rolling(25).sum().iloc[-1])
    
    ftd_history = np.zeros(len(idx_c))
    rally_day, rally_low, last_ftd_idx = 0, float('inf'), -999
    
    for i in range(1, len(idx_c)):
        c, pc, l, v, pv = idx_c.iloc[i], idx_c.iloc[i-1], idx_l.iloc[i], idx_v.iloc[i], idx_v.iloc[i-1]
        if l < rally_low: rally_low, rally_day = l, 1 if c > pc else 0
        else:
            if c > pc: rally_day = max(1, rally_day + 1)
            elif rally_day > 0: rally_day += 1
        if rally_day >= 4 and c > pc * 1.012 and v > pv:
            last_ftd_idx, rally_low, rally_day = i, c, 0
        ftd_history[i] = (i - last_ftd_idx) if last_ftd_idx > 0 else 999
        
    curr_ftd_days = int(ftd_history[-1])
    is_bull = float(idx_c.iloc[-1]) > float(idx_c.rolling(200).mean().iloc[-1])
    
    if vix_c.iloc[-1] > 25: status, color = "🚨 VIX 恐慌警戒", "text-red-500 bg-red-500/20 border-red-500/50"
    elif is_bull: status, color = "🟢 牛市格局", "text-emerald-500 bg-emerald-500/10 border-emerald-500/20"
    elif curr_ftd_days <= FTD_VALID_DAYS: status, color = f"✅ 底部確認 ({curr_ftd_days}日 FTD)", "text-blue-400 bg-blue-500/10 border-blue-500/20"
    else: status, color = "❌ 熊市空頭", "text-red-500 bg-red-500/10 border-red-500/20"
    
    return curr_dist_days, is_bull, status, color

us_dist, us_is_bull, us_status, us_color = calc_macro_regime('SPY')
jp_dist, jp_is_bull, jp_status, jp_color = calc_macro_regime('^N225')

# 判定紅黃綠燈
def evaluate_market_health(price, ma200, idx_50, tot_50, idx_200, tot_20, dist):
    if price < ma200 or idx_200 < 30 or dist >= 6: return "🔴 防禦/熊市:", 16711680, " 長線破位或極端派發，嚴禁新建倉，現金為主。"
    elif (idx_50 > 50 and tot_50 < 30): return "🟡 內部背馳:", 16766720, " 指數強但中小盤弱 (拉大出細)，注碼減半，鎖定利潤。"
    elif idx_50 < 40 or dist >= 4: return "🟡 派發警告:", 16766720, " 大市動力減弱，提高警覺，切勿追高。"
    elif tot_20 < 15: return "🟡 極度超賣:", 16766720, " 短線跌幅極端，隨時暴力反彈，留意底部 VCP。"
    elif idx_50 >= 50 and tot_50 >= 40 and dist <= 3: return "🟢 全面牛市:", 65280, " 大細盤共振向上，勝率極高，可 Full Size 積極做多！"
    else: return "⚪ 震盪過渡:", 8421504, " 大市方向未明，維持現有持倉，小注試水溫。"

spx_price, spx_200ma = float(closes['SPY'].iloc[-1]), float(closes['SPY'].rolling(200).mean().iloc[-1])
n225_price, n225_200ma = float(closes['^N225'].iloc[-1]), float(closes['^N225'].rolling(200).mean().iloc[-1])

# 抽出狀態、顏色同行動指引
us_macro_status, us_macro_color, us_action = evaluate_market_health(spx_price, spx_200ma, us_matrix['index_50ma_pct'], us_matrix['total_50ma_pct'], us_matrix['index_200ma_pct'], us_matrix['total_20ma_pct'], us_dist)
jp_macro_status, jp_macro_color, jp_action = evaluate_market_health(n225_price, n225_200ma, jp_matrix['index_50ma_pct'], jp_matrix['total_50ma_pct'], jp_matrix['index_200ma_pct'], jp_matrix['total_20ma_pct'], jp_dist)

# 繪製 SPY 圖表
spy_c, spy_v, spy_l = closes['SPY'], vols['SPY'], lows['SPY']
spy_20, spy_50, spy_200 = spy_c.rolling(20).mean(), spy_c.rolling(50).mean(), spy_c.rolling(200).mean()
fig, ax = plt.subplots(figsize=(8, 3), dpi=100)
ax.plot(spy_c.index[-200:], spy_c.iloc[-200:], color='#cbd5e1', label='SPX', linewidth=1.5)
ax.plot(spy_20.index[-200:], spy_20.iloc[-200:], color='#3b82f6', label='20MA', linewidth=1, alpha=0.8)
ax.plot(spy_50.index[-200:], spy_50.iloc[-200:], color='#f59e0b', label='50MA', linewidth=1, alpha=0.8)
ax.plot(spy_200.index[-200:], spy_200.iloc[-200:], color='#dc2626', label='200MA', linestyle='-.', linewidth=1.5)
fig.patch.set_facecolor('#0f172a'); ax.set_facecolor('#0f172a')
ax.tick_params(colors='white', labelsize=8)
ax.legend(facecolor='#1e293b', labelcolor='white', loc='upper left', ncol=3, fontsize=8)
for spine in ax.spines.values(): spine.set_edgecolor('#334155')
plt.tight_layout()
plt.savefig(os.path.join(CHARTS_DIR, "SPY_Trend.png"), transparent=True)
plt.close(fig)

r126 = closes / closes.shift(126) - 1
r252 = closes / closes.shift(252) - 1

# 檢查是否因為時光機回溯太深，導致 252 日數據全線陣亡
if r252.isna().all().all():
    print("⚠️ [系統警告] 剩餘數據不足 252 日！RS 計算將智能降級為半年期 (126日) 基準。")
    rs_rank = r126.rank(axis=1, pct=True) * 99 + 1
else:
    r252_filled = r252.fillna(r126) 
    rs_rank = ((0.6 * r126.fillna(0)) + (0.4 * r252_filled.fillna(0))).rank(axis=1, pct=True) * 99 + 1

rs_momentum = rs_rank - rs_rank.shift(20)

# =============================================================================
# MODULE 4 & 5 — 雙策略判定引擎與自動結算 (🚀 向量化極速版)
# =============================================================================
print(f"⏳ [4-6/7] 正在按 {today_str} 視角進行策略演算 (啟動極速向量化)...")

# 1. 處理現有持倉結案
current_prices = closes.iloc[-1].to_dict()
current_highs = highs.iloc[-1].to_dict()   # 引入全日最高價
current_lows = lows.iloc[-1].to_dict()     # 引入全日最低價
dict_low20 = lows.rolling(20).min().iloc[-1].to_dict()
dict_low5 = lows.rolling(5).min().iloc[-1].to_dict()
closed_this_run = []

for trade in trade_history:
    if trade.get('status') == 'OPEN':
        tk = trade.get('tk')
        if tk in current_prices and not pd.isna(current_prices[tk]):
            now_px = round(float(current_prices[tk]), 2)
            today_high = float(current_highs[tk])
            today_low = float(current_lows[tk])
            buy_px = trade.get('px')
            strat_tag = trade.get('tag', '')
            trade['last_px'] = now_px

            if now_px > buy_px * 10 or now_px < buy_px * 0.1: continue
            if 'partial_tp_hit' not in trade: trade['partial_tp_hit'] = False
            if 'initial_sl' not in trade: trade['initial_sl'] = trade['sl']
            
            initial_risk = buy_px - trade['initial_sl']
            is_short_term = ('缺口' in strat_tag or '超賣' in strat_tag)
            
            # 👇 讀取專屬的 TP1 價格 (相容舊紀錄)
            tp1_price = trade.get('tp1_price', round(buy_px + (initial_risk * 2), 2))
            
            # --- 分注平倉 ---
            if not trade['partial_tp_hit'] and today_high >= tp1_price and initial_risk > 0:
                trade['partial_tp_hit'] = True
                trade['sl'] = buy_px
                print(f"🎯 [分注系統] {tk} 觸發 TP1 ({tp1_price})，保本鎖定。")

            # --- 最終結案判定 (3-Way Classification) ---
            tp, sl = trade.get('tp'), trade.get('sl')
            hit_tp = tp and today_high >= tp
            hit_sl = sl and today_low <= sl
            
            if trade['partial_tp_hit']:
                # 🌟 改善三：雙軌放飛制 (短線 5 日，波段 20 日)
                tk_trail_low = dict_low5.get(tk, today_low) if is_short_term else dict_low20.get(tk, today_low)
                
                if today_low <= tk_trail_low:
                    trade['last_px'] = round((tp1_price + max(today_low, tk_trail_low)) / 2, 2)
                    trade['status'], trade['close_date'] = '✅ TRAIL EXIT', today_str
                    closed_this_run.append(trade)
                elif hit_tp:
                    trade['last_px'] = round((tp1_price + tp) / 2, 2)
                    trade['status'], trade['close_date'] = '✅ MAX TP', today_str
                    closed_this_run.append(trade)
            else:
                if hit_sl:
                    trade['last_px'] = sl
                    trade['status'], trade['close_date'] = '❌ STOP LOSS', today_str
                    closed_this_run.append(trade)
                elif hit_tp:
                    trade['last_px'] = tp
                    trade['status'], trade['close_date'] = '✅ MAX TP', today_str
                    closed_this_run.append(trade)

swing_results, short_term_results, js_payload = [], [], []

# =========================================================================
# ⚡ 核心提速區：向量化計算所有技術指標 (Out of Loop)
# =========================================================================
# 價格與成交量基礎
prev_prices = closes.iloc[-2]
curr_opens = opens.iloc[-1]
curr_vols = vols.iloc[-1]

# 平均成交額 (20日)
dollar_vol_20 = (closes * vols).rolling(20).mean().iloc[-1]

# 布林帶 (Bollinger Bands)
sma20_all = closes.rolling(20).mean()
std20_all = closes.rolling(20).std()
bb_lower_all = sma20_all - (2 * std20_all)
bb_width_all = (4 * std20_all) / sma20_all
bb_width_min120 = bb_width_all.rolling(120).min().iloc[-1]

# ATR
atr_14 = (highs - lows).rolling(14).mean().iloc[-1]

# RSI
delta = closes.diff()
gain = delta.where(delta > 0, 0).rolling(14).mean()
loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
rsi_14 = (100 - (100 / (1 + gain / loss))).iloc[-1]

# VCP 形態參數 (Base Drawdown & Recent Volatility)
max60 = closes.rolling(60).max()
min60 = closes.rolling(60).min()
base_dd = ((max60 - min60) / max60).iloc[-1]

max10 = closes.rolling(10).max()
min10 = closes.rolling(10).min()
rec_volat = ((max10 - min10) / max10).iloc[-1]

sma50_all = closes.rolling(50).mean()
sma200_all = closes.rolling(200).mean()
max120_all = closes.rolling(120).max() # 半年高位
max10_prev_all = closes.shift(1).rolling(10).max() # 尋日為止嘅10日高位 (阻力線)

# Volume MA
vol_ma50 = vols.rolling(50).mean().iloc[-1]
vol_ma20 = vols.rolling(20).mean().iloc[-1]

# 👇 將最終結果轉為 Dict 以達到 O(1) 極速查詢 (慳 CPU 神技)
dict_dollar_vol = dollar_vol_20.to_dict()
dict_rs = rs_rank.iloc[-1].to_dict()
dict_mom = rs_momentum.iloc[-1].to_dict()
dict_bb_lower = bb_lower_all.iloc[-1].to_dict()
dict_bb_width = bb_width_all.iloc[-1].to_dict()
dict_bb_width_min120 = bb_width_min120.to_dict()
dict_atr = atr_14.to_dict()
dict_rsi = rsi_14.to_dict()
dict_base_dd = base_dd.to_dict()
dict_rec_volat = rec_volat.to_dict()
dict_vol_ma50 = vol_ma50.to_dict()

dict_vol_ma20 = vol_ma20.to_dict()
dict_prev_price = prev_prices.to_dict()
dict_curr_open = curr_opens.to_dict()
dict_curr_vol = curr_vols.to_dict()
dict_prev_vol = vols.iloc[-2].to_dict()
dict_curr_high = highs.iloc[-1].to_dict()
dict_curr_low = lows.iloc[-1].to_dict()
dict_sma50 = sma50_all.iloc[-1].to_dict()
dict_sma200 = sma200_all.iloc[-1].to_dict()
dict_max120 = max120_all.iloc[-1].to_dict()
dict_max10_prev = max10_prev_all.iloc[-1].to_dict()
# =========================================================================

# 找出美股成交額 > 500萬 USD 的股票
us_mask = (~dollar_vol_20.index.str.endswith('.T')) & (dollar_vol_20 >= 5_000_000)
# 找出日股成交額 > 3億 JPY 的股票
jp_mask = (dollar_vol_20.index.str.endswith('.T')) & (dollar_vol_20 >= 300_000_000)

# 合併符合資格的名單
valid_tickers = dollar_vol_20[us_mask | jp_mask].index.tolist()

# 踢走大盤指數與 RS 無效的新股
valid_tickers = [t for t in valid_tickers if t not in ['SPY', '^VIX', '^N225'] and not pd.isna(dict_rs.get(t))]

print(f"🧹 過濾成交量低迷股票後，掃描名單由 {len(ALL_TICKERS)} 縮減至 {len(valid_tickers)} 隻！")

# =========================================================================
# 開始極速掃描 (只行精華名單)
# =========================================================================
for ticker in valid_tickers:
    try:
        # 因為上面已經做咗過濾，呢度唔使再 check pd.isna(rs) 同 dollar_vol 啦！
        rs = dict_rs.get(ticker)
        cp = float(current_prices[ticker])
        is_jp = ticker.endswith('.T')

        # 🛡️ 防禦：剔除仙股與錯價股 (美股 > $1, 日股 > 100円)
        min_price_threshold = 100 if is_jp else 1
        if cp < min_price_threshold: continue
        
        # 攞出對應嘅燈號
        ticker_macro = jp_macro_status if is_jp else us_macro_status 

        rs_mom = dict_mom.get(ticker)
        catr = float(dict_atr.get(ticker))
        rsi_val = float(dict_rsi.get(ticker))
        
        # 👇 每日更新「目前持倉」的現時指標 (curr_metric)
        for t in trade_history:
            if t.get('status') == 'OPEN' and t.get('tk') == ticker:
                if '超賣' in t.get('tag', ''):
                    t['curr_metric'] = f"RSI: {int(rsi_val)}"
                else:
                    t['curr_metric'] = f"RS: {int(rs)}"

        # 波段策略 (Swing) 的 RS 門檻過濾
        if rs < PQR_SWING_MIN: continue

        # 提取 VCP 雛形數據
        v_base_dd = dict_base_dd.get(ticker)
        v_rec_vol = dict_rec_volat.get(ticker)
        c_vol = dict_curr_vol.get(ticker)
        v_ma20 = dict_vol_ma20.get(ticker)
        
        # 提取趨勢與突破阻力
        sma50 = dict_sma50.get(ticker)
        sma200 = dict_sma200.get(ticker)
        high120 = dict_max120.get(ticker)
        resist_10d = dict_max10_prev.get(ticker) 
        
        is_uptrend = (cp > sma50) and (sma50 > sma200)
        is_near_high = ((high120 - cp) / high120) <= 0.15
        
        # 🛡️ 優化 1：形態收窄微調至 12% 波幅，提高捕捉活躍領袖股勝率
        is_tight = (v_base_dd <= 0.35) and (v_rec_vol <= 0.12)
        
        # 🚀 今日帶量真突破
        is_breaking_out = (cp > resist_10d) and (c_vol > v_ma20 * 1.2)
        
        is_vcp = is_uptrend and is_near_high and is_tight and is_breaking_out
        is_bb_sqz = (dict_bb_width.get(ticker) <= dict_bb_width_min120.get(ticker) * 1.1)

        trade_info = None 
        tag_name = ""
        sl_p, tp_p = 0, 0
        risk_per_share = 0
        entry_metric = ""

        # 🛡️ 優化 2：放寬大盤大閘。只要不是「🔴防禦熊市」而且不是「🟡派發警告」，系統即通行
        # 🌟 提取燈號狀態
        is_red_light = '🔴' in ticker_macro
        is_yellow_light = '🟡' in ticker_macro

        if (is_vcp or is_bb_sqz):
            # ⛔ 改善二 (煞車)：紅燈嚴禁任何波段新建倉！
            if is_red_light: continue 
            
            tag_name = "🏆 VCP 突破" if is_vcp else "💥 BB 擠壓"
            sl_p = round(cp - 1.5 * catr, 2)
            tp_p = round(cp + 4.5 * catr, 2) 
            risk_per_share = cp - sl_p
            entry_metric = f"RS: {int(rs)}"
            
            # ⚖️ 改善二 (動態目標)：黃燈降溫，+1R 就食第一注
            target_r = 1.0 if is_yellow_light else 2.0
            tp1_price = round(cp + (risk_per_share * target_r), 2)
            
            swing_results.append({'tk': ticker, 'rs': round(rs,0), 'mom': round(rs_mom,1), 'px': round(cp,2), 'sl': sl_p, 'tp': tp_p, 'tag': tag_name})
            
            # 🔔 將 tp1_price 存入系統
            trade_info = {
                'date': today_str, 'tk': ticker, 'px': round(cp, 2), 
                'sl': sl_p, 'tp': tp_p, 'initial_sl': sl_p, 'tp1_price': tp1_price,
                'last_px': round(cp, 2), 'status': 'OPEN', 'tag': tag_name, 
                'entry_metric': entry_metric, 'curr_metric': entry_metric
            }

        elif not trade_info: 
            p_px = dict_prev_price.get(ticker)
            c_op = dict_curr_open.get(ticker)
            v_ma20 = dict_vol_ma20.get(ticker)
            b_lower = dict_bb_lower.get(ticker)
            
            h_val = dict_curr_high.get(ticker)
            l_val = dict_curr_low.get(ticker)
            
            gap_magnitude = (c_op - p_px) / p_px
            closing_strength = (cp - l_val) / (h_val - l_val) if h_val != l_val else 0
            
            is_gap_up = (
                (gap_magnitude >= 0.03) and 
                (c_vol > v_ma20 * 2) and 
                (cp > c_op) and 
                (closing_strength >= 0.6)
            )
            
            is_oversold = (rsi_val < 28) and (cp < b_lower)
            
            if is_gap_up or is_oversold:
                tag_name = "⚡ 缺口動能" if is_gap_up else "📉 極度超賣"
                sl_p, tp_p = round(cp * 0.95, 2), round(cp * 1.15, 2)
                risk_per_share = cp - sl_p
                entry_metric = f"RSI: {int(rsi_val)}" if is_oversold else f"RS: {int(rs)}"
                
                # ⚡ 改善一 (降目標)：短線游擊太難中，一律降至 +1R (5%) 食第一注
                tp1_price = round(cp + (risk_per_share * 1.0), 2)
                
                short_term_results.append({'tk': ticker, 'rs': round(rs,0), 'mom': round(rs_mom,1), 'px': round(cp,2), 'sl': sl_p, 'tp': tp_p, 'tag': tag_name})
                trade_info = {
                    'date': today_str, 'tk': ticker, 'px': round(cp, 2), 
                    'sl': sl_p, 'tp': tp_p, 'initial_sl': sl_p, 'tp1_price': tp1_price,
                    'last_px': round(cp, 2), 'status': 'OPEN', 'tag': tag_name, 
                    'entry_metric': entry_metric, 'curr_metric': entry_metric
                }

        if trade_info:
            # 👇 由 TICKER_MAP 抽返隻股到底屬於邊幾個名單
            ticker_sources = TICKER_MAP.get(ticker, [])
            # 👇 智能獲取板塊與市值
            s_info = get_stock_info(ticker) 
            # 👇 寫入 trade_info，等 Dashboard 讀取
            trade_info['sources'] = ticker_sources
            trade_info['sector'] = s_info['sector']
            trade_info['mcap'] = s_info['mcap']
            # 呼叫 Discord 時傳入專屬的 tp1_price
            send_discord_alert(ticker, tag_name, round(cp, 2), sl_p, tp_p, True, ticker_sources, tp1_price=tp1_price)
            if not any(t.get('tk') == ticker and t.get('status') == 'OPEN' for t in trade_history):
                 trade_history.append(trade_info)
            
            js_payload.append({
                "ticker": ticker, "tag": tag_name, "curr_price": round(cp, 2), 
                "sl_price": sl_p, "tp_price": tp_p, "risk_per_share": risk_per_share
            })

    except Exception as e: 
        pass

swing_results.sort(key=lambda x: x['rs'], reverse=True)
short_term_results.sort(key=lambda x: x['rs'], reverse=True)
 
# 保留 20000 條紀錄以確保歷史倉位對帳準確
with open(HISTORY_FILE, "w", encoding="utf-8") as f: json.dump(trade_history[-20000:], f, indent=4)

# =============================================================================
# MODULE 6 — 總結算與 Discord 報告
# =============================================================================
print("⏳ [6/7] 正在結算戰績並發送 Discord 報告...")

def calculate_stats(history):
    closed = [t for t in history if '✅' in t['status'] or '❌' in t['status']]
    if not closed: return 0, 0, 0
    wins = [t for t in closed if '✅' in t['status']]
    return len(closed), len(wins), round(len(wins)/len(closed)*100, 1)

total_closed, wins, win_rate = calculate_stats(trade_history)

if DISCORD_SUMMARY_WEBHOOK:
    # 1. 今日結案明細
    detail_lines = []
    if closed_this_run:
        for t in closed_this_run:
            shares = 10000 / t['px']
            pnl = shares * (t['last_px'] - t['px'])
            pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
            icon = "🟢" if pnl >= 0 else "🔴"
            detail_lines.append(f"{icon} **{t['tk']}** ({t.get('tag', 'N/A')}): {pnl_str}")
    details_text = "\n".join(detail_lines) if detail_lines else "今日無新結案交易。"

    # 1.5 新增：計算今日新開倉數量與結案數量 (對帳用)
    new_trades_today = [t for t in trade_history if t.get('date') == today_str and t.get('status') == 'OPEN']
    new_count = len(new_trades_today)
    closed_count = len(closed_this_run) if 'closed_this_run' in locals() else 0

    # 2. 目前持倉浮盈與總數量 (🛡️ 兼容分注平倉的精準會計版)
    open_trades = [t for t in trade_history if t.get('status') == 'OPEN']
    current_open_count = len(open_trades)
    
    floating_pnl = 0
    for t in open_trades:
        buy_px = t['px']
        last_px = t['last_px']
        
        if t.get('partial_tp_hit', False):
            # 50% 已經鎖定在 TP1 (+2R)，50% 隨現價浮動
            initial_risk = buy_px - t.get('initial_sl', buy_px)
            tp1_price = buy_px + (initial_risk * 2)
            
            pnl_closed_half = (7500 / buy_px) * (tp1_price - buy_px)   # 已鎖定利潤
            pnl_floating_half = (2500 / buy_px) * (last_px - buy_px) # 剩餘浮動盈虧
            floating_pnl += (pnl_closed_half + pnl_floating_half)
        else:
            # 常規未分注持倉，100% 隨現價浮動
            floating_pnl += (10000 / buy_px) * (last_px - buy_px)
            
    floating_str = f"+${floating_pnl:.2f}" if floating_pnl >= 0 else f"-${abs(floating_pnl):.2f}"

    # 3. 細分策略 P&L 結算 (歷史總計 - 強制清洗並排序)
    strategy_stats = {}
    for t in [x for x in trade_history if '✅' in x['status'] or '❌' in x['status']]:
        raw_tag = t.get('tag', '未分類')
        
        # 🧹 清洗標籤：統一合併分注與全平倉的數據
        clean_tag = raw_tag.replace(' (🎯已分注平倉)', '').replace(' (已分注平倉)', '').replace(' (🎯已部分平倉)', '').strip()
        
        if clean_tag not in strategy_stats: 
            strategy_stats[clean_tag] = {'total': 0, 'wins': 0, 'pnl': 0}
            
        trade_pnl = (10000 / t['px']) * (t['last_px'] - t['px'])
        strategy_stats[clean_tag]['total'] += 1
        strategy_stats[clean_tag]['pnl'] += trade_pnl
        if '✅' in t['status']:
            strategy_stats[clean_tag]['wins'] += 1

    breakdown_lines = []
    
    # 🔠 關鍵修正：使用 sorted() 強制按策略名稱 (tag) 排序
    sorted_stats = sorted(strategy_stats.items(), key=lambda x: x[0])
    
    for tag, st in sorted_stats:
        w_rate = round((st['wins'] / st['total']) * 100, 1) if st['total'] > 0 else 0
        pnl_s = f"+${st['pnl']:.0f}" if st['pnl'] >= 0 else f"-${abs(st['pnl']):.0f}"
        breakdown_lines.append(f"**{tag}**: {w_rate}% 勝率 | P&L: {pnl_s} ({st['total']}單)")
        
    breakdown_text = "\n".join(breakdown_lines) if breakdown_lines else "尚無足夠結案數據。"    
    # ==========================================
    # 👇 搬上嚟：多維度分組對帳邏輯 (Market x Strategy)
    # ==========================================
    group_stats = {'US': {}, 'JP': {}}

    def ensure_strat(mkt, strat):
        if strat not in group_stats[mkt]:
            # prev: 原有持倉, new: 今日新開, closed: 今日結案, final: 最終持倉
            group_stats[mkt][strat] = {'prev': 0, 'new': 0, 'closed': 0, 'final': 0}

    # 1. 統計今日新開 (New)
    for t in new_trades_today:
        mkt = 'JP' if t['tk'].endswith('.T') else 'US'
        strat = t.get('tag', '未分類')
        ensure_strat(mkt, strat)
        group_stats[mkt][strat]['new'] += 1

    # 2. 統計今日結案 (Closed)
    for t in closed_this_run:
        mkt = 'JP' if t['tk'].endswith('.T') else 'US'
        strat = t.get('tag', '未分類')
        ensure_strat(mkt, strat)
        group_stats[mkt][strat]['closed'] += 1

    # 3. 統計最終持倉 (Final Open)
    for t in open_trades:
        mkt = 'JP' if t['tk'].endswith('.T') else 'US'
        strat = t.get('tag', '未分類')
        ensure_strat(mkt, strat)
        group_stats[mkt][strat]['final'] += 1

    # 4. 反推原有持倉 (Prev = Final - New + Closed)
    for mkt in ['US', 'JP']:
        for strat, s in group_stats[mkt].items():
            s['prev'] = s['final'] - s['new'] + s['closed']

    # 5. 生成 Discord 友善排版 (放棄大表格，改用分組與 Inline Code 對齊)
    summary_lines = ["\n**【📊 策略持倉對帳表】**"]
    
    for mkt in ['US', 'JP']:
        mkt_name = "🇺🇸 **美股 (US)**" if mkt == 'US' else "🇯🇵 **日股 (JP)**"
        mkt_lines = []
        
        for strat, s in group_stats[mkt].items():
            if s['prev'] == 0 and s['new'] == 0 and s['closed'] == 0 and s['final'] == 0: continue
            
            # 將中文字/Emoji 與數字拆開，利用 Inline Code (` `) 確保數字絕對垂直對齊
            # 確保 "+" 同 "-" 號後面嘅數字位數一致
            line = f"{strat} ➔ 原有: `{s['prev']:3}` | 新開: `+{s['new']:<2}` | 結案: `-{s['closed']:<2}` ＝ 總持倉: `{s['final']:3}`"
            mkt_lines.append(line)
            
        # 如果該市場有數據，先將市場標題同數據加入總結
        if mkt_lines:
            summary_lines.append(f"\n{mkt_name}")
            summary_lines.extend(mkt_lines)

    group_summary_text = "\n".join(summary_lines)

    # 6. 準備 Discord 宏觀數據
    us_scan_count = len(us_tickers)
    jp_scan_count = len(jp_tickers)

    if us_macro_color == 16711680 or jp_macro_color == 16711680: final_color = 16711680
    elif us_macro_color == 16766720 or jp_macro_color == 16766720: final_color = 16766720
    else: final_color = 65280

    us_macro_str = f"狀態: **{us_macro_status}**\n🔸 盤長(>200MA): **{us_matrix['index_200ma_pct']}%**\n🔸 盤中(>50MA): **{us_matrix['index_50ma_pct']}%**\n🔸 總中(>50MA): **{us_matrix['total_50ma_pct']}%**\n🔸 超賣(>20MA): **{us_matrix['total_20ma_pct']}%**\n🛑 派發: **{us_dist} 日** | 掃描: {us_scan_count}"
    jp_macro_str = f"狀態: **{jp_macro_status}**\n🔸 盤長(>200MA): **{jp_matrix['index_200ma_pct']}%**\n🔸 盤中(>50MA): **{jp_matrix['index_50ma_pct']}%**\n🔸 總中(>50MA): **{jp_matrix['total_50ma_pct']}%**\n🔸 超賣(>20MA): **{jp_matrix['total_20ma_pct']}%**\n🛑 派發: **{jp_dist} 日** | 掃描: {jp_scan_count}"

    # 7. 發送 Payload (將 group_summary_text 放入 description)
    payload = {
        "embeds": [{
            "title": f"📊 系統戰績與 3D 矩陣雷達 ({today_str})", 
            "description": f"**今日結案動態:**\n{details_text}\n{group_summary_text}\n\n**🔍 各策略歷史表現:**\n{breakdown_text}",
            "color": final_color,
            "fields": [
                {"name": '\u200b', "value": '\u200b', "inline": False},
                {"name": "🇺🇸 美股 (SPX vs Total)", "value": us_macro_str, "inline": True},
                {"name": "🇯🇵 日股 (N225 vs Total)", "value": jp_macro_str, "inline": True},
                {"name": '\u200b', "value": '\u200b', "inline": False},
                {"name": "🇺🇸 美股行動指引", "value": f"`{us_action}`", "inline": False},
                {"name": "🇯🇵 日股行動指引", "value": f"`{jp_action}`", "inline": False},
                {"name": '\u200b', "value": '\u200b', "inline": False},
                {"name": "🆕 今日新開", "value": f"{new_count} 隻", "inline": True},
                {"name": "🏁 今日結案", "value": f"{closed_count} 隻", "inline": True},
                {"name": "📂 總持倉量", "value": f"{current_open_count} 隻", "inline": True},
                {"name": "🌊 總浮動盈虧", "value": f"**{floating_str}**", "inline": True},
                {"name": "📈 總勝率", "value": f"{win_rate}% ({wins}/{total_closed})", "inline": True}
            ],
            "footer": {"text": f"每單本金 $10,000 USD | 時光機回溯 {SIMULATE_DAYS_AGO} 日"}
        }]
    }
    
    try: requests.post(DISCORD_SUMMARY_WEBHOOK, json=payload)
    except: pass
# =============================================================================
# MODULE 7 — 生成 UAT 前端 HTML (雙分頁系統：Dashboard + Journal)
# =============================================================================
print("⏳ [7/7] 正在生成雙分頁量化儀表板...")

def get_unit(tk): return "¥" if tk.endswith(".T") else "$"

# 👇 新增：準備歷史走勢圖表數據 (最近 60 日)
print("⏳ 正在生成歷史宏觀走勢圖表數據...")
hist_dates = closes.index[-60:]

# 🛡️ 核心修正：利用 ffill() 解決歷史走勢圖休市變 0 的問題
c_us_valid = closes[us_tickers].ffill()
c_us_idx_valid = closes[us_index_tickers].ffill()
v_us_tot50 = (c_us_valid > c_us_valid.rolling(50, min_periods=25).mean()).sum(axis=1) / max(1, len(us_tickers)) * 100
v_us_idx50 = (c_us_idx_valid > c_us_idx_valid.rolling(50, min_periods=25).mean()).sum(axis=1) / max(1, len(us_index_tickers)) * 100
v_us_idx200 = (c_us_idx_valid > c_us_idx_valid.rolling(200, min_periods=100).mean()).sum(axis=1) / max(1, len(us_index_tickers)) * 100

c_jp_valid = closes[jp_tickers].ffill()
c_jp_idx_valid = closes[jp_index_tickers].ffill()
v_jp_tot50 = (c_jp_valid > c_jp_valid.rolling(50, min_periods=25).mean()).sum(axis=1) / max(1, len(jp_tickers)) * 100
v_jp_idx50 = (c_jp_idx_valid > c_jp_idx_valid.rolling(50, min_periods=25).mean()).sum(axis=1) / max(1, len(jp_index_tickers)) * 100
v_jp_idx200 = (c_jp_idx_valid > c_jp_idx_valid.rolling(200, min_periods=100).mean()).sum(axis=1) / max(1, len(jp_index_tickers)) * 100

# 向量化計算歷史派發日
us_dist_mask = (closes['SPY'].pct_change() < -0.002) & (vols['SPY'] > vols['SPY'].shift(1))
us_hist_dist = us_dist_mask.rolling(25).sum()
jp_dist_mask = (closes['^N225'].pct_change() < -0.002) & (vols['^N225'] > vols['^N225'].shift(1))
jp_hist_dist = jp_dist_mask.rolling(25).sum()

chart_data = []
for i, d in enumerate(hist_dates):
    d_str = d.strftime('%Y-%m-%d')
    us_open_profit, us_open_loss = 0, 0
    jp_open_profit, jp_open_loss = 0, 0
    
    strat_counts = {"VCP": 0, "BB": 0, "GAP": 0, "OVERSOLD": 0}
    
    # 🌟 新增：用來記錄截至當日的累積利潤 (Cumulative P&L)
    cum_pnl = {"VCP": 0, "BB": 0, "GAP": 0, "OVERSOLD": 0}
        
    d_prices = closes.loc[d]
        
    for t in trade_history:
        # 1. 計算 Open 數量狀態
        if t['date'] <= d_str:
            c_date = t.get('close_date', '9999-99-99')
            if c_date > d_str or t.get('status') == 'OPEN':
                tk = t['tk']
                if tk in d_prices.index and not pd.isna(d_prices[tk]):
                    is_profit = float(d_prices[tk]) >= float(t['px'])
                        
                    if tk.endswith('.T'):
                        if is_profit: jp_open_profit += 1
                        else: jp_open_loss += 1
                    else:
                        if is_profit: us_open_profit += 1
                        else: us_open_loss += 1
                    
                    tag = t.get('tag', '')
                    if 'VCP' in tag: strat_counts['VCP'] += 1
                    elif 'BB' in tag: strat_counts['BB'] += 1
                    elif '缺口' in tag: strat_counts['GAP'] += 1
                    elif '超賣' in tag: strat_counts['OVERSOLD'] += 1
            
            # 2. 🌟 新增：計算累積 P&L (只計當日或之前已經結案的單)
            if c_date <= d_str and ('✅' in t.get('status', '') or '❌' in t.get('status', '')):
                pnl = (10000 / t['px']) * (t['last_px'] - t['px'])
                tag = t.get('tag', '')
                if 'VCP' in tag: cum_pnl['VCP'] += pnl
                elif 'BB' in tag: cum_pnl['BB'] += pnl
                elif '缺口' in tag: cum_pnl['GAP'] += pnl
                elif '超賣' in tag: cum_pnl['OVERSOLD'] += pnl

    # 判斷歷史燈號顏色 (保留你原本的邏輯)
    us_c_color = "#22c55e" 
    if closes['SPY'].loc[d] < closes['SPY'].rolling(200).mean().loc[d] or v_us_idx200.loc[d] < 30 or us_hist_dist.loc[d] >= 6: us_c_color = "#ef4444"
    elif (v_us_idx50.loc[d] > 50 and v_us_tot50.loc[d] < 30) or v_us_idx50.loc[d] < 40 or us_hist_dist.loc[d] >= 4: us_c_color = "#eab308"
        
    jp_c_color = "#22c55e"
    if closes['^N225'].loc[d] < closes['^N225'].rolling(200).mean().loc[d] or v_jp_idx200.loc[d] < 30 or jp_hist_dist.loc[d] >= 6: jp_c_color = "#ef4444"
    elif (v_jp_idx50.loc[d] > 50 and v_jp_tot50.loc[d] < 30) or v_jp_idx50.loc[d] < 40 or jp_hist_dist.loc[d] >= 4: jp_c_color = "#eab308"
        
    chart_data.append({
        'date': d_str,
        'us_idx_breadth': round(float(v_us_idx50.loc[d]), 1), 'us_tot_breadth': round(float(v_us_tot50.loc[d]), 1),
        'us_open_profit': us_open_profit, 'us_open_loss': us_open_loss, 'us_color': us_c_color,
        'jp_idx_breadth': round(float(v_jp_idx50.loc[d]), 1), 'jp_tot_breadth': round(float(v_jp_tot50.loc[d]), 1),
        'jp_open_profit': jp_open_profit, 'jp_open_loss': jp_open_loss, 'jp_color': jp_c_color,
        
        'strat_vcp': strat_counts['VCP'], 'strat_bb': strat_counts['BB'],
        'strat_gap': strat_counts['GAP'], 'strat_oversold': strat_counts['OVERSOLD'],
        
        # 🌟 新增：匯出累積 P&L 數據
        'pnl_vcp': round(cum_pnl['VCP'], 2),
        'pnl_bb': round(cum_pnl['BB'], 2),
        'pnl_gap': round(cum_pnl['GAP'], 2),
        'pnl_oversold': round(cum_pnl['OVERSOLD'], 2)
    })

chart_data_str = json.dumps(chart_data)
# ==========================================

# 將 Python 字典轉為 JSON 字串，直接注入 JS，避免 fetch CORS 錯誤
js_payload_str = json.dumps(js_payload)
trade_history_str = json.dumps(trade_history)

html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <script src="https://cdn.tailwindcss.com"></script>
    <script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
    <title>UAT QUANT ({today_str})</title>
    <script src="https://cdn.jsdelivr.net/npm/apexcharts"></script>
    <style>
        .apexcharts-tooltip {{
            z-index: 99999 !important; 
        }}
        th.cursor-pointer {{ transition: color 0.2s; }}
        th.cursor-pointer:hover {{ color: #f8fafc; }}
    </style>
</head>
<body class="bg-[#020617] text-slate-300 p-4 font-sans h-screen flex flex-col overflow-hidden">
    
    <header class="bg-slate-900 border border-slate-800 rounded-xl p-3 shrink-0 mb-3 shadow-lg flex flex-col gap-3 relative overflow-hidden">
        <div class="absolute -right-10 -top-10 opacity-5 pointer-events-none transform rotate-12">
            <span class="text-9xl font-black italic">UAT TEST</span>
        </div>
        
        <div class="flex justify-between items-center z-10">
            <div class="flex items-center gap-4">
                <div>
                    <h1 class="text-2xl font-black text-white italic tracking-tighter">UAT場 <span class="text-fuchsia-500">QUANT</span></h1>
                    <div class="mt-1 inline-block px-3 py-0.5 bg-fuchsia-500/20 border border-fuchsia-500/30 rounded-full text-fuchsia-400 text-[10px] font-black tracking-widest shadow-[0_0_15px_rgba(217,70,239,0.2)]">
                        🕰️ 時光機: {today_str}
                    </div>
                </div>
                <div class="flex gap-2 ml-6 bg-slate-950 p-1 rounded-lg border border-slate-800">
                    <button id="tabBtn-dashboard" onclick="switchTab('dashboard')" class="bg-indigo-600 text-white px-4 py-1.5 rounded-md font-bold text-sm shadow-md transition">📊 儀表板 (Dashboard)</button>
                    <button id="tabBtn-journal" onclick="switchTab('journal')" class="text-slate-400 hover:text-white hover:bg-slate-800 px-4 py-1.5 rounded-md font-bold text-sm transition">📜 交易日誌 (Journal)</button>
                    <button id="tabBtn-charts" onclick="switchTab('charts')" class="text-slate-400 hover:text-white hover:bg-slate-800 px-4 py-1.5 rounded-md font-bold text-sm transition">📈 宏觀走勢 (Charts)</button>
                </div>
            </div>
            <div class="text-xs font-black text-slate-500 bg-black/50 px-3 py-1 rounded-lg border border-slate-800">🌐 Dual-Market Macro Radar</div>
        </div>

        <div class="grid grid-cols-2 gap-4 z-10">
            <div class="flex items-center gap-2 bg-slate-800/30 p-2 rounded-lg border border-slate-800">
                <div class="w-16 text-center text-xs font-black text-slate-400 border-r border-slate-700">美股<br><span class="text-[9px] {us_color}">{us_status.split(' ', 1)[-1] if ' ' in us_status else us_status}</span></div>
                <div class="flex-1 grid grid-cols-5 gap-1 px-2 text-center items-center">
                    <div class="flex flex-col"><span class="text-[8px] text-slate-500">大盤>200MA</span><span class="text-[11px] font-bold {'text-emerald-400' if us_matrix['index_200ma_pct']>=40 else 'text-red-400'}">{us_matrix['index_200ma_pct']}%</span></div>
                    <div class="flex flex-col"><span class="text-[8px] text-slate-500">大盤>50MA</span><span class="text-[11px] font-bold {'text-emerald-400' if us_matrix['index_50ma_pct']>=40 else 'text-amber-400' if us_matrix['index_50ma_pct']>=20 else 'text-red-400'}">{us_matrix['index_50ma_pct']}%</span></div>
                    <div class="flex flex-col border-l border-slate-700/50 pl-1"><span class="text-[8px] text-slate-500">全市>50MA</span><span class="text-[11px] font-bold {'text-emerald-400' if us_matrix['total_50ma_pct']>=40 else 'text-red-400'}">{us_matrix['total_50ma_pct']}%</span></div>
                    <div class="flex flex-col"><span class="text-[8px] text-slate-500">超賣>20MA</span><span class="text-[11px] font-bold {'text-red-500' if us_matrix['total_20ma_pct']<=15 else 'text-slate-300'}">{us_matrix['total_20ma_pct']}%</span></div>
                    <div class="flex flex-col border-l border-slate-700/50 pl-1"><span class="text-[8px] text-slate-500">派發日</span><span class="text-[11px] font-bold {'text-red-400' if us_dist>=5 else 'text-emerald-400'}">{us_dist}d</span></div>
                </div>
            </div>
            
            <div class="flex items-center gap-2 bg-slate-800/30 p-2 rounded-lg border border-slate-800">
                <div class="w-16 text-center text-xs font-black text-slate-400 border-r border-slate-700">日股<br><span class="text-[9px] {jp_color}">{jp_status.split(' ', 1)[-1] if ' ' in jp_status else jp_status}</span></div>
                <div class="flex-1 grid grid-cols-5 gap-1 px-2 text-center items-center">
                    <div class="flex flex-col"><span class="text-[8px] text-slate-500">大盤>200MA</span><span class="text-[11px] font-bold {'text-emerald-400' if jp_matrix['index_200ma_pct']>=40 else 'text-red-400'}">{jp_matrix['index_200ma_pct']}%</span></div>
                    <div class="flex flex-col"><span class="text-[8px] text-slate-500">大盤>50MA</span><span class="text-[11px] font-bold {'text-emerald-400' if jp_matrix['index_50ma_pct']>=40 else 'text-amber-400' if jp_matrix['index_50ma_pct']>=20 else 'text-red-400'}">{jp_matrix['index_50ma_pct']}%</span></div>
                    <div class="flex flex-col border-l border-slate-700/50 pl-1"><span class="text-[8px] text-slate-500">全市>50MA</span><span class="text-[11px] font-bold {'text-emerald-400' if jp_matrix['total_50ma_pct']>=40 else 'text-red-400'}">{jp_matrix['total_50ma_pct']}%</span></div>
                    <div class="flex flex-col"><span class="text-[8px] text-slate-500">超賣>20MA</span><span class="text-[11px] font-bold {'text-red-500' if jp_matrix['total_20ma_pct']<=15 else 'text-slate-300'}">{jp_matrix['total_20ma_pct']}%</span></div>
                    <div class="flex flex-col border-l border-slate-700/50 pl-1"><span class="text-[8px] text-slate-500">派發日</span><span class="text-[11px] font-bold {'text-red-400' if jp_dist>=5 else 'text-emerald-400'}">{jp_dist}d</span></div>
                </div>
            </div>
        </div>
    </header>

    <main id="tab-dashboard" class="flex-1 flex gap-4 overflow-hidden z-10">
        <div class="w-1/3 flex flex-col gap-4 overflow-hidden">
            <div class="bg-slate-900 p-2 rounded-xl border border-slate-800 h-[200px] shrink-0 relative flex items-center justify-center shadow-lg">
                <div class="absolute top-2 left-3 z-10 flex gap-2 items-center">
                    <span class="text-xs font-bold text-slate-400">SPX Anatomy:</span>
                    <span class="text-[9px] bg-red-500/20 text-red-400 px-1 rounded border border-red-500/30">200MA</span>
                    <span class="text-[9px] text-emerald-400 ml-2">▲ FTD</span>
                </div>
                <img src="charts/SPY_Trend.png" class="max-h-full max-w-full object-contain">
            </div>

            <div class="bg-slate-900 rounded-xl border border-slate-800 flex-1 flex flex-col overflow-hidden shadow-lg">
                <div class="p-3 border-b border-slate-800 font-black text-fuchsia-400 flex justify-between items-center shrink-0">
                    <span>🎯 模擬推介信號 (點擊查看)</span>
                </div>
                <div class="overflow-y-auto flex-1 p-2 space-y-2" id="signal-list">
                    <div class="text-[10px] font-bold text-slate-500 uppercase ml-1 mt-2">🏆 波段策略 (Swing)</div>
                    {"".join([f'''
                    <div class="bg-slate-800/50 hover:bg-fuchsia-900/30 cursor-pointer border border-slate-700/50 hover:border-fuchsia-500/50 rounded-lg p-2 transition" onclick="loadContent('{d['tk']}')">
                        <div class="flex justify-between items-center">
                            <span class="font-black text-white text-sm">{d['tk']}</span>
                            <span class="text-[9px] bg-fuchsia-500/20 text-fuchsia-300 px-1.5 py-0.5 rounded">{d['tag']}</span>
                        </div>
                        <div class="flex justify-between text-[10px] text-slate-400 mt-1">
                            <span>RS: {d['rs']} (<span class="{ 'text-emerald-400' if d['mom']>0 else 'text-red-400'}">{'+' if d['mom']>0 else ''}{d['mom']}</span>)</span>
                            <span class="font-bold text-white">現價: {get_unit(d['tk'])}{d['px']}</span>
                        </div>
                        <div class="flex justify-between text-[9px] mt-1.5 pt-1.5 border-t border-slate-700/50">
                            <span class="text-emerald-400 font-mono">🎯 TP: {get_unit(d['tk'])}{d['tp']} (+{((d['tp']-d['px'])/d['px']*100):.1f}%)</span>
                            <span class="text-red-400 font-mono">🛑 SL: {get_unit(d['tk'])}{d['sl']} ({((d['sl']-d['px'])/d['px']*100):.1f}%)</span>
                        </div>
                    </div>
                    ''' for d in swing_results]) if swing_results else '<p class="text-slate-600 italic text-xs px-2">無訊號</p>'}
                    
                    <div class="text-[10px] font-bold text-slate-500 uppercase ml-1 mt-4">⚡ 短線游擊 (Short Term)</div>
                    {"".join([f'''
                    <div class="bg-slate-800/50 hover:bg-amber-900/30 cursor-pointer border border-slate-700/50 hover:border-amber-500/50 rounded-lg p-2 transition" onclick="loadContent('{d['tk']}')">
                        <div class="flex justify-between items-center">
                            <span class="font-black text-white text-sm">{d['tk']}</span>
                            <span class="text-[9px] bg-amber-500/20 text-amber-300 px-1.5 py-0.5 rounded">{d['tag']}</span>
                        </div>
                        <div class="flex justify-between text-[10px] text-slate-400 mt-1">
                            <span>RS: {d['rs']}</span>
                            <span class="font-bold text-white">現價: {get_unit(d['tk'])}{d['px']}</span>
                        </div>
                        <div class="flex justify-between text-[9px] mt-1.5 pt-1.5 border-t border-slate-700/50">
                            <span class="text-emerald-400 font-mono">🎯 TP: {get_unit(d['tk'])}{d['tp']} (+{((d['tp']-d['px'])/d['px']*100):.1f}%)</span>
                            <span class="text-red-400 font-mono">🛑 SL: {get_unit(d['tk'])}{d['sl']} ({((d['sl']-d['px'])/d['px']*100):.1f}%)</span>
                        </div>
                    </div>
                    ''' for d in short_term_results]) if short_term_results else '<p class="text-slate-600 italic text-xs px-2">無訊號</p>'}
                </div>
            </div>
        </div>

        <div class="w-2/3 flex flex-col gap-4 h-full">
            <div class="bg-slate-900 rounded-xl border border-slate-700 p-4 shrink-0 shadow-lg">
                <div class="flex justify-between items-center mb-3">
                    <div class="flex items-center gap-2">
                        <h3 class="text-sm font-black text-amber-500">🧮 專業部位計算機</h3>
                        <span id="calc_ticker_name" class="text-xs font-bold text-white bg-slate-700 px-2 py-0.5 rounded">-</span>
                        <a id="tv_out_link" href="#" target="_blank" class="hidden text-[10px] font-bold bg-blue-600/30 text-blue-400 border border-blue-500/50 hover:bg-blue-600 hover:text-white px-2 py-0.5 rounded transition">🔗 在 TV 開啟</a>
                    </div>
                    <div class="flex items-center gap-2">
                        <label class="text-[10px] text-slate-400 font-bold uppercase">總資金 (Account Size):</label>
                        <input type="number" id="acc_size" value="10000" class="bg-slate-800 border border-slate-600 text-white text-xs px-2 py-1 rounded w-24 text-right focus:outline-none focus:border-amber-500" onchange="updateCalculator()" onkeyup="updateCalculator()">
                    </div>
                </div>
                <div class="grid grid-cols-5 gap-3 text-center">
                    <div class="bg-slate-800/50 p-2 rounded-lg border border-slate-700">
                        <div class="text-[9px] text-slate-400 uppercase font-bold">進場現價</div>
                        <div class="font-black text-white text-lg" id="calc_entry">-</div>
                    </div>
                    <div class="bg-red-900/10 p-2 rounded-lg border border-red-900/50">
                        <div class="text-[9px] text-red-400 uppercase font-bold">嚴格止損 (-2.5 ATR)</div>
                        <div class="font-black text-red-400 text-lg" id="calc_sl">-</div>
                    </div>
                    <div class="bg-emerald-900/10 p-2 rounded-lg border border-emerald-900/50">
                        <div class="text-[9px] text-emerald-400 uppercase font-bold">目標止盈 (+4.5 ATR)</div>
                        <div class="font-black text-emerald-400 text-lg" id="calc_tp">-</div>
                    </div>
                    <div class="bg-amber-500/10 p-2 rounded-lg border border-amber-500/30 relative">
                        <div class="absolute -top-2 -right-2 bg-amber-500 text-black text-[8px] font-black px-1.5 py-0.5 rounded-full">1% Risk</div>
                        <div class="text-[9px] text-amber-500 uppercase font-bold">建議買入股數</div>
                        <div class="font-black text-amber-400 text-lg" id="calc_shares">-</div>
                    </div>
                    <div class="bg-slate-800/50 p-2 rounded-lg border border-slate-700">
                        <div class="text-[9px] text-slate-400 uppercase font-bold">總持倉成本 (佔比)</div>
                        <div class="font-black text-blue-300 text-lg" id="calc_cost">-</div>
                    </div>
                </div>
            </div>

            <div class="bg-slate-900 p-1 rounded-xl border border-slate-800 flex-1 relative shadow-lg" id="tv_chart_container">
                <div class="absolute inset-0 flex items-center justify-center text-slate-600 text-sm italic font-bold z-0 pointer-events-none">
                    請點擊左側信號以載入圖表
                </div>
            </div>
        </div>
    </main>

    <main id="tab-charts" class="hidden flex-1 overflow-y-auto bg-slate-900 rounded-xl border border-slate-800 p-6 z-10 flex flex-col gap-6 shadow-lg">
        <div class="flex justify-between items-center border-b border-slate-800 pb-2">
            <h2 class="text-2xl font-black text-white flex items-center gap-2">📈 歷史宏觀與持倉走勢 (最近 60 日)</h2>
            <div class="text-xs text-slate-500">底色反映當日大盤狀態 (紅=熊市防禦 / 黃=背馳警告 / 綠=牛市通行)</div>
        </div>
        <div class="grid grid-cols-1 gap-6">
            <div class="bg-slate-800/30 p-4 rounded-xl border border-slate-700">
                <h3 class="font-black text-slate-300 mb-2">🇺🇸 美股 (SPX)</h3>
                <div id="chart-us" class="h-[350px]"></div>
            </div>
            <div class="bg-slate-800/30 p-4 rounded-xl border border-slate-700">
                <h3 class="font-black text-slate-300 mb-2">🇯🇵 日股 (N225)</h3>
                <div id="chart-jp" class="h-[350px]"></div>
            </div>
            <div class="bg-slate-800/30 p-4 rounded-xl border border-slate-700">
                <div class="flex justify-between items-center mb-2">
                    <h3 class="font-black text-slate-300">🧩 策略持倉分佈 (Strategy Exposure)</h3>
                    <div class="text-[10px] text-slate-500">反映不同市況下的資金流向</div>
                </div>
                <div id="chart-exposure" class="h-[300px]"></div>
            </div>
            <div class="bg-slate-800/30 p-4 rounded-xl border border-slate-700">
                <div class="flex justify-between items-center mb-2">
                    <h3 class="font-black text-slate-300">💰 策略累積利潤走勢 (Cumulative P&L)</h3>
                    <div class="text-[10px] text-slate-500">各策略歷史淨利增長曲線</div>
                </div>
                <div id="chart-cumulative-pnl" class="h-[350px]"></div>
            </div>
        </div>
    </main>

    <main id="tab-journal" class="hidden flex-1 overflow-y-auto bg-slate-900 rounded-xl border border-slate-800 p-6 z-10 flex flex-col gap-6 shadow-lg">
        
        <div class="flex justify-between items-center border-b border-slate-800 pb-2">
            <h2 class="text-2xl font-black text-white flex items-center gap-2">📜 歷史交易結算與日誌</h2>
            <div class="text-xs text-slate-500">每單固定以 $10,000 基準結算盈虧</div>
        </div>

        <div class="grid grid-cols-4 gap-4" id="journal-stats"></div>

        <div class="bg-slate-800/30 rounded-xl border border-slate-700 p-3 flex gap-4 items-end shadow-lg">
            <div>
                <label class="text-[10px] text-slate-400 font-bold uppercase mb-1 block">🔍 策略篩選</label>
                <select id="filter-strat" onchange="renderJournal()" class="bg-slate-900 border border-slate-600 text-xs text-white px-3 py-1.5 rounded outline-none focus:border-fuchsia-500">
                    <option value="ALL">全部策略</option>
                    <option value="VCP">🏆 VCP 突破</option>
                    <option value="BB">💥 BB 擠壓</option>
                    <option value="缺口">⚡ 缺口動能</option>
                    <option value="超賣">📉 極度超賣</option>
                </select>
            </div>
            <div>
                <label class="text-[10px] text-slate-400 font-bold uppercase mb-1 block">📁 股票來源篩選</label>
                <select id="filter-source" onchange="renderJournal()" class="bg-slate-900 border border-slate-600 text-xs text-white px-3 py-1.5 rounded outline-none focus:border-fuchsia-500">
                    <option value="ALL">全部來源</option>
                    </select>
            </div>
        </div>

        <div class="bg-slate-800/30 rounded-xl border border-slate-700 p-4">
            <h3 class="font-black text-fuchsia-400 mb-3 flex items-center gap-2">🎯 按策略分析 (Strategy Performance)</h3>
            <div class="grid grid-cols-2 lg:grid-cols-4 gap-4" id="strategy-stats-container">
                </div>
        </div>

        <div class="bg-slate-800/30 rounded-xl border border-slate-700 p-4">
            <h3 class="font-black text-indigo-400 mb-3 flex items-center gap-2">📊 進場指標與勝率分析</h3>
            <div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
                <div class="bg-slate-900/50 rounded-lg border border-slate-700/50 overflow-hidden">
                    <div class="bg-slate-800 px-3 py-1 text-xs font-bold text-slate-300 border-b border-slate-700">📈 動能策略 (按 RS 分佈)</div>
                    <div class="overflow-x-auto">
                        <table class="w-full text-xs text-left whitespace-nowrap">
                            <thead class="text-slate-500 uppercase border-b border-slate-700">
                                <tr><th class="p-2">RS 區間</th><th class="p-2 text-center">單數</th><th class="p-2 text-center">勝率</th><th class="p-2 text-right">實現 P&L</th></tr>
                            </thead>
                            <tbody id="metric-rs-tbody"></tbody>
                        </table>
                    </div>
                </div>
                <div class="bg-slate-900/50 rounded-lg border border-slate-700/50 overflow-hidden">
                    <div class="bg-slate-800 px-3 py-1 text-xs font-bold text-slate-300 border-b border-slate-700">📉 撈底策略 (按 RSI 分佈)</div>
                    <div class="overflow-x-auto">
                        <table class="w-full text-xs text-left whitespace-nowrap">
                            <thead class="text-slate-500 uppercase border-b border-slate-700">
                                <tr><th class="p-2">RSI 區間</th><th class="p-2 text-center">單數</th><th class="p-2 text-center">勝率</th><th class="p-2 text-right">實現 P&L</th></tr>
                            </thead>
                            <tbody id="metric-rsi-tbody"></tbody>
                        </table>
                    </div>
                </div>
            </div>
        </div>

        <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <div class="bg-slate-800/30 rounded-xl border border-slate-700 p-4">
                <h3 class="font-black text-cyan-400 mb-3 flex items-center gap-2">📂 目前持倉 (Open Positions)</h3>
                <div class="overflow-x-auto">
                    <table class="w-full text-xs text-left whitespace-nowrap">
                        <thead class="text-slate-500 uppercase border-b border-slate-700 bg-slate-800/50">
                            <tr>
                                <th class="p-2">日期</th><th class="p-2">代號</th><th class="p-2">策略</th>
                                <th class="p-2 text-center">持倉狀態</th>
                                <th class="p-2">進場指標</th><th class="p-2">現時指標</th>
                                <th class="p-2">買入價</th><th class="p-2">止損</th><th class="p-2">止盈</th><th class="p-2">現價</th>
                                <th class="p-2 text-right">浮動 P&L</th><th class="p-2 text-right">回報 (%)</th>
                            </tr>
                        </thead>
                        <tbody id="journal-open-tbody"></tbody>
                    </table>
                </div>
            </div>

            <div class="bg-slate-800/30 rounded-xl border border-slate-700 p-4">
                <h3 class="font-black text-emerald-400 mb-3 flex items-center gap-2">📁 最近結案紀錄 (Closed Trades)</h3>
                <div class="overflow-x-auto">
                    <table class="w-full text-xs text-left whitespace-nowrap">
                        <thead class="text-slate-500 uppercase border-b border-slate-700 bg-slate-800/50">
                            <tr>
                                <th class="p-2">買入日期</th><th class="p-2">平倉日期</th><th class="p-2">代號</th>
                                <th class="p-2">策略</th><th class="p-2">狀態</th>
                                <th class="p-2">買入價</th><th class="p-2">賣出價</th>
                                <th class="p-2 text-right">實現 P&L</th><th class="p-2 text-right">回報 (%)</th>
                            </tr>
                        </thead>
                        <tbody id="journal-closed-tbody"></tbody>
                    </table>
                </div>
            </div>
        </div>
    </main>

    <script>
        const rawData = {js_payload_str};
        const tradeHistory = {trade_history_str};
        const chartData = {chart_data_str}; // 👈 加入呢行
        
        let chartsRendered = false; // 👈 確保圖表只渲染一次
        let currentSelectedTicker = null;
        let tvWidget = null;

        function switchTab(tabId) {{
            ['dashboard', 'journal', 'charts'].forEach(id => {{
            const tabEl = document.getElementById('tab-' + id);
            const btnEl = document.getElementById('tabBtn-' + id);
            if (tabEl) tabEl.classList.toggle('hidden', tabId !== id);
            if (btnEl) btnEl.className = tabId === id 
                ? 'bg-indigo-600 text-white px-4 py-1.5 rounded-md font-bold text-sm shadow-md transition' 
                : 'text-slate-400 hover:text-white hover:bg-slate-800 px-4 py-1.5 rounded-md font-bold text-sm transition';
        }});

        if (tabId === 'journal') renderJournal();
        if (tabId === 'charts' && !chartsRendered) renderCharts();
        }}

        function renderCharts() {{
            const dates = chartData.map(d => d.date);
            
            const createChartOptions = (market) => {{
                // 讀取所有數據欄位
                const idxBreadthData = chartData.map(d => d[market + '_idx_breadth']);
                const totBreadthData = chartData.map(d => d[market + '_tot_breadth']);
                const profitData = chartData.map(d => d[market + '_open_profit']);
                const lossData = chartData.map(d => d[market + '_open_loss']);
                
                // 動態生成底色區塊 (Annotations)
                const annotations = chartData.map((d, i) => ({{
                    x: d.date,
                    x2: i < chartData.length - 1 ? chartData[i+1].date : d.date,
                    fillColor: d[market + '_color'],
                    opacity: 0.15,
                    strokeDashArray: 0,
                    borderWidth: 0
                }}));

                return {{
                    series: [
                        {{ name: '大盤市寬 (>50MA)', type: 'line', data: idxBreadthData }},
                        {{ name: '全市市寬 (>50MA)', type: 'line', data: totBreadthData }},
                        // 👇 新增：Profit 與 Loss 數據，並將 P&L 狀態綁定為 Column 類型
                        {{ name: '賺錢持倉 (Profit)', type: 'column', data: profitData }},
                        {{ name: '蝕本持倉 (Loss)', type: 'column', data: lossData }}
                    ],
                    chart: {{ 
                        height: 350, 
                        type: 'line', 
                        // 👇 開啟 Stacked (堆疊) 模式！
                        stacked: true,
                        toolbar: {{ show: false }}, 
                        background: 'transparent' 
                    }},
                    stroke: {{ 
                        width: [3, 2, 0, 0], // 前兩條是線，後兩條是柱狀圖的邊框
                        curve: 'smooth', 
                        dashArray: [0, 4, 0, 0] // 大盤實線，全市虛線
                    }},
                    // 👇 定義顏色：[大盤線, 全市虛線, Profit柱, Loss柱]
                    colors: ['#f59e0b', '#06b6d4', '#22c55e', '#ef4444'], // 橙色, 湖水綠, 綠色, 紅色
                    annotations: {{ 
                        position: 'back', 
                        xaxis: annotations 
                    }},
                    xaxis: {{ categories: dates, labels: {{ style: {{ colors: '#94a3b8' }} }}, tickAmount: 10 }},
                    yaxis: [
                        {{ 
                            seriesName: '大盤市寬 (>50MA)', 
                            title: {{ text: '市寬 (%)', style: {{ color: '#94a3b8' }} }}, 
                            labels: {{ style: {{ colors: '#94a3b8' }} }}, 
                            min: 0, max: 100 
                        }},
                        {{ seriesName: '大盤市寬 (>50MA)', show: false }}, // 共用市寬Y軸
                        {{ 
                            opposite: true, 
                            seriesName: '賺錢持倉 (Profit)', 
                            title: {{ text: '持倉數量 (隻)', style: {{ color: '#8b5cf6' }} }}, 
                            labels: {{ style: {{ colors: '#8b5cf6' }} }} 
                        }},
                        {{ seriesName: '賺錢持倉 (Profit)', show: false }} // 共用持倉Y軸
                    ],
                    plotOptions: {{
                        bar: {{
                            // 👇 設定柱狀圖圓角 (只讓最頂部的 Profit 柱有圓角，中間的 Loss 是平的)
                            borderRadius: 4,
                            borderRadiusApplication: 'around',
                            borderRadiusWhenStacked: 'last'
                        }}
                    }},
                    theme: {{ mode: 'dark' }},
                    legend: {{ position: 'top' }},
                    dataLabels: {{ enabled: false }},
                    grid: {{ borderColor: '#334155', strokeDashArray: 3 }}
                }};
            }};
            
            new ApexCharts(document.querySelector("#chart-us"), createChartOptions('us')).render();
            new ApexCharts(document.querySelector("#chart-jp"), createChartOptions('jp')).render();

            // 👇 新增：策略持倉分佈圖 (Stacked Area Chart)
            const exposureOptions = {{
                series: [
                    {{ name: '🏆 VCP 突破', data: chartData.map(d => d.strat_vcp) }},
                    {{ name: '💥 BB 擠壓', data: chartData.map(d => d.strat_bb) }},
                    {{ name: '⚡ 缺口動能', data: chartData.map(d => d.strat_gap) }},
                    {{ name: '📉 極度超賣', data: chartData.map(d => d.strat_oversold) }}
                ],
                chart: {{
                    type: 'area',
                    height: 300,
                    stacked: true, // 使用堆疊面積圖
                    toolbar: {{ show: false }},
                    background: 'transparent'
                }},
                colors: ['#a855f7', '#ec4899', '#3b82f6', '#14b8a6'], // 紫, 粉紅, 藍, 綠
                dataLabels: {{ enabled: false }},
                stroke: {{ curve: 'smooth', width: 2 }},
                fill: {{
                    type: 'gradient',
                    gradient: {{ opacityFrom: 0.6, opacityTo: 0.1 }}
                }},
                legend: {{ position: 'top', labels: {{ colors: '#cbd5e1' }} }},
                xaxis: {{
                    categories: dates,
                    labels: {{ style: {{ colors: '#94a3b8' }} }},
                    tickAmount: 10
                }},
                yaxis: {{
                    title: {{ text: '持倉數量 (隻)', style: {{ color: '#94a3b8' }} }},
                    labels: {{ style: {{ colors: '#94a3b8' }} }},
                    min: 0,
                    forceNiceScale: true
                }},
                theme: {{ mode: 'dark' }},
                grid: {{ borderColor: '#334155', strokeDashArray: 3 }}
            }};
            
            new ApexCharts(document.querySelector("#chart-exposure"), exposureOptions).render();

            // ==========================================
            // 👇 貼喺呢度：新增嘅策略累積 P&L 折線圖 (雙括號安全版)
            // ==========================================
            const pnlOptions = {{
                series: [
                    {{ name: '🏆 VCP 突破', data: chartData.map(d => d.pnl_vcp) }},
                    {{ name: '💥 BB 擠壓', data: chartData.map(d => d.pnl_bb) }},
                    {{ name: '⚡ 缺口動能', data: chartData.map(d => d.pnl_gap) }},
                    {{ name: '📉 極度超賣', data: chartData.map(d => d.pnl_oversold) }}
                ],
                chart: {{
                    type: 'line',
                    height: 350,
                    toolbar: {{ show: false }},
                    background: 'transparent'
                }},
                colors: ['#a855f7', '#ec4899', '#3b82f6', '#14b8a6'],
                dataLabels: {{ enabled: false }},
                stroke: {{ curve: 'smooth', width: 3 }},
                legend: {{ position: 'top', labels: {{ colors: '#cbd5e1' }} }},
                xaxis: {{
                    categories: dates,
                    labels: {{ style: {{ colors: '#94a3b8' }} }},
                    tickAmount: 10
                }},
                yaxis: {{
                    title: {{ text: '累積利潤 ($)', style: {{ color: '#94a3b8' }} }},
                    labels: {{ 
                        style: {{ colors: '#94a3b8' }},
                        formatter: (value) => "$" + value.toLocaleString() 
                    }}
                }},
                theme: {{ mode: 'dark' }},
                grid: {{ borderColor: '#334155', strokeDashArray: 3 }},
                tooltip: {{
                    y: {{ formatter: function (val) {{ return "$" + val.toLocaleString() }} }}
                }}
            }};
            
            new ApexCharts(document.querySelector("#chart-cumulative-pnl"), pnlOptions).render(); 
            // ==========================================

            chartsRendered = true;
        }}

        function loadContent(ticker) {{
            currentSelectedTicker = ticker;
            const isJp = ticker.endsWith('.T');
            const tvSymbol = isJp ? 'TSE:' + ticker.replace('.T', '') : ticker;

            const tvLink = document.getElementById('tv_out_link');
            tvLink.href = `https://www.tradingview.com/chart/?symbol=${{tvSymbol}}`;
            tvLink.classList.remove('hidden');

            if (tvWidget) {{ tvWidget.remove(); }}
            tvWidget = new TradingView.widget({{
                "autosize": true, "symbol": tvSymbol, "interval": "D", "timezone": "Etc/UTC",
                "theme": "dark", "style": "1", "locale": "en", "container_id": "tv_chart_container"
            }});

            updateCalculator();
        }}

        function updateCalculator() {{
            if (!currentSelectedTicker) return;
            const data = rawData.find(d => d.ticker === currentSelectedTicker);
            if (!data) return;

            const isJp = data.ticker.endsWith('.T');
            const unit = isJp ? '¥' : '$';

            document.getElementById('calc_ticker_name').innerText = data.ticker + " (" + data.tag + ")";
            const accountSize = parseFloat(document.getElementById('acc_size').value) || 10000;
            const riskAmount = accountSize * {MAX_ACCOUNT_RISK_PCT};
            
            let shares = Math.floor(riskAmount / data.risk_per_share);
            if (shares <= 0) shares = 0;
            
            const totalCost = shares * data.curr_price;
            const actualPosPct = (accountSize > 0) ? (totalCost / accountSize * 100).toFixed(1) : 0;
            
            document.getElementById('calc_entry').innerText = unit + data.curr_price.toFixed(2);
            document.getElementById('calc_sl').innerText = unit + data.sl_price.toFixed(2);
            document.getElementById('calc_tp').innerText = unit + data.tp_price.toFixed(2);
            document.getElementById('calc_shares').innerText = shares;
            document.getElementById('calc_cost').innerText = unit + totalCost.toLocaleString(undefined, {{maximumFractionDigits: 0}}) + " (" + actualPosPct + "%)";
        }}

        // 👇 新增全域變數 (控制排序與過濾)
        let currentSort = 'date';
        let isAsc = false;
        let sourcesLoaded = false;

        function sortData(col) {{
            if (currentSort === col) {{ isAsc = !isAsc; }} 
            else {{ currentSort = col; isAsc = false; }}
            renderJournal();
        }}

        // 數值格式化工具 (處理市值 Market Cap)
        function formatMcap(val) {{
            if (!val) return '-';
            if (val >= 1e12) return (val / 1e12).toFixed(1) + 'T';
            if (val >= 1e9) return (val / 1e9).toFixed(1) + 'B';
            if (val >= 1e6) return (val / 1e6).toFixed(1) + 'M';
            return val.toLocaleString();
        }}

        // 🌟 終極整合版 renderJournal
        function renderJournal() {{
            const openTbody = document.getElementById('journal-open-tbody');
            const closedTbody = document.getElementById('journal-closed-tbody');
            const statsContainer = document.getElementById('journal-stats');

            // 1️⃣ 讀取 Filter 數值 (防呆設計：如果 HTML 未加 Filter UI，就預設 ALL)
            const stratFilter = document.getElementById('filter-strat') ? document.getElementById('filter-strat').value : 'ALL';
            const sourceFilter = document.getElementById('filter-source') ? document.getElementById('filter-source').value : 'ALL';

            // 動態載入來源 Filter 選項 (只執行一次)
            if (!sourcesLoaded && document.getElementById('filter-source')) {{
                let allSources = new Set();
                tradeHistory.forEach(t => {{ if(t.sources) t.sources.forEach(s => allSources.add(s)); }});
                const sourceSelect = document.getElementById('filter-source');
                allSources.forEach(s => {{
                    const opt = document.createElement('option');
                    opt.value = s; opt.innerText = s;
                    sourceSelect.appendChild(opt);
                }});
                sourcesLoaded = true;
            }}

            // 2️⃣ 過濾邏輯
            let filteredHist = tradeHistory.filter(t => {{
                let matchStrat = stratFilter === 'ALL' || (t.tag && t.tag.includes(stratFilter));
                let matchSource = sourceFilter === 'ALL' || (t.sources && t.sources.includes(sourceFilter));
                return matchStrat && matchSource;
            }});

            // 3️⃣ 排序邏輯
            filteredHist.sort((a, b) => {{
                let valA = a[currentSort]; let valB = b[currentSort];
                // 特殊處理：浮動盈虧排序
                if (currentSort === 'pnl') {{
                    valA = a.status === 'OPEN' ? (a.last_px - a.px)/a.px : (a.last_px - a.px);
                    valB = b.status === 'OPEN' ? (b.last_px - b.px)/b.px : (b.last_px - b.px);
                }}
                if (valA < valB) return isAsc ? -1 : 1;
                if (valA > valB) return isAsc ? 1 : -1;
                return 0;
            }});

            const opens = filteredHist.filter(t => t.status === 'OPEN');
            const closeds = filteredHist.filter(t => t.status !== 'OPEN');

            // ==========================================
            // 📊 頂部 4 個總計數據方塊 (已經升級 75/25 會計)
            // ==========================================
            let totalClosedPnl = 0, wins = 0, totalOpenPnl = 0;
            
            closeds.forEach(t => {{
                totalClosedPnl += (10000 / t.px) * (t.last_px - t.px);
                if (t.status.includes('✅')) wins++;
            }});
            
            opens.forEach(t => {{
                let buy_px = t.px;
                let last_px = t.last_px;
                // 🌟 混合會計公式：75% 已鎖定，25% 隨現價浮動
                let tp1 = t.tp1_price || (buy_px + (buy_px - (t.initial_sl || buy_px))*2); 
                let pnl = t.partial_tp_hit ? 
                    ((7500 / buy_px) * (tp1 - buy_px) + (2500 / buy_px) * (last_px - buy_px)) :
                    (10000 / buy_px) * (last_px - buy_px);
                totalOpenPnl += pnl;
            }});

            const winRate = closeds.length > 0 ? ((wins / closeds.length) * 100).toFixed(1) : 0;
            const closedPct = closeds.length > 0 ? ((totalClosedPnl / (closeds.length * 10000)) * 100).toFixed(2) : "0.00";
            const openPct = opens.length > 0 ? ((totalOpenPnl / (opens.length * 10000)) * 100).toFixed(2) : "0.00";

            const closedSign = totalClosedPnl >= 0 ? '+' : '';
            const openSign = totalOpenPnl >= 0 ? '+' : '';
            const closedColor = totalClosedPnl >= 0 ? 'text-emerald-400' : 'text-red-400';
            const openColor = totalOpenPnl >= 0 ? 'text-emerald-400' : 'text-red-400';

            statsContainer.innerHTML = `
                <div class="bg-slate-800/50 p-4 rounded-xl border border-slate-700 text-center">
                    <div class="text-[10px] text-slate-400 uppercase font-bold mb-1">已結案總利潤</div>
                    <div class="text-2xl font-black ${{closedColor}}">${{closedSign}}$${{totalClosedPnl.toFixed(0)}} <span class="text-sm">(${{closedSign}}${{closedPct}}%)</span></div>
                </div>
                <div class="bg-slate-800/50 p-4 rounded-xl border border-slate-700 text-center">
                    <div class="text-[10px] text-slate-400 uppercase font-bold mb-1">歷史勝率</div>
                    <div class="text-2xl font-black text-white">${{winRate}}%</div>
                    <div class="text-[9px] text-slate-500 mt-1">${{wins}} 贏 / ${{closeds.length - wins}} 輸</div>
                </div>
                <div class="bg-slate-800/50 p-4 rounded-xl border border-slate-700 text-center">
                    <div class="text-[10px] text-slate-400 uppercase font-bold mb-1">目前未平倉</div>
                    <div class="text-2xl font-black text-cyan-400">${{opens.length}} 隻</div>
                </div>
                <div class="bg-slate-800/50 p-4 rounded-xl border border-slate-700 text-center">
                    <div class="text-[10px] text-slate-400 uppercase font-bold mb-1">總浮動盈虧</div>
                    <div class="text-2xl font-black ${{openColor}}">${{openSign}}$${{totalOpenPnl.toFixed(0)}} <span class="text-sm">(${{openSign}}${{openPct}}%)</span></div>
                </div>
            `;

            // ==========================================
            // 🎯 1. 生成策略卡片
            // ==========================================
            const strategyStats = {{}};
            closeds.forEach(t => {{
                const strat = t.tag || '未分類';
                if (!strategyStats[strat]) {{
                    strategyStats[strat] = {{ trades: 0, wins: 0, pnl: 0, deployed: 0 }};
                }}
                strategyStats[strat].trades += 1;
                if (t.status.includes('✅')) strategyStats[strat].wins += 1;
                strategyStats[strat].pnl += (10000 / t.px) * (t.last_px - t.px);
                strategyStats[strat].deployed += 10000;
            }});

            const strategyHtml = Object.keys(strategyStats).map(strat => {{
                const stats = strategyStats[strat];
                const stratWinRate = ((stats.wins / stats.trades) * 100).toFixed(1);
                const pColor = stats.pnl >= 0 ? 'text-emerald-400' : 'text-red-400';
                const pSign = stats.pnl >= 0 ? '+' : '';
                return `
                <div class="bg-slate-900/50 p-3 rounded-lg border border-slate-700/50 hover:border-fuchsia-500/50 transition">
                    <div class="text-xs font-black text-white mb-2 uppercase px-1 bg-slate-800 inline-block rounded">${{strat}}</div>
                    <div class="flex justify-between text-[10px] text-slate-400 mb-1">
                        <span>勝率 (${{stats.wins}}/${{stats.trades}})</span><span class="font-bold text-white">${{stratWinRate}}%</span>
                    </div>
                    <div class="flex justify-between text-[10px] text-slate-400 mb-1">
                        <span>已動用資金</span><span class="font-bold">$${{stats.deployed.toLocaleString()}}</span>
                    </div>
                    <div class="flex justify-between text-[10px] text-slate-400 mt-2 pt-2 border-t border-slate-700">
                        <span>實現利潤</span><span class="font-black ${{pColor}}">${{pSign}}$${{stats.pnl.toFixed(0)}}</span>
                    </div>
                </div>`;
            }}).join('');
            document.getElementById('strategy-stats-container').innerHTML = strategyHtml || '<div class="text-xs text-slate-500 italic p-2">暫無策略數據</div>';

            // ==========================================
            // 📈 2. 按進場指標 (RS / RSI) 分組統計
            // ==========================================
            const metricStats = {{
                rs: {{ '95-99 (極強)': {{ trades: 0, wins: 0, pnl: 0 }}, '90-94 (強勢)': {{ trades: 0, wins: 0, pnl: 0 }}, '80-89 (中等)': {{ trades: 0, wins: 0, pnl: 0 }}, '< 80 (較弱)': {{ trades: 0, wins: 0, pnl: 0 }} }},
                rsi: {{ '< 20 (極度超賣)': {{ trades: 0, wins: 0, pnl: 0 }}, '20-25 (嚴重超賣)': {{ trades: 0, wins: 0, pnl: 0 }}, '> 25 (輕微超賣)': {{ trades: 0, wins: 0, pnl: 0 }} }}
            }};

            closeds.forEach(t => {{
                const isWin = t.status.includes('✅');
                const tradePnl = (10000 / t.px) * (t.last_px - t.px);
                
                if (t.entry_metric) {{
                    if (t.entry_metric.startsWith('RS:')) {{
                        const rsVal = parseInt(t.entry_metric.replace('RS:', '').trim());
                        let bucket = '< 80 (較弱)';
                        if (rsVal >= 95) bucket = '95-99 (極強)';
                        else if (rsVal >= 90) bucket = '90-94 (強勢)';
                        else if (rsVal >= 80) bucket = '80-89 (中等)';
                        
                        metricStats.rs[bucket].trades++;
                        if (isWin) metricStats.rs[bucket].wins++;
                        metricStats.rs[bucket].pnl += tradePnl;
                    }} else if (t.entry_metric.startsWith('RSI:')) {{
                        const rsiVal = parseInt(t.entry_metric.replace('RSI:', '').trim());
                        let bucket = '> 25 (輕微超賣)';
                        if (rsiVal < 20) bucket = '< 20 (極度超賣)';
                        else if (rsiVal <= 25) bucket = '20-25 (嚴重超賣)';
                        
                        metricStats.rsi[bucket].trades++;
                        if (isWin) metricStats.rsi[bucket].wins++;
                        metricStats.rsi[bucket].pnl += tradePnl;
                    }}
                }}
            }});

            const renderMetricRows = (statsObj) => {{
                return Object.keys(statsObj).map(key => {{
                    const s = statsObj[key];
                    if (s.trades === 0) return `<tr><td class="p-2 text-slate-500">${{key}}</td><td colspan="3" class="p-2 text-center text-slate-600 text-[10px]">無數據</td></tr>`;
                    const winRate = ((s.wins / s.trades) * 100).toFixed(1);
                    const pColor = s.pnl >= 0 ? 'text-emerald-400' : 'text-red-400';
                    const pSign = s.pnl >= 0 ? '+' : '';
                    return `
                    <tr class="border-b border-slate-700/50 hover:bg-slate-800 transition">
                        <td class="p-2 font-bold text-white">${{key}}</td>
                        <td class="p-2 text-center">${{s.trades}}</td>
                        <td class="p-2 text-center font-bold text-cyan-400">${{winRate}}%</td>
                        <td class="p-2 text-right font-black font-mono ${{pColor}}">${{pSign}}$${{s.pnl.toFixed(0)}}</td>
                    </tr>`;
                }}).join('');
            }};

            const rsTbody = document.getElementById('metric-rs-tbody');
            const rsiTbody = document.getElementById('metric-rsi-tbody');
            if(rsTbody) rsTbody.innerHTML = renderMetricRows(metricStats.rs);
            if(rsiTbody) rsiTbody.innerHTML = renderMetricRows(metricStats.rsi);

            // ==========================================
            // 📂 3. 渲染 Open Positions (加入過濾/排序/板塊/市值/75%)
            // ==========================================
            const openThead = openTbody.parentElement.querySelector('thead');
            openThead.innerHTML = `
                <tr>
                    <th class="p-2 cursor-pointer hover:text-white" onclick="sortData('date')">日期 ↕</th>
                    <th class="p-2 cursor-pointer hover:text-white" onclick="sortData('tk')">代號 ↕</th>
                    <th class="p-2">策略 & 來源</th>
                    <th class="p-2 cursor-pointer hover:text-white" onclick="sortData('sector')">板塊 (Sector) ↕</th>
                    <th class="p-2 cursor-pointer hover:text-white text-right" onclick="sortData('mcap')">市值 ↕</th>
                    <th class="p-2 text-center">持倉狀態</th>
                    <th class="p-2">進場指標</th>
                    <th class="p-2 text-right cursor-pointer hover:text-white" onclick="sortData('pnl')">回報 (%) ↕</th>
                </tr>
            `;

            openTbody.innerHTML = opens.length === 0 ? '<tr><td colspan="8" class="p-4 text-center text-slate-500">目前無符合條件持倉</td></tr>' : opens.map(t => {{
                let pnl = 0;
                let buy_px = t.px;
                let last_px = t.last_px;
                
                // 🌟 75/25 混合會計公式
                if (t.partial_tp_hit) {{
                    let tp1_price = t.tp1_price || (buy_px + (buy_px - (t.initial_sl || buy_px)) * 2);
                    let pnl_closed = (7500 / buy_px) * (tp1_price - buy_px);
                    let pnl_floating = (2500 / buy_px) * (last_px - buy_px);
                    pnl = pnl_closed + pnl_floating; 
                }} else {{
                    pnl = (10000 / buy_px) * (last_px - buy_px);
                }}
                
                let pnlPct = ((last_px - buy_px) / buy_px * 100).toFixed(2);
                const pColor = pnl >= 0 ? 'text-emerald-400' : 'text-red-400';
                
                // 動態生成 Source 標籤
                let sourceBadges = (t.sources || []).map(s => `<span class="text-[8px] bg-blue-500/20 text-blue-300 px-1 rounded ml-1 border border-blue-500/30">${{s}}</span>`).join('');

                return `
                <tr class="border-b border-slate-700/50 hover:bg-slate-800 transition">
                    <td class="p-2">${{t.date}}</td>
                    <td class="p-2 font-bold text-white">${{t.tk}}</td>
                    <td class="p-2 flex flex-wrap items-center gap-1 mt-1"><span class="text-[9px] bg-slate-700 px-1 rounded">${{t.tag || 'N/A'}}</span>${{sourceBadges}}</td>
                    <td class="p-2 text-[10px] text-slate-400 truncate max-w-[100px]">${{t.sector || 'N/A'}}</td>
                    <td class="p-2 text-[10px] text-slate-400 font-mono text-right">${{formatMcap(t.mcap)}}</td>
                    <td class="p-2 text-center">
                        ${{t.partial_tp_hit 
                            ? '<span class="text-amber-400 bg-amber-400/10 px-2 py-0.5 rounded border border-amber-500/20 text-[10px] font-black">🎯 75% 已止盈 (25% 放飛)</span>' 
                            : '<span class="text-cyan-400 bg-cyan-400/10 px-2 py-0.5 rounded border border-cyan-500/20 text-[10px] font-black">⏳ 100% 正常持倉中</span>'
                        }}
                    </td>
                    <td class="p-2 text-[10px] font-mono text-indigo-300">${{t.entry_metric || '-'}}</td>
                    <td class="p-2 text-right font-black font-mono ${{pColor}}">${{pnl >= 0 ? '+' : ''}}${{pnlPct}}%<br><span class="text-[9px] font-normal opacity-70">${{pnl >= 0 ? '+' : ''}}$${{pnl.toFixed(0)}}</span></td>
                </tr>`;
            }}).join('');

            // ==========================================
            // 📁 4. 渲染 Closed Trades (維持原本顯示)
            // ==========================================
            const closedThead = document.querySelector('#journal-closed-tbody').parentElement.querySelector('thead');
            if(closedThead) {{
                closedThead.innerHTML = `
                    <tr>
                        <th class="p-2">買入日期</th><th class="p-2">平倉日期</th><th class="p-2">代號</th>
                        <th class="p-2">策略</th><th class="p-2 text-indigo-400">進場指標</th><th class="p-2">狀態</th>
                        <th class="p-2">買入價</th><th class="p-2">賣出價</th>
                        <th class="p-2 text-right">實現 P&L</th><th class="p-2 text-right">回報 (%)</th>
                    </tr>
                `;
            }}

            closedTbody.innerHTML = closeds.length === 0 ? '<tr><td colspan="10" class="p-4 text-center text-slate-500">無結案紀錄</td></tr>' : closeds.slice(0,50).map(t => {{
                const pnl = (10000 / t.px) * (t.last_px - t.px);
                const pnlPct = ((t.last_px - t.px) / t.px * 100).toFixed(2);
                const isWin = t.status.includes('✅');
                const pColor = isWin ? 'text-emerald-400' : 'text-red-400';
                const isJp = t.tk.endsWith('.T');
                const unit = isJp ? '¥' : '$';

                return `
                <tr class="border-b border-slate-700/50 hover:bg-slate-800 transition">
                    <td class="p-2 text-slate-400">${{t.date}}</td>
                    <td class="p-2">${{t.close_date || t.date}}</td>
                    <td class="p-2 font-bold text-white">${{t.tk}}</td>
                    <td class="p-2 text-[10px] text-slate-400">${{t.tag || 'N/A'}}</td>
                    <td class="p-2 text-[10px] font-mono text-indigo-300">${{t.entry_metric || '-'}}</td>
                    <td class="p-2">${{(() => {{
                        if (t.status.includes("MAX TP")) return '<span class="text-fuchsia-400 font-bold">🏆 終極止賺</span>';
                        if (t.status.includes("TRAIL EXIT")) return '<span class="text-blue-400 font-bold">🚀 放飛平倉</span>';
                        if (t.status.includes("✅")) return '<span class="text-emerald-400 font-bold">🎯 止盈</span>';
                        return '<span class="text-red-400 font-bold">🛑 止損</span>';
                    }})()}}</td>
                    <td class="p-2">${{unit}}${{t.px}}</td>
                    <td class="p-2 text-white font-bold">${{unit}}${{t.last_px}}</td>
                    <td class="p-2 text-right font-black font-mono ${{pColor}}">${{pnl >= 0 ? '+' : ''}}${{pnl.toFixed(2)}}</td>
                    <td class="p-2 text-right font-black font-mono ${{pColor}}">${{pnl >= 0 ? '+' : ''}}${{pnlPct}}%</td>
                </tr>`;
            }}).join('');
        }}
    </script>
</body>
</html>"""

with open(os.path.join(OUTPUT_DIR, "index.html"), "w", encoding="utf-8") as f: f.write(html)
print(f"\n🎉 UAT 時光機版建置完成！")
