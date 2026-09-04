# =============================================================================
# 🅱️ RS 核心策略 — Production
#
# 策略邏輯同已驗證嘅 IS 設定（R2）逐行對齊：
#   RS Top20 · 美日一齊 · 每月第一個交易日換倉 · 等權 · 無止損
#   唯一過濾層：個股趨勢（現價 > 50MA > 200MA）
#
# ⚠️ 改任何策略參數 = 你實盤跑緊嘅唔再係驗證過嗰個策略
# =============================================================================

import pandas as pd, numpy as np, yfinance as yf, matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import warnings, os, datetime, json, logging, time, requests, csv, io, re
from io import StringIO

logging.getLogger('yfinance').setLevel(logging.CRITICAL)
warnings.filterwarnings('ignore')
plt.style.use('dark_background'); plt.ioff()

# =============================================================================
# 系統設定
# =============================================================================
OUTPUT_DIR = "docs"
CHARTS_DIR = os.path.join(OUTPUT_DIR, "charts")
os.makedirs(CHARTS_DIR, exist_ok=True)

DISCORD_WEBHOOK = os.environ.get("DISCORD_SUMMARY_WEBHOOK", "")

HISTORY_FILE    = os.path.join(OUTPUT_DIR, "rs_trade_history.json")
CSV_EXPORT_FILE = os.path.join(OUTPUT_DIR, "rs_trade_history.csv")
WATCHLIST_CACHE = os.path.join(OUTPUT_DIR, "watchlist_cache.json")
INFO_CACHE_FILE = os.path.join(OUTPUT_DIR, "stock_info_cache.json")
DATA_CACHE_FILE = os.path.join(OUTPUT_DIR, "market_data_cache.pkl")

# =============================================================================
# 🔒 策略參數 — 同 IS 驗證設定完全一致，唔好改
# =============================================================================
RS_TAG          = "🅱️ RS 核心"
RS_TOP_N        = 20
RS_USE_TREND    = True          # ✅ 唯一啟用嘅過濾層
PCT_LIQUIDITY   = 0.35
BENCH           = ['SPY', '^VIX', '^N225', 'JPY=X']
LOOKBACK_YEARS  = 3             # 夠計 252 日動能 + 200MA

COMMISSION_PCT  = 0.0005
SLIPPAGE_PCT    = 0.0010
ROUND_TRIP_COST = (COMMISSION_PCT + SLIPPAGE_PCT) * 2      # 0.3%

# 資金
INITIAL_EQUITY  = float(os.environ.get("INITIAL_EQUITY", "100000"))
FORCE_REBALANCE = os.environ.get("FORCE_REBALANCE", "0") == "1"

print("=" * 70)
print(f"🅱️ RS 核心 Production | Top{RS_TOP_N} | 美日 | 趨勢過濾={RS_USE_TREND}")
print("=" * 70)

# =============================================================================
# 功能函數
# =============================================================================
STOCK_INFO_CACHE = {}
if os.path.exists(INFO_CACHE_FILE):
    try:
        with open(INFO_CACHE_FILE, "r", encoding="utf-8") as f:
            STOCK_INFO_CACHE = json.load(f)
    except Exception:
        STOCK_INFO_CACHE = {}


def get_stock_info(tk):
    if tk in STOCK_INFO_CACHE:
        return STOCK_INFO_CACHE[tk]
    try:
        info = yf.Ticker(tk).info
        data = {'sector': info.get('sector', 'N/A'),
                'name': info.get('shortName', tk),
                'mcap': info.get('marketCap', 0)}
    except Exception:
        data = {'sector': 'N/A', 'name': tk, 'mcap': 0}
    STOCK_INFO_CACHE[tk] = data
    return data


def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


trade_history = load_history()


