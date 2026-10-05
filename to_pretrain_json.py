#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 CSV / TSV / Excel 表格转成 LLM 预训练用的 JSON 格式。

每一行表格 -> 一条样本 {"text": "..."}，默认输出 JSONL（一行一个 JSON），
这是 Hugging Face datasets、Megatron、LLaMA-Factory 等预训练工具通用的格式：

    {"text": "Age: 34\\nSex: Male\\nNarrative: ..."}
    {"text": "Age: 51\\nSex: Female\\nNarrative: ..."}

text 的三种拼法（按优先级）：

    1. TEMPLATE   自定义模板，用 {列名} 引用列：
                  "{Age}岁{Sex}，职业：{Occupation}。经过：{Narrative}"
    2. TEXT_COLS  只取这几列的值，用 JOINER 连起来（适合叙述文本列）
    3. 都不填     每个非空列写成 "列名: 值"，一列一行

可选：
    META_COLS     额外原样放进每条样本的列（如 ID、年份），不参与 text
    ADD_SOURCE    记录来源文件名和行号，方便回查
    MIN_CHARS     text 太短的行丢掉
    DEDUPE        去掉 text 完全相同的重复样本
    OUTPUT_FORMAT "jsonl"（默认）或 "json"（一个大数组）

输入可以是单个文件、多个文件或目录（目录里的 .csv/.tsv/.xlsx/.xls 全部处理，
Excel 的每个工作表都会读）。CSV 分块读，几 GB 的文件也只占几百 MB 内存。

依赖：pandas；读 .xlsx 需要 openpyxl，读 .xls 需要 xlrd。

用法 1：填好下面「配置区」，然后

    python to_pretrain_json.py

用法 2：命令行（覆盖配置区）

    python to_pretrain_json.py --input data.xlsx --output pretrain.jsonl
    python to_pretrain_json.py --input data/ --output out.jsonl --text-cols Narrative
    python to_pretrain_json.py --input a.csv --output out.jsonl \\
        --template "{Age}岁{Sex}。经过：{Narrative}" --meta-cols CaseID
