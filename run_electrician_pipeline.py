#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从原始 NVDRS 大文件一遍扫完：年龄分段 + 年份筛选 + 电工三分组。

不写中间文件 —— 2 GB 的源文件只读一遍，直接产出最终结果。
最终只保留电工，所以输出文件都很小。

四个判断，全部只看列的内容：

    年龄段  = Age 列的数字落在 18-30 / 31-40 / 41-50 / 51-60 / 61-70
    年份    = IncidentYear 列在 2018-2024
    是电工  = Census2018_Occupation 里【出现】 "Electrician"
    是建筑  = Census2018_Industry   【等于】   "Construction"

输出四组：

    Construction_electrician      是电工 且 行业 = Construction
    Non_construction_electrician  是电工 且 行业 = 其他已填写的行业
    Unknown_industry_electrician  是电工 且 行业为空 / Unknown
    All_industry_electrician      是电工（= 上面三组之和）

目录结构：

    OUTPUT_DIR/
      All_year/                              跨全部年份（最终总表）
        <组>_All_year.csv                    全年龄
        <组>_All_year_age_18-30.csv ...      分年龄段
      by_year/2018/ ... by_year/2024/
        <组>_2018.csv                        该年全年龄
      summary_by_year_and_age.csv            年份 × 年龄段 × 组 计数
      funnel.csv                             逐级筛选的行数交代
      file_map.csv                           输出文件 -> 行数 / 完整路径
      electrician_industry_values.csv        电工所在行业的原文取值
      matched_occupation_values.csv          匹配到的职业原文

每个输出行都带 age_band / incident_year / electrician_group 三列，
所以从 All_year 总表随时能自己重新切分。

单文件，只依赖 pandas。

用法：把「配置区」的 INPUT_FILES / OUTPUT_DIR 填好，然后运行

    python run_electrician_pipeline.py
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

import pandas as pd

# =============================================================================
# 配置区 —— 把路径填进下面的空白引号里
# =============================================================================

# 【必填 1】输入。原始大文件，逐个列出（可以只有一个）
INPUT_FILES = [
    r"",     # 例：r"D:\School_project\Project\NVDRS\Liu_1191_nvdrs_2024.csv"
    r"",
]

# 或者填一个目录，读里面所有 .csv（填了 INPUT_FILES 就忽略这个）
INPUT_DIR = r""

# 【必填 2】输出目录（不存在会自动创建）
OUTPUT_DIR = r""     # 例：r"D:\School_project\Project\NVDRS\Electricians_18_70_2018_2024"

# -----------------------------------------------------------------------------
# 以下通常不用改
# -----------------------------------------------------------------------------

# 年龄分段（闭区间，含两端）
AGE_BANDS = [(18, 30), (31, 40), (41, 50), (51, 60), (61, 70)]

# 要保留的年份
YEARS = [2018, 2019, 2020, 2021, 2022, 2023, 2024]

# 列名。全部留空 = 自动识别
AGE_COL = r""
YEAR_COL = r""
OCCUPATION_COL = r""
INDUSTRY_COL = r""

# 是电工：职业列里【出现】这个词（不区分大小写）
ELECTRICIAN_KEYWORD = "electrician"

# 是建筑业：行业列【等于】这个值（不区分大小写、忽略首尾空格）
CONSTRUCTION_VALUE = "construction"

# 行业未知的电工：
#   "separate"        -> 单独成组（默认）
#   "nonconstruction" -> 并入 Non_construction_electrician
UNKNOWN_INDUSTRY_GOES_TO = "separate"

UNKNOWN_INDUSTRY_VALUES = [
    "unknown", "unk", "not available", "not specified", "not stated",
    "not reported", "unspecified", "missing", "refused", "n/a", "na", "none",
    "blank", "未知",
]

