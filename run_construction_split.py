#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把年龄段 CSV 按 census_2018 分成 construction / 非 construction。

同时产出两套结果：
  1. 年龄分层：每个年龄段各一对文件
  2. 合并：所有年龄段合成一对文件（带 age_band 列，分层信息不丢）

用法：把下面「配置区」的路径空白填好，然后直接运行

    python run_construction_split.py

也可以不改文件，用命令行覆盖：

    python run_construction_split.py --input-dir "D:\\age_chunks" --output-dir "D:\\out"

文件是分块读、分块追加写的，1.7 GB 的输入也只占几百 MB 内存。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from census_2018 import (  # noqa: E402
    BLANK,
    CENSUS_2018_COL_CANDIDATES,
    UNPARSEABLE,
    build_construction_ranges,
    describe_ranges,
    in_ranges,
    parse_code,
)
from nvdrs_split import _norm_colname  # noqa: E402

# =============================================================================
# 配置区 —— 把路径填进下面的空白引号里
# =============================================================================

# 【必填 1】输入。两种填法，二选一即可。
#
# 填法 A：只填目录，脚本自动找里面的 nvdrs_age_18_27.csv 等年龄段文件
#         （会自动跳过 age_distribution.csv 和 nvdrs_age_excluded.csv）
INPUT_DIR = r""          # 例：r"D:\NVDRS\age_chunks"

# 填法 B：逐个列出文件的完整路径（填了这个就忽略 INPUT_DIR）
INPUT_FILES = [
    r"",                 # 例：r"D:\NVDRS\age_chunks\nvdrs_age_18_27.csv"
    r"",                 # 例：r"D:\NVDRS\age_chunks\nvdrs_age_28_37.csv"
    r"",                 # 例：r"D:\NVDRS\age_chunks\nvdrs_age_38_47.csv"
    r"",                 # 例：r"D:\NVDRS\age_chunks\nvdrs_age_48_57.csv"
    r"",                 # 例：r"D:\NVDRS\age_chunks\nvdrs_age_58_67.csv"
]

# 【必填 2】输出目录（不存在会自动创建）
OUTPUT_DIR = r""         # 例：r"D:\NVDRS\construction_out"

# -----------------------------------------------------------------------------
# 以下为可选项，通常不用改
# -----------------------------------------------------------------------------

# census_2018 列名。留空 = 自动识别
CENSUS_COL = r""

# 空白 census_2018 的行怎么处理：
#   "separate"        -> 单独写 *_blank.csv（默认，推荐）
#   "nonconstruction" -> 并入非 construction
# 空白是「不知道职业」，和「知道且不是建筑」不是一回事，合并会让非建筑组的分母
# 变大。确认你的分析要把未知也算作非建筑，再改成 "nonconstruction"。
BLANK_GOES_TO = "separate"

# 是否把采掘业（6800-6950）算作 construction
INCLUDE_EXTRACTION = False

# 是否把建筑经理（0220）算作 construction
INCLUDE_MANAGERS = False

# 输入文件编码。NVDRS 的 Windows 导出常见 "utf-8" 或 "cp1252"
ENCODING = "utf-8"

# 每次读入内存的行数。越小越省内存
CHUNK_SIZE = 50_000

# =============================================================================
# 配置区结束
# =============================================================================


# 目录扫描时要跳过的非数据文件：这些是上一步的汇总/排除文件，不是样本。
SKIP_NAME_PATTERNS = (
    re.compile(r"age_distribution", re.I),
    re.compile(r"_excluded", re.I),
    re.compile(r"_construction\.csv$", re.I),
    re.compile(r"_nonconstruction\.csv$", re.I),
    re.compile(r"_blank\.csv$", re.I),
    re.compile(r"^summary", re.I),
    re.compile(r"_list\.txt$", re.I),
)

AGE_BAND_RE = re.compile(r"(\d{2})[_\-](\d{2})")


def band_from_name(path: Path) -> str:
    match = AGE_BAND_RE.search(path.stem)
    return f"{match.group(1)}-{match.group(2)}" if match else path.stem


def band_slug(path: Path) -> str:
    match = AGE_BAND_RE.search(path.stem)
    return f"{match.group(1)}_{match.group(2)}" if match else path.stem


def fail(message: str) -> "NoReturn":  # noqa: F821
    print(f"\n错误：{message}\n", file=sys.stderr)
    sys.exit(2)


