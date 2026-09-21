#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从年龄段 CSV 中筛出电工，再按行业交叉分成三组。

  Construction_electrician      职业=电工 且 行业=Construction(0770)
  Non_construction_electrician  职业=电工 且 行业=其他已知行业
  All_industry_electrician      职业=电工（不分行业，= 上面各组之和）

电工是【职业】码 Census2018_Occupation = 6330；
Construction 是【行业】码 Census2018_Industry = 0770。两列缺一不可。

注意 All_industry_electrician 是并集，同一行会同时出现在 All 文件和它所属的
行业组文件里 —— 这是设计如此，不是重复写入。脚本会明确核对：
    All = Construction + Non_construction + Unknown_industry

每组都产出【年龄分层】和【合并】两套结果。

用法：把下面「配置区」的 INPUT_DIR 填好（OUTPUT_DIR 已预填），然后运行

    python run_electrician_split.py
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pandas as pd

# =============================================================================
# 单文件自带：Census 2018 码表与解析工具（无需其他 .py 文件，只依赖 pandas）
#
# 这一段原本是 census_2018.py / census_2018_industry.py 两个模块，内联进来是为了
# 让本脚本可以单独拷到任何机器上直接运行。仓库里的测试会核对这里的码段与那两个
# 模块完全一致，所以不会出现两处定义不同步的情况。
# =============================================================================

# --- 职业码（Census2018_Occupation）---------------------------------------
ELECTRICIANS = 6330                       # Electricians
OCC_COL_CANDIDATES = (
    "census2018occupation", "census2018occ", "census2018",
    "censusoccupation2018", "occupationcensus2018", "occ2018",
)
OCCUPATION_TITLES = {
    6200: "First-line supervisors of construction trades and extraction workers",
    6230: "Carpenters",
    6260: "Construction laborers",
    6320: "Drywall installers, ceiling tile installers, and tapers",
    6330: "Electricians",
    6441: "Plumbers, pipefitters, and steamfitters",
    6515: "Roofers",
    6540: "Solar photovoltaic installers",
    6600: "Helpers, construction trades",
    6765: "Other construction and related workers",
}

# --- 行业码（Census2018_Industry）------------------------------------------
# 行业码表里 Construction 是【单个码】0770，不是区间。采矿是另一个大类。
CONSTRUCTION_INDUSTRY = (770, 770)
MINING_INDUSTRY = (370, 490)
IND_COL_CANDIDATES = (
    "census2018industry", "censusindustry2018", "industrycensus2018",
    "census2018ind", "industry2018", "ind2018",
)
# 行业大类名称，仅用于 breakdown 显示。筛选只依据上面的数值码段，
# 所以名称不全也绝不会改变你拿到的行。
INDUSTRY_SECTORS = [
    ((170, 290), "Agriculture, forestry, fishing and hunting"),
    ((370, 490), "Mining, quarrying, and oil and gas extraction"),
    ((570, 690), "Utilities"),
    ((770, 770), "Construction"),
    ((1070, 3990), "Manufacturing"),
    ((4070, 4590), "Wholesale trade"),
    ((4670, 5790), "Retail trade"),
    ((6070, 6390), "Transportation and warehousing"),
    ((6470, 6780), "Information"),
    ((6870, 6992), "Finance and insurance"),
    ((7071, 7190), "Real estate and rental and leasing"),
    ((7270, 7490), "Professional, scientific, and technical services"),
    ((7570, 7570), "Management of companies and enterprises"),
    ((7580, 7790), "Administrative, support and waste management services"),
    ((7860, 7890), "Educational services"),
    ((7970, 8470), "Health care and social assistance"),
    ((8561, 8590), "Arts, entertainment, and recreation"),
    ((8660, 8690), "Accommodation and food services"),
    ((8770, 9290), "Other services, except public administration"),
    ((9370, 9590), "Public administration"),
    ((9670, 9870), "Military"),
    ((9920, 9920), "Unemployed, with no work experience or never worked"),
]

BLANK = "BLANK"
UNPARSEABLE = "UNPARSEABLE"
MISSING_TOKENS = {"", ".", "-", "--", "nan", "none", "null", "<na>"}


def norm_value(value) -> str:
    """小写、压缩空白；缺失值返回空字符串。"""
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


def parse_code(value) -> "int | str":
    """把码值解析成整数，或 BLANK / UNPARSEABLE。

    接受 "6330"、"06330"、"6330.0"、6330。非数字一律不猜，返回 UNPARSEABLE。
    """
    text = norm_value(value)
    if not text:
        return BLANK
    match = re.fullmatch(r"(\d{1,5})(?:\.0+)?", text)
    return int(match.group(1)) if match else UNPARSEABLE


