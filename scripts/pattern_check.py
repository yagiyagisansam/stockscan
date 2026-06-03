#!/usr/bin/env python3
"""
パターン検証レポート生成スクリプト
~120銘柄・過去2年分のデータから各テクニカルパターンのヒット事例を
3件ずつ収集し、インタラクティブなHTMLレポートを生成する。

使用方法:
    python scripts/pattern_check.py
出力:
    data/pattern_check.html
"""

import yfinance as yf
import pandas as pd
import numpy as np
import json
import os
import sys
import random
from datetime import datetime

# analyze.py から CHECKS・calc_indicators・各 chk_* 関数をインポート
_script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _script_dir)
import analyze
from analyze import CHECKS, calc_indicators

# ── 銘柄ユニバース (~120銘柄) ──────────────────────────────────
UNIVERSE = [
    # 自動車・輸送機器
    '7203','7267','7201','7270','7011','7012',
    # 電機・精密・半導体
    '6758','6861','8035','6902','6981','6954','6501','6594',
    '6367','6273','7741','6146','6857','7735','6752','6971',
    '6479','6963','6770','6762','6723',
    # 化学・素材
    '4063','4188','4911','4523','4519','4151','4568','4021',
    '4183','4208','4042','4204','5401',
    # 金融・保険
    '8306','8316','8411','8766','8750','8604','8697','8252',
    # 通信・IT・ソフト
    '9984','9432','9433','9437','2413','3659','4755','3984',
    '3817','3993','4390',
    # 小売・消費財
    '9983','8267','2802','2914','4452','4661',
    # 建設・不動産
    '1925','1928','8801','8802','3289','1828','1879',
    # 食品・飲料
    '2503','2502','2801','2871','2282',
    # 医薬品・バイオ
    '4507','4578','4506','4565',
    # インフラ・エネルギー
    '9501','9503','5019','5020',
    # 輸送・物流
    '9020','9022','9064',
    # その他製造
    '7751','7733','4902','7912','7013',
    # 中小型・成長株（パターンが出やすい）
    '6920','4385','2160','4478','4726','4480',
    '6488','3697','6369','4485','4169','5290','6625',
    '7974','3994','7061',
]
UNIVERSE = list(dict.fromkeys(UNIVERSE))  # 重複除去

EXAMPLES_NEEDED   = 3    # パターンあたり最低件数
EXAMPLES_STORE    = 6    # 収集する最大件数（表示は3件）
CONTEXT_BARS      = 45   # トリガー日より前に表示するバー数
AFTER_BARS        = 12   # トリガー日より後に表示するバー数
MIN_BARS          = 82   # パターン検出に必要な最低バー数


# ── データ取得 ────────────────────────────────────────────────

def download_stock(code: str) -> pd.DataFrame | None:
    """1銘柄の2年分日足をダウンロードしてインジケータ計算済みDFを返す"""
    try:
        ticker = yf.Ticker(f"{code}.T")
        df = ticker.history(period="2y", auto_adjust=True)
        if df is None or len(df) < MIN_BARS + AFTER_BARS:
            return None
        df.columns = [c.lower() for c in df.columns]
        df.index = pd.to_datetime(df.index)
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        df = df[['open', 'high', 'low', 'close', 'volume']].copy()
        df = df[df['close'] > 0].dropna()
        if len(df) < MIN_BARS + AFTER_BARS:
            return None

        # running ATH (当日を除いた過去最高値) を列として保持
        df['_ath'] = df['close'].shift(1).expanding().max()

        # インジケータ計算（全期間に対して1回）
        df = calc_indicators(df)

        # 週足データ（weekly_po_first 用）
        try:
            wdf = ticker.history(period="3y", interval="1wk", auto_adjust=True)
            if wdf is not None and len(wdf) >= 30:
                wdf.columns = [c.lower() for c in wdf.columns]
                if wdf.index.tz is not None:
                    wdf.index = wdf.index.tz_localize(None)
                wdf = wdf[['open', 'high', 'low', 'close', 'volume']].copy()
                wdf = wdf[wdf['close'] > 0].dropna()
                wdf = calc_indicators(wdf)
                df.attrs['weekly_df'] = wdf
            else:
                df.attrs['weekly_df'] = None
        except Exception:
            df.attrs['weekly_df'] = None

        return df
    except Exception:
        return None


