#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NVDRS 自杀 case 的州分布：统计表 + 两张分布图（2018-2024）。

回答三个问题：

  1. 2018-2024 年 NVDRS 的自杀 case 都来自哪些州？
     —— 州的判断走 `InjuryState`，这一列空白时退回 `SiteID`（站点 FIPS）。
  2. 全部 50 州 + DC 的分布图，没有数据的州画成灰色。
  3. 只画 2018 年 NVDRS 已覆盖的 35 州 + DC（面板 36 个辖区）的分布图，
     没有数据的州同样画成灰色。

州的口径（名字、USPS、FIPS、以及「哪 36 个辖区算 2018 面板」）全部来自
面板文件 `config/panel36_filter_key_2018_2024.csv` 的 `in_panel` 列，
脚本里不另写一份州名单。

输出：

    OUTPUT_DIR/
      state_year_counts.csv          51 个辖区 × 2018-2024 计数（含 0）
      states_with_cases.csv          有 case 的州（问题 1 的答案，按总数排序）
      states_without_cases.csv       没有 case 的州（图上画灰色的那些）
      panel36_state_year_counts.csv  只含 2018 面板 36 个辖区
      state_source_counts.csv        每行的州是从 InjuryState 还是 SiteID 认出来的
      unresolved_state_values.csv    认不出来的州取值（逐个列出，绝不静默丢弃）
      funnel.csv                     读入 -> 保留 的逐级交代
      map_all_states_2018_2024.png       图 2：全部 50 州 + DC
      map_panel36_2018_2024.png          图 3：2018 面板 35 州 + DC
      （MAP_STYLE="both" 时另出 *_grid.png 等面积方块版）

依赖：pandas（统计表）+ matplotlib（画图，缺了只出表并明确提示）。
地图边界用仓库自带的 `assets/us_states_lowres.geojson`，不联网。

用法：把「配置区」的 INPUT_DIR / INPUT_FILES 和 OUTPUT_DIR 填好，然后

    python nvdrs_state_map.py
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent

# =============================================================================
# 配置区 —— 把路径填进下面的空白引号里
# =============================================================================

# 【必填 1】输入。填法 A：目录（读里面所有 .csv）
INPUT_DIR = r""          # 例：r"D:\School_project\Project\NVDRS\Label_year\_all_ages"

# 填法 B：逐个列出（填了就忽略 INPUT_DIR）
INPUT_FILES = [
    r"",                 # 例：r"D:\...\Liu_1191_nvdrs_2024.csv"
]

# 【必填 2】输出目录（不存在会自动创建）
OUTPUT_DIR = r""         # 例：r"D:\School_project\Project\NVDRS\State_map"

# -----------------------------------------------------------------------------
# 以下通常不用改
# -----------------------------------------------------------------------------

# 面板文件：州名 / USPS / FIPS 的对照，以及 2018 面板（in_panel=1）是哪 36 个辖区
PANEL_CSV = str(HERE / "config" / "panel36_filter_key_2018_2024.csv")

# 「2018 面板」取面板文件里哪一年的 in_panel（各年一致，取 2018 只是说清楚口径）
PANEL_YEAR = 2018

# 州界（仓库自带，离线可用）。留空 = 不画地理图，只画方块图
GEOJSON = str(HERE / "assets" / "us_states_lowres.geojson")

# 统计的年份
YEARS = [2018, 2019, 2020, 2021, 2022, 2023, 2024]

# 列名。全部留空 = 自动识别
STATE_COL = r""          # 受伤地州，例：r"InjuryState"
SITE_COL = r""           # 站点 ID（FIPS 编码），例：r"SiteID"
YEAR_COL = r""           # 例：r"IncidentYear"
MANNER_COL = r""         # 死亡方式，例：r"AbstractorAssignedDeathManner"

# 自杀筛选："auto" = 找到死亡方式列就筛，找不到就当输入已经是自杀数据（会提示）
#           "on" = 必须筛（找不到列直接报错）   "off" = 不筛
SUICIDE_FILTER = "auto"

# 死亡方式列里算自杀的取值。留空 = 取值里含 "suicide" 就算
# 数据是编码（1/2/3…）时必须在这里填，例：SUICIDE_VALUES = ["2"]
SUICIDE_VALUES: list[str] = []

# 州的判断顺序：先 InjuryState，空白时退回 SiteID
#   "injury_then_site" / "site_then_injury" / "injury_only" / "site_only"
STATE_SOURCE = "injury_then_site"

# 画图。"geo" = 地理图；"grid" = 等面积方块图（小州也看得见）；"both" = 都出
MAP_STYLE = "geo"

# 配色主题："light" / "dark" / "both"
THEME = "light"

# 图上文字的语言。"auto" = 系统有中文字体就用中文，没有就换英文（避免变成方块豆腐）
#                 "zh" = 强制中文    "en" = 强制英文
FIGURE_LANG = "auto"

# 图上每个州标 USPS 缩写 + case 数（关掉就只标缩写）
ANNOTATE_COUNTS = True

# 分档数（颜色深浅的档位，2-6）
N_BINS = 5

DPI = 200
ENCODING = "utf-8"
CHUNK_SIZE = 50_000

# =============================================================================
# 配置区结束
# =============================================================================

# ---- 列名候选 ---------------------------------------------------------------

STATE_COL_CANDIDATES = (
    "injurystate", "injuredstate", "stateofinjury", "injurystatename",
    "incidentstate", "state",
)

# 长得像州但【不是】受伤地的列。跨州案例里（在 A 州受伤、送到 B 州死亡、
# 家住 C 州）三者不同，拿它们顶替会把 case 记到别的州，所以只在报错里点名。
WRONG_STATE_COLS = {
    "deathstate": "死亡地州",
    "stateofdeath": "死亡地州",
    "residencestate": "居住地州",
    "stateofresidence": "居住地州",
    "birthstate": "出生地州",
    "injurycounty": "受伤地县",
    "injurycity": "受伤地市",
}

SITE_COL_CANDIDATES = ("siteid", "site", "sitecode", "sitefips", "sitenumber")

YEAR_COL_CANDIDATES = ("incidentyear", "incyear", "yearofincident", "incidentyearc")

# 同理：这些年份列不是 incident year
WRONG_YEAR_COLS = {
    "deathyear": "死亡年份",
    "yearofdeath": "死亡年份",
    "injuryyear": "受伤年份",
    "yearofinjury": "受伤年份",
    "filingyear": "归档年份",
    "reportyear": "报告年份",
    "abstractionyear": "摘录年份",
    "birthyear": "出生年份",
}

MANNER_COL_CANDIDATES = (
    "abstractorassigneddeathmanner", "mannerofdeath", "deathmanner",
    "manner", "mannerofdeathabstractor", "cmemannerofdeath",
)

# 目录扫描时跳过的非样本文件（多半是上一步脚本自己的输出）
SKIP_NAME_PATTERNS = (
    re.compile(r"^summary", re.I),
    re.compile(r"^file_map", re.I),
    re.compile(r"^funnel", re.I),
    re.compile(r"distribution", re.I),
    re.compile(r"_excluded", re.I),
    re.compile(r"breakdown", re.I),
    re.compile(r"^state_", re.I),
    re.compile(r"^panel36_", re.I),
    re.compile(r"^unresolved_", re.I),
    re.compile(r"filter_key", re.I),
)

