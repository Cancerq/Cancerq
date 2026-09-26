#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NVDRS（分子）÷ ACS PUMS（分母）按年份算自杀率。

    自杀率 = NVDRS 死亡数 ÷ (ACS PUMS 人口 ÷ 1000)      即「每 1000 人」

分子：已经按 18-67 岁筛好的 NVDRS 行级 CSV，每行一例（本脚本不再筛年龄）。
分母：ACS PUMS 加权人口，按 年份 × 州。默认直接读
      acs_pums_2018_2024_all_workers_nvdrs_rad_coverage_weighted.xlsx
      的「State coverage detail」工作表，用
      「Coverage-weighted employed population」列（部分覆盖的州已乘覆盖比例）。

产出两张表：

    表 A  2018-2024 各年自杀率（州范围 = 2018 年 NVDRS 覆盖的州）
          每年 = 2018 年的州 ∩ 当年覆盖的州，分子分母用同一组州
          -> 后来才加入的州不进来，趋势不会被覆盖扩张抬高
          （2018 的州若某年退出，如 New York 2019，那年分子分母都不算它，
            并在 dropped_from_base 列写明）

    表 B  2024 年单年自杀率（2024 年覆盖的全部州）

「覆盖的州」默认取 ACS 分母表里当年列出的州（COVERAGE_FROM = "acs"），
也就是那份 xlsx 按 NVDRS RAD 覆盖整理好的名单；
改成 "nvdrs" 则按 NVDRS 数据里当年出现过的州。
两边对不上的地方（有死亡但不在覆盖里 / 在覆盖里但 0 死亡）都会在表里列出。

输出目录：

    OUTPUT_DIR/
      A_rate_2018_2024_2018_states.csv     表 A
      B_rate_2024_all_states.csv           表 B
      B_rate_2024_by_state.csv             表 B 按州拆开（核对用）
      detail_by_year_state.csv             年份 × 州 的分子、分母明细
      state_coverage.csv                   每个州哪些年份被覆盖
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
import codecs
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
# 方式一：文件。直接填那份 xlsx 即可：
#     acs_pums_2018_2024_all_workers_nvdrs_rad_coverage_weighted.xlsx
#   脚本自动找到「State coverage detail」工作表（表头在第 5 行也没关系），
#   用 Year / Jurisdiction / Coverage-weighted employed population 三列。
#   也可以是自己整理的 CSV 长表：year, state, population
#     state 写 FIPS（06）、缩写（CA）、全名（California）都可以
#     population 是原始人数，不用除以 1000
ACS_FILE = r""       # 例：r"D:\School_project\Project\ACS_PUMS\acs_pums_2018_2024_all_workers_nvdrs_rad_coverage_weighted.xlsx"

# xlsx 里读哪个工作表。留空 = 自动找含 Year + 州 + 人口 三列的那张
ACS_SHEET = r""      # 例："State coverage detail"

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

# 哪些州算「当年覆盖」：
#   "acs"   -> ACS 分母表里当年列出的州（默认；那份 xlsx 只列 NVDRS RAD 覆盖的州）
#   "nvdrs" -> NVDRS 数据里当年出现过的州（分母表列了全部州时用这个）
COVERAGE_FROM = "acs"

# ACS_FILE 的列名。留空 = 自动识别
ACS_YEAR_COL = r""
ACS_STATE_COL = r""
ACS_POP_COL = r""        # 自动优先用 Coverage-weighted employed population
ACS_FULL_POP_COL = r""   # 自动找 Full-state employed population（只在下面改权重时用到）

# 改某个 年份 × 州 的覆盖权重：分母 = Full-state 人口 × 新权重。
# 例：那份 xlsx 的 Method 页写 2024 年 Florida 为 statewide，
#     但 State coverage detail 里 2024 Florida 权重是 0.70。
#     确认是全州覆盖的话，填 {(2024, "FL"): 1.0}
ACS_WEIGHT_OVERRIDES: dict[tuple[int, str], float] = {
    # (2024, "FL"): 1.0,
}

# CSV 编码。留空 = 自动识别（依次试 UTF-8 / GBK / Windows-1252）。
# 中文 Windows 上用 Excel「另存为 CSV」存出来的文件通常是 GBK。
ENCODING = ""
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