def resolve_inputs(input_dir: str, input_files: list[str]) -> list[Path]:
    """把配置区的填空解析成实际的文件列表，并说清楚哪里没填。"""
    listed = [f.strip() for f in input_files if f and f.strip()]

    if listed:
        paths = []
        for item in listed:
            path = Path(item)
            if not path.is_file():
                fail(f"INPUT_FILES 里这个路径找不到：\n  {path}")
            paths.append(path)
    elif input_dir and input_dir.strip():
        directory = Path(input_dir.strip())
        if not directory.is_dir():
            fail(f"INPUT_DIR 不是一个目录：\n  {directory}")
        everything = sorted(directory.glob("*.csv"))
        if not everything:
            fail(f"INPUT_DIR 里没有 .csv 文件：\n  {directory}")
        paths, skipped = [], []
        for path in everything:
            if any(p.search(path.name) for p in SKIP_NAME_PATTERNS):
                skipped.append(path.name)
            else:
                paths.append(path)
        if skipped:
            print("已跳过以下非样本文件（汇总表 / 排除行 / 本脚本的输出）：")
            for name in skipped:
                print(f"  - {name}")
        if not paths:
            fail(
                f"目录里的 .csv 全部被当作非样本文件跳过了：\n  {directory}\n"
                "如果确实要处理它们，请改用 INPUT_FILES 逐个列出。"
            )
    else:
        fail(
            "输入路径还没填。请打开本脚本，在「配置区」填写：\n"
            "    INPUT_DIR   = r\"...\"      （填目录，自动找年龄段文件）\n"
            "  或\n"
            "    INPUT_FILES = [r\"...\", ]  （逐个列出文件）\n"
            "也可以用命令行：--input-dir \"路径\"  或  --input 文件1 文件2 ..."
        )

    seen, unique = set(), []
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            print(f"  （重复路径，只算一次）{path.name}")
            continue
        seen.add(resolved)
        unique.append(path)
    return unique


def resolve_output(output_dir: str) -> Path:
    if not output_dir or not output_dir.strip():
        fail(
            "输出路径还没填。请打开本脚本，在「配置区」填写：\n"
            "    OUTPUT_DIR = r\"...\"\n"
            "也可以用命令行：--output-dir \"路径\""
        )
    path = Path(output_dir.strip())
    path.mkdir(parents=True, exist_ok=True)
    return path


def find_census_column(columns, override: str = "") -> str:
    if override and override.strip():
        name = override.strip()
        if name not in columns:
            fail(
                f"CENSUS_COL 指定的列 {name!r} 不在文件里。\n"
                f"该文件的列有：{', '.join(map(str, columns))}"
            )
        return name

    normalised = {_norm_colname(c): c for c in columns}
    for cand in CENSUS_2018_COL_CANDIDATES:
        if cand in normalised:
            return normalised[cand]
    for cand in CENSUS_2018_COL_CANDIDATES:
        for norm, original in normalised.items():
            if cand in norm:
                return original

    wrong_vintage = [
        original for norm, original in normalised.items()
        if "census" in norm and "2018" not in norm
    ]
    extra = ""
    if wrong_vintage:
        extra = (
            f"\n注意：找到了其他年份的 census 列（{', '.join(wrong_vintage)}），"
            "但 2010 和 2018 码表不通用，不能替代。"
        )
    fail(
        "找不到 census_2018 列。"
        f"{extra}\n请在配置区填 CENSUS_COL = r\"你的列名\"。"
    )


class Writer:
    """按需创建的追加写入器：表头只写一次。"""

    def __init__(self, path: Path, encoding: str):
        self.path = path
        self.encoding = encoding
        self.handle = None
        self.rows = 0

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
        """没写过任何行也要留一个带表头的空文件，后续步骤不会因缺文件而崩。"""
        if self.handle is None:
            pd.DataFrame(columns=columns).to_csv(
                self.path, index=False, encoding=self.encoding
            )
        else:
            self.handle.close()
            self.handle = None