BLANK = "BLANK"
UNPARSEABLE = "UNPARSEABLE"
UNRESOLVED = "UNRESOLVED"

MISSING_TOKENS = {"", ".", "-", "--", "nan", "none", "null", "<na>"}
UNKNOWN_TOKENS = {
    "unknown", "unk", "not available", "not specified", "not stated",
    "missing", "n/a", "na", "other", "refused", "9999", "99",
}

# ---- 配色（dataviz 参考调色板：单色蓝顺序色阶 + 中性灰） ----------------------

THEMES = {
    "light": {
        "surface": "#fcfcfb",
        "ink": "#0b0b0b",
        "ink2": "#52514e",
        "ink3": "#807e78",
        # 顺序色阶 blue 100/200/300/450/600（浅 -> 深 = 少 -> 多）
        "ramp": ["#cde2fb", "#9ec5f4", "#6da7ec", "#2a78d6", "#184f95", "#0d366b"],
        "no_data": "#d3d2cd",      # 范围内但没有 case
        "out_of_scope": "#ecebe8",  # 不在这张图的范围里（图 3 的非面板州）
        "edge": "#fcfcfb",
        "edge_dark": "#8d8b85",
        "label_on_fill": "#ffffff",
    },
    "dark": {
        "surface": "#1a1a19",
        "ink": "#ffffff",
        "ink2": "#c3c2b7",
        "ink3": "#8f8e86",
        "ramp": ["#184f95", "#256abf", "#3987e5", "#6da7ec", "#b7d3f6", "#cde2fb"],
        "no_data": "#3a3a37",
        "out_of_scope": "#272725",
        "edge": "#1a1a19",
        "edge_dark": "#6c6b66",
        "label_on_fill": "#0b0b0b",
    },
}

# 方块图的格子位置：(行, 列)，行从上往下。覆盖 50 州 + DC，各占一格
GRID_LAYOUT = {
    "AK": (0, 0), "ME": (0, 10),
    "VT": (1, 9), "NH": (1, 10),
    "WA": (2, 0), "ID": (2, 1), "MT": (2, 2), "ND": (2, 3), "MN": (2, 4),
    "WI": (2, 5), "MI": (2, 6), "NY": (2, 7), "MA": (2, 8), "RI": (2, 9),
    "OR": (3, 0), "NV": (3, 1), "WY": (3, 2), "SD": (3, 3), "IA": (3, 4),
    "IL": (3, 5), "IN": (3, 6), "OH": (3, 7), "PA": (3, 8), "NJ": (3, 9),
    "CT": (3, 10),
    "CA": (4, 0), "UT": (4, 1), "CO": (4, 2), "NE": (4, 3), "MO": (4, 4),
    "KY": (4, 5), "WV": (4, 6), "VA": (4, 7), "MD": (4, 8), "DE": (4, 9),
    "AZ": (5, 1), "NM": (5, 2), "KS": (5, 3), "AR": (5, 4), "TN": (5, 5),
    "NC": (5, 6), "SC": (5, 7), "DC": (5, 8),
    "OK": (6, 3), "LA": (6, 4), "MS": (6, 5), "AL": (6, 6), "GA": (6, 7),
    "HI": (7, 0), "TX": (7, 3), "FL": (7, 8),
}

# 地理图上挤在东北角、框里放不下名字的辖区：标签移到图外，用引线连过去
OFFSET_LABELS = ("VT", "NH", "MA", "RI", "CT", "NJ", "DE", "MD", "DC")

# 图上文字：中文字体找不到时整张图换英文，而不是画出一排方块
CJK_FONTS = (
    "Microsoft YaHei", "SimHei", "SimSun", "PingFang SC", "Heiti SC",
    "Noto Sans CJK SC", "Noto Sans SC", "Source Han Sans SC", "Source Han Sans CN",
    "WenQuanYi Zen Hei", "WenQuanYi Micro Hei", "Arial Unicode MS",
)

FIGURE_TEXT = {
    "zh": {
        "title_all": "NVDRS 自杀 case 的州分布（{years}）",
        "subtitle_all": "全部 {n} 个辖区（{composition}）；灰色 = 该州没有 case",
        "title_panel": "NVDRS 自杀 case 的州分布 · {panel_year} 面板（{years}）",
        "subtitle_panel": ("只看 {panel_year} 年 NVDRS 已覆盖的 {n} 个辖区（{composition}）；"
                           "灰色 = 没有 case 或不在面板"),
        "legend_title": "自杀 case 数（{years}）",
        "no_data": "无数据（0 例）",
        "out_of_scope": "不在 {panel_year} 面板",
        "footnote": ("数据：NVDRS 自杀 case，{years}；州 = InjuryState（空白时用 SiteID）。"
                     "州名单与 {panel_year} 面板口径来自 {panel_file}。合计 {total} 例。"),
        "states": "州",
        "plus_dc": "{n} 州 + DC",
        "only_states": "{n} 州",
    },
    "en": {
        "title_all": "NVDRS suicide cases by state ({years})",
        "subtitle_all": "All {n} jurisdictions ({composition}); grey = no cases",
        "title_panel": "NVDRS suicide cases by state · {panel_year} panel ({years})",
        "subtitle_panel": ("Only the {n} jurisdictions NVDRS covered in {panel_year} "
                           "({composition}); grey = no cases or outside the panel"),
        "legend_title": "Suicide cases ({years})",
        "no_data": "No data (0 cases)",
        "out_of_scope": "Outside the {panel_year} panel",
        "footnote": ("NVDRS suicide cases, {years}; state = InjuryState (SiteID when blank). "
                     "Jurisdiction list and {panel_year} panel from {panel_file}. "
                     "{total} cases in total."),
        "states": "states",
        "plus_dc": "{n} states + DC",
        "only_states": "{n} states",
    },
}


# =============================================================================
# 小工具
# =============================================================================


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


def fail(message: str) -> "NoReturn":  # noqa: F821
    print(f"\n错误：{message}\n", file=sys.stderr)
    sys.exit(2)


def parse_year(value) -> "int | str":
    """取出 4 位年份，或 BLANK / UNPARSEABLE。两位数年份不猜。"""
    text = norm_text(value)
    if not text:
        return BLANK
    bare = re.fullmatch(r"(\d{4})(?:\.0+)?", text)
    if bare:
        return int(bare.group(1))
    years = re.findall(r"(?<!\d)(1[89]\d{2}|20\d{2})(?!\d)", text)
    if len(set(years)) == 1:
        return int(years[0])
    return UNPARSEABLE


def look_alike_note(columns, wrong: dict, what: str) -> str:
    """把「长得像但不是」的列点名说清楚，别让人以为脚本没看见它们。"""
    found = {
        original: wrong[norm_colname(original)]
        for original in columns
        if norm_colname(original) in wrong
    }
    if not found:
        return ""
    return (
        f"\n找到了这些像{what}的列，但它们【不是】要的那一列，顶替会算错：\n  "
        + "\n  ".join(f"{c}（{why}）" for c, why in found.items())
    )