"""

from __future__ import annotations

import argparse
import codecs
import hashlib
import json
import math
import re
import string
import sys
from pathlib import Path
from typing import Iterable, Iterator

import pandas as pd

# =============================================================================
# 配置区 —— 把路径填进下面的空白引号里
# =============================================================================

# 【必填 1】输入：文件或目录，可以填多个
INPUT_PATHS = [
    r"",                 # 例：r"D:\data\cases.xlsx"  或  r"D:\data\csv_folder"
]

# 【必填 2】输出文件
OUTPUT_PATH = r""        # 例：r"D:\data\pretrain.jsonl"，填文件夹也行（自动起文件名）

# -----------------------------------------------------------------------------
# text 怎么拼（三选一，都不填 = 每列写成 "列名: 值"）
# -----------------------------------------------------------------------------

# 模板，{列名} 会替换成该列的值。列名里有空格也行：{Death Year}
TEMPLATE = r""

# 只用这些列当正文，按顺序用 JOINER 连接
TEXT_COLS: list[str] = []

# 只拼这些列（仅对第 3 种默认拼法生效）。留空 = 全部列
INCLUDE_COLS: list[str] = []

# 这些列不拼进 text（仅对第 3 种默认拼法生效）
EXCLUDE_COLS: list[str] = []

JOINER = "\n"
KEY_VALUE_SEP = ": "

# -----------------------------------------------------------------------------
# 以下通常不用改
# -----------------------------------------------------------------------------

# 原样放进每条样本的附加字段（不进 text）
META_COLS: list[str] = []

# 记录来源 {"source": "cases.xlsx#Sheet1", "row": 12}（row = 数据行号，从 1 起，不含表头）
ADD_SOURCE = False

# 正文少于这么多字符的样本丢弃
MIN_CHARS = 1

# 去掉 text 完全相同的样本
DEDUPE = False

# "jsonl" 或 "json"
OUTPUT_FORMAT = "jsonl"

# 正文字段名。大部分预训练框架默认读 "text"
TEXT_FIELD = "text"

# Excel 只读这些工作表。留空 = 全部
SHEETS: list[str] = []

# CSV 编码。"auto" = 依次尝试 utf-8-sig / gb18030 / latin-1
ENCODING = "auto"
CHUNK_SIZE = 50_000

# =============================================================================
# 配置区结束
# =============================================================================

SUPPORTED_SUFFIXES = {".csv", ".tsv", ".txt", ".xlsx", ".xlsm", ".xls"}
EXCEL_SUFFIXES = {".xlsx", ".xlsm", ".xls"}
AUTO_ENCODINGS = ("utf-8-sig", "gb18030", "latin-1")
MISSING_TOKENS = {"", "nan", "none", "null", "<na>", "nat"}
WHITESPACE_RUN = re.compile(r"[ \t\u3000]+")


# -----------------------------------------------------------------------------
# 取值与清洗
# -----------------------------------------------------------------------------

def clean_value(value) -> str:
    """单元格 -> 字符串。空值返回 ""；整数形式的浮点 34.0 写成 34。"""
    if value is None:
        return ""
    if isinstance(value, float):
        if math.isnan(value):
            return ""
        if value.is_integer():
            return str(int(value))
    if isinstance(value, pd.Timestamp):
        if pd.isna(value):
            return ""
        return value.strftime("%Y-%m-%d") if value == value.normalize() else str(value)
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    lines = [WHITESPACE_RUN.sub(" ", line).strip() for line in text.split("\n")]
    text = "\n".join(line for line in lines if line)
    return "" if text.lower() in MISSING_TOKENS else text


def template_fields(template: str) -> list[str]:
    return [name for _, name, _, _ in string.Formatter().parse(template) if name]


def render_template(template: str, row: dict) -> str:
    """{列名} 替换成值。缺值替换成空串；字面花括号写成 {{ }}。"""
    out = []
    for literal, name, _, _ in string.Formatter().parse(template):
        out.append(literal)
        if name is not None:
            out.append(row.get(name, ""))
    return "".join(out).strip()


class RowFormatter:
    def __init__(self, template: str, text_cols: list[str], include_cols: list[str],
                 exclude_cols: list[str], joiner: str, kv_sep: str):
        self.template = template
        self.text_cols = text_cols
        self.include_cols = include_cols
        self.exclude_cols = set(exclude_cols)
        self.joiner = joiner
        self.kv_sep = kv_sep

    def required_cols(self) -> list[str]:
        if self.template:
            return template_fields(self.template)
        if self.text_cols:
            return list(self.text_cols)
        return list(self.include_cols)

    def format(self, row: dict, columns: list[str]) -> str:
        if self.template:
            # 模板里的列全空时只剩字面文字（如 "岁："），不算样本
            if not any(row.get(c) for c in template_fields(self.template)):
                return ""
            return render_template(self.template, row)
        if self.text_cols:
            return self.joiner.join(row[c] for c in self.text_cols if row.get(c))
        cols = self.include_cols or columns
        parts = [f"{c}{self.kv_sep}{row[c]}" for c in cols
                 if c not in self.exclude_cols and row.get(c)]
        return self.joiner.join(parts)


# -----------------------------------------------------------------------------
# 读文件
# -----------------------------------------------------------------------------

def collect_inputs(paths: Iterable[str]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        if not raw or not raw.strip():
            continue
        p = Path(raw.strip()).expanduser()
        if p.is_dir():
            files.extend(sorted(f for f in p.rglob("*")
                                if f.is_file() and f.suffix.lower() in SUPPORTED_SUFFIXES
                                and not f.name.startswith("~$")))
        elif p.is_file():
            files.append(p)
        else:
            sys.exit(f"找不到输入：{p}")
    if not files:
        sys.exit("没有可处理的输入文件（支持 .csv .tsv .txt .xlsx .xlsm .xls）")
    return files


def detect_encoding(path: Path, encoding: str) -> str:
    if encoding != "auto":
        return encoding
    with open(path, "rb") as fh:
        sample = fh.read(1 << 20)
    for enc in AUTO_ENCODINGS:
        # final=False：样本截断在多字节字符中间不算错
        try:
            codecs.getincrementaldecoder(enc)().decode(sample, final=False)
            return enc
        except UnicodeDecodeError:
            continue
    return "latin-1"


def iter_frames(path: Path, sheets: list[str], encoding: str,
                chunk_size: int) -> Iterator[tuple[str, pd.DataFrame]]:
    """产出 (来源标签, DataFrame)。所有值按字符串读，避免 ID 前导 0 丢失。"""
    suffix = path.suffix.lower()
    if suffix in EXCEL_SUFFIXES:
        book = pd.read_excel(path, sheet_name=sheets or None, dtype=object)
        for sheet_name, df in book.items():
            yield f"{path.name}#{sheet_name}", df
        return
    # .txt 分隔符不确定，交给 python 引擎嗅探；.csv/.tsv 用更快的 C 引擎
    sep = {".csv": ",", ".tsv": "\t"}.get(suffix)
    enc = detect_encoding(path, encoding)
    reader = pd.read_csv(path, sep=sep, engine="c" if sep else "python",
                         dtype=str, keep_default_na=False, encoding=enc,
                         chunksize=chunk_size)
    for chunk in reader:
        yield path.name, chunk


def check_columns(source: str, columns: list[str], wanted: list[str]) -> None:
    missing = [c for c in wanted if c not in columns]
    if missing:
        sys.exit(f"{source} 缺少列：{missing}\n现有列：{columns}")


# -----------------------------------------------------------------------------
# 主流程
# -----------------------------------------------------------------------------

def iter_records(files: list[Path], formatter: RowFormatter, meta_cols: list[str],
                 add_source: bool, min_chars: int, dedupe: bool, text_field: str,
                 sheets: list[str], encoding: str, chunk_size: int,
                 stats: dict) -> Iterator[dict]:
    seen: set[bytes] = set()
    for path in files:
        row_no: dict[str, int] = {}
        for source, df in iter_frames(path, sheets, encoding, chunk_size):
            df.columns = [str(c).strip() for c in df.columns]
            columns = list(df.columns)
            check_columns(source, columns, formatter.required_cols() + meta_cols)
            start = row_no.get(source, 0)
            for offset, values in enumerate(df.itertuples(index=False, name=None)):
                stats["rows"] += 1
                row = {c: clean_value(v) for c, v in zip(columns, values)}
                text = formatter.format(row, columns)
                if len(text) < max(min_chars, 1):
                    stats["short"] += 1
                    continue
                if dedupe:
                    digest = hashlib.md5(text.encode("utf-8")).digest()
                    if digest in seen:
                        stats["duplicate"] += 1
                        continue
                    seen.add(digest)
                record = {text_field: text}
                for c in meta_cols:
                    record[c] = row[c]
                if add_source:
                    record["source"] = source
                    record["row"] = start + offset + 1
                stats["written"] += 1
                yield record
            row_no[source] = start + len(df)


def resolve_output(output: Path, files: list[Path], fmt: str | None) -> tuple[Path, str]:
    """OUTPUT 是文件夹（已存在或没有后缀）时，在里面自动起文件名：
    单个输入用输入文件名（cases.xlsx -> cases.jsonl），多个输入用 pretrain。"""
    is_dir = output.is_dir() or not output.suffix
    if fmt is None:
        fmt = "json" if not is_dir and output.suffix.lower() == ".json" else OUTPUT_FORMAT
    if is_dir:
        stem = files[0].stem if len(files) == 1 else "pretrain"
        output = output / f"{stem}.{fmt}"
    return output, fmt


def open_output(output: Path):
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        return open(output, "w", encoding="utf-8", newline="\n")
    except PermissionError:
        sys.exit(f"没有权限写入：{output}\n"
                 "请检查：这个文件是否正被其他程序打开；路径是否指向只读位置。")


def write_records(records: Iterable[dict], output: Path, fmt: str) -> None:
    with open_output(output) as fh:
        if fmt == "jsonl":
            for rec in records:
                fh.write(json.dumps(rec, ensure_ascii=False))
                fh.write("\n")
        else:
            fh.write("[\n")
            for i, rec in enumerate(records):
                if i:
                    fh.write(",\n")
                fh.write("  " + json.dumps(rec, ensure_ascii=False))
            fh.write("\n]\n")


def split_list(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="CSV / Excel -> LLM 预训练 JSON(L)")
    ap.add_argument("--input", nargs="+", help="输入文件或目录，可多个")
    ap.add_argument("--output", help="输出文件 .jsonl / .json")
    ap.add_argument("--template", help='模板，例如 "{Age}岁。经过：{Narrative}"')
    ap.add_argument("--text-cols", help="正文列，逗号分隔")
    ap.add_argument("--include-cols", help="默认拼法只用这些列，逗号分隔")
    ap.add_argument("--exclude-cols", help="默认拼法排除这些列，逗号分隔")
    ap.add_argument("--meta-cols", help="附加字段列，逗号分隔")
    ap.add_argument("--joiner", help=r'列之间的分隔符，默认 "\n"')
    ap.add_argument("--sheets", help="Excel 工作表名，逗号分隔")
    ap.add_argument("--format", choices=["jsonl", "json"], help="输出格式")
    ap.add_argument("--text-field", help='正文字段名，默认 "text"')
    ap.add_argument("--min-chars", type=int, help="正文最少字符数")
    ap.add_argument("--dedupe", action="store_true", default=None, help="去重")
    ap.add_argument("--add-source", action="store_true", default=None,
                    help="记录来源文件和行号")
    ap.add_argument("--encoding", help='CSV 编码，默认 "auto"')
    return ap.parse_args(argv)


def pick(cli, default):
    return default if cli is None else cli


def main(argv: list[str] | None = None) -> dict:
    args = parse_args(argv)
    inputs = pick(args.input, INPUT_PATHS)
    output_raw = pick(args.output, OUTPUT_PATH)
    if not output_raw or not output_raw.strip():
        sys.exit("请填写 OUTPUT_PATH 或传 --output")
    output = Path(output_raw.strip()).expanduser()

    joiner = JOINER if args.joiner is None else \
        args.joiner.replace("\\n", "\n").replace("\\t", "\t")

    formatter = RowFormatter(
        template=pick(args.template, TEMPLATE),
        text_cols=pick(split_list(args.text_cols), TEXT_COLS),
        include_cols=pick(split_list(args.include_cols), INCLUDE_COLS),
        exclude_cols=pick(split_list(args.exclude_cols), EXCLUDE_COLS),
        joiner=joiner,
        kv_sep=KEY_VALUE_SEP,
    )
    meta_cols = pick(split_list(args.meta_cols), META_COLS)
    text_field = pick(args.text_field, TEXT_FIELD)
    if text_field in meta_cols:
        sys.exit(f"META_COLS 里不能有正文字段名 {text_field!r}")

    files = collect_inputs(inputs)
    output, fmt = resolve_output(output, files, args.format)
    stats = {"rows": 0, "written": 0, "short": 0, "duplicate": 0}
    records = iter_records(
        files, formatter, meta_cols,
        add_source=pick(args.add_source, ADD_SOURCE),
        min_chars=pick(args.min_chars, MIN_CHARS),
        dedupe=pick(args.dedupe, DEDUPE),
        text_field=text_field,
        sheets=pick(split_list(args.sheets), SHEETS),
        encoding=pick(args.encoding, ENCODING),
        chunk_size=CHUNK_SIZE,
        stats=stats,
    )
    write_records(records, output, fmt)

    print(f"输入文件 {len(files)} 个，共 {stats['rows']} 行")
    print(f"写出 {stats['written']} 条 -> {output}  ({fmt})")
    if stats["short"]:
        print(f"丢弃 过短/空白 {stats['short']} 条")
    if stats["duplicate"]:
        print(f"丢弃 重复 {stats['duplicate']} 条")
    return stats


if __name__ == "__main__":
    main()
