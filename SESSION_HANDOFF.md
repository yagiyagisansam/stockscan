# セッション引き継ぎ資料

## 次セッションでやること

### keiba-ev リポジトリの整理
**最初にやること：** 「keiba-evリポジトリをセッションに追加して、stockscan関連ファイルを整理・削除してください」と伝える。

削除確定ファイル：
- `stocks.html`
- `SESSION_HANDOFF.md`
- `テクニカル分析手法_新規...`（ファイル名先頭部分）

内容確認してから削除：
- `scripts/` → `analyze.py` があれば削除（競馬スクリプトは残す）
- `data/` → `results.json` があれば削除
- `index.html` → 株関連ならば削除

---

## 現在の状態

### stockscan（yagiyagisansam/stockscan）✅ 完了
- アプリURL: `https://yagiyagisansam.github.io/stockscan/`
- 最新機能（2026-06-02 実装済み）:
  - チャートデータ 90日 → 252日（1年）
  - 週足/日足切り替えボタン
  - 陽線=赤 / 陰線=薄水色
  - MA色：5MA緑 / 25MAライトグリーン / 75MA薄桃色
  - タップ横線（クロスヘア Normal モード）
  - パターンバッジタップでネックライン描画
- `deploy-root.yml` 削除済み（エラーメール解消）
- 毎日18:00 JST に自動分析実行中

### keiba-ev（yagiyagisansam/keiba-ev）🔲 整理中
- `stock-analysis.yml` ワークフロー削除済み（エラーメール解消）
- stockscan関連ファイルが残存しているため上記の整理が必要

---

## リポジトリ構成

| リポジトリ | 用途 | 状態 |
|-----------|------|------|
| `yagiyagisansam/stockscan` | 日本株テクニカル分析 | 稼働中 |
| `yagiyagisansam/keiba-ev` | 競馬期待値分析 | 整理中 |
