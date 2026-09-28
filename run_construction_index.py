#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NVDRS construction 预处理：按 Census2018_Industry 筛出 construction case，
记下每个 case 的 index，再用 index 把各年龄段的 construction case concat 成一份。

输入：已经按年龄切好的 NVDRS CSV（18-27 / 28-37 / 38-47 / 48-57 / 58-67）
判断：Census2018_Industry【等于】 "Construction"（不区分大小写、忽略首尾空格）
      —— "Construction and extraction" 之类不算，要算就加进 CONSTRUCTION_VALUES
      列里是行业码的话，0770 / 770 也算

两步：

    第 1 步  取 index   只读行业列（和 ID 列），记下每个 construction case
                         在原文件里的位置：source_file + source_row_index
    第 2 步  按 index 取行并 concat
                         回到原文件按 index 取出整行，写成每个年龄段一份，
                         再把所有年龄段 concat 成一份总表

输出目录：

    OUTPUT_DIR/
      construction_index.csv              第 1 步的 index 表（每个 case 一行）
      NVDRS_construction_concat.csv       第 2 步：全部年龄段 concat 后的总表
      by_age/
        nvdrs_age_18_27_construction.csv  每个年龄段各一份
        ...
      summary_by_age.csv                  每个年龄段：读入行数 / construction 数 / 行业为空数
      industry_values.csv                 行业列出现过的所有取值及计数（核对哪些没被算进来）

总表和分年龄段文件都在最前面加了三列：
    age_band           年龄段（从文件名读，如 nvdrs_age_18_27.csv -> 18-27）
    source_file        来自哪个文件
    source_row_index   在原文件里的行号（从 0 开始，不含表头；
                       用 Excel 打开原文件时对应第 source_row_index + 2 行）

单文件，只依赖 pandas。分块读，GB 级的文件也只占几百 MB 内存。

用法：把「配置区」的路径填好，然后运行

    python run_construction_index.py