def find_column(columns, candidates, override, *, what, wrong=None, required=True):
    """按候选名找列。override 优先；找不到时把「像但不是」的列点名说清楚。"""
    wrong = wrong or {}
    if override and override.strip():
        name = override.strip()
        if name not in columns:
            fail(f"指定的{what}列 {name!r} 不在文件里。\n"
                 f"该文件的列有：{', '.join(map(str, columns))}")
        return name

    normalised = {norm_colname(c): c for c in columns}
    for cand in candidates:
        if cand in normalised:
            return normalised[cand]
    for cand in candidates:
        for norm, original in normalised.items():
            if cand in norm and norm not in wrong:
                return original

    if not required:
        return ""

    fail(
        f"找不到{what}列（找过：{', '.join(candidates)}）。"
        f"{look_alike_note(columns, wrong, what)}\n"
        f"该文件的列有：{', '.join(map(str, columns))}\n"
        f"请在配置区手动指定列名。"
    )


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
            print("已跳过以下非样本文件（汇总表 / 本脚本的输出）：")
            for name in skipped:
                print(f"  - {name}")
        if not paths:
            fail(f"目录里的 .csv 全部被当作非样本文件跳过了：\n  {directory}\n"
                 "请改用 INPUT_FILES 逐个列出。")
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


# =============================================================================
# 面板文件：州的口径全部从这里来
# =============================================================================


class Panel:
    """面板文件读出来的州名单：名字 / USPS / FIPS 对照 + 2018 面板成员。"""

    def __init__(self, frame: pd.DataFrame, panel_year: int):
        self.frame = frame
        self.panel_year = panel_year

        key = frame.drop_duplicates("usps").sort_values("jurisdiction")
        self.usps_order: list[str] = list(key["usps"])
        self.name_of: dict[str, str] = dict(zip(key["usps"], key["jurisdiction"]))
        self.fips_of: dict[str, int] = dict(zip(key["usps"], key["state_fips"]))

        self.by_usps = {u.lower(): u for u in self.usps_order}
        self.by_name = {norm_text(n): u for u, n in self.name_of.items()}
        self.by_fips = {int(f): u for u, f in self.fips_of.items()}

        year_rows = frame[frame["year"] == panel_year]
        if year_rows.empty:
            fail(f"面板文件里没有 year={panel_year} 的行，无法确定 2018 面板是哪些州。")
        self.panel_usps: set[str] = set(year_rows.loc[year_rows["in_panel"] == 1, "usps"])

    def __len__(self) -> int:
        return len(self.usps_order)


def load_panel(path: str, panel_year: int = PANEL_YEAR) -> Panel:
    p = Path(path)
    if not p.is_file():
        fail(f"找不到面板文件：\n  {p}\n"
             "它决定州的口径（州名 / USPS / FIPS / 2018 面板成员），必须有。")
    frame = pd.read_csv(p)
    need = {"year", "jurisdiction", "usps", "state_fips", "in_panel"}
    missing = need - set(frame.columns)
    if missing:
        fail(f"面板文件缺列：{', '.join(sorted(missing))}\n  {p}")

    frame = frame.copy()
    frame["year"] = pd.to_numeric(frame["year"], errors="coerce").astype("Int64")
    frame["state_fips"] = pd.to_numeric(frame["state_fips"], errors="coerce").astype(int)
    frame["in_panel"] = pd.to_numeric(frame["in_panel"], errors="coerce").fillna(0).astype(int)
    frame["usps"] = frame["usps"].astype(str).str.strip().str.upper()
    frame["jurisdiction"] = frame["jurisdiction"].astype(str).str.strip()

    varies = frame.groupby("usps")["in_panel"].nunique()
    if (varies > 1).any():
        moving = ", ".join(sorted(varies[varies > 1].index))
        print(f"  注意：这些辖区的 in_panel 在各年之间不一致：{moving}\n"
              f"        面板成员按 year={panel_year} 那一年取。")
    return Panel(frame, panel_year)


class StateResolver:
    """把一行里的州取值（名字 / USPS / FIPS）认成 USPS 缩写。

    认不出来的取值不丢，原样记下来写进 unresolved_state_values.csv。
    """

    def __init__(self, panel: Panel):
        self.panel = panel
        self._cache: dict[str, str] = {}

    def resolve(self, value) -> str:
        text = norm_text(value)
        if not text:
            return BLANK
        hit = self._cache.get(text)
        if hit is not None:
            return hit
        self._cache[text] = result = self._lookup(text)
        return result

    def _lookup(self, text: str) -> str:
        if text in UNKNOWN_TOKENS:
            return BLANK
        name = self.panel.by_name.get(text)
        if name:
            return name
        if len(text) == 2 and text in self.panel.by_usps:
            return self.panel.by_usps[text]
        digits = re.fullmatch(r"0*(\d{1,2})(?:\.0+)?", text)
        if digits:
            return self.panel.by_fips.get(int(digits.group(1)), UNRESOLVED)

        # "Alabama (AL)" / "AL - Alabama" / "01 - Alabama" 这类带修饰的写法：
        # 去掉括号内容、按分隔符拆开，每一段单独试
        pieces = [re.sub(r"\([^)]*\)", " ", text)]
        pieces += re.split(r"[-–—|/,:()]", text)
        for piece in pieces:
            piece = re.sub(r"\s+", " ", piece).strip()
            if not piece:
                continue
            if piece in self.panel.by_name:
                return self.panel.by_name[piece]
            if len(piece) == 2 and piece in self.panel.by_usps:
                return self.panel.by_usps[piece]
        return UNRESOLVED


# =============================================================================
# 统计
# =============================================================================


def is_suicide(value, allowed: set[str]) -> bool:
    text = norm_text(value)
    if not text:
        return False
    if allowed:
        return text in allowed
    return "suicide" in text