def in_ranges(code: int, ranges) -> bool:
    return any(low <= code <= high for low, high in ranges)


def build_construction_ranges(include_mining: bool = False):
    ranges = [CONSTRUCTION_INDUSTRY]
    if include_mining:
        ranges.append(MINING_INDUSTRY)
    return sorted(ranges)


def describe_ranges(ranges) -> str:
    return ", ".join(
        f"{low:04d}" if low == high else f"{low:04d}-{high:04d}" for low, high in ranges
    )


def sector_of(code) -> str:
    if not isinstance(code, int):
        return ""
    for (low, high), name in INDUSTRY_SECTORS:
        if low <= code <= high:
            return name
    return ""


def occupation_title(code) -> str:
    return OCCUPATION_TITLES.get(code, "") if isinstance(code, int) else ""


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
    """找职业码列，绝不会返回行业列。

    "Census2018_Industry" 归一化后是 "census2018industry"，【包含】职业列的候选词
    "census2018"。没有这道防护，子串匹配会把行业列当成职业列，导致每个码都对着
    错误的码表解释。
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

# 电工的 Census 2018 职业码。6330 = Electricians。
# 注意这三类【不在】默认范围内，需要时自行加入：
#   6600  Helpers, construction trades（电工帮工并入了这个总类，无法单独拆出）
#   电力线路安装维修工 / 电气电子维修工 属于 Installation, Maintenance and Repair
#   大类（7000-7640），不是 Electricians
ELECTRICIAN_CODES = [ELECTRICIANS]   # 6330

# 行业未知（码为空或读不出）的电工怎么处理：
#   "separate"        -> 单独写 Unknown_industry_electrician（默认）
#   "nonconstruction" -> 并入 Non_construction_electrician
# 做 construction vs non-construction 对比时，把行业未知的人塞进对照组会污染
# 对照组 —— 他们当中可能就有建筑业电工。确认要合并再改。
UNKNOWN_INDUSTRY_GOES_TO = "separate"

# 是否把采矿业（0370-0490）也算作 construction 行业
INCLUDE_MINING = False

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

SKIP_NAME_PATTERNS = (
    re.compile(r"age_distribution", re.I),
    re.compile(r"_excluded", re.I),
    re.compile(r"electrician", re.I),
    re.compile(r"^summary", re.I),
    re.compile(r"^file_map", re.I),
    re.compile(r"breakdown", re.I),
)

AGE_BAND_RE = re.compile(r"(\d{2})[_\-](\d{2})")


def band_of(path: Path) -> tuple[str, str]:
    """(显示用 '18-27', 文件名用 '18_27')"""
    m = AGE_BAND_RE.search(path.stem)
    return (f"{m.group(1)}-{m.group(2)}", f"{m.group(1)}_{m.group(2)}") if m else (
        path.stem, path.stem
    )


def fail(message: str) -> "NoReturn":  # noqa: F821
    print(f"\n错误：{message}\n", file=sys.stderr)
    sys.exit(2)


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
    """Locate the occupation and industry columns, each claimed once.

    Industry is resolved first and excluded from the occupation search: the
    normalised name "census2018industry" contains the occupation candidate
    "census2018", so without that the industry column could be read as
    occupation and every code checked against the wrong list.
    """
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
        missing.append("Census2018_Occupation（职业码，用来找电工 6330）")
    if not industry:
        missing.append("Census2018_Industry（行业码，用来判断是否 Construction 0770）")
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
    electrician_codes=(6330,),
    unknown_industry_goes_to: str = "separate",
    include_mining: bool = False,
    encoding: str = "utf-8",
    chunk_size: int = 50_000,
) -> dict:
    if unknown_industry_goes_to not in {"separate", "nonconstruction"}:
        fail(
            "UNKNOWN_INDUSTRY_GOES_TO 只能是 \"separate\" 或 \"nonconstruction\"，"
            f"现在是 {unknown_industry_goes_to!r}"
        )
    elec = {int(c) for c in electrician_codes}
    if not elec:
        fail("ELECTRICIAN_CODES 是空的，至少要有一个码（默认 6330）")

    files = resolve_inputs(input_dir, input_files)
    out_dir = resolve_output(output_dir)
    constr_ranges = build_construction_ranges(include_mining)
    separate_unknown = unknown_industry_goes_to == "separate"
    groups = [g for g in GROUPS if separate_unknown or g != "Unknown_industry_electrician"]

    print("=" * 78)
    print("输入文件：")
    for path in files:
        print(f"  [{band_of(path)[0]:>5}] {path}  ({path.stat().st_size / 1048576:,.0f} MB)")
    print(f"\n输出目录：{out_dir}")
    print(f"\n电工（职业码 Census2018_Occupation）：{sorted(elec)}"
          f"  -> {', '.join(occupation_title(c) or '?' for c in sorted(elec))}")
    print(f"Construction（行业码 Census2018_Industry）：{describe_ranges(constr_ranges)}")
    print(f"  采矿 0370-0490 : {'计入' if include_mining else '不计入'}")
    print(f"  行业未知的电工 : "
          f"{'单独成组' if separate_unknown else '并入 Non_construction_electrician'}")
    print("=" * 78)

    merged = {g: Writer(out_dir / f"{g}_all_ages.csv", encoding) for g in groups}
    merged_columns = None
    summary_rows: list[dict] = []
    rows_map: list[dict] = []
    industry_counts: dict[object, int] = {}
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
            occ_codes = chunk[occ_col].map(parse_code)
            is_elec = occ_codes.map(lambda c: isinstance(c, int) and c in elec)
            electricians = chunk.loc[is_elec]
            if electricians.empty:
                continue
            n_elec += len(electricians)

            ind_codes = electricians[ind_col].map(parse_code)
            for value, n in ind_codes.value_counts().items():
                industry_counts[value] = industry_counts.get(value, 0) + int(n)

            is_constr = ind_codes.map(
                lambda c: isinstance(c, int) and in_ranges(c, constr_ranges)
            )
            is_unknown = ind_codes.isin([BLANK, UNPARSEABLE])

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
                subset = subset[out_columns]
                strat[group].write(subset)
                merged[group].write(subset)

            # All_industry is the union; the same rows are written again here
            # on purpose, so the file stands alone as "every electrician".
            allrows = base.copy()
            allrows["electrician_group"] = "All_industry_electrician"
            allrows = allrows[out_columns]
            strat["All_industry_electrician"].write(allrows)
            merged["All_industry_electrician"].write(allrows)

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
    file_map = pd.DataFrame(rows_map)
    file_map.to_csv(out_dir / "file_map.csv", index=False)

    breakdown = pd.DataFrame([
        {
            "Census2018_Industry": str(v),
            "sector": sector_of(v),
            "n_electricians": n,
            "is_construction": isinstance(v, int) and in_ranges(v, constr_ranges),
        }
        for v, n in sorted(industry_counts.items(), key=lambda kv: str(kv[0]))
    ])
    breakdown.to_csv(out_dir / "electrician_industry_breakdown.csv", index=False)

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
    n_parts = sum(
        merged[g].rows for g in groups if g != "All_industry_electrician"
    )
    print(f"\n读入总行数：{total_read:,}")
    print(f"电工总数（All_industry）：{n_all:,}")
    print(f"各行业组之和：{n_parts:,}")
    if n_all == n_parts:
        print("  （All = Construction + Non_construction"
              + (" + Unknown_industry）" if separate_unknown else "）"))
    else:
        print(f"  警告：对不上，差 {n_all - n_parts:,} 行")
    print("  注意：All_industry 是并集，同一行也出现在它所属的行业组文件里。")

    if separate_unknown:
        n_unknown = merged["Unknown_industry_electrician"].rows
        if n_unknown:
            print(
                f"\n  提醒：{n_unknown:,} 名电工的行业码为空或读不出，单独成组。"
                "把他们并进 Non_construction 会污染对照组（其中可能就有建筑业电工）。"
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
    print(f"  {out_dir / 'electrician_industry_breakdown.csv'}")

    return {
        "input_files": files, "output_dir": out_dir,
        "summary": summary, "file_map": file_map, "breakdown": breakdown,
        "merged_rows": {g: merged[g].rows for g in groups},
        "rows_read": total_read,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="筛出电工并按行业分成 Construction / Non-construction / All industry"
    )
    parser.add_argument("--input-dir", default=None)
    parser.add_argument("--input", nargs="+", default=None)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--occupation-col", default=None)
    parser.add_argument("--industry-col", default=None)
    parser.add_argument("--electrician-codes", nargs="+", type=int, default=None)
    parser.add_argument(
        "--unknown-industry-goes-to", choices=["separate", "nonconstruction"], default=None
    )
    parser.add_argument("--include-mining", action="store_true", default=None)
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
        electrician_codes=pick(args.electrician_codes, ELECTRICIAN_CODES),
        unknown_industry_goes_to=pick(
            args.unknown_industry_goes_to, UNKNOWN_INDUSTRY_GOES_TO
        ),
        include_mining=pick(args.include_mining, INCLUDE_MINING),
        encoding=pick(args.encoding, ENCODING),
        chunk_size=pick(args.chunk_size, CHUNK_SIZE),
    )
    return 0 if result["merged_rows"].get("All_industry_electrician") else 1


if __name__ == "__main__":
    sys.exit(main())