"""

from __future__ import annotations

import argparse
import codecs
import re
import sys
from pathlib import Path

import pandas as pd

# =============================================================================
# 配置区 —— 把路径填进下面的空白引号里
# =============================================================================

# 【必填 1】输入。两种填法，二选一即可。
#
# 填法 A：只填目录，读里面所有年龄段 CSV（自动跳过本脚本的输出和汇总文件）
INPUT_DIR = r""          # 例：r"D:\School_project\Project\NVDRS\age_chunks"

# 填法 B：逐个列出文件（填了这个就忽略 INPUT_DIR）
INPUT_FILES = [
    r"",                 # 例：r"D:\...\age_chunks\nvdrs_age_18_27.csv"
    r"",                 # 例：r"D:\...\age_chunks\nvdrs_age_28_37.csv"
    r"",                 # 例：r"D:\...\age_chunks\nvdrs_age_38_47.csv"
    r"",                 # 例：r"D:\...\age_chunks\nvdrs_age_48_57.csv"
    r"",                 # 例：r"D:\...\age_chunks\nvdrs_age_58_67.csv"
]

# 【必填 2】输出目录（不存在会自动创建）
OUTPUT_DIR = r""         # 例：r"D:\School_project\Project\NVDRS\construction_out"

# -----------------------------------------------------------------------------
# 以下通常不用改
# -----------------------------------------------------------------------------

# 行业列名。留空 = 自动找 Census2018_Industry
INDUSTRY_COL = r""

# 行业列【等于】其中任何一个就算 construction（不区分大小写、忽略首尾空格）
CONSTRUCTION_VALUES = ["Construction", "0770", "770"]

# 写进 index 表的 ID 列。留空 = 自动找 IncidentID / PersonID 等（有几个带几个）
ID_COLS: list[str] = []

# 输出总表的文件名
CONCAT_NAME = "NVDRS_construction_concat.csv"

# CSV 编码。留空 = 自动识别（依次试 UTF-8 / GBK / Windows-1252）
ENCODING = ""
CHUNK_SIZE = 50_000

# =============================================================================
# 配置区结束
# =============================================================================

INDUSTRY_COL_CANDIDATES = ("census2018industry", "censusindustry2018",
                           "industrycensus2018", "census2018ind", "industry2018")
ID_COL_CANDIDATES = ("incidentid", "personid", "victimid", "caseid", "recordid",
                     "siteid", "incidentyear")
ENCODING_CANDIDATES = ("utf-8-sig", "gbk", "cp1252")
ADDED_COLS = ["age_band", "source_file", "source_row_index"]

SKIP_NAME_PATTERNS = (
    re.compile(r"construction", re.I),
    re.compile(r"^summary", re.I),
    re.compile(r"_values\.csv$", re.I),
    re.compile(r"distribution", re.I),
    re.compile(r"_excluded", re.I),
    re.compile(r"_index\.csv$", re.I),
)
AGE_BAND_RE = re.compile(r"(?<!\d)(\d{2})[_\-](\d{2})(?!\d)")
BLANK_LABEL = "(空白)"


def fail(message: str) -> "NoReturn":  # noqa: F821
    print(f"\n错误：{message}\n", file=sys.stderr)
    sys.exit(2)


def norm_colname(name) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower().lstrip("\ufeff"))


def norm_value(value) -> str:
    return re.sub(r"\s+", " ", str(value)).strip().lower()


def find_col(columns, candidates):
    normalised = {norm_colname(c): c for c in columns}
    for cand in candidates:
        if cand in normalised:
            return normalised[cand]
    return None


def band_from_name(path: Path) -> str:
    m = AGE_BAND_RE.search(path.stem)
    return f"{m.group(1)}-{m.group(2)}" if m else path.stem


def detect_encoding(path: Path, preferred: str = "") -> str:
    with open(path, "rb") as handle:
        sample = handle.read(4 * 1024 * 1024)
    tried = []
    for enc in ([preferred] if preferred else []) + list(ENCODING_CANDIDATES):
        if enc in tried:
            continue
        tried.append(enc)
        try:
            codecs.getincrementaldecoder(enc)().decode(sample, final=False)
        except (UnicodeDecodeError, LookupError):
            continue
        return enc
    fail(f"认不出文件编码：\n  {path}\n试过：{', '.join(tried)}。"
         "请在配置区填 ENCODING。")


def resolve_inputs(input_files, input_dir) -> list[Path]:
    listed = [f.strip() for f in input_files if f and f.strip()]
    if listed:
        paths = [Path(f) for f in listed]
        for path in paths:
            if not path.is_file():
                fail(f"INPUT_FILES 里这个路径找不到：\n  {path}")
    elif input_dir and input_dir.strip():
        directory = Path(input_dir.strip())
        if not directory.is_dir():
            fail(f"INPUT_DIR 不是一个目录：\n  {directory}")
        every = sorted(directory.glob("*.csv"))
        paths = [p for p in every
                 if not any(pat.search(p.name) for pat in SKIP_NAME_PATTERNS)]
        skipped = sorted(set(every) - set(paths))
        if skipped:
            print("已跳过以下非年龄段文件：")
            for p in skipped:
                print(f"  - {p.name}")
        if not paths:
            fail(f"INPUT_DIR 里没有可用的 .csv：\n  {directory}")
    else:
        fail("输入路径还没填。请在「配置区」填 INPUT_DIR 或 INPUT_FILES，\n"
             "也可以用命令行：--input 文件1 文件2 ... 或 --input-dir 目录")

    seen, unique = set(), []
    for path in paths:
        if path.resolve() in seen:
            print(f"  （重复路径，只算一次）{path.name}")
            continue
        seen.add(path.resolve())
        unique.append(path)
    return unique


def read_chunks(path: Path, encoding: str, chunk_size: int, usecols=None):
    # dtype=str + keep_default_na=False：原样保留每个格子（前导 0、"NA" 字样都不变）
    return pd.read_csv(path, dtype=str, keep_default_na=False, encoding=encoding,
                       usecols=usecols, chunksize=chunk_size)


# -----------------------------------------------------------------------------
# 第 1 步：取 index
# -----------------------------------------------------------------------------

def build_index(paths, *, industry_col="", construction_values=CONSTRUCTION_VALUES,
                id_cols=(), encoding="", chunk_size=CHUNK_SIZE):
    """只读行业列和 ID 列，返回 (index 表, 每个文件的信息, 行业取值计数)。"""
    targets = {norm_value(v) for v in construction_values if norm_value(v)}
    if not targets:
        fail("CONSTRUCTION_VALUES 是空的")

    index_parts, files, value_counts = [], [], {}
    for path in paths:
        enc = detect_encoding(path, encoding)
        header = list(pd.read_csv(path, nrows=0, encoding=enc).columns)
        icol = industry_col or find_col(header, INDUSTRY_COL_CANDIDATES)
        if not icol or icol not in header:
            fail(f"{path.name}：找不到行业列 Census2018_Industry，"
                 "请在配置区填 INDUSTRY_COL。\n"
                 f"该文件的列有：{', '.join(map(str, header))}")
        ids = list(id_cols) if id_cols else [
            c for c in (find_col(header, (cand,)) for cand in ID_COL_CANDIDATES) if c]
        ids = [c for c in dict.fromkeys(ids) if c != icol]
        for c in ids:
            if c not in header:
                fail(f"{path.name}：ID_COLS 里的 {c!r} 不在文件里。")

        band = band_from_name(path)
        print(f"第 1 步  {path.name}   年龄段={band}  编码={enc}  行业列={icol}"
              + (f"  ID列={', '.join(ids)}" if ids else ""))

        rows = blank = 0
        for chunk in read_chunks(path, enc, chunk_size, usecols=[icol] + ids):
            rows += len(chunk)
            values = chunk[icol].map(norm_value)
            blank += int((values == "").sum())
            for raw, n in chunk[icol].str.strip().replace("", BLANK_LABEL) \
                    .value_counts().items():
                value_counts[raw] = value_counts.get(raw, 0) + int(n)
            hit = chunk[values.isin(targets)]
            if not hit.empty:
                part = pd.DataFrame({
                    "age_band": band,
                    "source_file": path.name,
                    "source_row_index": hit.index,
                })
                for c in ids:
                    part[c] = hit[c].to_numpy()
                part["Census2018_Industry"] = hit[icol].to_numpy()
                index_parts.append(part)

        files.append({"path": path, "encoding": enc, "age_band": band,
                      "header": header, "rows": rows, "blank_industry": blank})

    index = (pd.concat(index_parts, ignore_index=True) if index_parts else
             pd.DataFrame(columns=ADDED_COLS + ["Census2018_Industry"]))
    return index, files, value_counts


# -----------------------------------------------------------------------------
# 第 2 步：按 index 取行并 concat
# -----------------------------------------------------------------------------

def extract_by_index(index, files, out: Path, *, concat_name=CONCAT_NAME,
                     chunk_size=CHUNK_SIZE):
    """回到每个原文件按 index 取整行，写分年龄段文件，同时 concat 成总表。

    各文件列不完全一样时，总表取所有列的并集（按首次出现顺序），缺的格子留空。
    """
    union = list(dict.fromkeys(
        c for f in files for c in f["header"] if c not in ADDED_COLS))
    by_age_dir = out / "by_age"
    by_age_dir.mkdir(parents=True, exist_ok=True)
    concat_path = out / concat_name

    written = {}
    with open(concat_path, "w", newline="", encoding="utf-8-sig") as concat:
        pd.DataFrame(columns=ADDED_COLS + union).to_csv(concat, index=False)
        for f in files:
            path = f["path"]
            wanted = set(index.loc[index["source_file"] == path.name,
                                   "source_row_index"].astype(int))
            band_path = by_age_dir / f"{path.stem}_construction.csv"
            band_cols = ADDED_COLS + [c for c in f["header"] if c not in ADDED_COLS]
            n = 0
            with open(band_path, "w", newline="", encoding="utf-8-sig") as band:
                pd.DataFrame(columns=band_cols).to_csv(band, index=False)
                if wanted:
                    for chunk in read_chunks(path, f["encoding"], chunk_size):
                        hit = chunk[chunk.index.isin(wanted)]
                        if hit.empty:
                            continue
                        hit = hit.drop(columns=[c for c in ADDED_COLS if c in hit])
                        hit.insert(0, "source_row_index", hit.index)
                        hit.insert(0, "source_file", path.name)
                        hit.insert(0, "age_band", f["age_band"])
                        hit.to_csv(band, index=False, header=False)
                        hit.reindex(columns=ADDED_COLS + union).to_csv(
                            concat, index=False, header=False)
                        n += len(hit)
            if n != len(wanted):
                fail(f"{path.name}：index 里有 {len(wanted)} 行，按 index 只取回 {n} 行。"
                     "文件在两步之间被改动过？")
            written[path.name] = (band_path, n)
            print(f"第 2 步  {path.name}   取回 {n} 行 -> by_age/{band_path.name}")
    return concat_path, written


# -----------------------------------------------------------------------------

def run(input_files, input_dir, output_dir, *, industry_col="",
        construction_values=CONSTRUCTION_VALUES, id_cols=(), encoding="",
        concat_name=CONCAT_NAME, chunk_size=CHUNK_SIZE) -> dict:
    paths = resolve_inputs(input_files, input_dir)
    if not output_dir or not output_dir.strip():
        fail("输出路径还没填。请在「配置区」填 OUTPUT_DIR，或用命令行 --output-dir")
    out = Path(output_dir.strip())
    out.mkdir(parents=True, exist_ok=True)

    index, files, value_counts = build_index(
        paths, industry_col=industry_col, construction_values=construction_values,
        id_cols=id_cols, encoding=encoding, chunk_size=chunk_size)
    index.to_csv(out / "construction_index.csv", index=False, encoding="utf-8-sig")

    concat_path, written = extract_by_index(index, files, out,
                                            concat_name=concat_name,
                                            chunk_size=chunk_size)

    summary = pd.DataFrame([{
        "age_band": f["age_band"],
        "source_file": f["path"].name,
        "rows_read": f["rows"],
        "construction_cases": written[f["path"].name][1],
        "blank_industry": f["blank_industry"],
        "construction_share": (written[f["path"].name][1] / f["rows"]
                               if f["rows"] else float("nan")),
    } for f in files])
    total = {"age_band": "All", "source_file": "",
             "rows_read": int(summary["rows_read"].sum()),
             "construction_cases": int(summary["construction_cases"].sum()),
             "blank_industry": int(summary["blank_industry"].sum())}
    total["construction_share"] = (total["construction_cases"] / total["rows_read"]
                                   if total["rows_read"] else float("nan"))
    summary = pd.concat([summary, pd.DataFrame([total])], ignore_index=True)
    summary.to_csv(out / "summary_by_age.csv", index=False, encoding="utf-8-sig")

    targets = {norm_value(v) for v in construction_values}
    values = pd.DataFrame(sorted(value_counts.items(), key=lambda kv: -kv[1]),
                          columns=["Census2018_Industry", "n"])
    values["counted_as_construction"] = values["Census2018_Industry"] \
        .map(norm_value).isin(targets).astype(int)
    values.to_csv(out / "industry_values.csv", index=False, encoding="utf-8-sig")

    near = values[(values["counted_as_construction"] == 0)
                  & values["Census2018_Industry"].str.contains("construct", case=False)]
    print("\n" + summary.to_string(index=False))
    if not near.empty:
        print("\n注意：以下行业取值含 \"construct\" 但【没有】算进 construction"
              "（要算就加进 CONSTRUCTION_VALUES）：")
        for _, row in near.iterrows():
            print(f"  {row['Census2018_Industry']!r}: {row['n']}")
    if total["construction_cases"] == 0:
        print("\n警告：一个 construction case 都没有。看 industry_values.csv 里"
              "实际的写法，改 CONSTRUCTION_VALUES。")
    print(f"\n总表：{concat_path}")
    print(f"输出目录：{out}")
    return {"index": index, "summary": summary, "values": values,
            "concat_path": concat_path, "output_dir": out}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="按 Census2018_Industry 筛 construction，取 index 后 concat。"
                    "不带参数时用脚本顶部配置区的值。")
    parser.add_argument("--input", nargs="+", default=None)
    parser.add_argument("--input-dir", default=None)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--industry-col", default=None)
    parser.add_argument("--construction-values", nargs="+", default=None)
    parser.add_argument("--encoding", default=None)
    args = parser.parse_args(argv)
    run(
        args.input if args.input is not None else INPUT_FILES,
        args.input_dir if args.input_dir is not None else
        ("" if args.input is not None else INPUT_DIR),
        args.output_dir if args.output_dir is not None else OUTPUT_DIR,
        industry_col=args.industry_col or INDUSTRY_COL,
        construction_values=args.construction_values or CONSTRUCTION_VALUES,
        id_cols=ID_COLS,
        encoding=args.encoding or ENCODING,
        chunk_size=CHUNK_SIZE,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
