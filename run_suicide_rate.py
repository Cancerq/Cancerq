#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NVDRS（分子）÷ ACS PUMS（分母）按年份算自杀率。

    自杀率 = NVDRS 死亡数 ÷ (ACS PUMS 人口 ÷ 1000)      即「每 1000 人」

分子：已经按 18-67 岁筛好的 NVDRS 行级 CSV，每行一例（本脚本不再筛年龄）。
分母：ACS PUMS 加权人口（PWGTP 之和），按 年份 × 州 填好。

产出两张表：

    表 A  2018-2024 各年自杀率（州范围固定为 2018 年 NVDRS 覆盖的州）
          分子 = 每年只数这些州的死亡
          分母 = 每年只加这些州的 ACS 人口
          -> 7 年的州范围一致，可以直接比较趋势

    表 B  2024 年单年自杀率（2024 年 NVDRS 覆盖的全部州）
          分子 = 2024 年全部死亡
          分母 = 2024 年这些州的 ACS 人口

「覆盖的州」直接从 NVDRS 数据本身读：某年数据里出现过的州，就算那年覆盖。

输出目录：

    OUTPUT_DIR/
      A_rate_2018_2024_2018_states.csv     表 A
      B_rate_2024_all_states.csv           表 B
      B_rate_2024_by_state.csv             表 B 按州拆开（核对用）
      detail_by_year_state.csv             年份 × 州 的分子、分母明细
      state_coverage.csv                   每个州在哪些年份出现在 NVDRS 里
      funnel.csv                           分子行数的去向交代
      suicide_rates.xlsx                   以上各表合进一个 Excel（装了 openpyxl 才有）

分母还没准备好时：把 ACS_FILE 留空、ACS_TABLE 也留空，直接运行一次，
脚本会在输出目录写一个 acs_denominator_template.csv —— 里面列好了需要的
每一个 年份 × 州，把 population 那一列填上，再把路径填进 ACS_FILE 即可。

单文件，只依赖 pandas。

用法：把「配置区」的路径填好，然后运行

    python run_suicide_rate.py
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

# 【必填 1】输入：NVDRS 分子（已按 18-67 岁筛好的行级 CSV，可以多个）
NVDRS_FILES = [
    r"",     # 例：r"D:\School_project\Project\NVDRS\NVDRS_18_67_2018_2024.csv"
    r"",
]

# 【必填 2】输出目录（不存在会自动创建）
OUTPUT_DIR = r""     # 例：r"D:\School_project\Project\NVDRS\Suicide_rate"

# 【必填 3】ACS PUMS 分母 —— 两种方式二选一
#
# 方式一：CSV 文件（长表），三列：year, state, population
#     year        2018 ... 2024
#     state       州，可以写 FIPS 码（ACS 的 ST 列，如 6 / 06）、缩写（CA）或全名（California）
#     population  该年该州的 ACS PUMS 加权人口（PWGTP 之和），未除以 1000 的原始人数
ACS_FILE = r""       # 例：r"D:\School_project\Project\ACS_PUMS\acs_pums_18_67_by_year_state.csv"

# 方式二：直接填在这里（填了 ACS_FILE 就忽略这个）
#     {年份: {州: 人口, ...}, ...}
ACS_TABLE: dict[int, dict[str, float]] = {
    # 2018: {"AK": 0, "CO": 0, ...},
    # 2019: {...},
    # ...
    # 2024: {...},
}

# -----------------------------------------------------------------------------
# 以下通常不用改
# -----------------------------------------------------------------------------

YEARS = [2018, 2019, 2020, 2021, 2022, 2023, 2024]
BASE_YEAR = 2018       # 表 A 的州范围取这一年 NVDRS 覆盖的州
SINGLE_YEAR = 2024     # 表 B 的年份

# 分母先除以这个数：1000 -> 每 1000 人；想要每 10 万人就改成 100000
DENOMINATOR_SCALE = 1000

# NVDRS 列名。留空 = 自动识别
YEAR_COL = r""         # 自动找 IncidentYear
STATE_COL = r""        # 自动找 SiteState / State / ...
COUNT_COL = r""        # 行级数据留空（每行算 1 例）；已汇总的表填计数列名

