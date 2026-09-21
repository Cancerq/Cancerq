#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从年龄段 CSV 中筛出电工，再按行业分组。判断只看两列的文本内容：

    是电工    = Census2018_Occupation 里含有 "Electrician"
    是建筑业  = Census2018_Industry   等于 "Construction"

不做任何行业码段推断，不查码表。

输出四组：
    Construction_electrician      是电工 且 行业 = Construction
    Non_construction_electrician  是电工 且 行业 = 其他已填写的行业
    Unknown_industry_electrician  是电工 且 行业 为空 / Unknown
    All_industry_electrician      是电工（不分行业，= 上面三组之和）

All_industry 是并集，同一行会同时出现在 All 和它所属的行业组里 —— 设计如此。
每次运行都核对 All = Construction + Non_construction + Unknown_industry。

每组都产出【年龄分层】和【合并】两套结果。单文件，只依赖 pandas。

用法：把「配置区」的 INPUT_DIR 填好（OUTPUT_DIR 已预填），然后运行

    python run_electrician_split.py
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pandas as pd

# =============================================================================
# 配置区
# =============================================================================

# 【必填】输入。填法 A：目录（自动找 nvdrs_age_18_27.csv 等年龄段文件）
INPUT_DIR = r""          # 例：r"D:\School_project\Project\NVDRS\Label_year\2024\age_chunks"

# 填法 B：逐个列出（填了就忽略 INPUT_DIR）
INPUT_FILES = [
    r"",                 # 例：r"D:\...\nvdrs_age_18_27.csv"
    r"",                 # nvdrs_age_28_37.csv
    r"",                 # nvdrs_age_38_47.csv
    r"",                 # nvdrs_age_48_57.csv
    r"",                 # nvdrs_age_58_67.csv
]

# 【输出】已按你给的路径预填。目录不存在会自动创建。
OUTPUT_DIR = (
    r"D:\School_project\Project\NVDRS\Label_year\2024"
    r"\Construction_electrician vs Non-Construction_electrician vs All industry_electrician"
)

# -----------------------------------------------------------------------------
# 以下通常不用改
# -----------------------------------------------------------------------------

# 列名。留空 = 自动识别 Census2018_Occupation / Census2018_Industry
OCCUPATION_COL = r""
INDUSTRY_COL = r""

# 电工：职业列里【含有】这个词就算（不区分大小写）。
# "electrician" 能同时匹配 "Electricians"、"Electrician, apprentice" 等写法。
ELECTRICIAN_KEYWORD = "electrician"

# 建筑业：行业列【等于】这个值才算（不区分大小写、忽略首尾空格）。
# 用等于而不是包含，避免误匹配到其他含该词的行业名称。
CONSTRUCTION_VALUE = "construction"

# 行业未知（空白，或下面 UNKNOWN_INDUSTRY_VALUES 里的写法）的电工怎么处理：
#   "separate"        -> 单独写 Unknown_industry_electrician（默认）
#   "nonconstruction" -> 并入 Non_construction_electrician
# 做 construction vs non-construction 对比时，把行业未知的人塞进对照组会污染
# 对照组 —— 他们当中可能就有建筑业电工。
UNKNOWN_INDUSTRY_GOES_TO = "separate"

# 行业列里代表「未知」的写法（整格匹配，不区分大小写）
UNKNOWN_INDUSTRY_VALUES = [
    "unknown", "unk", "not available", "not specified", "not stated",
    "not reported", "unspecified", "missing", "refused", "n/a", "na", "none",
    "blank", "未知",
]

ENCODING = "utf-8"
CHUNK_SIZE = 50_000

# =============================================================================
# 配置区结束
# =============================================================================

GROUPS = (
    "All_industry_electrician",
    "Construction_electrician",
    "Non_construction_electrician",
    "Unknown_industry_electrician",
)

# 目录扫描时跳过的非样本文件
SKIP_NAME_PATTERNS = (
    re.compile(r"age_distribution", re.I),
    re.compile(r"_excluded", re.I),
    re.compile(r"electrician", re.I),
    re.compile(r"^summary", re.I),
    re.compile(r"^file_map", re.I),
    re.compile(r"breakdown", re.I),
    re.compile(r"_values\.csv$", re.I),
)

AGE_BAND_RE = re.compile(r"(\d{2})[_\-](\d{2})")

# 统计表里代表「原本是空格子」的占位符。必须和真实取值区分开，否则
# counted_as 会把空白错标成 Non_construction。
BLANK_LABEL = "(空白)"

# 这些取值视同空白
MISSING_TOKENS = {"", ".", "-", "--", "nan", "none", "null", "<na>"}

