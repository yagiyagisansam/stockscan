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
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime


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

    # ATH (split-adjusted via auto_adjust=True default)
    try:
        df_max = ticker.history(period="max")
        if df_max is not None and len(df_max) > 0:
            df_max.columns = [c.lower() for c in df_max.columns]
            ath = float(df_max['close'].max())
        else:
            ath = float(df['close'].max())
    except Exception:
        ath = float(df['close'].max())
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

    return df


def get_stock_info(code: str) -> dict:
    try:
        t = yf.Ticker(f"{code}.T")
        info = t.info
        return {
            'name_en': info.get('shortName', ''),
            'name_ja': info.get('longName', ''),
        }
    except Exception:
        return {}


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


# ─── テクニカル手法 ──────────────────────────────────────────

# ── 既存手法（継続採用） ──────────────────────────────────────────

def chk_bullish_engulfing(df: pd.DataFrame) -> bool:
    """陽の包み足"""
    if len(df) < 2:
        return False
    p, c = df.iloc[-2], df.iloc[-1]
    p_bear = p['close'] < p['open']
    c_bull = c['close'] > c['open']
    engulf = c['open'] <= p['close'] and c['close'] >= p['open']
    c_body = c['close'] - c['open']
    p_body = p['open'] - p['close']
    return bool(p_bear and c_bull and engulf and c_body > p_body > 0)


def chk_hammer(df: pd.DataFrame) -> bool:
    """下ひげ陽線（ハンマー）"""
    if len(df) < 2:
        return False
    c = df.iloc[-1]
    body     = abs(c['close'] - c['open'])
    lo_shadow = min(c['open'], c['close']) - c['low']
    hi_shadow = c['high'] - max(c['open'], c['close'])
    total = c['high'] - c['low']
    if total <= 0 or body <= 0:
        return False
    return bool(
        c['close'] > c['open'] and
        lo_shadow >= 2 * body and
        hi_shadow <= body * 0.5
    )


def chk_morning_star(df: pd.DataFrame) -> bool:
    """朝の明星"""
    if len(df) < 3:
        return False
    d1, d2, d3 = df.iloc[-3], df.iloc[-2], df.iloc[-1]
    d1_body = abs(d1['close'] - d1['open'])
    d2_body = abs(d2['close'] - d2['open'])
    if d1_body <= 0:
        return False
    d1_mid = (d1['open'] + d1['close']) / 2
    return bool(
        d1['close'] < d1['open'] and
        d2_body < d1_body * 0.5 and
        d3['close'] > d3['open'] and
        d3['close'] > d1_mid
    )


def chk_three_white_soldiers(df: pd.DataFrame) -> bool:
    """陽の三兵"""
    if len(df) < 3:
        return False
    c = [df.iloc[-3], df.iloc[-2], df.iloc[-1]]
    for i, candle in enumerate(c):
        if candle['close'] <= candle['open']:
            return False
        if i > 0:
            if candle['close'] <= c[i - 1]['close']:
                return False
            if not (c[i - 1]['open'] <= candle['open'] <= c[i - 1]['close']):
                return False
    return True


def chk_gap_up(df: pd.DataFrame) -> bool:
    """窓開け陽線（真空ギャップ）"""
    if len(df) < 2:
        return False
    p, c = df.iloc[-2], df.iloc[-1]
    return bool(c['open'] > p['high'] and c['close'] > c['open'])


def chk_perfect_order(df: pd.DataFrame) -> bool:
    """パーフェクトオーダー"""
    if len(df) < 80:
        return False
    c = df.iloc[-1]
    if any(pd.isna([c['ma5'], c['ma25'], c['ma75']])):
        return False
    price_order = c['close'] > c['ma5'] > c['ma25'] > c['ma75']
    ma5_up  = df['ma5'].iloc[-1]  > df['ma5'].iloc[-5]
    ma25_up = df['ma25'].iloc[-1] > df['ma25'].iloc[-5]
    return bool(price_order and ma5_up and ma25_up)


def chk_gc_25_75(df: pd.DataFrame) -> bool:
    """ゴールデンクロス（25日/75日線）"""
    if len(df) < 77:
        return False
    p, c = df.iloc[-2], df.iloc[-1]
    if any(pd.isna([p['ma25'], p['ma75'], c['ma25'], c['ma75']])):
        return False
    return bool(p['ma25'] < p['ma75'] and c['ma25'] >= c['ma75'])


def chk_ma25_debut(df: pd.DataFrame) -> bool:
    """25日線デビュー買い（下向きから上向きへ転換）"""
    if len(df) < 35:
        return False
    ma25 = df['ma25'].dropna()
    if len(ma25) < 10:
        return False
    was_declining = ma25.iloc[-8] >= ma25.iloc[-5]
    now_rising    = ma25.iloc[-1] > ma25.iloc[-3]
    price_above   = df.iloc[-1]['close'] > df.iloc[-1]['ma25']
    return bool(was_declining and now_rising and price_above)


def chk_ma75_recovery(df: pd.DataFrame) -> bool:
    """75日線回復（下から上へ突破）"""
    if len(df) < 78:
        return False
    c = df.iloc[-1]
    if pd.isna(c['ma75']):
        return False
    closes = df['close'].iloc[-12:-1]
    ma75s  = df['ma75'].iloc[-12:-1]
    was_below = (closes < ma75s).any()
    return bool(was_below and c['close'] > c['ma75'])


def chk_ma_squeeze_breakout(df: pd.DataFrame) -> bool:
    """MA収晵後ブレイク"""
    if len(df) < 85:
        return False
    recent = df.iloc[-12:-1]
    if recent[['ma5', 'ma75']].isna().any().any():
        return False
    spread = (recent['ma5'] - recent['ma75']).abs() / recent['close']
    c = df.iloc[-1]
    was_tight = (spread < 0.03).all()
    breaking  = c['close'] > c['ma5'] and c['close'] > c['ma25'] and c['close'] > c['ma75']
    return bool(was_tight and breaking)