def tally(
    files: list[Path],
    panel: Panel,
    *,
    years: list[int],
    state_col: str,
    site_col: str,
    year_col: str,
    manner_col: str,
    suicide_filter: str,
    suicide_values: list[str],
    state_source: str,
    encoding: str,
    chunk_size: int,
) -> dict:
    """逐块读文件，数出 州 × 年 的自杀 case 数，并交代每一行的去向。"""
    resolver = StateResolver(panel)
    allowed = {norm_text(v) for v in suicide_values if norm_text(v)}

    counts: Counter[tuple[str, int]] = Counter()
    source_counts = Counter()
    unresolved_values: Counter[str] = Counter()
    year_values: Counter[str] = Counter()

    stats = dict(
        rows_read=0, dropped_year=0, dropped_blank_year=0, dropped_bad_year=0,
        dropped_not_suicide=0, dropped_blank_state=0, dropped_unresolved_state=0,
        kept=0,
    )
    used_columns: list[dict] = []
    wanted = set(years)

    for path in files:
        header = pd.read_csv(path, dtype=str, nrows=0, encoding=encoding)
        columns = list(header.columns)

        want_injury = state_source in ("injury_then_site", "site_then_injury", "injury_only")
        want_site = state_source in ("injury_then_site", "site_then_injury", "site_only")

        injury = find_column(
            columns, STATE_COL_CANDIDATES, state_col, what="受伤地州",
            wrong=WRONG_STATE_COLS, required=(state_source == "injury_only"),
        ) if want_injury else ""
        site = find_column(
            columns, SITE_COL_CANDIDATES, site_col, what="站点 ID",
            required=(state_source == "site_only"),
        ) if want_site else ""

        if not injury and not site:
            fail(
                f"{path.name} 里既找不到 InjuryState 也找不到 SiteID，无法判断州。"
                f"{look_alike_note(columns, WRONG_STATE_COLS, '受伤地州')}\n"
                f"该文件的列有：{', '.join(columns)}\n"
                "请在配置区填 STATE_COL 或 SITE_COL。"
            )

        order = [c for c in (
            (injury, site) if state_source != "site_then_injury" else (site, injury)
        ) if c]

        year_column = find_column(
            columns, YEAR_COL_CANDIDATES, year_col, what="incident year",
            wrong=WRONG_YEAR_COLS,
        )
        manner = ""
        if suicide_filter != "off":
            manner = find_column(
                columns, MANNER_COL_CANDIDATES, manner_col, what="死亡方式",
                required=(suicide_filter == "on"),
            )
            if not manner and suicide_filter == "auto":
                print(f"  注意：{path.name} 里没有死亡方式列，本文件按「已经只剩自杀 case」"
                      f"处理，不再筛。若不是，请先筛好或填 MANNER_COL。")

        used_columns.append({
            "file": str(path), "state_columns": " -> ".join(order),
            "year_column": year_column, "manner_column": manner or "（未筛）",
        })
        print(f"  [{path.name}] 州：{' -> '.join(order)}；年份：{year_column}；"
              f"死亡方式：{manner or '（未筛）'}")

        usecols = [c for c in dict.fromkeys(order + [year_column] + ([manner] if manner else []))]
        for chunk in pd.read_csv(
            path, dtype=str, usecols=usecols, encoding=encoding, chunksize=chunk_size,
        ):
            stats["rows_read"] += len(chunk)

            parsed_year = chunk[year_column].map(parse_year)
            for value, n in parsed_year.astype(str).value_counts().items():
                year_values[value] += int(n)
            in_year = parsed_year.map(lambda v: isinstance(v, int) and v in wanted)
            stats["dropped_blank_year"] += int((parsed_year == BLANK).sum())
            stats["dropped_bad_year"] += int((parsed_year == UNPARSEABLE).sum())
            stats["dropped_year"] += int((~in_year).sum())
            chunk = chunk.loc[in_year]
            parsed_year = parsed_year.loc[in_year]
            if chunk.empty:
                continue

            if manner:
                keep = chunk[manner].map(lambda v: is_suicide(v, allowed))
                stats["dropped_not_suicide"] += int((~keep).sum())
                chunk = chunk.loc[keep]
                parsed_year = parsed_year.loc[keep]
                if chunk.empty:
                    continue

            primary = chunk[order[0]].map(resolver.resolve)
            source = pd.Series(order[0], index=chunk.index)
            if len(order) > 1:
                need_fallback = primary.isin([BLANK, UNRESOLVED])
                if need_fallback.any():
                    backup = chunk.loc[need_fallback, order[1]].map(resolver.resolve)
                    better = backup != BLANK
                    primary.loc[backup.index[better]] = backup[better]
                    source.loc[backup.index[better]] = order[1]

            good = ~primary.isin([BLANK, UNRESOLVED])
            stats["dropped_blank_state"] += int((primary == BLANK).sum())
            stats["dropped_unresolved_state"] += int((primary == UNRESOLVED).sum())
            for col in order:
                bad = chunk.loc[primary == UNRESOLVED, col]
                for raw, n in bad[bad.notna()].value_counts().items():
                    unresolved_values[f"{col}={raw}"] += int(n)

            for (usps, year), n in pd.Series(
                list(zip(primary[good], parsed_year[good]))
            ).value_counts().items():
                counts[(usps, int(year))] += int(n)
            for src, n in source[good].value_counts().items():
                source_counts[src] += int(n)
            stats["kept"] += int(good.sum())

    return {
        "counts": counts,
        "source_counts": source_counts,
        "unresolved_values": unresolved_values,
        "year_values": year_values,
        "stats": stats,
        "used_columns": used_columns,
    }


def build_tables(counts: Counter, panel: Panel, years: list[int]) -> pd.DataFrame:
    """51 个辖区 × 各年的计数表（没有 case 的州也留一行，值为 0）。"""
    rows = []
    for usps in panel.usps_order:
        row = {
            "jurisdiction": panel.name_of[usps],
            "usps": usps,
            "state_fips": panel.fips_of[usps],
            "in_panel_2018": int(usps in panel.panel_usps),
        }
        total = 0
        for year in years:
            n = int(counts.get((usps, year), 0))
            row[str(year)] = n
            total += n
        row["TOTAL"] = total
        row["years_with_cases"] = sum(1 for y in years if counts.get((usps, y), 0))
        rows.append(row)
    frame = pd.DataFrame(rows)
    grand = int(frame["TOTAL"].sum())
    frame["share_pct"] = (frame["TOTAL"] / grand * 100).round(2) if grand else 0.0
    return frame.sort_values("jurisdiction").reset_index(drop=True)


# =============================================================================
# 分档（颜色深浅）
# =============================================================================


def make_bins(values: list[int], n_bins: int) -> list[tuple[int, int]]:
    """把有 case 的州按分位数切成若干档，返回 [(下界, 上界), ...]（都是闭区间）。"""
    positive = sorted(v for v in values if v > 0)
    if not positive:
        return []
    unique = sorted(set(positive))
    n_bins = max(2, min(int(n_bins), 6))
    if len(unique) <= n_bins:
        return [(v, v) for v in unique]

    edges: list[int] = []
    for i in range(1, n_bins):
        q = positive[min(len(positive) - 1, int(round(i / n_bins * len(positive))))]
        edges.append(int(q))
    cuts: list[int] = []
    for e in edges:
        if e > positive[0] and (not cuts or e > cuts[-1]):
            cuts.append(e)

    bins, low = [], positive[0]
    for cut in cuts:
        bins.append((low, cut - 1))
        low = cut
    bins.append((low, positive[-1]))
    return [(lo, hi) for lo, hi in bins if lo <= hi]


def bin_index(value: int, bins: list[tuple[int, int]]) -> int:
    for i, (lo, hi) in enumerate(bins):
        if lo <= value <= hi:
            return i
    return len(bins) - 1


def bin_label(lo: int, hi: int) -> str:
    return f"{lo:,}" if lo == hi else f"{lo:,}–{hi:,}"


def ramp_colors(ramp: list[str], k: int) -> list[str]:
    """从 6 档色阶里挑 k 个，浅 -> 深均匀取。"""
    if k <= 1:
        return [ramp[3]]
    step = (len(ramp) - 1) / (k - 1)
    return [ramp[int(round(i * step))] for i in range(k)]


# =============================================================================
# 地图
# =============================================================================


def load_geometry(path: str) -> dict[str, list[list[list[tuple[float, float]]]]]:
    """读 GeoJSON，返回 {USPS: [多边形, ...]}，每个多边形 = [外环, 洞...]。"""
    p = Path(path)
    if not p.is_file():
        fail(f"找不到州界文件：\n  {p}\n"
             "仓库自带 assets/us_states_lowres.geojson；也可以把 GEOJSON 留空只画方块图。")
    data = json.loads(p.read_text(encoding="utf-8"))
    shapes: dict[str, list] = {}
    for feature in data.get("features", []):
        usps = str(feature.get("properties", {}).get("usps", "")).upper()
        geom = feature.get("geometry") or {}
        coords = geom.get("coordinates")
        if not usps or not coords:
            continue
        polys = [coords] if geom.get("type") == "Polygon" else coords
        shapes[usps] = [[[(float(x), float(y)) for x, y in ring] for ring in poly]
                        for poly in polys]
    if not shapes:
        fail(f"州界文件里没读到任何州：\n  {p}")
    return shapes


