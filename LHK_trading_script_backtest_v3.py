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
_hs = os.environ.get("HISTORY_SUFFIX", "")
HISTORY_FILE = os.path.join(OUTPUT_DIR, f"uat_trade_history{_hs}.json")

# =============================================================================
# 核心策略與時光機參數 
# =============================================================================
LOOKBACK_YEARS = 10
PQR_SWING_MIN = 75
FTD_VALID_DAYS = 20
MAX_ACCOUNT_RISK_PCT = 0.01 # 每單最多虧損總資金的 1%

TICKETSIZE = 10000
PARTIAL_TP_PCT = 0.33
PARTIAL_TP_R   = 2.0

COMMISSION_PCT = 0.0005      # 單邊 0.05%
SLIPPAGE_PCT   = 0.0010      # 突破日尾市買入，單邊 0.1%
ROUND_TRIP_COST = (COMMISSION_PCT + SLIPPAGE_PCT) * 2   # 0.3%

USE_MAX_TP = False 
MAX_TP_ATR = 4.5

ATR_TRAIL_MULT = 3.0 

SWING_TIME_STOP_DAYS = 15
SWING_TIME_STOP_MIN_R = 1.0

BENCH = ['SPY', '^VIX', '^N225', 'JPY=X']

# =========================================================================
# 📊 百分位門檻（取代絕對數值，令策略自動適應市況變化）
# =========================================================================
USE_PCT_MODE = os.environ.get("PCT_MODE", "1") == "1"

PCT_LOOKBACK   = 252    # 自我參照窗口（約一年交易日）
PCT_LIQUIDITY  = 0.60   # 流動性：只取當日成交額最高嘅 40%
PCT_ELITE_LIQ  = 0.85   # 缺口策略專用：最高嘅 15%
PCT_REC_VOLAT  = 0.30   # 近期波幅：處於自己過去一年最靜嘅 30%
PCT_BASE_DD    = 0.50   # 底部深度：淺過自己過去一年中位數
PCT_GAP_ATR    = 0.8    # 缺口：至少 0.8 倍 ATR（取代固定 3%）
PCT_DMA50_OS   = 0.10   # 超賣：偏離度處於過去一年最極端 10%

print(f"📊 門檻模式：{'百分位 (自適應)' if USE_PCT_MODE else '絕對值 (舊版)'}")

IS_END  = '2022-12-31'    # 2019-01 → 2022-12（4 年，含 2020 崩盤 + 2022 熊市）
OOS_END = '2025-03-31'    # 2023-01 → 2025-03（2.25 年）
                          # 2025-04 → 至今（1.4 年）

# 👇 時光機設定：從 GitHub Actions 讀取要回溯幾多日 (預設回溯 10 日)
# 假設你的腳本內新增一個模式
START_DAYS = 2780
END_DAYS   = int(os.environ.get("UAT_END_DAYS", "0"))

raw_days = os.environ.get("UAT_DAYS_AGO", "10")
SIMULATE_DAYS_AGO = int(raw_days)

IS_BACKTEST  = True                              # 👈 寫死
IS_FINAL_RUN = (SIMULATE_DAYS_AGO <= END_DAYS)

# =========================================================================
# 🎚️ 出場方案（用環境變數切換，方便做 A/B/C 對照測試）
# =========================================================================
EXIT_PROFILES = {
    # A = 你原本嘅設定
    'A_current':    {'tp1_r': 1.5, 'tp1_pct': 0.75, 'use_max_tp': True,  'trail_mult': 3.0},
    # B = 建議方案：細注止盈 + 無上限 + Chandelier
    'B_runner':     {'tp1_r': 2.0, 'tp1_pct': 0.33, 'use_max_tp': False, 'trail_mult': 3.0},
}
EXIT_MODE = os.environ.get("EXIT_MODE", "B_runner")
_CFG = EXIT_PROFILES[EXIT_MODE]

PARTIAL_TP_R   = _CFG['tp1_r']
PARTIAL_TP_PCT = _CFG['tp1_pct']
USE_MAX_TP     = _CFG['use_max_tp']
ATR_TRAIL_MULT = _CFG['trail_mult']
MAX_TP_ATR     = 4.5

print(f"🎚️ 出場方案：{EXIT_MODE} | TP1 {PARTIAL_TP_R}R 平 {PARTIAL_TP_PCT*100:.0f}% | MaxTP={USE_MAX_TP}")

# =============================================================================
# 功能函數區
# =============================================================================
INFO_CACHE_FILE = os.path.join(OUTPUT_DIR, "stock_info_cache.json")

# 👇 開機時讀一次入記憶體（唔好喺 function 入面每次讀）
STOCK_INFO_CACHE = {}
if os.path.exists(INFO_CACHE_FILE):
    try:
        with open(INFO_CACHE_FILE, "r", encoding="utf-8") as f:
            STOCK_INFO_CACHE = json.load(f)
        print(f"⚡ 由 cache 讀取 {len(STOCK_INFO_CACHE)} 隻股票基本資料")
    except Exception:
        STOCK_INFO_CACHE = {}

def get_stock_info(tk):
    if tk in STOCK_INFO_CACHE:
        return STOCK_INFO_CACHE[tk]
    try:
        info = yf.Ticker(tk).info
        data = {
            'sector': info.get('sector', 'N/A'),
            'mcap': info.get('marketCap', 0),
            'info_asof': datetime.date.today().isoformat()
        }
    except Exception:
        data = {'sector': 'N/A', 'mcap': 0, 'info_asof': datetime.date.today().isoformat()}
    STOCK_INFO_CACHE[tk] = data      # 👈 只係「改內容」，唔係賦值，所以唔會變局部變數
    return data

def send_discord_alert(ticker, strategy_name, price, sl, tp, embed_color, sources, tp1_price=None, features=None):
    if IS_BACKTEST and not IS_FINAL_RUN: return
    if not DISCORD_WEBHOOK_URL: return
    unit = "¥" if ticker.endswith(".T") else "$"
    
    if sources:
        clean_sources = [f"#{s.replace('&', '').replace(' ', '_')}" for s in sources]
        source_str = " ".join(clean_sources)
    else:
        source_str = "#動態掃描"
        
    color = embed_color

    type_str = "**波段建倉 (Swing)**" if strategy_name in ["🏆 VCP 突破", "💥 BB 擠壓"] else "**短線游擊 (Short Term)**"
    trail_str = "跌穿 5日新低" if "短線" in type_str else "跌穿 20日新低"
    tp1_val = tp1_price if tp1_price else tp
    
    max_tp_line = f"3️⃣ **Max TP:** `{unit}{tp}` (全數強制平倉)" if tp else "3️⃣ **Max TP:** 無上限，由 Trailing 決定"
    action_text = (f"{type_str}\n"
                   f"1️⃣ **TP1:** `{unit}{tp1_val}` (平倉 {int(PARTIAL_TP_PCT*100)}% 並保本)\n"
                   f"2️⃣ **TP2 (Trail):** {trail_str}清倉\n"
                   f"{max_tp_line}")
    
    # 👇 新增：動態生成 Discord 專用嘅機構特徵字串
    feature_str = ""
    if features:
        badges = []
        if features.get('mss'): badges.append("🛡️ MSS")
        if features.get('smc'): badges.append("🐋 SMC")
        if features.get('amd'): badges.append("🔄 AMD")
        score = len(badges)
        rsi_val = features.get('ml_rsi', '-')
        badge_text = " | ".join(badges) if badges else "無特殊共振"
        feature_str = f"\n🧬 共振度: **{score}/3**\n🔖 特徵: {badge_text} | 🧠 RSI: {rsi_val}"
    
    embed_data = {
        "title": f"🚨 系統異動觸發: {ticker}",
        "description": f"**{strategy_name}** 條件已達成！\n🔍 來源: **{source_str}**{feature_str}", # 👈 塞入 description
        "color": color,
        "fields": [
            {"name": "💵 當前現價", "value": f"{unit}{price}", "inline": True},
            {"name": "🛑 初始止損", "value": f"{unit}{sl}", "inline": True},
            {"name": "⚙️ 離場策略", "value": action_text, "inline": False}
        ],
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
print(f"⏳ [1-2/8] 正在抓取數據與啟動時光機 (回溯 {SIMULATE_DAYS_AGO} 日)...")

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
        ("https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies", "NDX100_科技")]

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
    except Exception as e:
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

    if IS_BACKTEST:
        print("🕰️ [時光機] 已停用所有即時排行榜 source，只使用指數成分股")
    else:
        # ---------------------------------------------------------
        # 2. 獲取美股異動黑馬 (改用 Yahoo Finance US 避開 Cloudflare)
        # ---------------------------------------------------------
        yahoo_us_urls = [
            ("https://finance.yahoo.com/gainers", "Yahoo升幅"),
            ("https://finance.yahoo.com/most-active", "Yahoo異動")
        ]
        for url, label in yahoo_us_urls:
            try:
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Accept-Language': 'en-US,en;q=0.5'
                }
                res = requests.get(url, headers=headers, timeout=10)
                
                if res.status_code == 200:
                    import re
                    # 抽取 Yahoo US 的 href="/quote/AAPL" 結構
                    matches = re.findall(r'href="/quote/([A-Z]+)"', res.text)
                    if matches:
                        found = list(dict.fromkeys(matches))[:30] # 保留最熱門 30 隻
                        add_to_map(found, label)
                        print(f"  🔥 成功捕捉到 {label}: {len(found)} 隻")
                    else:
                        print(f"  ⚠️ {label} 抓取略過: 找不到代號")
                else:
                    print(f"  ⚠️ {label} 抓取略過: HTTP {res.status_code}")
            except Exception as e:
                print(f"  ⚠️ {label} 抓取略過: {e}")

    # ---------------------------------------------------------
    # 3. 獲取日股動態名單 (Nikkei 225 + 當日熱門)
    # ---------------------------------------------------------
    wiki_jp_indexes = [
        ("https://zh.wikipedia.org/zh-hk/%E6%97%A5%E7%B6%93%E5%B9%B3%E5%9D%87%E6%8C%87%E6%95%B0", "NK225"),
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
                target_table = max(tables, key=len)
                    
                for col in target_table.columns:
                    col_name = str(col).lower()
                    # 💡 加入中文「編號」與「编号」嘅欄位名稱匹配
                    if 'code' in col_name or 'ticker' in col_name or 'symbol' in col_name or 'コード' in col_name or '編號' in col_name or '编号' in col_name:
                        target_col = col; break
                    
                if target_col is None:
                    for col in target_table.columns:
                        sample_vals = target_table[col].dropna().astype(str).tolist()[:5]
                        # 💡 放寬驗證：只要字串入面包含連續 4 個數字就當係
                        if sample_vals and all(re.search(r'\d{4}', str(x)) for x in sample_vals):
                            target_col = col; break

                if target_col is not None:
                    found_nk = []
                    for x in target_table[target_col].dropna():
                        # 💡 核心修復：用 re.search 抽走「東證1部：1332」入面嘅「1332」
                        match = re.search(r'(\d{4})', str(x))
                        if match:
                            found_nk.append(f"{match.group(1)}.T")
                            
                    add_to_map(list(dict.fromkeys(found_nk)), label)
                    print(f"  ✅ 成功從 Wikipedia 載入 {label} (共 {len(found_nk)} 隻)")
            except Exception as e:
                print(f"  ⚠️ {label} 載入失敗: {e}")
    except Exception as e:
            print(f"  ⚠️ 日股名單爬蟲區發生錯誤: {e}")
            
    # 👇 全局防呆保險絲 👇
    jp_index_count = len([tk for tk, src in ticker_sources.items() if 'NK225' in src])
    if jp_index_count < 50:
        print("🆘 偵測到日股大盤抓取異常，強制啟動 NK225 超級後備名單！")
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


    if IS_BACKTEST:
        print("🕰️ [時光機] 已停用所有即時排行榜 source，只使用指數成分股")
    else:
    # ---------------------------------------------------------
    # 3B. 捕捉 JP Trending (修復 404 網址改版問題)
    # ---------------------------------------------------------
        try:
            # 💡 更新為最新的 Yahoo JP 排行榜網址
            jp_trending_url = "https://finance.yahoo.co.jp/stocks/ranking/volume" 
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            }
            res_jp = requests.get(jp_trending_url, headers=headers, timeout=10)
            
            if res_jp.status_code == 200:
                import re
                matches = re.findall(r'/quote/(\d{4}\.T)', res_jp.text)
                
                if matches:
                    jp_trending = list(dict.fromkeys(matches))[:30]
                    add_to_map(jp_trending, "JP熱門")
                    print(f"  🔥 成功捕捉到日股當日熱錢焦點 (Yahoo JP): {len(jp_trending)} 隻")
                else:
                    print("  ⚠️ JP Trending: 網頁結構改變，找不到代號")
            else:
                print(f"  ⚠️ JP Trending 失敗: HTTP {res_jp.status_code}")
        except Exception as e:
            print(f"  ⚠️ JP Trending 略過: {e}")

    add_to_map(['SPY', '^VIX', '^N225'], "基準指數")
    # 名單加入匯率
    add_to_map(['JPY=X'], "匯率")
    return ticker_sources

_wl_tag = 'bt' if IS_BACKTEST else 'live'
WATCHLIST_CACHE = os.path.join(OUTPUT_DIR, f"watchlist_cache_{_wl_tag}.json")

_use_wl_cache = (os.path.exists(WATCHLIST_CACHE) and
                 (time.time() - os.path.getmtime(WATCHLIST_CACHE)) < 86400)  # 24 小時

if _use_wl_cache:
    with open(WATCHLIST_CACHE, "r", encoding="utf-8") as f:
        TICKER_MAP = json.load(f)
    print(f"⚡ 由 cache 讀取觀察名單 ({len(TICKER_MAP)} 隻)")
else:
    TICKER_MAP = build_dynamic_watchlist()
    with open(WATCHLIST_CACHE, "w", encoding="utf-8") as f:
        json.dump(TICKER_MAP, f, ensure_ascii=False)

print("⚠️ [偏差聲明] 名單使用當前指數成分股，存在生存者偏差，回測表現偏高")
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
    cutoff = closes.index[-1] - pd.Timedelta(days=SIMULATE_DAYS_AGO)
    print(f"⏰ [時光機] 回溯至 {cutoff.date()}")
    closes = closes.loc[:cutoff]
    highs  = highs.loc[:cutoff]
    lows   = lows.loc[:cutoff]
    vols   = vols.loc[:cutoff]
    opens  = opens.loc[:cutoff]

# 👇 獲取模擬當日的日期字串
today_str = closes.index[-1].strftime('%Y-%m-%d')
_p = 'IS' if today_str <= IS_END else ('OOS' if today_str <= OOS_END else 'FWD')
print(f"📅 [UAT] 模擬今日：{today_str} | 樣本期間：{_p}")

# =============================================================================
# MODULE 3 — 雙市場宏觀剖析 (FTD, 市寬, 派發日 獨立計算)
# =============================================================================
print("⏳ [3/8] 正在計算美/日雙市場宏觀指標...")