ACS_YEAR_CANDIDATES = ("year", "acsyear", "surveyyear", "年份", "年")
ACS_STATE_CANDIDATES = ("jurisdiction", "state", "st", "statefips", "stateabbr",
                        "statecode", "州", "州名", "州代码")
ACS_POP_CANDIDATES = ("coverageweightedemployedpopulation",
                      "coverageweightedpopulation",
                      "population", "pop", "pwgtp", "weightedpop",
                      "weightedpopulation", "denominator", "n",
                      "人口", "人数", "分母", "加权人口")
ACS_FULL_POP_CANDIDATES = ("fullstateemployedpopulation", "fullstatepopulation")
ACS_WEIGHT_CANDIDATES = ("coverageweight", "weight", "覆盖权重")
ACS_SOURCE_CANDIDATES = ("acspumssource", "source", "数据来源")

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
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", str(name).lower().lstrip("\ufeff"))


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


ENCODING_CANDIDATES = ("utf-8-sig", "gbk", "cp1252")


def detect_encoding(path: Path, preferred: str = "") -> str:
    """读文件开头几 MB 试解码；指定了 ENCODING 就先试它。都不行就报错说明。"""
    with open(path, "rb") as handle:
        sample = handle.read(4 * 1024 * 1024)
    tried = []
    for enc in ([preferred] if preferred else []) + list(ENCODING_CANDIDATES):
        if enc in tried:
            continue
        tried.append(enc)
        try:
            # final=False：样本末尾切断的半个多字节字符不算错
            codecs.getincrementaldecoder(enc)().decode(sample, final=False)
        except (UnicodeDecodeError, LookupError):
            continue
        return enc
    fail(f"认不出文件编码：\n  {path}\n试过：{', '.join(tried)}。\n"
         "请用 Excel「另存为」-> 「CSV UTF-8（逗号分隔）」重新保存，"
         "或在配置区填 ENCODING。")


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
                   encoding="", chunk_size=200_000):
    """返回 (年份 × 州 死亡数 DataFrame, funnel 列表)。只读需要的列、分块读。"""
    counts: dict[tuple[int, str], float] = {}
    funnel = {"rows_read": 0, "year_unreadable": 0, "year_outside": 0,
              "state_unreadable": 0}
    bad_states: dict[str, int] = {}

    for path in paths:
        file_encoding = detect_encoding(path, encoding)
        header = pd.read_csv(path, nrows=0, encoding=file_encoding).columns
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
        print(f"读取 {path.name}   编码={file_encoding}  年份列={ycol}  州列={scol}"
              + (f"  计数列={count_col}" if count_col else ""))

        usecols = [ycol, scol] + ([count_col] if count_col else [])
        for chunk in pd.read_csv(path, usecols=usecols, dtype=str,
                                 encoding=file_encoding, chunksize=chunk_size):
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

def _excel_table(path: Path, sheet: str, year_col: str, state_col: str,
                 pop_col: str):
    """在 xlsx 里找表：指定了 sheet 就只看它，否则逐张找。
    表头行不一定是第 1 行（前面可能有标题、说明），扫前 30 行，
    找同时含 年份 + 州 + 人口 三列的那一行。"""
    book = pd.ExcelFile(path)
    if sheet and sheet not in book.sheet_names:
        fail(f"ACS_SHEET = {sheet!r} 不在文件里。工作表有：{', '.join(book.sheet_names)}")

    def has(header, given, candidates):
        return given in header if given else find_col(header, candidates) is not None

    for name in ([sheet] if sheet else book.sheet_names):
        grid = pd.read_excel(book, sheet_name=name, header=None, dtype=str)
        for i in range(min(30, len(grid))):
            header = [str(v).strip() if pd.notna(v) else f"_blank{j}"
                      for j, v in enumerate(grid.iloc[i])]
            if (has(header, year_col, ACS_YEAR_CANDIDATES)
                    and has(header, state_col, ACS_STATE_CANDIDATES)
                    and has(header, pop_col, ACS_POP_CANDIDATES)):
                table = grid.iloc[i + 1:].copy()
                table.columns = header
                print(f"ACS 分母：工作表 {name!r}，表头在第 {i + 1} 行")
                return table.reset_index(drop=True)
    fail(f"在 {path.name} 里找不到含 年份 + 州 + 人口 三列的工作表。\n"
         f"工作表有：{', '.join(book.sheet_names)}\n"
         "请在配置区填 ACS_SHEET 和 ACS_YEAR_COL / ACS_STATE_COL / ACS_POP_COL。")