def albers(lon: float, lat: float, lon0: float, lat0: float, p1: float, p2: float):
    """Albers 等积圆锥投影（画州级面量图够用）。"""
    lon, lat = math.radians(lon), math.radians(lat)
    lon0, lat0 = math.radians(lon0), math.radians(lat0)
    p1, p2 = math.radians(p1), math.radians(p2)
    n = 0.5 * (math.sin(p1) + math.sin(p2))
    if abs(n) < 1e-12:
        n = 1e-12
    c = math.cos(p1) ** 2 + 2 * n * math.sin(p1)
    rho0 = math.sqrt(max(c - 2 * n * math.sin(lat0), 0)) / n
    rho = math.sqrt(max(c - 2 * n * math.sin(lat), 0)) / n
    theta = n * (lon - lon0)
    return rho * math.sin(theta), rho0 - rho * math.cos(theta)


PROJECTIONS = {
    "conus": dict(lon0=-96.0, lat0=37.5, p1=29.5, p2=45.5),
    "AK": dict(lon0=-152.0, lat0=60.0, p1=55.0, p2=65.0),
    "HI": dict(lon0=-157.0, lat0=20.0, p1=8.0, p2=18.0),
}


def bbox(polys) -> tuple[float, float, float, float]:
    xs = [x for poly in polys for ring in poly for x, _ in ring]
    ys = [y for poly in polys for ring in poly for _, y in ring]
    return min(xs), min(ys), max(xs), max(ys)


def project_all(shapes: dict) -> dict[str, list]:
    """CONUS 用一套投影；AK / HI 各自投影后缩放平移到左下角（和常见做法一致）。"""
    projected: dict[str, list] = {}
    for usps, polys in shapes.items():
        params = PROJECTIONS.get(usps, PROJECTIONS["conus"])
        projected[usps] = [
            [[albers(lon, lat, **params) for lon, lat in ring] for ring in poly]
            for poly in polys
        ]

    conus = [p for u, p in projected.items() if u not in ("AK", "HI")]
    if not conus:
        return projected
    cx0, cy0, cx1, cy1 = bbox([poly for state in conus for poly in state])
    width, height = cx1 - cx0, cy1 - cy0

    def place(usps: str, target_width: float, x: float, y: float) -> None:
        if usps not in projected:
            return
        x0, y0, x1, y1 = bbox(projected[usps])
        scale = target_width / max(x1 - x0, 1e-9)
        projected[usps] = [
            [[(x + (px - x0) * scale, y + (py - y0) * scale) for px, py in ring]
             for ring in poly]
            for poly in projected[usps]
        ]

    place("AK", 0.30 * width, cx0 - 0.02 * width, cy0 - 0.11 * height)
    place("HI", 0.14 * width, cx0 + 0.33 * width, cy0 - 0.07 * height)
    return projected


def polygon_centroid(poly) -> tuple[float, float]:
    """外环的形心（面积加权）；退化时取顶点平均。"""
    ring = poly[0]
    area = cx = cy = 0.0
    for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1]):
        cross = x0 * y1 - x1 * y0
        area += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(area) < 1e-12:
        return (sum(x for x, _ in ring) / len(ring), sum(y for _, y in ring) / len(ring))
    area *= 0.5
    return cx / (6 * area), cy / (6 * area)


def largest_polygon(polys):
    def size(poly):
        x0, y0, x1, y1 = bbox([poly])
        return (x1 - x0) * (y1 - y0)
    return max(polys, key=size)


def _mpl():
    """延迟导入 matplotlib（没装就只出表）。"""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import PathPatch, Rectangle
        from matplotlib.path import Path as MplPath
        return plt, PathPatch, Rectangle, MplPath
    except ImportError:
        return None


def pick_figure_language(preference: str) -> tuple[str, str]:
    """选图上文字的语言，顺带把中文字体挂上。

    系统里找不到中文字体时用英文 —— 画一排方块豆腐比英文更难看懂。
    返回 (语言, 用上的字体名或说明)。
    """
    parts = _mpl()
    if parts is None:
        return (preference if preference in ("zh", "en") else "en"), "（没装 matplotlib）"
    if preference == "en":
        return "en", "（配置指定英文）"

    from matplotlib import font_manager, rcParams
    available = {f.name for f in font_manager.fontManager.ttflist}
    found = next((name for name in CJK_FONTS if name in available), "")
    if found:
        rcParams["font.sans-serif"] = [found] + list(rcParams.get("font.sans-serif", []))
        rcParams["axes.unicode_minus"] = False
        return "zh", found
    if preference == "zh":
        return "zh", "（没找到中文字体，图上中文可能显示成方块）"
    return "en", "（没找到中文字体，图上文字改用英文）"


def _legend_handles(Rectangle, bins, colors, theme, extra: list[tuple[str, str]]):
    handles, labels = [], []
    for (lo, hi), color in zip(bins, colors):
        handles.append(Rectangle((0, 0), 1, 1, facecolor=color,
                                 edgecolor=theme["edge_dark"], linewidth=0.4))
        labels.append(bin_label(lo, hi))
    for label, color in extra:
        handles.append(Rectangle((0, 0), 1, 1, facecolor=color,
                                 edgecolor=theme["edge_dark"], linewidth=0.4))
        labels.append(label)
    return handles, labels


