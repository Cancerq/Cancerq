#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NVDRS · Construction 预处理：按 Census2018_Industry 筛 construction，
再用 construction case 的 index 去 NVDRS concat 总表里取行。

前提：已经按年龄切好了 18-67 岁的文件（18-27 / 28-37 / 38-47 / 48-57 / 58-67）。

两步：

  第 1 步  逐个年龄段文件读 Census2018_Industry，
           等于 "Construction"（不区分大小写、忽略首尾空格）的行 = construction case。
           把这些行的 index（默认 IncidentID + PersonID）收集起来。

  第 2 步  流式扫一遍 NVDRS concat 总表，
           index 落在第 1 步集合里的行全部取出，合成一张表，并补上 age_band 列。

用法：把下面「配置区」的路径空白填好，然后直接运行

    python run_construction_concat.py

也可以不改文件，用命令行覆盖：

    python run_construction_concat.py --input-dir "D:\\age_chunks" \\
        --concat-file "D:\\NVDRS_concat.csv" --output-dir "D:\\construction_out"

输出（OUTPUT_DIR 下）：

  construction_index.csv                  construction case 的 index + age_band + 来源文件
  construction_age_<段>.csv               每个年龄段的 construction 行（来自年龄段文件）
  construction_all_ages.csv               上面 5 个合并（带 age_band 列）
  NVDRS_concat_construction.csv           ★ 用 index 从 concat 总表取出的行（带 age_band 列）
  index_not_found_in_concat.csv           年龄段里有、concat 里找不到的 index（空 = 全部对上）
  industry_values.csv                     Census2018_Industry 每个取值的行数（核对用）
  summary.csv                             各年龄段计数

文件都是分块读、分块写的，GB 级输入也只占几百 MB 内存。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pandas as pd

# =============================================================================
# 配置区 —— 把路径填进下面的空白引号里
# =============================================================================

# 【输入 1】年龄段文件（18-67 岁）。两种填法，二选一。
#
# 填法 A：只填目录，脚本自动找里面文件名带 18_27 / 28-37 这类年龄段的 .csv
AGE_CHUNK_DIR = r""        # 例：r"D:\NVDRS\age_chunks"

# 填法 B：逐个列出文件（填了这个就忽略 AGE_CHUNK_DIR）
AGE_CHUNK_FILES = [
    r"",                   # 例：r"D:\NVDRS\age_chunks\nvdrs_age_18_27.csv"
    r"",                   # 例：r"D:\NVDRS\age_chunks\nvdrs_age_28_37.csv"
    r"",                   # 例：r"D:\NVDRS\age_chunks\nvdrs_age_38_47.csv"
    r"",                   # 例：r"D:\NVDRS\age_chunks\nvdrs_age_48_57.csv"
    r"",                   # 例：r"D:\NVDRS\age_chunks\nvdrs_age_58_67.csv"
]

# 【输入 2】NVDRS concat 总表（按 index 从这里取 construction 行）
NVDRS_CONCAT_FILE = r""    # 例：r"D:\NVDRS\NVDRS_concat.csv"

# 【输出】输出目录（不存在会自动创建）
OUTPUT_DIR = r""           # 例：r"D:\NVDRS\construction_out"

# -----------------------------------------------------------------------------
# 以下为可选项，通常不用改
# -----------------------------------------------------------------------------

# 行业列名。留空 = 自动找 Census2018_Industry
INDUSTRY_COL = r""

# Census2018_Industry 等于以下任一值即算 construction（不区分大小写、忽略首尾空格）
CONSTRUCTION_VALUES = ["Construction"]

# 用哪几列当 index 去匹配 concat 总表。留空 = 自动：
#   有 IncidentID 和 PersonID 就两列一起用（NVDRS 一个事件可能有多名死者，
#   只用 IncidentID 会把同一事件里的非建筑业死者也带进来）；
#   否则只用能找到的那一列。
# 两个文件列名不同时可写成 {"年龄段文件的列名": "concat 里的列名"}。
KEY_COLS: list | dict = []

# 输入文件编码。NVDRS 的 Windows 导出常见 "utf-8" 或 "cp1252"
ENCODING = "utf-8"

