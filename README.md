# テスト設計フレームワーク：spec.yml × テストタグ × 品質レポート

テスト計画・実装・品質確認を一本のファイル（`spec.yml`）で結びつける軽量フレームワークです。

---

## コンセプト

### 背景にある問題

テストを書いていると、こんな疑問が生まれます。

- 「どの要件をカバーしているのか」がコードを読まないとわからない
- テスト数が増えても「何が足りていないか」が見えない
- spec（仕様書）とテストコードが別々に管理されて乖離していく

このフレームワークは「仕様書 = テスト計画書」として `spec.yml` を一元管理することで、これらを解決します。

### 3 層構造

```
spec.yml（要件・テスト観点）   ← 実装前に確定（シフトレフト）
     ↕ docstring タグで紐付け
テスト実装（コード）
     ↓
テスト品質レポート（要件網羅率 × カテゴリ分布 × レベル分布）
```

**重要な設計方針：spec.yml は実装前に書く。**  
テストを書いてから spec.yml に追記するのではなく、まず spec.yml に「何をテストすべきか」を定義し、それに従ってテストを実装します。これにより、テスト設計のレビューが実装前に行えます（シフトレフト）。

---

## spec.yml の構造

```yaml
project: your-app
test_dirs: [tests/]
source_dirs: [src/]

requirements:
  REQ-001:
    title: "イベントを作成する"
    perspectives:
      TC-001-01: {category: normal,   level: unit,        desc: "タイトル・開始日時・終了日時を指定してイベントを作成できる"}
      TC-001-02: {category: error,    level: unit,        desc: "タイトルが空のとき ValidationError を raise する"}
      TC-001-03: {category: boundary, level: unit,        desc: "タイトルが最大文字数（255文字）のとき作成できる"}
      TC-001-04: {category: branch,   level: unit,        desc: "繰り返し設定（daily/weekly/monthly）が保存される"}
      TC-001-05: {category: normal,   level: integration, desc: "作成したイベントがDBに永続化されて取得できる"}
      TC-001-06: {category: normal,   level: e2e,         desc: "UI からイベントを作成するとカレンダーに表示される"}
```

### カテゴリ（category）

テストケースの種類を4つに分類します。

| キー | 説明 |
|------|------|
| `normal` | 典型的な入力・期待通りの出力 |
| `error` | 不正入力・例外・エラー処理 |
| `boundary` | 上限・下限・空・最大サイズ |
| `branch` | 条件分岐・フラグの組み合わせ |

4カテゴリのバランスを見ることで、「正常系しかない」「境界値を考えていない」といった設計の偏りを発見できます。

### テストレベル（level）

| キー | 説明 |
|------|------|
| `unit` | 関数・クラス単体の動作確認 |
| `integration` | 複数モジュール・外部依存を含む処理フローの確認 |
| `e2e` | ユーザー操作から最終出力までの一連の確認 |

省略時は `unit` として扱います。レベル分布を見ることで「単体テストだけで統合の確認が抜けている」といった盲点を可視化できます。

### REQ-E プレフィックス（E2E シナリオ）

複数の REQ にまたがる E2E シナリオは専用の REQ に分離します。

```yaml
REQ-E01:
  title: "イベントの作成から通知までの一気通貫フロー"
  perspectives:
    TC-E01-01: {category: normal, level: e2e, desc: "イベント作成・リマインダー設定・通知受信の正常フロー全体"}
    TC-E01-02: {category: error,  level: e2e, desc: "未認証状態で操作すると認証画面にリダイレクトされる"}
```

---

## テストへのタグ付け

テスト関数の docstring 先頭行に `[REQ-XXX][TC-XXX-XX]` タグを付与します。

```python
def test_create_event_with_valid_params():
    """[REQ-001][TC-001-01] タイトル・開始日時・終了日時を指定してイベントを作成できる."""
    ...

def test_create_event_empty_title():
    """[REQ-001][TC-001-02] タイトルが空のとき ValidationError を raise する."""
    ...
```

タグがないテスト関数は品質レポートで「未紐付け」として警告されます。

---

## 品質レポートの出力

### スクリプト実行

```bash
# 複雑度なし（シンプル）
python scripts/test_quality_report.py \
    --spec tests/spec.yml \
    --test-dirs tests/ \
    --output reports/quality_report.md

# radon による複雑度付き（Python プロジェクト）
radon cc src/ -s -j > reports/complexity.json
python scripts/test_quality_report.py \
    --spec tests/spec.yml \
    --test-dirs tests/ \
    --radon-json reports/complexity.json \
    --output reports/quality_report.md
```

### レポートの構成

```
## サマリー
要件数・TC数・テスト数・ERROR/WARN 件数を一覧表示

## レベル別実装状況
単体/結合/E2E ごとの TC定義数・割合・実装数

## 要件 × カテゴリ分布
REQ ごとに normal/error/boundary/branch の実装数/定義数

## 要件 × レベル分布
REQ ごとに unit/integration/e2e の TC数

## TC 詳細
TC ごとに紐付いたテスト関数名を表示（REQ ごとに折りたたみ）

## ソース複雑度（radon cc）
ファイルごとの最大複雑度（radon 連携時のみ）
```

### 判定ルール

**サマリーの ERROR/WARN は spec.yml ↔ テストタグのトレーサビリティを示します。テスト実行の pass/fail ではありません。**

| 種別 | 条件 |
|------|------|
| ERROR | spec.yml に存在しない `[REQ-XXX]` / `[TC-XXX-XX]` タグ |
| ERROR | spec.yml の REQ に対応テストが 0 件 |
| WARN | spec.yml の TC に対応テストが 0 件 |
| WARN | タグなしテスト関数 |

---

## 開発フローにおける使いどころ

このフレームワークは3つのタイミングで活用します。

### 1. 実装前（計画確認）

spec.yml を書いた段階でレポートを出力します。テストが 0 件なので全 TC が WARN になりますが、それが正常な状態です。このレポートをレビューすることで：

- カテゴリの偏り（正常系ばかりなど）を早期に発見
- レベルのバランス（unit しかないなど）を確認
- 要件の抜け漏れをチェック

### 2. 実装後（品質確認）

テスト実装が進むにつれ WARN が減っていきます。全 TC を実装し終えると WARN がゼロになります。

### 3. リファクタリング後（回帰確認）

コードやテストを変更した後でも、トレーサビリティが維持されていることを確認します。

---

## このリポジトリの内容

```
spec.yml               # カレンダーアプリを想定したサンプル spec.yml
scripts/
  test_quality_report.py   # 品質レポート生成スクリプト（Python + PyYAML 依存）
```

### 依存ライブラリ

```bash
pip install pyyaml       # 必須
pip install radon        # 複雑度計測を使う場合（Python プロジェクトのみ）
```

---

## 他言語への応用

スクリプト内の言語依存箇所は `_scan_file()` 関数のみです。Python の AST でテスト関数の docstring を読んでいますが、他言語では正規表現でコメントを読む実装に差し替えるだけで動きます。

spec.yml のスキーマ、タグ規約、レポート構造は言語を問わず共通です。
