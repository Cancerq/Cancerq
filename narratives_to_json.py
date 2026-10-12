#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 NVDRS 叙述文本文件整理成每个死者一条的 JSON 数组：

    [
      {
        "case_id": "你的内部唯一编号",
        "LE": "该死者的执法叙述原文",
        "CME": "该死者的验尸官或法医叙述原文"
      }
    ]

只读叙述文本文件（.txt / .csv / .tsv，逗号、Tab、竖线分隔都能自动识别）。

列怎么找（默认自动识别，也可以在配置区写死）：

    LE    NarrativeLE / LE_Narrative / LawEnforcementNarrative ...
    CME   NarrativeCME / CME_Narrative / NarrativeME / CoronerNarrative ...
    case_id
          填了 CASE_ID_COLS 就用它（多列用 "_" 连起来）；
          没填时依次找：CaseID 这类单独编号列 -> IncidentID + PersonID -> IncidentID

叙述保留原文：只统一换行符、去掉首尾空白，不改正文。

筛选：
    - LE 和 CME 都为空的行丢掉（REQUIRE_BOTH = True 时两者缺一就丢）
    - 同一个 case_id 出现多次只保留第一条，重复的写进 duplicate_case_ids.csv
    - 填了 KEEP_IDS_FILE（例如建筑业案例表）时，只保留里面出现过的 case_id

用法：把「配置区」填好，然后运行

    python narratives_to_json.py

或者命令行：

    python narratives_to_json.py --input narratives.txt --output out_folder
    python narratives_to_json.py --input narratives.txt --output out.json \\
        --keep-ids construction_cases.csv