# 每次读入内存的行数。越小越省内存
CHUNK_SIZE = 100_000

# =============================================================================
# 配置区结束
# =============================================================================

AGE_BAND_RE = re.compile(r"(?<!\d)(\d{2})[_\-](\d{2})(?!\d)")
MISSING_TOKENS = {"", ".", "-", "--", "nan", "none", "null", "<na>"}
BLANK_LABEL = "(空白)"

INDUSTRY_COL_CANDIDATES = (
    "census2018industry", "censusindustry2018", "industrycensus2018",
    "census2018ind", "industry2018",
)
# 按优先级：先找两列组合，找不到再退到单列
KEY_COL_CANDIDATES = (
    ("incidentid", "personid"),
    ("incidentid",),
    ("personid",),
    ("index",),
    ("unnamed0",),   # pandas to_csv 默认写出的无名 index 列
)


def fail(message: str) -> "NoReturn":  # noqa: F821
    print(f"\n错误：{message}\n", file=sys.stderr)
    sys.exit(2)


def norm_text(value) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = re.sub(r"\s+", " ", str(value)).strip().lower()
    return "" if text in MISSING_TOKENS else text


def norm_colname(name) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def norm_key(series: pd.Series) -> pd.Series:
    """ID 统一成字符串：去空格，"12345.0" 当成 "12345"（Excel/浮点导出常见）。"""
    text = series.fillna("").astype(str).str.strip()
    return text.str.replace(r"^(\d+)\.0+$", r"\1", regex=True)


def band_from_name(path: Path) -> str | None:
    match = AGE_BAND_RE.search(path.stem)
    return f"{match.group(1)}-{match.group(2)}" if match else None


# ---- 输入输出路径 ------------------------------------------------------------

def resolve_age_files(input_dir: str, input_files: list[str]) -> list[Path]:
    listed = [f.strip() for f in input_files if f and f.strip()]
    if listed:
        paths = []
        for item in listed:
            path = Path(item)
            if not path.is_file():
                fail(f"AGE_CHUNK_FILES 里这个路径找不到：\n  {path}")
            paths.append(path)
    elif input_dir and input_dir.strip():
        directory = Path(input_dir.strip())
        if not directory.is_dir():
            fail(f"AGE_CHUNK_DIR 不是一个目录：\n  {directory}")
        # 只收文件名里带年龄段的：age_distribution.csv、*_excluded.csv 这类汇总不算样本
        paths = [
            p for p in sorted(directory.glob("*.csv"))
            if band_from_name(p) and not re.search(r"excluded|distribution", p.name, re.I)
        ]
        if not paths:
            fail(
                f"AGE_CHUNK_DIR 里没有文件名带年龄段（如 18_27）的 .csv：\n  {directory}\n"
                "请改用 AGE_CHUNK_FILES 逐个列出。"
            )
    else:
        fail(
            "年龄段输入路径还没填。请打开本脚本，在「配置区」填写：\n"
            "    AGE_CHUNK_DIR   = r\"...\"      （填目录）\n"
            "  或\n"
            "    AGE_CHUNK_FILES = [r\"...\", ]  （逐个列出文件）\n"
            "也可以用命令行：--input-dir \"路径\"  或  --input 文件1 文件2 ..."
        )

    seen, unique = set(), []
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique


def resolve_concat(concat_file: str) -> Path:
    if not concat_file or not concat_file.strip():
        fail(
            "NVDRS concat 总表路径还没填。请在「配置区」填写：\n"
            "    NVDRS_CONCAT_FILE = r\"...\"\n"
            "也可以用命令行：--concat-file \"路径\""
        )
    path = Path(concat_file.strip())
    if not path.is_file():
        fail(f"NVDRS_CONCAT_FILE 找不到：\n  {path}")
    return path


def resolve_output(output_dir: str) -> Path:
    if not output_dir or not output_dir.strip():
        fail(
            "输出路径还没填。请在「配置区」填写：\n"
            "    OUTPUT_DIR = r\"...\"\n"
            "也可以用命令行：--output-dir \"路径\""
        )
    path = Path(output_dir.strip())
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---- 列识别 ------------------------------------------------------------------

