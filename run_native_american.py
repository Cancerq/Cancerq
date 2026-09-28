#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 NVDRS 数据里按 Race_c 筛出 American Indian / Alaska Native（Native American），
并做 Gender / Education Level / State / Age Group 四个分布表。

判断：Race_c【包含】以下任一关键词（不区分大小写）就算：
          american indian / alaska native / native american
      "Two or more races" 这类多种族取值不含关键词，不算（见 race_values.csv 核对）。
      Race_c 是数字代码的话，把对应代码填进 RACE_CODES。

年龄组的来源（按顺序取第一个有的）：
    1. 数据里已有的年龄组列（AgeGroup / age_band ...）
    2. 数值 Age 列 -> 按 AGE_BANDS 分段（18-27 / 28-37 / 38-47 / 48-57 / 58-67）
    3. 文件名里的年龄段（如 nvdrs_age_18_27.csv -> 18-27）

输出目录：

    OUTPUT_DIR/
      native_american_cases.csv        筛出的全部行（原样保留所有列，前面加 source_file）
      dist_gender.csv                  Gender 分布
      dist_education.csv               Education Level 分布
      dist_state.csv                   State 分布
      dist_age_group.csv               Age Group 分布
      distributions_all.csv            四个分布合成一张长表
      race_values.csv                  Race_c 的全部取值 / 计数 / 是否算进来（核对用）
      native_american_distributions.xlsx   以上各表放进一个 Excel（装了 openpyxl 才有）

每张分布表：category / n / percent（占 Native American 总数的 %），空白单列为 "(空白)"。

单文件，只依赖 pandas。分块读，GB 级的文件也只占几百 MB 内存。

用法：把「配置区」的路径填好，然后运行

    python run_native_american.py
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

# 【必填 1】input_location。两种填法，二选一即可。
#
# 填法 A：逐个列出文件（可以只有一个）
INPUT_FILES = [
    r"",     # 例：r"D:\School_project\Project\NVDRS\NVDRS_18_67.csv"
    r"",
]

# 填法 B：填一个目录，读里面所有 .csv（填了 INPUT_FILES 就忽略这个）
INPUT_DIR = r""          # 例：r"D:\School_project\Project\NVDRS\age_chunks"

# 【必填 2】output_location（不存在会自动创建）
OUTPUT_DIR = r""         # 例：r"D:\School_project\Project\NVDRS\Native_American"

# -----------------------------------------------------------------------------
# 以下通常不用改
# -----------------------------------------------------------------------------

# Race_c 里【包含】其中任一关键词就算 Native American（不区分大小写）
RACE_KEYWORDS = ["american indian", "alaska native", "native american"]

# Race_c 是数字代码时，填算作 Native American 的代码，例：["3"]
RACE_CODES: list[str] = []

# 列名。留空 = 自动识别
RACE_COL = r""           # 自动找 Race_c
GENDER_COL = r""         # 自动找 Sex / Gender
EDUCATION_COL = r""      # 自动找 EducationLevel / Education
STATE_COL = r""          # 自动找 SiteState / State
AGE_GROUP_COL = r""      # 自动找 AgeGroup / age_band；没有就用 Age 分段
AGE_COL = r""            # 自动找 Age（数值年龄）

# 用数值 Age 分年龄组时的分段（闭区间）
AGE_BANDS = [(18, 27), (28, 37), (38, 47), (48, 57), (58, 67)]

# CSV 编码。留空 = 自动识别（依次试 UTF-8 / GBK / Windows-1252）
ENCODING = ""
CHUNK_SIZE = 50_000

# =============================================================================
# 配置区结束
# =============================================================================