def chk_price_above_all_ma(df: pd.DataFrame) -> bool:
    """株価が全MA上（5・25・75日線）"""
    if len(df) < 76:
        return False
    c = df.iloc[-1]
    if any(pd.isna([c['ma5'], c['ma25'], c['ma75']])):
        return False
    return bool(c['close'] > c['ma5'] and c['close'] > c['ma25'] and c['close'] > c['ma75'])


def chk_vol_surge_150(df: pd.DataFrame) -> bool:
    """出来高急増（前日比150%以上 + 上昇）"""
    if len(df) < 2:
        return False
    c, p = df.iloc[-1], df.iloc[-2]
    if p['volume'] <= 0:
        return False
    return bool(c['volume'] >= p['volume'] * 1.5 and c['close'] > p['close'])


def chk_new_high_vol(df: pd.DataFrame) -> bool:
    """新高値＋出来高急増"""
    if len(df) < 100:
        return False
    c = df.iloc[-1]
    year_high = df['close'].iloc[-252:-1].max() if len(df) >= 253 else df['close'].iloc[:-1].max()
    new_high  = c['close'] >= year_high
    vol_ma    = c['vol_ma25']
    if pd.isna(vol_ma) or vol_ma <= 0:
        return False
    return bool(new_high and c['volume'] >= vol_ma * 1.5)


def chk_vol_dry_surge(df: pd.DataFrame) -> bool:
    """出来高枯れ→急増（4日縮小後に2倍超）"""
    if len(df) < 7:
        return False
    vols = df['volume'].iloc[-6:].tolist()
    drying = all(vols[i] >= vols[i + 1] for i in range(0, 4))
    surge  = vols[-1] >= vols[-2] * 2.0
    return bool(drying and surge)


def chk_vcp(df: pd.DataFrame) -> bool:
    """VCP（ボラティリティ収縮パターン）"""
    if len(df) < 65:
        return False
    c = df.iloc[-1]
    if pd.isna(c['ma25']) or c['close'] < c['ma25']:
        return False

    def range_pct(start: int, end: int) -> float:
        seg = df.iloc[start:end]
        if len(seg) == 0 or seg['close'].mean() == 0:
            return 0.0
        return float((seg['high'].max() - seg['low'].min()) / seg['close'].mean())

    r1 = range_pct(-60, -40)
    r2 = range_pct(-40, -20)
    r3 = range_pct(-20, -5)
    contracting = r1 > r2 > r3 > 0
    tight_now   = range_pct(-10, -1) < 0.08
    vol_dec     = df['volume'].iloc[-6:-1].mean() < df['volume'].iloc[-25:-6].mean()
    return bool(contracting and tight_now and vol_dec)


def chk_cup_with_handle(df: pd.DataFrame) -> bool:
    """カップウィズハンドル"""
    if len(df) < 65:
        return False
    cup    = df['close'].iloc[-55:-10]
    handle = df['close'].iloc[-10:]
    c      = df.iloc[-1]

    cup_left_high  = cup.iloc[:5].max()
    cup_bottom     = cup.min()
    cup_right_high = cup.iloc[-5:].max()
    if cup_left_high <= 0 or (cup_left_high - cup_bottom) <= 0:
        return False

    depth    = (cup_left_high - cup_bottom) / cup_left_high
    recovery = (cup_right_high - cup_bottom) / (cup_left_high - cup_bottom)
    rounded  = 0.1 < depth < 0.5 and recovery > 0.8

    handle_pull = (cup_right_high - handle.min()) / cup_right_high if cup_right_high > 0 else 1
    breakout    = c['close'] > cup_right_high
    vol_confirm = c['volume'] > c['vol_ma25'] * 1.3 if not pd.isna(c['vol_ma25']) else False

    return bool(rounded and handle_pull < 0.15 and breakout and vol_confirm)


def chk_tight_area(df: pd.DataFrame) -> bool:
    """タイト保ち合い"""
    if len(df) < 30:
        return False
    c = df.iloc[-1]
    if pd.isna(c['ma25']) or c['close'] < c['ma25']:
        return False
    rec = df.iloc[-8:]
    avg = rec['close'].mean()
    if avg <= 0:
        return False
    tight   = (rec['close'].max() - rec['close'].min()) / avg < 0.05
    vol_dec = rec['volume'].iloc[:4].mean() > rec['volume'].iloc[4:].mean()
    return bool(tight and vol_dec)


def chk_double_bottom(df: pd.DataFrame) -> bool:
    """ダブルボトム（W底ネックライン突破）"""
    if len(df) < 45:
        return False
    prices = df['close'].iloc[-45:]
    lows: list[tuple[int, float]] = []
    for i in range(3, len(prices) - 3):
        if prices.iloc[i] == prices.iloc[i - 3:i + 4].min():
            lows.append((i, float(prices.iloc[i])))
    if len(lows) < 2:
        return False
    (i1, v1), (i2, v2) = lows[-2], lows[-1]
    similar  = abs(v1 - v2) / v1 < 0.05 if v1 > 0 else False
    neckline = float(prices.iloc[i1:i2 + 1].max())
    between_recovery = neckline > max(v1, v2) * 1.05
    breaking = df.iloc[-1]['close'] > neckline
    return bool(similar and between_recovery and breaking)


def chk_flag(df: pd.DataFrame) -> bool:
    """フラッグ・ペナント"""
    if len(df) < 25:
        return False
    pole  = df.iloc[-20:-10]
    flag  = df.iloc[-10:-1]
    c     = df.iloc[-1]
    pole_c = pole['close']
    if pole_c.iloc[0] <= 0:
        return False
    pole_ret   = (pole_c.iloc[-1] - pole_c.iloc[0]) / pole_c.iloc[0]
    flag_avg   = flag['close'].mean()
    flag_range = (flag['close'].max() - flag['close'].min()) / flag_avg if flag_avg > 0 else 1
    breakout   = c['close'] > flag['close'].max()
    vol_surge  = c['volume'] > flag['volume'].mean() * 1.3 if flag['volume'].mean() > 0 else False
    return bool(pole_ret > 0.05 and flag_range < 0.06 and breakout and vol_surge)