def find_industry_column(columns, override: str, file_name: str) -> str:
    if override and override.strip():
        if override.strip() not in columns:
            fail(f"{file_name} 里没有 INDUSTRY_COL 指定的列 {override.strip()!r}")
        return override.strip()
    normalised = {norm_colname(c): c for c in columns}
    for cand in INDUSTRY_COL_CANDIDATES:
        if cand in normalised:
            return normalised[cand]
    fail(
        f"{file_name} 里找不到 Census2018_Industry 列。\n"
        f"该文件的列有：{', '.join(map(str, columns))}\n"
        "请在配置区填 INDUSTRY_COL = r\"你的列名\"。"
    )


def resolve_key_columns(age_columns, concat_columns, key_cols) -> list[tuple[str, str]]:
    """返回 [(年龄段文件里的列名, concat 里的列名), ...]。"""
    if isinstance(key_cols, dict) and key_cols:
        pairs = list(key_cols.items())
    elif key_cols:
        pairs = [(c, c) for c in key_cols]
    else:
        age_norm = {norm_colname(c): c for c in age_columns}
        concat_norm = {norm_colname(c): c for c in concat_columns}
        pairs = []
        for combo in KEY_COL_CANDIDATES:
            if all(c in age_norm and c in concat_norm for c in combo):
                pairs = [(age_norm[c], concat_norm[c]) for c in combo]
                break
        if not pairs:
            fail(
                "两个文件里找不到共同的 index 列（找过 IncidentID / PersonID / index）。\n"
                f"  年龄段文件的列：{', '.join(map(str, age_columns))}\n"
                f"  concat 的列：  {', '.join(map(str, concat_columns))}\n"
                "请在配置区填 KEY_COLS，例如 [\"IncidentID\", \"PersonID\"]，\n"
                "列名不同时写 {\"年龄段里的列名\": \"concat 里的列名\"}。"
            )

    for age_col, concat_col in pairs:
        if age_col not in age_columns:
            fail(f"年龄段文件里没有 index 列 {age_col!r}")
        if concat_col not in concat_columns:
            fail(f"NVDRS concat 里没有 index 列 {concat_col!r}")
    return pairs


def make_keys(frame: pd.DataFrame, cols: list[str]) -> pd.Series:
    parts = [norm_key(frame[c]) for c in cols]
    key = parts[0]
    for part in parts[1:]:
        key = key + "|" + part
    return key


class Writer:
    """按需创建的追加写入器：表头只写一次；一行都没有也留带表头的空文件。"""

    def __init__(self, path: Path, encoding: str):
        self.path, self.encoding, self.handle, self.rows = path, encoding, None, 0

    def write(self, frame: pd.DataFrame) -> None:
        if frame.empty:
            return
        if self.handle is None:
            self.handle = open(self.path, "w", newline="", encoding=self.encoding)
            frame.to_csv(self.handle, index=False, header=True)
        else:
            frame.to_csv(self.handle, index=False, header=False)
        self.rows += len(frame)

    def finish(self, columns) -> None:
        if self.handle is None:
            pd.DataFrame(columns=list(columns)).to_csv(
                self.path, index=False, encoding=self.encoding
            )
        else:
            self.handle.close()
            self.handle = None


# ---- 主流程 ------------------------------------------------------------------