# 是否额外输出「每年 × 每个年龄段」的行级文件（7×5×4 = 140 个小文件）。
# 关掉也能从 summary 看到这些格子的计数，或从 All_year 总表自己切。
WRITE_PER_YEAR_PER_BAND = False

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
    re.compile(r"electrician", re.I),
    re.compile(r"^summary", re.I),
    re.compile(r"^file_map", re.I),
    re.compile(r"^funnel", re.I),
    re.compile(r"_values\.csv$", re.I),
    re.compile(r"distribution", re.I),
)

BLANK_LABEL = "(空白)"
MISSING_TOKENS = {"", ".", "-", "--", "nan", "none", "null", "<na>"}

AGE_COL_CANDIDATES = ("age", "agec", "ageyears", "victimage", "ageatdeath")
CATEGORICAL_AGE_COLS = {"agegroup", "agerange", "agecategory", "ageband", "agegrp"}
YEAR_COL_CANDIDATES = ("incidentyear", "incyear", "yearofincident")
WRONG_YEAR_COLS = {
    "deathyear": "死亡年份", "yearofdeath": "死亡年份",
    "injuryyear": "受伤年份", "yearofinjury": "受伤年份",
    "birthyear": "出生年份", "filingyear": "归档年份", "reportyear": "报告年份",
}
OCC_COL_CANDIDATES = (
    "census2018occupation", "census2018occ", "occupation2018",
    "censusoccupation2018", "occupationcensus2018", "occupation",
)
IND_COL_CANDIDATES = (
    "census2018industry", "censusindustry2018", "industrycensus2018",
    "census2018ind", "industry2018", "industry",
)


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