def calc_pnl(t):
    """真實 P&L（美元，已扣來回成本，日股已換匯）"""
    buy, last, sh = t.get('px', 0), t.get('last_px', 0), t.get('shares', 0)
    if not buy or buy <= 0 or not sh:
        return 0.0
    pnl = (last - buy) * sh
    fx_e = t.get('fx_entry')
    fx_x = t.get('fx_exit') or fx_e
    if fx_e and fx_x and fx_x > 0:
        pnl /= fx_x
    notional = buy * sh / (fx_e if fx_e else 1)
    return pnl - notional * ROUND_TRIP_COST


def unit_of(tk):
    return "¥" if tk.endswith(".T") else "$"


# =============================================================================
# MODULE 1 — 觀察名單（只用指數成分股，同 IS 一致）
# =============================================================================
HDR = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                     '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}


def build_watchlist():
    src = {}

    def add(tickers, label):
        for t in tickers:
            if not isinstance(t, str) or not t.strip():
                continue
            ct = t.strip()
            if not ct.endswith('.T'):
                ct = ct.replace('.', '-')
            src.setdefault(ct, [])
            if label not in src[ct]:
                src[ct].append(label)

    for url, label in [
        ("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", "S&P500"),
        ("https://en.wikipedia.org/wiki/List_of_S%26P_400_companies", "S&P400"),
        ("https://en.wikipedia.org/wiki/List_of_S%26P_600_companies", "S&P600"),
        ("https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies", "NDX100"),
    ]:
        try:
            res = requests.get(url, headers=HDR, timeout=15)
            for df in pd.read_html(StringIO(res.text)):
                col = next((c for c in df.columns
                            if 'symbol' in str(c).lower() or 'ticker' in str(c).lower()), None)
                if col is not None:
                    add(df[col].dropna().astype(str).tolist(), label)
                    print(f"  ✅ {label}: {len(df)} 隻")
                    break
        except Exception as e:
            print(f"  ⚠️ {label} 失敗: {e}")

    for url, label in [
        ("https://zh.wikipedia.org/zh-hk/%E6%97%A5%E7%B6%93%E5%B9%B3%E5%9D%87%E6%8C%87%E6%95%B8", "NK225"),
        ("https://ja.wikipedia.org/wiki/TOPIX_Mid400", "TOPIX400"),
        ("https://ja.wikipedia.org/wiki/TOPIX_Small500", "TOPIX500"),
    ]:
        try:
            res = requests.get(url, headers=HDR, timeout=15)
            tgt = max(pd.read_html(StringIO(res.text)), key=len)
            col = next((c for c in tgt.columns if any(
                k in str(c).lower() for k in ('code', 'ticker', 'symbol', 'コード', '編號', '编号'))), None)
            if col is None:
                for c in tgt.columns:
                    sv = tgt[c].dropna().astype(str).tolist()[:5]
                    if sv and all(re.search(r'\d{4}', str(x)) for x in sv):
                        col = c
                        break
            if col is not None:
                found = [f"{m.group(1)}.T" for x in tgt[col].dropna()
                         if (m := re.search(r'(\d{4})', str(x)))]
                add(list(dict.fromkeys(found)), label)
                print(f"  ✅ {label}: {len(found)} 隻")
        except Exception as e:
            print(f"  ⚠️ {label} 失敗: {e}")

    add(BENCH, "基準")
    return src


if os.path.exists(WATCHLIST_CACHE) and \
        (time.time() - os.path.getmtime(WATCHLIST_CACHE)) < 86400 * 7:
    with open(WATCHLIST_CACHE, "r", encoding="utf-8") as f:
        TICKER_MAP = json.load(f)
    print(f"⚡ Cache 讀取觀察名單 ({len(TICKER_MAP)} 隻)")
else:
    print("⏳ [1/6] 建立觀察名單...")
    TICKER_MAP = build_watchlist()
    with open(WATCHLIST_CACHE, "w", encoding="utf-8") as f:
        json.dump(TICKER_MAP, f, ensure_ascii=False)

ALL_TICKERS = list(TICKER_MAP.keys())

# =============================================================================
# MODULE 2 — 數據（分批下載 + 重試）
# =============================================================================
print(f"⏳ [2/6] 市場數據 ({len(ALL_TICKERS)} 隻)...")

