# SESSION_HANDOFF — stockscan (2026-06-09)

## リポジトリ
- **URL**: https://github.com/yagiyagisansam/stockscan
- **作業ブランチ**: `claude/stockscan-handoff-mxlsv2`（main に対して 8 コミット先行・PR 未作成）
- **GitHub Pages**: index.html がそのままフロントエンド（単一ファイルアプリ）

---

## 前セッション（2026-06-03〜2026-06-08）の完了事項

### 1. 会社名の日本語化 ✅
`scripts/analyze.py` に `get_japanese_name()` を実装。Yahoo Finance Search API（`lang=ja&region=JP`）で日本語名を取得し、fallback に chart endpoint を使用。`stocks.json` の全 17 銘柄が日本語名になった。

### 2. パターン検出条件の全面改訂 ✅
- **40 手法** に再整理（4 手法削除・複数追加）
- 週足チャートデータ（`weekly_chart` フィールド）を `results.json` に付加
- `index.html` が週足チャートを表示可能に
- `vol_ma25` ラインをチャートオーバーレイに追加

### 3. 6255 銘柄の除去 ✅
yfinance でデータ取得不可のため `stocks.json` から削除。現在 17 銘柄。

### 4. pattern_check.py / generate_report.py 更新 ✅
- `pattern_check.py`: ~120 銘柄ユニバースで 40 手法の Hit 事例を収集
- `generate_report.py`: レポートを 40 手法対応に更新

### 5. 最新分析結果（2026-06-08）
```
総数: 17 銘柄, マッチ: 6 銘柄
1879 新日本建設  → 高値圏コンソリデーション, PPP押し目
3984 ユーザーローカル → ウィリアムズ%R
1828 田辺工業   → ウィリアムズ%R
4390 アイ・ピー・エス → PPP押し目
6625 JALCOホールディングス → ウィリアムズ%R
7974 任天堂    → ウィリアムズ%R
```

---

## 未解決の問題・次セッションでやること

### 🔴 要確認

#### 1. ウィリアムズ%R の出来高条件を強化（今セッションで対応済み）
- **修正内容**: `chk_williams_r` の出来高条件を `_vol_prev(c, p, 1.0)` → `_vol_prev(c, p, 1.3)` に変更
- 前日比 1.0 倍（＝前日以上なら OK）は実質無条件だったため 1.3 倍（30% 増）に引き上げ
- 次回 GitHub Actions 実行後、マッチ数が適切か確認する

#### 2. ブランチを main にマージ
このブランチ（`claude/stockscan-handoff-mxlsv2`）は main に対して 8 コミット先行。PR を作成して main にマージする。

### 🟡 その後の改善候補

#### 3. 監視銘柄の追加・入れ替え
現在 17 銘柄。ユーザーが追加したい銘柄があれば `stocks.json` を更新する。フロントエンドから追加もできる（`addStock()` → `syncStocksOnly()` で即時 GitHub Push）。

#### 4. GitHub Actions の手動実行で動作確認
PR マージ後、Actions タブ → stock-analysis.yml → 「Run workflow」で手動実行し、analyze.py の改善が正しく動作するか確認。

#### 5. 誤検知パターンの精査
- `williams_r`: 今セッションで出来高条件強化済み。次回結果で効果を確認。
- `ppp_oshine` (PPP押し目): 条件を確認し過検知なら強化。

---

## アーキテクチャ概要

### ファイル構成
```
stockscan/
├── index.html              # フロントエンド全体（~2228行）単一ファイルアプリ
├── data/
│   ├── stocks.json         # 監視銘柄リスト [{code, name}, ...] 17銘柄
│   ├── results.json        # 分析結果 {date, timestamp, total, matched, stocks:[...]}
│   ├── pattern_check.html  # パターン検証レポート（pattern_check.py 生成）
│   └── pattern_report.html # パターン一覧レポート（generate_report.py 生成）
├── scripts/
│   ├── analyze.py          # Python分析スクリプト（~1609行・40手法）
│   ├── pattern_check.py    # ~120銘柄ユニバースでのパターン検証
│   └── generate_report.py  # 手法説明レポート生成
├── .gitignore
└── .github/workflows/
    ├── stock-analysis.yml  # 毎日18:00 JST + 手動実行
    ├── pattern-check.yml   # パターン検証ワークフロー
    └── pages.yml           # GitHub Pages デプロイ
```

### results.json のスキーマ
```json
{
  "date": "2026/06/08",
  "timestamp": "...",
  "total": 17,
  "matched": 6,
  "stocks": [
    {
      "code": "7974",
      "name": "任天堂",
      "close": 7524.0,
      "change": 3.47,
      "volume": 1234567,
      "matches": [{"key": "williams_r", "label": "ウィリアムズ%R（I-10）"}],
      "supporting": [],
      "chart": [...],
      "weekly_chart": [...]
    }
  ]
}
```

### CHECKS テーブル（40 手法）
```
bullish_engulfing, hammer, morning_star, three_white_soldiers, gap_up,
perfect_order(*), gc_25_75(*), ma25_debut(*), ma75_recovery(*), ma_squeeze_breakout,
price_above_all_ma(*), vol_surge_150(*), new_high_vol, vol_dry_surge(*),
vcp, cup_with_handle, tight_area, double_bottom, flag, high_level_tight,
large_bullish_5pct, uwabane_large, island_reversal, ma25_touch_rebound(*),
weinstein_stage2, vol_surge_200(*), obv_new_high(*), vol_acceleration(*),
super_tight, high_tight_flag, v_recovery, saucer_bottom,
ascending_triangle, alltime_high, base_breakout, williams_r,
neckline_vol(*), weekly_po_first, narabiaka, ppp_oshine
* は is_standalone=False（サポートシグナル扱い）
```

### フロントエンド (index.html) の主要機能
- **銘柄追加/削除**: `addStock()`, `removeStock()` → `syncStocksOnly()` で GitHub に即時反映
- **日本語名取得**: `fetchStockName(code)` → Yahoo Finance Search API (`lang=ja&region=JP`)
- **チャート**: TradingView Lightweight Charts v4.1.3
  - 日足デフォルト: 直近 65 本
  - 週足デフォルト: 直近 52 本（1 年分）
  - vol_ma25 ラインをチャートオーバーレイに表示
- **パターンオーバーレイ**: バッジタップ → `computePatternOverlay(stock, key)` → マーカー + 水平線

### GitHub Actions ワークフロー
- **スケジュール**: 毎日 09:00 UTC (= 18:00 JST) 平日のみ
- **手動実行**: workflow_dispatch（Actions タブから「Run workflow」）
- **処理**: analyze.py 実行 → data/results.json + data/stocks.json を commit & push
- **git push**: `git pull --rebase && git push`（コンフリクト対策済み）
- **Secrets**: SMTP_USER, SMTP_PASS, TO_EMAIL（メール通知用）

---

## Git 操作メモ
```bash
# 現在地
cd /home/user/stockscan

# リモート確認（プロキシ経由）
git remote -v

# main の最新を取得
git fetch origin main

# 新しい作業ブランチを作成してプッシュ
git checkout -b claude/new-branch-name main
git push -u origin claude/new-branch-name
```

## GitHub MCP ツール利用メモ
- `ToolSearch` で schema をロードしてから使う
- PRの作成: `mcp__github__create_pull_request`
- PRのマージ: `mcp__github__merge_pull_request`
- ファイル内容取得: `mcp__github__get_file_contents`
- **大きいファイル（index.html ~81KB）は MCP push_files が使えない** → ローカル git コマンドで push すること
