# =============================================================================
# 📊 rs_dashboard.py — RS 核心策略專用分析儀表板
#
# 用法（喺 production 或 UAT script 尾部）：
#     from rs_dashboard import build_dashboard
#     build_dashboard(trade_history, closes, OUTPUT_DIR, today_str,
#                     cfg={'top_n': RS_TOP_N, 'cost': ROUND_TRIP_COST,
#                          'ticket': POSITION_SIZE},
#                     sector_now=sector_rrg)      # sector_rrg 可選
#
# 設計原則：
#   1. HTML/JS 用 __PLACEHOLDER__ 注入，唔用 f-string —— 避免 {{ }} 逃逸出錯
#   2. 所有指標基於「月度組合回報」，唔係逐單 —— 因為呢個係月度策略
#   3. 每個圖表都要答一條具體問題，唔為靚而加
# =============================================================================

import os, json, math
import numpy as np
import pandas as pd


# =============================================================================
# 計算層
# =============================================================================
def _trade_ret(t, ticket):
    """單一交易嘅小數回報（已扣成本、已換算美元）"""
    px = t.get('px', 0)
    if not px or px <= 0:
        return 0.0
    last = t.get('last_px', px)
    sh = t.get('shares')
    fx_e = t.get('fx_entry')
    fx_x = t.get('fx_exit') or fx_e

    if sh:                                   # production：真實股數
        notional = px * sh / (fx_e if fx_e else 1)
        pnl = (last - px) * sh
        if fx_e and fx_x and fx_x > 0:
            pnl = pnl / fx_x
    else:                                    # 回測：固定注碼
        notional = ticket
        pnl = ticket * (last / px - 1)
        if fx_e and fx_x and fx_x > 0:
            pnl = ticket * ((last / px) * (fx_e / fx_x) - 1)
    return (pnl / notional) if notional else 0.0