vix_c = closes['^VIX'].ffill()

jp_tickers = [t for t in closes.columns if str(t).endswith('.T')]
us_tickers = [t for t in closes.columns if not str(t).endswith('.T') and t not in BENCH]

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

# =========================================================================
# 🌍 四象限 (4-Regime) 大盤狀態與顏色判定 (統一整合版)
# =========================================================================
COLOR_BULL      = 65280     # 🟢 綠色 (#00FF00) - 全面牛市
COLOR_MILD_BULL = 16776960  # 🟡 黃色 (#FFE600) - 震盪微牛
COLOR_MILD_BEAR = 16753920  # 🟠 橙色 (#FF9900) - 防禦微熊
COLOR_BEAR      = 16711680  # 🔴 紅色 (#FF0000) - 凜冬熊市

def evaluate_4_regime(price, ma200, idx_50, tot_20):
    """判定 4 象限宏觀狀態、行動指引、顏色與風險等級 (數字愈大愈危險)"""
    if price > ma200:
        if idx_50 > 60:
            return "🟢 **全面牛市 (Bull)**", "正常建倉 (100% Risk)", COLOR_BULL, 1
        else:
            return "🟡 **震盪微牛 (Mild Bull)**", "防禦建倉 (收緊止損, 提早止盈)", COLOR_MILD_BULL, 2
    else:
        if tot_20 > 20:
            return "🟠 **防禦微熊 (Mild Bear)**", "僅限超賣撈底", COLOR_MILD_BEAR, 3
        else:
            return "🔴 **凜冬熊市 (Bear)**", "暫停突破建倉", COLOR_BEAR, 4

spx_price, spx_200ma = float(closes['SPY'].iloc[-1]), float(closes['SPY'].rolling(200).mean().iloc[-1])
n225_price, n225_200ma = float(closes['^N225'].iloc[-1]), float(closes['^N225'].rolling(200).mean().iloc[-1])

# 一次過計好狀態、文字、顏色同風險等級
us_regime, us_action, us_macro_color, us_risk_rank = evaluate_4_regime(spx_price, spx_200ma, us_matrix['index_50ma_pct'], us_matrix['total_20ma_pct'])
jp_regime, jp_action, jp_macro_color, jp_risk_rank = evaluate_4_regime(n225_price, n225_200ma, jp_matrix['index_50ma_pct'], jp_matrix['total_20ma_pct'])

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
    score = r126.rank(axis=1, pct=True) * 99 + 1
else:
    r252_filled = r252.fillna(r126) 
    score = ((0.6 * r126.fillna(0)) + (0.4 * r252_filled.fillna(0))).rank(axis=1, pct=True) * 99 + 1

us_cols = [c for c in score.columns if not str(c).endswith('.T') and c not in BENCH]
jp_cols = [c for c in score.columns if str(c).endswith('.T')]

rs_rank = pd.concat([
    score[us_cols].rank(axis=1, pct=True) * 99 + 1,
    score[jp_cols].rank(axis=1, pct=True) * 99 + 1,
], axis=1).reindex(columns=closes.columns)

rs_momentum = rs_rank - rs_rank.shift(20)

# =============================================================================
# MODULE 4 & 5 — 雙策略判定引擎與自動結算 (🚀 2026年7月終極進化版)
# =============================================================================
print(f"⏳ [4-5/8] 正在按 {today_str} 視角進行策略演算 (啟動極速向量化)...")

# 1. 處理現有持倉結案
current_prices = closes.iloc[-1].to_dict()
current_highs = highs.iloc[-1].to_dict()   # 引入全日最高價
current_lows = lows.iloc[-1].to_dict()     # 引入全日最低價
dict_low20 = lows.rolling(20).min().iloc[-1].to_dict()
dict_low5 = lows.rolling(5).min().iloc[-1].to_dict()

# ATR（保留完整序列，供 Chandelier 使用）
atr_series = (highs - lows).rolling(14).mean()
atr_14 = atr_series.iloc[-1]

# 🌟 CL-203：Chandelier Exit = 近期最高價 - N × ATR
#    用 shift(1)，即係「尋日已知嘅 stop 水平」，避免當日自我觸發
running_high_22 = highs.rolling(22).max()
chandelier_swing = (running_high_22 - ATR_TRAIL_MULT * atr_series).shift(1)
chandelier_short = (running_high_22 - 2.0 * atr_series).shift(1)   # 短線收緊啲
dict_chandelier_swing = chandelier_swing.iloc[-1].to_dict()
dict_chandelier_short = chandelier_short.iloc[-1].to_dict()
current_opens = opens.iloc[-1].to_dict()      # 保守成交價用

# 👇 新增：預先計算 SMA20 供超賣時間止損使用
dict_sma20_stop = closes.rolling(20).mean().iloc[-1].to_dict()
today_fx = float(current_prices.get('JPY=X', 0)) if not pd.isna(current_prices.get('JPY=X', np.nan)) else 0
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
            
            # 👇 新增：累加持倉日數 (Days Held)
            days_held = trade.get('days_held', 0) + 1
            trade['days_held'] = days_held
            
            if now_px > buy_px * 10 or now_px < buy_px * 0.1: 
                trade['status'], trade['close_date'] = '⚠️ 價格異常 (疑似拆股)', today_str
                closed_this_run.append(trade)
                continue
            if 'partial_tp_hit' not in trade: trade['partial_tp_hit'] = False
            if 'initial_sl' not in trade: trade['initial_sl'] = trade['sl']
            
            # ==========================================
            # 🛡️ 方案一：極度超賣專屬風險管理 (硬性斬倉)
            # ==========================================
            if "超賣" in strat_tag:
                # ❌ 條件 A：絕對金額止損 (Hard SL) - 跌穿買入價 5% 無條件投降
                if now_px < (buy_px * 0.95):
                    trade['last_px'] = now_px
                    trade['status'], trade['close_date'] = '❌ 觸發 5% 絕對止損', today_str
                    if trade.get('fx_entry') and today_fx: trade['fx_exit'] = round(today_fx, 4)
                    closed_this_run.append(trade)
                    continue
                    
                # ❌ 條件 B：時間止損 (Time Stop) - 撈底後 3 日內無反彈且處於 20 天線下方
                current_ma20 = dict_sma20_stop.get(tk, buy_px)
                if days_held >= 3 and now_px < current_ma20:
                    trade['last_px'] = now_px
                    trade['status'], trade['close_date'] = '❌ 觸發 3 日時間止損', today_str
                    if trade.get('fx_entry') and today_fx: trade['fx_exit'] = round(today_fx, 4)
                    closed_this_run.append(trade)
                    continue
            
            initial_risk = buy_px - trade['initial_sl']
            is_short_term = ('缺口' in strat_tag or '超賣' in strat_tag)

            # ==========================================
            # ⏱️ CL-205：波段時間止損（15 日未行出 1R 就放走，釋放資金）
            # ==========================================
            if (not is_short_term
                    and not trade['partial_tp_hit']
                    and days_held >= SWING_TIME_STOP_DAYS
                    and initial_risk > 0
                    and (now_px - buy_px) < (initial_risk * SWING_TIME_STOP_MIN_R)):
                trade['last_px'] = now_px
                trade['status'], trade['close_date'] = '⏱️ 時間止損 (無進展)', today_str
                if trade.get('fx_entry') and today_fx: trade['fx_exit'] = round(today_fx, 4)
                closed_this_run.append(trade)
                continue
            
            # 👇 讀取專屬的 TP1 價格 (相容舊紀錄)
            tp1_price = trade.get('tp1_price', round(buy_px + (initial_risk * PARTIAL_TP_R), 2))
            
            # --- 分注平倉 ---
            if not trade['partial_tp_hit'] and today_high >= tp1_price and initial_risk > 0:
                trade['partial_tp_hit'] = True
                trade['sl'] = buy_px
                print(f"🎯 [分注系統] {tk} 觸發 TP1 ({tp1_price})，鎖定 {int(PARTIAL_TP_PCT*100)}% 利潤並保本。")

            # --- 最終結案判定 (3-Way Classification) ---
            tp, sl = trade.get('tp'), trade.get('sl')
            hit_tp = tp and today_high >= tp
            hit_sl = sl and today_low <= sl
            
            if trade['partial_tp_hit']:
                # 🌟 CL-203：Chandelier Exit（最高價回撤 N×ATR）
                trail_stop = (dict_chandelier_short.get(tk) if is_short_term
                              else dict_chandelier_swing.get(tk))

                # 👇 保本優先：實際 stop = chandelier 同 買入價 之中較高者
                if trail_stop and not pd.isna(trail_stop):
                    eff_stop = max(float(trail_stop), buy_px)
                else:
                    eff_stop = buy_px

                if today_low <= eff_stop:
                    today_open = float(current_opens.get(tk, eff_stop))
                    trade['last_px'] = round(min(eff_stop, today_open), 2)
                    trade['status'], trade['close_date'] = '✅ TRAIL EXIT', today_str
                    if trade.get('fx_entry') and today_fx: trade['fx_exit'] = round(today_fx, 4)
                    closed_this_run.append(trade)
                elif hit_tp:
                    # 撞 MAX TP 爆升：尾倉 25% 以 tp 價格完美止賺
                    trade['last_px'] = tp 
                    trade['status'], trade['close_date'] = '✅ MAX TP', today_str
                    if trade.get('fx_entry') and today_fx: trade['fx_exit'] = round(today_fx, 4)
                    closed_this_run.append(trade)
            else:
                if hit_sl:
                    trade['last_px'] = sl
                    trade['status'], trade['close_date'] = '❌ STOP LOSS', today_str
                    if trade.get('fx_entry') and today_fx: trade['fx_exit'] = round(today_fx, 4)
                    closed_this_run.append(trade)
                elif hit_tp:
                    trade['last_px'] = tp
                    trade['status'], trade['close_date'] = '✅ MAX TP', today_str
                    if trade.get('fx_entry') and today_fx: trade['fx_exit'] = round(today_fx, 4)
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

# 布林帶 (Bollinger Bands) & SMA20
sma20_all = closes.rolling(20).mean()
std20_all = closes.rolling(20).std()
bb_lower_all = sma20_all - (2 * std20_all)
bb_width_all = (4 * std20_all) / sma20_all
bb_width_min120 = bb_width_all.rolling(120).min().iloc[-1]

# ML-RSI 核心：保留時間序列以計算動態標準差
delta = closes.diff()
gain = delta.where(delta > 0, 0).rolling(14).mean()
loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
rsi_all = 100 - (100 / (1 + gain / loss))
rsi_14 = rsi_all.iloc[-1]
rsi_std_14 = rsi_all.rolling(14).std().iloc[-1]

# VCP 形態參數 (Base Drawdown & Recent Volatility)
max60 = closes.rolling(60).max()
min60 = closes.rolling(60).min()
_base_dd_hist = (max60 - min60) / max60          # 👈 保留完整序列
base_dd = _base_dd_hist.iloc[-1]

_c_prev = closes.shift(1)
max10 = _c_prev.rolling(10).max()
min10 = _c_prev.rolling(10).min()
_rec_volat_hist = (max10 - min10) / max10        # 👈 保留完整序列
rec_volat = _rec_volat_hist.iloc[-1] 

sma50_all = closes.rolling(50).mean()
sma200_all = closes.rolling(200).mean()
max120_all = closes.rolling(120).max() # 半年高位
max10_prev_all = closes.shift(1).rolling(10).max() # 尋日為止嘅10日高位 (阻力線)

# DMA50 (偏離度) 與 SMC 平均實體
dma50_all = (closes - sma50_all) / sma50_all
avg_body_all = abs(closes - opens).rolling(14).mean()

# 🌟 7月27日最新升級：滾動 VWAP 與 VAVS 巨鯨吸收率
typical_price = (highs + lows + closes) / 3
vwap_20_all = (typical_price * vols).rolling(20).sum() / vols.rolling(20).sum()
daily_spread = highs - lows
vavs_all = vols / (daily_spread + 1e-5)
vavs_ma20_all = vavs_all.rolling(20).mean()

# Volume MA
vol_ma50 = vols.rolling(50).mean().iloc[-1]
vol_ma20 = vols.rolling(20).mean().iloc[-1]

# 宏觀事件避險 (Macro Event Filter)
import datetime
today_date = closes.index[-1]
current_month = today_date.month
is_cpi_eve = (today_date.month == 7 and today_date.day == 13)

# 👇 將最終結果轉為 Dict 以達到 O(1) 極速查詢
dict_dollar_vol = dollar_vol_20.to_dict()
dict_rs = rs_rank.iloc[-1].to_dict()
dict_mom = rs_momentum.iloc[-1].to_dict()
dict_bb_lower = bb_lower_all.iloc[-1].to_dict()
dict_bb_width = bb_width_all.iloc[-1].to_dict()
dict_bb_width_min120 = bb_width_min120.to_dict()
dict_atr = atr_14.to_dict()
dict_rsi = rsi_14.to_dict()
dict_rsi_std = rsi_std_14.to_dict()
dict_base_dd = base_dd.to_dict()
dict_rec_volat = rec_volat.to_dict()
dict_vol_ma50 = vol_ma50.to_dict()
dict_vol_ma20 = vol_ma20.to_dict()
dict_prev_price = prev_prices.to_dict()
dict_curr_open = curr_opens.to_dict()
dict_curr_vol = curr_vols.to_dict()
dict_curr_high = highs.iloc[-1].to_dict()
dict_curr_low = lows.iloc[-1].to_dict()
dict_prev_high = highs.shift(1).iloc[-1].to_dict()
dict_sma20 = sma20_all.iloc[-1].to_dict()
dict_sma50 = sma50_all.iloc[-1].to_dict()
dict_sma200 = sma200_all.iloc[-1].to_dict()
dict_dma50 = dma50_all.iloc[-1].to_dict()
dict_avg_body = avg_body_all.iloc[-1].to_dict()
dict_max120 = max120_all.iloc[-1].to_dict()
dict_max10_prev = max10_prev_all.iloc[-1].to_dict()
dict_vwap20 = vwap_20_all.iloc[-1].to_dict()
dict_vavs = vavs_all.iloc[-1].to_dict()
dict_vavs_ma = vavs_ma20_all.iloc[-1].to_dict()

# =========================================================================
# 📊 自我參照百分位：只對最後一行做 rank，避免 rolling.rank 拖死回測
#    數值意義：0.0 = 過去一年最低，1.0 = 過去一年最高
# =========================================================================
_win = min(PCT_LOOKBACK, len(closes))
dict_rec_volat_pct = _rec_volat_hist.iloc[-_win:].rank(pct=True).iloc[-1].to_dict()
dict_base_dd_pct   = _base_dd_hist.iloc[-_win:].rank(pct=True).iloc[-1].to_dict()
dict_dma50_pct     = dma50_all.iloc[-_win:].rank(pct=True).iloc[-1].to_dict()

