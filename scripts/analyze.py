#!/usr/bin/env python3
"""
StockScan JP - 日本株テクニカル分析スクリプト
GitHub Actions により毎日18:00 JST に実行される
"""

import yfinance as yf
import pandas as pd
import numpy as np
import json
import os
import re
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
import requests


# ─── データ取得 ──────────────────────────────────────────

def get_stock_data(code: str) -> pd.DataFrame | None:
    ticker = yf.Ticker(f"{code}.T")
    df = ticker.history(period="1y")
    if df is None or len(df) < 30:
        return None
    df.columns = [c.lower() for c in df.columns]
    df.index = pd.to_datetime(df.index)
    df = df[['open', 'high', 'low', 'close', 'volume']].copy()
    df = df[df['close'] > 0].copy()

    # ATH: 今日を除いた全履歴の最高値（高値ベース・split-adjusted via auto_adjust=True default）
    try:
        df_max = ticker.history(period="max")
        if df_max is not None and len(df_max) > 0:
            df_max.columns = [c.lower() for c in df_max.columns]
            df_max.index = pd.to_datetime(df_max.index)
            today_ts = df.index[-1]
            df_hist = df_max[df_max.index.normalize() < today_ts.normalize()]
            ath = float(df_hist['high'].max()) if len(df_hist) > 0 else float(df['high'].iloc[:-1].max())
        else:
            ath = float(df['high'].iloc[:-1].max())
    except Exception:
        ath = float(df['high'].iloc[:-1].max())
    df.attrs['ath'] = ath

    # Weekly data for K-09
    try:
        wdf = ticker.history(period="2y", interval="1wk")
        if wdf is not None and len(wdf) >= 30:
            wdf.columns = [c.lower() for c in wdf.columns]
            wdf = wdf[['open', 'high', 'low', 'close', 'volume']].copy()
            wdf = wdf[wdf['close'] > 0].copy()
            df.attrs['weekly_df'] = wdf
        else:
            df.attrs['weekly_df'] = None
    except Exception:
        df.attrs['weekly_df'] = None

    # 日経平均の5MA方向（マーケット強弱フィルター用）
    df.attrs['nikkei_ma5_up'] = _load_nikkei()

    return df


def _has_japanese(text: str) -> bool:
    return bool(re.search(r'[　-鿿＀-￯]', text))