def read_denominator(acs_file="", acs_table=None, *, year_col="", state_col="",
                     pop_col="", full_pop_col="", sheet="", overrides=None,
                     encoding=""):
    """返回 年份 × 州 分母 DataFrame（year, state, population, 以及文件里有的
    coverage_weight / full_state_population / acs_source）；都没填返回 None。"""
    if acs_file and acs_file.strip():
        path = Path(acs_file.strip())
        if not path.is_file():
            fail(f"ACS_FILE 找不到：\n  {path}")
        if path.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
            raw = _excel_table(path, sheet, year_col, state_col, pop_col)
        else:
            raw = pd.read_csv(path, dtype=str,
                              encoding=detect_encoding(path, encoding))
        ycol = year_col or find_col(raw.columns, ACS_YEAR_CANDIDATES)
        scol = state_col or find_col(raw.columns, ACS_STATE_CANDIDATES)
        pcol = pop_col or find_col(raw.columns, ACS_POP_CANDIDATES)
        missing = [n for n, c in (("year", ycol), ("state", scol),
                                  ("population", pcol)) if not c or c not in raw]
        if missing:
            fail(f"ACS_FILE 缺少列：{', '.join(missing)}\n"
                 f"列有：{', '.join(map(str, raw.columns))}\n"
                 "在配置区填 ACS_YEAR_COL / ACS_STATE_COL / ACS_POP_COL。")
        print(f"ACS 分母列：年份={ycol}  州={scol}  人口={pcol}")
        source = pd.DataFrame({"year": raw[ycol], "state": raw[scol],
                               "population": raw[pcol]})
        extras = {
            "coverage_weight": find_col(raw.columns, ACS_WEIGHT_CANDIDATES),
            "full_state_population": full_pop_col
            or find_col(raw.columns, ACS_FULL_POP_CANDIDATES),
            "acs_source": find_col(raw.columns, ACS_SOURCE_CANDIDATES),
        }
        for key, col in extras.items():
            if col and col in raw and col not in (ycol, scol, pcol):
                source[key] = raw[col]
    elif acs_table:
        source = pd.DataFrame(
            [(y, s, p) for y, row in acs_table.items() for s, p in row.items()],
            columns=["year", "state", "population"],
        )
    else:
        return None

    def number(col):
        return pd.to_numeric(source[col].astype(str).str.replace(",", ""),
                             errors="coerce")

    frame = pd.DataFrame({
        "year": source["year"].map(parse_year),
        "state": source["state"].map(parse_state),
        "population": number("population"),
    })
    for key in ("coverage_weight", "full_state_population"):
        if key in source:
            frame[key] = number(key)
    if "acs_source" in source:
        frame["acs_source"] = source["acs_source"]

    # 表格下方的空行、脚注：年份和人口都读不出来的行直接跳过
    filler = frame["year"].isna() & frame["population"].isna()
    frame, source = frame[~filler], source[~filler]
    bad = frame["year"].isna() | frame["state"].isna()
    if bad.any():
        fail("ACS 分母里有认不出的年份或州：\n"
             + source[bad].head(10).to_string(index=False))
    frame = frame.copy()
    frame["year"] = frame["year"].astype(int)
    dup = frame.duplicated(["year", "state"], keep=False)
    if dup.any():
        fail("ACS 分母里同一 年份 × 州 出现了多次：\n"
             + frame[dup].sort_values(["year", "state"]).to_string(index=False))

    for (year, state), weight in (overrides or {}).items():
        abbr = parse_state(state)
        hit = (frame["year"] == int(year)) & (frame["state"] == abbr)
        if not hit.any():
            fail(f"ACS_WEIGHT_OVERRIDES 里的 ({year}, {state!r}) 在分母表里找不到。")
        if "full_state_population" not in frame:
            fail("ACS_WEIGHT_OVERRIDES 需要 Full-state 人口列，分母表里没找到；"
                 "请在配置区填 ACS_FULL_POP_COL。")
        old = frame.loc[hit, "population"].iloc[0]
        frame.loc[hit, "population"] = (frame.loc[hit, "full_state_population"]
                                        * float(weight)).round()
        if "coverage_weight" in frame:
            frame.loc[hit, "coverage_weight"] = float(weight)
        print(f"覆盖权重改写：{year} {abbr} -> {weight}，分母 {old:,.0f} -> "
              f"{frame.loc[hit, 'population'].iloc[0]:,.0f}")

    blank = frame["population"].isna()
    if blank.any():
        print(f"注意：ACS 分母有 {int(blank.sum())} 格人口为空，相关年份的率会留空。")
    return frame.reset_index(drop=True)