def _analyse(trade_history, closes, cfg):
    ticket = cfg.get('ticket', 10000)
    cost = cfg.get('cost', 0.003)
    top_n = cfg.get('top_n', 20)

    closed = [t for t in trade_history if t.get('status') != 'OPEN']
    opens = [t for t in trade_history if t.get('status') == 'OPEN']

    # ---- 逐單回報（扣成本）----
    rows = []
    for t in closed:
        r = _trade_ret(t, ticket) - cost
        rows.append({
            'date': t.get('date'), 'close_date': t.get('close_date'),
            'tk': t.get('tk'), 'ret': r,
            'sector': t.get('sector', 'N/A'),
            'mkt': 'JP' if str(t.get('tk', '')).endswith('.T') else 'US',
            'entry_rs': t.get('entry_rs') or t.get('rs'),
            'days': t.get('days_held', 21),
        })
    rows.sort(key=lambda x: (x['date'] or ''))

    # ---- 按進場月份分組（= 每次換倉 = 一個組合月）----
    cohorts = {}
    for r in rows:
        if r['date']:
            cohorts.setdefault(r['date'], []).append(r)
    months = sorted(cohorts.keys())

    m_ret, m_n, m_names, m_sec, m_mkt = [], [], [], [], []
    for d in months:
        c = cohorts[d]
        m_ret.append(float(np.mean([x['ret'] for x in c])))
        m_n.append(len(c))
        m_names.append({x['tk'] for x in c})
        sc = {}
        for x in c:
            sc[x['sector']] = sc.get(x['sector'], 0) + 1
        m_sec.append(sc)
        m_mkt.append(sum(1 for x in c if x['mkt'] == 'JP'))

    # ---- Equity curve ----
    eq = [1.0]
    for r in m_ret:
        eq.append(eq[-1] * (1 + r))
    eq = eq[1:] if eq else [1.0]

    dd = []
    peak = -1e9
    for v in eq:
        peak = max(peak, v)
        dd.append(v / peak - 1 if peak > 0 else 0)

    n_m = max(len(m_ret), 1)
    total_ret = (eq[-1] - 1) if eq else 0
    cagr = ((eq[-1]) ** (12 / n_m) - 1) if eq and eq[-1] > 0 and n_m >= 3 else 0
    maxdd = min(dd) if dd else 0
    mar = (cagr / abs(maxdd)) if maxdd < 0 else 0

    _sd = float(np.std(m_ret, ddof=1)) if len(m_ret) > 1 else 0
    sharpe = (float(np.mean(m_ret)) / _sd * math.sqrt(12)) if _sd > 0 else 0
    _dn = [r for r in m_ret if r < 0]
    _dsd = float(np.std(_dn, ddof=1)) if len(_dn) > 1 else 0
    sortino = (float(np.mean(m_ret)) / _dsd * math.sqrt(12)) if _dsd > 0 else 0

    # ---- 換手率 ----
    turnover = [1.0]
    for i in range(1, len(m_names)):
        prev, cur = m_names[i - 1], m_names[i]
        turnover.append(len(cur - prev) / max(len(cur), 1))
    avg_turn = float(np.mean(turnover)) if turnover else 0
    cost_drag = avg_turn * cost * 12          # 年化成本拖累

    # ---- 集中度 ----
    all_ret = sorted([r['ret'] for r in rows], reverse=True)
    total_r = sum(all_ret)
    contrib = []
    for k in (1, 3, 5, 10, 20):
        if len(all_ret) >= k and total_r != 0:
            contrib.append({'k': k, 'pct': round(sum(all_ret[:k]) / total_r * 100, 1)})
    top_trades = sorted(rows, key=lambda x: -x['ret'])[:10]
    bot_trades = sorted(rows, key=lambda x: x['ret'])[:10]

    # 回報封頂測試
    wins_cap = []
    for cap in (0.25, 0.50, 1.00, 2.00):
        capped = [min(r, cap) for r in all_ret]
        wins_cap.append({'cap': int(cap * 100),
                         'exp': round(float(np.mean(capped)) * 100, 3) if capped else 0})

    # ---- 滾動 6 個月期望值（衰減偵測）----
    roll = []
    W = 6
    for i in range(len(m_ret)):
        if i >= W - 1:
            roll.append(round(float(np.mean(m_ret[i - W + 1:i + 1])) * 100, 3))
        else:
            roll.append(None)

    # ---- 板塊佔比 over time ----
    all_secs = {}
    for sc in m_sec:
        for k, v in sc.items():
            all_secs[k] = all_secs.get(k, 0) + v
    top_secs = [k for k, _ in sorted(all_secs.items(), key=lambda x: -x[1])[:8]]
    sec_series = {s: [] for s in top_secs}
    sec_series['其他'] = []
    for sc in m_sec:
        tot = max(sum(sc.values()), 1)
        other = 0
        for s in top_secs:
            sec_series[s].append(round(sc.get(s, 0) / tot * 100, 1))
        for k, v in sc.items():
            if k not in top_secs:
                other += v
        sec_series['其他'].append(round(other / tot * 100, 1))

    # ---- 進場 RS 分桶 ----
    buckets = {'99+': [], '97-99': [], '95-97': [], '90-95': [], '<90': []}
    for r in rows:
        v = r.get('entry_rs')
        if v is None:
            continue
        v = float(v)
        k = '99+' if v >= 99 else '97-99' if v >= 97 else '95-97' if v >= 95 \
            else '90-95' if v >= 90 else '<90'
        buckets[k].append(r['ret'])
    rs_bucket = []
    for k, v in buckets.items():
        if v:
            rs_bucket.append({'k': k, 'n': len(v),
                              'win': round(sum(1 for x in v if x > 0) / len(v) * 100, 1),
                              'exp': round(float(np.mean(v)) * 100, 2)})

    # ---- 持續性（連續留喺名單幾多個月）----
    streak, cur_streak = {}, {}
    for i, names in enumerate(m_names):
        for tk in names:
            cur_streak[tk] = cur_streak.get(tk, 0) + 1
        for tk in list(cur_streak):
            if tk not in names:
                streak.setdefault(cur_streak[tk], 0)
                streak[cur_streak[tk]] += 1
                del cur_streak[tk]
    for tk, v in cur_streak.items():
        streak[v] = streak.get(v, 0) + 1
    persist = [{'m': k, 'n': v} for k, v in sorted(streak.items())][:12]

    # ---- 美/日拆分 ----
    mkt_stat = {}
    for m in ('US', 'JP'):
        sub = [r['ret'] for r in rows if r['mkt'] == m]
        if sub:
            mkt_stat[m] = {'n': len(sub),
                           'win': round(sum(1 for x in sub if x > 0) / len(sub) * 100, 1),
                           'exp': round(float(np.mean(sub)) * 100, 2)}

    # ---- SPY 對照 ----
    spy_eq = []
    if closes is not None and 'SPY' in getattr(closes, 'columns', []):
        try:
            s = closes['SPY']
            base = None
            for d in months:
                ts = pd.to_datetime(d)
                sub = s.loc[:ts]
                if sub.empty:
                    spy_eq.append(None); continue
                px = float(sub.iloc[-1])
                if base is None:
                    base = px
                spy_eq.append(round(px / base, 4))
        except Exception:
            spy_eq = []

    # ---- 逐單基本統計 ----
    wins = [r['ret'] for r in rows if r['ret'] > 0]
    loss = [r['ret'] for r in rows if r['ret'] <= 0]
    pf = (sum(wins) / abs(sum(loss))) if loss and sum(loss) != 0 else 0

    return {
        'months': months,
        'm_ret': [round(r * 100, 3) for r in m_ret],
        'm_n': m_n,
        'm_jp': m_mkt,
        'eq': [round(v, 4) for v in eq],
        'dd': [round(v * 100, 2) for v in dd],
        'spy_eq': spy_eq,
        'roll6': roll,
        'turnover': [round(v * 100, 1) for v in turnover],
        'sec_series': sec_series,
        'sec_names': list(sec_series.keys()),
        'rs_bucket': rs_bucket,
        'persist': persist,
        'mkt_stat': mkt_stat,
        'contrib': contrib,
        'wins_cap': wins_cap,
        'top_trades': [{'tk': t['tk'], 'date': t['date'],
                        'ret': round(t['ret'] * 100, 1)} for t in top_trades],
        'bot_trades': [{'tk': t['tk'], 'date': t['date'],
                        'ret': round(t['ret'] * 100, 1)} for t in bot_trades],
        'kpi': {
            'trades': len(rows), 'opens': len(opens), 'months': n_m,
            'total_ret': round(total_ret * 100, 1),
            'cagr': round(cagr * 100, 1),
            'maxdd': round(maxdd * 100, 1),
            'mar': round(mar, 2),
            'sharpe': round(sharpe, 2),
            'sortino': round(sortino, 2),
            'win_rate': round(len(wins) / max(len(rows), 1) * 100, 1),
            'pf': round(pf, 2),
            'exp_mean': round(float(np.mean([r['ret'] for r in rows])) * 100, 2) if rows else 0,
            'exp_median': round(float(np.median([r['ret'] for r in rows])) * 100, 2) if rows else 0,
            'best_m': round(max(m_ret) * 100, 1) if m_ret else 0,
            'worst_m': round(min(m_ret) * 100, 1) if m_ret else 0,
            'pos_months': round(sum(1 for r in m_ret if r > 0) / max(len(m_ret), 1) * 100, 1),
            'avg_turn': round(avg_turn * 100, 1),
            'cost_drag': round(cost_drag * 100, 2),
            'top_n': top_n,
        },
    }


