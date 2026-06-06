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

        # running ATH (当日を除いた過去最高値・高値ベース) を列として保持
        df['_ath'] = df['high'].shift(1).expanding().max()

        # インジケータ計算（全期間に対して1回）
        df = calc_indicators(df)

        # 週足データ（weekly_po_first 用） — インジケータは付けず生のOHLCVを保持
        try:
            wdf = ticker.history(period="3y", interval="1wk", auto_adjust=True)
            if wdf is not None and len(wdf) >= 30:
                wdf.columns = [c.lower() for c in wdf.columns]
                wdf.index = pd.to_datetime(wdf.index)
                if wdf.index.tz is not None:
                    wdf.index = wdf.index.tz_localize(None)
                wdf = wdf[['open', 'high', 'low', 'close', 'volume']].copy()
                wdf = wdf[wdf['close'] > 0].dropna()
                df.attrs['weekly_df'] = wdf
            else:
                df.attrs['weekly_df'] = None
        except Exception:
            df.attrs['weekly_df'] = None

        # 日経平均5MA方向（マーケット強弱フィルター用）
        df.attrs['nikkei_ma5_up'] = analyze._load_nikkei()

        return df
    except Exception:
        return None


def check_at(df_full: pd.DataFrame, i: int, fn) -> bool:
    """df_full の i 番目の日をトリガー日としてパターンを評価する"""
    sl = df_full.iloc[:i + 1]
    ath_val = df_full['_ath'].iloc[i]
    sl.attrs['ath'] = float(ath_val) if pd.notna(ath_val) else None
    # 週足はトリガー日までに切り詰める（未来データを参照しない）
    wdf = df_full.attrs.get('weekly_df')
    if wdf is not None:
        try:
            trigger_date = df_full.index[i]
            sl.attrs['weekly_df'] = wdf[wdf.index <= trigger_date]
        except Exception:
            sl.attrs['weekly_df'] = wdf
    else:
        sl.attrs['weekly_df'] = None
    sl.attrs['nikkei_ma5_up'] = df_full.attrs.get('nikkei_ma5_up')
    try:
        return bool(fn(sl))
    except Exception:
        return False


# ── 週足表示パターン ──────────────────────────────────────────
# これらの手法は週足ベースで評価されるため、チャートも週足で表示する
WEEKLY_DISPLAY = {'cup_with_handle', 'weekly_po_first'}
CONTEXT_WEEKS  = 60   # トリガー週より前に表示する週数
AFTER_WEEKS    = 6    # トリガー週より後に表示する週数