# ACS_FILE 的列名。留空 = 自动识别
ACS_YEAR_COL = r""
ACS_STATE_COL = r""
ACS_POP_COL = r""

ENCODING = "utf-8"
CHUNK_SIZE = 200_000

# =============================================================================
# 配置区结束
# =============================================================================

MISSING_TOKENS = {"", ".", "-", "--", "nan", "none", "null", "<na>", "unknown", "unk"}

YEAR_COL_CANDIDATES = ("incidentyear", "incyear", "yearofincident")
WRONG_YEAR_COLS = {"deathyear", "yearofdeath", "injuryyear", "yearofinjury",
                   "birthyear", "filingyear", "reportyear"}
STATE_COL_CANDIDATES = ("sitestate", "state", "incidentstate",
                        "stateabbr", "statecode", "st")

ACS_YEAR_CANDIDATES = ("year", "acsyear", "surveyyear")
ACS_STATE_CANDIDATES = ("state", "st", "statefips", "stateabbr", "statecode")
ACS_POP_CANDIDATES = ("population", "pop", "pwgtp", "weightedpop",
                      "weightedpopulation", "denominator", "n")

# (FIPS, 缩写, 全名) —— 50 州 + DC + PR
STATES = [
    (1, "AL", "Alabama"), (2, "AK", "Alaska"), (4, "AZ", "Arizona"),
    (5, "AR", "Arkansas"), (6, "CA", "California"), (8, "CO", "Colorado"),
    (9, "CT", "Connecticut"), (10, "DE", "Delaware"),
    (11, "DC", "District of Columbia"), (12, "FL", "Florida"),
    (13, "GA", "Georgia"), (15, "HI", "Hawaii"), (16, "ID", "Idaho"),
    (17, "IL", "Illinois"), (18, "IN", "Indiana"), (19, "IA", "Iowa"),
    (20, "KS", "Kansas"), (21, "KY", "Kentucky"), (22, "LA", "Louisiana"),
    (23, "ME", "Maine"), (24, "MD", "Maryland"), (25, "MA", "Massachusetts"),
    (26, "MI", "Michigan"), (27, "MN", "Minnesota"), (28, "MS", "Mississippi"),
    (29, "MO", "Missouri"), (30, "MT", "Montana"), (31, "NE", "Nebraska"),
    (32, "NV", "Nevada"), (33, "NH", "New Hampshire"), (34, "NJ", "New Jersey"),
    (35, "NM", "New Mexico"), (36, "NY", "New York"),
    (37, "NC", "North Carolina"), (38, "ND", "North Dakota"), (39, "OH", "Ohio"),
    (40, "OK", "Oklahoma"), (41, "OR", "Oregon"), (42, "PA", "Pennsylvania"),
    (44, "RI", "Rhode Island"), (45, "SC", "South Carolina"),
    (46, "SD", "South Dakota"), (47, "TN", "Tennessee"), (48, "TX", "Texas"),
    (49, "UT", "Utah"), (50, "VT", "Vermont"), (51, "VA", "Virginia"),
    (53, "WA", "Washington"), (54, "WV", "West Virginia"),
    (55, "WI", "Wisconsin"), (56, "WY", "Wyoming"), (72, "PR", "Puerto Rico"),
]
STATE_LOOKUP: dict[str, str] = {}
for _fips, _abbr, _name in STATES:
    STATE_LOOKUP[str(_fips)] = _abbr
    STATE_LOOKUP[f"{_fips:02d}"] = _abbr
    STATE_LOOKUP[_abbr.lower()] = _abbr
    STATE_LOOKUP[_name.lower()] = _abbr
STATE_LOOKUP["washington dc"] = "DC"
STATE_LOOKUP["washington, dc"] = "DC"
STATE_LOOKUP["washington d.c."] = "DC"
STATE_NAME = {abbr: name for _, abbr, name in STATES}


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