单文件，只依赖 pandas。分块读写，几 GB 的文件也只占几百 MB 内存。
"""

from __future__ import annotations

import argparse
import codecs
import csv
import json
import re
import sys
from pathlib import Path
from typing import Iterator

import pandas as pd

# =============================================================================
# 配置区 —— 把路径填进下面的空白引号里
# =============================================================================

# 【必填 1】叙述文本文件，可以填多个，也可以填文件夹
INPUT_PATHS = [
    r"",                 # 例：r"D:\NVDRS\Liu_1191_nvdrs_2024_narratives.txt"
]

# 【必填 2】输出：文件（.json）或文件夹（自动写 narratives.json）
OUTPUT_PATH = r""        # 例：r"D:\NVDRS\Json_all_and_sampling"

# -----------------------------------------------------------------------------
# 可选
# -----------------------------------------------------------------------------

# 只保留这个表里出现过的 case_id（例如建筑业案例 CSV）。留空 = 不筛
KEEP_IDS_FILE = r""

# case_id 用哪几列。留空 = 自动识别。例：["IncidentID", "PersonID"]
CASE_ID_COLS: list[str] = []

# 多列拼 case_id 时的连接符
CASE_ID_JOINER = "_"

# LE / CME 列名。留空 = 自动识别
LE_COL = r""
CME_COL = r""

# True = LE 和 CME 都要有才保留；False = 有一个就保留
REQUIRE_BOTH = False

# -----------------------------------------------------------------------------
# 以下通常不用改
# -----------------------------------------------------------------------------

# 文件编码。"auto" = 依次尝试 utf-8-sig / cp1252 / latin-1
ENCODING = "auto"
CHUNK_SIZE = 50_000

# =============================================================================
# 配置区结束
# =============================================================================

TEXT_SUFFIXES = {".txt", ".csv", ".tsv", ".dat"}
AUTO_ENCODINGS = ("utf-8-sig", "cp1252", "latin-1")
MISSING_TOKENS = {"", ".", "nan", "none", "null", "<na>"}
DEFAULT_OUTPUT_NAME = "narratives.json"

# 候选列名（归一化后比较：小写、去掉空格下划线等符号）
LE_CANDIDATES = (
    "narrativele", "lenarrative", "narrle", "lenarr",
    "lawenforcementnarrative", "narrativelawenforcement", "narrativepolice",
)
CME_CANDIDATES = (
    "narrativecme", "cmenarrative", "narrcme", "cmenarr",
    "narrativeme", "menarrative", "coronernarrative", "narrativecoroner",
    "coronermenarrative", "medicalexaminernarrative", "narrativemedicalexaminer",
)
SINGLE_ID_CANDIDATES = ("caseid", "internalid", "uniqueid", "recordid", "decedentid", "victimid")
INCIDENT_ID_CANDIDATES = ("incidentid", "incid", "incident")
PERSON_ID_CANDIDATES = ("personid", "persid", "person", "victimnumber", "personnumber")

NON_ALNUM = re.compile(r"[^0-9a-z]")


def norm_name(name: str) -> str:
    return NON_ALNUM.sub("", str(name).lower())


def find_col(columns: list[str], candidates: tuple[str, ...]) -> str | None:
    lookup = {norm_name(c): c for c in columns}
    for cand in candidates:
        if cand in lookup:
            return lookup[cand]
    return None


def resolve_id_cols(columns: list[str], wanted: list[str], source: str) -> list[str]:
    if wanted:
        missing = [c for c in wanted if c not in columns]
        if missing:
            sys.exit(f"{source} 缺少 case_id 列：{missing}\n现有列：{columns}")
        return list(wanted)
    single = find_col(columns, SINGLE_ID_CANDIDATES)
    if single:
        return [single]
    incident = find_col(columns, INCIDENT_ID_CANDIDATES)
    person = find_col(columns, PERSON_ID_CANDIDATES)
    if incident and person:
        return [incident, person]
    if incident:
        return [incident]
    sys.exit(f"{source} 里找不到编号列，请在配置区填 CASE_ID_COLS。\n现有列：{columns}")


def resolve_text_col(columns: list[str], wanted: str, candidates: tuple[str, ...],
                     label: str, source: str) -> str:
    if wanted:
        if wanted not in columns:
            sys.exit(f"{source} 里没有 {label} 列 {wanted!r}\n现有列：{columns}")
        return wanted
    found = find_col(columns, candidates)
    if found is None:
        sys.exit(f"{source} 里找不到 {label} 叙述列，请在配置区填 {label}_COL。\n现有列：{columns}")
    return found


# -----------------------------------------------------------------------------
# 读文件
# -----------------------------------------------------------------------------

def collect_inputs(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        if not raw or not raw.strip():
            continue
        p = Path(raw.strip()).expanduser()
        if p.is_dir():
            files.extend(sorted(f for f in p.iterdir()
                                if f.is_file() and f.suffix.lower() in TEXT_SUFFIXES))
        elif p.is_file():
            files.append(p)
        else:
            sys.exit(f"找不到输入：{p}")
    if not files:
        sys.exit("没有可处理的叙述文本文件（支持 .txt .csv .tsv .dat）")
    return files


def detect_encoding(path: Path, encoding: str) -> str:
    if encoding != "auto":
        return encoding
    with open(path, "rb") as fh:
        sample = fh.read(4 << 20)
    for enc in AUTO_ENCODINGS:
        # final=False：样本截断在多字节字符中间不算错
        try:
            codecs.getincrementaldecoder(enc)().decode(sample, final=False)
            return enc
        except UnicodeDecodeError:
            continue
    return "latin-1"


def detect_sep(path: Path, encoding: str) -> str:
    """看表头那一行里哪种分隔符最多。"""
    with open(path, "r", encoding=encoding, newline="") as fh:
        header = fh.readline()
    counts = {sep: header.count(sep) for sep in ("\t", "|", ",", ";")}
    sep = max(counts, key=counts.get)
    if counts[sep] == 0:
        sys.exit(f"{path.name} 的表头里没有分隔符，读不出列：{header[:200]!r}")
    return sep


def read_chunks(path: Path, encoding: str, chunk_size: int) -> Iterator[pd.DataFrame]:
    enc = detect_encoding(path, encoding)
    sep = detect_sep(path, enc)
    print(f"读取 {path.name}  (编码 {enc}，分隔符 {sep!r})")
    reader = pd.read_csv(path, sep=sep, dtype=str, keep_default_na=False,
                         encoding=enc, chunksize=chunk_size,
                         quoting=csv.QUOTE_MINIMAL)
    for chunk in reader:
        chunk.columns = [str(c).strip() for c in chunk.columns]
        yield chunk


# -----------------------------------------------------------------------------
# 取值
# -----------------------------------------------------------------------------

def clean_id(value) -> str:
    text = "" if value is None else str(value).strip()
    if text.lower() in MISSING_TOKENS:
        return ""
    # 34.0 这种被导出成小数的整数编号还原成 34
    if re.fullmatch(r"-?\d+\.0+", text):
        text = text.split(".")[0]
    return text


def clean_narrative(value) -> str:
    """保留原文，只统一换行、去首尾空白。"""
    text = "" if value is None else str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return "" if text.lower() in MISSING_TOKENS else text


def make_case_id(values: list, joiner: str) -> str:
    parts = [clean_id(v) for v in values]
    if not all(parts):
        return ""
    return joiner.join(parts)


def load_keep_ids(path_raw: str, id_cols: list[str], joiner: str,
                  encoding: str, chunk_size: int) -> set[str] | None:
    if not path_raw or not path_raw.strip():
        return None
    path = Path(path_raw.strip()).expanduser()
    if not path.is_file():
        sys.exit(f"找不到 KEEP_IDS_FILE：{path}")
    ids: set[str] = set()
    cols: list[str] | None = None
    for chunk in read_chunks(path, encoding, chunk_size):
        if cols is None:
            cols = resolve_id_cols(list(chunk.columns), id_cols, path.name)
            print(f"  筛选表编号列：{' + '.join(cols)}")
        for values in chunk[cols].itertuples(index=False, name=None):
            cid = make_case_id(list(values), joiner)
            if cid:
                ids.add(cid)
    print(f"  筛选表里共 {len(ids)} 个 case_id")
    return ids


# -----------------------------------------------------------------------------
# 输出
# -----------------------------------------------------------------------------

def resolve_output(raw: str) -> Path:
    if not raw or not raw.strip():
        sys.exit("请填写 OUTPUT_PATH 或传 --output")
    output = Path(raw.strip()).expanduser()
    if output.is_dir() or not output.suffix:
        output = output / DEFAULT_OUTPUT_NAME
    return output


def open_for_write(path: Path, newline: str = "\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return open(path, "w", encoding="utf-8", newline=newline)
    except PermissionError:
        sys.exit(f"没有权限写入：{path}\n"
                 "请检查：这个文件是否正被其他程序打开；路径是否指向只读位置。")


# -----------------------------------------------------------------------------
# 主流程
# -----------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="NVDRS 叙述文本 -> [{case_id, LE, CME}] JSON")
    ap.add_argument("--input", nargs="+", help="叙述文本文件或文件夹，可多个")
    ap.add_argument("--output", help="输出 .json 文件或文件夹")
    ap.add_argument("--keep-ids", help="只保留这个表里的 case_id")
    ap.add_argument("--id-cols", help="case_id 列，逗号分隔，例如 IncidentID,PersonID")
    ap.add_argument("--le-col", help="LE 叙述列名")
    ap.add_argument("--cme-col", help="CME 叙述列名")
    ap.add_argument("--require-both", action="store_true", default=None,
                    help="LE 和 CME 都有才保留")
    ap.add_argument("--encoding", help='文件编码，默认 "auto"')
    return ap.parse_args(argv)


def pick(cli, default):
    return default if cli is None else cli


def main(argv: list[str] | None = None) -> dict:
    args = parse_args(argv)
    files = collect_inputs(pick(args.input, INPUT_PATHS))
    output = resolve_output(pick(args.output, OUTPUT_PATH))
    id_cols_cfg = ([c.strip() for c in args.id_cols.split(",") if c.strip()]
                   if args.id_cols else CASE_ID_COLS)
    le_cfg = pick(args.le_col, LE_COL)
    cme_cfg = pick(args.cme_col, CME_COL)
    require_both = pick(args.require_both, REQUIRE_BOTH)
    encoding = pick(args.encoding, ENCODING)

    keep_ids = load_keep_ids(pick(args.keep_ids, KEEP_IDS_FILE), id_cols_cfg,
                             CASE_ID_JOINER, encoding, CHUNK_SIZE)

    stats = {"rows": 0, "written": 0, "blank_id": 0, "no_text": 0,
             "not_in_keep": 0, "duplicate": 0}
    seen: set[str] = set()
    duplicates: list[tuple[str, str, int]] = []
    dup_path = output.with_name("duplicate_case_ids.csv")

    with open_for_write(output) as out:
        out.write("[")
        for path in files:
            id_cols = le_col = cme_col = None
            row_no = 0
            for chunk in read_chunks(path, encoding, CHUNK_SIZE):
                if id_cols is None:
                    columns = list(chunk.columns)
                    id_cols = resolve_id_cols(columns, id_cols_cfg, path.name)
                    le_col = resolve_text_col(columns, le_cfg, LE_CANDIDATES, "LE", path.name)
                    cme_col = resolve_text_col(columns, cme_cfg, CME_CANDIDATES, "CME", path.name)
                    print(f"  case_id = {' + '.join(id_cols)}   LE = {le_col}   CME = {cme_col}")
                for values in chunk[id_cols + [le_col, cme_col]].itertuples(index=False, name=None):
                    row_no += 1
                    stats["rows"] += 1
                    case_id = make_case_id(list(values[:-2]), CASE_ID_JOINER)
                    le, cme = clean_narrative(values[-2]), clean_narrative(values[-1])
                    if not case_id:
                        stats["blank_id"] += 1
                        continue
                    if (require_both and not (le and cme)) or not (le or cme):
                        stats["no_text"] += 1
                        continue
                    if keep_ids is not None and case_id not in keep_ids:
                        stats["not_in_keep"] += 1
                        continue
                    if case_id in seen:
                        stats["duplicate"] += 1
                        duplicates.append((case_id, path.name, row_no))
                        continue
                    seen.add(case_id)
                    record = {"case_id": case_id, "LE": le, "CME": cme}
                    out.write("," if stats["written"] else "")
                    out.write("\n  " + json.dumps(record, ensure_ascii=False))
                    stats["written"] += 1
        out.write("\n]\n" if stats["written"] else "]\n")

    if duplicates:
        with open_for_write(dup_path, newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["case_id", "file", "row"])
            w.writerows(duplicates)

    print()
    print(f"共读 {stats['rows']} 行，写出 {stats['written']} 个死者 -> {output}")
    if stats["blank_id"]:
        print(f"丢弃 编号为空     {stats['blank_id']}")
    if stats["no_text"]:
        what = "LE 或 CME 为空" if require_both else "LE 和 CME 都为空"
        print(f"丢弃 {what}  {stats['no_text']}")
    if stats["not_in_keep"]:
        print(f"丢弃 不在筛选表里 {stats['not_in_keep']}")
    if keep_ids is not None:
        missing = len(keep_ids - seen)
        if missing:
            print(f"注意：筛选表里有 {missing} 个 case_id 在叙述文件里没找到")
    if duplicates:
        print(f"丢弃 重复 case_id {stats['duplicate']}（只留第一条，明细见 {dup_path}）")
    return stats


if __name__ == "__main__":
    main()