def pct_of(d, tk, default=1.0):
    """安全讀取百分位，NaN 或者搵唔到就返回 default（=最寬鬆，唔會誤觸發）"""
    v = d.get(tk)
    return default if v is None or pd.isna(v) else float(v)

# =========================================================================
_is_jp_idx = dollar_vol_20.index.str.endswith('.T')
_dv_us = dollar_vol_20[~_is_jp_idx].dropna()
_dv_jp = dollar_vol_20[_is_jp_idx].dropna()

if USE_PCT_MODE and len(_dv_us) > 50 and len(_dv_jp) > 50:
    us_thresh   = _dv_us.quantile(PCT_LIQUIDITY)
    jp_thresh   = _dv_jp.quantile(PCT_LIQUIDITY)
    elite_liq   = _dv_us.quantile(PCT_ELITE_LIQ)      # 缺口策略專用
else:
    us_thresh, jp_thresh, elite_liq = 20_000_000, 300_000_000, 50_000_000

print(f"💧 流動性門檻 | 美股 ${us_thresh/1e6:.1f}M | 日股 ¥{jp_thresh/1e6:.0f}M | 精英 ${elite_liq/1e6:.0f}M")

us_mask = (~_is_jp_idx) & (dollar_vol_20 >= us_thresh)
jp_mask = (_is_jp_idx)  & (dollar_vol_20 >= jp_thresh)

# 合併符合資格的名單
valid_tickers = dollar_vol_20[us_mask | jp_mask].index.tolist()

# 🛡️ 終極修復：踢走大盤指數，並加入 np.nan 預防 pd.isna 報錯
valid_tickers = [t for t in valid_tickers if t not in BENCH and not pd.isna(dict_rs.get(t, np.nan))]

print(f"🧹 過濾成交量低迷股票後，掃描名單由 {len(ALL_TICKERS)} 縮減至 {len(valid_tickers)} 隻！")

# =========================================================================
# 開始極速掃描 (只行精華名單)
# =========================================================================
from collections import Counter
reject = Counter() 
scan_errors = {}

open_by_tk = {}
for _t in trade_history:
    if _t.get('status') == 'OPEN':
        open_by_tk.setdefault(_t['tk'], []).append(_t)

# 每日更新「目前持倉」的現時指標
for ticker in valid_tickers:
    try:
        rs = dict_rs.get(ticker)
        cp = float(current_prices[ticker])
        is_jp = ticker.endswith('.T')

        # 🛡️ 防禦：剔除仙股與錯價股 (美股 > $1, 日股 > 100円)
        min_price_threshold = 100 if is_jp else 1
        if cp < min_price_threshold: continue
        
        ticker_macro = jp_regime if is_jp else us_regime

        rs_mom = dict_mom.get(ticker)
        catr = float(dict_atr.get(ticker))
        rsi_val = float(dict_rsi.get(ticker))
        
        for t in open_by_tk.get(ticker, []):
            if '超賣' in t.get('tag', ''):
                t['curr_metric'] = f"RSI: {int(rsi_val)}"
            else:
                t['curr_metric'] = f"RS: {int(rs)}"

        if rs < PQR_SWING_MIN: continue

        # 提取高階量化特徵參數
        v_base_dd = dict_base_dd.get(ticker)
        v_rec_vol = dict_rec_volat.get(ticker)
        c_vol = dict_curr_vol.get(ticker)
        v_ma20 = dict_vol_ma20.get(ticker)
        v_ma50 = dict_vol_ma50.get(ticker)
        
        sma20 = dict_sma20.get(ticker)
        sma50 = dict_sma50.get(ticker)
        sma200 = dict_sma200.get(ticker)
        dma50 = dict_dma50.get(ticker)
        high120 = dict_max120.get(ticker)
        resist_10d = dict_max10_prev.get(ticker) 
        
        c_op = dict_curr_open.get(ticker)
        h_val = dict_curr_high.get(ticker)
        l_val = dict_curr_low.get(ticker)
        p_px = dict_prev_price.get(ticker)
        b_lower = dict_bb_lower.get(ticker)
        
        avg_body_size = dict_avg_body.get(ticker, 0)
        current_body_size = abs(cp - c_op)
        prev_high = dict_prev_high.get(ticker, 9999)
        rsi_std = dict_rsi_std.get(ticker, 5)
        low5_min = dict_low5.get(ticker, 0)
        low20_min = dict_low20.get(ticker, 0)
        vwap20 = dict_vwap20.get(ticker, 0)
        curr_vavs = dict_vavs.get(ticker, 0)
        vavs_ma = dict_vavs_ma.get(ticker, 0)

        # 🌟 微觀 K 線結構
        full_range = h_val - l_val
        candle_architecture_score = current_body_size / full_range if full_range > 0 else 0
        is_solid_candle = candle_architecture_score >= 0.7
        closing_strength = (cp - l_val) / full_range if full_range > 0 else 0

        # =================================================================
        # 🎯 方案二：設定「雙重共振」質量過濾器 (Quality Filter)
        # =================================================================
        # 強制要求動能 (Momentum) 大於 2，且當日成交量必須是 20日平均的 1.5 倍以上
        quality_filter_passed = (rs_mom > 2) and (c_vol > v_ma20 * 1.5)

        # =================================================================
        # 📈 策略 1：波段建倉 (VCP 突破 / BB 擠壓) 
        # 結合 AlphaTrend, SMC 訂單塊, AMD 洗盤, VWAP 機構護航
        # =================================================================
        is_uptrend = (cp > sma50) and (sma50 > sma200)
        is_near_high = ((high120 - cp) / high120) <= 0.15
        if USE_PCT_MODE:
            _rv_p = pct_of(dict_rec_volat_pct, ticker)
            _bd_p = pct_of(dict_base_dd_pct, ticker)
            is_tight = (_rv_p <= PCT_REC_VOLAT) and (_bd_p <= PCT_BASE_DD)
        else:
            is_tight = (v_base_dd <= 0.35) and (v_rec_vol <= 0.12)
        
        # 💡 升級：VCP 突破必須綁定「雙重共振」
        is_breaking_out = (cp > resist_10d) and quality_filter_passed
        
        is_alpha_trend = (rsi_val > 50) and (cp > (sma20 + 0.5 * catr))
        is_institutional_ob = current_body_size > (1.5 * avg_body_size)
        is_amd_manipulation = (low5_min <= low20_min * 1.01) and (cp > (low5_min + 0.5 * catr))
        is_above_vwap = cp > vwap20

        _conds = {
            'uptrend': is_uptrend, 'near_high': is_near_high, 'tight': is_tight,
            'breakout': is_breaking_out, 'solid_candle': is_solid_candle,
            'alpha_trend': is_alpha_trend, 'smc_ob': is_institutional_ob,
            'amd': is_amd_manipulation, 'above_vwap': is_above_vwap,
            'quality': quality_filter_passed,
        }
        for k, v in _conds.items():
            if not v: reject[k] += 1

        is_vcp = is_uptrend and is_near_high and is_tight and is_breaking_out and is_solid_candle and is_alpha_trend and is_institutional_ob and is_amd_manipulation and (not is_cpi_eve) and is_above_vwap
        
        # 💡 升級：BB 擠壓強制綁定「雙重共振」，過濾平庸的波動
        is_bb_sqz = (dict_bb_width.get(ticker) <= dict_bb_width_min120.get(ticker) * 1.1) and is_uptrend and is_alpha_trend and (not is_cpi_eve) and is_above_vwap and quality_filter_passed
        
        # =================================================================
        # 📉 策略 2：短線游擊 (缺口動能 / 極度超賣)
        # 結合 ML-RSI, MSS 結構轉變, 恐慌極值, 巨鯨吸收率
        # =================================================================
        gap_magnitude = (c_op - p_px) / p_px if p_px > 0 else 0

        if USE_PCT_MODE:
            _atr_pct = (catr / cp) if cp > 0 else 0.03
            _gap_min = max(0.02, PCT_GAP_ATR * _atr_pct)     # 至少 2%，防止低波股濫發訊號
        else:
            _gap_min = 0.03

        is_gap_up = (gap_magnitude >= _gap_min) and (c_vol > v_ma20 * 2) and (cp > c_op) and (closing_strength >= 0.6)
        
        dynamic_oversold_threshold = max(18, 30 - (rsi_std * 0.5)) 
        is_ml_oversold = (rsi_val < dynamic_oversold_threshold)
        is_mss = cp > prev_high
        is_volumetric_extreme = c_vol > (v_ma50 * 1.5)
        is_whale_absorption = curr_vavs > (vavs_ma * 2.0)

        if USE_PCT_MODE:
            _dma_p = pct_of(dict_dma50_pct, ticker)          # 低百分位 = 特別偏離
            is_deep_below = (_dma_p <= PCT_DMA50_OS) and (dma50 < -0.05)   # 加絕對下限防呆
        else:
            is_deep_below = (dma50 < -0.15)

        is_oversold = is_ml_oversold and (cp < b_lower) and is_deep_below and is_volumetric_extreme and (is_mss or is_whale_absorption)
        # =================================================================
        # ⚖️ 大市四象限過濾與動態止損 (Seasonal & Regime Control)
        # =================================================================
        is_red_light = '🔴' in ticker_macro or '🟠' in ticker_macro # 包含 Bear 與 Mild Bear
        is_mild_bull = '🟡' in ticker_macro
        
        trade_info = None 
        tag_name, entry_metric = "", ""
        sl_p, tp_p, tp1_price, risk_per_share = 0, 0, 0, 0

        if (is_vcp or is_bb_sqz):
            if is_red_light: continue # 熊市嚴禁突破建倉

            # 👇 新增：美股如果處於「🟡 震盪微牛」，假突破極多，直接封印！
            # 只有日股（走勢較順）先容許喺黃燈做突破
            if not is_jp and is_mild_bull: continue
            
            tag_name = "🏆 VCP 突破" if is_vcp else "💥 BB 擠壓"
            seasonal_vix_multiplier = 1.2 if current_month == 7 else 1.0 
            
            # 👇 新增：美日非對稱止損引擎
            if is_jp:
                # 日股趨勢較穩，可以配合大盤收緊止損 (1.0 ATR)
                if is_mild_bull or current_month == 7:
                    sl_p = round(cp - (1.0 * catr * seasonal_vix_multiplier), 2)
                else:
                    sl_p = round(cp - 1.5 * catr, 2)
            else:
                # 美股雜訊大，必須強制給予最少 1.5 ATR 的呼吸空間，防震倉！
                sl_p = round(cp - 1.5 * catr, 2)
                
            tp_p = round(cp + MAX_TP_ATR * catr, 2) if USE_MAX_TP else None
            risk_per_share = cp - sl_p
            
            # 👇 核心調校：將 TP1 統一改為 1.5R (原為 2.0R 太難觸發)
            tp1_price = round(cp + (risk_per_share * PARTIAL_TP_R), 2)
            
            _u = "¥" if is_jp else "$"
            swing_results.append({
                'tk': ticker, 'rs': round(rs,0), 'mom': round(rs_mom,1), 
                'px': round(cp,2), 'sl': sl_p, 'tp': tp_p, 'tag': tag_name,
                # 👇 新增：顯示用文字，避免 None 爆 HTML
                'tp_txt': (f"{_u}{tp_p}" if tp_p else "Trailing 出場"),
                'tp_pct': (f"(+{((tp_p-cp)/cp*100):.1f}%)" if tp_p else "(無上限)"),
                'has_mss': is_mss, 'has_smc': is_institutional_ob, 
                'has_amd': is_amd_manipulation, 'ml_rsi': round(rsi_val, 1)
            })
            
            trade_info = {
                'date': today_str, 'tk': ticker, 'px': round(cp, 2), 
                'sl': sl_p, 'tp': tp_p, 'initial_sl': sl_p, 'tp1_price': tp1_price,
                'last_px': round(cp, 2), 'status': 'OPEN', 'tag': tag_name, 
                'entry_metric': entry_metric, 'curr_metric': entry_metric
            }

        # =================================================================
        # 獨立處理 1：⚡ 缺口動能 (爆發力強 -> 要求 1.5R 盈虧比)
        # =================================================================
        elif is_gap_up:
            if is_red_light: continue # 熊市嚴禁做「缺口高開」接火棒
            
            # 👇 Manager 特批：美股「精英制」缺口放行條件
            if not is_jp:
                # 條件 A：大市必須係「🟢 全面牛市」(黃燈/紅燈一律禁賽)
                if is_mild_bull: continue 
                
                # 條件 B：只做真正的市場領頭羊 (RS 必須 > 90)
                if rs < 90: continue 
                
                # 條件 C：極高流動性防護 (每日平均成交額 > 5000萬美金，過濾容易被操控的中小企)
                if dict_dollar_vol.get(ticker, 0) < elite_liq: continue
                
                # 條件 D：爆發力必須異常強大 (當日成交量大於 20日平均的 3倍！)
                if c_vol < v_ma20 * 3: continue
            
            # 如果過到上面嘅地獄測試 (或者本身係日股)，就可以正常建倉！
            tag_name = "⚡ 缺口動能"
            sl_p = round(cp - (2.0 * catr), 2)  # 畀多啲空間避開震倉
            tp_p = round(cp + (6.0 * catr), 2)
            risk_per_share = cp - sl_p
            entry_metric = f"RS: {int(rs)}"
            
            # 動能爆發，強制要求 TP1，修復 EV (數學期望值)！
            tp1_price = round(cp + (risk_per_share * PARTIAL_TP_R), 2)
            
            _u = "¥" if is_jp else "$"
            short_term_results.append({
                'tk': ticker, 'rs': round(rs,0), 'mom': round(rs_mom,1), 
                'px': round(cp,2), 'sl': sl_p, 'tp': tp_p, 'tag': tag_name,
                # 👇 新增：顯示用文字，避免 None 爆 HTML
                'tp_txt': (f"{_u}{tp_p}" if tp_p else "Trailing 出場"),
                'tp_pct': (f"(+{((tp_p-cp)/cp*100):.1f}%)" if tp_p else "(無上限)"),
                'has_mss': is_mss, 'has_smc': is_institutional_ob, 
                'has_amd': is_amd_manipulation, 'ml_rsi': round(rsi_val, 1)
            })

            trade_info = {
                'date': today_str, 'tk': ticker, 'px': round(cp, 2), 
                'sl': sl_p, 'tp': tp_p, 'initial_sl': sl_p, 'tp1_price': tp1_price,
                'last_px': round(cp, 2), 'status': 'OPEN', 'tag': tag_name, 
                'entry_metric': entry_metric, 'curr_metric': entry_metric
            }

        # =================================================================
        # 獨立處理 2：📉 極度超賣 (搶反彈 -> 1R 提早鎖定利潤，防禦極端單邊市)
        # =================================================================
        elif is_oversold:
            tag_name = "📉 極度超賣"
            
            # 超賣撈底需要極窄止損，錯咗即走！(改用 1.5 倍 ATR)
            sl_p = round(cp - (1.5 * catr), 2)
            tp_p = round(cp + (4.5 * catr), 2)
            risk_per_share = cp - sl_p
            entry_metric = f"RSI: {int(rsi_val)}"
            
            # 搶反彈見好就收，保留 1R 觸發 TP1，保本最重要！
            tp1_price = round(cp + (risk_per_share * PARTIAL_TP_R), 2)
            
            _u = "¥" if is_jp else "$"
            short_term_results.append({
                'tk': ticker, 'rs': round(rs,0), 'mom': round(rs_mom,1), 
                'px': round(cp,2), 'sl': sl_p, 'tp': tp_p, 'tag': tag_name,
                # 👇 新增：顯示用文字，避免 None 爆 HTML
                'tp_txt': (f"{_u}{tp_p}" if tp_p else "Trailing 出場"),
                'tp_pct': (f"(+{((tp_p-cp)/cp*100):.1f}%)" if tp_p else "(無上限)"),
                'has_mss': is_mss, 'has_smc': is_institutional_ob, 
                'has_amd': is_amd_manipulation, 'ml_rsi': round(rsi_val, 1)
            })

            trade_info = {
                'date': today_str, 'tk': ticker, 'px': round(cp, 2), 
                'sl': sl_p, 'tp': tp_p, 'initial_sl': sl_p, 'tp1_price': tp1_price,
                'last_px': round(cp, 2), 'status': 'OPEN', 'tag': tag_name, 
                'entry_metric': entry_metric, 'curr_metric': entry_metric
            }
                
        if trade_info:
            ticker_sources = TICKER_MAP.get(ticker, [])
            s_info = get_stock_info(ticker) 

            # 👇 新增：日股記低開倉當日匯率
            if is_jp and 'JPY=X' in current_prices and not pd.isna(current_prices['JPY=X']):
                trade_info['fx_entry'] = round(float(current_prices['JPY=X']), 4)
            
            # 👇 計算分數
            feature_score = int(is_mss) + int(is_institutional_ob) + int(is_amd_manipulation)
            
            trade_info['sources'] = ticker_sources
            trade_info['sector'] = s_info['sector']
            trade_info['mcap'] = s_info['mcap']
            trade_info['feature_score'] = feature_score
            trade_info['features'] = {
                'mss': is_mss, 'smc': is_institutional_ob,
                'amd': is_amd_manipulation, 'ml_rsi': round(rsi_val, 1),
                'rv_pct': round(pct_of(dict_rec_volat_pct, ticker), 3),
                'bd_pct': round(pct_of(dict_base_dd_pct, ticker), 3),
            }
            trade_info['period'] = ('IS' if today_str <= IS_END
                else 'OOS' if today_str <= OOS_END
                else 'FWD')

            current_ticker_color = jp_macro_color if is_jp else us_macro_color
            
            # 👇 將 features 傳畀 Discord
            send_discord_alert(ticker, tag_name, round(cp, 2), sl_p, tp_p, current_ticker_color, ticker_sources, tp1_price=tp1_price, features=trade_info['features'])
            
            if ticker not in open_by_tk:
                 trade_history.append(trade_info)
                 open_by_tk.setdefault(ticker, []).append(trade_info)
            
            js_payload.append({
                "ticker": ticker, "tag": tag_name, "curr_price": round(cp, 2), 
                "sl_price": sl_p, "tp_price": tp_p if tp_p else 0,   # 👈 加 if
                "risk_per_share": risk_per_share,
                "feature_score": feature_score
            })

    except Exception as e:
        scan_errors[ticker] = repr(e)