def norm_colname(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def parse_int(value, max_digits: int = 4):
    """纯数字取整；"45.0" 也接受。非数字返回 None（绝不猜）。"""
    text = norm_text(value)
    if not text:
        return None
    m = re.fullmatch(rf"(\d{{1,{max_digits}}})(?:\.0+)?", text)
    return int(m.group(1)) if m else None


def parse_year(value):
    """4 位年份；也从 "2024-05-13" 这类日期里取。两位数年份不猜。"""
    text = norm_text(value)
    if not text:
        return None
    bare = re.fullmatch(r"(\d{4})(?:\.0+)?", text)
    if bare:
        return int(bare.group(1))
    years = re.findall(r"(?<!\d)(1[89]\d{2}|20\d{2})(?!\d)", text)
    return int(years[0]) if len(set(years)) == 1 else None


def fail(message: str) -> "NoReturn":  # noqa: F821
    print(f"\n错误：{message}\n", file=sys.stderr)
    sys.exit(2)


def _find(columns, candidates, exclude=(), forbid=None, skip_norms=()):
    excluded = set(exclude)
    normalised = {
        norm_colname(c): c
        for c in columns
        if c not in excluded
        and (forbid is None or forbid not in norm_colname(c))
        and norm_colname(c) not in skip_norms
    }
    for cand in candidates:
        if cand in normalised:
            return normalised[cand]
    for cand in candidates:
        for norm, original in normalised.items():
            if cand in norm:
                return original
    return None


def resolve_columns(columns, overrides: dict) -> dict:
    cols = {}

    cols["industry"] = overrides.get("industry") or _find(columns, IND_COL_CANDIDATES)
    # 职业列排除任何含 "industry" 的列名："census2018industry" 里含
    # "census2018"，不挡住的话会被当成职业列。
    cols["occupation"] = overrides.get("occupation") or _find(
        columns, OCC_COL_CANDIDATES,
        exclude=[cols["industry"]] if cols["industry"] else [], forbid="industry",
    )
    cols["age"] = overrides.get("age") or _find(
        columns, AGE_COL_CANDIDATES, skip_norms=CATEGORICAL_AGE_COLS
    )
    cols["year"] = overrides.get("year") or _find(
        columns, YEAR_COL_CANDIDATES, skip_norms=set(WRONG_YEAR_COLS)
    )

    for role, value in cols.items():
        if value is not None and value not in columns:
            fail(f"指定的{role}列 {value!r} 不在文件里。\n"
                 f"该文件的列有：{', '.join(map(str, columns))}")

    labels = {
        "age": "Age（数值年龄，用来分年龄段）",
        "year": "IncidentYear（用来筛 2018-2024）",
        "occupation": "Census2018_Occupation（用来找 Electrician）",
        "industry": "Census2018_Industry（用来判断 Construction）",
    }
    missing = [labels[r] for r in ("age", "year", "occupation", "industry")
               if not cols[r]]
    if missing:
        hint = ""
        normalised = {norm_colname(c): c for c in columns}
        wrong_year = [o for n, o in normalised.items() if n in WRONG_YEAR_COLS]
        if not cols["year"] and wrong_year:
            hint += ("\n注意：找到了 " + "、".join(wrong_year)
                     + " —— 这些不是 incident year（跨年案例中会不同），不能顶替。")
        cat_age = [o for n, o in normalised.items() if n in CATEGORICAL_AGE_COLS]
        if not cols["age"] and cat_age:
            hint += ("\n注意：找到了 " + "、".join(cat_age)
                     + " —— 这是预先分好的年龄类别，不是数值年龄。")
        fail("缺少必需的列：\n  - " + "\n  - ".join(missing) + hint
             + f"\n该文件的列有：{', '.join(map(str, columns))}\n"
             "在配置区填 AGE_COL / YEAR_COL / OCCUPATION_COL / INDUSTRY_COL。")

    if len({cols[r] for r in cols}) != 4:
        fail(f"有两个角色解析到了同一列：{cols}。请在配置区分别指定列名。")
    return cols


def resolve_inputs(input_files: list[str], input_dir: str) -> list[Path]:
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
            print("已跳过以下非样本文件：")
            for name in skipped:
                print(f"  - {name}")
        if not paths:
            fail(f"目录里的 .csv 全部被跳过了：\n  {directory}")
    else:
        fail(
            "输入路径还没填。请打开本脚本，在「配置区」填写：\n"
            "    INPUT_FILES = [r\"...\\Liu_1191_nvdrs_2024.csv\", ]\n"
            "  或\n"
            "    INPUT_DIR = r\"...\"\n"
            "也可以用命令行：--input \"文件路径\""
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
        fail("输出路径还没填。请在「配置区」填写：\n    OUTPUT_DIR = r\"...\"\n"
             "也可以用命令行：--output-dir \"路径\"")
    path = Path(output_dir.strip())
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        fail(f"创建输出目录失败：\n  {path}\n  {exc}")
    return path


def parse_bands(spec) -> list[tuple[int, int]]:
    """接受 [(18,30),...] 或 '18-30 31-40 ...'；拒绝重叠区间。"""
    bands: list[tuple[int, int]] = []
    if isinstance(spec, str):
        spec = [t for t in re.split(r"[,\s]+", spec) if t]
    for item in spec:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            low, high = int(item[0]), int(item[1])
        else:
            m = re.fullmatch(r"(\d{1,3})\s*[-:to]{1,2}\s*(\d{1,3})", str(item).strip())
            if not m:
                fail(f"看不懂年龄段 {item!r}，应该像 '18-30'")
            low, high = int(m.group(1)), int(m.group(2))
        if low > high:
            fail(f"年龄段 {low}-{high} 反了")
        bands.append((low, high))
    bands.sort()
    for (a_lo, a_hi), (b_lo, b_hi) in zip(bands, bands[1:]):
        if b_lo <= a_hi:
            fail(f"年龄段 {a_lo}-{a_hi} 和 {b_lo}-{b_hi} 重叠，一行会进两个文件")
    if not bands:
        fail("AGE_BANDS 是空的")
    return bands


class Writer:
    """按需创建的追加写入器：表头只写一次，没写过就留一个空表头文件。"""

    def __init__(self, path: Path, encoding: str):
        self.path, self.encoding, self.handle, self.rows = path, encoding, None, 0

    def write(self, frame: pd.DataFrame) -> None:
        if frame.empty:
            return
        if self.handle is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.handle = open(self.path, "w", newline="", encoding=self.encoding)
            frame.to_csv(self.handle, index=False, header=True)
        else:
            frame.to_csv(self.handle, index=False, header=False)
        self.rows += len(frame)

    def finish(self, columns) -> None:
        if self.handle is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(columns=columns).to_csv(
                self.path, index=False, encoding=self.encoding
            )
        else:
            self.handle.close()
            self.handle = None


def run(
    input_files: list[str],
    input_dir: str,
    output_dir: str,
    *,
    age_bands=AGE_BANDS,
    years=tuple(YEARS),
    age_col: str = "",
    year_col: str = "",
    occupation_col: str = "",
    industry_col: str = "",
    electrician_keyword: str = ELECTRICIAN_KEYWORD,
    construction_value: str = CONSTRUCTION_VALUE,
    unknown_industry_values=tuple(UNKNOWN_INDUSTRY_VALUES),
    unknown_industry_goes_to: str = "separate",
    write_per_year_per_band: bool = False,
    encoding: str = "utf-8",
    chunk_size: int = 50_000,
    progress_every: int = 1_000_000,
) -> dict:
    if unknown_industry_goes_to not in {"separate", "nonconstruction"}:
        fail("UNKNOWN_INDUSTRY_GOES_TO 只能是 \"separate\" 或 \"nonconstruction\"")
    keyword = norm_text(electrician_keyword)
    if not keyword:
        fail("ELECTRICIAN_KEYWORD 是空的")
    constr = norm_text(construction_value)
    if not constr:
        fail("CONSTRUCTION_VALUE 是空的")
    unknown_set = {norm_text(v) for v in unknown_industry_values} - {""}

    bands = parse_bands(age_bands)
    band_labels = [f"{lo}-{hi}" for lo, hi in bands]
    wanted_years = sorted({int(y) for y in years})
    files = resolve_inputs(input_files, input_dir)
    out_dir = resolve_output(output_dir)
    separate_unknown = unknown_industry_goes_to == "separate"
    groups = [g for g in GROUPS if separate_unknown or g != "Unknown_industry_electrician"]

    print("=" * 78)
    print("输入文件：")
    for path in files:
        print(f"  {path}  ({path.stat().st_size / 1048576:,.0f} MB)")
    print(f"\n输出目录：{out_dir}")
    print(f"\n年龄段：{', '.join(band_labels)}")
    print(f"年份  ：{wanted_years[0]}-{wanted_years[-1]}  {wanted_years}")
    print(f"是电工：职业列 出现 {electrician_keyword!r}（不区分大小写）")
    print(f"是建筑：行业列 等于 {construction_value!r}（不区分大小写）")
    print(f"行业未知：{'单独成组' if separate_unknown else '并入 Non_construction'}")
    print(f"每年×每段行级文件：{'输出' if write_per_year_per_band else '不输出（看 summary）'}")
    print("=" * 78)

    # ---- 输出写入器 ---------------------------------------------------------
    all_year = {g: Writer(out_dir / "All_year" / f"{g}_All_year.csv", encoding)
                for g in groups}
    all_year_band = {
        (g, b): Writer(out_dir / "All_year" / f"{g}_All_year_age_{b}.csv", encoding)
        for g in groups for b in band_labels
    }
    by_year = {
        (g, y): Writer(out_dir / "by_year" / str(y) / f"{g}_{y}.csv", encoding)
        for g in groups for y in wanted_years
    }
    by_year_band = {}
    if write_per_year_per_band:
        by_year_band = {
            (g, y, b): Writer(
                out_dir / "by_year" / str(y) / f"{g}_{y}_age_{b}.csv", encoding)
            for g in groups for y in wanted_years for b in band_labels
        }

    out_columns = None
    funnel = {
        "read": 0, "age_bad": 0, "age_out": 0,
        "year_bad": 0, "year_out": 0, "not_electrician": 0, "electricians": 0,
    }
    cell_counts: dict[tuple[int, str, str], int] = {}
    industry_counts: dict[str, int] = {}
    occupation_counts: dict[str, int] = {}
    started = time.time()
    next_progress = progress_every

    try:
        for path in files:
            header = pd.read_csv(path, dtype=str, nrows=0, encoding=encoding)
            cols = resolve_columns(header.columns, {
                "age": age_col.strip() or None, "year": year_col.strip() or None,
                "occupation": occupation_col.strip() or None,
                "industry": industry_col.strip() or None,
            })
            print(f"\n{path.name} 的列：age={cols['age']!r}  year={cols['year']!r}  "
                  f"occupation={cols['occupation']!r}  industry={cols['industry']!r}")

            file_columns = list(header.columns)
            for added in ("age_band", "incident_year", "electrician_group"):
                if added not in file_columns:
                    file_columns.append(added)
            if out_columns is None:
                out_columns = file_columns
            elif file_columns != out_columns:
                fail(f"{path.name} 的列和前面的文件不一致，合并会错位。\n"
                     f"  前面的列：{out_columns}\n  这个文件：{file_columns}")

            for chunk in pd.read_csv(
                path, dtype=str, keep_default_na=True, encoding=encoding,
                chunksize=chunk_size,
            ):
                funnel["read"] += len(chunk)

                # --- 年龄 -------------------------------------------------
                ages = chunk[cols["age"]].map(lambda v: parse_int(v, 3))
                age_ok = ages.notna()
                funnel["age_bad"] += int((~age_ok).sum())
                band = pd.Series([None] * len(chunk), index=chunk.index, dtype=object)
                for (lo, hi), label in zip(bands, band_labels):
                    band = band.where(
                        ~(age_ok & ages.between(lo, hi, inclusive="both")), label
                    )
                in_band = band.notna()
                funnel["age_out"] += int((age_ok & ~in_band).sum())
                chunk = chunk.loc[in_band]
                if chunk.empty:
                    continue
                band = band.loc[chunk.index]

                # --- 年份 -------------------------------------------------
                yrs = chunk[cols["year"]].map(parse_year)
                year_ok = yrs.notna()
                funnel["year_bad"] += int((~year_ok).sum())
                in_years = year_ok & yrs.isin(wanted_years)
                funnel["year_out"] += int((year_ok & ~in_years).sum())
                chunk = chunk.loc[in_years]
                if chunk.empty:
                    continue
                band = band.loc[chunk.index]
                yrs = yrs.loc[chunk.index].astype(int)

                # --- 电工 -------------------------------------------------
                occ_text = chunk[cols["occupation"]].map(norm_text)
                is_elec = occ_text.str.contains(keyword, regex=False, na=False)
                funnel["not_electrician"] += int((~is_elec).sum())
                chunk = chunk.loc[is_elec]
                if chunk.empty:
                    continue
                band = band.loc[chunk.index]
                yrs = yrs.loc[chunk.index]
                funnel["electricians"] += len(chunk)

                for value, n in chunk[cols["occupation"]].fillna(BLANK_LABEL) \
                        .value_counts().items():
                    occupation_counts[value] = occupation_counts.get(value, 0) + int(n)
                for value, n in chunk[cols["industry"]].fillna(BLANK_LABEL) \
                        .value_counts().items():
                    industry_counts[value] = industry_counts.get(value, 0) + int(n)

                # --- 行业分组 ---------------------------------------------
                ind_text = chunk[cols["industry"]].map(norm_text)
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

                base = chunk.copy()
                base["age_band"] = band.values
                base["incident_year"] = yrs.astype(str).values

                for group, mask in buckets.items():
                    sub = base.loc[mask]
                    if sub.empty:
                        continue
                    sub = sub.copy()
                    sub["electrician_group"] = group
                    sub = sub[out_columns]
                    all_year[group].write(sub)
                    for label in band_labels:
                        part = sub.loc[sub["age_band"] == label]
                        if not part.empty:
                            all_year_band[(group, label)].write(part)
                    for year in wanted_years:
                        ypart = sub.loc[sub["incident_year"] == str(year)]
                        if ypart.empty:
                            continue
                        by_year[(group, year)].write(ypart)
                        for label in band_labels:
                            cell = ypart.loc[ypart["age_band"] == label]
                            if cell.empty:
                                continue
                            key = (year, label, group)
                            cell_counts[key] = cell_counts.get(key, 0) + len(cell)
                            if write_per_year_per_band:
                                by_year_band[(group, year, label)].write(cell)

                # All_industry 是并集，再写一遍同样的行是有意为之
                allrows = base.copy()
                allrows["electrician_group"] = "All_industry_electrician"
                allrows = allrows[out_columns]
                g = "All_industry_electrician"
                all_year[g].write(allrows)
                for label in band_labels:
                    part = allrows.loc[allrows["age_band"] == label]
                    if not part.empty:
                        all_year_band[(g, label)].write(part)
                for year in wanted_years:
                    ypart = allrows.loc[allrows["incident_year"] == str(year)]
                    if ypart.empty:
                        continue
                    by_year[(g, year)].write(ypart)
                    for label in band_labels:
                        cell = ypart.loc[ypart["age_band"] == label]
                        if cell.empty:
                            continue
                        key = (year, label, g)
                        cell_counts[key] = cell_counts.get(key, 0) + len(cell)
                        if write_per_year_per_band:
                            by_year_band[(g, year, label)].write(cell)

                if funnel["read"] >= next_progress:
                    rate = funnel["read"] / max(time.time() - started, 1e-9)
                    print(f"  ... 已读 {funnel['read']:,} 行 "
                          f"（{rate:,.0f} 行/秒），电工 {funnel['electricians']:,}",
                          flush=True)
                    next_progress += progress_every
    finally:
        cols_out = out_columns or []
        for writer in list(all_year.values()) + list(all_year_band.values()) \
                + list(by_year.values()) + list(by_year_band.values()):
            writer.finish(cols_out)

    elapsed = time.time() - started

    # ---- 汇总表 -------------------------------------------------------------
    rows = [
        {"year": y, "age_band": b, "group": g, "rows": n}
        for (y, b, g), n in sorted(cell_counts.items())
    ]
    summary = pd.DataFrame(rows, columns=["year", "age_band", "group", "rows"])
    summary.to_csv(out_dir / "summary_by_year_and_age.csv", index=False)

    funnel_df = pd.DataFrame([
        {"stage": "读入总行数", "rows": funnel["read"]},
        {"stage": "年龄无法解析（剔除）", "rows": -funnel["age_bad"]},
        {"stage": f"年龄不在 {bands[0][0]}-{bands[-1][1]}（剔除）", "rows": -funnel["age_out"]},
        {"stage": "年份无法解析（剔除）", "rows": -funnel["year_bad"]},
        {"stage": f"年份不在 {wanted_years[0]}-{wanted_years[-1]}（剔除）",
         "rows": -funnel["year_out"]},
        {"stage": f"职业不含 {electrician_keyword!r}（剔除）",
         "rows": -funnel["not_electrician"]},
        {"stage": "= 电工（进入分组）", "rows": funnel["electricians"]},
    ])
    funnel_df.to_csv(out_dir / "funnel.csv", index=False)

    file_rows = []
    for g in groups:
        file_rows.append({"scope": "All_year", "year": "ALL", "age_band": "ALL",
                          "group": g, "rows": all_year[g].rows,
                          "output_file": str(all_year[g].path)})
        for b in band_labels:
            w = all_year_band[(g, b)]
            file_rows.append({"scope": "All_year", "year": "ALL", "age_band": b,
                              "group": g, "rows": w.rows, "output_file": str(w.path)})
        for y in wanted_years:
            w = by_year[(g, y)]
            file_rows.append({"scope": "by_year", "year": y, "age_band": "ALL",
                              "group": g, "rows": w.rows, "output_file": str(w.path)})
            if write_per_year_per_band:
                for b in band_labels:
                    w = by_year_band[(g, y, b)]
                    file_rows.append({"scope": "by_year", "year": y, "age_band": b,
                                      "group": g, "rows": w.rows,
                                      "output_file": str(w.path)})
    file_map = pd.DataFrame(file_rows)
    file_map.insert(0, "input_files", "; ".join(str(p) for p in files))
    file_map.to_csv(out_dir / "file_map.csv", index=False)

    def bucket_of(value: str) -> str:
        if value == BLANK_LABEL:
            return "Unknown" if separate_unknown else "Non_construction"
        text = norm_text(value)
        if text == constr:
            return "Construction"
        if text == "" or text in unknown_set:
            return "Unknown" if separate_unknown else "Non_construction"
        return "Non_construction"

    ind_breakdown = pd.DataFrame(
        [{"Census2018_Industry": v, "n_electricians": n, "counted_as": bucket_of(v)}
         for v, n in sorted(industry_counts.items(), key=lambda kv: -kv[1])],
        columns=["Census2018_Industry", "n_electricians", "counted_as"],
    )
    ind_breakdown.to_csv(out_dir / "electrician_industry_values.csv", index=False)
    occ_breakdown = pd.DataFrame(
        [{"Census2018_Occupation": v, "n": n}
         for v, n in sorted(occupation_counts.items(), key=lambda kv: -kv[1])],
        columns=["Census2018_Occupation", "n"],
    )
    occ_breakdown.to_csv(out_dir / "matched_occupation_values.csv", index=False)

    # ---- 报告 ---------------------------------------------------------------
    print("\n" + "=" * 78)
    print("逐级筛选")
    print("=" * 78)
    print(funnel_df.to_string(index=False))
    checked = (funnel["read"] - funnel["age_bad"] - funnel["age_out"]
               - funnel["year_bad"] - funnel["year_out"] - funnel["not_electrician"])
    if checked == funnel["electricians"]:
        print("  （逐级相减 = 电工数，没有行被漏算）")
    else:
        print(f"  警告：逐级相减得 {checked:,}，电工数是 {funnel['electricians']:,}")

    print("\n" + "=" * 78)
    print("All_year 总表（跨全部年份）")
    print("=" * 78)
    for g in groups:
        print(f"  {g:<30} {all_year[g].rows:>9,} 行   {all_year[g].path}")
    n_all = all_year["All_industry_electrician"].rows
    n_parts = sum(all_year[g].rows for g in groups if g != "All_industry_electrician")
    print(f"\n  All = {n_all:,}  各组之和 = {n_parts:,}"
          + ("  ✓" if n_all == n_parts else f"  警告：差 {n_all - n_parts:,}"))
    print("  注意：All_industry 是并集，同一行也在它所属的行业组文件里。")

    if not summary.empty:
        print("\n" + "=" * 78)
        print("年龄段 × 组（全部年份）")
        print("=" * 78)
        pivot = summary.pivot_table(index="age_band", columns="group",
                                    values="rows", aggfunc="sum", fill_value=0)
        print(pivot.to_string())

        print("\n" + "=" * 78)
        print("年份 × 组（全部年龄段）")
        print("=" * 78)
        pivot_y = summary.pivot_table(index="year", columns="group",
                                      values="rows", aggfunc="sum", fill_value=0)
        print(pivot_y.to_string())

    if n_all == 0:
        print(f"\n  警告：一个电工都没匹配到。职业列里没有出现 "
              f"{electrician_keyword!r} 的值。\n"
              "  如果这一列存的是数字码而不是文字，本脚本的判断方式不适用。")
    else:
        print("\n" + "=" * 78)
        print(f"匹配到的职业原文（共 {len(occ_breakdown)} 种）")
        print("=" * 78)
        print(occ_breakdown.head(20).to_string(index=False))
        print("\n" + "=" * 78)
        print(f"电工所在行业的原文取值（共 {len(ind_breakdown)} 种）")
        print("=" * 78)
        print(ind_breakdown.head(25).to_string(index=False))
        if all_year["Construction_electrician"].rows == 0:
            print(f"\n  警告：没有任何电工的行业等于 {construction_value!r}。"
                  "\n  上表就是实际出现的写法，按需要改 CONSTRUCTION_VALUE。")

    print(f"\n耗时 {elapsed:,.1f} 秒"
          f"（{funnel['read'] / max(elapsed, 1e-9):,.0f} 行/秒）")
    print(f"\n写出：")
    print(f"  {out_dir / 'All_year'}/            最终总表 + 分年龄段")
    print(f"  {out_dir / 'by_year'}/             各年")
    print(f"  {out_dir / 'summary_by_year_and_age.csv'}")
    print(f"  {out_dir / 'funnel.csv'}")
    print(f"  {out_dir / 'file_map.csv'}")
    print(f"  {out_dir / 'electrician_industry_values.csv'}")
    print(f"  {out_dir / 'matched_occupation_values.csv'}")

    return {
        "input_files": files, "output_dir": out_dir,
        "bands": band_labels, "years": wanted_years,
        "funnel": funnel, "summary": summary, "file_map": file_map,
        "industry_values": ind_breakdown, "occupation_values": occ_breakdown,
        "all_year_rows": {g: all_year[g].rows for g in groups},
        "elapsed_seconds": elapsed,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="从原始 NVDRS 文件一遍扫出电工三组（年龄段 × 年份）"
    )
    parser.add_argument("--input", nargs="+", default=None)
    parser.add_argument("--input-dir", default=None)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--bands", nargs="+", default=None,
                        help="例：--bands 18-30 31-40 41-50 51-60 61-70")
    parser.add_argument("--years", nargs="+", type=int, default=None)
    parser.add_argument("--age-col", default=None)
    parser.add_argument("--year-col", default=None)
    parser.add_argument("--occupation-col", default=None)
    parser.add_argument("--industry-col", default=None)
    parser.add_argument("--electrician-keyword", default=None)
    parser.add_argument("--construction-value", default=None)
    parser.add_argument("--unknown-industry-goes-to",
                        choices=["separate", "nonconstruction"], default=None)
    parser.add_argument("--per-year-per-band", action="store_true", default=None)
    parser.add_argument("--encoding", default=None)
    parser.add_argument("--chunk-size", type=int, default=None)
    args = parser.parse_args(argv)

    pick = lambda a, b: a if a is not None else b  # noqa: E731
    result = run(
        pick(args.input, INPUT_FILES),
        pick(args.input_dir, INPUT_DIR),
        pick(args.output_dir, OUTPUT_DIR),
        age_bands=pick(args.bands, AGE_BANDS),
        years=pick(args.years, YEARS),
        age_col=pick(args.age_col, AGE_COL),
        year_col=pick(args.year_col, YEAR_COL),
        occupation_col=pick(args.occupation_col, OCCUPATION_COL),
        industry_col=pick(args.industry_col, INDUSTRY_COL),
        electrician_keyword=pick(args.electrician_keyword, ELECTRICIAN_KEYWORD),
        construction_value=pick(args.construction_value, CONSTRUCTION_VALUE),
        unknown_industry_goes_to=pick(args.unknown_industry_goes_to,
                                      UNKNOWN_INDUSTRY_GOES_TO),
        write_per_year_per_band=pick(args.per_year_per_band, WRITE_PER_YEAR_PER_BAND),
        encoding=pick(args.encoding, ENCODING),
        chunk_size=pick(args.chunk_size, CHUNK_SIZE),
    )
    return 0 if result["all_year_rows"].get("All_industry_electrician") else 1


if __name__ == "__main__":
    sys.exit(main())