def chk_inv_head_shoulders(df: pd.DataFrame) -> bool:
    """逆ヘッド&ショルダー"""
    if len(df) < 60:
        return False
    prices = df['close'].iloc[-60:]
    lows: list[tuple[int, float]] = []
    for i in range(4, len(prices) - 4):
        if prices.iloc[i] == prices.iloc[i - 4:i + 5].min():
            lows.append((i, float(prices.iloc[i])))
    if len(lows) < 3:
        return False
    ls, hd, rs = lows[-3], lows[-2], lows[-1]
    head_lowest   = hd[1] < ls[1] and hd[1] < rs[1]
    shoulders_sim = abs(ls[1] - rs[1]) / ls[1] < 0.06 if ls[1] > 0 else False
    neckline      = float(prices.iloc[ls[0]:rs[0] + 1].max())
    breaking      = df.iloc[-1]['close'] > neckline
    return bool(head_lowest and shoulders_sim and breaking)


def chk_52week_high(df: pd.DataFrame) -> bool:
    """52週新高値"""
    if len(df) < 100:
        return False
    curr     = df.iloc[-1]['close']
    lookback = df['close'].iloc[-252:-1] if len(df) >= 253 else df['close'].iloc[:-1]
    return bool(curr >= lookback.max())


def chk_high_level_tight(df: pd.DataFrame) -> bool:
    """高値圈コンソリデーション"""
    if len(df) < 30:
        return False
    c = df.iloc[-1]
    lookback  = df['close'].iloc[-252:] if len(df) >= 252 else df['close']
    year_high = lookback.max()
    if year_high <= 0:
        return False
    near_high = c['close'] >= year_high * 0.90
    rec       = df['close'].iloc[-15:]
    avg       = rec.mean()
    tight     = (rec.max() - rec.min()) / avg < 0.05 if avg > 0 else False
    breaking  = c['close'] >= float(rec.iloc[:-1].max())
    return bool(near_high and tight and breaking)


# ── A群 ──────────────────────────────────────────────────────

def chk_large_bullish_5pct(df: pd.DataFrame) -> bool:
    """A-05: 大陽田5%超（クライマックス買い除外）"""
    if len(df) < 66:
        return False
    p, c = df.iloc[-2], df.iloc[-1]
    if p['close'] <= 0:
        return False
    body_pct = (c['close'] - c['open']) / p['close']
    if body_pct < 0.05:
        return False
    low_65 = df['close'].iloc[-66:-1].min()
    if low_65 > 0 and (c['close'] / low_65 - 1) >= 0.30:
        return False
    return True


def chk_uwabane_large(df: pd.DataFrame) -> bool:
    """A-07: 上放れ陽線（窓開け+実体3%+出来高2倍、窓<10%）"""
    if len(df) < 27:
        return False
    p, c = df.iloc[-2], df.iloc[-1]
    if p['high'] <= 0 or p['close'] <= 0:
        return False
    if c['open'] <= p['high']:
        return False
    gap_pct = (c['open'] - p['high']) / p['high']
    if gap_pct >= 0.10:
        return False
    if (c['close'] - c['open']) / p['close'] < 0.03:
        return False
    vol_ma = c['vol_ma25']
    if pd.isna(vol_ma) or vol_ma <= 0:
        return False
    return bool(c['volume'] >= vol_ma * 2.0)


# ── B群 ──────────────────────────────────────────────────────

def chk_sankasen_akebono(df: pd.DataFrame) -> bool:
    """B-02: 三川明けの明星"""
    if len(df) < 3:
        return False
    d1, d2, d3 = df.iloc[-3], df.iloc[-2], df.iloc[-1]
    d1_body = abs(d1['close'] - d1['open'])
    d2_body = abs(d2['close'] - d2['open'])
    if d1_body <= 0:
        return False
    d1_mid = (d1['open'] + d1['close']) / 2
    return bool(
        d1['close'] < d1['open'] and
        d2['open'] < d1['close'] and
        d2_body < d1_body * 0.5 and
        d3['open'] > d2['high'] and
        d3['close'] > d3['open'] and
        d3['close'] > d1_mid
    )


def chk_island_reversal(df: pd.DataFrame) -> bool:
    """B-06: 離れ小峳（アイランドリバーサル）"""
    if len(df) < 15:
        return False
    c    = df.iloc[-1]
    prev = df.iloc[-2]
    if c['open'] <= prev['high'] or c['close'] <= c['open']:
        return False
    island_found = False
    for i in range(3, 11):
        if len(df) < i + 2:
            break
        pre_island   = df.iloc[-(i + 1)]
        island_start = df.iloc[-i]
        if island_start['open'] < pre_island['low']:
            island_found = True
            break
    if not island_found:
        return False
    vol_ma = c['vol_ma25']
    if pd.isna(vol_ma) or vol_ma <= 0:
        return False
    return bool(c['volume'] >= vol_ma * 1.5)


def chk_triple_bottom(df: pd.DataFrame) -> bool:
    """B-08: 三点底（3安値±3%以内+ネックライン突破+出来高）"""
    if len(df) < 45:
        return False
    prices = df['close'].iloc[-45:]
    lows: list[tuple[int, float]] = []
    for i in range(3, len(prices) - 3):
        if prices.iloc[i] == prices.iloc[i - 3:i + 4].min():
            lows.append((i, float(prices.iloc[i])))
    if len(lows) < 3:
        return False
    (i1, v1), (i2, v2), (i3, v3) = lows[-3], lows[-2], lows[-1]
    avg_low = (v1 + v2 + v3) / 3
    if avg_low <= 0:
        return False
    within_3pct = all(abs(v - avg_low) / avg_low < 0.03 for v in [v1, v2, v3])
    if not within_3pct:
        return False
    neckline = float(prices.iloc[i1:i3 + 1].max())
    c = df.iloc[-1]
    vol_ma = c['vol_ma25']
    if pd.isna(vol_ma) or vol_ma <= 0:
        return False
    return bool(c['close'] > neckline and c['volume'] >= vol_ma * 1.5)