def check_at(df_full: pd.DataFrame, i: int, fn) -> bool:
    """df_full の i 番目の日をトリガー日としてパターンを評価する"""
    sl = df_full.iloc[:i + 1]
    ath_val = df_full['_ath'].iloc[i]
    sl.attrs['ath']       = float(ath_val) if pd.notna(ath_val) else None
    sl.attrs['weekly_df'] = df_full.attrs.get('weekly_df')
    try:
        return bool(fn(sl))
    except Exception:
        return False


# ── パターン探索 ──────────────────────────────────────────────

def collect_examples(stocks: dict) -> dict:
    """全パターンのヒット事例を収集する"""
    func_map = {
        key: getattr(analyze, fn_name)
        for key, _, fn_name, _ in CHECKS
        if hasattr(analyze, fn_name)
    }

    examples: dict[str, list] = {key: [] for key, *_ in CHECKS}
    codes = list(stocks.keys())
    random.shuffle(codes)

    total_patterns = len(func_map)

    for stock_idx, code in enumerate(codes):
        df = stocks[code]
        n  = len(df)

        # 最新日から遡ってスキャン（新しい例を優先）
        for i in range(n - AFTER_BARS - 1, MIN_BARS - 1, -1):
            for key, fn in func_map.items():
                if len(examples[key]) >= EXAMPLES_STORE:
                    continue
                if check_at(df, i, fn):
                    trigger_date = df.index[i]
                    start = max(0, i - CONTEXT_BARS)
                    end   = min(n, i + AFTER_BARS + 1)
                    chunk = df.iloc[start:end]
                    trigger_idx = i - start

                    ohlcv = []
                    for idx, row in chunk.iterrows():
                        ohlcv.append({
                            'time':  str(idx.date()),
                            'open':  round(float(row['open']),  1),
                            'high':  round(float(row['high']),  1),
                            'low':   round(float(row['low']),   1),
                            'close': round(float(row['close']), 1),
                            'vol':   int(row['volume']),
                        })

                    examples[key].append({
                        'code':        code,
                        'date':        str(trigger_date.date()),
                        'trigger_idx': trigger_idx,
                        'ohlcv':       ohlcv,
                    })

        covered = sum(1 for v in examples.values() if len(v) >= EXAMPLES_NEEDED)
        print(f"  [{stock_idx+1}/{len(codes)}] {code}: {covered}/{total_patterns} patterns covered")

        if all(len(v) >= EXAMPLES_NEEDED for v in examples.values()):
            print("  ✅ All patterns have 3+ examples — stopping early.")
            break

    return examples