def build_weekly_example(wdf, trigger_date):
    """週足DFからトリガー週周辺の週足OHLCV（週足MA付き）を構築する"""
    if wdf is None or len(wdf) < 30:
        return None
    w = wdf.copy()
    w['wma5']   = w['close'].rolling(5).mean()
    w['wma13']  = w['close'].rolling(13).mean()
    w['wma26']  = w['close'].rolling(26).mean()
    w['wvol13'] = w['volume'].rolling(13).mean()
    prior = w.index[w.index <= trigger_date]
    if len(prior) == 0:
        return None
    ti = w.index.get_loc(prior[-1])
    if isinstance(ti, slice):
        ti = ti.stop - 1
    start = max(0, ti - CONTEXT_WEEKS)
    end   = min(len(w), ti + AFTER_WEEKS + 1)
    chunk = w.iloc[start:end]
    trigger_idx = ti - start
    ohlcv = []
    for idx, row in chunk.iterrows():
        entry = {
            'time':  str(idx.date()),
            'open':  round(float(row['open']),  1),
            'high':  round(float(row['high']),  1),
            'low':   round(float(row['low']),   1),
            'close': round(float(row['close']), 1),
            'vol':   int(row['volume']),
        }
        if pd.notna(row['wma5']):   entry['ma5']  = round(float(row['wma5']),  1)
        if pd.notna(row['wma13']):  entry['ma25'] = round(float(row['wma13']), 1)
        if pd.notna(row['wma26']):  entry['ma75'] = round(float(row['wma26']), 1)
        if pd.notna(row['wvol13']): entry['vol_ma25'] = int(row['wvol13'])
        ohlcv.append(entry)
    return ohlcv, trigger_idx


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

                    # 週足表示パターンは週足チャートを構築
                    if key in WEEKLY_DISPLAY:
                        wk = build_weekly_example(df.attrs.get('weekly_df'), trigger_date)
                        if wk is None:
                            continue
                        w_ohlcv, w_trigger_idx = wk
                        examples[key].append({
                            'code':        code,
                            'date':        str(trigger_date.date()),
                            'trigger_idx': w_trigger_idx,
                            'ohlcv':       w_ohlcv,
                            'weekly':      True,
                        })
                        continue

                    start = max(0, i - CONTEXT_BARS)
                    end   = min(n, i + AFTER_BARS + 1)
                    chunk = df.iloc[start:end]
                    trigger_idx = i - start

                    ohlcv = []
                    for idx, row in chunk.iterrows():
                        entry = {
                            'time':  str(idx.date()),
                            'open':  round(float(row['open']),  1),
                            'high':  round(float(row['high']),  1),
                            'low':   round(float(row['low']),   1),
                            'close': round(float(row['close']), 1),
                            'vol':   int(row['volume']),
                        }
                        for ma in ('ma5', 'ma25', 'ma75'):
                            if ma in row and not pd.isna(row[ma]):
                                entry[ma] = round(float(row[ma]), 1)
                        if 'vol_ma25' in row and not pd.isna(row['vol_ma25']):
                            entry['vol_ma25'] = int(row['vol_ma25'])
                        ohlcv.append(entry)

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
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Hiragino Sans','Meiryo',sans-serif;background:#0d0d1a;color:#e0e0e0;font-size:13px}
header{background:#12122a;padding:12px 16px;border-bottom:1px solid #2a2a4a;position:sticky;top:0;z-index:100}
header h1{font-size:15px;color:#ffd700}
header p{font-size:11px;color:#666;margin-top:2px}
.tab-bar{display:flex;flex-wrap:wrap;gap:4px;padding:8px 16px;background:#12122a;border-bottom:1px solid #2a2a4a;position:sticky;top:52px;z-index:99}
.tab-btn{padding:5px 12px;border:1px solid #3a3a5a;border-radius:4px;background:#1a1a30;color:#aaa;cursor:pointer;font-size:11px;touch-action:manipulation}
.tab-btn.active{background:#2a4a7a;color:#fff;border-color:#4a7aaa}
.group{display:none;padding:12px 16px;gap:20px;flex-direction:column}
.group.active{display:flex}
.pattern-block{border:1px solid #2a2a4a;border-radius:6px;padding:12px;background:#0f0f20}
.pattern-header{display:flex;align-items:baseline;gap:8px;margin-bottom:10px}
.pattern-num{font-size:11px;color:#555;min-width:22px}
.pattern-name{font-size:14px;color:#ffd700;font-weight:bold}
.pattern-key{font-size:10px;color:#444;font-family:monospace}
.no-data{color:#444;font-size:12px;padding:16px 0;text-align:center}
.examples-row{display:flex;gap:10px;flex-wrap:wrap}
.example{flex:1;min-width:260px;max-width:420px}
.example-label{font-size:11px;color:#777;margin-bottom:3px}
.example-label b{color:#bbb}
canvas{display:block;border-radius:3px}
.gap2{margin-top:2px}
</style>
</head>
<body>
<header>
  <h1>📊 パターン検証レポート</h1>
  <p>生成日時: __GENERATED__ &nbsp;|&nbsp; 金色▲ = トリガー日</p>
</header>
<div class="tab-bar" id="tabBar"></div>
<div id="content"></div>

<script>
const DATA   = __DATA__;
const GROUPS = __GROUPS__;
const DPR    = Math.min(window.devicePixelRatio || 1, 2);
const CHART_H = 200;
const VOL_H   = 44;

// ── Canvas ローソク足描画 ──────────────────────────────────────
function drawCandle(canvas, ohlcv, triggerIdx, weekly) {
  const W  = canvas.width  / DPR;
  const H  = canvas.height / DPR;
  const ctx = canvas.getContext('2d');
  ctx.scale(DPR, DPR);

  const PL=54, PR=6, PT=6, PB=22;
  const CW = W-PL-PR, CH = H-PT-PB;
  const n  = ohlcv.length;

  let hi = -Infinity, lo = Infinity;
  ohlcv.forEach(d => {
    hi = Math.max(hi, d.high);
    lo = Math.min(lo, d.low);
    // MAがローソク足レンジ外でも確実に表示されるよう範囲に含める
    ['ma5','ma25','ma75'].forEach(k => {
      if (d[k] != null) { hi = Math.max(hi, d[k]); lo = Math.min(lo, d[k]); }
    });
  });
  const mg  = (hi - lo) * 0.06 || hi * 0.01 || 1;
  hi += mg; lo -= mg;
  const pr  = hi - lo;

  const bW  = CW / n;
  const cW  = Math.max(1, bW * 0.65);
  const toY = p => PT + CH * (1 - (p - lo) / pr);
  const toX = i => PL + (i + 0.5) * bW;

  // bg
  ctx.fillStyle = '#0d0d1a';
  ctx.fillRect(0, 0, W, H);

  // grid
  ctx.strokeStyle = '#1c1c2e';
  ctx.lineWidth = 0.5;
  for (let i=0;i<=4;i++){
    const y = PT + CH*i/4;
    ctx.beginPath(); ctx.moveTo(PL,y); ctx.lineTo(W-PR,y); ctx.stroke();
  }

  // trigger bg band
  if (triggerIdx >= 0 && triggerIdx < n) {
    const x = toX(triggerIdx);
    ctx.fillStyle = '#ffd70015';
    ctx.fillRect(x - bW*0.5, PT, bW, CH);
  }

  // candles
  ohlcv.forEach((d,i) => {
    const x    = toX(i);
    const isTr = i === triggerIdx;
    const isUp = d.close >= d.open;
    const col  = isTr ? '#ffd700' : (isUp ? '#26a69a' : '#ef5350');
    ctx.strokeStyle = col;
    ctx.fillStyle   = col;
    ctx.lineWidth   = 1;
    // wick
    ctx.beginPath();
    ctx.moveTo(x, toY(d.high));
    ctx.lineTo(x, toY(d.low));
    ctx.stroke();
    // body
    const top = Math.min(toY(d.open), toY(d.close));
    const bh  = Math.max(1, Math.abs(toY(d.open) - toY(d.close)));
    ctx.fillRect(x - cW/2, top, cW, bh);
  });

  // MA lines（週足表示時は週足MAラベル）
  const maStyles = weekly ? [
    { key: 'ma5',  color: '#f9a825', lw: 1.0, label: '5W'  },
    { key: 'ma25', color: '#42a5f5', lw: 1.5, label: '13W' },
    { key: 'ma75', color: '#ef6c00', lw: 1.5, label: '26W' },
  ] : [
    { key: 'ma5',  color: '#f9a825', lw: 1.0, label: 'MA5'  },
    { key: 'ma25', color: '#42a5f5', lw: 1.5, label: 'MA25' },
    { key: 'ma75', color: '#ef6c00', lw: 1.5, label: 'MA75' },
  ];
  maStyles.forEach(({ key, color, lw }) => {
    ctx.strokeStyle = color;
    ctx.lineWidth   = lw;
    ctx.beginPath();
    let started = false;
    ohlcv.forEach((d, i) => {
      if (d[key] == null) { started = false; return; }
      const x = toX(i), y = toY(d[key]);
      if (!started) { ctx.moveTo(x, y); started = true; }
      else           ctx.lineTo(x, y);
    });
    ctx.stroke();
  });

  // MA legend (top-right)
  ctx.font = '8px monospace';
  ctx.textBaseline = 'top';
  const lastBar = ohlcv[ohlcv.length - 1];
  let lx = W - PR - 2;
  maStyles.slice().reverse().forEach(({ key, color, label }) => {
    if (lastBar[key] == null) return;
    const txt = `${label}`;
    ctx.fillStyle = color;
    ctx.textAlign = 'right';
    ctx.fillText(txt, lx, PT + 1);
    lx -= ctx.measureText(txt).width + 10;
  });

  // trigger marker ▲
  if (triggerIdx >= 0 && triggerIdx < n) {
    const x  = toX(triggerIdx);
    const my = Math.min(toY(ohlcv[triggerIdx].low) + 14, H - PB + 10);
    ctx.fillStyle   = '#ffd700';
    ctx.font        = 'bold 11px sans-serif';
    ctx.textAlign   = 'center';
    ctx.textBaseline= 'middle';
    ctx.fillText('▲', x, my);
  }

  // Y labels
  ctx.fillStyle    = '#555';
  ctx.font         = '9px monospace';
  ctx.textAlign    = 'right';
  ctx.textBaseline = 'middle';
  for (let i=0;i<=4;i++){
    const p = hi - pr*i/4;
    const y = PT + CH*i/4;
    const lbl = p >= 10000 ? Math.round(p).toLocaleString()
              : p >= 1000  ? Math.round(p).toString()
              : p.toFixed(1);
    ctx.fillText(lbl, PL-3, y);
  }

  // X labels (first / trigger / last)
  ctx.fillStyle    = '#555';
  ctx.font         = '9px sans-serif';
  ctx.textAlign    = 'center';
  ctx.textBaseline = 'alphabetic';
  const seen = new Set();
  [0, triggerIdx, n-1].forEach(i => {
    if (i<0||i>=n||seen.has(i)) return;
    seen.add(i);
    const x = toX(i);
    if (x < PL+12 || x > W-PR-12) return;
    ctx.fillText(ohlcv[i].time.slice(5), x, H-4);
  });
}

function drawVolume(canvas, ohlcv, triggerIdx) {
  const W   = canvas.width  / DPR;
  const H   = canvas.height / DPR;
  const ctx = canvas.getContext('2d');
  ctx.scale(DPR, DPR);

  ctx.fillStyle = '#0d0d1a';
  ctx.fillRect(0, 0, W, H);

  const PL=54, PR=6, PT=2, PB=2;
  const CW = W-PL-PR, CH = H-PT-PB;
  const n  = ohlcv.length;
  const bW = CW / n;
  const allVols = ohlcv.map(d => d.vol_ma25 != null ? Math.max(d.vol, d.vol_ma25) : d.vol);
  const maxV = Math.max(...allVols) || 1;

  ohlcv.forEach((d,i) => {
    const x   = PL + (i+0.175)*bW;
    const w   = bW * 0.65;
    const h   = (d.vol / maxV) * CH;
    const isTr= i === triggerIdx;
    const isUp= d.close >= d.open;
    ctx.fillStyle = isTr ? '#ffd70099' : (isUp ? '#26a69a55' : '#ef535055');
    ctx.fillRect(x, PT+CH-h, w, h);
  });

  // vol_ma25 line (orange dashed)
  if (ohlcv.some(d => d.vol_ma25 != null)) {
    ctx.strokeStyle = '#ef6c00';
    ctx.lineWidth = 1.5;
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    let started = false;
    ohlcv.forEach((d, i) => {
      if (d.vol_ma25 == null) { started = false; return; }
      const x = PL + (i + 0.5) * bW;
      const y = PT + CH - (d.vol_ma25 / maxV) * CH;
      if (!started) { ctx.moveTo(x, y); started = true; }
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
    ctx.setLineDash([]);
  }
}

// ── DOM 構築・タブ ────────────────────────────────────────────
function buildTabs() {
  const bar = document.getElementById('tabBar');
  const total = GROUPS.reduce((s,g) => s+g.length, 0);
  let n = 0;
  GROUPS.forEach((g, gi) => {
    const s = n+1, e = Math.min(n+g.length, total);
    const btn = document.createElement('button');
    btn.className = 'tab-btn' + (gi===0 ? ' active' : '');
    btn.textContent = `${s}〜${e}`;
    btn.onclick = () => showGroup(gi);
    bar.appendChild(btn);
    n += g.length;
  });
}

function buildContent() {
  const cont = document.getElementById('content');
  let globalIdx = 0;
  GROUPS.forEach((keys, gi) => {
    const div = document.createElement('div');
    div.className = 'group' + (gi===0 ? ' active' : '');
    div.id = 'group-'+gi;
    div.innerHTML = keys.map(([key, label]) => {
      const idx = globalIdx++;
      const exs = (DATA[key]||[]).slice(0,3);
      const exHtml = exs.length === 0
        ? '<div class="no-data">ヒット事例なし（条件が稀すぎる可能性）</div>'
        : exs.map((ex,i) => `
          <div class="example">
            <div class="example-label"><b>${ex.code}</b> &nbsp;${ex.date}${ex.weekly ? ' &nbsp;<span style="color:#ffa726">週足</span>' : ''}</div>
            <canvas id="cc-${key}-${i}" style="width:100%;height:${CHART_H}px"></canvas>
            <canvas id="cv-${key}-${i}" class="gap2" style="width:100%;height:${VOL_H}px"></canvas>
          </div>`).join('');
      return `<div class="pattern-block">
        <div class="pattern-header">
          <span class="pattern-num">${idx+1}</span>
          <span class="pattern-name">${label}</span>
          <span class="pattern-key">${key}</span>
        </div>
        <div class="examples-row">${exHtml}</div>
      </div>`;
    }).join('');
    cont.appendChild(div);
  });
}

const drawn = {};
function showGroup(gi) {
  document.querySelectorAll('.group').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
  document.getElementById('group-'+gi).classList.add('active');
  document.querySelectorAll('.tab-btn')[gi].classList.add('active');
  if (!drawn[gi]) { drawn[gi]=true; requestAnimationFrame(() => renderGroup(gi)); }
}

function renderGroup(gi) {
  GROUPS[gi].forEach(([key]) => {
    (DATA[key]||[]).slice(0,3).forEach((ex,i) => {
      const cc = document.getElementById(`cc-${key}-${i}`);
      const cv = document.getElementById(`cv-${key}-${i}`);
      if (!cc || !cv) return;
      const W = cc.parentElement.clientWidth || 300;
      cc.width  = W * DPR; cc.height = CHART_H * DPR;
      cv.width  = W * DPR; cv.height = VOL_H   * DPR;
      drawCandle(cc,  ex.ohlcv, ex.trigger_idx, ex.weekly);
      drawVolume(cv,  ex.ohlcv, ex.trigger_idx);
    });
  });
}

buildTabs();
buildContent();
drawn[0] = true;
requestAnimationFrame(() => renderGroup(0));

window.addEventListener('resize', () => {
  Object.keys(drawn).forEach(gi => {
    if (document.getElementById('group-'+gi).classList.contains('active'))
      renderGroup(parseInt(gi));
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