def chk_engulfing_vol(df: pd.DataFrame) -> bool:
    """B-10: 包み足+出来高+RSI（実体1.5倍+出来高150%+RSI≤40+MA付近）"""
    if len(df) < 25:
        return False
    p, c = df.iloc[-2], df.iloc[-1]
    p_body = abs(p['close'] - p['open'])
    c_body = abs(c['close'] - c['open'])
    if p_body <= 0 or p['volume'] <= 0:
        return False
    engulf = (
        p['close'] < p['open'] and
        c['close'] > c['open'] and
        c['open'] <= p['close'] and
        c['close'] >= p['open'] and
        c_body >= p_body * 1.5
    )
    if not engulf:
        return False
    if c['volume'] < p['volume'] * 1.5:
        return False
    rsi = c['rsi']
    if pd.isna(rsi) or rsi > 40:
        return False
    ma25 = c['ma25']
    ma75 = c['ma75']
    close = c['close']
    near_ma25 = not pd.isna(ma25) and ma25 > 0 and abs(close - ma25) / ma25 <= 0.05
    near_ma75 = not pd.isna(ma75) and ma75 > 0 and abs(close - ma75) / ma75 <= 0.05
    return bool(near_ma25 or near_ma75)


def chk_gap_vol(df: pd.DataFrame) -> bool:
    """B-11: 窓開け+出来高（窓≥1%+出来高2倍+52週高値5%以内）"""
    if len(df) < 27:
        return False
    p, c = df.iloc[-2], df.iloc[-1]
    if p['high'] <= 0:
        return False
    if (c['open'] - p['high']) / p['high'] < 0.01:
        return False
    if c['close'] <= c['open']:
        return False
    vol_ma = c['vol_ma25']
    if pd.isna(vol_ma) or vol_ma <= 0 or c['volume'] < vol_ma * 2.0:
        return False
    lookback = df['close'].iloc[-252:-1] if len(df) >= 253 else df['close'].iloc[:-1]
    high_52w = lookback.max()
    return bool(high_52w > 0 and c['close'] >= high_52w * 0.95)


# ── C群 ──────────────────────────────────────────────────────

def chk_ma25_touch_rebound(df: pd.DataFrame) -> bool:
    """C-08: 25日線タッチ反発（MA25上向き+MA75<MA25+昨日MA25±5%+陽線+RSI35-65）"""
    if len(df) < 30:
        return False
    p, c = df.iloc[-2], df.iloc[-1]
    ma25 = c['ma25']
    ma75 = c['ma75']
    rsi  = c['rsi']
    if any(pd.isna([ma25, ma75, rsi])):
        return False
    if df['ma25'].iloc[-1] <= df['ma25'].iloc[-5]:
        return False
    if ma75 >= ma25:
        return False
    p_ma25 = p['ma25']
    if pd.isna(p_ma25) or p_ma25 <= 0:
        return False
    if abs(p['close'] - p_ma25) / p_ma25 > 0.05:
        return False
    if c['close'] <= c['open']:
        return False
    return bool(35 <= rsi <= 65)


def chk_weinstein_stage2(df: pd.DataFrame) -> bool:
    """C-10: ワインスタインステージ2（MA75フラット→上向き転換+株価上方+出来高）"""
    if len(df) < 80:
        return False
    c = df.iloc[-1]
    ma75 = df['ma75']
    ma75_curr = ma75.iloc[-1]
    if pd.isna(ma75_curr) or ma75_curr <= 0:
        return False
    ma75_65 = ma75.iloc[-66:-1].dropna()
    if len(ma75_65) < 30:
        return False
    ma75_range = (ma75_65.max() - ma75_65.min()) / ma75_65.mean()
    if ma75_range > 0.03:
        return False
    if ma75.iloc[-1] <= ma75.iloc[-6]:
        return False
    if c['close'] <= ma75_curr:
        return False
    vol_ma65 = df['vol_ma65'].iloc[-1]
    if pd.isna(vol_ma65) or vol_ma65 <= 0:
        vol_ma65 = df['volume'].iloc[-66:-1].mean()
    return bool(c['volume'] >= vol_ma65 * 1.5)


# ── D群 ──────────────────────────────────────────────────────

def chk_vol_surge_200(df: pd.DataFrame) -> bool:
    """D-02: 出来高200%急増+大陽線+52週高値付近"""
    if len(df) < 100:
        return False
    p, c = df.iloc[-2], df.iloc[-1]
    if p['volume'] <= 0 or p['close'] <= 0:
        return False
    if c['volume'] < p['volume'] * 2.0:
        return False
    if (c['close'] - c['open']) / p['close'] < 0.03:
        return False
    lookback = df['close'].iloc[-252:-1] if len(df) >= 253 else df['close'].iloc[:-1]
    high_52w = lookback.max()
    return bool(high_52w > 0 and c['close'] >= high_52w * 0.97)


def chk_obv_new_high(df: pd.DataFrame) -> bool:
    """D-06: OBV新高値（OBVぇ20日高値更新、価格はまだ高値更新せず）"""
    if len(df) < 25:
        return False
    c = df.iloc[-1]
    obv_val = c['obv']
    if pd.isna(obv_val):
        return False
    obv_20d_high   = df['obv'].iloc[-21:-1].max()
    price_20d_high = df['close'].iloc[-21:-1].max()
    return bool(obv_val > obv_20d_high and c['close'] < price_20d_high)