def norm_colname(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def parse_year(value):
    text = norm_text(value)
    m = re.fullmatch(r"(\d{4})(?:\.0+)?", text)
    return int(m.group(1)) if m else None


def parse_state(value):
    """FIPS（6 / 06 / 6.0）、缩写（CA）、全名（California）都转成缩写；认不出返回 None。"""
    text = norm_text(value)
    if not text:
        return None
    m = re.fullmatch(r"(\d{1,2})(?:\.0+)?", text)
    if m:
        text = str(int(m.group(1)))
    return STATE_LOOKUP.get(text)


def find_col(columns, candidates, skip=()):
    normalised = {norm_colname(c): c for c in columns if norm_colname(c) not in skip}
    for cand in candidates:
        if cand in normalised:
            return normalised[cand]
    return None


# -----------------------------------------------------------------------------
# 分子：NVDRS
# -----------------------------------------------------------------------------

def read_numerator(paths, *, year_col="", state_col="", count_col="",
                   encoding="utf-8", chunk_size=200_000):
    """返回 (年份 × 州 死亡数 DataFrame, funnel 列表)。只读需要的列、分块读。"""
    counts: dict[tuple[int, str], float] = {}
    funnel = {"rows_read": 0, "year_unreadable": 0, "year_outside": 0,
              "state_unreadable": 0}
    bad_states: dict[str, int] = {}

    for path in paths:
        header = pd.read_csv(path, nrows=0, encoding=encoding).columns
        ycol = year_col or find_col(header, YEAR_COL_CANDIDATES, skip=WRONG_YEAR_COLS)
        scol = state_col or find_col(header, STATE_COL_CANDIDATES)
        for role, col in (("年份", ycol), ("州", scol), ("计数", count_col)):
            if col and col not in header:
                fail(f"{path.name}：指定的{role}列 {col!r} 不在文件里。\n"
                     f"列有：{', '.join(map(str, header))}")
        if not ycol:
            fail(f"{path.name}：找不到 IncidentYear 列，请在配置区填 YEAR_COL。\n"
                 f"列有：{', '.join(map(str, header))}")
        if not scol:
            fail(f"{path.name}：找不到州列（SiteState / State），请在配置区填 STATE_COL。\n"
                 f"列有：{', '.join(map(str, header))}")
        print(f"读取 {path.name}   年份列={ycol}  州列={scol}"
              + (f"  计数列={count_col}" if count_col else ""))

        usecols = [ycol, scol] + ([count_col] if count_col else [])
        for chunk in pd.read_csv(path, usecols=usecols, dtype=str,
                                 encoding=encoding, chunksize=chunk_size):
            funnel["rows_read"] += len(chunk)
            years = chunk[ycol].map(parse_year)
            states = chunk[scol].map(parse_state)
            weight = (pd.to_numeric(chunk[count_col], errors="coerce").fillna(0)
                      if count_col else pd.Series(1, index=chunk.index))

            no_year = years.isna()
            outside = ~no_year & ~years.isin(YEARS)
            no_state = ~no_year & ~outside & states.isna()
            funnel["year_unreadable"] += int(no_year.sum())
            funnel["year_outside"] += int(outside.sum())
            funnel["state_unreadable"] += int(no_state.sum())
            for raw in chunk.loc[no_state, scol].fillna("(空白)"):
                bad_states[raw] = bad_states.get(raw, 0) + 1

            keep = ~(no_year | outside | no_state)
            grouped = (pd.DataFrame({"year": years[keep].astype(int),
                                     "state": states[keep],
                                     "w": weight[keep]})
                       .groupby(["year", "state"])["w"].sum())
            for key, value in grouped.items():
                counts[key] = counts.get(key, 0) + value

    if bad_states:
        print("注意：以下州取值认不出，这些行没计入分子：")
        for raw, n in sorted(bad_states.items(), key=lambda kv: -kv[1])[:20]:
            print(f"  {raw!r}: {n}")

    frame = pd.DataFrame(
        [(y, s, n) for (y, s), n in counts.items()],
        columns=["year", "state", "deaths"],
    ).sort_values(["year", "state"], ignore_index=True)
    used = funnel["rows_read"] - funnel["year_unreadable"] \
        - funnel["year_outside"] - funnel["state_unreadable"]
    funnel_rows = [
        ("读入行数", funnel["rows_read"]),
        ("IncidentYear 为空/认不出（剔除）", funnel["year_unreadable"]),
        (f"IncidentYear 不在 {YEARS[0]}-{YEARS[-1]}（剔除）", funnel["year_outside"]),
        ("州为空/认不出（剔除）", funnel["state_unreadable"]),
        ("进入分子的行数", used),
    ]
    return frame, funnel_rows


# -----------------------------------------------------------------------------
# 分母：ACS PUMS
# -----------------------------------------------------------------------------

def read_denominator(acs_file="", acs_table=None, *, year_col="", state_col="",
                     pop_col="", encoding="utf-8"):
    """返回 年份 × 州 人口 DataFrame；两种方式都没填返回 None。"""
    if acs_file and acs_file.strip():
        path = Path(acs_file.strip())
        if not path.is_file():
            fail(f"ACS_FILE 找不到：\n  {path}")
        raw = pd.read_csv(path, dtype=str, encoding=encoding)
        ycol = year_col or find_col(raw.columns, ACS_YEAR_CANDIDATES)
        scol = state_col or find_col(raw.columns, ACS_STATE_CANDIDATES)
        pcol = pop_col or find_col(raw.columns, ACS_POP_CANDIDATES)
        missing = [n for n, c in (("year", ycol), ("state", scol),
                                  ("population", pcol)) if not c or c not in raw]
        if missing:
            fail(f"ACS_FILE 缺少列：{', '.join(missing)}\n"
                 f"列有：{', '.join(map(str, raw.columns))}\n"
                 "在配置区填 ACS_YEAR_COL / ACS_STATE_COL / ACS_POP_COL。")
        source = pd.DataFrame({"year": raw[ycol], "state": raw[scol],
                               "population": raw[pcol]})
    elif acs_table:
        source = pd.DataFrame(
            [(y, s, p) for y, row in acs_table.items() for s, p in row.items()],
            columns=["year", "state", "population"],
        )
    else:
        return None

    frame = pd.DataFrame({
        "year": source["year"].map(parse_year),
        "state": source["state"].map(parse_state),
        "population": pd.to_numeric(
            source["population"].astype(str).str.replace(",", ""), errors="coerce"),
    })
    bad = frame["year"].isna() | frame["state"].isna()
    if bad.any():
        fail("ACS 分母里有认不出的年份或州：\n"
             + source[bad].head(10).to_string(index=False))
    frame["year"] = frame["year"].astype(int)
    blank = frame["population"].isna()
    if blank.any():
        print(f"注意：ACS 分母有 {int(blank.sum())} 格人口为空，相关年份的率会留空。")
    dup = frame.duplicated(["year", "state"], keep=False)
    if dup.any():
        fail("ACS 分母里同一 年份 × 州 出现了多次：\n"
             + frame[dup].sort_values(["year", "state"]).to_string(index=False))
    return frame


# -----------------------------------------------------------------------------
# 计算
# -----------------------------------------------------------------------------

def rate_row(label, year, states, num, den, scale):
    """把一组州的分子、分母加总成一行。缺分母的州让整行的率留空，绝不少算。"""
    deaths = num[(num["year"] == year) & num["state"].isin(states)]["deaths"].sum()
    den_year = den[(den["year"] == year) & den["state"].isin(states)]
    have = set(den_year.dropna(subset=["population"])["state"])
    lacking = sorted(set(states) - have)
    population = den_year["population"].sum() if not lacking else float("nan")
    per_scale = population / scale
    return {
        "table": label,
        "year": year,
        "n_states": len(states),
        "states": " ".join(sorted(states)),
        "nvdrs_deaths": int(deaths) if float(deaths).is_integer() else deaths,
        "acs_population": population,
        f"acs_population_div_{scale}": per_scale,
        f"rate_per_{scale}": deaths / per_scale if per_scale else float("nan"),
        "missing_acs_states": " ".join(lacking),
    }


def denominator_template(num, base_year, single_year, years):
    base = sorted(num.loc[num["year"] == base_year, "state"].unique())
    single = sorted(num.loc[num["year"] == single_year, "state"].unique())
    needed = {(y, s) for y in years for s in base} | {(single_year, s) for s in single}
    return pd.DataFrame(
        [(y, s, STATE_NAME[s], "") for y, s in sorted(needed)],
        columns=["year", "state", "state_name", "population"],
    )


def run(nvdrs_files, output_dir, *, acs_file="", acs_table=None,
        year_col="", state_col="", count_col="",
        acs_year_col="", acs_state_col="", acs_pop_col="",
        base_year=BASE_YEAR, single_year=SINGLE_YEAR, years=tuple(YEARS),
        scale=DENOMINATOR_SCALE, encoding="utf-8", chunk_size=200_000) -> dict:
    paths = [Path(f.strip()) for f in nvdrs_files if f and f.strip()]
    if not paths:
        fail("NVDRS 输入路径还没填。请在「配置区」填写 NVDRS_FILES，"
             "或用命令行 --input \"文件路径\"")
    for path in paths:
        if not path.is_file():
            fail(f"NVDRS 文件找不到：\n  {path}")
    if not output_dir or not output_dir.strip():
        fail("输出路径还没填。请在「配置区」填写 OUTPUT_DIR，或用命令行 --output-dir")
    out = Path(output_dir.strip())
    out.mkdir(parents=True, exist_ok=True)
    if scale <= 0:
        fail("DENOMINATOR_SCALE 必须大于 0")

    num, funnel = read_numerator(paths, year_col=year_col, state_col=state_col,
                                 count_col=count_col, encoding=encoding,
                                 chunk_size=chunk_size)
    pd.DataFrame(funnel, columns=["step", "rows"]).to_csv(
        out / "funnel.csv", index=False, encoding="utf-8-sig")

    base_states = sorted(num.loc[num["year"] == base_year, "state"].unique())
    single_states = sorted(num.loc[num["year"] == single_year, "state"].unique())
    if not base_states:
        fail(f"NVDRS 里没有 {base_year} 年的数据，定不出表 A 的州范围。")
    if not single_states:
        fail(f"NVDRS 里没有 {single_year} 年的数据，算不了表 B。")

    coverage = (num.assign(flag=1)
                .pivot_table(index="state", columns="year", values="flag",
                             aggfunc="max", fill_value=0)
                .reindex(columns=list(years), fill_value=0))
    coverage.insert(0, "state_name", coverage.index.map(STATE_NAME))
    coverage[f"in_{base_year}_set"] = coverage.index.isin(base_states).astype(int)
    coverage.reset_index().to_csv(out / "state_coverage.csv", index=False,
                                  encoding="utf-8-sig")

    den = read_denominator(acs_file, acs_table, year_col=acs_year_col,
                           state_col=acs_state_col, pop_col=acs_pop_col,
                           encoding=encoding)
    if den is None:
        template = out / "acs_denominator_template.csv"
        denominator_template(num, base_year, single_year, years).to_csv(
            template, index=False, encoding="utf-8-sig")
        fail("ACS 分母还没填。已生成分母模板：\n"
             f"  {template}\n"
             "把 population 列填好（ACS PUMS 加权人口，原始人数，不用除以 1000），\n"
             "再把路径填进配置区的 ACS_FILE，重新运行。")

    table_a = pd.DataFrame([
        rate_row(f"A: {base_year} NVDRS states", y, base_states, num, den, scale)
        for y in years
    ])
    table_b = pd.DataFrame([
        rate_row(f"B: {single_year} all NVDRS states", single_year,
                 single_states, num, den, scale)
    ])
    by_state = pd.DataFrame([
        rate_row(f"B: {single_year} by state", single_year, [s], num, den, scale)
        for s in single_states
    ]).drop(columns=["n_states", "table"]).rename(columns={"states": "state"})
    by_state.insert(1, "state_name", by_state["state"].map(STATE_NAME))

    detail = num.merge(den, on=["year", "state"], how="outer")
    detail["deaths"] = detail["deaths"].fillna(0)
    detail.insert(2, "state_name", detail["state"].map(STATE_NAME))
    detail[f"in_{base_year}_set"] = detail["state"].isin(base_states).astype(int)
    detail[f"rate_per_{scale}"] = detail["deaths"] / (detail["population"] / scale)
    detail = detail.sort_values(["year", "state"], ignore_index=True)

    files = {
        f"A_rate_{years[0]}_{years[-1]}_{base_year}_states.csv": table_a,
        f"B_rate_{single_year}_all_states.csv": table_b,
        f"B_rate_{single_year}_by_state.csv": by_state,
        "detail_by_year_state.csv": detail,
    }
    for name, frame in files.items():
        frame.to_csv(out / name, index=False, encoding="utf-8-sig")

    try:
        import openpyxl  # noqa: F401
        with pd.ExcelWriter(out / "suicide_rates.xlsx") as xl:
            table_a.to_excel(xl, sheet_name="A_2018_2024_2018states", index=False)
            table_b.to_excel(xl, sheet_name="B_2024_all_states", index=False)
            by_state.to_excel(xl, sheet_name="B_2024_by_state", index=False)
            detail.to_excel(xl, sheet_name="detail_year_state", index=False)
            coverage.reset_index().to_excel(xl, sheet_name="state_coverage", index=False)
            pd.DataFrame(funnel, columns=["step", "rows"]).to_excel(
                xl, sheet_name="funnel", index=False)
    except ImportError:
        print("（没装 openpyxl，跳过 Excel；CSV 已全部写好）")

    for msg, table in (("表 A", table_a), ("表 B", table_b)):
        if table["missing_acs_states"].astype(bool).any():
            print(f"注意：{msg} 有年份缺 ACS 分母，率已留空，见 missing_acs_states 列。")

    rate_col = f"rate_per_{scale}"
    show = ["year", "n_states", "nvdrs_deaths", "acs_population", rate_col]
    print(f"\n表 A：{years[0]}-{years[-1]} 各年自杀率（{base_year} 年 NVDRS 覆盖的 "
          f"{len(base_states)} 个州，每 {scale} 人）")
    print(table_a[show].to_string(index=False))
    print(f"\n表 B：{single_year} 年自杀率（{single_year} 年 NVDRS 覆盖的 "
          f"{len(single_states)} 个州，每 {scale} 人）")
    print(table_b[show].to_string(index=False))
    print(f"\n输出目录：{out}")
    return {"table_a": table_a, "table_b": table_b, "by_state": by_state,
            "detail": detail, "base_states": base_states,
            "single_states": single_states, "output_dir": out}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="NVDRS ÷ (ACS PUMS / 1000) 按年份算自杀率。"
                    "不带参数时用脚本顶部配置区的值。")
    parser.add_argument("--input", nargs="+", default=None, help="NVDRS CSV")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--acs-file", default=None, help="ACS PUMS 分母 CSV")
    parser.add_argument("--year-col", default=None)
    parser.add_argument("--state-col", default=None)
    parser.add_argument("--count-col", default=None)
    parser.add_argument("--scale", type=float, default=None)
    parser.add_argument("--encoding", default=None)
    args = parser.parse_args(argv)

    scale = args.scale if args.scale is not None else DENOMINATOR_SCALE
    if float(scale).is_integer():
        scale = int(scale)
    run(
        args.input if args.input is not None else NVDRS_FILES,
        args.output_dir if args.output_dir is not None else OUTPUT_DIR,
        acs_file=args.acs_file if args.acs_file is not None else ACS_FILE,
        acs_table=ACS_TABLE,
        year_col=args.year_col if args.year_col is not None else YEAR_COL,
        state_col=args.state_col if args.state_col is not None else STATE_COL,
        count_col=args.count_col if args.count_col is not None else COUNT_COL,
        acs_year_col=ACS_YEAR_COL, acs_state_col=ACS_STATE_COL,
        acs_pop_col=ACS_POP_COL,
        scale=scale,
        encoding=args.encoding or ENCODING,
        chunk_size=CHUNK_SIZE,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