COL_CANDIDATES = {
    "race": ("racec", "race", "victimrace", "racecode"),
    "gender": ("sex", "gender", "sexc", "genderc", "victimsex"),
    "education": ("educationlevel", "educationlevelc", "education", "educationc",
                  "educ", "educationattainment"),
    "state": ("sitestate", "state", "incidentstate", "stateabbr", "st"),
    "age_group": ("agegroup", "ageband", "agerange", "agecategory", "agegrp",
                  "agecat"),
    "age": ("age", "ageyears", "victimage", "agec"),
}
DIMENSIONS = [
    ("gender", "Gender", "dist_gender.csv"),
    ("education", "Education Level", "dist_education.csv"),
    ("state", "State", "dist_state.csv"),
    ("age_group", "Age Group", "dist_age_group.csv"),
]
ENCODING_CANDIDATES = ("utf-8-sig", "gbk", "cp1252")
SKIP_NAME_PATTERNS = (
    re.compile(r"native_american", re.I),
    re.compile(r"^dist_", re.I),
    re.compile(r"^distributions", re.I),
    re.compile(r"_values\.csv$", re.I),
    re.compile(r"^summary", re.I),
    re.compile(r"distribution", re.I),
    re.compile(r"_excluded", re.I),
)
AGE_BAND_RE = re.compile(r"(?<!\d)(\d{2})[_\-](\d{2})(?!\d)")
BLANK_LABEL = "(空白)"
MISSING_TOKENS = {"", ".", "nan", "none", "null", "<na>"}


def fail(message: str) -> "NoReturn":  # noqa: F821
    print(f"\n错误：{message}\n", file=sys.stderr)
    sys.exit(2)


def norm_colname(name) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower().lstrip("\ufeff"))


def find_col(columns, candidates):
    normalised = {norm_colname(c): c for c in columns}
    for cand in candidates:
        if cand in normalised:
            return normalised[cand]
    return None


def clean(series: pd.Series) -> pd.Series:
    text = series.fillna("").astype(str).str.replace(r"\s+", " ", regex=True).str.strip()
    return text.where(~text.str.lower().isin(MISSING_TOKENS), BLANK_LABEL)


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
    fail(f"认不出文件编码：\n  {path}\n试过：{', '.join(tried)}。请在配置区填 ENCODING。")


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
        for p in sorted(set(every) - set(paths)):
            print(f"  （跳过非数据文件）{p.name}")
        if not paths:
            fail(f"INPUT_DIR 里没有可用的 .csv：\n  {directory}")
    else:
        fail("input_location 还没填。请在「配置区」填 INPUT_FILES 或 INPUT_DIR，\n"
             "也可以用命令行：--input 文件 ... 或 --input-dir 目录")
    seen, unique = set(), []
    for path in paths:
        if path.resolve() not in seen:
            seen.add(path.resolve())
            unique.append(path)
    return unique


def band_label(low: int, high: int) -> str:
    return f"{low}-{high}"


def age_to_band(series: pd.Series, bands) -> pd.Series:
    age = pd.to_numeric(series, errors="coerce")
    out = pd.Series(BLANK_LABEL, index=series.index, dtype=object)
    outside = age.notna()
    for low, high in bands:
        hit = age.between(low, high)
        out[hit] = band_label(low, high)
        outside &= ~hit
    out[outside] = "超出分段"
    return out


def band_from_name(path: Path):
    m = AGE_BAND_RE.search(path.stem)
    return f"{m.group(1)}-{m.group(2)}" if m else None


def resolve_columns(header, overrides: dict) -> dict:
    cols = {}
    for role, candidates in COL_CANDIDATES.items():
        given = overrides.get(role)
        if given:
            if given not in header:
                fail(f"指定的列 {given!r} 不在文件里。\n列有：{', '.join(map(str, header))}")
            cols[role] = given
        else:
            cols[role] = find_col(header, candidates)
    if not cols["race"]:
        fail("找不到 Race_c 列，请在配置区填 RACE_COL。\n"
             f"列有：{', '.join(map(str, header))}")
    return cols


def is_native_american(race: pd.Series, keywords, codes) -> pd.Series:
    text = race.fillna("").astype(str).str.strip()
    lowered = text.str.lower()
    hit = pd.Series(False, index=race.index)
    for kw in keywords:
        if kw.strip():
            hit |= lowered.str.contains(kw.strip().lower(), regex=False)
    if codes:
        code = text.str.replace(r"\.0+$", "", regex=True)
        hit |= code.isin([str(c).strip() for c in codes])
    return hit