def chk_pocket_pivot(df: pd.DataFrame) -> bool:
    """D-07: ポケットピボット（陽線+出来高>直近10日の陰線日出来高最大値+MA25以上）"""
    if len(df) < 30:
        return False
    c = df.iloc[-1]
    if pd.isna(c['ma25']) or c['ma25'] <= 0:
        return False
    if c['close'] <= c['open'] or c['close'] < c['ma25']:
        return False
    past10     = df.iloc[-11:-1]
    down_days  = past10[past10['close'] < past10['open']]
    if len(down_days) == 0:
        return False
    max_down_vol = down_days['volume'].max()
    return bool(c['volume'] > max_down_vol)


def chk_vol_acceleration(df: pd.DataFrame) -> bool:
    """D-09: 出来高加速（3連続陽線+終値上昇+出来高増加）"""
    if len(df) < 4:
        return False
    last3 = df.iloc[-3:]
    if not all(last3['close'] > last3['open']):
        return False
    closes = last3['close'].values
    if not (closes[0] < closes[1] < closes[2]):
        return False
    vols = last3['volume'].values
    return bool(vols[0] < vols[1] < vols[2])


# ── E群 ──────────────────────────────────────────────────────

def chk_super_tight(df: pd.DataFrame) -> bool:
    """E-03: スーパータイト（5日値幅<3%+出来高枯溇+52週高値5%以内+MA25上）"""
    if len(df) < 30:
        return False
    c = df.iloc[-1]
    ma25    = c['ma25']
    vol_ma  = c['vol_ma25']
    if pd.isna(ma25) or pd.isna(vol_ma) or ma25 <= 0 or vol_ma <= 0:
        return False
    if c['close'] < ma25:
        return False
    recent5 = df.iloc[-5:]
    avg5    = recent5['close'].mean()
    if avg5 <= 0 or (recent5['close'].max() - recent5['close'].min()) / avg5 >= 0.03:
        return False
    if recent5['volume'].mean() >= vol_ma * 0.33:
        return False
    lookback = df['close'].iloc[-252:-1] if len(df) >= 253 else df['close'].iloc[:-1]
    high_52w = lookback.max()
    return bool(high_52w > 0 and c['close'] >= high_52w * 0.95)


def chk_high_tight_flag(df: pd.DataFrame) -> bool:
    """E-06: ハイタイトフラッグ（56日で100%超+20%以内調整+出来高枯れ→急増）"""
    if len(df) < 90:
        return False
    c = df.iloc[-1]
    vol_ma = c['vol_ma25']
    if pd.isna(vol_ma) or vol_ma <= 0:
        return False
    recent60   = df.iloc[-60:]
    peak_price = float(recent60['close'].max())
    peak_loc   = recent60['close'].values.argmax()
    days_since_peak = len(recent60) - 1 - peak_loc
    if days_since_peak < 7 or days_since_peak > 42:
        return False
    peak_df_pos = len(df) - 60 + peak_loc
    start_pos   = max(0, peak_df_pos - 56)
    pre_peak    = df['close'].iloc[start_pos:peak_df_pos]
    if len(pre_peak) == 0:
        return False
    base_price = float(pre_peak.min())
    if base_price <= 0 or peak_price / base_price < 2.0:
        return False
    if (peak_price - c['close']) / peak_price > 0.20:
        return False
    flag_vol = df['volume'].iloc[-days_since_peak - 1:-1].mean()
    if flag_vol >= vol_ma * 0.5:
        return False
    return bool(c['volume'] >= vol_ma * 1.5)


# ── F群 ──────────────────────────────────────────────────────

def chk_v_recovery(df: pd.DataFrame) -> bool:
    """F-03: V字回復（前日5-15%下落→当日50%以上回復+出来高少+MA付近）"""
    if len(df) < 30:
        return False
    d_prev2 = df.iloc[-3]
    p       = df.iloc[-2]
    c       = df.iloc[-1]
    if d_prev2['close'] <= 0:
        return False
    prev_decline = (d_prev2['close'] - p['close']) / d_prev2['close']
    if prev_decline < 0.05 or prev_decline > 0.15:
        return False
    decline_amt = d_prev2['close'] - p['close']
    recovery    = c['close'] - p['close']
    if decline_amt <= 0 or recovery / decline_amt < 0.50:
        return False
    if c['close'] <= c['open']:
        return False
    if p['volume'] <= 0 or c['volume'] >= p['volume']:
        return False
    ma25  = c['ma25']
    ma75  = c['ma75']
    close = c['close']
    near_ma25 = not pd.isna(ma25) and ma25 > 0 and abs(close - ma25) / ma25 <= 0.05
    near_ma75 = not pd.isna(ma75) and ma75 > 0 and abs(close - ma75) / ma75 <= 0.05
    return bool(near_ma25 or near_ma75)


def chk_inv_triple_bottom(df: pd.DataFrame) -> bool:
    """F-05+F-08: 逆三山（上昇も3安値+各安値で出来高縮小+ネックライン突破）"""
    if len(df) < 60:
        return False
    prices  = df['close'].iloc[-60:]
    volumes = df['volume'].iloc[-60:]
    lows: list[tuple[int, float, float]] = []
    for i in range(4, len(prices) - 4):
        if prices.iloc[i] == prices.iloc[i - 4:i + 5].min():
            lows.append((i, float(prices.iloc[i]), float(volumes.iloc[i])))
    if len(lows) < 3:
        return False
    (i1, v1, vol1), (i2, v2, vol2), (i3, v3, vol3) = lows[-3], lows[-2], lows[-1]
    if not (v1 < v2 < v3):
        return False
    if not (vol1 > vol2 > vol3):
        return False
    neckline = float(prices.iloc[i1:i3 + 1].max())
    c = df.iloc[-1]
    vol_ma = c['vol_ma25']
    if pd.isna(vol_ma) or vol_ma <= 0:
        return False
    return bool(c['close'] > neckline and c['volume'] >= vol_ma * 1.5)