_fresh = (os.path.exists(DATA_CACHE_FILE) and
          datetime.datetime.fromtimestamp(os.path.getmtime(DATA_CACHE_FILE))
          > datetime.datetime.now() - datetime.timedelta(hours=6))

if _fresh:
    print("⚡ 使用 6 小時內 cache")
    data_raw = pd.read_pickle(DATA_CACHE_FILE)
else:
    def dl(tickers, label, chunk=150, retries=3):
        frames = []
        for i in range(0, len(tickers), chunk):
            batch = tickers[i:i + chunk]
            for a in range(1, retries + 1):
                try:
                    df = yf.download(batch, period=f"{LOOKBACK_YEARS}y", progress=False,
                                     threads=True, timeout=60, group_by='column',
                                     auto_adjust=False)
                    if df is not None and not df.empty:
                        frames.append(df); break
                    raise ValueError("空數據")
                except Exception as e:
                    if a == retries:
                        print(f"   ⚠️ {label} 批次 {i//chunk+1} 失敗: {e}")
                    else:
                        time.sleep(5 * a)
            print(f"   👉 {label} {min(i+chunk, len(tickers))}/{len(tickers)}")
        return pd.concat(frames, axis=1) if frames else pd.DataFrame()

    parts = [d for d in (dl([t for t in ALL_TICKERS if not t.endswith('.T')], "美股"),
                         dl([t for t in ALL_TICKERS if t.endswith('.T')], "日股"))
             if d is not None and not d.empty]
    if not parts:
        raise SystemExit("❌ 數據下載完全失敗")
    data_raw = pd.concat(parts, axis=1)
    data_raw.to_pickle(DATA_CACHE_FILE)
    print("💾 已寫入 cache")

data_raw.index = pd.to_datetime(data_raw.index).tz_localize(None).normalize()
data_raw = data_raw.groupby(data_raw.index).max()
closes = data_raw['Close'].ffill()
vols   = data_raw['Volume'].ffill()

if closes.empty or len(closes.index) < 260:
    raise SystemExit(f"❌ 數據不足（{len(closes.index)} 個交易日，需要 260+）")

today_str = closes.index[-1].strftime('%Y-%m-%d')
print(f"✅ 數據到 {today_str} | {len(closes.index)} 交易日 | "
      f"{closes.notna().any().sum()}/{len(closes.columns)} 隻有數據")

# =============================================================================
# MODULE 3 — RS 排名（公式同 IS 逐字一致）
# =============================================================================
print("⏳ [3/6] 計算 RS...")

r126 = closes / closes.shift(126) - 1
r252 = closes / closes.shift(252) - 1
if r252.isna().all().all():
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

dict_rs        = rs_rank.iloc[-1].to_dict()
current_prices = closes.iloc[-1].to_dict()
dict_sma50     = closes.rolling(50).mean().iloc[-1].to_dict()
dict_sma200    = closes.rolling(200).mean().iloc[-1].to_dict()

# 流動性（分市場百分位）
dollar_vol_20 = (closes * vols).rolling(20).mean().iloc[-1]
_is_jp = dollar_vol_20.index.str.endswith('.T')
_dv_us, _dv_jp = dollar_vol_20[~_is_jp].dropna(), dollar_vol_20[_is_jp].dropna()
if len(_dv_us) > 50 and len(_dv_jp) > 50:
    us_thresh, jp_thresh = _dv_us.quantile(PCT_LIQUIDITY), _dv_jp.quantile(PCT_LIQUIDITY)
else:
    us_thresh, jp_thresh = 20_000_000, 300_000_000
print(f"💧 流動性門檻 | 美股 ${us_thresh/1e6:.1f}M | 日股 ¥{jp_thresh/1e6:.0f}M")

valid_tickers = dollar_vol_20[((~_is_jp) & (dollar_vol_20 >= us_thresh)) |
                              ((_is_jp) & (dollar_vol_20 >= jp_thresh))].index.tolist()