def distribution(values: pd.Series, label: str, order=None) -> pd.DataFrame:
    counts = values.value_counts()
    if order:
        known = [c for c in order if c in counts.index]
        rest = [c for c in counts.index if c not in known]
        counts = counts.reindex(known + rest)
    total = int(counts.sum())
    table = pd.DataFrame({"dimension": label, "category": counts.index,
                          "n": counts.to_numpy().astype(int)})
    table["percent"] = (table["n"] / total * 100).round(2) if total else 0.0
    table = pd.concat([table, pd.DataFrame([{
        "dimension": label, "category": "Total", "n": total,
        "percent": 100.0 if total else 0.0}])], ignore_index=True)
    return table


def run(input_files, input_dir, output_dir, *, race_col="", gender_col="",
        education_col="", state_col="", age_group_col="", age_col="",
        race_keywords=tuple(RACE_KEYWORDS), race_codes=tuple(RACE_CODES),
        age_bands=tuple(AGE_BANDS), encoding="", chunk_size=CHUNK_SIZE) -> dict:
    paths = resolve_inputs(input_files, input_dir)
    if not output_dir or not output_dir.strip():
        fail("output_location 还没填。请在「配置区」填 OUTPUT_DIR，或用命令行 --output-dir")
    out = Path(output_dir.strip())
    out.mkdir(parents=True, exist_ok=True)
    if not any(k.strip() for k in race_keywords) and not race_codes:
        fail("RACE_KEYWORDS 和 RACE_CODES 都是空的")

    overrides = {"race": race_col, "gender": gender_col, "education": education_col,
                 "state": state_col, "age_group": age_group_col, "age": age_col}

    # 第一遍只读表头，定下所有文件列的并集，输出文件才能用同一个表头
    headers = {}
    for path in paths:
        enc = detect_encoding(path, encoding)
        headers[path] = (enc, list(pd.read_csv(path, nrows=0, encoding=enc).columns))
    union = list(dict.fromkeys(c for _, h in headers.values() for c in h))

    cases_path = out / "native_american_cases.csv"
    race_counts: dict[str, int] = {}
    dims = {role: [] for role, _, _ in DIMENSIONS}
    age_source, rows_read, matched = {}, 0, 0

    with open(cases_path, "w", newline="", encoding="utf-8-sig") as handle:
        out_cols = ["source_file", "age_group_used"] + union
        pd.DataFrame(columns=out_cols).to_csv(handle, index=False)
        for path in paths:
            enc, header = headers[path]
            cols = resolve_columns(header, overrides)
            name_band = band_from_name(path)
            if cols["age_group"]:
                age_source[path.name] = f"列 {cols['age_group']}"
            elif cols["age"]:
                age_source[path.name] = f"数值年龄列 {cols['age']} 按 AGE_BANDS 分段"
            elif name_band:
                age_source[path.name] = f"文件名 -> {name_band}"
            else:
                age_source[path.name] = "无（年龄组记为空白）"
            print(f"读取 {path.name}   编码={enc}  Race={cols['race']}  "
                  f"Gender={cols['gender']}  Education={cols['education']}  "
                  f"State={cols['state']}  AgeGroup来源={age_source[path.name]}")

            for chunk in pd.read_csv(path, dtype=str, keep_default_na=False,
                                     encoding=enc, chunksize=chunk_size):
                rows_read += len(chunk)
                race = clean(chunk[cols["race"]])
                for raw, n in race.value_counts().items():
                    race_counts[raw] = race_counts.get(raw, 0) + int(n)
                hit = chunk[is_native_american(chunk[cols["race"]],
                                               race_keywords, race_codes)]
                if hit.empty:
                    continue
                matched += len(hit)

                if cols["age_group"]:
                    group = clean(hit[cols["age_group"]])
                elif cols["age"]:
                    group = age_to_band(hit[cols["age"]], age_bands)
                else:
                    group = pd.Series(name_band or BLANK_LABEL, index=hit.index)
                for role, _, _ in DIMENSIONS:
                    if role == "age_group":
                        dims[role].append(group)
                    elif cols[role]:
                        dims[role].append(clean(hit[cols[role]]))

                rows = hit.reindex(columns=union)
                rows.insert(0, "age_group_used", group.to_numpy())
                rows.insert(0, "source_file", path.name)
                rows.to_csv(handle, index=False, header=False)

    band_order = [band_label(lo, hi) for lo, hi in age_bands]
    tables, missing_dims = {}, []
    for role, label, filename in DIMENSIONS:
        if not dims[role]:
            if matched:
                missing_dims.append(label)
            values = pd.Series([], dtype=object)
        else:
            values = pd.concat(dims[role], ignore_index=True)
        table = distribution(values, label,
                             order=band_order if role == "age_group" else None)
        table.to_csv(out / filename, index=False, encoding="utf-8-sig")
        tables[role] = table
    long = pd.concat(tables.values(), ignore_index=True)
    long.to_csv(out / "distributions_all.csv", index=False, encoding="utf-8-sig")

    race_table = pd.DataFrame(sorted(race_counts.items(), key=lambda kv: -kv[1]),
                              columns=["Race_c", "n"])
    race_table["counted_as_native_american"] = is_native_american(
        race_table["Race_c"], race_keywords, race_codes).astype(int)
    race_table.to_csv(out / "race_values.csv", index=False, encoding="utf-8-sig")

    try:
        import openpyxl  # noqa: F401
        with pd.ExcelWriter(out / "native_american_distributions.xlsx") as xl:
            for role, label, _ in DIMENSIONS:
                tables[role].to_excel(xl, sheet_name=label, index=False)
            long.to_excel(xl, sheet_name="All distributions", index=False)
            race_table.to_excel(xl, sheet_name="Race_c values", index=False)
    except ImportError:
        print("（没装 openpyxl，跳过 Excel；CSV 已全部写好）")

    print(f"\n读入 {rows_read} 行，其中 Native American {matched} 行"
          + (f"（{matched / rows_read:.2%}）" if rows_read else ""))
    for role, label, _ in DIMENSIONS:
        print(f"\n{label} 分布")
        print(tables[role].drop(columns="dimension").to_string(index=False))
    if missing_dims:
        print(f"\n注意：找不到这些列，分布为空：{', '.join(missing_dims)}。"
              "在配置区填 GENDER_COL / EDUCATION_COL / STATE_COL。")
    multi = race_table[(race_table["counted_as_native_american"] == 0)
                       & race_table["Race_c"].str.contains(
                           r"two or more|multi|more than one", case=False)]
    if not multi.empty:
        print("\n注意：以下多种族取值没算进来（可能包含 Native American），见 race_values.csv：")
        for _, r in multi.iterrows():
            print(f"  {r['Race_c']!r}: {r['n']}")
    if matched == 0:
        print("\n警告：一行都没筛到。看 race_values.csv 里 Race_c 的实际写法，"
              "改 RACE_KEYWORDS 或 RACE_CODES。")
    print(f"\n输出目录：{out}")
    return {"rows_read": rows_read, "matched": matched, "tables": tables,
            "race_values": race_table, "age_source": age_source,
            "cases_path": cases_path, "output_dir": out}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="按 Race_c 筛 Native American，做 Gender/Education/State/Age Group 分布。"
                    "不带参数时用脚本顶部配置区的值。")
    parser.add_argument("--input", nargs="+", default=None)
    parser.add_argument("--input-dir", default=None)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--race-col", default=None)
    parser.add_argument("--encoding", default=None)
    args = parser.parse_args(argv)
    run(
        args.input if args.input is not None else INPUT_FILES,
        args.input_dir if args.input_dir is not None else
        ("" if args.input is not None else INPUT_DIR),
        args.output_dir if args.output_dir is not None else OUTPUT_DIR,
        race_col=args.race_col or RACE_COL, gender_col=GENDER_COL,
        education_col=EDUCATION_COL, state_col=STATE_COL,
        age_group_col=AGE_GROUP_COL, age_col=AGE_COL,
        race_keywords=RACE_KEYWORDS, race_codes=RACE_CODES, age_bands=AGE_BANDS,
        encoding=args.encoding or ENCODING, chunk_size=CHUNK_SIZE,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