def chk_saucer_bottom(df: pd.DataFrame) -> bool:
    """F-06: 円形底（放物線フィット法：90日U字+出来高増加+高値付近回復）"""
    if len(df) < 90:
        return False
    prices  = df['close'].iloc[-90:].values.astype(float)
    volumes = df['volume'].iloc[-90:].values.astype(float)
    t = np.arange(len(prices), dtype=float)
    try:
        coeffs = np.polyfit(t, prices, 2)
        a = coeffs[0]
    except Exception:
        return False
    if a <= 0:
        return False
    vertex_t = -coeffs[1] / (2 * a)
    if vertex_t < 13 or vertex_t > 76:
        return False
    vol_first  = float(np.mean(volumes[:45]))
    vol_second = float(np.mean(volumes[45:]))
    if vol_first <= 0 or vol_second <= vol_first:
        return False
    high_90d = float(np.max(prices))
    return bool(high_90d > 0 and prices[-1] >= high_90d * 0.90)


# ── G群 ──────────────────────────────────────────────────────

def chk_ascending_triangle(df: pd.DataFrame) -> bool:
    """G-03: 上昇三角形（N=60日、水平上値+切り上がる下値+ブレイク）"""
    if len(df) < 65:
        return False
    segment = df.iloc[-61:-1]
    c = df.iloc[-1]

    highs = []
    for i in range(2, len(segment) - 2):
        h = float(segment['high'].iloc[i])
        if h == segment['high'].iloc[i - 2:i + 3].max():
            highs.append(h)
    if len(highs) < 3:
        return False

    high_arr = np.array(highs)
    mean_h   = high_arr.mean()
    if mean_h <= 0 or high_arr.std() / mean_h > 0.02:
        return False
    resistance = mean_h

    lows_idx: list[int] = []
    lows_val: list[float] = []
    for i in range(2, len(segment) - 2):
        l = float(segment['low'].iloc[i])
        if l == segment['low'].iloc[i - 2:i + 3].min():
            lows_idx.append(i)
            lows_val.append(l)
    if len(lows_idx) < 3:
        return False

    slope = float(np.polyfit(lows_idx, lows_val, 1)[0])
    if slope <= 0:
        return False

    if c['close'] <= resistance:
        return False

    vol_ma = c['vol_ma25']
    if pd.isna(vol_ma) or vol_ma <= 0:
        return False
    return bool(c['volume'] >= vol_ma * 1.5)


def chk_alltime_high(df: pd.DataFrame) -> bool:
    """G-05: 上場来高値更新（株式分割調整済みATH）"""
    ath = df.attrs.get('ath', None)
    if ath is None or ath <= 0:
        return False
    c = df.iloc[-1]
    return bool(c['close'] >= ath * 0.9995)


def chk_base_breakout(df: pd.DataFrame) -> bool:
    """G-07: ベース内ブレイク（20日値幅<10%+直近5-10日高値突破+出来高）"""
    if len(df) < 25:
        return False
    c = df.iloc[-1]
    ma25 = c['ma25']
    if pd.isna(ma25) or ma25 <= 0 or c['close'] < ma25:
        return False
    base     = df.iloc[-21:-1]
    base_avg = base['close'].mean()
    base_high = base['close'].max()
    if base_avg <= 0 or (base['close'].max() - base['close'].min()) / base_avg >= 0.10:
        return False
    prior_high = df['high'].iloc[-11:-1].max()
    if c['close'] <= prior_high:
        return False
    vol_5d = df['volume'].iloc[-6:-1].mean()
    if vol_5d <= 0 or c['volume'] < vol_5d * 1.3:
        return False
    return bool(c['close'] <= base_high * 1.05)


# ── I群 ──────────────────────────────────────────────────────

def chk_williams_r(df: pd.DataFrame) -> bool:
    """I-10: ウィリアムズ%R（過去14日≤-80→現在≥-50+MA25上向き）"""
    if len(df) < 20:
        return False
    c   = df.iloc[-1]
    wpr = c['williams_r']
    if pd.isna(wpr) or wpr < -50:
        return False
    past_wpr = df['williams_r'].iloc[-15:-1].dropna()
    if len(past_wpr) == 0 or not (past_wpr <= -80).any():
        return False
    ma25 = df['ma25']
    if pd.isna(ma25.iloc[-1]) or pd.isna(ma25.iloc[-6]):
        return False
    return bool(ma25.iloc[-1] > ma25.iloc[-6])


# ── K群 ──────────────────────────────────────────────────────

def chk_canslim(df: pd.DataFrame) -> bool:
    """K-04: CAN-SLIM複合（PO+52週新高値+出来高MA25×1.5、3条件同時）"""
    if len(df) < 100:
        return False
    c = df.iloc[-1]
    if any(pd.isna([c['ma5'], c['ma25'], c['ma75']])):
        return False
    if not (c['close'] > c['ma5'] > c['ma25'] > c['ma75']):
        return False
    if df['ma5'].iloc[-1] <= df['ma5'].iloc[-5]:
        return False
    if df['ma25'].iloc[-1] <= df['ma25'].iloc[-5]:
        return False
    lookback = df['close'].iloc[-252:-1] if len(df) >= 253 else df['close'].iloc[:-1]
    if c['close'] < lookback.max():
        return False
    vol_ma = c['vol_ma25']
    if pd.isna(vol_ma) or vol_ma <= 0:
        return False
    return bool(c['volume'] >= vol_ma * 1.5)