print(f"\n📊 掃描 {len(valid_tickers)} 隻，各條件不通過統計：")
for k, v in reject.most_common():
    print(f"   {k:<15} 擋走 {v:>5} 隻 ({v/max(len(valid_tickers),1)*100:5.1f}%)")

if scan_errors:
    print(f"⚠️ 掃描期間有 {len(scan_errors)} 隻股票發生錯誤")
    for err, cnt in Counter(scan_errors.values()).most_common(5):
        print(f"   ×{cnt}  {err[:150]}")

swing_results.sort(key=lambda x: x['rs'], reverse=True)
short_term_results.sort(key=lambda x: x['rs'], reverse=True)

# 保留 20000 條紀錄以確保歷史倉位對帳準確
_open   = [x for x in trade_history if x.get('status') == 'OPEN']
_closed = [x for x in trade_history if x.get('status') != 'OPEN']
with open(HISTORY_FILE, "w", encoding="utf-8") as f:
    json.dump(_open + _closed[-20000:], f, indent=4)

# =========================================================================
# 📊 額外擴充：將 Trade History 自動匯出為 CSV 方便 Excel 覆盤
# =========================================================================
import csv

CSV_EXPORT_FILE = os.path.join(OUTPUT_DIR, f"uat_trade_history{_hs}.csv")

if trade_history and IS_FINAL_RUN:
    # 💡 修復：動態收集所有出現過嘅 Keys，防止因欄位缺失而報錯
    all_keys = set()
    for t in trade_history:
        all_keys.update(t.keys())
    
    # 將 keys 轉為 list，並可以排個序令佢整齊啲
    keys = list(all_keys)
    
    try:
        with open(CSV_EXPORT_FILE, 'w', newline='', encoding='utf-8-sig') as output_file:
            dict_writer = csv.DictWriter(output_file, fieldnames=keys, extrasaction='ignore')
            dict_writer.writeheader()
            dict_writer.writerows(trade_history)
        print(f"📁 [UAT 覆盤] 成功匯出交易紀錄至 CSV 檔案：{CSV_EXPORT_FILE}")
    except Exception as e:
        print(f"⚠️ CSV 匯出失敗: {e}")


# =============================================================================
# MODULE 7 — 總結算與 Discord 報告 (UAT 詳盡數據統一版)
# =============================================================================
print("⏳ [7/8] 正在結算戰績並發送 Discord 報告...")

# =========================================================================
# 🛠️ 修正版：基於真實 P&L 計算勝率 (杜絕「蝕錢卻當贏」的 Bug)
# =========================================================================
def calc_true_pnl(t, partial_pct=PARTIAL_TP_PCT):
    buy_px  = t.get('px', 0)
    last_px = t.get('last_px', buy_px)
    if not buy_px or buy_px <= 0: return 0.0

    if t.get('partial_tp_hit', False):
        initial_sl = t.get('initial_sl', buy_px)
        tp1_price  = t.get('tp1_price', buy_px + (buy_px - initial_sl) * PARTIAL_TP_R)
        pnl = ((partial_pct * TICKETSIZE / buy_px) * (tp1_price - buy_px)
               + ((1 - partial_pct) * TICKETSIZE / buy_px) * (last_px - buy_px))
    else:
        pnl = (TICKETSIZE / buy_px) * (last_px - buy_px)

    # 👇 CL-106：日股要換算返美元
    fx_e, fx_x = t.get('fx_entry'), t.get('fx_exit')
    if fx_e and fx_x and fx_x > 0:
        pnl = TICKETSIZE * ((1 + pnl / TICKETSIZE) * (fx_e / fx_x) - 1)

    # 👇 CL-105：扣返來回交易成本
    return pnl - (TICKETSIZE * ROUND_TRIP_COST)

def calculate_stats(history):
    closed = [t for t in history if t.get('status') != 'OPEN']
    if not closed: return 0, 0, 0
    wins = [t for t in closed if calc_true_pnl(t) > 0]
    return len(closed), len(wins), round(len(wins)/len(closed)*100, 1)

if IS_FINAL_RUN:
    total_closed, wins, win_rate = calculate_stats(trade_history)
else:
    total_closed, wins, win_rate = 0, 0, 0