def get_japanese_name(code: str) -> str:
    """Yahoo Finance search API（日本語ロケール）で会社名を取得する"""
    ticker = f"{code}.T"
    try:
        url = (
            f"https://query1.finance.yahoo.com/v1/finance/search"
            f"?q={ticker}&lang=ja&region=JP&quotesCount=5"
        )
        headers = {'User-Agent': 'Mozilla/5.0'}
        res = requests.get(url, headers=headers, timeout=10)
        data = res.json()
        for q in data.get('quotes', []):
            if q.get('symbol') == ticker:
                name = q.get('longname') or q.get('shortname') or ''
                if name:
                    return name
    except Exception:
        pass
    # fallback: chart endpoint（日本語名が含まれることがある）
    try:
        url2 = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
            f"?lang=ja&region=JP"
        )
        res2 = requests.get(url2, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        meta = res2.json().get('chart', {}).get('result', [{}])[0].get('meta', {})
        return meta.get('longName') or meta.get('shortName') or ''
    except Exception:
        return ''


# 日経平均5MA方向のキャッシュ（normalize済み日付 index の bool Series）
_NIKKEI_MA5_UP: pd.Series | None = None


def _load_nikkei() -> pd.Series:
    """日経平均（^N225）の5MAが前日比で上向きか否かを日付別に返す。
    取得失敗時は空 Series（=フィルター無効）。"""
    global _NIKKEI_MA5_UP
    if _NIKKEI_MA5_UP is not None:
        return _NIKKEI_MA5_UP
    try:
        n = yf.Ticker("^N225").history(period="3y")
        n.columns = [c.lower() for c in n.columns]
        n.index = pd.to_datetime(n.index)
        if n.index.tz is not None:
            n.index = n.index.tz_localize(None)
        ma5 = n['close'].rolling(5).mean()
        up = ma5 > ma5.shift(1)
        up.index = up.index.normalize()
        _NIKKEI_MA5_UP = up
    except Exception:
        _NIKKEI_MA5_UP = pd.Series(dtype=bool)
    return _NIKKEI_MA5_UP


def calc_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df['ma5']  = df['close'].rolling(5).mean()
    df['ma25'] = df['close'].rolling(25).mean()
    df['ma65'] = df['close'].rolling(65).mean()
    df['ma75'] = df['close'].rolling(75).mean()
    df['vol_ma25'] = df['volume'].rolling(25).mean()
    df['vol_ma65'] = df['volume'].rolling(65).mean()

    # RSI(14)
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0.0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    df['rsi'] = 100 - (100 / (1 + rs))

    # MACD
    ema12 = df['close'].ewm(span=12, adjust=False).mean()
    ema26 = df['close'].ewm(span=26, adjust=False).mean()
    df['macd'] = ema12 - ema26
    df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()

    # Bollinger Bands (20, 2σ)
    df['bb_mid']   = df['close'].rolling(20).mean()
    df['bb_std']   = df['close'].rolling(20).std()
    df['bb_upper'] = df['bb_mid'] + 2 * df['bb_std']
    df['bb_lower'] = df['bb_mid'] - 2 * df['bb_std']
    df['bb_width'] = (df['bb_upper'] - df['bb_lower']) / df['bb_mid'].replace(0, np.nan)

    # OBV
    obv = [0.0]
    for i in range(1, len(df)):
        prev_c = df['close'].iloc[i - 1]
        curr_c = df['close'].iloc[i]
        vol    = df['volume'].iloc[i]
        if curr_c > prev_c:
            obv.append(obv[-1] + vol)
        elif curr_c < prev_c:
            obv.append(obv[-1] - vol)
        else:
            obv.append(obv[-1])
    df['obv'] = obv
    df['obv_ma20'] = df['obv'].rolling(20).mean()

    # Williams %R (period=14)
    wpr_high = df['high'].rolling(14).max()
    wpr_low  = df['low'].rolling(14).min()
    denom = (wpr_high - wpr_low).replace(0, np.nan)
    df['williams_r'] = -100 * (wpr_high - df['close']) / denom

    return df


# ─── 共通ヘルパー ────────────────────────────────────────────

def _ma_up(df: pd.DataFrame, col: str, n: int = 5) -> bool:
    """col の移動平均が n 日前より高い（上向き）"""
    s = df[col]
    if len(s) <= n:
        return False
    a, b = s.iloc[-1], s.iloc[-1 - n]
    return bool(pd.notna(a) and pd.notna(b) and a > b)


def _ma_flat_or_up(df: pd.DataFrame, col: str, n: int = 5) -> bool:
    """col の移動平均が n 日前以上（水平〜上向き）"""
    s = df[col]
    if len(s) <= n:
        return False
    a, b = s.iloc[-1], s.iloc[-1 - n]
    return bool(pd.notna(a) and pd.notna(b) and a >= b)


def _ma25_down(df: pd.DataFrame, n: int = 5) -> bool:
    """25日線が n 日前より低い（下向き）"""
    s = df['ma25']
    if len(s) <= n:
        return False
    a, b = s.iloc[-1], s.iloc[-1 - n]
    return bool(pd.notna(a) and pd.notna(b) and a < b)


def _vol_surge(c: pd.Series, mult: float) -> bool:
    """当日出来高が25日平均の mult 倍以上"""
    vm = c.get('vol_ma25', np.nan)
    return bool(pd.notna(vm) and vm > 0 and c['volume'] >= vm * mult)


def _vol_prev(c: pd.Series, p: pd.Series, mult: float) -> bool:
    """当日出来高が前日比 mult 倍以上"""
    return bool(p['volume'] > 0 and c['volume'] >= p['volume'] * mult)


def _low_zone(df: pd.DataFrame, lookback: int = 60, thr: float = 0.33) -> bool:
    """直近 lookback 日のレンジ下位 thr 内に終値があるか（安値圏）"""
    seg = df.iloc[-lookback:] if len(df) >= lookback else df
    hi = float(seg['high'].max())
    lo = float(seg['low'].min())
    if hi <= lo:
        return False
    pos = (float(df.iloc[-1]['close']) - lo) / (hi - lo)
    return bool(pos <= thr)


def _near_support(df: pd.DataFrame, lookback: int = 20, tol: float = 0.03) -> bool:
    """当日安値が直近 lookback 日の安値圏（±tol）にある（支持線付近）"""
    if len(df) < lookback + 1:
        return False
    recent_low = float(df['low'].iloc[-lookback - 1:-1].min())
    if recent_low <= 0:
        return False
    return bool(float(df.iloc[-1]['low']) <= recent_low * (1 + tol))


def _downtrend(df: pd.DataFrame) -> bool:
    """下落トレンド（25日線が下向き）"""
    return _ma25_down(df, 5)


def _consolidation_break(df: pd.DataFrame, base_len: int = 15, tol: float = 0.10) -> bool:
    """直前 base_len 日がタイトな保ち合い（レンジ<tol）で当日その高値を上抜け"""
    end = len(df) - 1
    start = end - base_len
    if start < 0:
        return False
    base = df.iloc[start:end]
    avg = float(base['close'].mean())
    if avg <= 0:
        return False
    rng = (float(base['high'].max()) - float(base['low'].min())) / avg
    if rng >= tol:
        return False
    return bool(float(df.iloc[-1]['close']) > float(base['high'].max()))


def _direction_change(df: pd.DataFrame) -> bool:
    """直前に方向転換（保ち合いブレイク or 底値反転）があったか"""
    return _consolidation_break(df) or _downtrend(df) or _low_zone(df)


def _weekly_uptrend(df: pd.DataFrame) -> bool:
    """週足が上昇基調（終値>週足13MA かつ 週足13MA上向き）"""
    wdf = df.attrs.get('weekly_df', None)
    if wdf is None or len(wdf) < 14:
        return False
    wma13 = wdf['close'].rolling(13).mean()
    if pd.isna(wma13.iloc[-1]) or pd.isna(wma13.iloc[-2]):
        return False
    return bool(wdf['close'].iloc[-1] > wma13.iloc[-1] and wma13.iloc[-1] >= wma13.iloc[-2])


def _market_strong(df: pd.DataFrame) -> bool:
    """日経平均の5MAが上向きか。データが無ければ True（フィルター無効）"""
    series = df.attrs.get('nikkei_ma5_up', None)
    if series is None or len(series) == 0:
        return True
    try:
        date = df.index[-1].normalize()
    except Exception:
        return True
    sub = series[series.index <= date]
    if len(sub) == 0:
        return True
    return bool(sub.iloc[-1])


def _local_minima(prices: pd.Series, w: int = 5) -> list[tuple[int, float]]:
    """局所安値（前後 w 本の最小）を (位置, 値) で返す"""
    out: list[tuple[int, float]] = []
    for i in range(w, len(prices) - w):
        seg = prices.iloc[i - w:i + w + 1]
        v = float(prices.iloc[i])
        if v <= float(seg.min()) and v < float(prices.iloc[i - 1]) and v < float(prices.iloc[i + 1]):
            out.append((i, v))
    return out


# ─── テクニカル手法 ──────────────────────────────────────────

# 1 陽の包み足
def chk_bullish_engulfing(df: pd.DataFrame) -> bool:
    if len(df) < 27:
        return False
    p, c = df.iloc[-2], df.iloc[-1]
    if not (p['close'] < p['open']):          # 前日陰線
        return False
    if not (c['close'] > c['open']):          # 当日陽線
        return False
    # 明確に包む（始値<前日終値 かつ 終値>前日始値）
    if not (c['open'] < p['close'] and c['close'] > p['open']):
        return False
    p_body = abs(p['close'] - p['open'])
    c_body = c['close'] - c['open']
    if p_body <= 0 or c_body < p_body * 1.5:   # 実体1.5倍以上
        return False
    if c['close'] <= 0 or c_body / c['close'] < 0.02:  # 実体が株価の2%以上
        return False
    if not (_downtrend(df) or _near_support(df)):       # ①下落トレンド/支持線付近
        return False
    return _vol_prev(c, p, 1.5)                         # ②出来高


# 2 下ひげ陽線（ハンマー）
def chk_hammer(df: pd.DataFrame) -> bool:
    if len(df) < 61:
        return False
    p, c = df.iloc[-2], df.iloc[-1]
    body = abs(c['close'] - c['open'])
    if body <= 0 or not (c['close'] > c['open']):
        return False
    lo_shadow = min(c['open'], c['close']) - c['low']
    hi_shadow = c['high'] - max(c['open'], c['close'])
    if lo_shadow < 2 * body:           # 下ひげ>=実体2倍
        return False
    if c['close'] <= 0 or lo_shadow / c['close'] < 0.02:  # 下ひげ>=株価2%
        return False
    if hi_shadow > body * 0.3:         # 上ひげ<=実体0.3倍
        return False
    if not _vol_prev(c, p, 1.5):       # 出来高前日比1.5倍
        return False
    return _low_zone(df)               # 安値圏


# 3 朝の明星
def chk_morning_star(df: pd.DataFrame) -> bool:
    if len(df) < 27:
        return False
    if not _ma25_down(df):                              # ①25MA下向き
        return False
    d1, d2, d3 = df.iloc[-3], df.iloc[-2], df.iloc[-1]
    d1_body = abs(d1['close'] - d1['open'])
    if d1_body <= 0 or d1['close'] >= d1['open']:       # ②1日目大陰線
        return False
    if d1['open'] <= 0 or d1_body / d1['open'] < 0.03: # 始値-終値が3%以上
        return False
    if d2['open'] >= d1['close']:                       # 2日目ギャップダウン
        return False
    d2_body = abs(d2['close'] - d2['open'])
    if d2_body >= d1_body * 0.5:                        # 星型（小実体）
        return False
    if d3['close'] <= d3['open']:                       # ③3日目陽線
        return False
    d1_mid = (d1['open'] + d1['close']) / 2
    if d3['close'] <= d1_mid:                           # 1日目陰線の半値以上回復
        return False
    if not _vol_prev(df.iloc[-1], df.iloc[-2], 2.0):    # ④3日目出来高前日比2倍
        return False
    return True


# 4 陽の三兵
def chk_three_white_soldiers(df: pd.DataFrame) -> bool:
    if len(df) < 61:
        return False
    c3 = [df.iloc[-3], df.iloc[-2], df.iloc[-1]]
    for i, cd in enumerate(c3):
        if cd['close'] <= cd['open']:
            return False
        rng = cd['high'] - cd['low']
        body = cd['close'] - cd['open']
        if rng <= 0 or body / rng < 0.60:               # ④実体>=レンジ60%（上ひげ短い）
            return False
        if i > 0:
            prev = c3[i - 1]
            if cd['close'] <= prev['close']:            # 連続上昇
                return False
            # ②始値が前日実体内（窓なし・前日始値以上・前日終値未満）
            if cd['open'] >= prev['close'] or cd['open'] < prev['open']:
                return False
    v = [cd['volume'] for cd in c3]
    if not (v[0] < v[1] < v[2]):                        # ③出来高漸増
        return False
    if not (_low_zone(df) or _consolidation_break(df)): # ①底値圏/保ち合いブレイク
        return False
    return True


# 5 窓開け陽線
def chk_gap_up(df: pd.DataFrame) -> bool:
    if len(df) < 27:
        return False
    p, c = df.iloc[-2], df.iloc[-1]
    if p['close'] <= 0 or p['high'] <= 0:
        return False
    if c['open'] <= p['high']:                           # ②真の窓開け（始値>前日高値）
        return False
    if c['close'] <= c['open']:                         # 陽線
        return False
    if not _vol_surge(c, 1.5):                          # ③出来高25日平均1.5倍
        return False
    if not _consolidation_break(df):                    # ①保ち合いブレイク後
        return False
    return _ma_up(df, 'ma75')                            # ④75MA上向き


# 6 パーフェクトオーダー
def chk_perfect_order(df: pd.DataFrame) -> bool:
    if len(df) < 80:
        return False
    c = df.iloc[-1]
    if any(pd.isna([c['ma5'], c['ma25'], c['ma75']])):
        return False
    if not (c['close'] > c['ma5'] > c['ma25'] > c['ma75']):
        return False
    if not (_ma_up(df, 'ma5') and _ma_up(df, 'ma25') and _ma_up(df, 'ma75')):  # ①全MA上向き
        return False
    last5 = df.iloc[-5:]
    if (last5['close'] < last5['ma5']).any():           # ②MA5を下抜けしていない
        return False
    vm = c['vol_ma25']                                  # ③出来高25日平均以上
    if pd.isna(vm) or vm <= 0 or c['volume'] < vm * 0.8:
        return False
    return True


# 7 GC 25/75日線
def chk_gc_25_75(df: pd.DataFrame) -> bool:
    if len(df) < 77:
        return False
    p, c = df.iloc[-2], df.iloc[-1]
    if any(pd.isna([p['ma25'], p['ma75'], c['ma25'], c['ma75']])):
        return False
    if not (p['ma25'] < p['ma75'] and c['ma25'] >= c['ma75']):  # GC
        return False
    if not _ma_flat_or_up(df, 'ma75', 5):               # ①75MA水平or上向き
        return False
    if not _vol_prev(c, p, 1.5):                        # ②出来高前日比1.5倍
        return False
    if c['close'] <= c['ma75']:                         # ③株価>MA75
        return False
    return True


# 8 25日線デビュー買い
def chk_ma25_debut(df: pd.DataFrame) -> bool:
    if len(df) < 35:
        return False
    p, c = df.iloc[-2], df.iloc[-1]
    ma25 = c['ma25']
    if pd.isna(ma25) or ma25 <= 0:
        return False
    if c['close'] < ma25 * 1.005:                       # ②0.5%以上上抜け
        return False
    win = df.iloc[-11:-1]                               # ①過去10日に下回り
    if win['ma25'].isna().any() or not (win['close'] < win['ma25']).any():
        return False
    if df['ma25'].iloc[-1] < df['ma25'].iloc[-3]:       # ③25MA水平〜上向き転換中
        return False
    if not _vol_prev(c, p, 1.2):                        # ④出来高前日比1.2倍
        return False
    return True


# 9 75日線回復
def chk_ma75_recovery(df: pd.DataFrame) -> bool:
    if len(df) < 98:
        return False
    c = df.iloc[-1]
    p = df.iloc[-2]
    ma75 = c['ma75']
    if pd.isna(ma75) or ma75 <= 0:
        return False
    if c['close'] < ma75 * 1.005:                       # ②0.5%以上上回る
        return False
    win = df.iloc[-21:-1]                               # ①直近20日以上下回り
    if win['ma75'].isna().any() or not (win['close'] < win['ma75']).all():
        return False
    if not _vol_prev(c, p, 1.3):                        # ③出来高前日比1.3倍
        return False
    if not _ma_flat_or_up(df, 'ma75', 5):               # ④75MA水平〜上向き
        return False
    return True


# 10 MA収縮後ブレイク
def chk_ma_squeeze_breakout(df: pd.DataFrame) -> bool:
    if len(df) < 85:
        return False
    c = df.iloc[-1]
    win = df.iloc[-6:-1]                                # ①直近5日収束
    if win[['ma5', 'ma25', 'ma75']].isna().any().any():
        return False
    for _, r in win.iterrows():
        mmax = max(r['ma5'], r['ma25'], r['ma75'])
        mmin = min(r['ma5'], r['ma25'], r['ma75'])
        if r['close'] <= 0 or (mmax - mmin) / r['close'] >= 0.03:
            return False
    if not (c['close'] > c['ma5'] and c['close'] > c['ma25'] and c['close'] > c['ma75']):  # ②3MA上抜け
        return False
    if c['close'] <= float(win['high'].max()):          # 収束レンジを上抜け
        return False
    p = df.iloc[-2]
    return _vol_prev(c, p, 1.5)                          # ③出来高


# 11 株価が全MA上
def chk_price_above_all_ma(df: pd.DataFrame) -> bool:
    if len(df) < 80:
        return False
    c = df.iloc[-1]
    if any(pd.isna([c['ma5'], c['ma25'], c['ma75']])):
        return False
    if not (c['close'] > c['ma5'] > c['ma25'] > c['ma75']):  # ①
        return False
    if not (_ma_up(df, 'ma25') and _ma_up(df, 'ma75')):      # ②MA25,75上向き
        return False
    last5 = df.iloc[-5:]                                      # ③直近5日割り込まない
    if (last5['close'] < last5['ma5']).any():
        return False
    if (last5['close'] < last5['ma25']).any():
        return False
    if (last5['close'] < last5['ma75']).any():
        return False
    return True


# 12 出来高急増（前日比150%超）
def chk_vol_surge_150(df: pd.DataFrame) -> bool:
    if len(df) < 27:
        return False
    c, p = df.iloc[-1], df.iloc[-2]
    vm = c['vol_ma25']
    if pd.isna(vm) or vm <= 0 or p['volume'] <= 0:
        return False
    return bool(c['volume'] >= vm * 1.5 and c['volume'] >= p['volume'] * 1.5)


# 13 新高値＋出来高急増
def chk_new_high_vol(df: pd.DataFrame) -> bool:
    if len(df) < 100:
        return False
    c = df.iloc[-1]
    p = df.iloc[-2]
    lookback = df['close'].iloc[-252:-1] if len(df) >= 253 else df['close'].iloc[:-1]
    if c['close'] < float(lookback.max()):              # ①52週高値更新
        return False
    if not _vol_prev(c, p, 1.5):                        # ②出来高
        return False
    if any(pd.isna([c['ma5'], c['ma25'], c['ma75']])):
        return False
    if not (c['close'] > c['ma5'] and c['close'] > c['ma25'] and c['close'] > c['ma75']):  # ③全MA上
        return False
    return True


# 14 出来高枯れ→急増
def chk_vol_dry_surge(df: pd.DataFrame) -> bool:
    if len(df) < 37:
        return False
    c, p = df.iloc[-1], df.iloc[-2]
    vm = c['vol_ma25']
    if pd.isna(vm) or vm <= 0:
        return False
    win = df['volume'].iloc[-11:-1]                     # ①直近10日 60%以下
    if not (win <= vm * 0.6).all():
        return False
    if p['volume'] <= 0:                                # ②前日2倍 & 平均1.2倍
        return False
    if c['volume'] < p['volume'] * 2.0 or c['volume'] < vm * 1.2:
        return False
    if c['close'] <= p['close']:                        # ③上昇
        return False
    return True


# 15 VCP
def chk_vcp(df: pd.DataFrame) -> bool:
    if len(df) < 65:
        return False
    c = df.iloc[-1]
    if pd.isna(c['ma25']) or c['close'] < c['ma25']:
        return False
    if df['ma25'].iloc[-1] <= df['ma25'].iloc[-15]:     # 上昇トレンド
        return False

    def range_pct(start: int, end: int) -> float:
        seg = df.iloc[start:end]
        if len(seg) == 0 or seg['close'].mean() == 0:
            return 0.0
        return float((seg['high'].max() - seg['low'].min()) / seg['close'].mean())

    r1 = range_pct(-60, -40)
    r2 = range_pct(-40, -25)
    r3 = range_pct(-25, -10)
    if not (r1 > r2 > r3 > 0):                          # ①振れ幅縮小
        return False
    if r1 < 0.08:
        return False
    if range_pct(-10, -1) >= 0.05:                      # ③最終振れ幅5%以内
        return False
    v1 = df['volume'].iloc[-60:-40].mean()              # ②各調整で出来高縮小
    v2 = df['volume'].iloc[-40:-25].mean()
    v3 = df['volume'].iloc[-25:-10].mean()
    if not (v1 > v2 > v3 > 0):
        return False
    p = df.iloc[-2]
    if not _vol_prev(c, p, 1.5):                        # ④ブレイク出来高前日比1.5倍
        return False
    if c['close'] <= float(df['high'].iloc[-6:-1].max()):    # ブレイクアウト
        return False
    if not _market_strong(df):                          # ⑤日経5MA上向き
        return False
    return True


# 16 カップウィズハンドル
def chk_cup_with_handle(df: pd.DataFrame) -> bool:
    if len(df) < 60:
        return False
    wdf = df.attrs.get('weekly_df', None)
    if wdf is None or len(wdf) < 12:
        return False
    w = wdf
    n = len(w)
    if n < 4:
        return False
    handle = w.iloc[-2:]                                # ③ハンドル：直近1〜2週
    best = None
    for L in range(7, 66):                              # ②カップ長さ7〜65週
        if n - 2 - L < 0:
            break
        cup = w['close'].iloc[n - 2 - L:n - 2]
        if len(cup) < L:
            continue
        q = max(1, L // 4)
        left_high  = float(cup.iloc[:q].max())
        bottom     = float(cup.min())
        right_high = float(cup.iloc[-q:].max())
        if left_high <= 0 or bottom <= 0:
            continue
        depth = (left_high - bottom) / left_high
        if not (0.10 <= depth <= 0.35):                 # ①深さ10〜35%
            continue
        if right_high < left_high * 0.90:               # 右肩が左高値近くまで回復
            continue
        handle_low = float(handle['low'].min())
        if handle_low < bottom + (left_high - bottom) / 2.0:  # ハンドルはカップ上半分
            continue
        best = max(left_high, right_high)
        break
    if best is None:
        return False
    ten_week_vol = float(w['volume'].iloc[-10:].mean())  # ③ハンドル出来高枯れ
    handle_vol = float(handle['volume'].mean())
    if ten_week_vol <= 0 or handle_vol > ten_week_vol * 0.70:
        return False
    c = df.iloc[-1]                                       # ④日足ブレイク+出来高前日比1.5倍
    p = df.iloc[-2]
    if c['close'] <= best:
        return False
    if not _vol_prev(c, p, 1.5):
        return False
    return True


# 17 タイト保ち合い
def chk_tight_area(df: pd.DataFrame) -> bool:
    if len(df) < 80:
        return False
    c = df.iloc[-1]
    if any(pd.isna([c['ma25'], c['ma75']])):
        return False
    if not (c['close'] > c['ma25'] and c['close'] > c['ma75']):  # ③MA25,75の上
        return False
    if not _ma_up(df, 'ma75'):                          # ④75MA上向き
        return False
    win = df.iloc[-20:]                                 # ①直近20営業日
    avg = float(win['close'].mean())
    if avg <= 0 or (float(win['high'].max()) - float(win['low'].min())) / avg >= 0.05:  # 高安差5%以内
        return False
    if win['volume'].iloc[:10].mean() <= win['volume'].iloc[10:].mean():  # ②出来高縮小傾向
        return False
    return True


# 18 ダブルボトム（W底）
def chk_double_bottom(df: pd.DataFrame) -> bool:
    if len(df) < 50:
        return False
    seg = df.iloc[-60:] if len(df) >= 60 else df
    prices = seg['close']
    lows = _local_minima(prices, 5)
    if len(lows) < 2:
        return False
    (i1, v1), (i2, v2) = lows[-2], lows[-1]
    if i2 - i1 < 10:                                    # ④期間（数週間）
        return False
    if v1 <= 0 or abs(v1 - v2) / v1 >= 0.03:            # ①安値差3%以内
        return False
    neckline = float(prices.iloc[i1:i2 + 1].max())
    if neckline < max(v1, v2) * 1.05:                   # ②明確な反発
        return False
    pre = prices.iloc[:i1]                              # ⑤ネックラインより高所から下落開始
    if len(pre) == 0 or float(pre.max()) < neckline:
        return False
    c = df.iloc[-1]
    p = df.iloc[-2]
    if c['close'] <= neckline * 1.005:                  # 突破
        return False
    if not _vol_prev(c, p, 1.5):                        # ③出来高前日比1.5倍
        return False
    return True


# 19 フラッグ・ペナント
def chk_flag(df: pd.DataFrame) -> bool:
    if len(df) < 35:
        return False
    c = df.iloc[-1]
    p = df.iloc[-2]
    best = None
    for flag_len in range(5, 21):                        # ②フラッグ：急騰後5〜20日
        for pole_len in range(5, 11):                    # ①旗竿：5〜10日
            if len(df) < flag_len + pole_len + 1:
                continue
            pole = df.iloc[-(flag_len + pole_len + 1):-(flag_len + 1)]
            flag = df.iloc[-(flag_len + 1):-1]
            if len(pole) < pole_len or len(flag) < flag_len:
                continue
            if pole['close'].iloc[0] <= 0:
                continue
            pole_ret = (pole['close'].iloc[-1] - pole['close'].iloc[0]) / pole['close'].iloc[0]
            if pole_ret < 0.10:                          # 旗竿で10%以上急騰
                continue
            pole_top = float(pole['high'].max())
            if pole_top <= 0:
                continue
            flag_low = float(flag['low'].min())
            pullback = (pole_top - flag_low) / pole_top
            if not (0.01 <= pullback <= 0.08):           # ②上昇に対して1〜8%反落
                continue
            if flag['volume'].mean() >= pole['volume'].mean():  # ③フラッグ中出来高枯れ
                continue
            best = float(flag['high'].max())
            break
        if best is not None:
            break
    if best is None:
        return False
    if c['close'] <= best:                               # ④ペナント上限ブレイク＝トリガー
        return False
    if not _vol_prev(c, p, 1.5):                         # ブレイク出来高前日比1.5倍
        return False
    return True


# 22 高値圏コンソリデーション
def chk_high_level_tight(df: pd.DataFrame) -> bool:
    if len(df) < 80:
        return False
    c = df.iloc[-1]
    lookback = df['close'].iloc[-252:] if len(df) >= 252 else df['close']
    year_high = float(lookback.max())
    if year_high <= 0:
        return False
    if c['close'] < year_high * 0.90:                   # ①高値の90%以上
        return False
    win = df.iloc[-15:]                                 # ②3週間
    hi = float(win['high'].max())
    lo = float(win['low'].min())
    if hi <= 0 or (hi - lo) / hi >= 0.10:               # ③レンジ10%以内
        return False
    if (win['close'] < year_high * 0.85).any():
        return False
    vm = c['vol_ma25']                                  # ④出来高枯れ
    if pd.isna(vm) or vm <= 0 or win['volume'].mean() >= vm:
        return False
    return True


# 23 大陽線5%超（A-05）
def chk_large_bullish_5pct(df: pd.DataFrame) -> bool:
    if len(df) < 66:
        return False
    p, c = df.iloc[-2], df.iloc[-1]
    if p['close'] <= 0 or c['open'] <= 0:
        return False
    body = c['close'] - c['open']
    if body <= 0 or body / c['open'] < 0.05:            # ①陽線実体5%以上（始値-終値で計測）
        return False
    if not _vol_prev(c, p, 1.5):                        # ②出来高前日比1.5倍
        return False
    hi_shadow = c['high'] - max(c['open'], c['close'])
    if hi_shadow > body * 0.30:                         # ④上ひげ短い
        return False
    if not _direction_change(df):                       # ③直前に方向転換
        return False
    return True


# 24 上放れ陽線（A-07）
def chk_uwabane_large(df: pd.DataFrame) -> bool:
    if len(df) < 27:
        return False
    p, c = df.iloc[-2], df.iloc[-1]
    if p['high'] <= 0:
        return False
    if c['open'] <= p['high']:                           # ①上放れ（始値>前日高値で真の窓開け）
        return False
    if c['close'] <= c['open']:                         # ②陽線
        return False
    if not _vol_surge(c, 2.0):                          # ③出来高25日平均2倍
        return False
    lookback = df['close'].iloc[-252:-1] if len(df) >= 253 else df['close'].iloc[:-1]
    h52 = float(lookback.max())
    new_52w_high = h52 > 0 and c['close'] >= h52        # 52週高値ブレイク
    if not (new_52w_high or _consolidation_break(df)):  # ④保ち合い上限or52週高値からのブレイク
        return False
    return True


# 26 離れ小島（B-06）
def chk_island_reversal(df: pd.DataFrame) -> bool:
    if len(df) < 40:
        return False
    c = df.iloc[-1]
    p = df.iloc[-2]
    if c['close'] <= c['open']:                          # 当日は陽線
        return False
    if not _vol_prev(c, p, 1.5):                        # ④出来高前日比1.5倍
        return False
    vm = c['vol_ma25']
    if pd.isna(vm) or vm <= 0:
        return False
    for L in (1, 2, 3):                                 # ①島の長さ1〜3日
        if len(df) < L + 32:
            continue
        island    = df.iloc[-(L + 1):-1]                # 島（直近L日、当日除く）
        isl_high  = float(island['high'].max())
        prior     = df.iloc[-(L + 31):-(L + 1)]         # 島の前30営業日
        prior_low = float(prior['low'].min())
        # 孤立条件：島全体が直前30日の最安値より下（最近この水準に落ちていない＝真の島）
        if isl_high >= prior_low:
            continue
        # 上昇ギャップで島を脱出：当日始値が島の高値を明確に上抜ける
        if c['open'] <= isl_high:
            continue
        # ②元の価格帯へ回帰：当日終値が島より上の元の水準まで戻る
        if c['close'] < prior_low:
            continue
        # ③島の出来高枯れ
        if (island['volume'] > vm * 0.70).any():
            continue
        return True
    return False


# 30 25日線タッチ反発（C-08）
def chk_ma25_touch_rebound(df: pd.DataFrame) -> bool:
    if len(df) < 80:
        return False
    c = df.iloc[-1]
    ma25, ma75 = c['ma25'], c['ma75']
    if any(pd.isna([ma25, ma75])) or ma25 <= 0:
        return False
    win = df.iloc[-11:-1]                               # ①上昇トレンド中（2週間以上、株価が25MA・75MAより上）
    if win[['ma25', 'ma75']].isna().any().any():
        return False
    if (win['close'] < win['ma25']).any() or (win['close'] < win['ma75']).any():
        return False
    if c['close'] <= ma75:                              # 現在も75MA上
        return False
    if not _ma_flat_or_up(df, 'ma25', 5):               # ②25MA水平or上向き
        return False
    if abs(c['low'] - ma25) / ma25 > 0.01:              # ③安値が25MA±1%以内
        return False
    if c['close'] <= ma25 or c['close'] <= c['open']:   # ④終値>25MAの陽線
        return False
    vm = c['vol_ma25']                                  # ⑤出来高枯れ
    if pd.isna(vm) or vm <= 0 or c['volume'] > vm * 0.70:
        return False
    return True


# 31 ワインスタインS2（C-10）
def chk_weinstein_stage2(df: pd.DataFrame) -> bool:
    if len(df) < 170:
        return False
    c = df.iloc[-1]
    ma150 = df['close'].rolling(150).mean()
    cur = ma150.iloc[-1]
    if pd.isna(cur) or cur <= 0:
        return False
    if ma150.iloc[-1] <= ma150.iloc[-6]:                # 現在上向き
        return False
    if pd.isna(ma150.iloc[-20]) or pd.isna(ma150.iloc[-30]):
        return False
    if ma150.iloc[-20] > ma150.iloc[-30]:               # ①以前は下向き（転換）
        return False
    last5 = df.iloc[-5:]                                # ②株価が30週線上で保合い
    if (last5['close'].values < ma150.iloc[-5:].values).any():
        return False
    if df['volume'].iloc[-10:].mean() <= df['volume'].iloc[-30:-10].mean():  # ③出来高増加傾向
        return False
    return True


# 32 出来高200%急増（D-02）
def chk_vol_surge_200(df: pd.DataFrame) -> bool:
    if len(df) < 27:
        return False
    c = df.iloc[-1]
    vm = c['vol_ma25']
    if pd.isna(vm) or vm <= 0:
        return False
    if c['close'] <= c['open']:                          # ②陽線（株価上昇）
        return False
    return bool(c['volume'] >= vm * 2.0)                # ①25日平均の2倍以上


# 33 OBV新高値（D-06）
def chk_obv_new_high(df: pd.DataFrame) -> bool:
    if len(df) < 60:
        return False
    c = df.iloc[-1]
    obv_val = c['obv']
    if pd.isna(obv_val):
        return False
    obv_look = df['obv'].iloc[-252:-1] if len(df) >= 253 else df['obv'].iloc[:-1]
    if obv_val <= float(obv_look.max()):                # ①OBV52週新高値
        return False
    price_look = df['close'].iloc[-252:-1] if len(df) >= 253 else df['close'].iloc[:-1]
    if c['close'] >= float(price_look.max()):           # ②株価はまだ高値更新せず
        return False
    if not _ma_up(df, 'ma25'):                          # ④MA25上向き
        return False
    win = df.iloc[-25:]                                 # ③上昇日出来高>下落日出来高
    up_vol = win[win['close'] > win['open']]['volume'].sum()
    dn_vol = win[win['close'] < win['open']]['volume'].sum()
    return bool(up_vol > dn_vol)


# 35 出来高加速（D-09）
def chk_vol_acceleration(df: pd.DataFrame) -> bool:
    if len(df) < 30:
        return False
    last3 = df.iloc[-3:]
    if not (last3['close'] > last3['open']).all():      # ②3連続陽線
        return False
    v = last3['volume'].values
    if not (v[0] < v[1] < v[2]):                        # ①出来高増加
        return False
    c = df.iloc[-1]
    vm = c['vol_ma25']
    if pd.isna(vm) or vm <= 0:
        return False
    if not (v[0] <= vm and v[1] <= vm and v[2] > vm):   # ③1〜2日目は25日平均以下、3日目に超える
        return False
    up = pd.notna(c['ma25']) and c['close'] > c['ma25'] and _ma_up(df, 'ma25')
    if not (up or _consolidation_break(df)):            # ④上昇トレンド/保ち合いブレイク
        return False
    return True


# 36 スーパータイト（E-03）
def chk_super_tight(df: pd.DataFrame) -> bool:
    if len(df) < 80:
        return False
    c = df.iloc[-1]
    win = df.iloc[-15:]                                 # ①直近15日レンジ2%以内
    hi = float(win['high'].max())
    lo = float(win['low'].min())
    if hi <= 0 or (hi - lo) / hi >= 0.02:
        return False
    vm = c['vol_ma25']                                  # ②出来高70%以下に枯れ
    if pd.isna(vm) or vm <= 0 or win['volume'].mean() > vm * 0.70:
        return False
    if not (_ma_up(df, 'ma5') and _ma_up(df, 'ma25') and _ma_up(df, 'ma75')):  # ④全MA上向き
        return False
    wdf = df.attrs.get('weekly_df', None)               # ③週足もタイト
    if wdf is None or len(wdf) < 4:
        return False
    w = wdf.iloc[-3:]
    whi = float(w['high'].max())
    wlo = float(w['low'].min())
    if whi <= 0 or (whi - wlo) / whi >= 0.05:
        return False
    return True


# 37 ハイタイトフラッグ（E-06）
def chk_high_tight_flag(df: pd.DataFrame) -> bool:
    if len(df) < 90:
        return False
    c = df.iloc[-1]
    vm = c['vol_ma25']
    if pd.isna(vm) or vm <= 0:
        return False
    recent60 = df.iloc[-60:]
    peak_price = float(recent60['close'].max())
    peak_loc = int(recent60['close'].values.argmax())
    days_since = len(recent60) - 1 - peak_loc
    if days_since < 15 or days_since > 20:              # ③保ち合い3〜4週間
        return False
    peak_df_pos = len(df) - 60 + peak_loc
    start_pos = max(0, peak_df_pos - 40)                # ①先行上昇8週以内
    pre = df['close'].iloc[start_pos:peak_df_pos]
    if len(pre) == 0:
        return False
    base = float(pre.min())
    if base <= 0 or peak_price / base < 2.0:            # +100%以上
        return False
    depth = (peak_price - float(df['close'].iloc[-days_since:].min())) / peak_price
    if depth < 0.03 or depth > 0.15:                    # ②調整深度10〜15%以内
        return False
    flag_vol = df['volume'].iloc[-days_since:-1].mean() # ④保ち合い中急減
    if flag_vol >= vm * 0.7:
        return False
    return bool(c['volume'] >= vm * 1.5)                # ブレイク時急増


# 38 V字回復（F-03）
def chk_v_recovery(df: pd.DataFrame) -> bool:
    if len(df) < 30:
        return False
    c = df.iloc[-1]
    n = len(df)
    for rec_days in (1, 2, 3):                          # ③翌1〜3日で回復
        bi = n - 1 - rec_days                           # 底の位置
        if bi - 3 < 0:
            continue
        k = 0                                           # ①連続下落日数
        while bi - k - 1 >= 0 and df['close'].iloc[bi - k] < df['close'].iloc[bi - k - 1]:
            k += 1
        if k < 3:
            continue
        start_close = float(df['close'].iloc[bi - k])
        bottom_close = float(df['close'].iloc[bi])
        if start_close <= 0:
            continue
        if (start_close - bottom_close) / start_close < 0.10:  # 合計10%以上の急落
            continue
        vm = df['vol_ma25'].iloc[bi]                     # ②底で出来高急増
        if pd.isna(vm) or vm <= 0 or df['volume'].iloc[bi] < vm * 1.5:
            continue
        amt = start_close - bottom_close
        if amt <= 0 or (c['close'] - bottom_close) / amt < 0.50:  # 50%以上回復
            continue
        ok = True                                        # ④保ち合いを作らず回復
        for m in range(bi, n - 1):
            if df['close'].iloc[m + 1] < df['close'].iloc[m]:
                ok = False
                break
        if ok:
            return True
    return False


# 40 円形底（F-06）
def chk_saucer_bottom(df: pd.DataFrame) -> bool:
    if len(df) < 90:
        return False
    prices = df['close'].iloc[-90:].values.astype(float)
    volumes = df['volume'].iloc[-90:].values.astype(float)
    bottom_idx = int(np.argmin(prices))
    if bottom_idx < 35 or bottom_idx > 63:              # ①底値形成7週以上
        return False
    bottom = prices[bottom_idx]
    left_high = float(np.max(prices[:bottom_idx]))
    right_high = float(np.max(prices[bottom_idx:]))
    if left_high <= 0 or bottom <= 0:
        return False
    if (left_high - bottom) / left_high < 0.15:         # 深さ15%以上
        return False
    if right_high < left_high * 0.85:
        return False
    if prices[-1] < right_high * 0.90:
        return False
    rets = np.abs(np.diff(prices) / prices[:-1])        # ③緩やか（急落・急反発なし、日次5%以内）
    if np.nanmax(rets) > 0.05:
        return False
    b0 = max(0, bottom_idx - 7)                         # 底は丸く平坦（±7日のレンジ8%以内）
    b1 = min(len(prices), bottom_idx + 8)
    bot_zone = prices[b0:b1]
    bz_hi = float(np.max(bot_zone))
    if bz_hi <= 0 or (bz_hi - float(np.min(bot_zone))) / bz_hi > 0.08:
        return False
    bot_vol = float(np.mean(volumes[max(0, bottom_idx - 5):bottom_idx + 5]))  # ②底値出来高枯れ
    if bot_vol >= float(np.mean(volumes)):
        return False
    if float(np.mean(volumes[45:])) <= float(np.mean(volumes[:45])):  # ④回復時出来高増加
        return False
    return True


# 41 上昇三角形（G-03）
def chk_ascending_triangle(df: pd.DataFrame) -> bool:
    if len(df) < 65:
        return False
    segment = df.iloc[-61:-1]
    c = df.iloc[-1]
    highs = []
    for i in range(2, len(segment) - 2):
        h = float(segment['high'].iloc[i])
        if h == float(segment['high'].iloc[i - 2:i + 3].max()):
            highs.append(h)
    if len(highs) < 3:                                  # ①水平上値抵抗（複数タッチ）
        return False
    high_arr = np.array(highs)
    mean_h = high_arr.mean()
    if mean_h <= 0 or high_arr.std() / mean_h > 0.015:
        return False
    resistance = mean_h
    lows_idx, lows_val = [], []
    for i in range(2, len(segment) - 2):
        l = float(segment['low'].iloc[i])
        if l == float(segment['low'].iloc[i - 2:i + 3].min()):
            lows_idx.append(i)
            lows_val.append(l)
    if len(lows_idx) < 3:
        return False
    slope = float(np.polyfit(lows_idx, lows_val, 1)[0])  # ②切り上がる安値
    if slope <= 0:
        return False
    if c['close'] <= resistance:                        # 上限突破
        return False
    vm = c['vol_ma25']                                  # ③内部出来高枯れ
    if pd.isna(vm) or vm <= 0 or segment['volume'].mean() >= vm:
        return False
    p = df.iloc[-2]
    if not _vol_prev(c, p, 1.5):                        # ④突破出来高前日比1.5倍
        return False
    return True


# 42 上場来高値更新（G-05）
def chk_alltime_high(df: pd.DataFrame) -> bool:
    ath = df.attrs.get('ath', None)
    if ath is None or ath <= 0:
        return False
    c = df.iloc[-1]
    p = df.iloc[-2]
    if c['close'] <= ath:                               # ①上場来高値更新
        return False
    if not _vol_prev(c, p, 1.5):                        # ②出来高前日比1.5倍
        return False
    if any(pd.isna([c['ma5'], c['ma25'], c['ma75']])):
        return False
    if not (c['close'] > c['ma5'] and c['close'] > c['ma25'] and c['close'] > c['ma75']):  # ③全MA上
        return False
    if not _consolidation_break(df):                    # ④直前に保ち合い
        return False
    return True


# 43 ベース内ブレイク（G-07）
def chk_base_breakout(df: pd.DataFrame) -> bool:
    if len(df) < 80:
        return False
    c = df.iloc[-1]
    if any(pd.isna([c['ma5'], c['ma25'], c['ma75']])):
        return False
    if not (c['close'] > c['ma5'] and c['close'] > c['ma25'] and c['close'] > c['ma75']):  # ④全MA上
        return False
    base = df.iloc[-26:-1]                              # ①5週間以上のベース
    avg = float(base['close'].mean())
    if avg <= 0 or (float(base['high'].max()) - float(base['low'].min())) / avg >= 0.10:
        return False
    base_high = float(base['high'].max())
    if c['close'] <= base_high:                         # ③ベース上限突破
        return False
    vm = c['vol_ma25']                                  # ②ベース内出来高枯れ
    if pd.isna(vm) or vm <= 0 or base['volume'].mean() >= vm:
        return False
    if c['volume'] < vm * 1.5:                          # 出来高150%以上で突破
        return False
    if not _market_strong(df):                          # ⑤日経5MA上向き
        return False
    return True


# 44 ウィリアムズ%R（I-10）
def chk_williams_r(df: pd.DataFrame) -> bool:
    if len(df) < 20:
        return False
    c = df.iloc[-1]
    wpr = c['williams_r']
    if pd.isna(wpr) or wpr < -50:                       # ②現在 %R>=-50
        return False
    past = df['williams_r'].iloc[-15:-1].dropna()       # ①過去14日に %R<=-80
    if len(past) == 0 or not (past <= -80).any():
        return False
    p = df.iloc[-2]
    if not _vol_prev(c, p, 1.3):                        # ③出来高前日比1.3倍以上
        return False
    return True


# 46 ネックライン突破＋出来高（K-07）
def chk_neckline_vol(df: pd.DataFrame) -> bool:
    if len(df) < 35:
        return False
    c = df.iloc[-1]
    p = df.iloc[-2]
    seg = df.iloc[-31:-1]                               # 当日を除く直近30日
    highs = seg['high'].values.astype(float)
    peaks = []                                          # スイングハイ（局所高値）
    for i in range(2, len(highs) - 2):
        if highs[i] == float(np.max(highs[i - 2:i + 3])):
            peaks.append((i, highs[i]))
    if len(peaks) < 2:
        return False
    neckline = max(h for _, h in peaks)
    if neckline <= 0:
        return False
    touches = [(i, h) for i, h in peaks if h >= neckline * 0.99]  # ①水平抵抗に2回以上タッチ
    if len(touches) < 2:
        return False
    if touches[-1][0] - touches[0][0] < 8:             # タッチが時間的に離れている（真の水平線）
        return False
    if p['close'] > neckline:                          # 前日まではネックライン下
        return False
    if c['close'] <= neckline * 1.005:                 # ③出来高急増を伴って明確に突破
        return False
    return _vol_prev(c, p, 1.5)                         # ②出来高前日比1.5倍


# 47 週足PO初達成（K-09）
def chk_weekly_po_first(df: pd.DataFrame) -> bool:
    today = df.index[-1]
    if today.weekday() != 4:                            # 金曜のみ
        return False
    wdf_raw = df.attrs.get('weekly_df', None)
    if wdf_raw is None or len(wdf_raw) < 30:
        return False
    wdf = wdf_raw.copy()
    wdf['wma5']  = wdf['close'].rolling(5).mean()
    wdf['wma13'] = wdf['close'].rolling(13).mean()
    wdf['wma26'] = wdf['close'].rolling(26).mean()
    wdf['wvol26'] = wdf['volume'].rolling(26).mean()

    def _is_po(row: pd.Series) -> bool:
        if any(pd.isna(row.get(col, np.nan)) for col in ('wma5', 'wma13', 'wma26')):
            return False
        return bool(row['close'] > row['wma5'] > row['wma13'] > row['wma26'])

    if not _is_po(wdf.iloc[-1]):                         # ①今週PO成立
        return False
    if _is_po(wdf.iloc[-2]):                             # 先週は未成立（初達成）
        return False
    if pd.isna(wdf['wma26'].iloc[-1]) or pd.isna(wdf['wma26'].iloc[-3]):
        return False
    if wdf['wma26'].iloc[-1] < wdf['wma26'].iloc[-3]:   # ②週足26MA水平〜上向き
        return False
    wv = wdf['wvol26'].iloc[-1]                          # ③週足出来高26週平均以上で増加傾向
    if pd.isna(wv) or wv <= 0:
        return False
    if wdf['volume'].iloc[-1] < wv or wdf['volume'].iloc[-1] <= wdf['volume'].iloc[-2]:
        return False
    c = df.iloc[-1]                                      # ④日足も強気
    if pd.isna(c['ma25']) or c['close'] < c['ma25']:
        return False
    return True


# 48 上放れ並び赤
def chk_narabiaka(df: pd.DataFrame) -> bool:
    if len(df) < 27:
        return False
    d0, d1, d2 = df.iloc[-3], df.iloc[-2], df.iloc[-1]
    if d0['high'] <= 0 or d1['open'] <= d0['high']:    # ⑤上放れ（始値>前日高値で真の窓開け）
        return False
    for d in (d1, d2):                                  # ①両日陽線&実体2%以上
        if d['open'] <= 0 or d['close'] <= d['open']:
            return False
        if (d['close'] - d['open']) / d['open'] < 0.02:
            return False
    if d1['close'] <= 0 or abs(d2['open'] - d1['close']) / d1['close'] > 0.01:  # ②2日目始値が1日目終値付近（ギャップほぼなし）
        return False
    if d2['close'] <= d1['close'] * 1.005:              # ③2日目終値>1日目終値
        return False
    if not (d1['volume'] > d0['volume'] and d2['volume'] > d1['volume']):  # ④出来高増加傾向
        return False
    vm = df['vol_ma25'].iloc[-1]
    if pd.isna(vm) or vm <= 0 or max(d1['volume'], d2['volume']) < vm:  # 少なくとも1日は25日平均以上
        return False
    return True


# 49 パンパカパン押し目（PPP）
def chk_ppp_oshine(df: pd.DataFrame) -> bool:
    if len(df) < 80:
        return False
    c = df.iloc[-1]
    if any(pd.isna([c['ma5'], c['ma25'], c['ma75']])):
        return False
    if not _ma_up(df, 'ma25'):                          # ②25日線が上向き
        return False
    if c['ma25'] <= c['ma75']:                          # PO配列（25MA>75MA）
        return False
    had_po = False                                      # ①PO達成後
    win = df.iloc[-16:-1]
    for _, r in win.iterrows():
        if any(pd.isna([r['ma5'], r['ma25'], r['ma75']])):
            continue
        if r['close'] > r['ma5'] > r['ma25'] > r['ma75']:
            had_po = True
            break
    if not had_po:
        return False
    win2 = df.iloc[-6:]                                 # 直近1週間で明確に25MAを割っていない（25MAが支持）
    if (win2['ma25'].isna()).any() or (win2['close'] < win2['ma25'] * 0.98).any():
        return False
    if c['ma25'] <= 0 or abs(c['low'] - c['ma25']) / c['ma25'] > 0.02:  # 25日線まで押した
        return False
    if c['close'] <= c['open'] or c['close'] < c['ma25']:  # ④反発の陽線
        return False
    vm = c['vol_ma25']                                  # ③押し目で出来高枯れ
    if pd.isna(vm) or vm <= 0 or c['volume'] > vm * 0.70:
        return False
    return True


# ─── 手法定義テーブル ───────────────────────────────────────────────

CHECKS: list[tuple[str, str, str, bool]] = [
    # (key, label, func, is_standalone)

    # ── 基本ローソク足・トレンドパターン ──────────────────────
    ('bullish_engulfing',    '陽の包み足',                     'chk_bullish_engulfing',    True),
    ('hammer',               '下ひげ陽線（ハンマー）',          'chk_hammer',               True),
    ('morning_star',         '朝の明星',                       'chk_morning_star',          True),
    ('three_white_soldiers', '陽の三兵',                       'chk_three_white_soldiers',  True),
    ('gap_up',               '窓開け陽線',                     'chk_gap_up',                True),
    ('perfect_order',        'パーフェクトオーダー',            'chk_perfect_order',         False),
    ('gc_25_75',             'GC 25/75日線',                   'chk_gc_25_75',              False),
    ('ma25_debut',           '25日線デビュー買い',              'chk_ma25_debut',            False),
    ('ma75_recovery',        '75日線回復',                     'chk_ma75_recovery',         False),
    ('ma_squeeze_breakout',  'MA収縮後ブレイク',                'chk_ma_squeeze_breakout',   True),
    ('price_above_all_ma',   '株価が全MA上',                    'chk_price_above_all_ma',   False),
    ('vol_surge_150',        '出来高急増（前日比150%超）',      'chk_vol_surge_150',         False),
    ('new_high_vol',         '新高値＋出来高急増',              'chk_new_high_vol',          True),
    ('vol_dry_surge',        '出来高枯れ→急増',                 'chk_vol_dry_surge',         False),
    ('vcp',                  'VCP',                            'chk_vcp',                   True),
    ('cup_with_handle',      'カップウィズハンドル',            'chk_cup_with_handle',       True),
    ('tight_area',           'タイト保ち合い',                  'chk_tight_area',            True),
    ('double_bottom',        'ダブルボトム（W底）',             'chk_double_bottom',         True),
    ('flag',                 'フラッグ・ペナント',              'chk_flag',                  True),
    ('high_level_tight',     '高値圏コンソリデーション',        'chk_high_level_tight',      True),

    # ── A群 ────────────────────────────────────────────────
    ('large_bullish_5pct',   '大陽線5%超（A-05）',              'chk_large_bullish_5pct',   True),
    ('uwabane_large',        '上放れ陽線（A-07）',               'chk_uwabane_large',         True),

    # ── B群 ────────────────────────────────────────────────
    ('island_reversal',      '離れ小島（B-06）',                 'chk_island_reversal',       True),

    # ── C群 ────────────────────────────────────────────────
    ('ma25_touch_rebound',   '25日線タッチ反発（C-08）',         'chk_ma25_touch_rebound',    False),
    ('weinstein_stage2',     'ワインスタインS2（C-10）',         'chk_weinstein_stage2',      True),

    # ── D群 ────────────────────────────────────────────────
    ('vol_surge_200',        '出来高200%急増（D-02）',           'chk_vol_surge_200',         False),
    ('obv_new_high',         'OBV新高値（D-06）',                'chk_obv_new_high',          False),
    ('vol_acceleration',     '出来高加速（D-09）',               'chk_vol_acceleration',      False),

    # ── E群 ────────────────────────────────────────────────
    ('super_tight',          'スーパータイト（E-03）',           'chk_super_tight',           True),
    ('high_tight_flag',      'ハイタイトフラッグ（E-06）',       'chk_high_tight_flag',       True),

    # ── F群 ────────────────────────────────────────────────
    ('v_recovery',           'V字回復（F-03）',                  'chk_v_recovery',            True),
    ('saucer_bottom',        '円形底（F-06）',                   'chk_saucer_bottom',         True),

    # ── G群 ────────────────────────────────────────────────
    ('ascending_triangle',   '上昇三角形（G-03）',               'chk_ascending_triangle',    True),
    ('alltime_high',         '上場来高値更新（G-05）',           'chk_alltime_high',          True),
    ('base_breakout',        'ベース内ブレイク（G-07）',         'chk_base_breakout',         True),

    # ── I群 ────────────────────────────────────────────────
    ('williams_r',           'ウィリアムズ%R（I-10）',          'chk_williams_r',            True),

    # ── K群 ────────────────────────────────────────────────
    ('neckline_vol',         'ネックライン突破＋出来高（K-07）', 'chk_neckline_vol',          False),
    ('weekly_po_first',      '週足PO初達成（K-09）',             'chk_weekly_po_first',       True),

    # ── 追加手法 ────────────────────────────────────────────
    ('narabiaka',            '上放れ並び赤',                     'chk_narabiaka',             True),
    ('ppp_oshine',           'パンパカパン押し目（PPP）',          'chk_ppp_oshine',            True),
]

_FUNC_MAP = {key: globals()[fn] for key, _, fn, _ in CHECKS}


# ─── 単一銘柄分析 ─────────────────────────────────────────────

def analyze_stock(code: str, name: str = '') -> dict | None:
    df = get_stock_data(code)
    if df is None or len(df) < 30:
        print(f"  ⚠ {code}: データ取得失敗またはデータ不足")
        return None

    weekly_df = df.attrs.get('weekly_df', None)
    ath = df.attrs.get('ath', None)
    nikkei = df.attrs.get('nikkei_ma5_up', None)

    df = calc_indicators(df)
    # calc_indicators で attrs が落ちるため再設定
    df.attrs['weekly_df'] = weekly_df
    df.attrs['ath'] = ath
    df.attrs['nikkei_ma5_up'] = nikkei

    curr = df.iloc[-1]
    prev = df.iloc[-2]

    matches: list[dict]    = []
    supporting: list[dict] = []

    for key, label, _, is_standalone in CHECKS:
        try:
            if _FUNC_MAP[key](df):
                entry = {'key': key, 'label': label}
                if is_standalone:
                    matches.append(entry)
                else:
                    supporting.append(entry)
        except Exception:
            pass

    change = (curr['close'] - prev['close']) / prev['close'] * 100 if prev['close'] > 0 else 0.0

    chart = []
    for dt, row in df.tail(252).iterrows():
        chart.append({
            'time':   dt.strftime('%Y-%m-%d'),
            'open':   round(float(row['open']),  2),
            'high':   round(float(row['high']),  2),
            'low':    round(float(row['low']),   2),
            'close':  round(float(row['close']), 2),
            'volume': int(row['volume']),
            'ma5':    round(float(row['ma5']),  2) if not pd.isna(row['ma5'])  else None,
            'ma25':   round(float(row['ma25']), 2) if not pd.isna(row['ma25']) else None,
            'ma75':   round(float(row['ma75']), 2) if not pd.isna(row['ma75']) else None,
        })

    weekly_chart = []
    wdf_raw = df.attrs.get('weekly_df', None)
    if wdf_raw is not None and len(wdf_raw) > 0:
        wdf_c = wdf_raw.copy()
        wdf_c['wma5']  = wdf_c['close'].rolling(5).mean()
        wdf_c['wma25'] = wdf_c['close'].rolling(13).mean()
        wdf_c['wma75'] = wdf_c['close'].rolling(26).mean()
        for dt, row in wdf_c.tail(104).iterrows():
            weekly_chart.append({
                'time':   dt.strftime('%Y-%m-%d'),
                'open':   round(float(row['open']),  2),
                'high':   round(float(row['high']),  2),
                'low':    round(float(row['low']),   2),
                'close':  round(float(row['close']), 2),
                'volume': int(row['volume']),
                'ma5':    round(float(row['wma5']),  2) if not pd.isna(row['wma5'])  else None,
                'ma25':   round(float(row['wma25']), 2) if not pd.isna(row['wma25']) else None,
                'ma75':   round(float(row['wma75']), 2) if not pd.isna(row['wma75']) else None,
            })

    return {
        'code':         code,
        'name':         name,
        'close':        round(float(curr['close']), 2),
        'change':       round(float(change), 2),
        'volume':       int(curr['volume']),
        'matches':      matches,
        'supporting':   supporting,
        'chart':        chart,
        'weekly_chart': weekly_chart,
    }


# ─── メール送信 ───────────────────────────────────────────────

def send_email(matched: list[dict], date_str: str) -> None:
    smtp_host = os.environ.get('SMTP_HOST', 'smtp.gmail.com')
    smtp_port = int(os.environ.get('SMTP_PORT', '587'))
    smtp_user = os.environ.get('SMTP_USER', '')
    smtp_pass = os.environ.get('SMTP_PASS', '')
    to_email  = os.environ.get('TO_EMAIL', smtp_user)

    if not smtp_user or not smtp_pass:
        print("メール設定なし。スキップ。")
        return

    subject = f"【StockScan JP】テクニカル一致 {date_str}（{len(matched)}銘柄）"

    lines = [
        "StockScan JP テクニカル分析レポート",
        f"日付: {date_str}",
        f"一致銘柄数: {len(matched)} 件",
        "=" * 50,
    ]
    for s in matched:
        sign       = "+" if s['change'] >= 0 else ""
        standalone = "、".join(m['label'] for m in s['matches'])
        supplem    = "、".join(m['label'] for m in s.get('supporting', []))
        lines += [
            "",
            f"■ {s['code']}  {s['name']}",
            f"   終値 ¥{s['close']:,.0f}  ({sign}{s['change']:.1f}%)",
        ]
        if standalone:
            lines += [f"   単独シグナル: {standalone}"]
        if supplem:
            lines += [f"   補助シグナル: {supplem}"]
    lines += ["", "─" * 50, "https://yagiyagisansam.github.io/stocks.html"]

    body = "\n".join(lines)
    msg  = MIMEMultipart()
    msg['From']    = smtp_user
    msg['To']      = to_email
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain', 'utf-8'))

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as s:
            s.starttls()
            s.login(smtp_user, smtp_pass)
            s.send_message(msg)
        print(f"✉ メール送信完了 → {to_email}")
    except Exception as e:
        print(f"✗ メール送信エラー: {e}")


# ─── メイン ────────────────────────────────────────────────────

def main() -> None:
    base  = os.path.dirname(os.path.abspath(__file__))
    root  = os.path.join(base, '..')

    stocks_path  = os.path.join(root, 'data', 'stocks.json')
    results_path = os.path.join(root, 'data', 'results.json')

    with open(stocks_path, 'r', encoding='utf-8') as f:
        stocks = json.load(f)

    results: list[dict] = []
    matched: list[dict] = []

    stocks_updated = False
    for s in stocks:
        code = str(s['code'])
        name = s.get('name', '')
        if not name or not _has_japanese(name):
            ja_name = get_japanese_name(code)
            if ja_name:
                name = ja_name
                s['name'] = name
                stocks_updated = True
        print(f"→ {code} {name} を分析中...")
        result = analyze_stock(code, name)
        if result:
            results.append(result)
            if result['matches']:
                matched.append(result)
                all_labels = [m['label'] for m in result['matches'] + result['supporting']]
                print(f"  ✅ 一致: {all_labels}")
            else:
                print(f"  　 マッチなし")
        else:
            # データ取得失敗でも results.json に含める（フロントで「未分析」にならないよう）
            print(f"  ⚠ データ取得失敗")
            results.append({
                'code':         code,
                'name':         name,
                'close':        None,
                'change':       None,
                'volume':       None,
                'matches':      [],
                'supporting':   [],
                'chart':        [],
                'weekly_chart': [],
                'error':        True,
            })

    results.sort(key=lambda x: (-len(x['matches']), -len(x.get('supporting', [])), x['code']))

    now = datetime.now()
    output = {
        'date':      now.strftime('%Y/%m/%d'),
        'timestamp': now.isoformat(),
        'total':     len(results),
        'matched':   len(matched),
        'stocks':    results,
    }

    with open(results_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    if stocks_updated:
        with open(stocks_path, 'w', encoding='utf-8') as f:
            json.dump(stocks, f, ensure_ascii=False, indent=2)

    print(f"\n分析完了: {len(matched)}/{len(results)} 銘柄一致")

    send_email_flag = os.environ.get('SEND_EMAIL', 'true').lower() == 'true'
    if matched and send_email_flag:
        send_email(matched, now.strftime('%Y/%m/%d'))


if __name__ == '__main__':
    main()