valid_tickers = [t for t in valid_tickers
                 if t not in BENCH and not pd.isna(dict_rs.get(t, np.nan))]
print(f"🧹 候選池 {len(valid_tickers)} 隻")

# 大市狀態（僅供顯示，唔影響選股）
spx, spx200 = float(closes['SPY'].iloc[-1]), float(closes['SPY'].rolling(200).mean().iloc[-1])
n225, n225200 = float(closes['^N225'].iloc[-1]), float(closes['^N225'].rolling(200).mean().iloc[-1])
us_ok, jp_ok = spx > spx200, n225 > n225200
us_regime = "🟢 SPY > 200MA" if us_ok else "🔴 SPY < 200MA"
jp_regime = "🟢 N225 > 200MA" if jp_ok else "🔴 N225 < 200MA"
print(f"🇺🇸 {us_regime} | 🇯🇵 {jp_regime}   (僅供參考)")

today_fx = float(current_prices.get('JPY=X', 0)) \
    if not pd.isna(current_prices.get('JPY=X', np.nan)) else 0

# =============================================================================
# MODULE 4 — 更新持倉 + 計算目前資金
# =============================================================================
print("⏳ [4/6] 更新持倉...")

open_trades = [t for t in trade_history if t.get('status') == 'OPEN']
for t in open_trades:
    px = current_prices.get(t['tk'])
    if px is not None and not pd.isna(px):
        t['last_px'] = round(float(px), 2)
    r = dict_rs.get(t['tk'])
    t['curr_rs'] = int(r) if r is not None and not pd.isna(r) else None
    if t.get('fx_entry') and today_fx:
        t['fx_exit'] = round(today_fx, 4)

realized = sum(calc_pnl(t) for t in trade_history if t.get('status') != 'OPEN')
floating = 0.0
for t in open_trades:
    v = (t['last_px'] - t['px']) * t.get('shares', 0)
    if t.get('fx_entry') and t.get('fx_exit'):
        v /= t['fx_exit']
    floating += v

EQUITY = max(INITIAL_EQUITY + realized + floating, INITIAL_EQUITY * 0.1)
POSITION_SIZE = EQUITY / RS_TOP_N
print(f"📂 持倉 {len(open_trades)} 隻 | 已實現 ${realized:,.0f} | 浮動 ${floating:,.0f}")
print(f"💰 目前資金 ${EQUITY:,.0f} → 每倉 ${POSITION_SIZE:,.0f}")

# =============================================================================
# MODULE 5 — 換倉判斷 + 指令
# =============================================================================
_prev = closes.index[-2] if len(closes.index) >= 2 else None
IS_REBAL = FORCE_REBALANCE or (_prev is not None and closes.index[-1].month != _prev.month)
if IS_REBAL and any(RS_TAG in t.get('tag', '') and t.get('date') == today_str
                    for t in trade_history):
    print("⏭️ 今日已換過倉，略過")
    IS_REBAL = False

# 排行榜（每日都出）
ranking = []
for tk in valid_tickers:
    r, px = dict_rs.get(tk), current_prices.get(tk)
    if r is None or pd.isna(r) or px is None or pd.isna(px) or float(px) <= 0:
        continue
    is_jp = tk.endswith('.T')
    trend_ok = True
    if RS_USE_TREND:
        s50, s200 = dict_sma50.get(tk), dict_sma200.get(tk)
        trend_ok = (s50 is not None and s200 is not None
                    and not pd.isna(s50) and not pd.isna(s200)
                    and float(px) > float(s50) > float(s200))
    ranking.append({'rs': round(float(r), 1), 'tk': tk, 'px': round(float(px), 2),
                    'unit': unit_of(tk), 'mkt': 'JP' if is_jp else 'US', 'trend': trend_ok})
ranking.sort(key=lambda x: -x['rs'])

sell_orders, buy_orders, hold_list = [], [], []