OCC_COL_CANDIDATES = (
    "census2018occupation", "census2018occ", "occupation2018",
    "censusoccupation2018", "occupationcensus2018", "occupation",
)
IND_COL_CANDIDATES = (
    "census2018industry", "censusindustry2018", "industrycensus2018",
    "census2018ind", "industry2018", "industry",
)


def norm_text(value) -> str:
    """小写、压缩空白、去首尾空格；缺失值返回空字符串。"""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = re.sub(r"\s+", " ", str(value)).strip().lower()
    return "" if text in MISSING_TOKENS else text


def norm_colname(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def band_of(path: Path) -> tuple[str, str]:
    """(显示用 '18-27', 文件名用 '18_27')"""
    m = AGE_BAND_RE.search(path.stem)
    if m:
        return f"{m.group(1)}-{m.group(2)}", f"{m.group(1)}_{m.group(2)}"
    return path.stem, path.stem


def fail(message: str) -> "NoReturn":  # noqa: F821
    print(f"\n错误：{message}\n", file=sys.stderr)
    sys.exit(2)


def find_industry_column(columns, exclude=()):
    excluded = set(exclude)
    normalised = {norm_colname(c): c for c in columns if c not in excluded}
    for cand in IND_COL_CANDIDATES:
        if cand in normalised:
            return normalised[cand]
    for cand in IND_COL_CANDIDATES:
        for norm, original in normalised.items():
            if cand in norm:
                return original
    return None


def find_occupation_column(columns, exclude=()):
    """找职业列，绝不会返回行业列。

    "Census2018_Industry" 归一化后是 "census2018industry"。若职业候选词里有较短的
    前缀，子串匹配可能把行业列当成职业列，那样两个判断都会落在同一列上。这里显式
    排除任何列名含 "industry" 的列。
    """
    excluded = set(exclude)
    normalised = {
        norm_colname(c): c
        for c in columns
        if c not in excluded and "industry" not in norm_colname(c)
    }
    for cand in OCC_COL_CANDIDATES:
        if cand in normalised:
            return normalised[cand]
    for cand in OCC_COL_CANDIDATES:
        for norm, original in normalised.items():
            if cand in norm:
                return original
    return None


def resolve_inputs(input_dir: str, input_files: list[str]) -> list[Path]:
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
        every = sorted(directory.glob("*.csv"))
        if not every:
            fail(f"INPUT_DIR 里没有 .csv 文件：\n  {directory}")
        paths, skipped = [], []
        for path in every:
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
                "请改用 INPUT_FILES 逐个列出。"
            )
    else:
        fail(
            "输入路径还没填。请打开本脚本，在「配置区」填写：\n"
            "    INPUT_DIR   = r\"...\"      （填目录）\n"
            "  或\n"
            "    INPUT_FILES = [r\"...\", ]  （逐个列出）\n"
            "也可以用命令行：--input-dir \"路径\""
        )

    seen, unique = set(), []
    for path in paths:
        key = path.resolve()
        if key in seen:
            print(f"  （重复路径，只算一次）{path.name}")
            continue
        seen.add(key)
        unique.append(path)
    return unique


def resolve_output(output_dir: str) -> Path:
    if not output_dir or not output_dir.strip():
        fail(
            "输出路径还没填。请在「配置区」填写：\n"
            "    OUTPUT_DIR = r\"...\"\n"
            "也可以用命令行：--output-dir \"路径\""
        )
    path = Path(output_dir.strip())
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        fail(f"创建输出目录失败：\n  {path}\n  {exc}")
    return path


def resolve_columns(columns, occ_override: str, ind_override: str) -> tuple[str, str]:
    if ind_override and ind_override.strip():
        industry = ind_override.strip()
        if industry not in columns:
            fail(f"INDUSTRY_COL 指定的列 {industry!r} 不在文件里。\n"
                 f"该文件的列有：{', '.join(map(str, columns))}")
    else:
        industry = find_industry_column(columns)

    if occ_override and occ_override.strip():
        occupation = occ_override.strip()
        if occupation not in columns:
            fail(f"OCCUPATION_COL 指定的列 {occupation!r} 不在文件里。\n"
                 f"该文件的列有：{', '.join(map(str, columns))}")
    else:
        occupation = find_occupation_column(
            columns, exclude=[industry] if industry else []
        )

    missing = []
    if not occupation:
        missing.append("Census2018_Occupation（职业，用来找 Electrician）")
    if not industry:
        missing.append("Census2018_Industry（行业，用来判断是否 Construction）")
    if missing:
        fail(
            "缺少必需的列：\n  - " + "\n  - ".join(missing)
            + f"\n该文件的列有：{', '.join(map(str, columns))}\n"
            "在配置区填 OCCUPATION_COL / INDUSTRY_COL 指定列名。"
        )
    if occupation == industry:
        fail(
            f"职业列和行业列解析成了同一列 {occupation!r}。"
            "请在配置区分别指定 OCCUPATION_COL 和 INDUSTRY_COL。"
        )
    return occupation, industry