def chk_neckline_vol(df: pd.DataFrame) -> bool:
    """K-07: ネックライン突破+出来高急増（直近20日高値を出来高1.5倍で上抜け）"""
    if len(df) < 25:
        return False
    c = df.iloc[-1]
    neckline = df['high'].iloc[-21:-1].max()
    if neckline <= 0 or c['close'] <= neckline:
        return False
    vol_ma = c['vol_ma25']
    if pd.isna(vol_ma) or vol_ma <= 0:
        return False
    return bool(c['volume'] >= vol_ma * 1.5)


def chk_weekly_po_first(df: pd.DataFrame) -> bool:
    """K-09: 週足PO初達成（金曜のみ、長期下落後の初転換）"""
    today = df.index[-1]
    if today.weekday() != 4:
        return False

    wdf_raw = df.attrs.get('weekly_df', None)
    if wdf_raw is None or len(wdf_raw) < 55:
        return False

    wdf = wdf_raw.copy()
    wdf['wma5']  = wdf['close'].rolling(5).mean()
    wdf['wma13'] = wdf['close'].rolling(13).mean()
    wdf['wma26'] = wdf['close'].rolling(26).mean()

    def _is_po(row: pd.Series) -> bool:
        cols = ['wma5', 'wma13', 'wma26']
        if any(pd.isna(row.get(col, np.nan)) for col in cols):
            return False
        return bool(row['close'] > row['wma5'] > row['wma13'] > row['wma26'])

    def _mas_up(idx: int) -> bool:
        if idx < 4:
            return False
        for col in ['wma5', 'wma13', 'wma26']:
            v_now  = wdf[col].iloc[idx]
            v_prev = wdf[col].iloc[idx - 4]
            if pd.isna(v_now) or pd.isna(v_prev) or v_now <= v_prev:
                return False
        return True

    n = len(wdf)
    if not _is_po(wdf.iloc[-1]) or not _mas_up(n - 1):
        return False
    if _is_po(wdf.iloc[-2]):
        return False

    for i in range(n - 27, n - 1):
        if i < 0:
            continue
        if _is_po(wdf.iloc[i]):
            return False

    inverse_found = False
    for i in range(n - 54, n - 27):
        if i < 0:
            continue
        row = wdf.iloc[i]
        v5  = row.get('wma5', np.nan)
        v13 = row.get('wma13', np.nan)
        v26 = row.get('wma26', np.nan)
        if any(pd.isna([v5, v13, v26])):
            continue
        if v5 < v13 < v26:
            inverse_found = True
            break

    return inverse_found


# ── 追加手法 ───────────────────────────────────────────────────

def chk_narabiaka(df: pd.DataFrame) -> bool:
    """上放れ並び赤（窓開け陽線+翸日同位置・同サイズの陽線）"""
    if len(df) < 3:
        return False
    d0 = df.iloc[-3]
    d1 = df.iloc[-2]
    d2 = df.iloc[-1]

    if d1['open'] <= d0['high']:
        return False
    if d1['close'] <= d1['open']:
        return False
    d1_body = d1['close'] - d1['open']
    if d1_body <= 0 or d1['open'] <= 0:
        return False

    if abs(d2['open'] - d1['open']) / d1['open'] > 0.03:
        return False
    if d2['close'] <= d2['open']:
        return False
    d2_body = d2['close'] - d2['open']
    if d2_body <= 0:
        return False
    body_ratio = d2_body / d1_body
    if body_ratio < 0.5 or body_ratio > 1.5:
        return False
    return bool(d2['open'] > d0['high'])


def chk_ppp(df: pd.DataFrame) -> bool:
    """パンパカパン(PPP)初達成（前日非PO→当日PO初転換）"""
    if len(df) < 80:
        return False
    c = df.iloc[-1]
    p = df.iloc[-2]

    if any(pd.isna([c['ma5'], c['ma25'], c['ma75']])):
        return False
    today_po = c['close'] > c['ma5'] > c['ma25'] > c['ma75']
    if not today_po:
        return False
    if df['ma5'].iloc[-1]  <= df['ma5'].iloc[-5]:
        return False
    if df['ma25'].iloc[-1] <= df['ma25'].iloc[-5]:
        return False
    if df['ma75'].iloc[-1] <= df['ma75'].iloc[-5]:
        return False

    if any(pd.isna([p['ma5'], p['ma25'], p['ma75']])):
        return False
    prev_po = p['close'] > p['ma5'] > p['ma25'] > p['ma75']
    return not prev_po


# ─── 手法定義テーブル ───────────────────────────────────────────────