def draw_geo_map(
    counts_by_state: dict[str, int],
    scope: set[str] | None,
    *,
    shapes: dict,
    panel: Panel,
    title: str,
    subtitle: str,
    footnote: str,
    legend: dict[str, str],
    out_path: Path,
    theme_name: str,
    bins: list[tuple[int, int]],
    annotate: bool,
    dpi: int,
) -> Path | None:
    parts = _mpl()
    if parts is None:
        return None
    plt, PathPatch, Rectangle, MplPath = parts
    theme = THEMES[theme_name]
    colors = ramp_colors(theme["ramp"], len(bins)) if bins else []

    projected = project_all(shapes)
    fig, ax = plt.subplots(figsize=(13.5, 8.6), dpi=dpi)
    fig.patch.set_facecolor(theme["surface"])
    ax.set_facecolor(theme["surface"])

    label_points: dict[str, tuple[float, float]] = {}
    for usps, polys in projected.items():
        n = int(counts_by_state.get(usps, 0))
        in_scope = scope is None or usps in scope
        if not in_scope:
            face, text_color = theme["out_of_scope"], theme["ink3"]
        elif n <= 0:
            face, text_color = theme["no_data"], theme["ink2"]
        else:
            idx = bin_index(n, bins)
            face = colors[idx]
            text_color = theme["label_on_fill"] if idx >= len(colors) - 2 else theme["ink"]

        for poly in polys:
            vertices, codes = [], []
            for ring in poly:
                vertices.extend(ring + [ring[0]])
                codes.extend([MplPath.MOVETO] + [MplPath.LINETO] * (len(ring) - 1)
                             + [MplPath.CLOSEPOLY])
            ax.add_patch(PathPatch(
                MplPath(vertices, codes), facecolor=face,
                edgecolor=theme["edge"], linewidth=0.7, zorder=2,
            ))
        label_points[usps] = polygon_centroid(largest_polygon(polys))
        if usps not in OFFSET_LABELS:
            text = usps if not in_scope else (f"{usps}\n{n:,}" if annotate else usps)
            ax.text(*label_points[usps], text, ha="center", va="center",
                    fontsize=7.0, linespacing=1.15, color=text_color, zorder=4)

    x0, y0, x1, y1 = bbox([poly for polys in projected.values() for poly in polys])
    pad_x, pad_y = 0.03 * (x1 - x0), 0.05 * (y1 - y0)
    ax.set_xlim(x0 - pad_x, x1 + pad_x + 0.20 * (x1 - x0))
    ax.set_ylim(y0 - pad_y, y1 + pad_y)
    ax.set_aspect("equal")
    ax.axis("off")

    # 东北角挤在一起的几个辖区：框里放不下，标签移到右侧，画一条细引线连回去
    present = sorted((u for u in OFFSET_LABELS if u in label_points),
                     key=lambda u: -label_points[u][1])
    if present:
        lx = x1 + 0.045 * (x1 - x0)
        top = max(label_points[u][1] for u in present) + 0.10 * (y1 - y0)
        gap = 0.058 * (y1 - y0)
        for i, usps in enumerate(present):
            n = int(counts_by_state.get(usps, 0))
            in_scope = scope is None or usps in scope
            if not in_scope:
                text, color = f"{usps}  —", theme["ink3"]
            elif annotate:
                text, color = f"{usps}  {n:,}", theme["ink2"]
            else:
                text, color = usps, theme["ink2"]
            ax.annotate(
                text, xy=label_points[usps], xytext=(lx, top - i * gap),
                ha="left", va="center", fontsize=7.6, color=color, zorder=5,
                arrowprops=dict(arrowstyle="-", color=theme["ink3"],
                                linewidth=0.5, shrinkA=0, shrinkB=1),
            )

    extra = [(legend["no_data"], theme["no_data"])]
    if scope is not None:
        extra.append((legend["out_of_scope"], theme["out_of_scope"]))
    handles, labels = _legend_handles(Rectangle, bins, colors, theme, extra)
    box = ax.legend(
        handles, labels, loc="lower right", frameon=False, ncol=1,
        title=legend["title"], fontsize=8.5, title_fontsize=9,
        labelcolor=theme["ink2"], borderpad=0.8, labelspacing=0.55,
    )
    box.get_title().set_color(theme["ink2"])

    _finish(fig, ax, theme, title, subtitle, footnote)
    fig.savefig(out_path, facecolor=theme["surface"], bbox_inches="tight")
    plt.close(fig)
    return out_path


def draw_grid_map(
    counts_by_state: dict[str, int],
    scope: set[str] | None,
    *,
    panel: Panel,
    title: str,
    subtitle: str,
    footnote: str,
    legend: dict[str, str],
    out_path: Path,
    theme_name: str,
    bins: list[tuple[int, int]],
    annotate: bool,
    dpi: int,
) -> Path | None:
    """等面积方块图：每州一格，小州不会被大州压掉。"""
    parts = _mpl()
    if parts is None:
        return None
    plt, PathPatch, Rectangle, MplPath = parts
    theme = THEMES[theme_name]
    colors = ramp_colors(theme["ramp"], len(bins)) if bins else []

    fig, ax = plt.subplots(figsize=(12.4, 8.2), dpi=dpi)
    fig.patch.set_facecolor(theme["surface"])
    ax.set_facecolor(theme["surface"])

    n_rows = max(r for r, _ in GRID_LAYOUT.values()) + 1
    n_cols = max(c for _, c in GRID_LAYOUT.values()) + 1
    for usps, (row, col) in GRID_LAYOUT.items():
        if usps not in panel.name_of:
            continue
        n = int(counts_by_state.get(usps, 0))
        in_scope = scope is None or usps in scope
        if not in_scope:
            face, text_color = theme["out_of_scope"], theme["ink3"]
        elif n <= 0:
            face, text_color = theme["no_data"], theme["ink2"]
        else:
            idx = bin_index(n, bins)
            face = colors[idx]
            text_color = theme["label_on_fill"] if idx >= len(colors) - 2 else theme["ink"]

        x, y = col, n_rows - row
        ax.add_patch(Rectangle(
            (x + 0.03, y + 0.03), 0.94, 0.94, facecolor=face,
            edgecolor=theme["surface"], linewidth=1.6, zorder=2,
        ))
        label = usps if not in_scope else (f"{usps}\n{n:,}" if annotate else usps)
        ax.text(x + 0.5, y + 0.5, label, ha="center", va="center",
                fontsize=9.5, linespacing=1.3, color=text_color, zorder=3)

    ax.set_xlim(-0.3, n_cols + 0.3)
    ax.set_ylim(-0.3, n_rows + 1.3)
    ax.set_aspect("equal")
    ax.axis("off")

    extra = [(legend["no_data"], theme["no_data"])]
    if scope is not None:
        extra.append((legend["out_of_scope"], theme["out_of_scope"]))
    handles, labels = _legend_handles(Rectangle, bins, colors, theme, extra)
    box = ax.legend(
        handles, labels, loc="lower left", frameon=False, ncol=2,
        title=legend["title"], fontsize=8.5, title_fontsize=9,
        labelcolor=theme["ink2"], borderpad=0.8, labelspacing=0.5,
    )
    box.get_title().set_color(theme["ink2"])

    _finish(fig, ax, theme, title, subtitle, footnote)
    fig.savefig(out_path, facecolor=theme["surface"], bbox_inches="tight")
    plt.close(fig)
    return out_path


def _finish(fig, ax, theme, title: str, subtitle: str, footnote: str) -> None:
    ax.set_title("")
    fig.suptitle(title, x=0.06, ha="left", fontsize=15.5, color=theme["ink"], y=0.97)
    fig.text(0.06, 0.925, subtitle, ha="left", fontsize=10, color=theme["ink2"])
    fig.text(0.06, 0.035, footnote, ha="left", fontsize=8, color=theme["ink3"])
    fig.subplots_adjust(top=0.90, bottom=0.08)


# =============================================================================
# 主流程
# =============================================================================