def run(
    input_dir: str,
    input_files: list[str],
    concat_file: str,
    output_dir: str,
    *,
    industry_col: str = "",
    construction_values=("Construction",),
    key_cols: list | dict = (),
    encoding: str = "utf-8",
    chunk_size: int = 100_000,
) -> dict:
    age_files = resolve_age_files(input_dir, input_files)
    concat_path = resolve_concat(concat_file)
    out_dir = resolve_output(output_dir)
    wanted = {norm_text(v) for v in construction_values} - {""}
    if not wanted:
        fail("CONSTRUCTION_VALUES 不能为空")

    concat_columns = list(pd.read_csv(concat_path, dtype=str, nrows=0, encoding=encoding).columns)

    print("=" * 78)
    print("年龄段输入：")
    for path in age_files:
        print(f"  [{band_from_name(path) or path.stem:>5}] {path}")
    print(f"NVDRS concat：{concat_path}")
    print(f"输出目录：    {out_dir}")
    print(f"construction 判定：Census2018_Industry ∈ {sorted(construction_values)}")
    print("=" * 78)

    # ---- 第 1 步：年龄段文件 -> construction index ----------------------------
    key_to_band: dict[str, str] = {}
    duplicate_keys: list[str] = []
    index_rows: list[pd.DataFrame] = []
    industry_counts: dict[str, int] = {}
    summary_rows: list[dict] = []
    key_pairs = None
    all_ages_writer = Writer(out_dir / "construction_all_ages.csv", encoding)
    all_ages_columns = None

    for path in age_files:
        band = band_from_name(path) or path.stem
        header = list(pd.read_csv(path, dtype=str, nrows=0, encoding=encoding).columns)
        ind_col = find_industry_column(header, industry_col, path.name)
        pairs = resolve_key_columns(header, concat_columns, key_cols)
        if key_pairs is None:
            key_pairs = pairs
        elif pairs != key_pairs:
            fail(f"{path.name} 识别出的 index 列 {pairs} 和前面文件的 {key_pairs} 不一致")
        age_keys = [a for a, _ in pairs]

        out_columns = header + (["age_band"] if "age_band" not in header else [])
        if all_ages_columns is None:
            all_ages_columns = out_columns
        elif out_columns != all_ages_columns:
            fail(
                f"{path.name} 的列和前面的文件不一致，合并会错位。\n"
                f"  前面：{all_ages_columns}\n  这个：{out_columns}"
            )

        band_writer = Writer(out_dir / f"construction_age_{band.replace('-', '_')}.csv", encoding)
        n_read = n_constr = n_blank = 0

        for chunk in pd.read_csv(path, dtype=str, encoding=encoding, chunksize=chunk_size):
            industry = chunk[ind_col].map(norm_text)
            for value, n in chunk[ind_col].fillna(BLANK_LABEL).value_counts().items():
                industry_counts[value] = industry_counts.get(value, 0) + int(n)
            mask = industry.isin(wanted)
            n_read += len(chunk)
            n_blank += int((industry == "").sum())

            hits = chunk.loc[mask].copy()
            if hits.empty:
                continue
            n_constr += len(hits)
            hits["age_band"] = band
            hits = hits[out_columns]
            band_writer.write(hits)
            all_ages_writer.write(hits)

            keys = make_keys(hits, age_keys)
            for key in keys:
                if key in key_to_band:
                    duplicate_keys.append(key)
                else:
                    key_to_band[key] = band
            idx = hits[age_keys].copy()
            idx["age_band"] = band
            idx["source_file"] = str(path)
            index_rows.append(idx)

        band_writer.finish(out_columns)
        summary_rows.append({
            "age_band": band,
            "input_file": str(path),
            "rows_read": n_read,
            "construction": n_constr,
            "industry_blank": n_blank,
        })
        pct = n_constr / n_read * 100 if n_read else 0.0
        print(f"  [{band:>5}] {n_read:,} 行 -> construction {n_constr:,} ({pct:.2f}%)")

    all_ages_writer.finish(all_ages_columns or [])

    age_key_cols = [a for a, _ in key_pairs]
    concat_key_cols = [c for _, c in key_pairs]
    index_df = (
        pd.concat(index_rows, ignore_index=True) if index_rows
        else pd.DataFrame(columns=age_key_cols + ["age_band", "source_file"])
    )
    index_df.to_csv(out_dir / "construction_index.csv", index=False, encoding=encoding)
    print(f"\nconstruction index：{len(key_to_band):,} 个（按 {' + '.join(age_key_cols)}）")
    if duplicate_keys:
        print(f"  警告：{len(duplicate_keys):,} 个 index 在年龄段文件里重复出现，只算一次")

    # ---- 第 2 步：用 index 从 NVDRS concat 取行 --------------------------------
    out_concat_columns = concat_columns + (["age_band"] if "age_band" not in concat_columns else [])
    concat_writer = Writer(out_dir / "NVDRS_concat_construction.csv", encoding)
    found: set[str] = set()
    n_concat = n_concat_dup = 0

    for chunk in pd.read_csv(concat_path, dtype=str, encoding=encoding, chunksize=chunk_size):
        n_concat += len(chunk)
        keys = make_keys(chunk, concat_key_cols)
        mask = keys.isin(key_to_band.keys())
        if not mask.any():
            continue
        hits = chunk.loc[mask].copy()
        hit_keys = keys[mask]
        n_concat_dup += int(hit_keys.isin(found).sum() + hit_keys.duplicated().sum())
        found.update(hit_keys)
        hits["age_band"] = hit_keys.map(key_to_band).values
        concat_writer.write(hits[out_concat_columns])

    concat_writer.finish(out_concat_columns)

    missing = [k for k in key_to_band if k not in found]
    missing_df = pd.DataFrame(
        [k.split("|") + [key_to_band[k]] for k in missing],
        columns=age_key_cols + ["age_band"],
    )
    missing_df.to_csv(out_dir / "index_not_found_in_concat.csv", index=False, encoding=encoding)

    summary = pd.DataFrame(summary_rows)
    summary.to_csv(out_dir / "summary.csv", index=False, encoding=encoding)
    values = pd.DataFrame(
        [
            {"Census2018_Industry": v, "n": n, "is_construction": norm_text(v) in wanted}
            for v, n in sorted(industry_counts.items(), key=lambda kv: -kv[1])
        ]
    )
    values.to_csv(out_dir / "industry_values.csv", index=False, encoding=encoding)

    # ---- 报告 --------------------------------------------------------------
    print("\n" + "=" * 78)
    print(f"NVDRS concat 扫描 {n_concat:,} 行，取出 {concat_writer.rows:,} 行")
    print(f"  index 对上：{len(found):,} / {len(key_to_band):,}")
    if missing:
        print(f"  警告：{len(missing):,} 个 index 在 concat 里找不到，见 index_not_found_in_concat.csv")
    if n_concat_dup:
        print(f"  警告：concat 里有 {n_concat_dup:,} 行 index 重复（同一人多行），都保留了")
    print("=" * 78)
    print(summary[["age_band", "rows_read", "construction", "industry_blank"]].to_string(index=False))
    print(f"\n★ 主输出：{concat_writer.path}")

    return {
        "summary": summary,
        "n_construction_keys": len(key_to_band),
        "n_found": len(found),
        "n_missing": len(missing),
        "concat_rows_out": concat_writer.rows,
        "key_cols": key_pairs,
        "output_dir": out_dir,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="按 Census2018_Industry 筛 construction，并用其 index 从 NVDRS concat 取行",
    )
    parser.add_argument("--input-dir", default=None, help="覆盖配置区的 AGE_CHUNK_DIR")
    parser.add_argument("--input", nargs="+", default=None, help="覆盖配置区的 AGE_CHUNK_FILES")
    parser.add_argument("--concat-file", default=None, help="覆盖配置区的 NVDRS_CONCAT_FILE")
    parser.add_argument("--output-dir", default=None, help="覆盖配置区的 OUTPUT_DIR")
    parser.add_argument("--industry-col", default=None)
    parser.add_argument("--key-cols", nargs="+", default=None, help="例：--key-cols IncidentID PersonID")
    parser.add_argument("--encoding", default=None)
    parser.add_argument("--chunk-size", type=int, default=None)
    args = parser.parse_args(argv)

    result = run(
        args.input_dir if args.input_dir is not None else AGE_CHUNK_DIR,
        args.input if args.input is not None else AGE_CHUNK_FILES,
        args.concat_file if args.concat_file is not None else NVDRS_CONCAT_FILE,
        args.output_dir if args.output_dir is not None else OUTPUT_DIR,
        industry_col=args.industry_col if args.industry_col is not None else INDUSTRY_COL,
        construction_values=CONSTRUCTION_VALUES,
        key_cols=args.key_cols if args.key_cols is not None else KEY_COLS,
        encoding=args.encoding if args.encoding is not None else ENCODING,
        chunk_size=args.chunk_size if args.chunk_size is not None else CHUNK_SIZE,
    )
    return 0 if result["concat_rows_out"] else 1


if __name__ == "__main__":
    sys.exit(main())
