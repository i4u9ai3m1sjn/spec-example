#!/usr/bin/env python3
"""テスト品質レポート生成スクリプト.

spec.yml（要件・テスト観点）× テストソース（REQ/TC タグ）× 複雑度 を集計し、
要件網羅率マトリクスを Markdown で出力する。

【言語共通の使い方】
    python scripts/test_quality_report.py [options]

【デフォルト設定での実行例（csv2kakeibo）】
    python scripts/test_quality_report.py
    → tests/spec.yml と tests/ を読んで reports/quality_report.md を生成

【オプション付き実行例】
    python scripts/test_quality_report.py \\
        --spec tests/spec.yml \\
        --test-dirs tests/ \\
        --radon-json reports/complexity.json \\
        --output reports/quality_report.md

【他言語プロジェクトへの適用】
    言語ごとに変わるのは _scan_file() の実装のみ。
    Python 以外では test_quality_report.py をコピーし _scan_file() をオーバーライドする。
    テストランナーの JSON 出力（pytest-json-report, jest --json 等）は将来の拡張ポイント。
"""

import argparse
import ast
import json
import logging
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

try:
    import yaml
except ImportError:
    print("PyYAML が必要です: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

logger = logging.getLogger(__name__)

# --- 定数 ---
CATEGORIES = ("normal", "error", "boundary", "branch")
CATEGORY_LABELS = {
    "normal": "正常系",
    "error": "異常値",
    "boundary": "境界値",
    "branch": "分岐",
}
LEVELS = ("unit", "integration", "e2e")
LEVEL_LABELS = {"unit": "単体", "integration": "結合", "e2e": "E2E"}
DEFAULT_LEVEL = "unit"

REQ_PATTERN = re.compile(r"\[REQ-(\d+)\]")
TC_PATTERN = re.compile(r"\[TC-([A-Z]?\d+)-(\d+)\]")  # [A-Z]? で TC-E01-01 形式にも対応

STATUS_OK = "✅"
STATUS_WARN = "⚠️"
STATUS_ERROR = "❌"

TC_DETAIL_HEADER = "| TC | カテゴリ | レベル | TC説明 | テスト関数 | テスト説明 |"
TC_DETAIL_SEP    = "|----|---------|--------|--------|-----------|----------|"


# --- データクラス ---

@dataclass
class TestTag:
    """テスト関数から抽出したタグ情報."""

    file_path: str
    class_name: str | None
    func_name: str
    req_id: str | None    # e.g. "REQ-001"
    tc_id: str | None     # e.g. "TC-001-01"
    first_line: str       # docstring の先頭行

    @property
    def full_name(self) -> str:
        base = Path(self.file_path).name
        if self.class_name:
            return f"{base}::{self.class_name}::{self.func_name}"
        return f"{base}::{self.func_name}"


@dataclass
class ValidationIssue:
    level: str    # "ERROR" or "WARN"
    message: str


# --- spec.yml ロード ---

def load_spec(spec_path: Path) -> dict:
    """spec.yml から requirements 辞書を返す."""
    with open(spec_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"spec.yml の形式が不正です（dict を期待）: {spec_path}")
    return data.get("requirements") or {}


# --- TC マップ構築（共通ヘルパー） ---

def _build_tc_maps(spec: dict) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """spec から (tc_to_req, tc_to_category, tc_to_level) を構築する."""
    tc_to_req: dict[str, str] = {}
    tc_to_category: dict[str, str] = {}
    tc_to_level: dict[str, str] = {}
    for req_id, req_data in spec.items():
        for tc_id, tc_data in (req_data.get("perspectives") or {}).items():
            tc_to_req[tc_id] = req_id
            tc_to_category[tc_id] = tc_data.get("category", "")
            tc_to_level[tc_id] = tc_data.get("level", DEFAULT_LEVEL)
    return tc_to_req, tc_to_category, tc_to_level


# --- テストタグ抽出（Python ast 実装） ---

class _TagExtractor(ast.NodeVisitor):
    """Python AST から test_ 関数の docstring タグを抽出する."""

    def __init__(self, file_path: Path) -> None:
        self._file_path = file_path
        self._class_stack: list[str] = []
        self.tags: list[TestTag] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._class_stack.append(node.name)
        self.generic_visit(node)
        self._class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node.name.startswith("test_"):
            docstring = ast.get_docstring(node) or ""
            first_line = docstring.split("\n")[0].strip()

            req_m = REQ_PATTERN.search(first_line)
            tc_m = TC_PATTERN.search(first_line)

            if tc_m:
                # ゼロパディングで正規化: [TC-1-1] → TC-001-01, [TC-E1-1] → TC-E01-01
                first_part = tc_m.group(1)
                if first_part[0].isalpha():
                    normalized_first = first_part[0] + first_part[1:].zfill(2)
                else:
                    normalized_first = first_part.zfill(3)
                tc_id = f"TC-{normalized_first}-{tc_m.group(2).zfill(2)}"
            else:
                tc_id = None

            self.tags.append(TestTag(
                file_path=str(self._file_path),
                class_name=self._class_stack[-1] if self._class_stack else None,
                func_name=node.name,
                req_id=f"REQ-{req_m.group(1).zfill(3)}" if req_m else None,
                tc_id=tc_id,
                first_line=first_line,
            ))

    # async def test_xxx も同様に処理する
    visit_AsyncFunctionDef = visit_FunctionDef


def _scan_file(path: Path) -> list[TestTag]:
    """1つのテストファイルからタグを抽出する（Python 専用実装）."""
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (SyntaxError, UnicodeDecodeError) as e:
        logger.warning("スキップ（パースエラー）: %s: %s", path, e)
        return []
    extractor = _TagExtractor(path)
    extractor.visit(tree)
    return extractor.tags


def scan_test_tags(test_dirs: list[Path]) -> list[TestTag]:
    """テストディレクトリの test_*.py から REQ/TC タグを収集する."""
    tags: list[TestTag] = []
    for d in test_dirs:
        for path in sorted(d.rglob("test_*.py")):
            tags.extend(_scan_file(path))
    return tags


# --- 複雑度ロード（radon cc -j） ---

def load_radon(radon_json: Path | None) -> dict[str, int]:
    """radon cc -j 出力から {ファイルパス: 最大複雑度} を返す."""
    if not radon_json or not radon_json.exists():
        return {}
    with open(radon_json, encoding="utf-8") as f:
        data = json.load(f)
    result: dict[str, int] = {}
    for file_path, functions in data.items():
        if functions:
            result[file_path] = max(fn.get("complexity", 0) for fn in functions)
    return result


# --- バリデーション ---

def validate(spec: dict, tags: list[TestTag]) -> list[ValidationIssue]:
    """spec.yml ↔ テストタグの双方向バリデーション."""
    issues: list[ValidationIssue] = []

    all_req_ids = set(spec.keys())
    tc_to_req, _, tc_to_level = _build_tc_maps(spec)

    # TC の level フィールドが有効値か確認
    for tc_id, req_id in tc_to_req.items():
        lv = tc_to_level.get(tc_id, DEFAULT_LEVEL)
        if lv not in LEVELS:
            issues.append(ValidationIssue("ERROR",
                f"無効な level 値 `{lv}` in `[{req_id}][{tc_id}]`"))

    # テストタグが spec.yml に存在するか
    for tag in tags:
        if tag.req_id and tag.req_id not in all_req_ids:
            issues.append(ValidationIssue("ERROR",
                f"存在しない REQ タグ `[{tag.req_id}]`: {tag.full_name}"))
        if tag.tc_id and tag.tc_id not in tc_to_req:
            issues.append(ValidationIssue("ERROR",
                f"存在しない TC タグ `[{tag.tc_id}]`: {tag.full_name}"))
        if tag.req_id and tag.tc_id:
            expected = tc_to_req.get(tag.tc_id)
            if expected and expected != tag.req_id:
                issues.append(ValidationIssue("ERROR",
                    f"REQ と TC の対応が不一致 `[{tag.req_id}][{tag.tc_id}]`"
                    f"（spec では {expected}）: {tag.full_name}"))

    # REQ に対応テストが 0 件
    tagged_req_ids = {t.req_id for t in tags if t.req_id}
    for req_id in all_req_ids:
        if req_id not in tagged_req_ids:
            issues.append(ValidationIssue("ERROR", f"対応テストが 0 件: `{req_id}`"))

    # TC に対応テストが 0 件
    tagged_tc_ids = {t.tc_id for t in tags if t.tc_id}
    for tc_id, req_id in tc_to_req.items():
        if tc_id not in tagged_tc_ids:
            issues.append(ValidationIssue("WARN",
                f"対応テストが 0 件: `[{req_id}][{tc_id}]`"))

    # タグなしテスト
    untagged_count = sum(1 for t in tags if not t.req_id)
    if untagged_count:
        issues.append(ValidationIssue("WARN", f"タグなしテスト: {untagged_count} 件"))

    return issues


# --- レポート生成 ---

def generate_report(
    spec: dict,
    tags: list[TestTag],
    issues: list[ValidationIssue],
    radon_data: dict[str, int],
    output: Path,
) -> None:
    """マトリクス + バリデーション結果を Markdown で出力する."""
    lines: list[str] = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ヘッダー
    lines.extend([
        "# テスト品質レポート",
        "",
        f"生成日時: {now}",
        "",
        "> このレポートの判定・ERROR/WARN は spec.yml ↔ テストタグの**トレーサビリティ**を示します。テスト実行の pass/fail ではありません。",
    ])

    # サマリー
    total_req = len(spec)
    total_tc = sum(len(r.get("perspectives") or {}) for r in spec.values())
    tagged = [t for t in tags if t.req_id]
    untagged_count = len(tags) - len(tagged)
    errors = [i for i in issues if i.level == "ERROR"]
    warns = [i for i in issues if i.level == "WARN"]

    tag_status    = STATUS_OK if len(tagged) == len(tags) else STATUS_ERROR
    untag_status  = STATUS_OK if untagged_count == 0      else STATUS_ERROR
    err_status    = STATUS_OK if not errors               else STATUS_ERROR
    warn_status   = STATUS_OK if not warns                else STATUS_WARN

    lines.extend([
        "",
        "## サマリー",
        "",
        "| 指標 | 値 | 判定 |",
        "|------|---|------|",
        f"| 要件数 (REQ) | {total_req} | - |",
        f"| テスト観点数 (TC) | {total_tc} | - |",
        f"| 総テスト数 | {len(tags)} | - |",
        f"| タグ付きテスト数 | {len(tagged)} | {tag_status} |",
        f"| 未紐付けテスト数 | {untagged_count} | {untag_status} |",
        f"| ERROR | {len(errors)} | {err_status} |",
        f"| WARN | {len(warns)} | {warn_status} |",
    ])

    # バリデーション詳細（折りたたみ）
    lines.extend([
        "",
        "<details>",
        "<summary>バリデーション詳細（ERROR/WARN の内訳）</summary>",
        "",
        "> spec.yml とテストタグの双方向整合性チェック。",
        "> ERROR があるとスクリプトが終了コード 1 で失敗します。",
        ">",
        "> | レベル | 条件 |",
        "> |--------|------|",
        "> | ERROR | spec.yml に存在しない `[REQ-XXX]` / `[TC-XXX-XX]` タグ、または対応テストが 0 件の REQ |",
        "> | WARN | 対応テストが 0 件の TC、タグなしテスト関数 |",
        "",
    ])
    if not issues:
        lines.append("問題なし ✅")
    else:
        if errors:
            lines.extend(["**ERROR**", ""])
            for issue in errors:
                lines.append(f"- `[ERROR]` {issue.message}")
        if warns:
            lines.extend(["", "**WARN**", ""])
            for issue in warns:
                lines.append(f"- `[WARN]` {issue.message}")
    lines.append("")
    lines.append("</details>")

    # レベル別実装状況
    tc_to_req, tc_to_category, tc_to_level = _build_tc_maps(spec)

    spec_by_level: dict[str, int] = defaultdict(int)
    for req_data in spec.values():
        for tc_data in (req_data.get("perspectives") or {}).values():
            spec_by_level[tc_data.get("level", DEFAULT_LEVEL)] += 1

    impl_by_level: dict[str, int] = defaultdict(int)
    for tag in tags:
        if tag.tc_id:
            impl_by_level[tc_to_level.get(tag.tc_id, DEFAULT_LEVEL)] += 1

    total_spec = sum(spec_by_level.values())
    total_impl = sum(impl_by_level.values())

    lines.extend([
        "",
        "## レベル別実装状況",
        "",
        "| レベル | TC定義数 | 割合 | 実装数 | 判定 |",
        "|--------|---------|------|-------|------|",
    ])
    for lv in LEVELS:
        label = LEVEL_LABELS[lv]
        spec_cnt = spec_by_level[lv]
        impl_cnt = impl_by_level[lv]
        ratio = f"{spec_cnt / total_spec:.0%}" if total_spec > 0 else "-"
        if spec_cnt == 0:
            lv_status = "-"
        elif impl_cnt == 0:
            lv_status = STATUS_WARN
        else:
            lv_status = STATUS_OK
        lines.append(f"| {label} | {spec_cnt} | {ratio} | {impl_cnt} | {lv_status} |")
    total_ratio = f"{total_spec / total_spec:.0%}" if total_spec > 0 else "-"
    lines.append(f"| **合計** | **{total_spec}** | **{total_ratio}** | **{total_impl}** | |")

    # REQ × カテゴリ分布
    req_to_tags: dict[str, list[TestTag]] = defaultdict(list)
    for tag in tags:
        if tag.req_id:
            req_to_tags[tag.req_id].append(tag)

    cat_header = " | ".join(CATEGORY_LABELS[c] for c in CATEGORIES)
    cat_sep = " | ".join("---" for _ in CATEGORIES)
    lines.extend([
        "",
        "## 要件 × カテゴリ分布",
        "",
        "<details>",
        "<summary>凡例</summary>",
        "",
        "| カテゴリ | 説明 |",
        "|---------|------|",
        "| 正常系 | 典型的な入力・期待通りの出力 |",
        "| 異常値 | 不正入力・例外・エラー処理 |",
        "| 境界値 | 上限・下限・空・最大サイズ |",
        "| 分岐 | 条件分岐・フラグの組み合わせ |",
        "",
        "表中の数値は `テスト関数数/TC定義数`。`-` は spec.yml にそのカテゴリの TC が定義されていないことを示す。",
        "",
        "</details>",
        "",
        f"| REQ-ID | 要件名 | TC定義 | {cat_header} | テスト数 | 判定 |",
        f"|--------|--------|--------|{cat_sep} |---------|------|",
    ])

    for req_id, req_data in spec.items():
        title = req_data.get("title", "")
        perspectives = req_data.get("perspectives") or {}
        tc_count = len(perspectives)

        spec_by_cat: dict[str, int] = defaultdict(int)
        for tc_data in perspectives.values():
            spec_by_cat[tc_data.get("category", "")] += 1

        test_by_cat: dict[str, int] = defaultdict(int)
        for tag in req_to_tags.get(req_id, []):
            if tag.tc_id:
                cat = tc_to_category.get(tag.tc_id, "")
                if cat:
                    test_by_cat[cat] += 1

        linked = len(req_to_tags.get(req_id, []))

        has_gap = any(spec_by_cat[c] > 0 and test_by_cat[c] == 0 for c in CATEGORIES)
        if linked == 0:
            status = STATUS_ERROR
        elif has_gap:
            status = STATUS_WARN
        else:
            status = STATUS_OK

        cat_cells = " | ".join(
            f"{test_by_cat[c]}/{spec_by_cat[c]}" if spec_by_cat[c] > 0 else "-"
            for c in CATEGORIES
        )
        lines.append(f"| {req_id} | {title} | {tc_count} | {cat_cells} | {linked} | {status} |")

    # 要件 × レベル分布
    level_header_cells = " | ".join(LEVEL_LABELS[lv] for lv in LEVELS)
    level_sep_cells = " | ".join("---" for _ in LEVELS)
    lines.extend([
        "",
        "## 要件 × レベル分布",
        "",
        f"| REQ-ID | 要件名 | {level_header_cells} |",
        f"|--------|--------|{level_sep_cells} |",
    ])
    for req_id, req_data in spec.items():
        title = req_data.get("title", "")
        perspectives = req_data.get("perspectives") or {}
        cnt_by_level: dict[str, int] = defaultdict(int)
        for tc_data in perspectives.values():
            cnt_by_level[tc_data.get("level", DEFAULT_LEVEL)] += 1
        level_cells = " | ".join(
            str(cnt_by_level[lv]) if cnt_by_level[lv] > 0 else "-"
            for lv in LEVELS
        )
        lines.append(f"| {req_id} | {title} | {level_cells} |")

    # TC 詳細（REQ ごとに折りたたみ、表形式）
    tc_to_tags: dict[str, list[TestTag]] = defaultdict(list)
    for tag in tags:
        if tag.tc_id:
            tc_to_tags[tag.tc_id].append(tag)

    lines.extend(["", "## TC 詳細", ""])

    for req_id, req_data in spec.items():
        title = req_data.get("title", "")
        perspectives = req_data.get("perspectives") or {}

        lines.append("<details>")
        lines.append(f"<summary><strong>{req_id}: {title}</strong></summary>")
        lines.append("")
        lines.append(TC_DETAIL_HEADER)
        lines.append(TC_DETAIL_SEP)

        for tc_id, tc_data in perspectives.items():
            cat = tc_data.get("category", "")
            lv = tc_data.get("level", DEFAULT_LEVEL)
            desc = tc_data.get("desc", "")
            cat_label = CATEGORY_LABELS.get(cat, cat)
            lv_label = LEVEL_LABELS.get(lv, lv)
            linked_tests = tc_to_tags.get(tc_id, [])

            if linked_tests:
                for i, t in enumerate(linked_tests):
                    tc_cell   = tc_id     if i == 0 else ""
                    cat_cell  = cat_label if i == 0 else ""
                    lv_cell   = lv_label  if i == 0 else ""
                    desc_cell = desc      if i == 0 else ""
                    func_name = f"{t.class_name}::{t.func_name}" if t.class_name else t.func_name
                    lines.append(
                        f"| {tc_cell} | {cat_cell} | {lv_cell} | {desc_cell} | `{func_name}` | {t.first_line} |"
                    )
            else:
                lines.append(f"| {tc_id} | {cat_label} | {lv_label} | {desc} | *(テストなし)* | |")

        lines.append("")
        lines.append("</details>")
        lines.append("")

    # 複雑度テーブル（radon データがある場合のみ）
    if radon_data:
        lines.extend([
            "",
            "## ソース複雑度（radon cc）",
            "",
            "| ファイル | 最大複雑度 |",
            "|---------|-----------|",
        ])
        for fp, complexity in sorted(radon_data.items(), key=lambda x: -x[1]):
            lines.append(f"| `{fp}` | {complexity} |")

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("レポートを出力しました: %s", output)

    if errors:
        logger.error("%d 件の ERROR があります。", len(errors))
        sys.exit(1)


# --- エントリポイント ---

def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(levelname)s %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="テスト品質レポートを生成する（spec.yml × テストタグ × 複雑度）"
    )
    parser.add_argument(
        "--spec", type=Path, default=Path("tests/spec.yml"),
        help="spec.yml のパス（デフォルト: tests/spec.yml）",
    )
    parser.add_argument(
        "--test-dirs", type=Path, nargs="+", default=[Path("tests")],
        help="テストディレクトリ（デフォルト: tests/）",
    )
    parser.add_argument(
        "--radon-json", type=Path, default=None,
        help="radon cc -j の出力 JSON（省略時は複雑度列を非表示）",
    )
    parser.add_argument(
        "--output", type=Path, default=Path("reports/quality_report.md"),
        help="出力先 Markdown（デフォルト: reports/quality_report.md）",
    )
    args = parser.parse_args(argv)

    if not args.spec.exists():
        logger.error("spec.yml が見つかりません: %s", args.spec)
        sys.exit(1)

    spec = load_spec(args.spec)
    tags = scan_test_tags(args.test_dirs)
    issues = validate(spec, tags)
    radon_data = load_radon(args.radon_json)
    generate_report(spec, tags, issues, radon_data, args.output)


if __name__ == "__main__":
    main()