if IS_REBAL:
    print(f"🔔 [{today_str}] 換倉日")
    picks = [r for r in ranking if r['trend']][:RS_TOP_N]
    pick_set = {r['tk'] for r in picks}

    for t in open_trades:
        if t['tk'] in pick_set:
            hold_list.append(t); continue
        px = current_prices.get(t['tk'])
        if px is None or pd.isna(px):
            continue
        t['last_px'] = round(float(px), 2)
        t['status'] = '✅ RS 換倉平倉'
        t['close_date'] = today_str
        t['days_held'] = max(int(round(
            (pd.to_datetime(today_str) - pd.to_datetime(t['date'])).days * 21 / 30.44)), 1)
        if t.get('fx_entry') and today_fx:
            t['fx_exit'] = round(today_fx, 4)
        sell_orders.append({'tk': t['tk'], 'shares': t.get('shares', 0),
                            'px': t['last_px'], 'unit': unit_of(t['tk']),
                            'pnl': round(calc_pnl(t), 2)})

    held = {t['tk'] for t in hold_list}
    for r in picks:
        tk, px, is_jp = r['tk'], r['px'], r['mkt'] == 'JP'
        if tk in held:
            continue
        fx = today_fx if (is_jp and today_fx) else None
        budget = POSITION_SIZE * fx if fx else POSITION_SIZE
        shares = int(budget / px)
        if is_jp:
            shares = (shares // 100) * 100          # 日股一手 100 股
        if shares <= 0:
            print(f"   ⚠️ {tk} 資金不足一手，略過")
            continue
        info = get_stock_info(tk)
        rec = {'date': today_str, 'tk': tk, 'name': info.get('name', tk),
               'px': px, 'shares': shares, 'last_px': px, 'status': 'OPEN',
               'tag': RS_TAG, 'entry_rs': int(r['rs']), 'curr_rs': int(r['rs']),
               'sector': info.get('sector', 'N/A'), 'mcap': info.get('mcap', 0),
               'mkt': r['mkt'], 'sources': TICKER_MAP.get(tk, []),
               'cfg': f"N{RS_TOP_N}|trd{int(RS_USE_TREND)}|liq{PCT_LIQUIDITY}"}
        if fx:
            rec['fx_entry'] = round(fx, 4)
        trade_history.append(rec)
        buy_orders.append({'tk': tk, 'shares': shares, 'px': px, 'unit': unit_of(tk),
                           'rs': int(r['rs']), 'cost': round(shares * px, 0),
                           'name': info.get('name', tk)})

    print(f"🔄 賣 {len(sell_orders)} | 保留 {len(hold_list)} | 買 {len(buy_orders)}")
else:
    print(f"😴 唔係換倉日。下次：下個月第一個交易日")

# ---- 儲存 ----
_op = [t for t in trade_history if t.get('status') == 'OPEN']
_cl = [t for t in trade_history if t.get('status') != 'OPEN']
with open(HISTORY_FILE, "w", encoding="utf-8") as f:
    json.dump(_op + _cl[-5000:], f, indent=4, ensure_ascii=False)

try:
    with open(INFO_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(STOCK_INFO_CACHE, f, ensure_ascii=False)
except Exception:
    pass

if trade_history:
    _ORDER = ['date', 'close_date', 'tk', 'name', 'tag', 'mkt', 'sector',
              'entry_rs', 'curr_rs', 'px', 'last_px', 'shares', 'days_held',
              'status', 'fx_entry', 'fx_exit', 'mcap', 'sources', 'cfg']
    keys = set().union(*(t.keys() for t in trade_history))
    cols = [k for k in _ORDER if k in keys] + [k for k in sorted(keys) if k not in _ORDER]
    try:
        with open(CSV_EXPORT_FILE, 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore')
            w.writeheader(); w.writerows(trade_history)
    except Exception as e:
        print(f"⚠️ CSV 匯出失敗: {e}")

# =============================================================================
# MODULE 6 — Discord
# =============================================================================
print("⏳ [5/6] 發送通知...")

closed = [t for t in trade_history if t.get('status') != 'OPEN']
wins = [t for t in closed if calc_pnl(t) > 0]
win_rate = round(len(wins) / len(closed) * 100, 1) if closed else 0

if DISCORD_WEBHOOK:
    if IS_REBAL:
        s_txt = "\n".join(f"🔴 **{o['tk']}** × {o['shares']} @ {o['unit']}{o['px']} "
                          f"({'+' if o['pnl'] >= 0 else ''}${o['pnl']:,.0f})"
                          for o in sell_orders) or "無"
        b_txt = "\n".join(f"🟢 **{o['tk']}** × {o['shares']} @ {o['unit']}{o['px']} (RS {o['rs']})"
                          for o in buy_orders) or "無"
        desc = (f"**🔴 賣出 ({len(sell_orders)})**\n{s_txt}\n\n"
                f"**🟢 買入 ({len(buy_orders)})**\n{b_txt}\n\n"
                f"**⏸️ 保留 ({len(hold_list)})**\n" +
                (", ".join(t['tk'] for t in hold_list) or "無"))
        title, color = f"🔔 換倉指令 ({today_str})", 3066993
    else:
        top = "\n".join(f"`{i+1:>2}.` **{r['tk']}** RS {r['rs']} {r['unit']}{r['px']}"
                        for i, r in enumerate([x for x in ranking if x['trend']][:10]))
        desc = f"今日唔換倉。合資格 RS 前十：\n{top}"
        title, color = f"📊 每日監察 ({today_str})", 3447003

    try:
        requests.post(DISCORD_WEBHOOK, json={"embeds": [{
            "title": title, "description": desc[:4000], "color": color,
            "fields": [
                {"name": "💰 資金", "value": f"${EQUITY:,.0f}", "inline": True},
                {"name": "📂 持倉", "value": f"{len(_op)} 隻", "inline": True},
                {"name": "🌊 浮動", "value": f"${floating:,.0f}", "inline": True},
                {"name": "📈 勝率", "value": f"{win_rate}% ({len(wins)}/{len(closed)})", "inline": True},
            ],
            "footer": {"text": f"RS Top{RS_TOP_N} · 趨勢過濾 · 每倉 ${POSITION_SIZE:,.0f}"}
        }]}, timeout=15)
    except Exception as e:
        print(f"⚠️ Discord 失敗: {e}")

# SPY 圖
try:
    spy = closes['SPY']
    fig, ax = plt.subplots(figsize=(8, 3), dpi=100)
    ax.plot(spy.index[-250:], spy.iloc[-250:], color='#cbd5e1', lw=1.5, label='SPY')
    ax.plot(spy.index[-250:], spy.rolling(200).mean().iloc[-250:],
            color='#dc2626', ls='-.', lw=1.5, label='200MA')
    fig.patch.set_facecolor('#0f172a'); ax.set_facecolor('#0f172a')
    ax.tick_params(colors='white', labelsize=8)
    ax.legend(facecolor='#1e293b', labelcolor='white', fontsize=8)
    for s in ax.spines.values(): s.set_edgecolor('#334155')
    plt.tight_layout(); plt.savefig(os.path.join(CHARTS_DIR, "SPY_Trend.png"), transparent=True)
    plt.close(fig)
except Exception as e:
    print(f"⚠️ 圖表失敗: {e}")

# =============================================================================
# MODULE 7 — Dashboard（共用 rs_dashboard.py）
# =============================================================================
print("⏳ [6/6] 生成 Dashboard...")

from rs_dashboard import build_dashboard

build_dashboard(
    trade_history, closes, OUTPUT_DIR, today_str,
    cfg={'top_n': RS_TOP_N, 'cost': ROUND_TRIP_COST,
         'ticket': POSITION_SIZE, 'tag': 'RS 核心'},
    regime=[{'label': '🇺🇸 美股', 'ok': bool(us_ok), 'text': us_regime},
            {'label': '🇯🇵 日股', 'ok': bool(jp_ok), 'text': jp_regime}],
)

print("\n🎉 完成")
if IS_REBAL:
    print("⚠️ 今日係換倉日 —— 請於下一個交易日開市執行指令")
