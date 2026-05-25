# PRD — Coding Agent for Harness CI/CD

---

## 概要
PR が開かれると Harness webhook 経由でこのエージェントが起動し、コード差分を AI でレビューし、問題があれば自動修正コミットを作成して Harness パイプラインを再トリガーする。

## 目的
- PR レビューの自動化により、マージ前のバグ・セキュリティ問題を自動検出する
- 修正可能な問題は自動コミットで解消し、人間のレビュー負担を下げる
- Supervisor (Opus) による二重チェックで誤った自動修正を防ぐ

---

## アーキテクチャ決定

### モデル分工（最新）
| 役割 | モデル | 切替方法 |
|------|--------|----------|
| Reviewer（一次審査） | `ollama/llama3.1`（ローカル）/ fallback: `claude-sonnet-4-6` | `REVIEWER_MODEL` env var |
| Supervisor（監督） | `claude-opus-4-5`（固定） | 変更禁止 |

### Ollama ルーティング
- `REVIEWER_MODEL=ollama/<model>` を設定すると `reviewer.py` の `_call_llm()` が Ollama の OpenAI 互換エンドポイント（`OLLAMA_BASE_URL`）に自動ルーティング
- Supervisor は常に Anthropic SDK 経由で Opus を呼ぶ。Ollama には切り替えない

---

## 機能要件
- [x] POST /webhook で Harness からの PR イベントを受信
- [x] HMAC-SHA256 で署名検証（`WEBHOOK_SECRET`）
- [x] PR diff を取得して Reviewer LLM でコードレビュー
- [x] 問題あり＆修正可能 → Reviewer が `needs_supervision: true` を返した場合のみ Supervisor (Opus) に二重チェックを依頼
- [x] Supervisor が `approve_fix` → 自動修正コミットを PR ブランチに push
- [x] Supervisor が `override_to_comment` → コメントのみ投稿、コミットなし
- [x] Supervisor が `escalate` → 人間レビュー要求コメントを投稿
- [x] Supervisor 失敗時はコメントのみにフォールバック
- [x] Harness パイプライン再トリガー（修正コミット後）
- [x] Reviewer に Ollama ローカルモデルを使用可能

---

## 非機能要件
- **型アノテーション**: 全関数シグネチャに必須
- **非同期**: I/O 処理は全て `async def`
- **ログ**: `logging` モジュール使用、diff 内容を ERROR レベルでログしない
- **HTTP 200 必須**: `/webhook` は内部エラー時も常に 200 を返す（Harness リトライ防止）
- **ステートレス**: DB 不使用、全コンテキストは webhook ペイロードから取得
- **diff 上限**: 8,000 tokens（超過時は先頭から切り詰め `[diff truncated]` を付記）

---

## 禁止事項
- `supervisor.py` の Opus モデルを Ollama に切り替えない
- `harness/secrets.yaml` を編集しない（シークレット値はリポジトリ外で管理）
- `main` ブランチへの直接 push 禁止（エージェント自身の修正も PR 経由）
- `async def` 内で同期ブロッキング I/O を使わない（`asyncio.to_thread()` を使う）
- System prompt を動的に構築しない（module-level 定数として定義）
- Opus を `needs_supervision: true` なしに無条件で呼ばない

---

## 完了条件
- [ ] `pytest tests/ -v` が全て通る
- [ ] `reviewer.py` と `patcher.py` のカバレッジ ≥ 80%
- [ ] `python scripts/review.py --pr <番号>` で approve が返る
- [ ] Ollama モデルで `REVIEWER_MODEL=ollama/llama3.1` を設定してレビューが動作する