# -----------------------------------------------------------------------------
# 计算
# -----------------------------------------------------------------------------

def rate_row(label, year, pool, covered, num, den, scale):
    """pool 里当年被覆盖的州 -> 分子、分母加总成一行。

    缺分母的州让整行的率留空（绝不当 0 加，少算分母）。
    pool 里当年没覆盖的州不算，但它们若在 NVDRS 里有死亡，写进 excluded_* 两列。
    """
    states = sorted(set(pool) & set(covered))
    num_year = num[num["year"] == year]
    by_state = num_year.groupby("state")["deaths"].sum()
    deaths = by_state.reindex(states, fill_value=0).sum()
    den_year = den[(den["year"] == year) & den["state"].isin(states)]
    have = set(den_year.dropna(subset=["population"])["state"])
    lacking = sorted(set(states) - have)
    population = den_year["population"].sum() if not lacking else float("nan")
    per_scale = population / scale
    excluded = by_state[by_state.index.isin(set(pool) - set(states))]
    sources = den_year["acs_source"].dropna().unique() \
        if "acs_source" in den_year else []
    return {
        "table": label,
        "year": year,
        "n_states": len(states),
        "states": " ".join(states),
        "dropped_from_base": " ".join(sorted(set(pool) - set(covered)))
        if label.startswith("A") else "",
        "nvdrs_deaths": int(deaths) if float(deaths).is_integer() else deaths,
        "acs_population": population,
        f"acs_population_div_{scale}": per_scale,
        f"rate_per_{scale}": deaths / per_scale if per_scale else float("nan"),
        "missing_acs_states": " ".join(lacking),
        "zero_death_states": " ".join(s for s in states if by_state.get(s, 0) == 0),
        "excluded_nvdrs_deaths": int(excluded.sum()),
        "excluded_states": " ".join(sorted(excluded.index)),
        "acs_source": " / ".join(sorted(sources)),
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
        acs_year_col="", acs_state_col="", acs_pop_col="", acs_full_pop_col="",
        acs_sheet="", acs_weight_overrides=None, coverage_from="acs",
        base_year=BASE_YEAR, single_year=SINGLE_YEAR, years=tuple(YEARS),
        scale=DENOMINATOR_SCALE, encoding="", chunk_size=200_000) -> dict:
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
    if coverage_from not in ("acs", "nvdrs"):
        fail('COVERAGE_FROM 只能是 "acs" 或 "nvdrs"')

    num, funnel = read_numerator(paths, year_col=year_col, state_col=state_col,
                                 count_col=count_col, encoding=encoding,
                                 chunk_size=chunk_size)
    pd.DataFrame(funnel, columns=["step", "rows"]).to_csv(
        out / "funnel.csv", index=False, encoding="utf-8-sig")

    den = read_denominator(acs_file, acs_table, year_col=acs_year_col,
                           state_col=acs_state_col, pop_col=acs_pop_col,
                           full_pop_col=acs_full_pop_col, sheet=acs_sheet,
                           overrides=acs_weight_overrides, encoding=encoding)
    if den is None:
        template = out / "acs_denominator_template.csv"
        denominator_template(num, base_year, single_year, years).to_csv(
            template, index=False, encoding="utf-8-sig")
        fail("ACS 分母还没填。已生成分母模板：\n"
             f"  {template}\n"
             "把 population 列填好（ACS PUMS 加权人口，原始人数，不用除以 1000），\n"
             "再把路径填进配置区的 ACS_FILE，重新运行。")

    source = den if coverage_from == "acs" else num
    covered = {y: sorted(source.loc[source["year"] == y, "state"].unique())
               for y in years}
    base_states, single_states = covered.get(base_year, []), covered.get(single_year, [])
    where = "ACS 分母表" if coverage_from == "acs" else "NVDRS 数据"
    if not base_states:
        fail(f"{where}里没有 {base_year} 年，定不出表 A 的州范围。")
    if not single_states:
        fail(f"{where}里没有 {single_year} 年，算不了表 B。")

    all_states = sorted(set(num["state"]) | set(den["state"]))
    coverage = pd.DataFrame(
        {y: [int(s in covered[y]) for s in all_states] for y in years},
        index=pd.Index(all_states, name="state"))
    coverage.insert(0, "state_name", coverage.index.map(STATE_NAME))
    coverage[f"in_{base_year}_set"] = coverage.index.isin(base_states).astype(int)
    coverage.reset_index().to_csv(out / "state_coverage.csv", index=False,
                                  encoding="utf-8-sig")

    table_a = pd.DataFrame([
        rate_row(f"A: {base_year} NVDRS states", y, base_states, covered[y],
                 num, den, scale)
        for y in years
    ])
    table_b = pd.DataFrame([
        rate_row(f"B: {single_year} all NVDRS states", single_year,
                 all_states, single_states, num, den, scale)
    ])
    by_state = pd.DataFrame([
        rate_row(f"B: {single_year} by state", single_year, [s], [s],
                 num, den, scale)
        for s in single_states
    ]).drop(columns=["n_states", "table", "dropped_from_base",
                     "excluded_nvdrs_deaths", "excluded_states",
                     "zero_death_states"]).rename(columns={"states": "state"})
    by_state.insert(1, "state_name", by_state["state"].map(STATE_NAME))

    detail = num.merge(den, on=["year", "state"], how="outer")
    detail = detail[detail["year"].isin(years)]
    detail["deaths"] = detail["deaths"].fillna(0)
    detail.insert(2, "state_name", detail["state"].map(STATE_NAME))
    detail.insert(3, "covered", [int(s in covered[y]) for y, s
                                 in zip(detail["year"], detail["state"])])
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
        for _, row in table.iterrows():
            if row["missing_acs_states"]:
                print(f"注意：{msg} {row['year']} 缺 ACS 分母（{row['missing_acs_states']}），"
                      "率已留空。")
            if row["excluded_nvdrs_deaths"]:
                print(f"注意：{msg} {row['year']} 有 {row['excluded_nvdrs_deaths']} 例死亡"
                      f"来自当年不在覆盖名单的州（{row['excluded_states']}），未计入。")
            if row["zero_death_states"]:
                print(f"注意：{msg} {row['year']} 这些覆盖州在 NVDRS 里 0 例："
                      f"{row['zero_death_states']} —— 确认州名/代码是否对得上。")
    if any("5-year" in str(v) for v in table_a["acs_source"]):
        print("注意：有年份的分母来自 ACS 5-year PUMS（2020 年无 1-year），"
              "做趋势比较时要说明，见 acs_source 列。")

    rate_col = f"rate_per_{scale}"
    show = ["year", "n_states", "nvdrs_deaths", "acs_population", rate_col]
    print(f"\n表 A：{years[0]}-{years[-1]} 各年自杀率（{base_year} 年覆盖的 "
          f"{len(base_states)} 个州，每 {scale} 人）")
    print(table_a[show].to_string(index=False))
    print(f"\n表 B：{single_year} 年自杀率（{single_year} 年覆盖的 "
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
    parser.add_argument("--coverage-from", choices=["acs", "nvdrs"], default=None)
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
        acs_pop_col=ACS_POP_COL, acs_full_pop_col=ACS_FULL_POP_COL,
        acs_sheet=ACS_SHEET, acs_weight_overrides=ACS_WEIGHT_OVERRIDES,
        coverage_from=args.coverage_from or COVERAGE_FROM,
        scale=scale,
        encoding=args.encoding or ENCODING,
        chunk_size=CHUNK_SIZE,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