class Writer:
    """按需创建的追加写入器：表头只写一次。"""

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
    occupation_col: str = "",
    industry_col: str = "",
    electrician_keyword: str = ELECTRICIAN_KEYWORD,
    construction_value: str = CONSTRUCTION_VALUE,
    unknown_industry_values=tuple(UNKNOWN_INDUSTRY_VALUES),
    unknown_industry_goes_to: str = "separate",
    encoding: str = "utf-8",
    chunk_size: int = 50_000,
) -> dict:
    if unknown_industry_goes_to not in {"separate", "nonconstruction"}:
        fail(
            "UNKNOWN_INDUSTRY_GOES_TO 只能是 \"separate\" 或 \"nonconstruction\"，"
            f"现在是 {unknown_industry_goes_to!r}"
        )
    keyword = norm_text(electrician_keyword)
    if not keyword:
        fail("ELECTRICIAN_KEYWORD 是空的")
    constr = norm_text(construction_value)
    if not constr:
        fail("CONSTRUCTION_VALUE 是空的")
    unknown_set = {norm_text(v) for v in unknown_industry_values} - {""}

    files = resolve_inputs(input_dir, input_files)
    out_dir = resolve_output(output_dir)
    separate_unknown = unknown_industry_goes_to == "separate"
    groups = [g for g in GROUPS if separate_unknown or g != "Unknown_industry_electrician"]

    print("=" * 78)
    print("输入文件：")
    for path in files:
        print(f"  [{band_of(path)[0]:>5}] {path}  ({path.stat().st_size / 1048576:,.0f} MB)")
    print(f"\n输出目录：{out_dir}")
    print("\n判断规则（只看这两列的文本内容）：")
    print(f"  是电工   ：职业列 含有 {electrician_keyword!r}（不区分大小写）")
    print(f"  是建筑业 ：行业列 等于 {construction_value!r}（不区分大小写）")
    print(f"  行业未知 ："
          f"{'单独成组' if separate_unknown else '并入 Non_construction_electrician'}")
    print("=" * 78)

    merged = {g: Writer(out_dir / f"{g}_all_ages.csv", encoding) for g in groups}
    merged_columns = None
    summary_rows: list[dict] = []
    rows_map: list[dict] = []
    industry_counts: dict[str, int] = {}
    occupation_counts: dict[str, int] = {}
    total_read = 0

    for path in files:
        band, slug = band_of(path)
        header = pd.read_csv(path, dtype=str, nrows=0, encoding=encoding)
        occ_col, ind_col = resolve_columns(header.columns, occupation_col, industry_col)

        out_columns = list(header.columns)
        for added in ("age_band", "electrician_group"):
            if added not in out_columns:
                out_columns.append(added)
        if merged_columns is None:
            merged_columns = out_columns
        elif out_columns != merged_columns:
            fail(
                f"{path.name} 的列和前面的文件不一致，合并会错位。\n"
                f"  前面的列：{merged_columns}\n  这个文件：{out_columns}"
            )

        strat = {g: Writer(out_dir / f"{g}_{slug}.csv", encoding) for g in groups}
        n_read = n_elec = 0

        for chunk in pd.read_csv(
            path, dtype=str, keep_default_na=True, encoding=encoding, chunksize=chunk_size
        ):
            n_read += len(chunk)
            occ_text = chunk[occ_col].map(norm_text)
            is_elec = occ_text.str.contains(keyword, regex=False, na=False)
            electricians = chunk.loc[is_elec]
            if electricians.empty:
                continue
            n_elec += len(electricians)

            # 记下匹配到的职业原文，方便核对关键词有没有匹配过宽
            for value, n in electricians[occ_col].fillna(BLANK_LABEL).value_counts().items():
                occupation_counts[value] = occupation_counts.get(value, 0) + int(n)
            # 记下这些电工所在行业的原文，方便核对 Construction 的写法
            for value, n in electricians[ind_col].fillna(BLANK_LABEL).value_counts().items():
                industry_counts[value] = industry_counts.get(value, 0) + int(n)

            ind_text = electricians[ind_col].map(norm_text)
            is_constr = ind_text == constr
            is_unknown = (ind_text == "") | ind_text.isin(unknown_set)

            if separate_unknown:
                buckets = {
                    "Construction_electrician": is_constr,
                    "Non_construction_electrician": ~(is_constr | is_unknown),
                    "Unknown_industry_electrician": is_unknown,
                }
            else:
                buckets = {
                    "Construction_electrician": is_constr,
                    "Non_construction_electrician": ~is_constr,
                }

            base = electricians.copy()
            base["age_band"] = band

            for group, mask in buckets.items():
                subset = base.loc[mask].copy()
                if subset.empty:
                    continue
                subset["electrician_group"] = group
                strat[group].write(subset[out_columns])
                merged[group].write(subset[out_columns])

            # All_industry 是并集，这里再写一遍同样的行是有意为之，
            # 让 All 文件能独立作为「全部电工」使用。
            allrows = base.copy()
            allrows["electrician_group"] = "All_industry_electrician"
            strat["All_industry_electrician"].write(allrows[out_columns])
            merged["All_industry_electrician"].write(allrows[out_columns])

        counts = {g: strat[g].rows for g in groups}
        for group in groups:
            strat[group].finish(out_columns)
            rows_map.append({
                "age_band": band, "group": group,
                "input_file": str(path), "output_file": str(strat[group].path),
                "rows": counts[group],
            })

        total_read += n_read
        summary_rows.append({
            "age_band": band,
            "input_file": str(path),
            "rows_read": n_read,
            "electricians_all_industry": counts["All_industry_electrician"],
            "construction": counts["Construction_electrician"],
            "non_construction": counts["Non_construction_electrician"],
            "unknown_industry": counts.get("Unknown_industry_electrician", 0),
            "occupation_column": occ_col,
            "industry_column": ind_col,
        })
        pct = n_elec / n_read * 100 if n_read else 0.0
        print(
            f"  [{band:>5}] {n_read:,} 行 -> 电工 {n_elec:,} ({pct:.3f}%)："
            f" construction {counts['Construction_electrician']:,},"
            f" 非 construction {counts['Non_construction_electrician']:,}"
            + (f", 行业未知 {counts.get('Unknown_industry_electrician', 0):,}"
               if separate_unknown else "")
        )

    for group in groups:
        merged[group].finish(merged_columns or [])
        rows_map.append({
            "age_band": "ALL", "group": group,
            "input_file": "; ".join(str(p) for p in files),
            "output_file": str(merged[group].path), "rows": merged[group].rows,
        })

    summary = pd.DataFrame(summary_rows)
    summary.to_csv(out_dir / "summary_by_age_band.csv", index=False)
    pd.DataFrame(rows_map).to_csv(out_dir / "file_map.csv", index=False)

    def bucket_of(value: str) -> str:
        if value == BLANK_LABEL:      # 占位符代表空格子 -> 行业未知
            return "Unknown" if separate_unknown else "Non_construction"
        text = norm_text(value)
        if text == constr:
            return "Construction"
        if text == "" or text in unknown_set:
            return "Unknown" if separate_unknown else "Non_construction"
        return "Non_construction"

    ind_breakdown = pd.DataFrame(
        [
            {"Census2018_Industry": v, "n_electricians": n, "counted_as": bucket_of(v)}
            for v, n in sorted(industry_counts.items(), key=lambda kv: -kv[1])
        ],
        columns=["Census2018_Industry", "n_electricians", "counted_as"],
    )
    ind_breakdown.to_csv(out_dir / "electrician_industry_values.csv", index=False)

    occ_breakdown = pd.DataFrame(
        [{"Census2018_Occupation": v, "n": n}
         for v, n in sorted(occupation_counts.items(), key=lambda kv: -kv[1])],
        columns=["Census2018_Occupation", "n"],
    )
    occ_breakdown.to_csv(out_dir / "matched_occupation_values.csv", index=False)

    # ---- 报告 --------------------------------------------------------------
    print("\n" + "=" * 78)
    print("年龄分层结果")
    print("=" * 78)
    cols = ["age_band", "rows_read", "electricians_all_industry",
            "construction", "non_construction"]
    if separate_unknown:
        cols.append("unknown_industry")
    print(summary[cols].to_string(index=False))

    print("\n" + "=" * 78)
    print("合并结果（全年龄段）")
    print("=" * 78)
    for group in groups:
        print(f"  {group:<30} {merged[group].rows:>10,} 行")
        print(f"  {'':<30} {merged[group].path}")

    n_all = merged["All_industry_electrician"].rows
    n_parts = sum(merged[g].rows for g in groups if g != "All_industry_electrician")
    print(f"\n读入总行数：{total_read:,}")
    print(f"电工总数（All_industry）：{n_all:,}")
    print(f"各行业组之和：{n_parts:,}")
    if n_all == n_parts:
        print("  （All = Construction + Non_construction"
              + (" + Unknown_industry）" if separate_unknown else "）"))
    else:
        print(f"  警告：对不上，差 {n_all - n_parts:,} 行")
    print("  注意：All_industry 是并集，同一行也出现在它所属的行业组文件里。")

    # ---- 匹配情况核对 -------------------------------------------------------
    if n_all == 0:
        occ_name = summary["occupation_column"].iloc[0] if not summary.empty else "职业列"
        print(
            f"\n  警告：一个电工都没匹配到。{occ_name!r} 里没有含 "
            f"{electrician_keyword!r} 的值。\n"
            "  如果这一列存的是数字码而不是文字，本脚本的判断方式不适用。"
        )
    else:
        print("\n" + "=" * 78)
        print(f"匹配到的职业原文（共 {len(occ_breakdown)} 种，确认关键词没匹配过宽）")
        print("=" * 78)
        print(occ_breakdown.head(20).to_string(index=False))

        print("\n" + "=" * 78)
        print(f"电工所在行业的原文取值（共 {len(ind_breakdown)} 种）")
        print("=" * 78)
        print(ind_breakdown.head(25).to_string(index=False))
        if merged["Construction_electrician"].rows == 0:
            print(
                f"\n  警告：没有任何电工的行业等于 {construction_value!r}。"
                "\n  上表就是实际出现的行业写法。如果建筑业在你的数据里写作别的文字，"
                "\n  把 CONSTRUCTION_VALUE 改成那个写法（大小写和首尾空格不影响）。"
            )

    if separate_unknown and merged["Unknown_industry_electrician"].rows:
        print(
            f"\n  提醒：{merged['Unknown_industry_electrician'].rows:,} 名电工行业未知，"
            "单独成组。把他们并进 Non_construction 会污染对照组"
            "（其中可能就有建筑业电工）。"
        )

    print("\n" + "=" * 78)
    print("输入文件 -> 输出文件 对照")
    print("=" * 78)
    for row in rows_map:
        tag = "合并" if row["age_band"] == "ALL" else row["age_band"]
        print(f"  [{tag:>5}] {row['group']:<30} {row['rows']:>8,} 行")
        if row["age_band"] != "ALL":
            print(f"          输入: {row['input_file']}")
        print(f"          输出: {row['output_file']}")

    print("\n另外写出：")
    print(f"  {out_dir / 'summary_by_age_band.csv'}")
    print(f"  {out_dir / 'file_map.csv'}")
    print(f"  {out_dir / 'electrician_industry_values.csv'}   电工所在行业的原文取值")
    print(f"  {out_dir / 'matched_occupation_values.csv'}     匹配到的职业原文")

    return {
        "input_files": files, "output_dir": out_dir,
        "summary": summary,
        "industry_values": ind_breakdown,
        "occupation_values": occ_breakdown,
        "merged_rows": {g: merged[g].rows for g in groups},
        "rows_read": total_read,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="按文本判断筛出电工并分成 Construction / Non-construction / All industry"
    )
    parser.add_argument("--input-dir", default=None)
    parser.add_argument("--input", nargs="+", default=None)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--occupation-col", default=None)
    parser.add_argument("--industry-col", default=None)
    parser.add_argument("--electrician-keyword", default=None)
    parser.add_argument("--construction-value", default=None)
    parser.add_argument(
        "--unknown-industry-goes-to", choices=["separate", "nonconstruction"], default=None
    )
    parser.add_argument("--encoding", default=None)
    parser.add_argument("--chunk-size", type=int, default=None)
    args = parser.parse_args(argv)

    pick = lambda a, b: a if a is not None else b  # noqa: E731
    result = run(
        pick(args.input_dir, INPUT_DIR),
        pick(args.input, INPUT_FILES),
        pick(args.output_dir, OUTPUT_DIR),
        occupation_col=pick(args.occupation_col, OCCUPATION_COL),
        industry_col=pick(args.industry_col, INDUSTRY_COL),
        electrician_keyword=pick(args.electrician_keyword, ELECTRICIAN_KEYWORD),
        construction_value=pick(args.construction_value, CONSTRUCTION_VALUE),
        unknown_industry_goes_to=pick(
            args.unknown_industry_goes_to, UNKNOWN_INDUSTRY_GOES_TO
        ),
        encoding=pick(args.encoding, ENCODING),
        chunk_size=pick(args.chunk_size, CHUNK_SIZE),
    )
    return 0 if result["merged_rows"].get("All_industry_electrician") else 1


if __name__ == "__main__":
    sys.exit(main())