# ── HTML 生成 ─────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>パターン検証レポート</title>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Hiragino Sans','Meiryo',sans-serif;background:#0d0d1a;color:#e0e0e0;font-size:13px}
header{background:#12122a;padding:14px 20px;border-bottom:1px solid #2a2a4a;position:sticky;top:0;z-index:100}
header h1{font-size:16px;color:#ffd700;display:inline}
header span{color:#888;margin-left:12px;font-size:12px}
.tab-bar{display:flex;flex-wrap:wrap;gap:4px;padding:10px 20px;background:#12122a;border-bottom:1px solid #2a2a4a;position:sticky;top:45px;z-index:99}
.tab-btn{padding:4px 10px;border:1px solid #3a3a5a;border-radius:3px;background:#1a1a30;color:#aaa;cursor:pointer;font-size:11px}
.tab-btn.active{background:#2a4a7a;color:#fff;border-color:#4a7aaa}
.tab-btn:hover:not(.active){background:#222240}
.group{display:none;padding:16px 20px;gap:24px;flex-direction:column}
.group.active{display:flex}
.pattern-block{border:1px solid #2a2a4a;border-radius:6px;padding:14px;background:#0f0f20}
.pattern-header{display:flex;align-items:baseline;gap:10px;margin-bottom:12px}
.pattern-num{font-size:11px;color:#666;width:24px}
.pattern-name{font-size:14px;color:#ffd700;font-weight:bold}
.pattern-key{font-size:10px;color:#555;font-family:monospace}
.no-data{color:#555;font-size:12px;padding:20px;text-align:center}
.examples-row{display:flex;gap:12px;flex-wrap:wrap}
.example{flex:1;min-width:260px}
.example-label{font-size:11px;color:#888;margin-bottom:4px;padding-left:2px}
.example-label b{color:#bbb}
.chart-box{height:200px;border-radius:3px;overflow:hidden;background:#161626}
.vol-box{height:50px;border-radius:3px;overflow:hidden;background:#161626;margin-top:2px}
</style>
</head>
<body>
<header>
  <h1>📊 パターン検証レポート</h1>
  <span>生成日時: __GENERATED__</span>
</header>
<div class="tab-bar" id="tabBar"></div>
<div id="content"></div>

<script>
const DATA   = __DATA__;
const GROUPS = __GROUPS__;

let initializedGroups = {};

function buildTabs() {
  const bar = document.getElementById('tabBar');
  GROUPS.forEach((g, gi) => {
    const btn = document.createElement('button');
    btn.className = 'tab-btn' + (gi === 0 ? ' active' : '');
    btn.textContent = `${gi*5+1}〜${Math.min(gi*5+5, GROUPS.flat().length)}`;
    btn.onclick = () => showGroup(gi);
    bar.appendChild(btn);
  });
}

function buildContent() {
  const container = document.getElementById('content');
  GROUPS.forEach((keys, gi) => {
    const div = document.createElement('div');
    div.className = 'group' + (gi === 0 ? ' active' : '');
    div.id = 'group-' + gi;
    div.innerHTML = keys.map(([key, label], li) => buildPatternHTML(gi*5+li, key, label)).join('');
    container.appendChild(div);
  });
}

function buildPatternHTML(idx, key, label) {
  const examples = (DATA[key] || []).slice(0, 3);
  const exHtml = examples.length === 0
    ? `<div class="no-data">ヒット事例なし</div>`
    : examples.map((ex, i) => `
      <div class="example">
        <div class="example-label"><b>${ex.code}</b> &nbsp;${ex.date}</div>
        <div class="chart-box" id="c-${key}-${i}"></div>
        <div class="vol-box"   id="v-${key}-${i}"></div>
      </div>`).join('');
  return `
    <div class="pattern-block">
      <div class="pattern-header">
        <span class="pattern-num">${idx+1}</span>
        <span class="pattern-name">${label}</span>
        <span class="pattern-key">${key}</span>
      </div>
      <div class="examples-row">${exHtml}</div>
    </div>`;
}

function showGroup(gi) {
  document.querySelectorAll('.group').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
  document.getElementById('group-' + gi).classList.add('active');
  document.querySelectorAll('.tab-btn')[gi].classList.add('active');
  if (!initializedGroups[gi]) {
    initCharts(gi);
    initializedGroups[gi] = true;
  }
}

function initCharts(gi) {
  GROUPS[gi].forEach(([key]) => {
    const examples = (DATA[key] || []).slice(0, 3);
    examples.forEach((ex, i) => {
      const cEl = document.getElementById(`c-${key}-${i}`);
      const vEl = document.getElementById(`v-${key}-${i}`);
      if (!cEl || !vEl) return;

      const chart = LightweightCharts.createChart(cEl, {
        width: cEl.clientWidth || 280,
        height: 200,
        layout: { background: {type:'solid',color:'#161626'}, textColor:'#ccc' },
        grid:   { vertLines:{color:'#1e1e32'}, horzLines:{color:'#1e1e32'} },
        crosshair:  { mode: LightweightCharts.CrosshairMode.Normal },
        timeScale:  { timeVisible:true, secondsVisible:false, borderColor:'#2a2a4a' },
        rightPriceScale: { borderColor:'#2a2a4a' },
        handleScroll: true,
        handleScale: true,
      });

      const cs = chart.addCandlestickSeries({
        upColor:'#26a69a', downColor:'#ef5350',
        borderVisible:false,
        wickUpColor:'#26a69a', wickDownColor:'#ef5350',
      });
      const candles = ex.ohlcv.map(d => ({time:d.time,open:d.open,high:d.high,low:d.low,close:d.close}));
      cs.setData(candles);

      const triggerTime = ex.ohlcv[ex.trigger_idx].time;
      cs.setMarkers([{
        time: triggerTime,
        position: 'belowBar',
        color: '#ffd700',
        shape: 'arrowUp',
        text: '▲',
        size: 1,
      }]);

      // volume chart
      const vchart = LightweightCharts.createChart(vEl, {
        width: vEl.clientWidth || 280,
        height: 50,
        layout: { background:{type:'solid',color:'#161626'}, textColor:'#ccc' },
        grid:   { vertLines:{color:'#1e1e32'}, horzLines:{color:'#1e1e32'} },
        crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
        timeScale:  { visible:false },
        rightPriceScale: { visible:false },
        leftPriceScale:  { visible:false },
        handleScroll: true,
        handleScale:  true,
      });
      const vs = vchart.addHistogramSeries({ priceScaleId:'' });
      vchart.priceScale('').applyOptions({ scaleMargins:{top:0.05, bottom:0} });
      vs.setData(ex.ohlcv.map(d => ({
        time:  d.time,
        value: d.vol,
        color: d.close >= d.open ? '#26a69a66' : '#ef535066',
      })));

      // sync timescales
      chart.timeScale().subscribeVisibleLogicalRangeChange(range => {
        if (range) vchart.timeScale().setVisibleLogicalRange(range);
      });
      vchart.timeScale().subscribeVisibleLogicalRangeChange(range => {
        if (range) chart.timeScale().setVisibleLogicalRange(range);
      });
    });
  });
}

buildTabs();
buildContent();
// 初期グループを描画
initializedGroups[0] = true;
initCharts(0);

// ウィンドウリサイズ時にチャート幅を更新
window.addEventListener('resize', () => {
  document.querySelectorAll('.chart-box,.vol-box').forEach(el => {
    if (el._lwchart) el._lwchart.applyOptions({width: el.clientWidth});
  });
});
</script>
</body>
</html>
"""


def generate_html(examples: dict, checks: list, output_path: str, generated_at: str):
    groups_data = []
    for i in range(0, len(checks), 5):
        group = [[key, label] for key, label, _, _ in checks[i:i+5]]
        groups_data.append(group)

    # 各 examples は最大 EXAMPLES_NEEDED 件のみ埋め込む
    embed = {key: exs[:EXAMPLES_NEEDED] for key, exs in examples.items()}

    html = HTML_TEMPLATE
    html = html.replace('__GENERATED__', generated_at)
    html = html.replace('__DATA__',   json.dumps(embed,       ensure_ascii=False))
    html = html.replace('__GROUPS__', json.dumps(groups_data, ensure_ascii=False))

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)


# ── メイン ────────────────────────────────────────────────────

def main():
    generated_at = datetime.now().strftime('%Y-%m-%d %H:%M JST')
    print(f"=== Pattern Check Report Generator ===")
    print(f"Start: {generated_at}")
    print(f"Universe: {len(UNIVERSE)} stocks\n")

    # ── データダウンロード
    stocks = {}
    for idx, code in enumerate(UNIVERSE):
        print(f"[{idx+1:3}/{len(UNIVERSE)}] Downloading {code}.T ...", end=' ', flush=True)
        df = download_stock(code)
        if df is not None:
            stocks[code] = df
            print(f"OK ({len(df)} bars)")
        else:
            print("skip")

    print(f"\n✅ {len(stocks)} stocks ready\n")

    # ── パターン探索
    print("=== Searching for pattern examples ===")
    examples = collect_examples(stocks)

    # ── 結果サマリ
    missing = [(key, label) for key, label, _, _ in CHECKS if len(examples[key]) < EXAMPLES_NEEDED]
    print(f"\n=== Summary ===")
    print(f"Patterns with {EXAMPLES_NEEDED}+ examples: {len(CHECKS)-len(missing)}/{len(CHECKS)}")
    if missing:
        print("Missing:")
        for key, label in missing:
            print(f"  - {label} ({key}): {len(examples[key])} examples")

    # ── HTML 生成
    root = os.path.join(os.path.dirname(_script_dir), 'data')
    os.makedirs(root, exist_ok=True)
    output_path = os.path.join(root, 'pattern_check.html')
    generate_html(examples, CHECKS, output_path, generated_at)
    print(f"\n📄 Report saved: {output_path}")


if __name__ == '__main__':
    main()