# =============================================================================
# HTML 模板（用 __PLACEHOLDER__ 注入，避免 f-string 逃逸問題）
# =============================================================================
_TPL = r"""<!DOCTYPE html>
<html lang="zh-TW"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://cdn.jsdelivr.net/npm/apexcharts"></script>
<title>RS QUANT · __TODAY__</title>
<style>.card{background:rgba(30,41,59,.35);border:1px solid #334155;border-radius:.75rem;padding:1rem}</style>
</head>
<body class="bg-[#020617] text-slate-300 p-4 font-sans">

<header class="card mb-4 flex flex-wrap justify-between items-center gap-3">
  <div>
    <h1 class="text-2xl font-black text-white italic">🅱️ RS <span class="text-lime-400">QUANT</span></h1>
    <div class="text-[11px] text-slate-500 mt-1">__TODAY__ · Top__TOPN__ · __NMONTHS__ 個換倉月 · __NTRADES__ 單</div>
  </div>
  <div class="flex gap-2 flex-wrap" id="regime-box"></div>
</header>

<!-- 核心 KPI -->
<div class="grid grid-cols-2 md:grid-cols-4 xl:grid-cols-8 gap-2 mb-4" id="kpi-row"></div>

<!-- ① 資金曲線 -->
<div class="card mb-4">
  <div class="flex justify-between items-baseline mb-1">
    <h3 class="font-black text-lime-400">① 資金曲線 vs SPY</h3>
    <span class="text-[10px] text-slate-500">每月等權換倉、複利、已扣成本</span>
  </div>
  <div id="c-equity" class="h-[320px]"></div>
  <div id="c-dd" class="h-[160px] -mt-2"></div>
</div>

<div class="grid grid-cols-1 xl:grid-cols-2 gap-4 mb-4">
  <!-- ② 月度回報 -->
  <div class="card">
    <h3 class="font-black text-cyan-400 mb-1">② 月度回報分佈</h3>
    <div class="text-[10px] text-slate-500 mb-2">動能策略靠少數大月份，睇下係咪過度依賴</div>
    <div id="c-monthly" class="h-[260px]"></div>
  </div>
  <!-- ③ 滾動衰減 -->
  <div class="card">
    <h3 class="font-black text-amber-400 mb-1">③ 滾動 6 個月期望值</h3>
    <div class="text-[10px] text-slate-500 mb-2">持續向下 = edge 正在消失；上落 = 統計噪音</div>
    <div id="c-roll" class="h-[260px]"></div>
  </div>
</div>

<!-- ④ 板塊輪動 -->
<div class="card mb-4">
  <h3 class="font-black text-fuchsia-400 mb-1">④ 板塊輪動軌跡（Top__TOPN__ 組成佔比）</h3>
  <div class="text-[10px] text-slate-500 mb-2">策略實際持有咩主題 — 唔使預測，佢自動跟</div>
  <div id="c-sector" class="h-[320px]"></div>
</div>

<div class="grid grid-cols-1 xl:grid-cols-2 gap-4 mb-4">
  <!-- ⑤ 集中度 -->
  <div class="card">
    <h3 class="font-black text-red-400 mb-1">⑤ 利潤集中度（Reality Check）</h3>
    <div class="text-[10px] text-slate-500 mb-3">最重要嘅穩健性指標 — 靠幾單撐起就唔可信</div>
    <div id="concentration"></div>
  </div>
  <!-- ⑥ 換手 + 成本 -->
  <div class="card">
    <h3 class="font-black text-orange-400 mb-1">⑥ 每月換手率</h3>
    <div class="text-[10px] text-slate-500 mb-2">換手愈高，成本拖累愈大</div>
    <div id="c-turn" class="h-[240px]"></div>
  </div>
</div>

<div class="grid grid-cols-1 xl:grid-cols-3 gap-4 mb-4">
  <div class="card"><h3 class="font-black text-indigo-400 mb-2">⑦ 進場 RS 分桶</h3>
    <div class="text-[10px] text-slate-500 mb-2">RS 愈高係咪愈好？決定 Top N 應該幾多</div>
    <div id="rs-bucket"></div></div>
  <div class="card"><h3 class="font-black text-teal-400 mb-2">⑧ 持股延續性</h3>
    <div class="text-[10px] text-slate-500 mb-2">連續留榜幾多個月 — 決定「保留仍在榜」值唔值</div>
    <div id="persist"></div></div>
  <div class="card"><h3 class="font-black text-sky-400 mb-2">⑨ 美股 vs 日股</h3>
    <div class="text-[10px] text-slate-500 mb-2">邊個市場真正貢獻</div>
    <div id="mkt-split"></div></div>
</div>

<!-- ⑩ 最佳/最差 -->
<div class="grid grid-cols-1 xl:grid-cols-2 gap-4 mb-4">
  <div class="card"><h3 class="font-black text-emerald-400 mb-2">⑩ 貢獻最大 10 單</h3><div id="top-t"></div></div>
  <div class="card"><h3 class="font-black text-red-400 mb-2">⑪ 拖累最大 10 單</h3><div id="bot-t"></div></div>
</div>

<!-- ⑫ 持倉 -->
<div class="card mb-4">
  <h3 class="font-black text-cyan-400 mb-2">⑫ 目前持倉</h3>
  <div class="overflow-x-auto"><table class="w-full text-xs text-left whitespace-nowrap">
    <thead class="text-slate-500 uppercase border-b border-slate-700"><tr>
      <th class="p-2">代號</th><th class="p-2">板塊</th><th class="p-2">買入日</th>
      <th class="p-2 text-center">進場RS</th><th class="p-2 text-center">現時RS</th>
      <th class="p-2 text-right">買入價</th><th class="p-2 text-right">現價</th>
      <th class="p-2 text-right">回報</th></tr></thead>
    <tbody id="open-tbody"></tbody></table></div>
</div>

<script>
const D = __DATA__;
const H = __HIST__;
const REG = __REGIME__;
const K = D.kpi;
const dark = {theme:{mode:'dark'}, chart:{background:'transparent',toolbar:{show:false}},
              grid:{borderColor:'#334155',strokeDashArray:3}, dataLabels:{enabled:false}};

// ── Regime ──
document.getElementById('regime-box').innerHTML = REG.map(r =>
  `<div class="bg-slate-800/50 px-3 py-2 rounded-lg border border-slate-700 text-center">
     <div class="text-[9px] text-slate-400 uppercase">${r.label}</div>
     <div class="text-xs font-bold ${r.ok?'text-emerald-400':'text-red-400'}">${r.text}</div></div>`).join('');

// ── KPI ──
const kpis = [
  ['總回報', K.total_ret+'%', K.total_ret>=0?'text-emerald-400':'text-red-400'],
  ['CAGR', K.cagr+'%', K.cagr>=0?'text-emerald-400':'text-red-400'],
  ['最大回撤', K.maxdd+'%', 'text-red-400'],
  ['MAR', K.mar, K.mar>=0.5?'text-emerald-400':'text-amber-400'],
  ['Sharpe', K.sharpe, K.sharpe>=1?'text-emerald-400':'text-amber-400'],
  ['勝率', K.win_rate+'%', 'text-white'],
  ['獲利因子', K.pf+'x', K.pf>=1.3?'text-emerald-400':'text-amber-400'],
  ['正回報月份', K.pos_months+'%', 'text-white'],
];
document.getElementById('kpi-row').innerHTML = kpis.map(k=>
  `<div class="bg-slate-800/50 p-3 rounded-xl border border-slate-700 text-center">
     <div class="text-[9px] text-slate-400 uppercase font-bold">${k[0]}</div>
     <div class="text-lg font-black ${k[2]}">${k[1]}</div></div>`).join('');

// ── ① Equity ──
const eqSeries = [{name:'RS 策略', data:D.eq.map(v=>+( (v-1)*100 ).toFixed(1))}];
if (D.spy_eq && D.spy_eq.length) eqSeries.push({name:'SPY', data:D.spy_eq.map(v=>v==null?null:+((v-1)*100).toFixed(1))});
new ApexCharts(document.querySelector("#c-equity"), Object.assign({}, dark, {
  series: eqSeries, chart:{type:'line',height:320,background:'transparent',toolbar:{show:false}},
  stroke:{width:[3,2],curve:'smooth',dashArray:[0,5]},
  colors:['#a3e635','#94a3b8'],
  xaxis:{categories:D.months,tickAmount:12,labels:{style:{colors:'#94a3b8'}}},
  yaxis:{title:{text:'累積回報 (%)',style:{color:'#94a3b8'}},labels:{formatter:v=>v.toFixed(0)+'%',style:{colors:'#94a3b8'}}},
  legend:{position:'top',labels:{colors:'#cbd5e1'}}, dataLabels:{enabled:false},
  grid:{borderColor:'#334155',strokeDashArray:3}, theme:{mode:'dark'}
})).render();

new ApexCharts(document.querySelector("#c-dd"), Object.assign({}, dark, {
  series:[{name:'回撤',data:D.dd}], chart:{type:'area',height:160,background:'transparent',toolbar:{show:false}},
  colors:['#ef4444'], stroke:{width:1.5,curve:'smooth'},
  fill:{type:'gradient',gradient:{opacityFrom:.5,opacityTo:.05}},
  xaxis:{categories:D.months,tickAmount:12,labels:{show:false}},
  yaxis:{title:{text:'回撤 (%)',style:{color:'#94a3b8'}},labels:{formatter:v=>v.toFixed(0)+'%',style:{colors:'#94a3b8'}},max:0},
  legend:{show:false}, dataLabels:{enabled:false},
  grid:{borderColor:'#334155',strokeDashArray:3}, theme:{mode:'dark'}
})).render();

// ── ② 月度回報 ──
new ApexCharts(document.querySelector("#c-monthly"), Object.assign({}, dark, {
  series:[{name:'月回報',data:D.m_ret}], chart:{type:'bar',height:260,background:'transparent',toolbar:{show:false}},
  plotOptions:{bar:{colors:{ranges:[{from:-100,to:0,color:'#ef4444'},{from:0,to:1000,color:'#22c55e'}]},borderRadius:2}},
  xaxis:{categories:D.months,tickAmount:10,labels:{style:{colors:'#94a3b8'}}},
  yaxis:{labels:{formatter:v=>v.toFixed(0)+'%',style:{colors:'#94a3b8'}}},
  legend:{show:false}, dataLabels:{enabled:false},
  grid:{borderColor:'#334155',strokeDashArray:3}, theme:{mode:'dark'}
})).render();

// ── ③ 滾動 6M ──
new ApexCharts(document.querySelector("#c-roll"), Object.assign({}, dark, {
  series:[{name:'滾動6月平均月回報',data:D.roll6}],
  chart:{type:'line',height:260,background:'transparent',toolbar:{show:false}},
  colors:['#f59e0b'], stroke:{width:3,curve:'smooth'},
  annotations:{yaxis:[{y:0,borderColor:'#64748b',strokeDashArray:4}]},
  xaxis:{categories:D.months,tickAmount:10,labels:{style:{colors:'#94a3b8'}}},
  yaxis:{labels:{formatter:v=>v.toFixed(1)+'%',style:{colors:'#94a3b8'}}},
  legend:{show:false}, dataLabels:{enabled:false},
  grid:{borderColor:'#334155',strokeDashArray:3}, theme:{mode:'dark'}
})).render();

// ── ④ 板塊 ──
new ApexCharts(document.querySelector("#c-sector"), Object.assign({}, dark, {
  series: D.sec_names.map(s=>({name:s,data:D.sec_series[s]})),
  chart:{type:'area',height:320,stacked:true,background:'transparent',toolbar:{show:false}},
  stroke:{curve:'smooth',width:1},
  fill:{type:'gradient',gradient:{opacityFrom:.75,opacityTo:.45}},
  xaxis:{categories:D.months,tickAmount:12,labels:{style:{colors:'#94a3b8'}}},
  yaxis:{max:100,title:{text:'佔比 (%)',style:{color:'#94a3b8'}},labels:{style:{colors:'#94a3b8'}}},
  legend:{position:'top',labels:{colors:'#cbd5e1'}}, dataLabels:{enabled:false},
  grid:{borderColor:'#334155',strokeDashArray:3}, theme:{mode:'dark'}
})).render();

// ── ⑤ 集中度 ──
let ch = '<table class="w-full text-xs mb-3"><tbody>';
D.contrib.forEach(c=>{
  const warn = c.pct > 50;
  ch += `<tr class="border-b border-slate-700/50"><td class="p-1.5 text-slate-400">最好 ${c.k} 單佔總回報</td>
   <td class="p-1.5 text-right font-black font-mono ${warn?'text-red-400':'text-slate-200'}">${c.pct}%</td></tr>`;
});
ch += '</tbody></table>';
ch += `<div class="text-[10px] text-slate-400 mb-1">每單回報封頂測試（剔走極端值後嘅期望值）</div>`;
ch += '<table class="w-full text-xs"><tbody>';
ch += `<tr class="border-b border-slate-700/50"><td class="p-1.5 text-slate-400">原始平均</td>
  <td class="p-1.5 text-right font-mono text-white">${K.exp_mean}%</td></tr>`;
ch += `<tr class="border-b border-slate-700/50"><td class="p-1.5 text-slate-400">中位數</td>
  <td class="p-1.5 text-right font-mono ${K.exp_median>=0?'text-emerald-400':'text-red-400'}">${K.exp_median}%</td></tr>`;
D.wins_cap.forEach(w=>{
  ch += `<tr class="border-b border-slate-700/50"><td class="p-1.5 text-slate-400">封頂 +${w.cap}%</td>
   <td class="p-1.5 text-right font-mono ${w.exp>=0?'text-emerald-400':'text-red-400'}">${w.exp}%</td></tr>`;
});
ch += '</tbody></table>';
ch += `<div class="mt-3 text-[10px] ${K.exp_median>=0?'text-emerald-400/80':'text-red-400/80'}">
  ${K.exp_median>=0 ? '✅ 中位數為正 — 典型交易都賺錢，唔淨係靠尾部' : '⚠️ 中位數為負 — 完全靠少數大贏單，穩健性有疑問'}</div>`;
document.getElementById('concentration').innerHTML = ch;

// ── ⑥ 換手 ──
new ApexCharts(document.querySelector("#c-turn"), Object.assign({}, dark, {
  series:[{name:'換手率',data:D.turnover}],
  chart:{type:'bar',height:240,background:'transparent',toolbar:{show:false}},
  colors:['#fb923c'], plotOptions:{bar:{borderRadius:2}},
  xaxis:{categories:D.months,tickAmount:10,labels:{style:{colors:'#94a3b8'}}},
  yaxis:{max:100,labels:{formatter:v=>v.toFixed(0)+'%',style:{colors:'#94a3b8'}}},
  legend:{show:false}, dataLabels:{enabled:false},
  grid:{borderColor:'#334155',strokeDashArray:3}, theme:{mode:'dark'}
})).render();

// ── ⑦⑧⑨ 表 ──
document.getElementById('rs-bucket').innerHTML = D.rs_bucket.length ?
 '<table class="w-full text-xs"><thead class="text-slate-500 border-b border-slate-700"><tr>'+
 '<th class="p-1.5 text-left">RS</th><th class="p-1.5 text-center">單數</th>'+
 '<th class="p-1.5 text-center">勝率</th><th class="p-1.5 text-right">期望值</th></tr></thead><tbody>'+
 D.rs_bucket.map(b=>`<tr class="border-b border-slate-700/50"><td class="p-1.5 font-bold text-white">${b.k}</td>
  <td class="p-1.5 text-center">${b.n}</td><td class="p-1.5 text-center text-cyan-400">${b.win}%</td>
  <td class="p-1.5 text-right font-mono ${b.exp>=0?'text-emerald-400':'text-red-400'}">${b.exp}%</td></tr>`).join('')+
 '</tbody></table>' : '<div class="text-slate-500 text-xs italic">冇進場 RS 資料</div>';

document.getElementById('persist').innerHTML =
 '<table class="w-full text-xs"><thead class="text-slate-500 border-b border-slate-700"><tr>'+
 '<th class="p-1.5 text-left">連續在榜</th><th class="p-1.5 text-right">次數</th></tr></thead><tbody>'+
 D.persist.map(p=>`<tr class="border-b border-slate-700/50"><td class="p-1.5">${p.m} 個月</td>
  <td class="p-1.5 text-right font-mono text-teal-400">${p.n}</td></tr>`).join('')+'</tbody></table>';

document.getElementById('mkt-split').innerHTML =
 '<table class="w-full text-xs"><thead class="text-slate-500 border-b border-slate-700"><tr>'+
 '<th class="p-1.5 text-left">市場</th><th class="p-1.5 text-center">單數</th>'+
 '<th class="p-1.5 text-center">勝率</th><th class="p-1.5 text-right">期望值</th></tr></thead><tbody>'+
 Object.keys(D.mkt_stat).map(m=>{const s=D.mkt_stat[m];
  return `<tr class="border-b border-slate-700/50"><td class="p-1.5 font-bold text-white">${m}</td>
  <td class="p-1.5 text-center">${s.n}</td><td class="p-1.5 text-center text-cyan-400">${s.win}%</td>
  <td class="p-1.5 text-right font-mono ${s.exp>=0?'text-emerald-400':'text-red-400'}">${s.exp}%</td></tr>`}).join('')+
 '</tbody></table>'+
 `<div class="mt-3 text-[10px] text-slate-500">平均換手 ${K.avg_turn}% · 年化成本拖累 ${K.cost_drag}%</div>`;

// ── ⑩⑪ ──
const tRow = (arr,cls) => '<table class="w-full text-xs"><tbody>'+arr.map(t=>
 `<tr class="border-b border-slate-700/50"><td class="p-1.5 font-bold text-white">${t.tk}</td>
  <td class="p-1.5 text-slate-500">${t.date}</td>
  <td class="p-1.5 text-right font-black font-mono ${cls}">${t.ret>=0?'+':''}${t.ret}%</td></tr>`).join('')+'</tbody></table>';
document.getElementById('top-t').innerHTML = tRow(D.top_trades,'text-emerald-400');
document.getElementById('bot-t').innerHTML = tRow(D.bot_trades,'text-red-400');

// ── ⑫ 持倉 ──
const opens = H.filter(t=>t.status==='OPEN');
document.getElementById('open-tbody').innerHTML = opens.length ? opens.map(t=>{
  const u = t.tk.endsWith('.T') ? '¥' : '$';
  const pct = t.px ? ((t.last_px/t.px-1)*100).toFixed(2) : '0.00';
  const c = pct>=0 ? 'text-emerald-400':'text-red-400';
  return `<tr class="border-b border-slate-700/50 hover:bg-slate-800">
   <td class="p-2 font-bold text-white">${t.tk}</td>
   <td class="p-2 text-[10px] text-slate-400 truncate max-w-[140px]">${t.sector||'N/A'}</td>
   <td class="p-2 text-slate-400">${t.date}</td>
   <td class="p-2 text-center text-slate-400">${t.entry_rs??'-'}</td>
   <td class="p-2 text-center text-lime-400 font-bold">${t.curr_rs??'-'}</td>
   <td class="p-2 text-right">${u}${t.px}</td><td class="p-2 text-right text-white">${u}${t.last_px}</td>
   <td class="p-2 text-right font-black font-mono ${c}">${pct>=0?'+':''}${pct}%</td></tr>`;
}).join('') : '<tr><td colspan="8" class="p-4 text-center text-slate-500">暫無持倉</td></tr>';
</script></body></html>"""