CHECKS: list[tuple[str, str, str, bool]] = [
    # (key, label, func, is_standalone)

    # ── 既存手法（継続採用） ──────────────────────────────
    ('bullish_engulfing',    '陽の包み足',                     'chk_bullish_engulfing',    True),
    ('hammer',               '下ひげ陽線（ハンマー）',          'chk_hammer',               True),
    ('morning_star',         '朝の明星',                       'chk_morning_star',          True),
    ('three_white_soldiers', '陽の三兵',                       'chk_three_white_soldiers',  True),
    ('gap_up',               '窓開け陽線',                     'chk_gap_up',                True),
    ('perfect_order',        'パーフェクトオーダー',            'chk_perfect_order',         False),
    ('gc_25_75',             'GC 25/75日線',                   'chk_gc_25_75',              False),
    ('ma25_debut',           '25日線デビュー買い',              'chk_ma25_debut',            False),
    ('ma75_recovery',        '75日線回復',                     'chk_ma75_recovery',         False),
    ('ma_squeeze_breakout',  'MA収晵後ブレイク',                'chk_ma_squeeze_breakout',   True),
    ('price_above_all_ma',   '株価が全MA上',                    'chk_price_above_all_ma',   False),
    ('vol_surge_150',        '出来高急増（前日比150%超）',      'chk_vol_surge_150',         False),
    ('new_high_vol',         '新高値＋出来高急増',              'chk_new_high_vol',          True),
    ('vol_dry_surge',        '出来高枯れ→急増',                 'chk_vol_dry_surge',         False),
    ('vcp',                  'VCP',                            'chk_vcp',                   True),
    ('cup_with_handle',      'カップウィズハンドル',            'chk_cup_with_handle',       True),
    ('tight_area',           'タイト保ち合い',                  'chk_tight_area',            True),
    ('double_bottom',        'ダブルボトム（W底）',             'chk_double_bottom',         True),
    ('flag',                 'フラッグ・ペナント',              'chk_flag',                  True),
    ('inv_head_shoulders',   '逆ヘッド&ショルダー',             'chk_inv_head_shoulders',    True),
    ('52week_high',          '52週新高値',                      'chk_52week_high',           False),
    ('high_level_tight',     '高値圈コンソリデーション',        'chk_high_level_tight',      True),

    # ── A群（単体）────────────────────────────────────────────
    ('large_bullish_5pct',   '大陽田5%超（A-05）',              'chk_large_bullish_5pct',   True),
    ('uwabane_large',        '上放れ陽線（A-07）',               'chk_uwabane_large',         True),

    # ── B群（単体）────────────────────────────────────────────
    ('sankasen_akebono',     '三川明けの明星（B-02）',           'chk_sankasen_akebono',      True),
    ('island_reversal',      '離れ小峳（B-06）',                 'chk_island_reversal',       True),
    ('triple_bottom',        '三点底（B-08）',                   'chk_triple_bottom',         True),
    ('engulfing_vol',        '包み足＋出来高（B-10）',           'chk_engulfing_vol',         True),
    ('gap_vol',              '窓開け＋出来高（B-11）',           'chk_gap_vol',               True),

    # ── C群（C-10のみ単体、他は補助）──────────────────────
    ('ma25_touch_rebound',   '25日線タッチ反発（C-08）',         'chk_ma25_touch_rebound',    False),
    ('weinstein_stage2',     'ワインスタインS2（C-10）',         'chk_weinstein_stage2',      True),

    # ── D群（D-07のみ単体、他は補助）──────────────────────
    ('vol_surge_200',        '出来高200%急増（D-02）',           'chk_vol_surge_200',         False),
    ('obv_new_high',         'OBV新高値（D-06）',                'chk_obv_new_high',          False),
    ('pocket_pivot',         'ポケットピボット（D-07）',         'chk_pocket_pivot',          True),
    ('vol_acceleration',     '出来高加速（D-09）',               'chk_vol_acceleration',      False),

    # ── E群（単体）────────────────────────────────────────────
    ('super_tight',          'スーパータイト（E-03）',           'chk_super_tight',           True),
    ('high_tight_flag',      'ハイタイトフラッグ（E-06）',       'chk_high_tight_flag',       True),

    # ── F群（単体）────────────────────────────────────────────
    ('v_recovery',           'V字回復（F-03）',                  'chk_v_recovery',            True),
    ('inv_triple_bottom',    '逆三山（F-05+F-08）',              'chk_inv_triple_bottom',     True),
    ('saucer_bottom',        '円形底（F-06）',                   'chk_saucer_bottom',         True),

    # ── G群（単体）────────────────────────────────────────────
    ('ascending_triangle',   '上昇三角形（G-03）',               'chk_ascending_triangle',    True),
    ('alltime_high',         '上場来高値更新（G-05）',           'chk_alltime_high',          True),
    ('base_breakout',        'ベース内ブレイク（G-07）',         'chk_base_breakout',         True),

    # ── I群（単体）────────────────────────────────────────────
    ('williams_r',           'ウィリアムズ%R（I-10）',          'chk_williams_r',            True),

    # ── K群（K-04・K-09は単体、K-07は補助）──────────────────
    ('canslim',              'CAN-SLIM複合（K-04）',             'chk_canslim',               True),
    ('neckline_vol',         'ネックライン突破＋出来高（K-07）', 'chk_neckline_vol',          False),
    ('weekly_po_first',      '週足PO初達成（K-09）',             'chk_weekly_po_first',       True),

    # ── 追加手法（単体）──────────────────────────────────────
    ('narabiaka',            '上放れ並び赤',                     'chk_narabiaka',             True),
    ('ppp',                  'パンパカパン（PPP）',               'chk_ppp',                   True),
]

_FUNC_MAP = {key: globals()[fn] for key, _, fn, _ in CHECKS}


# ─── 単一銀柄分析 ─────────────────────────────────────────────

def analyze_stock(code: str, name: str = '') -> dict | None:
    df = get_stock_data(code)
    if df is None or len(df) < 30:
        print(f"  ⚠ {code}: データ取得失敗またはデータ不足")
        return None

    df = calc_indicators(df)
    curr = df.iloc[-1]
    prev = df.iloc[-2]

    matches: list[dict]   = []
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
    for dt, row in df.tail(90).iterrows():
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

    return {
        'code':      code,
        'name':      name,
        'close':     round(float(curr['close']), 2),
        'change':    round(float(change), 2),
        'volume':    int(curr['volume']),
        'matches':   matches,
        'supporting': supporting,
        'chart':     chart,
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

    subject = f"【StockScan JP】テクニカル一致 {date_str}（{len(matched)}銀柄）"

    lines = [
        "StockScan JP テクニカル分析レポート",
        f"日付: {date_str}",
        f"一致銀柄数: {len(matched)} 件",
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

    for s in stocks:
        code = str(s['code'])
        name = s.get('name', '')
        print(f"→ {code} {name} を分析中...")
        result = analyze_stock(code, name)
        if result:
            results.append(result)
            if result['matches'] or result['supporting']:
                matched.append(result)
                all_labels = [m['label'] for m in result['matches'] + result['supporting']]
                print(f"  ✅ 一致: {all_labels}")
            else:
                print(f"  　 マッチなし")

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

    print(f"\n分析完了: {len(matched)}/{len(results)} 銀柄一致")

    if matched:
        send_email(matched, now.strftime('%Y/%m/%d'))


if __name__ == '__main__':
    main()
