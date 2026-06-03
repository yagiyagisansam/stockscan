# SESSION_HANDOFF — stockscan (2026-06-03)

## リポジトリ
- **URL**: https://github.com/yagiyagisansam/stockscan
- **作業ブランチ**: `claude/previous-session-handoff-78FrI`（PR済みでmainにマージ済み）
- **GitHub Pages**: index.html がそのままフロントエンド（単一ファイルアプリ）

## 現在の状態まとめ

### mainブランチの最新コミット（2026-06-03）
```
46a678e chore: update stock analysis results 2026-06-03 10:42 JST  ← GitHub Actions 自動実行
242a94d chore: ignore Python __pycache__ directories (#8)
25f1974 fix: 全49手法の判定精度向上・PPP分割・日本語名取得改善  ← PR#7
0da4b8a fix: 未分析銘柄表示・スピナー修正・名前補完・APIタイムアウト追加
abb3d1f fix: 上場来高値条件修正・PocketPivot厳格化・PPP押し目追加・銘柄名補完
```

### data/stocks.json — 18銘柄（うち名前あり）
| コード | 現在の名前 | 状態 |
|--------|----------|------|
| 7974 | 任天堂 | ✅ 日本語 |
| 4063 | 信越化学工業 | ✅ 日本語 |
| 8306 | 三菱UFJフィナンシャルG | ✅ 日本語 |
| 8766 | Tokio Marine Holdings, Inc. | ❌ 英語 |
| 3984 | User Local, Inc. | ❌ 英語 |
| 4042 | Tosoh Corporation | ❌ 英語 |
| 1828 | Tanabe Engineering Corporation | ❌ 英語 |
| 7013 | IHI Corporation | ❌ 英語 |
| 5401 | Nippon Steel Corporation | ❌ 英語 |
| 1879 | Shinnihon Corporation | ❌ 英語 |
| 3993 | PKSHA Technology Inc. | ❌ 英語 |
| 4390 | IPS, Inc. | ❌ 英語 |
| 3817 | SRA Holdings, Inc. | ❌ 英語 |
| 4204 | Sekisui Chemical Co., Ltd. | ❌ 英語 |
| 8252 | Marui Group Co., Ltd. | ❌ 英語 |
| 5290 | Vertex Corporation | ❌ 英語 |
| 6625 | JALCO Holdings Inc. | ❌ 英語 |
| 6255 | （空） | ❓ results.jsonに存在しない |

### data/results.json — 直近分析結果（2026/06/03）
- **17銘柄分析済み**（6255が欠落）
- **全17銘柄がマッチなし**

---

## 未解決の問題

### 🔴 優先度：高

#### 1. 会社名が英語になる問題
**原因**: `scripts/analyze.py` の `get_stock_info()` が yfinance の `longName`/`shortName` を使っており、これは英語名を返す。workflow が実行されるたびに stocks.json の名前が英語で上書きされる。

フロントエンドの `backfillMissingNames()` は「名前が空のもののみ補完」するため、英語名が入っていると日本語への変換が走らない。

**推奨修正 — analyze.py の名前取得を日本語APIに変更**:
```python
import requests

def get_japanese_name(code):
    """Yahoo Finance search APIで日本語の会社名を取得"""
    ticker = f"{code}.T"
    try:
        url = f"https://query1.finance.yahoo.com/v1/finance/search?q={ticker}&lang=ja&region=JP&quotesCount=5"
        headers = {'User-Agent': 'Mozilla/5.0'}
        res = requests.get(url, headers=headers, timeout=10)
        data = res.json()
        for q in data.get('quotes', []):
            if q.get('symbol') == ticker:
                return q.get('longname') or q.get('shortname') or ''
    except Exception:
        pass
    return ''
```

`main()` 内での呼び出しを `get_stock_info()` → `get_japanese_name()` に変更する。

または、**analyze.py での名前書き込みを完全に削除**し、フロントエンドの `backfillMissingNames()` が常に実行されるよう（空チェックをラテン文字チェックに変更）にする。

#### 2. 全銘柄がマッチなしの問題
**状況**: 2026-06-03の分析で全17銘柄がマッチなし（パターン検出ゼロ）

前セッションで多数の条件を強化した結果、全条件が厳しすぎる可能性がある。少なくとも `golden_cross`、`vol_surge_150`、`ma25_cross_up` は頻繁に発生するはずなので、これらもゼロなのは要調査。

**デバッグ方法**:
```bash
cd /home/user/stockscan
pip install yfinance pandas numpy requests
# analyze.py に print デバッグを追加して1銘柄だけ実行
python3 scripts/analyze.py 2>&1 | head -100
```

#### 3. 6255が分析されていない問題
**状況**: stocks.json には 6255 があるが results.json に存在しない（17銘柄のみ）
```python
import yfinance as yf
df = yf.download('6255.T', period='6mo', progress=False)
print(df.shape, df.tail())
```
で取得可否を確認する。上場廃止・データなしの場合は stocks.json から削除を検討。