# =============================================================================
# 入口
# =============================================================================
def build_dashboard(trade_history, closes, out_dir, today_str, cfg=None,
                    regime=None, filename="index.html"):
    cfg = cfg or {}
    regime = regime or []

    D = _analyse(trade_history, closes, cfg)

    html = (_TPL
            .replace("__DATA__", json.dumps(D, ensure_ascii=False))
            .replace("__HIST__", json.dumps(trade_history, ensure_ascii=False))
            .replace("__REGIME__", json.dumps(regime, ensure_ascii=False))
            .replace("__TODAY__", str(today_str))
            .replace("__TOPN__", str(D['kpi']['top_n']))
            .replace("__NMONTHS__", str(D['kpi']['months']))
            .replace("__NTRADES__", str(D['kpi']['trades'])))

    path = os.path.join(out_dir, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)

    k = D['kpi']
    print("\n" + "=" * 72)
    print(f"📊 RS 策略總結｜{k['months']} 個月 · {k['trades']} 單")
    print("=" * 72)
    print(f"   總回報 {k['total_ret']:>7}%  |  CAGR {k['cagr']:>6}%  |  MaxDD {k['maxdd']:>6}%")
    print(f"   MAR    {k['mar']:>7}   |  Sharpe {k['sharpe']:>4}  |  Sortino {k['sortino']:>4}")
    print(f"   勝率   {k['win_rate']:>6}%  |  PF {k['pf']:>6}    |  正回報月 {k['pos_months']}%")
    print(f"   平均每單 {k['exp_mean']:>5}%  |  中位數 {k['exp_median']:>5}%  ← 中位數為負即靠尾部")
    print(f"   最好月 {k['best_m']:>6}%  |  最差月 {k['worst_m']:>6}%")
    print(f"   平均換手 {k['avg_turn']:>4}%  |  年化成本拖累 {k['cost_drag']}%")
    if D['contrib']:
        print(f"   集中度：" + " | ".join([f"Top{c['k']} {c['pct']}%" for c in D['contrib']]))
    print("=" * 72 + "\n")

    return D