def run(
    input_dir: str,
    input_files: list[str],
    output_dir: str,
    *,
    panel_csv: str = PANEL_CSV,
    panel_year: int = PANEL_YEAR,
    geojson: str = GEOJSON,
    years=tuple(YEARS),
    state_col: str = STATE_COL,
    site_col: str = SITE_COL,
    year_col: str = YEAR_COL,
    manner_col: str = MANNER_COL,
    suicide_filter: str = SUICIDE_FILTER,
    suicide_values: list[str] | None = None,
    state_source: str = STATE_SOURCE,
    map_style: str = MAP_STYLE,
    theme: str = THEME,
    figure_lang: str = FIGURE_LANG,
    annotate_counts: bool = ANNOTATE_COUNTS,
    n_bins: int = N_BINS,
    dpi: int = DPI,
    encoding: str = ENCODING,
    chunk_size: int = CHUNK_SIZE,
) -> dict:
    wanted = sorted({int(y) for y in years})
    if not wanted:
        fail("YEARS 是空的，至少要有一个年份")
    if suicide_filter not in ("auto", "on", "off"):
        fail("SUICIDE_FILTER 只能是 \"auto\" / \"on\" / \"off\"")
    if state_source not in ("injury_then_site", "site_then_injury", "injury_only", "site_only"):
        fail("STATE_SOURCE 只能是 \"injury_then_site\" / \"site_then_injury\" / "
             "\"injury_only\" / \"site_only\"")
    if map_style not in ("geo", "grid", "both", "none"):
        fail("MAP_STYLE 只能是 \"geo\" / \"grid\" / \"both\" / \"none\"")
    if theme not in ("light", "dark", "both"):
        fail("THEME 只能是 \"light\" / \"dark\" / \"both\"")
    if figure_lang not in ("auto", "zh", "en"):
        fail("FIGURE_LANG 只能是 \"auto\" / \"zh\" / \"en\"")

    files = resolve_inputs(input_dir, input_files)
    out_dir = resolve_output(output_dir)
    panel = load_panel(panel_csv, panel_year)

    print("=" * 78)
    print("输入文件：")
    for path in files:
        print(f"  {path}  ({path.stat().st_size / 1048576:,.0f} MB)")
    print(f"\n输出目录：{out_dir}")
    print(f"州的口径：{panel_csv}")
    composition = (f"{len(panel.panel_usps) - 1} 州 + DC"
                   if "DC" in panel.panel_usps else f"{len(panel.panel_usps)} 州")
    print(f"  辖区总数 {len(panel)} 个；{panel_year} 年面板（in_panel=1）"
          f"{len(panel.panel_usps)} 个（{composition}）")
    print(f"年份：{wanted[0]}-{wanted[-1]}  {wanted}")
    print(f"州的判断：{state_source}（InjuryState 为主，空白退回 SiteID）")
    print("=" * 78)

    result = tally(
        files, panel, years=wanted, state_col=state_col, site_col=site_col,
        year_col=year_col, manner_col=manner_col, suicide_filter=suicide_filter,
        suicide_values=list(suicide_values if suicide_values is not None else SUICIDE_VALUES),
        state_source=state_source, encoding=encoding, chunk_size=chunk_size,
    )
    counts, stats = result["counts"], result["stats"]

    table = build_tables(counts, panel, wanted)
    with_cases = table[table["TOTAL"] > 0].sort_values(
        ["TOTAL", "jurisdiction"], ascending=[False, True]
    ).reset_index(drop=True)
    without_cases = table[table["TOTAL"] == 0].sort_values("jurisdiction").reset_index(drop=True)
    panel_table = table[table["in_panel_2018"] == 1].reset_index(drop=True)

    panel_table_name = f"panel{len(panel.panel_usps)}_state_year_counts.csv"
    table.to_csv(out_dir / "state_year_counts.csv", index=False, encoding="utf-8-sig")
    with_cases.to_csv(out_dir / "states_with_cases.csv", index=False, encoding="utf-8-sig")
    without_cases.to_csv(out_dir / "states_without_cases.csv", index=False, encoding="utf-8-sig")
    panel_table.to_csv(out_dir / panel_table_name, index=False, encoding="utf-8-sig")

    pd.DataFrame(
        [{"resolved_from": k, "rows": v} for k, v in result["source_counts"].most_common()],
        columns=["resolved_from", "rows"],
    ).to_csv(out_dir / "state_source_counts.csv", index=False, encoding="utf-8-sig")

    pd.DataFrame(
        [{"value": k, "rows": v} for k, v in result["unresolved_values"].most_common()],
        columns=["value", "rows"],
    ).to_csv(out_dir / "unresolved_state_values.csv", index=False, encoding="utf-8-sig")

    funnel = pd.DataFrame([
        {"stage": "读入总行数", "rows": stats["rows_read"]},
        {"stage": f"年份不在 {wanted[0]}-{wanted[-1]}（剔除）", "rows": -stats["dropped_year"]},
        {"stage": "死亡方式不是自杀（剔除）", "rows": -stats["dropped_not_suicide"]},
        {"stage": "州空白 / 未知（剔除）", "rows": -stats["dropped_blank_state"]},
        {"stage": "州取值认不出（剔除）", "rows": -stats["dropped_unresolved_state"]},
        {"stage": "= 计入分布的自杀 case", "rows": stats["kept"]},
    ])
    funnel.to_csv(out_dir / "funnel.csv", index=False, encoding="utf-8-sig")

    # ---- 报告 --------------------------------------------------------------
    total = int(table["TOTAL"].sum())
    print("\n" + "=" * 78)
    print(f"1) {wanted[0]}-{wanted[-1]} 年自杀 case 来自这 {len(with_cases)} 个辖区")
    print("=" * 78)
    show = with_cases[["jurisdiction", "usps", "in_panel_2018"]
                      + [str(y) for y in wanted] + ["TOTAL", "share_pct"]]
    print(show.to_string(index=False))
    print(f"\n合计 {total:,} 例；没有 case 的辖区 {len(without_cases)} 个"
          f"（图上画灰色）：{', '.join(without_cases['usps']) or '无'}")

    in_panel_with = set(with_cases.loc[with_cases["in_panel_2018"] == 1, "usps"])
    off_panel_with = set(with_cases.loc[with_cases["in_panel_2018"] == 0, "usps"])
    panel_missing = panel.panel_usps - in_panel_with
    if off_panel_with:
        print(f"\n注意：这些辖区有 case，但 in_panel=0（不在 {panel_year} 面板）："
              f"{', '.join(sorted(off_panel_with))}")
        print("      多半是后来才加入 NVDRS、或只覆盖部分县的州；面板图里不会画它们。")
    if panel_missing:
        print(f"\n注意：这些辖区在面板里（in_panel=1）却一个 case 都没有："
              f"{', '.join(sorted(panel_missing))}")
        print("      要么数据确实没导出，要么州的列没认对，值得回头查一眼。")

    print("\n" + "=" * 78)
    print("逐级筛选（funnel）")
    print("=" * 78)
    print(funnel.to_string(index=False))
    recovered = (stats["kept"] + stats["dropped_year"] + stats["dropped_not_suicide"]
                 + stats["dropped_blank_state"] + stats["dropped_unresolved_state"])
    if recovered == stats["rows_read"]:
        print("（逐级相减 = 计入数，没有行被漏算）")
    else:
        print(f"警告：对不上，差 {stats['rows_read'] - recovered:,} 行")
    if stats["dropped_blank_year"] or stats["dropped_bad_year"]:
        print(f"  其中年份空白 {stats['dropped_blank_year']:,} 行、"
              f"读不出 {stats['dropped_bad_year']:,} 行")

    if result["source_counts"]:
        print("\n每行的州是从哪一列认出来的：")
        for col, n in result["source_counts"].most_common():
            print(f"  {col:<20} {n:>10,} 行")
    if result["unresolved_values"]:
        print(f"\n认不出的州取值（{stats['dropped_unresolved_state']:,} 行被剔除；"
              f"下面按「列=取值」分别计数，同一行两列都认不出会各记一次，"
              f"完整清单见 unresolved_state_values.csv）：")
        for value, n in result["unresolved_values"].most_common(10):
            print(f"  {value}  ->  {n:,} 行")
        print("  SiteID 若不是州 FIPS（有些导出用的是流水号），会全部落在这里，"
              "请改用 InjuryState 或自己给出 SiteID -> 州 的对照。")

    # ---- 画图 --------------------------------------------------------------
    counts_by_state = dict(zip(table["usps"], table["TOTAL"]))
    bins = make_bins([int(v) for v in table["TOTAL"]], n_bins)
    themes = ["light", "dark"] if theme == "both" else [theme]
    styles = ["geo", "grid"] if map_style == "both" else ([] if map_style == "none" else [map_style])
    figures: list[Path] = []

    if styles and _mpl() is None:
        print("\n注意：没装 matplotlib，只出了统计表。装上就能出图：pip install matplotlib")
        styles = []

    shapes = load_geometry(geojson) if ("geo" in styles and geojson.strip()) else {}
    years_label = f"{wanted[0]}–{wanted[-1]}"

    lang, font_note = pick_figure_language(figure_lang)
    text = FIGURE_TEXT[lang]
    if styles:
        print(f"\n图上文字：{'中文' if lang == 'zh' else '英文'}  {font_note}")

    def composed(n_states: int, has_dc: bool) -> str:
        key = "plus_dc" if has_dc else "only_states"
        return text[key].format(n=n_states - 1 if has_dc else n_states)

    footnote = text["footnote"].format(
        years=years_label, panel_year=panel_year,
        panel_file=Path(panel_csv).name, total=f"{total:,}",
    )
    legend_labels = {
        "title": text["legend_title"].format(years=years_label),
        "no_data": text["no_data"],
        "out_of_scope": text["out_of_scope"].format(panel_year=panel_year),
    }

    scopes = [
        (None, "map_all_states",
         text["title_all"].format(years=years_label),
         text["subtitle_all"].format(
             n=len(panel), composition=composed(len(panel), "DC" in panel.name_of))),
        (panel.panel_usps, f"map_panel{len(panel.panel_usps)}",
         text["title_panel"].format(years=years_label, panel_year=panel_year),
         text["subtitle_panel"].format(
             n=len(panel.panel_usps), panel_year=panel_year,
             composition=composed(len(panel.panel_usps), "DC" in panel.panel_usps))),
    ]

    for theme_name in themes:
        suffix = "" if theme_name == "light" else "_dark"
        for scope, stem, title, subtitle in scopes:
            for style in styles:
                tag = "" if style == "geo" else "_grid"
                path = out_dir / f"{stem}_{wanted[0]}_{wanted[-1]}{tag}{suffix}.png"
                drawn = (
                    draw_geo_map(
                        counts_by_state, scope, shapes=shapes, panel=panel, title=title,
                        subtitle=subtitle, footnote=footnote, legend=legend_labels,
                        out_path=path, theme_name=theme_name, bins=bins,
                        annotate=annotate_counts, dpi=dpi,
                    ) if style == "geo" else
                    draw_grid_map(
                        counts_by_state, scope, panel=panel, title=title, subtitle=subtitle,
                        footnote=footnote, legend=legend_labels, out_path=path,
                        theme_name=theme_name, bins=bins, annotate=annotate_counts, dpi=dpi,
                    )
                )
                if drawn:
                    figures.append(drawn)

    print("\n" + "=" * 78)
    print("输出")
    print("=" * 78)
    for name in ("state_year_counts.csv", "states_with_cases.csv",
                 "states_without_cases.csv", panel_table_name,
                 "state_source_counts.csv", "unresolved_state_values.csv", "funnel.csv"):
        print(f"  {out_dir / name}")
    for path in figures:
        print(f"  {path}")
    if bins:
        print(f"\n颜色分档（按有 case 的辖区分位数切 {len(bins)} 档）："
              + "  ".join(bin_label(lo, hi) for lo, hi in bins))

    return {
        "table": table,
        "with_cases": with_cases,
        "without_cases": without_cases,
        "panel_table": panel_table,
        "counts": counts,
        "bins": bins,
        "figures": figures,
        "funnel": funnel,
        "stats": stats,
        "panel": panel,
        "source_counts": result["source_counts"],
        "unresolved_values": result["unresolved_values"],
        "output_dir": out_dir,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="NVDRS 自杀 case 的州分布：统计表 + 2018-2024 州分布图"
    )
    parser.add_argument("--input-dir", default=None)
    parser.add_argument("--input", nargs="+", default=None)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--panel-csv", default=None)
    parser.add_argument("--geojson", default=None)
    parser.add_argument("--years", nargs="+", type=int, default=None)
    parser.add_argument("--state-col", default=None)
    parser.add_argument("--site-col", default=None)
    parser.add_argument("--year-col", default=None)
    parser.add_argument("--manner-col", default=None)
    parser.add_argument("--suicide-filter", choices=["auto", "on", "off"], default=None)
    parser.add_argument("--suicide-values", nargs="+", default=None)
    parser.add_argument("--state-source",
                        choices=["injury_then_site", "site_then_injury",
                                 "injury_only", "site_only"], default=None)
    parser.add_argument("--map-style", choices=["geo", "grid", "both", "none"], default=None)
    parser.add_argument("--theme", choices=["light", "dark", "both"], default=None)
    parser.add_argument("--figure-lang", choices=["auto", "zh", "en"], default=None,
                        help="图上文字的语言（auto = 有中文字体就用中文）")
    parser.add_argument("--no-counts", action="store_true", help="图上只标州缩写，不标数字")
    parser.add_argument("--bins", type=int, default=None)
    parser.add_argument("--dpi", type=int, default=None)
    parser.add_argument("--encoding", default=None)
    parser.add_argument("--chunk-size", type=int, default=None)
    args = parser.parse_args(argv)

    pick = lambda a, b: a if a is not None else b  # noqa: E731
    result = run(
        pick(args.input_dir, INPUT_DIR),
        pick(args.input, INPUT_FILES),
        pick(args.output_dir, OUTPUT_DIR),
        panel_csv=pick(args.panel_csv, PANEL_CSV),
        geojson=pick(args.geojson, GEOJSON),
        years=pick(args.years, YEARS),
        state_col=pick(args.state_col, STATE_COL),
        site_col=pick(args.site_col, SITE_COL),
        year_col=pick(args.year_col, YEAR_COL),
        manner_col=pick(args.manner_col, MANNER_COL),
        suicide_filter=pick(args.suicide_filter, SUICIDE_FILTER),
        suicide_values=pick(args.suicide_values, SUICIDE_VALUES),
        state_source=pick(args.state_source, STATE_SOURCE),
        map_style=pick(args.map_style, MAP_STYLE),
        theme=pick(args.theme, THEME),
        figure_lang=pick(args.figure_lang, FIGURE_LANG),
        annotate_counts=(not args.no_counts) if args.no_counts else ANNOTATE_COUNTS,
        n_bins=pick(args.bins, N_BINS),
        dpi=pick(args.dpi, DPI),
        encoding=pick(args.encoding, ENCODING),
        chunk_size=pick(args.chunk_size, CHUNK_SIZE),
    )
    return 0 if int(result["table"]["TOTAL"].sum()) else 1


if __name__ == "__main__":
    sys.exit(main())