def run(
    input_dir: str,
    input_files: list[str],
    output_dir: str,
    *,
    census_col: str = "",
    blank_goes_to: str = "separate",
    include_extraction: bool = False,
    include_managers: bool = False,
    encoding: str = "utf-8",
    chunk_size: int = 50_000,
) -> dict:
    if blank_goes_to not in {"separate", "nonconstruction"}:
        fail(
            f"BLANK_GOES_TO 只能是 \"separate\" 或 \"nonconstruction\"，"
            f"现在是 {blank_goes_to!r}"
        )

    files = resolve_inputs(input_dir, input_files)
    out_dir = resolve_output(output_dir)
    ranges = build_construction_ranges(include_extraction, include_managers)
    separate_blank = blank_goes_to == "separate"

    print("=" * 78)
    print("输入文件：")
    for path in files:
        size_mb = path.stat().st_size / 1024 / 1024
        print(f"  [{band_from_name(path):>5}] {path}  ({size_mb:,.0f} MB)")
    print(f"\n输出目录：{out_dir}")
    print(f"\nconstruction 码段（Census 2018）：{describe_ranges(ranges)}")
    print(f"  采掘业 6800-6950 : {'计入' if include_extraction else '不计入'}")
    print(f"  建筑经理 0220    : {'计入' if include_managers else '不计入'}")
    print(f"  空白 census_2018 : {'单独成文件' if separate_blank else '并入非 construction'}")
    print("=" * 78)

    groups = ["construction", "nonconstruction"] + (["blank"] if separate_blank else [])

    merged = {
        g: Writer(out_dir / f"all_ages_{g}.csv", encoding) for g in groups
    }
    merged_columns = None
    rows_map: list[dict] = []
    summary_rows: list[dict] = []
    code_counts: dict[object, int] = {}

    for path in files:
        band = band_from_name(path)
        slug = band_slug(path)
        header = pd.read_csv(path, dtype=str, nrows=0, encoding=encoding)
        column = find_census_column(header.columns, census_col)

        out_columns = list(header.columns)
        for added in ("age_band", "occupation_group"):
            if added not in out_columns:
                out_columns.append(added)
        if merged_columns is None:
            merged_columns = out_columns
        elif out_columns != merged_columns:
            fail(
                f"{path.name} 的列和前面的文件不一致，合并会错位。\n"
                f"  前面的列：{merged_columns}\n"
                f"  这个文件：{out_columns}"
            )

        strat = {g: Writer(out_dir / f"nvdrs_age_{slug}_{g}.csv", encoding) for g in groups}
        n_read = n_bad = 0

        reader = pd.read_csv(
            path, dtype=str, keep_default_na=True, encoding=encoding, chunksize=chunk_size
        )
        for chunk in reader:
            codes = chunk[column].map(parse_code)
            for value, n in codes.value_counts().items():
                code_counts[value] = code_counts.get(value, 0) + int(n)

            is_constr = codes.map(lambda c: isinstance(c, int) and in_ranges(c, ranges))
            is_blank = codes == BLANK
            n_read += len(chunk)
            n_bad += int((codes == UNPARSEABLE).sum())

            if separate_blank:
                masks = {
                    "construction": is_constr,
                    "blank": is_blank,
                    "nonconstruction": ~(is_constr | is_blank),
                }
            else:
                masks = {
                    "construction": is_constr,
                    "nonconstruction": ~is_constr,
                }

            for group, mask in masks.items():
                subset = chunk.loc[mask]
                if subset.empty:
                    continue
                subset = subset.copy()
                subset["age_band"] = band
                subset["occupation_group"] = group
                subset = subset[out_columns]
                strat[group].write(subset)
                merged[group].write(subset)

        counts = {g: strat[g].rows for g in groups}
        for group in groups:
            strat[group].finish(out_columns)
            rows_map.append(
                {
                    "age_band": band,
                    "occupation_group": group,
                    "input_file": str(path),
                    "output_file": str(strat[group].path),
                    "rows": counts[group],
                }
            )

        summary_rows.append(
            {
                "age_band": band,
                "input_file": str(path),
                "rows_read": n_read,
                "construction": counts["construction"],
                "nonconstruction": counts["nonconstruction"],
                "blank": counts.get("blank", 0),
                "unparseable_code": n_bad,
                "census_column": column,
            }
        )
        pct = counts["construction"] / n_read * 100 if n_read else 0.0
        print(
            f"  [{band:>5}] {n_read:,} 行 -> construction {counts['construction']:,}"
            f" ({pct:.2f}%), 非 construction {counts['nonconstruction']:,}"
            + (f", 空白 {counts['blank']:,}" if separate_blank else "")
        )

    for group in groups:
        merged[group].finish(merged_columns or [])
        rows_map.append(
            {
                "age_band": "ALL",
                "occupation_group": group,
                "input_file": "; ".join(str(p) for p in files),
                "output_file": str(merged[group].path),
                "rows": merged[group].rows,
            }
        )

    summary = pd.DataFrame(summary_rows)
    summary.to_csv(out_dir / "summary_by_age_band.csv", index=False)
    file_map = pd.DataFrame(rows_map)
    file_map.to_csv(out_dir / "file_map.csv", index=False)

    breakdown = pd.DataFrame(
        [
            {
                "census_2018": str(v),
                "n": n,
                "is_construction": isinstance(v, int) and in_ranges(v, ranges),
            }
            for v, n in sorted(code_counts.items(), key=lambda kv: str(kv[0]))
        ]
    )
    breakdown.to_csv(out_dir / "census_2018_breakdown.csv", index=False)

    # ---- 报告 --------------------------------------------------------------
    print("\n" + "=" * 78)
    print("年龄分层结果")
    print("=" * 78)
    show = ["age_band", "rows_read", "construction", "nonconstruction"]
    if separate_blank:
        show.append("blank")
    print(summary[show].to_string(index=False))

    print("\n" + "=" * 78)
    print("合并结果（全年龄段）")
    print("=" * 78)
    for group in groups:
        print(f"  {group:<16} {merged[group].rows:>12,} 行   {merged[group].path}")

    total_read = int(summary["rows_read"].sum()) if not summary.empty else 0
    total_out = sum(merged[g].rows for g in groups)
    print(f"\n读入总行数：{total_read:,}")
    print(f"写出总行数：{total_out:,}")
    if total_read != total_out:
        print(f"  警告：数量对不上，差 {total_read - total_out:,} 行")
    else:
        print("  （读入 = 写出，没有行丢失或重复）")
    if not separate_blank:
        n_blank = int(breakdown.loc[breakdown["census_2018"] == BLANK, "n"].sum())
        if n_blank:
            print(
                f"\n  提醒：{n_blank:,} 行的 census_2018 为空白，已按设置并入"
                "非 construction。这些是「职业未知」而非「已知不是建筑」。"
            )

    print("\n" + "=" * 78)
    print("输入文件 -> 输出文件 对照")
    print("=" * 78)
    for row in rows_map:
        tag = "合并" if row["age_band"] == "ALL" else row["age_band"]
        print(f"  [{tag:>5}] {row['occupation_group']:<16} {row['rows']:>12,} 行")
        if row["age_band"] != "ALL":
            print(f"          输入: {row['input_file']}")
        print(f"          输出: {row['output_file']}")

    print(f"\n另外写出：")
    print(f"  {out_dir / 'summary_by_age_band.csv'}   各年龄段计数")
    print(f"  {out_dir / 'file_map.csv'}              输入->输出 路径对照表")
    print(f"  {out_dir / 'census_2018_breakdown.csv'} 逐个码的行数")

    return {
        "input_files": files,
        "output_dir": out_dir,
        "summary": summary,
        "file_map": file_map,
        "breakdown": breakdown,
        "merged_rows": {g: merged[g].rows for g in groups},
        "rows_read": total_read,
        "rows_written": total_out,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="按 census_2018 分出 construction / 非 construction，"
                    "同时产出年龄分层和合并两套结果",
    )
    parser.add_argument("--input-dir", default=None, help="覆盖配置区的 INPUT_DIR")
    parser.add_argument("--input", nargs="+", default=None, help="覆盖配置区的 INPUT_FILES")
    parser.add_argument("--output-dir", default=None, help="覆盖配置区的 OUTPUT_DIR")
    parser.add_argument("--census-col", default=None, help="覆盖配置区的 CENSUS_COL")
    parser.add_argument(
        "--blank-goes-to", choices=["separate", "nonconstruction"], default=None
    )
    parser.add_argument("--include-extraction", action="store_true", default=None)
    parser.add_argument("--include-managers", action="store_true", default=None)
    parser.add_argument("--encoding", default=None)
    parser.add_argument("--chunk-size", type=int, default=None)
    args = parser.parse_args(argv)

    result = run(
        args.input_dir if args.input_dir is not None else INPUT_DIR,
        args.input if args.input is not None else INPUT_FILES,
        args.output_dir if args.output_dir is not None else OUTPUT_DIR,
        census_col=args.census_col if args.census_col is not None else CENSUS_COL,
        blank_goes_to=(
            args.blank_goes_to if args.blank_goes_to is not None else BLANK_GOES_TO
        ),
        include_extraction=(
            args.include_extraction
            if args.include_extraction is not None
            else INCLUDE_EXTRACTION
        ),
        include_managers=(
            args.include_managers
            if args.include_managers is not None
            else INCLUDE_MANAGERS
        ),
        encoding=args.encoding if args.encoding is not None else ENCODING,
        chunk_size=args.chunk_size if args.chunk_size is not None else CHUNK_SIZE,
    )
    return 0 if result["merged_rows"].get("construction") else 1


if __name__ == "__main__":
    sys.exit(main())