---

## アーキテクチャ概要

### ファイル構成
```
stockscan/
├── index.html          # フロントエンド全体（~2224行）単一ファイルアプリ
├── data/
│   ├── stocks.json     # 監視銘柄リスト [{code, name}, ...]
│   └── results.json    # 分析結果 {date, timestamp, total, matched, stocks:[...]}
├── scripts/
│   └── analyze.py      # Python分析スクリプト（~1486行）
├── .gitignore          # __pycache__/ 等を除外
└── .github/workflows/
    └── stock-analysis.yml  # 毎日18:00 JST + 手動実行
```

### results.json のスキーマ
```json
{
  "date": "2026/06/03",
  "timestamp": "...",
  "total": 17,
  "matched": 0,
  "stocks": [
    {
      "code": "7974",
      "name": "任天堂",
      "methods": {
        "alltime_high": false,
        "golden_cross": false,
        ...
      }
    }
  ]
}
```

### フロントエンド (index.html) の主要機能
- **銘柄追加/削除**: `addStock()`, `removeStock()` → `syncStocksOnly()` でGitHubに即時反映（分析は実行しない）
- **日本語名取得**: `fetchStockName(code)` → Yahoo Finance Search API (`lang=ja&region=JP`) → fallback: chart endpoint
- **名前補完**: `backfillMissingNames()` → 名前が空の銘柄のみ自動補完（ページ読み込み時）
  - **注意**: 英語名が入っていると補完が走らない（空チェックのみ）
- **チャート**: TradingView Lightweight Charts v4.1.3
  - 日足デフォルト: 直近65本 `setVisibleLogicalRange({from: data.length-65, to: data.length+2})`
  - 週足デフォルト: 直近52本（1年分）
- **パターンオーバーレイ**: バッジタップ → `computePatternOverlay(stock, key)` → マーカー + 水平線表示

### 分析パターン一覧（50手法）
analyze.py の `CHECKS` テーブル:
```
alltime_high, pocket_pivot, golden_cross, death_cross, vol_surge_150,
ma25_cross_up, ma75_cross_up, macd_cross, rsi_oversold, bollinger_break_up,
bollinger_break_down, consecutive_rise3, three_soldiers, three_crows,
doji_star, hammer, shooting_star, morning_star, evening_star, saucer_bottom,
double_bottom, triple_bottom, inv_head_shoulders, vcp, ppp, ppp_oshine,
flag, pennant, ascending_triangle, descending_triangle, symmetrical_triangle,
cup_with_handle, inv_cup_with_handle, rectangle, channel_up, channel_down,
gap_up, gap_down, ma5_cross_ma25, ma25_cross_ma75, pullback_to_ma25,
pullback_to_ma75, ma75_recovery, vol_dry_up, high_tight_flag,
inv_triple_bottom, double_top, head_shoulders, wedge_up, wedge_down
```

### GitHub Actions ワークフロー
- **スケジュール**: 毎日 09:00 UTC (= 18:00 JST) 平日のみ
- **手動実行**: workflow_dispatch（Actions タブから「Run workflow」）
- **処理**: analyze.py 実行 → data/results.json + data/stocks.json を commit & push
- **git push**: `git pull --rebase && git push`（コンフリクト対策済み）
- **Secrets**: SMTP_USER, SMTP_PASS, TO_EMAIL（メール通知用）

---

## 次セッションでやること

### 最優先
1. **会社名を日本語で取得** — `scripts/analyze.py` の `get_stock_info()` を日本語API対応に変更（上記コード参照）
2. **全マッチなしの原因調査** — analyze.py をデバッグ実行して条件が厳しすぎないか確認
3. **6255の欠落原因調査** — yfinance で 6255.T のデータ取得可否を確認

### その後
4. 修正後に GitHub Actions を手動実行して結果を確認
5. 判定精度が適切かユーザーに確認してもらう

---

## Gitの操作メモ
```bash
# ローカルリポジトリのパス
cd /home/user/stockscan

# リモートURL（プロキシ経由 — ポート番号はセッションごとに変わる）
git remote -v

# 新しい作業ブランチを作成してプッシュ
git checkout -b claude/new-branch-name
git push -u origin claude/new-branch-name

# mainの最新を取得してからブランチ作成
git fetch origin main
git checkout -b claude/new-branch main
```

## GitHub MCPツール利用メモ
- `ToolSearch` で `select:mcp__github__create_pull_request,...` して schema をロードしてから使う
- PRの作成: `mcp__github__create_pull_request`
- PRのマージ: `mcp__github__merge_pull_request`
- ファイル内容取得: `mcp__github__get_file_contents`
- **大きいファイル（index.html ~81KB）は MCP push_files が使えない** → ローカル git コマンドで push すること