if DISCORD_SUMMARY_WEBHOOK and IS_FINAL_RUN:
    # 1. 今日結案明細
    detail_lines = []
    if closed_this_run:
        for t in closed_this_run:
            shares = TICKETSIZE / t['px']
            pnl = shares * (t['last_px'] - t['px'])
            pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
            icon = "🟢" if pnl >= 0 else "🔴"
            detail_lines.append(f"{icon} **{t['tk']}** ({t.get('tag', 'N/A')}): {pnl_str}")
    details_text = "\n".join(detail_lines) if detail_lines else "今日無新結案交易。"

    # 1.5 新增：計算今日新開倉數量與結案數量 (對帳用)
    new_trades_today = [t for t in trade_history if t.get('date') == today_str and t.get('status') == 'OPEN']
    new_count = len(new_trades_today)
    closed_count = len(closed_this_run) if 'closed_this_run' in locals() else 0

    # 2. 目前持倉浮盈與總數量 (🛡️ 75/25 分注平倉精準會計版)
    open_trades = [t for t in trade_history if t.get('status') == 'OPEN']
    current_open_count = len(open_trades)
    
    floating_pnl = 0
    for t in open_trades:
        buy_px = t['px']
        last_px = t['last_px']
        
        if t.get('partial_tp_hit', False):
            # 75% 已經鎖定在 TP1，25% 隨現價浮動
            initial_risk = buy_px - t.get('initial_sl', buy_px)
            tp1_price = t.get('tp1_price', buy_px + (initial_risk * PARTIAL_TP_R))
            
            pnl_closed_half = (TICKETSIZE * PARTIAL_TP_PCT / buy_px) * (tp1_price - buy_px)   # 已鎖定利潤
            pnl_floating_half = (TICKETSIZE * (1 - PARTIAL_TP_PCT) / buy_px) * (last_px - buy_px) # 剩餘浮動盈虧
            floating_pnl += (pnl_closed_half + pnl_floating_half)
        else:
            # 常規未分注持倉，100% 隨現價浮動
            floating_pnl += (TICKETSIZE / buy_px) * (last_px - buy_px)
            
    floating_str = f"+${floating_pnl:.2f}" if floating_pnl >= 0 else f"-${abs(floating_pnl):.2f}"

    # 3. 細分策略 P&L 結算 (歷史總計 - 強制清洗並排序)      
    strategy_stats = {}
    for t in [x for x in trade_history if x.get('status') != 'OPEN']:
        raw_tag = t.get('tag', '未分類')
        
        # 🧹 清洗標籤：統一合併分注與全平倉的數據
        clean_tag = raw_tag.replace(' (🎯已分注平倉)', '').replace(' (已分注平倉)', '').replace(' (🎯已部分平倉)', '').strip()
        
        if clean_tag not in strategy_stats: 
            strategy_stats[clean_tag] = {'total': 0, 'wins': 0, 'pnl': 0}
            
        trade_pnl = calc_true_pnl(t)
        strategy_stats[clean_tag]['total'] += 1
        strategy_stats[clean_tag]['pnl'] += trade_pnl
        if trade_pnl > 0:
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
    # 👇 多維度分組對帳邏輯 (Market x Strategy)
    # ==========================================
    group_stats = {'US': {}, 'JP': {}}

    def ensure_strat(mkt, strat):
        if strat not in group_stats[mkt]:
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

    # 5. 生成 Discord 友善排版
    summary_lines = ["\n**【📊 策略持倉對帳表】**"]
    
    for mkt in ['US', 'JP']:
        mkt_name = "🇺🇸 **美股 (US)**" if mkt == 'US' else "🇯🇵 **日股 (JP)**"
        mkt_lines = []
        
        for strat, s in group_stats[mkt].items():
            if s['prev'] == 0 and s['new'] == 0 and s['closed'] == 0 and s['final'] == 0: continue
            line = f"{strat} ➔ 原有: `{s['prev']:3}` | 新開: `+{s['new']:<2}` | 結案: `-{s['closed']:<2}` ＝ 總持倉: `{s['final']:3}`"
            mkt_lines.append(line)
            
        if mkt_lines:
            summary_lines.append(f"\n{mkt_name}")
            summary_lines.extend(mkt_lines)

    group_summary_text = "\n".join(summary_lines)

    # 6. 準備 Discord 宏觀數據
    us_scan_count = len(us_tickers)
    jp_scan_count = len(jp_tickers)

    # =========================================================================
    # 🌍 四象限 (4-Regime) 大盤狀態判定 (💡 結合 Production 詳盡排版)
    # =========================================================================
    # 美股 (SPX vs Total)
    spx_price = closes['SPY'].iloc[-1] if 'SPY' in closes.columns else 0
    spx_200ma = sma200_all['SPY'].iloc[-1] if 'SPY' in sma200_all.columns else 0
    us_50ma_pct = us_matrix.get('index_50ma_pct', 0)
    us_20ma_pct = us_matrix.get('total_20ma_pct', 0)

    if spx_price > spx_200ma:
        if us_50ma_pct > 60:
            us_regime = "🟢 **全面牛市 (Bull)**"
            us_action = "正常建倉 (100% Risk)"
        else:
            us_regime = "🟡 **震盪微牛 (Mild Bull)**"
            us_action = "防禦建倉 (收緊止損, 提早止盈)"
    else:
        if us_20ma_pct > 20:
            us_regime = "🟠 **防禦微熊 (Mild Bear)**"
            us_action = "僅限超賣撈底"
        else:
            us_regime = "🔴 **凜冬熊市 (Bear)**"
            us_action = "暫停突破建倉"

    # 日股 (N225 vs Total)
    n225_price = closes['^N225'].iloc[-1] if '^N225' in closes.columns else 0
    n225_200ma = sma200_all['^N225'].iloc[-1] if '^N225' in sma200_all.columns else 0
    jp_50ma_pct = jp_matrix.get('index_50ma_pct', 0)
    jp_20ma_pct = jp_matrix.get('total_20ma_pct', 0)

    if n225_price > n225_200ma:
        if jp_50ma_pct > 60:
            jp_regime = "🟢 **全面牛市 (Bull)**"
            jp_action = "正常建倉 (100% Risk)"
        else:
            jp_regime = "🟡 **震盪微牛 (Mild Bull)**"
            jp_action = "防禦建倉 (收緊止損, 提早止盈)"
    else:
        if jp_20ma_pct > 20:
            jp_regime = "🟠 **防禦微熊 (Mild Bear)**"
            jp_action = "僅限超賣撈底"
        else:
            jp_regime = "🔴 **凜冬熊市 (Bear)**"
            jp_action = "暫停突破建倉"

    # 💡 保留 UAT/Production 統一的詳細矩陣數據
    us_macro_str = f"狀態: {us_regime}\n🔸 盤長(>200MA): **{us_matrix['index_200ma_pct']}%**\n🔸 盤中(>50MA): **{us_matrix['index_50ma_pct']}%**\n🔸 總中(>50MA): **{us_matrix['total_50ma_pct']}%**\n🔸 超賣(>20MA): **{us_matrix['total_20ma_pct']}%**\n🛑 派發: **{us_dist} 日** | 掃描: {us_scan_count}"
    jp_macro_str = f"狀態: {jp_regime}\n🔸 盤長(>200MA): **{jp_matrix['index_200ma_pct']}%**\n🔸 盤中(>50MA): **{jp_matrix['index_50ma_pct']}%**\n🔸 總中(>50MA): **{jp_matrix['total_50ma_pct']}%**\n🔸 超賣(>20MA): **{jp_matrix['total_20ma_pct']}%**\n🛑 派發: **{jp_dist} 日** | 掃描: {jp_scan_count}"

    # 7. 發送 Payload (保留 UAT 專屬時光機 Footer)
    # =========================================================================
    # 🌍 Discord Summary 排版與 Worst-Case 顏色判定
    # =========================================================================
    # 💡 完美結合：直接使用 MODULE 3 計算好嘅 us_regime / jp_regime 變數！
    us_macro_str = f"狀態: {us_regime}\n🔸 盤長(>200MA): **{us_matrix['index_200ma_pct']}%**\n🔸 盤中(>50MA): **{us_matrix['index_50ma_pct']}%**\n🔸 總中(>50MA): **{us_matrix['total_50ma_pct']}%**\n🔸 超賣(>20MA): **{us_matrix['total_20ma_pct']}%**\n🛑 派發: **{us_dist} 日** | 掃描: {us_scan_count}"
    jp_macro_str = f"狀態: {jp_regime}\n🔸 盤長(>200MA): **{jp_matrix['index_200ma_pct']}%**\n🔸 盤中(>50MA): **{jp_matrix['index_50ma_pct']}%**\n🔸 總中(>50MA): **{jp_matrix['total_50ma_pct']}%**\n🔸 超賣(>20MA): **{jp_matrix['total_20ma_pct']}%**\n🛑 派發: **{jp_dist} 日** | 掃描: {jp_scan_count}"

    # Summary 條 Bar 採取 Worst-Case (風險最高者優先)
    summary_embed_color = us_macro_color if us_risk_rank >= jp_risk_rank else jp_macro_color

    payload = {
        "embeds": [{
            "title": f"📊 系統戰績與 3D 矩陣雷達 ({today_str})", 
            "description": f"**今日結案動態:**\n{details_text}\n{group_summary_text}\n\n**🔍 各策略歷史表現:**\n{breakdown_text}",
            "color": summary_embed_color,
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
    
    try: 
        requests.post(DISCORD_SUMMARY_WEBHOOK, json=payload)
    except Exception as e: 
        pass

# =============================================================================
# MODULE 8 — 生成 UAT 前端 HTML (雙分頁系統：Dashboard + Journal)
# =============================================================================
if not IS_FINAL_RUN:
    try:
        with open(INFO_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(STOCK_INFO_CACHE, f, ensure_ascii=False)
    except Exception:
        pass
    print(f"⏭️ [{today_str}] 中途回測，略過 HTML 生成")
    raise SystemExit(0)

# =============================================================================
# MODULE 6 — 大市主題與板塊熱度統計 (Market Themes & Sector Heat)
# =============================================================================
sector_performance = {}
stealth_hot_stocks = []

for ticker in valid_tickers:
    try:
        rs = dict_rs.get(ticker, 0)
        rs_mom = dict_mom.get(ticker, 0)
        cp = float(current_prices.get(ticker, 0))
        c_vol = dict_curr_vol.get(ticker, 0)
        v_ma20 = dict_vol_ma20.get(ticker, 1)
        
        # 篩選條件：RS極高 (>85) 且 近期動能向上 (>0) 且 成交量放大 (>1.5倍)
        if rs >= 85 and rs_mom > 2 and c_vol > (v_ma20 * 1.5):
            s_info = get_stock_info(ticker)
            sector = s_info['sector']
            mcap = s_info['mcap']
            
            # 統計板塊熱度
            if sector not in sector_performance:
                sector_performance[sector] = {'count': 0, 'tickers': []}
            sector_performance[sector]['count'] += 1
            sector_performance[sector]['tickers'].append(ticker)
            
            # 收集潛力異動股
            stealth_hot_stocks.append({
                'ticker': ticker,
                'sector': sector,
                'rs': round(rs, 0),
                'mom': round(rs_mom, 1),
                'price': cp,
                'unit': "¥" if ticker.endswith(".T") else "$"
            })
    except Exception:
        pass

# 按板塊熱度（強勢股數量）排序
sorted_sectors = sorted(sector_performance.items(), key=lambda x: x[1]['count'], reverse=True)
stealth_hot_stocks.sort(key=lambda x: x['rs'], reverse=True)

# 轉為 JSON 傳給前端 HTML
themes_data = {
    "sectors": [{"sector": k, "count": v['count'], "tickers": v['tickers'][:5]} for k, v in sorted_sectors[:8]],
    "stocks": stealth_hot_stocks[:20]
}
themes_data_str = json.dumps(themes_data)

# =============================================================================
# 📊 MODULE 6.5 — Benchmark 對照組（A: Buy&Hold SPY / B: 每月 RS Top20）
# =============================================================================
BENCH_TOP_N   = 20
BENCH_MARKETS = os.environ.get("BENCH_MARKETS", "US+JP")   # "US" 或 "US+JP"

print("⏳ 正在計算 Benchmark 對照組...")

_bt_dates   = [t['date'] for t in trade_history if t.get('date')]
bench_start = min(_bt_dates) if _bt_dates else closes.index[0].strftime('%Y-%m-%d')

def _perf(eq):
    if len(eq) < 2: return {'total': 0.0, 'cagr': 0.0, 'mdd': 0.0, '_raw': 0.0}
    total = float(eq.iloc[-1] / eq.iloc[0] - 1)
    mdd   = float((eq / eq.cummax() - 1).min())
    return {'total': round(total*100, 1), 'cagr': 0.0, 'mdd': round(mdd*100, 1), '_raw': total}

bench_result = {}

# --- A. Buy & Hold SPY ---
try:
    _spy = closes['SPY'].loc[bench_start:].dropna()
    _a = _perf(_spy)
    _yrs_a = max(len(_spy) / 252, 0.1)
    _a['cagr'] = round(((1 + _a['_raw']) ** (1/_yrs_a) - 1) * 100, 1)
    _a.pop('_raw', None)
    bench_result['A_SPY'] = _a
except Exception as e:
    bench_result['A_SPY'] = {'total': 0, 'cagr': 0, 'mdd': 0}
    print(f"⚠️ Benchmark A 失敗: {e}")

# --- B. 每月頭買入 RS Top N，等權，持有一個月 ---
try:
    _dv_full = (closes * vols).rolling(20).mean()
    _c  = closes.loc[bench_start:]
    _ix = _c.index.to_series()
    rebal_dates = _ix.groupby([_ix.dt.year, _ix.dt.month]).first().tolist()

    _pool = us_tickers + (jp_tickers if BENCH_MARKETS == "US+JP" else [])
    _pool = [t for t in _pool if t in closes.columns]
    _jp_flag = pd.Series([t.endswith('.T') for t in _pool], index=_pool)

    eq, dates_b, picks_log, m_rets = [1.0], [rebal_dates[0]], [], []

    for i in range(len(rebal_dates) - 1):
        d0, d1 = rebal_dates[i], rebal_dates[i+1]
        rs_row = rs_rank.loc[d0, _pool]
        dv_row = _dv_full.loc[d0, _pool]
        px0, px1 = closes.loc[d0, _pool], closes.loc[d1, _pool]

        # 用同策略一致嘅流動性同仙股過濾
        liq_ok = ((~_jp_flag) & (dv_row >= us_thresh)) | (_jp_flag & (dv_row >= jp_thresh))
        px_ok  = ((~_jp_flag) & (px0 >= 1))           | (_jp_flag & (px0 >= 100))
        ok = liq_ok & px_ok & rs_row.notna() & px0.notna() & px1.notna() & (px0 > 0)

        cand = rs_row[ok].nlargest(BENCH_TOP_N)
        if len(cand) == 0:
            eq.append(eq[-1]); dates_b.append(d1); continue

        rets = (px1[cand.index] / px0[cand.index] - 1)

        # 日股同樣做匯率換算
        if 'JPY=X' in closes.columns:
            fx0, fx1 = closes['JPY=X'].loc[d0], closes['JPY=X'].loc[d1]
            if pd.notna(fx0) and pd.notna(fx1) and fx1 > 0:
                for tk in cand.index:
                    if tk.endswith('.T'):
                        rets[tk] = (1 + rets[tk]) * (fx0 / fx1) - 1

        m_ret = float(rets.mean()) - ROUND_TRIP_COST   # 每月換倉都要交易成本
        m_rets.append(m_ret)
        eq.append(eq[-1] * (1 + m_ret)); dates_b.append(d1)
        picks_log.append({'date': d0.strftime('%Y-%m-%d'),
                          'ret': round(m_ret*100, 2), 'top': list(cand.index[:5])})

    _eq_s = pd.Series(eq, index=pd.DatetimeIndex(dates_b))
    _b = _perf(_eq_s)
    _yrs_b = max(len(m_rets) / 12, 0.1)
    _b['cagr']    = round(((1 + _b['_raw']) ** (1/_yrs_b) - 1) * 100, 1)
    _b['months']  = len(m_rets)
    _b['win_mth'] = round(sum(1 for r in m_rets if r > 0) / max(len(m_rets),1) * 100, 1)
    _b['bp_day']  = round((sum(m_rets)/max(len(m_rets),1)) / 21 * 10000, 2)  # 每日每倉位 bp
    _b.pop('_raw', None)
    bench_result['B_RS20'] = _b
    bench_result['B_picks'] = picks_log[-12:]
except Exception as e:
    bench_result['B_RS20'] = {'total': 0, 'cagr': 0, 'mdd': 0, 'months': 0, 'bp_day': 0}
    print(f"⚠️ Benchmark B 失敗: {e}")

# --- 你的策略（每倉位口徑，方便同 B 直接比）---
_cl = [t for t in trade_history if t.get('status') != 'OPEN']
if _cl:
    _exp_pct  = sum(calc_true_pnl(t) for t in _cl) / len(_cl) / TICKETSIZE
    _avg_days = max(sum(t.get('days_held', 1) for t in _cl) / len(_cl), 1)
    bench_result['strategy'] = {
        'trades': len(_cl),
        'exp_pct': round(_exp_pct * 100, 2),
        'avg_days': round(_avg_days, 1),
        'bp_day': round(_exp_pct / _avg_days * 10000, 2),   # 👈 同 B 同一把尺
    }

print("\n" + "="*68)
print(f"📊 BENCHMARK 對照（由 {bench_start} 起）")
_a, _b2 = bench_result['A_SPY'], bench_result['B_RS20']
print(f"   A. Buy&Hold SPY   : 總回報 {_a['total']:>7}% | CAGR {_a['cagr']:>6}% | MaxDD {_a['mdd']:>6}%")
print(f"   B. 每月 RS Top{BENCH_TOP_N}   : 總回報 {_b2['total']:>7}% | CAGR {_b2['cagr']:>6}% | MaxDD {_b2['mdd']:>6}%"
      f" | {_b2.get('months',0)} 個月 | {_b2.get('bp_day',0)} bp/日/倉")
if 'strategy' in bench_result:
    _s = bench_result['strategy']
    print(f"   C. 你的策略        : 每單 {_s['exp_pct']:>6}% | {_s['trades']} 單 "
          f"| 平均持倉 {_s['avg_days']} 日 | {_s['bp_day']} bp/日/倉")
print("="*68 + "\n")

bench_data_str = json.dumps(bench_result)

print("⏳ [8/8] 正在生成雙分頁量化儀表板...")

def get_unit(tk): return "¥" if tk.endswith(".T") else "$"

# 👇 新增：準備歷史走勢圖表數據 (最近 400 日)
print("⏳ 正在生成歷史宏觀走勢圖表數據...")
hist_dates = closes.index[-400:]

# 1. 🛡️ 強制全局 ffill()，填補美日假期交錯導致的 NaN 漏洞
c_us_valid = closes[us_tickers].ffill()
c_us_idx_valid = closes[us_index_tickers].ffill()
c_jp_valid = closes[jp_tickers].ffill()
c_jp_idx_valid = closes[jp_index_tickers].ffill()

spy_c = closes['SPY'].ffill() if 'SPY' in closes.columns else None
spy_200 = spy_c.rolling(200, min_periods=100).mean().ffill() if spy_c is not None else None

n225_c = closes['^N225'].ffill() if '^N225' in closes.columns else None
n225_200 = n225_c.rolling(200, min_periods=100).mean().ffill() if n225_c is not None else None

# 2. 🛡️ 智能市寬函數 (處理分母，防 0 防 NaN)
def get_hist_breadth(price_df, ma_window, min_periods):
    ma_df = price_df.rolling(ma_window, min_periods=min_periods).mean().ffill()
    valid_counts = ma_df.notna().sum(axis=1).replace(0, 1) # 避免除以 0
    return (price_df > ma_df).sum(axis=1) / valid_counts * 100

v_us_tot20 = get_hist_breadth(c_us_valid, 20, 10)
v_us_tot50 = get_hist_breadth(c_us_valid, 50, 25)
v_us_idx50 = get_hist_breadth(c_us_idx_valid, 50, 25)
v_us_idx200 = get_hist_breadth(c_us_idx_valid, 200, 100)

v_jp_tot20 = get_hist_breadth(c_jp_valid, 20, 10)
v_jp_tot50 = get_hist_breadth(c_jp_valid, 50, 25)
v_jp_idx50 = get_hist_breadth(c_jp_idx_valid, 50, 25)
v_jp_idx200 = get_hist_breadth(c_jp_idx_valid, 200, 100)

chart_data = []
for i, d in enumerate(hist_dates):
    d_str = d.strftime('%Y-%m-%d')
    us_open_profit, us_open_loss = 0, 0
    jp_open_profit, jp_open_loss = 0, 0
    
    strat_counts = {"VCP": 0, "BB": 0, "GAP": 0, "OVERSOLD": 0}
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
            
            # 2. 計算累積 P&L (只計當日或之前已經結案的單)
            if c_date <= d_str and t.get('status') != 'OPEN':
                pnl = calc_true_pnl(t) 
                tag = t.get('tag', '')
                if 'VCP' in tag: cum_pnl['VCP'] += pnl
                elif 'BB' in tag: cum_pnl['BB'] += pnl
                elif '缺口' in tag: cum_pnl['GAP'] += pnl
                elif '超賣' in tag: cum_pnl['OVERSOLD'] += pnl

    # 3. 🌟 徹底對齊 4 色燈宏觀邏輯 (Bull, Mild Bull, Mild Bear, Bear)
    us_c_color = "#22c55e" # 預設綠燈
    if spy_c is not None and spy_200 is not None and d in spy_c.index:
        if spy_c.loc[d] > spy_200.loc[d]:
            us_c_color = "#22c55e" if v_us_idx50.loc[d] > 60 else "#eab308" # 綠 或 黃
        else:
            us_c_color = "#f97316" if v_us_tot20.loc[d] > 20 else "#ef4444" # 橙 或 紅

    jp_c_color = "#22c55e" # 預設綠燈
    if n225_c is not None and n225_200 is not None and d in n225_c.index:
        if n225_c.loc[d] > n225_200.loc[d]:
            jp_c_color = "#22c55e" if v_jp_idx50.loc[d] > 60 else "#eab308" # 綠 或 黃
        else:
            jp_c_color = "#f97316" if v_jp_tot20.loc[d] > 20 else "#ef4444" # 橙 或 紅
        
    chart_data.append({
        'date': d_str,
        'us_idx_breadth': round(float(v_us_idx50.loc[d]), 1), 'us_tot_breadth': round(float(v_us_tot50.loc[d]), 1),
        'us_open_profit': us_open_profit, 'us_open_loss': us_open_loss, 'us_color': us_c_color,
        'jp_idx_breadth': round(float(v_jp_idx50.loc[d]), 1), 'jp_tot_breadth': round(float(v_jp_tot50.loc[d]), 1),
        'jp_open_profit': jp_open_profit, 'jp_open_loss': jp_open_loss, 'jp_color': jp_c_color,
        
        'strat_vcp': strat_counts['VCP'], 'strat_bb': strat_counts['BB'],
        'strat_gap': strat_counts['GAP'], 'strat_oversold': strat_counts['OVERSOLD'],
        
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
ticker_map_str = json.dumps(TICKER_MAP)

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
                    <button id="tabBtn-search" onclick="switchTab('search')" class="text-slate-400 hover:text-white hover:bg-slate-800 px-4 py-1.5 rounded-md font-bold text-sm transition">🔍 代號查詢 (Search)</button>
                    <button id="tabBtn-themes" onclick="switchTab('themes')" class="text-slate-400 hover:text-white hover:bg-slate-800 px-4 py-1.5 rounded-md font-bold text-sm transition">🔥 大市主題 (Themes)</button>
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

    <!-- 🔍 全新加入：代號查詢分頁 (Search Tab) -->
    <main id="tab-search" class="hidden flex-1 overflow-y-auto bg-slate-900 rounded-xl border border-slate-800 p-6 z-10 flex flex-col gap-6 shadow-lg">
        <div class="flex justify-between items-center border-b border-slate-800 pb-2">
            <h2 class="text-2xl font-black text-white flex items-center gap-2">🔍 股票範圍與歷史交易快速查詢</h2>
            <div class="text-xs text-slate-500">輸入代號即時檢索觀察範圍與過往戰績</div>
        </div>

        <!-- 搜尋輸入列 -->
        <div class="bg-slate-800/30 rounded-xl border border-slate-700 p-4 flex gap-3 items-center shadow-lg">
            <input type="text" id="search-ticker-input" placeholder="輸入代號 (例如: AAPL, MSFT, 7203.T)" class="bg-slate-900 border border-slate-600 text-white text-sm px-4 py-2 rounded-lg w-72 uppercase outline-none focus:border-fuchsia-500 font-bold" onkeyup="if(event.key === 'Enter') performTickerSearch()">
            <button onclick="performTickerSearch()" class="bg-fuchsia-600 hover:bg-fuchsia-500 text-white px-6 py-2 rounded-lg text-sm font-black transition shadow-md">立即檢索</button>
        </div>

        <!-- 1. 觀察範圍檢索結果 -->
        <div id="search-scope-card" class="bg-slate-800/30 p-4 rounded-xl border border-slate-700">
            <div class="text-xs font-bold text-slate-400 uppercase tracking-wider mb-2">🌐 觀察範圍狀態 (Watchlist Scope)</div>
            <div id="scope-status-content" class="text-sm font-bold text-slate-500 italic">請於上方輸入股票代號並點擊檢索...</div>
        </div>

        <!-- 2. 過往交易紀錄檢索結果 -->
        <div class="bg-slate-800/30 rounded-xl border border-slate-700 p-4 flex-1 flex flex-col">
            <div class="text-xs font-bold text-slate-400 uppercase tracking-wider mb-3">📜 該代號之歷史交易與持倉紀錄 (Trade History)</div>
            <div class="overflow-x-auto flex-1">
                <table class="w-full text-xs text-left whitespace-nowrap">
                    <thead class="text-slate-500 uppercase border-b border-slate-700 bg-slate-800/50">
                        <tr>
                            <th class="p-2">買入日期</th><th class="p-2">平倉日期</th><th class="p-2">策略</th>
                            <th class="p-2 text-center">狀態</th><th class="p-2">買入價</th><th class="p-2">賣出/現價</th>
                            <th class="p-2 text-right">實現/浮動 P&L</th><th class="p-2 text-right">回報 (%)</th>
                        </tr>
                    </thead>
                    <tbody id="search-history-tbody">
                        <tr><td colspan="8" class="p-4 text-center text-slate-500 italic">尚無檢索資料</td></tr>
                    </tbody>
                </table>
            </div>
        </div>
    </main>

    <!-- 🔥 全新加入：大市主題與熱話掃描 Tab -->
    <main id="tab-themes" class="hidden flex-1 overflow-y-auto bg-slate-900 rounded-xl border border-slate-800 p-6 z-10 flex flex-col gap-6 shadow-lg">
        <div class="flex justify-between items-center border-b border-slate-800 pb-2">
            <h2 class="text-2xl font-black text-white flex items-center gap-2">🔥 大市隱形主題與板塊資金流向</h2>
            <div class="text-xs text-slate-500">捕捉機構資金悄悄流入、主流媒體尚未大肆宣傳的熱話板塊</div>
        </div>

        <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <!-- 1. 板塊資金熱度榜 -->
            <div class="bg-slate-800/30 rounded-xl border border-slate-700 p-4 flex flex-col">
                <h3 class="font-black text-fuchsia-400 mb-3 flex items-center gap-2">📊 資金正湧入的熱話板塊 (Sector Heatmap)</h3>
                <div class="overflow-x-auto flex-1">
                    <table class="w-full text-xs text-left whitespace-nowrap">
                        <thead class="text-slate-500 uppercase border-b border-slate-700 bg-slate-800/50">
                            <tr><th class="p-2">板塊 (Sector)</th><th class="p-2 text-center">強勢股數量</th><th class="p-2">領頭羊代表</th></tr>
                        </thead>
                        <tbody id="themes-sector-tbody">
                            <!-- JS 動態填入 -->
                        </tbody>
                    </table>
                </div>
            </div>

            <!-- 2. 潛力異動突破股 (Stealth Surging) -->
            <div class="bg-slate-800/30 rounded-xl border border-slate-700 p-4 flex flex-col">
                <h3 class="font-black text-amber-400 mb-3 flex items-center gap-2">🚀 潛力異動爆發股 (Stealth Momentum)</h3>
                <div class="overflow-x-auto flex-1">
                    <table class="w-full text-xs text-left whitespace-nowrap">
                        <thead class="text-slate-500 uppercase border-b border-slate-700 bg-slate-800/50">
                            <tr><th class="p-2">代號</th><th class="p-2">板塊</th><th class="p-2 text-center">RS 評分</th><th class="p-2 text-center">動能變化</th><th class="p-2 text-right">現價</th></tr>
                        </thead>
                        <tbody id="themes-stocks-tbody">
                            <!-- JS 動態填入 -->
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </main>

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
                        <!-- 👇 特徵標籤 👇 -->
                        <div class="flex flex-wrap items-center gap-1 mt-1.5">
                            {'<span class="text-[8px] bg-purple-500/20 text-purple-300 px-1 rounded border border-purple-500/30">🛡️ MSS</span>' if d.get('has_mss') else ''}
                            {'<span class="text-[8px] bg-sky-500/20 text-sky-300 px-1 rounded border border-sky-500/30">🐋 SMC</span>' if d.get('has_smc') else ''}
                            {'<span class="text-[8px] bg-orange-500/20 text-orange-300 px-1 rounded border border-orange-500/30">🔄 AMD</span>' if d.get('has_amd') else ''}
                            <span class="text-[8px] bg-slate-700/50 text-slate-300 px-1 rounded border border-slate-600">🧠 {d.get('ml_rsi')}</span>
                        </div>
                        <div class="flex justify-between text-[9px] mt-1.5 pt-1.5 border-t border-slate-700/50">
                            <span class="text-emerald-400 font-mono">🎯 TP: {d['tp_txt']} {d['tp_pct']}</span>
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
                        <!-- 👇 特徵標籤 👇 -->
                        <div class="flex flex-wrap items-center gap-1 mt-1.5">
                            {'<span class="text-[8px] bg-purple-500/20 text-purple-300 px-1 rounded border border-purple-500/30">🛡️ MSS</span>' if d.get('has_mss') else ''}
                            {'<span class="text-[8px] bg-sky-500/20 text-sky-300 px-1 rounded border border-sky-500/30">🐋 SMC</span>' if d.get('has_smc') else ''}
                            {'<span class="text-[8px] bg-orange-500/20 text-orange-300 px-1 rounded border border-orange-500/30">🔄 AMD</span>' if d.get('has_amd') else ''}
                            <span class="text-[8px] bg-slate-700/50 text-slate-300 px-1 rounded border border-slate-600">🧠 {d.get('ml_rsi')}</span>
                        </div>
                        <div class="flex justify-between text-[9px] mt-1.5 pt-1.5 border-t border-slate-700/50">
                            <span class="text-emerald-400 font-mono">🎯 TP: {d['tp_txt']} {d['tp_pct']}</span>
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
                        <input type="number" id="acc_size" value="{TICKETSIZE}" class="bg-slate-800 border border-slate-600 text-white text-xs px-2 py-1 rounded w-24 text-right focus:outline-none focus:border-amber-500" onchange="updateCalculator()" onkeyup="updateCalculator()">
                    </div>
                </div>
                <div class="grid grid-cols-5 gap-3 text-center">
                    <div class="bg-slate-800/50 p-2 rounded-lg border border-slate-700">
                        <div class="text-[9px] text-slate-400 uppercase font-bold">進場現價</div>
                        <div class="font-black text-white text-lg" id="calc_entry">-</div>
                    </div>
                    <div class="bg-red-900/10 p-2 rounded-lg border border-red-900/50">
                        <div class="text-[9px] text-red-400 uppercase font-bold">嚴格止損 (1.5-2.0 ATR)</div>
                        <div class="font-black text-red-400 text-lg" id="calc_sl">-</div>
                    </div>
                    <div class="bg-emerald-900/10 p-2 rounded-lg border border-emerald-900/50">
                        <div class="text-[9px] text-emerald-400 uppercase font-bold">目標止盈 (Trailing)</div>
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
            <h2 class="text-2xl font-black text-white flex items-center gap-2">📈 歷史宏觀與持倉走勢 (最近 600 日)</h2>
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
        <div class="grid grid-cols-1 md:grid-cols-3 gap-4 mt-2" id="kpi-scorecard"></div>

        <div class="bg-slate-800/30 rounded-xl border border-slate-700 p-4">
            <div class="flex justify-between items-center mb-3">
                <h3 class="font-black text-lime-400 flex items-center gap-2">🏁 Benchmark 對照 (Reality Check)</h3>
                <div class="text-[10px] text-slate-500">打唔贏笨方法 = 你嗰堆條件冇貢獻</div>
            </div>
            <div class="overflow-x-auto">
                <table class="w-full text-xs text-left whitespace-nowrap">
                    <thead class="text-slate-500 uppercase border-b border-slate-700 bg-slate-800/50">
                        <tr>
                            <th class="p-2">對照組</th><th class="p-2 text-right">總回報</th>
                            <th class="p-2 text-right">CAGR</th><th class="p-2 text-right">MaxDD</th>
                            <th class="p-2 text-right text-amber-300">bp/日/倉位</th>
                        </tr>
                    </thead>
                    <tbody id="bench-tbody"></tbody>
                </table>
            </div>
            <div class="text-[10px] text-slate-500 mt-3 leading-relaxed">
                ⚠️ A/B 係複利 equity 曲線；你嘅策略係固定 $10,000 每單，冇複利，所以 <b>總回報同 CAGR 唔可以直接比</b>。
                真正可比嘅係最右邊嘅 <b>bp/日/倉位</b>（每個倉位每日賺幾多基點）。
            </div>
        </div>

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
            <div>
                <label class="text-[10px] text-slate-400 font-bold uppercase mb-1 block">🧪 樣本期間</label>
                <select id="filter-period" onchange="renderJournal()" class="bg-slate-900 border border-slate-600 text-xs text-white px-3 py-1.5 rounded outline-none focus:border-fuchsia-500">
                    <option value="ALL">全部期間</option>
                    <option value="IS">In-Sample (開發區)</option>
                    <option value="OOS">Out-of-Sample (驗證區)</option>
                    <option value="FWD">Forward (前瞻區)</option>
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
            <!-- 👇 新增：獨立特徵因子分析表 (Feature Factor Matrix) 👇 -->
            <div class="bg-slate-800/30 rounded-xl border border-slate-700 p-4 mt-4">
                <div class="flex justify-between items-center mb-3">
                    <h3 class="font-black text-pink-400 flex items-center gap-2">🧬 獨立特徵因子勝率分析 (Feature Matrix)</h3>
                    <div class="text-[10px] text-slate-500">找出不同策略最依賴的核心推動力</div>
                </div>
                <div class="overflow-x-auto">
                    <table class="w-full text-xs text-left whitespace-nowrap">
                        <thead class="text-slate-500 uppercase border-b border-slate-700 bg-slate-800/50">
                            <tr>
                                <th class="p-2 w-1/4">策略 (Strategy)</th>
                                <th class="p-2 text-center text-purple-300 w-1/4 border-l border-slate-700/50">🛡️ MSS (結構轉變)</th>
                                <th class="p-2 text-center text-sky-300 w-1/4 border-l border-slate-700/50">🐋 SMC (大戶訂單)</th>
                                <th class="p-2 text-center text-orange-300 w-1/4 border-l border-slate-700/50">🔄 AMD (洗盤完成)</th>
                            </tr>
                        </thead>
                        <tbody id="metric-features-tbody"></tbody>
                    </table>
                </div>
            </div>
            <!-- 👇 新增：特定指標組合勝率分析 (Combination Matrix) 👇 -->
            <div class="bg-slate-800/30 rounded-xl border border-slate-700 p-4 mt-4">
                <div class="flex justify-between items-center mb-3">
                    <h3 class="font-black text-amber-400 flex items-center gap-2">🔥 特定指標組合勝率分析 (Combination Matrix)</h3>
                    <div class="text-[10px] text-slate-500">尋找每種策略的「最佳拍檔」組合（已排除單一指標）</div>
                </div>
                <div class="overflow-x-auto">
                    <table class="w-full text-xs text-left whitespace-nowrap">
                        <thead class="text-slate-500 uppercase border-b border-slate-700 bg-slate-800/50">
                            <tr>
                                <th class="p-2 w-1/5">策略 (Strategy)</th>
                                <th class="p-2 text-center w-1/5 border-l border-slate-700/50">🛡️+🐋 MSS + SMC</th>
                                <th class="p-2 text-center w-1/5 border-l border-slate-700/50">🛡️+🔄 MSS + AMD</th>
                                <th class="p-2 text-center w-1/5 border-l border-slate-700/50">🐋+🔄 SMC + AMD</th>
                                <th class="p-2 text-center text-amber-300 w-1/5 border-l border-slate-700/50">S級三核共振 (All 3)</th>
                            </tr>
                        </thead>
                        <tbody id="metric-combination-tbody"></tbody>
                    </table>
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
        const tickerMap = {ticker_map_str}; // 接收 Python 傳入的完整觀察清單
        const themesData = {themes_data_str};
        const benchData = {bench_data_str};
        const TICKETSIZE      = {TICKETSIZE};
        const PARTIAL_TP_PCT  = {PARTIAL_TP_PCT};
        const PARTIAL_PCT     = {PARTIAL_TP_PCT};
        const PARTIAL_TP_R    = {PARTIAL_TP_R};
        const ROUND_TRIP_COST = {ROUND_TRIP_COST};       
        
        let chartsRendered = false; // 👈 確保圖表只渲染一次
        let currentSelectedTicker = null;
        let tvWidget = null;

        function switchTab(tabId) {{
            ['dashboard', 'journal', 'charts', 'search', 'themes'].forEach(id => {{
                const tabEl = document.getElementById('tab-' + id);
                const btnEl = document.getElementById('tabBtn-' + id);
                if (tabEl) tabEl.classList.toggle('hidden', tabId !== id);
                if (btnEl) btnEl.className = tabId === id 
                    ? 'bg-indigo-600 text-white px-4 py-1.5 rounded-md font-bold text-sm shadow-md transition' 
                    : 'text-slate-400 hover:text-white hover:bg-slate-800 px-4 py-1.5 rounded-md font-bold text-sm transition';
            }});

            if (tabId === 'journal') renderJournal();
            if (tabId === 'charts' && !chartsRendered) renderCharts();
            if (tabId === 'themes') renderThemesTab();
        }}

        // 🔍 執行代號檢索的核心邏輯
        function performTickerSearch() {{
            const inputVal = document.getElementById('search-ticker-input').value.trim().toUpperCase();
            if (!inputVal) return;

            // 標準化代號格式匹配 (兼容美股點轉橫線，日股保留 .T)
            let query = inputVal;
            if (!query.endsWith('.T')) {{
                query = query.replace('.', '-');
            }}

            // 1. 檢查是否 In Scope
            const scopeContainer = document.getElementById('search-scope-card');
            const scopeContent = document.getElementById('scope-status-content');
            const sources = tickerMap[query] || tickerMap[inputVal];

            if (sources) {{
                scopeContainer.className = "bg-emerald-950/20 p-4 rounded-xl border border-emerald-500/40 shadow-lg";
                let sourceBadges = sources.map(s => `<span class="bg-emerald-500/20 text-emerald-300 border border-emerald-500/30 px-2 py-0.5 rounded ml-2 font-mono text-xs">${{s}}</span>`).join('');
                scopeContent.innerHTML = `<div class="flex items-center"><span class="text-emerald-400 font-black text-base">🟢 IN SCOPE (在系統觀察範圍內)</span>${{sourceBadges}}</div>`;
            }} else {{
                scopeContainer.className = "bg-red-950/20 p-4 rounded-xl border border-red-500/40 shadow-lg";
                scopeContent.innerHTML = `<span class="text-red-400 font-black text-base">❌ NOT IN SCOPE (不在當前掃描名單內)</span>`;
            }}

            // 2. 抽取出以往的 Trade History
            const matchedTrades = tradeHistory.filter(t => t.tk.toUpperCase() === query || t.tk.toUpperCase() === inputVal);
            const historyTbody = document.getElementById('search-history-tbody');

            if (matchedTrades.length === 0) {{
                historyTbody.innerHTML = `<tr><td colspan="8" class="p-4 text-center text-slate-500 italic">此股票在歷史交易紀錄中沒有任何相關單子</td></tr>`;
                return;
            }}

            historyTbody.innerHTML = matchedTrades.map(t => {{
                let buy_px = t.px;
                let last_px = t.last_px || buy_px;
                let isClosed = t.status !== 'OPEN';
                let pnl = 0;

                if (isClosed) {{
                    pnl = (TICKETSIZE / buy_px) * (last_px - buy_px);
                }} else {{
                    if (t.partial_tp_hit) {{
                        let tp1_price = t.tp1_price || (buy_px + (buy_px - (t.initial_sl || buy_px))*PARTIAL_TP_R);
                        pnl = (TICKETSIZE * PARTIAL_TP_PCT / buy_px) * (tp1_price - buy_px) + (TICKETSIZE * (1 - PARTIAL_TP_PCT) / buy_px) * (last_px - buy_px);
                    }} else {{
                        pnl = (TICKETSIZE / buy_px) * (last_px - buy_px);
                    }}
                }}

                let pnlPct = (pnl / TICKETSIZE * 100).toFixed(2);
                let pColor = pnl >= 0 ? 'text-emerald-400' : 'text-red-400';
                let unit = t.tk.endsWith('.T') ? '¥' : '$';
                let statusBadge = isClosed ? `<span class="text-slate-400 font-bold">${{t.status}}</span>` : `<span class="text-cyan-400 font-black bg-cyan-950/50 px-2 py-0.5 rounded border border-cyan-800">OPEN 持倉中</span>`;

                return `
                <tr class="border-b border-slate-700/50 hover:bg-slate-800 transition">
                    <td class="p-2 text-slate-400">${{t.date}}</td>
                    <td class="p-2">${{t.close_date || '-'}}</td>
                    <td class="p-2 text-[10px] text-slate-400 font-bold">${{t.tag || 'N/A'}}</td>
                    <td class="p-2 text-center">${{statusBadge}}</td>
                    <td class="p-2">${{unit}}${{t.px}}</td>
                    <td class="p-2 text-white font-bold">${{unit}}${{last_px}}</td>
                    <td class="p-2 text-right font-black font-mono ${{pColor}}">${{pnl >= 0 ? '+' : ''}}$${{pnl.toFixed(2)}}</td>
                    <td class="p-2 text-right font-black font-mono ${{pColor}}">${{pnl >= 0 ? '+' : ''}}${{pnlPct}}%</td>
                </tr>`;
            }}).join('');
        }}

function renderThemesTab() {{
            // 1. 渲染板塊熱度榜
            const sectorTbody = document.getElementById('themes-sector-tbody');
            if (themesData.sectors.length === 0) {{
                sectorTbody.innerHTML = `<tr><td colspan="3" class="p-4 text-center text-slate-500">目前沒有偵測到明顯聚集的板塊熱度</td></tr>`;
            }} else {{
                sectorTbody.innerHTML = themesData.sectors.map(s => `
                    <tr class="border-b border-slate-700/50 hover:bg-slate-800 transition">
                        <td class="p-2 font-bold text-white">${{s.sector}}</td>
                        <td class="p-2 text-center font-black text-fuchsia-400">${{s.count}} 隻</td>
                        <td class="p-2 font-mono text-[10px] text-slate-300">${{s.tickers.join(', ')}}</td>
                    </tr>
                `).join('');
            }}

            // 2. 渲染潛力異動股
            const stocksTbody = document.getElementById('themes-stocks-tbody');
            if (themesData.stocks.length === 0) {{
                stocksTbody.innerHTML = `<tr><td colspan="5" class="p-4 text-center text-slate-500">目前沒有符合條件的潛力異動股</td></tr>`;
            }} else {{
                stocksTbody.innerHTML = themesData.stocks.map(st => `
                    <tr class="border-b border-slate-700/50 hover:bg-slate-800 transition cursor-pointer hover:bg-amber-950/20" onclick="loadContent('${{st.ticker}}')">
                        <td class="p-2 font-bold text-white">${{st.ticker}}</td>
                        <td class="p-2 text-[10px] text-slate-400 truncate max-w-[120px]">${{st.sector}}</td>
                        <td class="p-2 text-center font-bold text-cyan-400">${{st.rs}}</td>
                        <td class="p-2 text-center font-bold text-emerald-400">+${{st.mom}}</td>
                        <td class="p-2 text-right font-black font-mono text-white">${{st.unit}}${{st.price}}</td>
                    </tr>
                `).join('');
            }}
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
                    xaxis: {{ categories: dates, labels: {{ style: {{ colors: '#94a3b8' }} }}, tickAmount: 20 }},
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
                    tickAmount: 20
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
                    tickAmount: 20
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

            const accountSize = parseFloat(document.getElementById('acc_size').value) || TICKETSIZE;
            const riskAmount = accountSize * {MAX_ACCOUNT_RISK_PCT};

            let shares = Math.floor(riskAmount / data.risk_per_share);
            if (isJp) shares = Math.floor(shares / 100) * 100;
            if (shares <= 0) shares = 0;

            const totalCost = shares * data.curr_price;
            const actualPosPct = (accountSize > 0) ? (totalCost / accountSize * 100).toFixed(1) : 0;

            document.getElementById('calc_entry').innerText  = unit + data.curr_price.toFixed(2);
            document.getElementById('calc_sl').innerText     = unit + data.sl_price.toFixed(2);
            document.getElementById('calc_tp').innerText     = data.tp_price ? unit + data.tp_price.toFixed(2) : "Trailing 出場";
            document.getElementById('calc_shares').innerText = shares;
            document.getElementById('calc_cost').innerText   = unit + totalCost.toLocaleString(undefined, {{maximumFractionDigits: 0}}) + " (" + actualPosPct + "%)";
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
            // 🌟 唯一嘅 P&L 計算公式，全份 dashboard 只准用呢個
            const calcTruePnl = (t) => {{
                const buy_px  = t.px;
                const last_px = t.last_px || buy_px;
                let pnl;
                if (t.partial_tp_hit) {{
                    const initial_sl = t.initial_sl || buy_px;
                    const tp1_price  = t.tp1_price || (buy_px + (buy_px - initial_sl) * PARTIAL_TP_R);
                    pnl = (PARTIAL_PCT * TICKETSIZE / buy_px) * (tp1_price - buy_px)
                        + ((1 - PARTIAL_PCT) * TICKETSIZE / buy_px) * (last_px - buy_px);
                }} else {{
                    pnl = (TICKETSIZE / buy_px) * (last_px - buy_px);
                }}
                if (t.fx_entry && t.fx_exit) {{
                    pnl = TICKETSIZE * ((1 + pnl / TICKETSIZE) * (t.fx_entry / t.fx_exit) - 1);
                }}
                return pnl - (TICKETSIZE * ROUND_TRIP_COST);   // 👈 交易成本
            }};    

            const openTbody = document.getElementById('journal-open-tbody');
            const closedTbody = document.getElementById('journal-closed-tbody');
            const statsContainer = document.getElementById('journal-stats');

            // 1️⃣ 讀取 Filter 數值 (防呆設計：如果 HTML 未加 Filter UI，就預設 ALL)
            const stratFilter = document.getElementById('filter-strat') ? document.getElementById('filter-strat').value : 'ALL';
            const sourceFilter = document.getElementById('filter-source') ? document.getElementById('filter-source').value : 'ALL';
            const periodFilter = document.getElementById('filter-period') ? document.getElementById('filter-period').value : 'ALL';

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
                let matchPeriod = periodFilter === 'ALL' || t.period === periodFilter;
                return matchStrat && matchSource && matchPeriod;
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
            // 📊 頂部 4 個總計方塊 & 🎯 機構級核心 KPI
            // ==========================================
            let totalClosedPnl = 0, wins = 0, totalOpenPnl = 0;
            
            // --- 新增：核心 KPI 變數 ---
            let grossProfit = 0, grossLoss = 0;
            let cumulativePnl = 0, peakCapital = 0, maxDrawdown = 0;
            
            // 為了準確計算最大回撤 (MDD)，必須建立一個按時間順序排列的陣列
            let chronologicalCloseds = [...closeds].sort((a, b) => {{
                let dateA = a.close_date || a.date;
                let dateB = b.close_date || b.date;
                return dateA.localeCompare(dateB);
            }});

            chronologicalCloseds.forEach(t => {{
                const tradePnl = calcTruePnl(t);
                totalClosedPnl += tradePnl;
                
                if (tradePnl > 0) wins++;

                // 計算 Profit Factor 元素
                if (tradePnl > 0) grossProfit += tradePnl;
                else grossLoss += Math.abs(tradePnl);

                // 計算 Max Drawdown
                cumulativePnl += tradePnl;
                if (cumulativePnl > peakCapital) peakCapital = cumulativePnl;
                let drawdown = peakCapital - cumulativePnl;
                if (drawdown > maxDrawdown) maxDrawdown = drawdown;
            }});
            
            opens.forEach(t => {{
                let buy_px = t.px;
                let last_px = t.last_px;
                // 🌟 混合會計公式：xx% 已鎖定，xx% 隨現價浮動
                let tp1 = t.tp1_price || (buy_px + (buy_px - (t.initial_sl || buy_px))*PARTIAL_TP_R); 
                let pnl = t.partial_tp_hit ? 
                    ((TICKETSIZE * PARTIAL_TP_PCT / buy_px) * (tp1 - buy_px) + (TICKETSIZE * (1 - PARTIAL_TP_PCT) / buy_px) * (last_px - buy_px)) :
                    (TICKETSIZE / buy_px) * (last_px - buy_px);
                totalOpenPnl += pnl;
            }});

            // --- 計算 KPI 最終數值 ---
            const totalClosedCount = closeds.length;
            const profitFactor = grossLoss > 0 ? (grossProfit / grossLoss).toFixed(2) : "999.99";
            const winRateDec = totalClosedCount > 0 ? (wins / totalClosedCount) : 0;
            const lossRateDec = 1 - winRateDec;
            const avgWin = wins > 0 ? (grossProfit / wins) : 0;
            const avgLoss = (totalClosedCount - wins) > 0 ? (grossLoss / (totalClosedCount - wins)) : 0;
            const expectancy = ((winRateDec * avgWin) - (lossRateDec * avgLoss)).toFixed(2);

            // --- 渲染原本的 4 個舊方塊 ---
            const winRate = totalClosedCount > 0 ? (winRateDec * 100).toFixed(1) : 0;
            const closedPct = totalClosedCount > 0 ? ((totalClosedPnl / (totalClosedCount * TICKETSIZE)) * 100).toFixed(2) : "0.00";
            const openPct = opens.length > 0 ? ((totalOpenPnl / (opens.length * TICKETSIZE)) * 100).toFixed(2) : "0.00";

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
                    <div class="text-[9px] text-slate-500 mt-1">${{wins}} 贏 / ${{totalClosedCount - wins}} 輸</div>
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

            // --- 渲染新增的 3 個 KPI 方塊 ---
            const kpiContainer = document.getElementById('kpi-scorecard');
            if (kpiContainer) {{
                const pfColor = profitFactor >= 2 ? 'text-fuchsia-400' : (profitFactor >= 1.5 ? 'text-emerald-400' : 'text-amber-400');
                const expColor = expectancy > 150 ? 'text-emerald-400' : 'text-amber-400';
                const mddColor = maxDrawdown < 15000 ? 'text-emerald-400' : 'text-red-400';

                kpiContainer.innerHTML = `
                    <div class="bg-slate-800/50 p-5 rounded-xl border border-slate-700 relative overflow-hidden shadow-lg">
                        <div class="absolute -right-4 -top-4 opacity-10 text-6xl">⚖️</div>
                        <div class="text-[10px] text-slate-400 uppercase font-black tracking-widest mb-1">獲利因子 (Profit Factor)</div>
                        <div class="text-3xl font-black ${{pfColor}}">${{profitFactor}} <span class="text-sm font-bold text-slate-500">x</span></div>
                        <div class="text-[10px] text-slate-500 mt-2 font-bold">總利潤 ÷ 總虧損。量度系統的純粹攻擊力。</div>
                    </div>
                    <div class="bg-slate-800/50 p-5 rounded-xl border border-slate-700 relative overflow-hidden shadow-lg">
                        <div class="absolute -right-4 -top-4 opacity-10 text-6xl">🎯</div>
                        <div class="text-[10px] text-slate-400 uppercase font-black tracking-widest mb-1">數學期望值 (Expectancy)</div>
                        <div class="text-3xl font-black ${{expColor}}">+$${{expectancy}} <span class="text-sm font-bold text-slate-500">/ 單</span></div>
                        <div class="text-[10px] text-slate-500 mt-2 font-bold">每進行一次交易，預期帶來的淨利。</div>
                    </div>
                    <div class="bg-slate-800/50 p-5 rounded-xl border border-slate-700 relative overflow-hidden shadow-lg">
                        <div class="absolute -right-4 -top-4 opacity-10 text-6xl">🛡️</div>
                        <div class="text-[10px] text-slate-400 uppercase font-black tracking-widest mb-1">最大回撤 (Max Drawdown)</div>
                        <div class="text-3xl font-black ${{mddColor}}">-$${{maxDrawdown.toFixed(0)}}</div>
                        <div class="text-[10px] text-slate-500 mt-2 font-bold">歷史上遭遇過最嚴重的資金滑落。</div>
                    </div>
                `;
            }}

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
                
                const tradePnl = calcTruePnl(t);
                strategyStats[strat].pnl += tradePnl;
                strategyStats[strat].deployed += TICKETSIZE;
                if (tradePnl > 0) strategyStats[strat].wins += 1;
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
                const tradePnl = calcTruePnl(t);
                const isWin = (tradePnl > 0);
                
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
            // 🧬 獨立特徵因子分析 (Feature Matrix) 計算
            // ==========================================
            const featureStats = {{}};
            const ALL_STRATS = ["🏆 VCP 突破", "💥 BB 擠壓", "⚡ 缺口動能", "📉 極度超賣"];
            
            ALL_STRATS.forEach(s => {{
                featureStats[s] = {{
                    'mss': {{ t: 0, w: 0, pnl: 0 }},
                    'smc': {{ t: 0, w: 0, pnl: 0 }},
                    'amd': {{ t: 0, w: 0, pnl: 0 }}
                }};
            }});

            closeds.forEach(t => {{
                const strat = t.tag;
                if (!strat || !featureStats[strat]) return;
                
                const tradePnl = calcTruePnl(t);
                const isWin = (tradePnl > 0);
                
                if (t.features) {{
                    if (t.features.mss) {{ featureStats[strat].mss.t++; if(isWin) featureStats[strat].mss.w++; featureStats[strat].mss.pnl += tradePnl; }}
                    if (t.features.smc) {{ featureStats[strat].smc.t++; if(isWin) featureStats[strat].smc.w++; featureStats[strat].smc.pnl += tradePnl; }}
                    if (t.features.amd) {{ featureStats[strat].amd.t++; if(isWin) featureStats[strat].amd.w++; featureStats[strat].amd.pnl += tradePnl; }}
                }}
            }});

            const formatFeatureCell = (stat) => {{
                if (stat.t === 0) return `<td class="p-2 text-center text-slate-600 text-[10px] border-l border-slate-700/50">無數據</td>`;
                const winRate = ((stat.w / stat.t) * 100).toFixed(1);
                const pColor = stat.pnl >= 0 ? 'text-emerald-400' : 'text-red-400';
                const pSign = stat.pnl >= 0 ? '+' : '';
                return `
                    <td class="p-2 text-center border-l border-slate-700/50 hover:bg-slate-700/30 transition">
                        <div class="font-black text-white text-sm mb-0.5">${{winRate}}%</div>
                        <div class="text-[9px] text-slate-400 mb-1">${{stat.w}} 贏 / ${{stat.t}} 單</div>
                        <div class="font-black font-mono ${{pColor}} text-[10px] bg-slate-900/50 inline-block px-2 py-0.5 rounded">${{pSign}}$${{stat.pnl.toFixed(0)}}</div>
                    </td>
                `;
            }};

            const featuresTbody = document.getElementById('metric-features-tbody');
            if (featuresTbody) {{
                featuresTbody.innerHTML = ALL_STRATS.map(strat => `
                    <tr class="border-b border-slate-700/50 hover:bg-slate-800 transition">
                        <td class="p-2 font-bold text-white bg-slate-900/20">${{strat}}</td>
                        ${{formatFeatureCell(featureStats[strat].mss)}}
                        ${{formatFeatureCell(featureStats[strat].smc)}}
                        ${{formatFeatureCell(featureStats[strat].amd)}}
                    </tr>
                `).join('');
            }}

            // ==========================================
            // 🔥 特定指標組合 (Combination Matrix) 計算
            // ==========================================
            const comboStats = {{}};
            
            ALL_STRATS.forEach(s => {{
                comboStats[s] = {{
                    'mss_smc': {{ t: 0, w: 0, pnl: 0 }}, // 只中 MSS + SMC
                    'mss_amd': {{ t: 0, w: 0, pnl: 0 }}, // 只中 MSS + AMD
                    'smc_amd': {{ t: 0, w: 0, pnl: 0 }}, // 只中 SMC + AMD
                    'all_3': {{ t: 0, w: 0, pnl: 0 }}    // 3個全中
                }};
            }});

            closeds.forEach(t => {{
                const strat = t.tag;
                if (!strat || !comboStats[strat]) return;
                
                const tradePnl = calcTruePnl(t);
                const isWin = (tradePnl > 0);
                
                if (t.features) {{
                    // 將 Boolean 值轉做 true/false 方便判定
                    const hasMSS = !!t.features.mss;
                    const hasSMC = !!t.features.smc;
                    const hasAMD = !!t.features.amd;
                    
                    // 精準將單子分類落對應嘅特定組合 (Mutually Exclusive)
                    if (hasMSS && hasSMC && hasAMD) {{
                        comboStats[strat].all_3.t++;
                        if(isWin) comboStats[strat].all_3.w++;
                        comboStats[strat].all_3.pnl += tradePnl;
                    }} else if (hasMSS && hasSMC && !hasAMD) {{
                        comboStats[strat].mss_smc.t++;
                        if(isWin) comboStats[strat].mss_smc.w++;
                        comboStats[strat].mss_smc.pnl += tradePnl;
                    }} else if (hasMSS && !hasSMC && hasAMD) {{
                        comboStats[strat].mss_amd.t++;
                        if(isWin) comboStats[strat].mss_amd.w++;
                        comboStats[strat].mss_amd.pnl += tradePnl;
                    }} else if (!hasMSS && hasSMC && hasAMD) {{
                        comboStats[strat].smc_amd.t++;
                        if(isWin) comboStats[strat].smc_amd.w++;
                        comboStats[strat].smc_amd.pnl += tradePnl;
                    }}
                }}
            }});

            const comboTbody = document.getElementById('metric-combination-tbody');
            if (comboTbody) {{
                comboTbody.innerHTML = ALL_STRATS.map(strat => `
                    <tr class="border-b border-slate-700/50 hover:bg-slate-800 transition">
                        <td class="p-2 font-bold text-white bg-slate-900/20">${{strat}}</td>
                        ${{formatFeatureCell(comboStats[strat].mss_smc)}}
                        ${{formatFeatureCell(comboStats[strat].mss_amd)}}
                        ${{formatFeatureCell(comboStats[strat].smc_amd)}}
                        ${{formatFeatureCell(comboStats[strat].all_3)}}
                    </tr>
                `).join('');
            }}

            // ==========================================
            // 📂 3. 渲染 Open Positions (加入過濾/排序/板塊/市值/xx%)
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
                    let tp1_price = t.tp1_price || (buy_px + (buy_px - (t.initial_sl || buy_px)) * PARTIAL_TP_R);
                    let pnl_closed = (TICKETSIZE * PARTIAL_TP_PCT / buy_px) * (tp1_price - buy_px);
                    let pnl_floating = (TICKETSIZE * (1 - PARTIAL_TP_PCT) / buy_px) * (last_px - buy_px);
                    pnl = pnl_closed + pnl_floating; 
                }} else {{
                    pnl = (TICKETSIZE / buy_px) * (last_px - buy_px);
                }}
                
                let pnlPct = (pnl / TICKETSIZE * 100).toFixed(2);
                const pColor = pnl >= 0 ? 'text-emerald-400' : 'text-red-400';
                
                // 1. 動態生成 Source 標籤
                let sourceBadges = (t.sources || []).map(s => `<span class="text-[8px] bg-blue-500/20 text-blue-300 px-1 rounded ml-1 border border-blue-500/30">${{s}}</span>`).join('');
                
                // 2. 👇 動態生成高階量化標籤 (Smart Badges)
                let featureBadges = '';
                if (t.features) {{
                    if (t.features.mss) featureBadges += `<span class="text-[8px] bg-purple-500/20 text-purple-300 px-1 rounded ml-1 border border-purple-500/30" title="Market Structure Shift">🛡️ MSS</span>`;
                    if (t.features.smc) featureBadges += `<span class="text-[8px] bg-sky-500/20 text-sky-300 px-1 rounded ml-1 border border-sky-500/30" title="Smart Money Order Block">🐋 SMC</span>`;
                    if (t.features.amd) featureBadges += `<span class="text-[8px] bg-orange-500/20 text-orange-300 px-1 rounded ml-1 border border-orange-500/30" title="Accumulation/Manipulation">🔄 AMD</span>`;
                    if (t.features.ml_rsi) featureBadges += `<span class="text-[8px] bg-pink-500/20 text-pink-300 px-1 rounded ml-1 border border-pink-500/30">🧠 ML-RSI: ${{t.features.ml_rsi}}</span>`;
                }}

                return `
                <tr class="border-b border-slate-700/50 hover:bg-slate-800 transition">
                    <td class="p-2">${{t.date}}</td>
                    <td class="p-2 font-bold text-white">${{t.tk}}</td>
                    
                    <!-- 👇 將 featureBadges 加埋入去 -->
                    <td class="p-2 flex flex-wrap items-center gap-1 mt-1">
                        <span class="text-[9px] bg-slate-700 px-1 rounded">${{t.tag || 'N/A'}}</span>
                        ${{sourceBadges}}
                        ${{featureBadges}}
                    </td>
                    
                    <td class="p-2 text-[10px] text-slate-400 truncate max-w-[100px]">${{t.sector || 'N/A'}}</td>
                    <td class="p-2 text-[10px] text-slate-400 font-mono text-right">${{formatMcap(t.mcap)}}</td>
                    <td class="p-2 text-center">
                        ${{t.partial_tp_hit 
                            ? '<span class="text-amber-400 bg-amber-400/10 px-2 py-0.5 rounded border border-amber-500/20 text-[10px] font-black">🎯 ' + Math.round(PARTIAL_PCT*100) + '% 已止盈 (' + Math.round((1-PARTIAL_PCT)*100) + '% 放飛)</span>'
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
                const pnl = calcTruePnl(t);
                const pnlPct = (pnl / TICKETSIZE * 100).toFixed(2);
                const isWin = (pnl > 0);
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
                        if (t.status.includes("⏱️")) return '<span class="text-slate-400 font-bold">⏱️ 時間止損</span>'; 
                        if (t.status.includes("✅")) return '<span class="text-emerald-400 font-bold">🎯 止盈</span>';
                        return '<span class="text-red-400 font-bold">🛑 止損</span>';
                    }})()}}</td>
                    <td class="p-2">${{unit}}${{t.px}}</td>
                    <td class="p-2 text-white font-bold">${{unit}}${{t.last_px}}</td>
                    <td class="p-2 text-right font-black font-mono ${{pColor}}">${{pnl >= 0 ? '+' : ''}}${{pnl.toFixed(2)}}</td>
                    <td class="p-2 text-right font-black font-mono ${{pColor}}">${{pnl >= 0 ? '+' : ''}}${{pnlPct}}%</td>
                </tr>`;
            }}).join('');

            const benchTbody = document.getElementById('bench-tbody');
            if (benchTbody && typeof benchData !== 'undefined') {{
                const _row = (name, d, hi) => {{
                    if (!d) return '';
                    const c = (d.total >= 0) ? 'text-emerald-400' : 'text-red-400';
                    return `<tr class="border-b border-slate-700/50 ${{hi ? 'bg-lime-500/5' : ''}}">
                        <td class="p-2 font-bold text-white">${{name}}</td>
                        <td class="p-2 text-right font-mono ${{c}}">${{d.total != null ? d.total + '%' : '-'}}</td>
                        <td class="p-2 text-right font-mono ${{c}}">${{d.cagr != null ? d.cagr + '%' : '-'}}</td>
                        <td class="p-2 text-right font-mono text-red-400">${{d.mdd != null ? d.mdd + '%' : '-'}}</td>
                        <td class="p-2 text-right font-black font-mono text-amber-300">${{d.bp_day != null ? d.bp_day : '-'}}</td>
                    </tr>`;
                }};
                let h = _row('A · Buy &amp; Hold SPY', benchData.A_SPY, false)
                      + _row('B · 每月 RS Top20', benchData.B_RS20, false);
                if (benchData.strategy) {{
                    const s = benchData.strategy;
                    h += `<tr class="border-b border-slate-700/50 bg-lime-500/10">
                        <td class="p-2 font-black text-lime-300">C · 你的策略</td>
                        <td class="p-2 text-right font-mono text-slate-400" colspan="3">
                            每單 ${{s.exp_pct}}% · ${{s.trades}} 單 · 平均持倉 ${{s.avg_days}} 日</td>
                        <td class="p-2 text-right font-black font-mono text-amber-300">${{s.bp_day}}</td>
                    </tr>`;
                }}
                benchTbody.innerHTML = h;
            }}
        }}
    </script>
</body>
</html>"""

with open(os.path.join(OUTPUT_DIR, "index.html"), "w", encoding="utf-8") as f: f.write(html)
print(f"\n🎉 UAT 時光機版建置完成！")

try:
    with open(INFO_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(STOCK_INFO_CACHE, f, ensure_ascii=False)
    print(f"💾 已儲存 {len(STOCK_INFO_CACHE)} 隻股票資料 cache")
except Exception as e:
    print(f"⚠️ 股票資料 cache 寫入失敗: {e}")
