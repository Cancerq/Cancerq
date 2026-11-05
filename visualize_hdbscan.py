#!/usr/bin/env python3
"""HDBSCAN 聚类结果可视化：读 JSON / JSONL / CSV / TSV，生成一个独立的 HTML 页面。

页面内容：
  - 左栏：输入文件信息、HDBSCAN 参数、聚类统计、簇列表（颜色图例，可点击定位）
  - 右侧画布：每个簇一种颜色；簇中心词居中，其余词围绕中心排成一圈，
    并用线与中心相连。有 probability 时，越属于该簇的词离中心越近、连线越实。
    噪声点（label = -1）灰色单独放一块，不连线。
  - 数据里有 x / y 坐标时，可切换到“原始坐标”布局。

只用标准库，不需要 pandas / matplotlib。生成的 HTML 不联网也能打开。

用法：
  python visualize_hdbscan.py result.csv
  python visualize_hdbscan.py result.json -o clusters.html --open
  python visualize_hdbscan.py words.csv --word-col term --cluster-col topic \\
      --param min_cluster_size=10 --param min_samples=5

支持的输入（列名不区分大小写，可用 --word-col 等参数覆盖）：
  1. 表格（CSV / TSV / JSON 记录列表 / JSONL）：一行一个词
       word, cluster[, probability][, outlier_score][, x, y][, is_center]
  2. JSON 并列数组（直接 dump clusterer.labels_ 那种）：
       {"words": [...], "labels": [...], "probabilities": [...], "params": {...}}
  3. JSON 按簇组织：
       {"params": {...}, "clusters": [{"id": 0, "center": "苹果", "members": ["香蕉", ...]}],
        "noise": ["..."]}
     members 里的元素也可以是 {"word": ..., "probability": ...}。
  4. JSON 包一层：{"params": {...}, "points": [ 记录, ... ]}

HDBSCAN 参数来源（后者覆盖前者）：JSON 里的 params / parameters 字段、
CSV 开头的注释行（如 "# min_cluster_size = 10"）、--params 参数文件、--param k=v。

簇中心怎么定：is_center 为真的那个词 > probability 最高的词 >
（有坐标时）离簇质心最近的词 > 簇里第一个词。
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import io
import json
import math
import sys
import webbrowser
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- 列名识别

WORD_KEYS = ["word", "words", "text", "texts", "token", "term", "keyword",
             "name", "title", "doc", "document", "sentence", "item", "词", "词语", "文本"]
CLUSTER_KEYS = ["cluster", "clusters", "cluster_id", "clusterid", "label", "labels",
                "labels_", "cluster_label", "topic", "group", "簇", "类别", "聚类"]
PROB_KEYS = ["probability", "probabilities", "probabilities_", "prob", "probs",
             "membership", "membership_probability", "strength", "confidence", "概率"]
OUTLIER_KEYS = ["outlier_score", "outlier_scores", "outlier_scores_", "outlier", "glosh"]
X_KEYS = ["x", "umap_x", "umap_1", "umap1", "tsne_x", "tsne_1", "pca_x", "pc1",
          "dim_0", "dim0", "dim1", "component_1", "x_coord"]
Y_KEYS = ["y", "umap_y", "umap_2", "umap2", "tsne_y", "tsne_2", "pca_y", "pc2",
          "dim_1", "dim2", "component_2", "y_coord"]
CENTER_KEYS = ["is_center", "center", "is_centre", "centre", "exemplar",
               "is_exemplar", "medoid", "is_medoid", "中心"]

PARAM_KEYS = ["params", "parameters", "hdbscan_params", "hdbscan", "config", "settings", "args"]
RECORD_KEYS = ["points", "data", "records", "items", "rows", "results", "words_info"]

NOISE_VALUES = {"-1", "noise", "outlier", "none", "null", "nan", "", "噪声"}
TRUE_VALUES = {"1", "true", "t", "yes", "y", "是", "center", "centre"}


def _find_key(keys: list[str], candidates: list[str], override: str | None = None) -> str | None:
    """在 keys 里找第一个匹配 candidates 的列名（不区分大小写）。"""
    if override:
        if override in keys:
            return override
        lower = {k.lower(): k for k in keys}
        if override.lower() in lower:
            return lower[override.lower()]
        raise SystemExit(f"错误：找不到列 {override!r}，现有列：{', '.join(keys)}")
    lower = {str(k).strip().lower(): k for k in keys}
    for c in candidates:
        if c in lower:
            return lower[c]
    return None


def _to_float(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(str(v).strip())
    except ValueError:
        return None
    return f if math.isfinite(f) else None


def _to_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).strip().lower() in TRUE_VALUES


def _cluster_id(v: Any) -> int | str | None:
    """None 表示噪声。数字一律转成 int（"3.0" -> 3）。"""
    if v is None or isinstance(v, bool):
        return None
    s = str(v).strip()
    if s.lower() in NOISE_VALUES:
        return None
    f = _to_float(s)
    if f is not None and f == int(f):
        return None if int(f) < 0 else int(f)
    return s


def _parse_param_value(s: str) -> Any:
    s = s.strip()
    try:
        return json.loads(s)
    except ValueError:
        return s


# ---------------------------------------------------------------- 读文件

def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def load_table(text: str, suffix: str) -> tuple[list[dict], dict]:
    """CSV / TSV。开头的 # 注释行当作参数（key = value 或 key: value）。"""
    params: dict[str, Any] = {}
    lines = text.splitlines()
    body_start = 0
    for i, line in enumerate(lines):
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            kv = s.lstrip("#").strip()
            for sep in ("=", ":", "："):
                if sep in kv:
                    k, v = kv.split(sep, 1)
                    if k.strip():
                        params[k.strip()] = _parse_param_value(v)
                    break
            body_start = i + 1
            continue
        break
    body = "\n".join(lines[body_start:])
    if suffix == ".tsv":
        delimiter = "\t"
    else:
        try:
            delimiter = csv.Sniffer().sniff(body[:4096], delimiters=",\t;|").delimiter
        except csv.Error:
            delimiter = ","
    reader = csv.DictReader(io.StringIO(body), delimiter=delimiter)
    rows = [dict(r) for r in reader]
    return rows, params


def _records_from_clusters(clusters: Any, noise: Any) -> list[dict]:
    """JSON 按簇组织的格式 -> 记录列表。"""
    records: list[dict] = []
    if isinstance(clusters, dict):  # {"0": [...], "1": [...]} 或 {"0": {"center":..., "members":[...]}}
        items = []
        for k, v in clusters.items():
            if isinstance(v, dict):
                items.append({"id": k, **v})
            else:
                items.append({"id": k, "members": v})
    else:
        items = list(clusters)

    def add(member: Any, cid: Any, is_center: bool) -> None:
        if isinstance(member, dict):
            rec = dict(member)
        else:
            rec = {"word": member}
        rec["cluster"] = cid
        if is_center:
            rec["is_center"] = True
        records.append(rec)

    for idx, c in enumerate(items):
        if not isinstance(c, dict):
            c = {"members": c}
        cid = c.get("id", c.get("cluster", c.get("label", idx)))
        center = c.get("center", c.get("centre", c.get("exemplar")))
        members = c.get("members", c.get("words", c.get("points", c.get("items", []))))
        center_word = center.get("word") if isinstance(center, dict) else center
        seen_center = False
        for m in members:
            w = m.get("word", m.get("text")) if isinstance(m, dict) else m
            hit = center_word is not None and not seen_center and w == center_word
            seen_center = seen_center or hit
            add(m, cid, hit)
        if center is not None and not seen_center:
            add(center, cid, True)
    for m in noise or []:
        add(m, -1, False)
    return records


def load_json_obj(obj: Any) -> tuple[list[dict], dict]:
    params: dict[str, Any] = {}
    if isinstance(obj, list):
        return [r if isinstance(r, dict) else {"word": r} for r in obj], params
    if not isinstance(obj, dict):
        raise SystemExit("错误：JSON 顶层必须是列表或对象")

    for k in PARAM_KEYS:
        if isinstance(obj.get(k), dict):
            params.update(obj[k])
            break

    if "clusters" in obj and isinstance(obj["clusters"], (list, dict)):
        return _records_from_clusters(obj["clusters"], obj.get("noise")), params

    for k in RECORD_KEYS:
        if isinstance(obj.get(k), list) and obj[k] and isinstance(obj[k][0], dict):
            return obj[k], params

    # 并列数组：{"words": [...], "labels": [...], ...}
    arrays = {k: v for k, v in obj.items() if isinstance(v, list)}
    if arrays:
        label_key = _find_key(list(arrays), CLUSTER_KEYS)
        if label_key is None:
            raise SystemExit(f"错误：JSON 里没找到簇标签数组，现有键：{', '.join(obj)}")
        n = len(arrays[label_key])
        same = {k: v for k, v in arrays.items() if len(v) == n}
        records = [{k: v[i] for k, v in same.items()} for i in range(n)]
        return records, params
    raise SystemExit("错误：无法识别的 JSON 结构，请参考脚本开头的格式说明")


def load_file(path: Path) -> tuple[list[dict], dict, str]:
    suffix = path.suffix.lower()
    text = read_text(path)
    if suffix in (".json",):
        records, params = load_json_obj(json.loads(text))
        return records, params, "JSON"
    if suffix in (".jsonl", ".ndjson"):
        records = [json.loads(l) for l in text.splitlines() if l.strip()]
        return [r if isinstance(r, dict) else {"word": r} for r in records], {}, "JSONL"
    if suffix in (".csv", ".tsv", ".txt"):
        records, params = load_table(text, suffix)
        return records, params, suffix[1:].upper()
    # 不认识的后缀：先当 JSON，再当 CSV
    try:
        records, params = load_json_obj(json.loads(text))
        return records, params, "JSON"
    except ValueError:
        records, params = load_table(text, ".csv")
        return records, params, "CSV"


# ---------------------------------------------------------------- 整理成簇

def build_dataset(records: list[dict], params: dict, *, source: Path, fmt: str,
                  word_col: str | None = None, cluster_col: str | None = None,
                  prob_col: str | None = None, x_col: str | None = None,
                  y_col: str | None = None, center_col: str | None = None,
                  max_members: int = 0) -> dict:
    if not records:
        raise SystemExit("错误：输入文件里没有数据")
    keys: list[str] = []
    for r in records[:200]:
        for k in r:
            if k not in keys:
                keys.append(k)

    kc = _find_key(keys, CLUSTER_KEYS, cluster_col)
    if kc is None:
        raise SystemExit(f"错误：找不到簇标签列（如 cluster / label），现有列：{', '.join(keys)}；"
                         "可用 --cluster-col 指定")
    kw = _find_key(keys, WORD_KEYS, word_col)
    kp = _find_key(keys, PROB_KEYS, prob_col)
    ko = _find_key(keys, OUTLIER_KEYS)
    kx = _find_key(keys, X_KEYS, x_col)
    ky = _find_key(keys, Y_KEYS, y_col)
    kcen = _find_key(keys, CENTER_KEYS, center_col)
    if kx is None or ky is None:
        kx = ky = None
    extra_cols = [k for k in keys if k not in {kc, kw, kp, ko, kx, ky, kcen}]

    points: list[dict] = []
    for i, r in enumerate(records):
        word = r.get(kw) if kw else None
        word = f"#{i}" if word is None or str(word).strip() == "" else str(word)
        p = {
            "word": word,
            "cluster": _cluster_id(r.get(kc)),
            "prob": _to_float(r.get(kp)) if kp else None,
            "outlier": _to_float(r.get(ko)) if ko else None,
            "x": _to_float(r.get(kx)) if kx else None,
            "y": _to_float(r.get(ky)) if ky else None,
            "center": _to_bool(r.get(kcen)) if kcen else False,
        }
        extra = {k: r.get(k) for k in extra_cols[:6] if r.get(k) not in (None, "")}
        if extra:
            p["extra"] = {k: (v if isinstance(v, (int, float, str)) else json.dumps(v, ensure_ascii=False))
                          for k, v in extra.items()}
        points.append(p)

    has_xy = kx is not None and any(p["x"] is not None and p["y"] is not None for p in points)
    has_prob = kp is not None and any(p["prob"] is not None for p in points)

    groups: dict[Any, list[dict]] = {}
    noise: list[dict] = []
    for p in points:
        (noise if p["cluster"] is None else groups.setdefault(p["cluster"], [])).append(p)

    def sort_key(cid: Any) -> tuple:
        return (0, cid, "") if isinstance(cid, int) else (1, 0, str(cid))

    clusters = []
    for cid in sorted(groups, key=sort_key):
        members = groups[cid]
        center = pick_center(members)
        others = [m for m in members if m is not center]
        others.sort(key=lambda m: (-(m["prob"] if m["prob"] is not None else -1), m["word"]))
        truncated = 0
        if max_members and len(others) > max_members:
            truncated = len(others) - max_members
            others = others[:max_members]
        probs = [m["prob"] for m in members if m["prob"] is not None]
        clusters.append({
            "id": cid,
            "size": len(members),
            "center": center,
            "members": others,
            "truncated": truncated,
            "mean_prob": round(sum(probs) / len(probs), 4) if probs else None,
        })

    n = len(points)
    stats = {
        "n_points": n,
        "n_clusters": len(clusters),
        "n_noise": len(noise),
        "noise_ratio": round(len(noise) / n, 4) if n else 0,
        "largest": max((c["size"] for c in clusters), default=0),
        "smallest": min((c["size"] for c in clusters), default=0),
    }
    mapping = {"词": kw or "（无，用行号）", "簇标签": kc, "概率": kp or "—",
               "离群分数": ko or "—", "坐标": f"{kx}, {ky}" if kx else "—",
               "中心标记": kcen or "—"}
    return {
        "meta": {
            "file": source.name,
            "path": str(source),
            "format": fmt,
            "rows": n,
            "columns": keys,
            "mapping": mapping,
            "has_xy": has_xy,
            "has_prob": has_prob,
            "generated": _dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        },
        "params": params,
        "stats": stats,
        "clusters": clusters,
        "noise": noise,
    }


def pick_center(members: list[dict]) -> dict:
    flagged = [m for m in members if m["center"]]
    if flagged:
        return flagged[0]
    with_prob = [m for m in members if m["prob"] is not None]
    if with_prob:
        best = max(m["prob"] for m in with_prob)
        tied = [m for m in with_prob if m["prob"] == best]
        if len(tied) == 1:
            return tied[0]
        members = tied  # 概率并列（HDBSCAN 常见很多 1.0）时再按质心距离挑
    with_xy = [m for m in members if m["x"] is not None and m["y"] is not None]
    if with_xy:
        cx = sum(m["x"] for m in with_xy) / len(with_xy)
        cy = sum(m["y"] for m in with_xy) / len(with_xy)
        return min(with_xy, key=lambda m: (m["x"] - cx) ** 2 + (m["y"] - cy) ** 2)
    return members[0]


# ---------------------------------------------------------------- 输出 HTML

def render_html(dataset: dict, title: str) -> str:
    data = json.dumps(dataset, ensure_ascii=False, separators=(",", ":"))
    data = data.replace("</", "<\\/")
    return (HTML_TEMPLATE
            .replace("__TITLE__", _html_escape(title))
            .replace("__DATA__", data))


def _html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="把 HDBSCAN 聚类结果（JSON / CSV / TSV / JSONL）画成一个独立的 HTML 页面")
    ap.add_argument("input", help="聚类结果文件")
    ap.add_argument("-o", "--output", help="输出 HTML 路径（默认：<输入文件名>_hdbscan.html）")
    ap.add_argument("--title", help="页面标题（默认：HDBSCAN 聚类 · <文件名>）")
    ap.add_argument("--word-col", help="词所在的列名")
    ap.add_argument("--cluster-col", help="簇标签所在的列名")
    ap.add_argument("--prob-col", help="隶属概率所在的列名")
    ap.add_argument("--x-col", help="x 坐标列名")
    ap.add_argument("--y-col", help="y 坐标列名")
    ap.add_argument("--center-col", help="中心标记列名（值为 1/true 的是中心）")
    ap.add_argument("--params", help="HDBSCAN 参数 JSON 文件")
    ap.add_argument("--param", action="append", default=[], metavar="KEY=VALUE",
                    help="补充/覆盖一个 HDBSCAN 参数，可重复，如 --param min_cluster_size=10")
    ap.add_argument("--max-members", type=int, default=0,
                    help="每个簇最多画多少个词（按概率取前 N，0 = 不限）")
    ap.add_argument("--open", action="store_true", help="生成后用浏览器打开")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    src = Path(args.input)
    if not src.is_file():
        raise SystemExit(f"错误：找不到文件 {src}")

    records, params, fmt = load_file(src)
    if args.params:
        extra = json.loads(read_text(Path(args.params)))
        if not isinstance(extra, dict):
            raise SystemExit("错误：--params 文件必须是 JSON 对象")
        params.update(extra)
    for item in args.param:
        if "=" not in item:
            raise SystemExit(f"错误：--param 需要 KEY=VALUE 形式，收到 {item!r}")
        k, v = item.split("=", 1)
        params[k.strip()] = _parse_param_value(v)

    dataset = build_dataset(
        records, params, source=src, fmt=fmt,
        word_col=args.word_col, cluster_col=args.cluster_col, prob_col=args.prob_col,
        x_col=args.x_col, y_col=args.y_col, center_col=args.center_col,
        max_members=args.max_members,
    )
    out = Path(args.output) if args.output else src.with_name(src.stem + "_hdbscan.html")
    title = args.title or f"HDBSCAN 聚类 · {src.name}"
    out.write_text(render_html(dataset, title), encoding="utf-8")

    s = dataset["stats"]
    print(f"读取 {src.name}（{fmt}）：{s['n_points']} 个点，{s['n_clusters']} 个簇，"
          f"噪声 {s['n_noise']}（{s['noise_ratio']:.1%}）")
    print(f"列映射：" + "，".join(f"{k}={v}" for k, v in dataset["meta"]["mapping"].items()))
    if params:
        print("参数：" + "，".join(f"{k}={v}" for k, v in params.items()))
    print(f"已生成：{out}")
    if args.open:
        webbrowser.open(out.resolve().as_uri())
    return 0


# ---------------------------------------------------------------- 页面模板

HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10); --hover: rgba(11,11,11,0.05);
  --halo: #fcfcfb; --noise: #b4b2ab;
  --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a; --s4:#eda100; --s5:#e87ba4; --s6:#008300; --s7:#6250d6; --s8:#e34948;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10); --hover: rgba(255,255,255,0.06);
    --halo: #1a1a19; --noise: #5f5e5a;
    --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500; --s5:#d55181; --s6:#008300; --s7:#9085e9; --s8:#e66767;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10); --hover: rgba(255,255,255,0.06);
  --halo: #1a1a19; --noise: #5f5e5a;
  --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500; --s5:#d55181; --s6:#008300; --s7:#9085e9; --s8:#e66767;
}
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; }
body { background: var(--page); color: var(--ink); font: 13px/1.45 system-ui, -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; }
.app { display: grid; grid-template-columns: 320px 1fr; height: 100vh; }
aside { overflow-y: auto; border-right: 1px solid var(--border); background: var(--surface); padding: 16px; }
h1 { font-size: 15px; margin: 0 0 2px; font-weight: 600; word-break: break-all; }
.sub { color: var(--muted); font-size: 12px; margin-bottom: 14px; }
h2 { font-size: 12px; font-weight: 600; color: var(--ink-2); margin: 18px 0 6px; text-transform: none; letter-spacing: .02em; }
.tiles { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.tile { border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; }
.tile .v { font-size: 18px; font-weight: 600; }
.tile .k { font-size: 11px; color: var(--muted); }
table.kv { width: 100%; border-collapse: collapse; }
table.kv td { padding: 3px 0; border-bottom: 1px solid var(--border); vertical-align: top; }
table.kv td:first-child { color: var(--ink-2); padding-right: 10px; white-space: nowrap; }
table.kv td:last-child { text-align: right; font-variant-numeric: tabular-nums; word-break: break-all; }
.empty { color: var(--muted); font-size: 12px; }
.controls { display: flex; flex-wrap: wrap; gap: 6px 12px; align-items: center; }
.controls label { display: inline-flex; gap: 4px; align-items: center; cursor: pointer; }
input[type=search] { width: 100%; padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; background: var(--page); color: var(--ink); font: inherit; }
.seg { display: inline-flex; border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
.seg button { border: 0; background: transparent; color: var(--ink-2); padding: 4px 10px; font: inherit; cursor: pointer; }
.seg button.on { background: var(--hover); color: var(--ink); font-weight: 600; }
.seg button:disabled { color: var(--muted); cursor: not-allowed; }
.legend { list-style: none; margin: 0; padding: 0; }
.legend li { display: grid; grid-template-columns: 12px 1fr auto; gap: 8px; align-items: center; padding: 5px 6px; border-radius: 6px; cursor: pointer; }
.legend li:hover { background: var(--hover); }
.legend li.on { background: var(--hover); outline: 1px solid var(--border); }
.legend li.off { opacity: .35; }
.sw { width: 12px; height: 12px; border-radius: 3px; }
.legend .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.legend .name b { font-weight: 600; }
.legend .n { color: var(--ink-2); font-variant-numeric: tabular-nums; font-size: 12px; }
.bar { height: 3px; border-radius: 2px; background: var(--grid); margin-top: 3px; }
.bar i { display: block; height: 100%; border-radius: 2px; }
main { position: relative; overflow: hidden; background: var(--surface); }
svg#canvas { width: 100%; height: 100%; display: block; cursor: grab; touch-action: none; }
svg#canvas.drag { cursor: grabbing; }
.toolbar { position: absolute; top: 12px; right: 12px; display: flex; gap: 6px; }
.toolbar button { border: 1px solid var(--border); background: var(--surface); color: var(--ink); border-radius: 6px; padding: 5px 10px; font: inherit; cursor: pointer; }
.toolbar button:hover { background: var(--hover); }
.hint { position: absolute; left: 12px; bottom: 10px; color: var(--muted); font-size: 11px; pointer-events: none; }
#tip { position: absolute; pointer-events: none; background: var(--surface); color: var(--ink); border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; box-shadow: 0 4px 16px rgba(0,0,0,.12); font-size: 12px; max-width: 280px; display: none; z-index: 5; }
#tip .t { font-weight: 600; margin-bottom: 4px; display: flex; gap: 6px; align-items: center; word-break: break-all; }
#tip .r { display: flex; justify-content: space-between; gap: 14px; color: var(--ink-2); }
#tip .r span:last-child { color: var(--ink); font-variant-numeric: tabular-nums; }
.dim { opacity: .1; }
.hit circle.node { stroke: var(--ink) !important; stroke-width: 2.5px !important; }
.hit text { font-weight: 700; }
.nolabels text.ml { display: none; }
.tiny text.ml { display: none; }
@media (max-width: 760px) {
  .app { grid-template-columns: 1fr; grid-template-rows: auto 70vh; height: auto; }
  aside { border-right: 0; border-bottom: 1px solid var(--border); max-height: none; }
  main { height: 70vh; }
}
</style>
</head>
<body>
<div class="app">
  <aside>
    <h1 id="title"></h1>
    <div class="sub" id="subtitle"></div>

    <div class="tiles" id="tiles"></div>

    <h2>输入文件</h2>
    <table class="kv" id="fileinfo"></table>

    <h2>HDBSCAN 参数</h2>
    <table class="kv" id="params"></table>

    <h2>显示</h2>
    <div class="controls">
      <span class="seg" id="layoutSeg">
        <button data-l="radial" class="on">环形</button>
        <button data-l="coords">原始坐标</button>
      </span>
      <label><input type="checkbox" id="showNoise" checked> 噪声</label>
      <label><input type="checkbox" id="showLabels" checked> 词标签</label>
    </div>
    <div style="margin-top:8px"><input type="search" id="search" placeholder="搜索词，回车定位…"></div>

    <h2 id="legendTitle">簇（点击定位，再点取消）</h2>
    <ul class="legend" id="legend"></ul>
  </aside>
  <main>
    <svg id="canvas" xmlns="http://www.w3.org/2000/svg"><g id="vp"></g></svg>
    <div class="toolbar">
      <button id="fit" title="适应窗口">适应窗口</button>
      <button id="svgBtn" title="导出 SVG">导出 SVG</button>
      <button id="pngBtn" title="导出 PNG">导出 PNG</button>
    </div>
    <div class="hint">滚轮缩放 · 拖动平移 · 悬停查看详情</div>
    <div id="tip"></div>
  </main>
</div>
<script>
const DATA = __DATA__;
(function () {
  "use strict";
  const NS = "http://www.w3.org/2000/svg";
  const $ = (id) => document.getElementById(id);
  const svg = $("canvas"), vp = $("vp"), tip = $("tip");
  const M = DATA.meta, S = DATA.stats, CL = DATA.clusters, NOISE = DATA.noise;
  const FONT = 11, CHAR_W = 6.6, SPACING = 15, INNER = 0.55;

  // ---------- colors: 8 validated slots, then golden-angle hues (clusters are also labeled, so color is never the only cue)
  function css(v) { return getComputedStyle(document.documentElement).getPropertyValue(v).trim(); }
  function colorOf(i) {
    if (i < 8) return css("--s" + (i + 1));
    const dark = css("color-scheme") === "dark";
    const h = (i * 137.508 + 20) % 360, l = dark ? 58 : 46 + (i % 2) * 8;
    return `hsl(${h.toFixed(0)} 62% ${l}%)`;
  }
  const fmt = (v) => v == null ? "—" : (typeof v === "number" ? (Number.isInteger(v) ? v.toLocaleString() : (+v.toFixed(4)).toString()) : String(v));
  const pct = (v) => (v * 100).toFixed(1) + "%";
  const textW = (s) => { let w = 0; for (const ch of s) w += ch.charCodeAt(0) > 0x2e80 ? FONT : CHAR_W; return w; };
  const clip = (s, n = 24) => { const a = [...s]; return a.length > n ? a.slice(0, n - 1).join("") + "…" : s; };
  function el(tag, attrs, parent) {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }
  function esc(s) { return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]); }

  // ---------- sidebar
  $("title").textContent = document.title;
  $("subtitle").textContent = `${M.format} · 生成于 ${M.generated}`;
  const tiles = [
    [S.n_clusters, "簇数"], [S.n_points, "点数"],
    [S.n_noise, `噪声（${pct(S.noise_ratio)}）`], [S.n_clusters ? `${S.smallest}–${S.largest}` : "—", "簇大小范围"],
  ];
  $("tiles").innerHTML = tiles.map(([v, k]) => `<div class="tile"><div class="v">${esc(fmt(v))}</div><div class="k">${esc(k)}</div></div>`).join("");
  const fileRows = [["文件名", M.file], ["格式", M.format], ["行数", M.rows], ["列数", M.columns.length]]
    .concat(Object.entries(M.mapping).map(([k, v]) => [k + "列", v]));
  $("fileinfo").innerHTML = fileRows.map(([k, v]) => `<tr><td>${esc(k)}</td><td title="${esc(v)}">${esc(fmt(v))}</td></tr>`).join("");
  const pEntries = Object.entries(DATA.params || {});
  $("params").innerHTML = pEntries.length
    ? pEntries.map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(typeof v === "object" && v !== null ? JSON.stringify(v) : fmt(v))}</td></tr>`).join("")
    : `<tr><td colspan="2" class="empty">文件里没有参数。可用 --param min_cluster_size=10 补充</td></tr>`;
  if (!M.has_xy) { const b = document.querySelector('[data-l="coords"]'); b.disabled = true; b.title = "数据里没有 x / y 坐标列"; }

  // ---------- state
  const state = { layout: "radial", focus: null, hidden: new Set(), noise: true, labels: true, query: "" };
  let view = { x: 0, y: 0, k: 1 }, bounds = null, nodes = [];

  function buildLegend() {
    const ul = $("legend"); ul.innerHTML = "";
    CL.forEach((c, i) => {
      const li = document.createElement("li");
      li.className = (state.focus === i ? "on" : "") + (state.hidden.has(i) ? " off" : "");
      const meanP = c.mean_prob == null ? "" : ` · 均概率 ${(+c.mean_prob).toFixed(2)}`;
      li.title = `簇 ${c.id}：中心「${c.center.word}」，${c.size} 个词${meanP}${c.truncated ? `（图中省略 ${c.truncated} 个）` : ""}\n点击定位；Alt/⌥+点击 显示/隐藏`;
      li.innerHTML = `<span class="sw" style="background:${colorOf(i)}"></span>
        <span class="name"><b>簇 ${esc(c.id)}</b> · ${esc(clip(c.center.word, 18))}
          <div class="bar"><i style="width:${(100 * c.size / Math.max(1, S.largest)).toFixed(1)}%;background:${colorOf(i)}"></i></div></span>
        <span class="n">${c.size}</span>`;
      li.onclick = (e) => {
        if (e.altKey) { state.hidden.has(i) ? state.hidden.delete(i) : state.hidden.add(i); render(); return; }
        state.focus = state.focus === i ? null : i; render(); state.focus == null ? fit() : zoomTo(clusterBox(i));
      };
      ul.appendChild(li);
    });
    if (NOISE.length) {
      const li = document.createElement("li");
      li.className = state.noise ? "" : "off";
      li.innerHTML = `<span class="sw" style="background:${css("--noise")}"></span><span class="name"><b>噪声</b> · label = -1</span><span class="n">${NOISE.length}</span>`;
      li.onclick = () => { state.noise = !state.noise; $("showNoise").checked = state.noise; render(); };
      ul.appendChild(li);
    }
    if (!CL.length && !NOISE.length) ul.innerHTML = `<li class="empty">没有数据</li>`;
  }

  // ---------- layouts: each returns {clusters:[{cx,cy,R,title:{x,y},center:{x,y},members:[{x,y,a}]}], noise:[{x,y}], box}
  function radialLayout() {
    const shapes = CL.map((c) => {
      const n = c.members.length;
      const R = n ? Math.max(70, (n * SPACING) / (2 * Math.PI * INNER)) : 0;
      const lab = n ? Math.min(170, Math.max(...c.members.map((m) => textW(clip(m.word))))) : 0;
      const foot = Math.max(R + lab + 18, textW(clip(c.center.word, 18)) / 2 + 30, 60);
      return { R, foot, n };
    });
    const order = CL.map((_, i) => i).sort((a, b) => shapes[b].foot - shapes[a].foot);
    const area = shapes.reduce((s, sh) => s + (2 * sh.foot + 40) ** 2, 0);
    const rowW = Math.max(shapes.length ? 2 * shapes[order[0]].foot + 40 : 600, Math.sqrt(area) * 1.5, 600);
    const pos = new Array(CL.length);
    let x = 0, y = 0, rowH = 0;
    for (const i of order) {
      const d = 2 * shapes[i].foot + 40;
      if (x > 0 && x + d > rowW) { x = 0; y += rowH; rowH = 0; }
      pos[i] = { cx: x + d / 2, cy: y + d / 2 + 14 };
      x += d; rowH = Math.max(rowH, d + 14);
    }
    const clusters = CL.map((c, i) => {
      const { cx, cy } = pos[i], { R, n, foot } = shapes[i];
      const members = c.members.map((m, j) => {
        const a = -Math.PI / 2 + (2 * Math.PI * j) / n;
        const p = m.prob == null ? 1 : Math.max(0, Math.min(1, m.prob));
        const r = M.has_prob ? R * (INNER + (1 - INNER) * (1 - p)) : R;
        return { x: cx + r * Math.cos(a), y: cy + r * Math.sin(a), a };
      });
      return { cx, cy, foot, center: { x: cx, y: cy }, title: { x: cx, y: cy - foot + 4 }, members };
    });
    // noise block below
    const noise = [];
    if (NOISE.length && state.noise) {
      y += rowH + 40;
      const cellW = Math.min(170, Math.max(70, ...NOISE.map((p) => textW(clip(p.word, 18)) + 22)));
      const cols = Math.max(1, Math.floor(rowW / cellW));
      NOISE.forEach((p, j) => noise.push({ x: (j % cols) * cellW + 6, y: y + 30 + Math.floor(j / cols) * 20, a: 0 }));
      noise.titleAt = { x: 0, y: y + 8 };
    }
    return { clusters, noise };
  }

  function coordsLayout() {
    const all = [];
    CL.forEach((c) => { all.push(c.center, ...c.members); });
    NOISE.forEach((p) => all.push(p));
    const pts = all.filter((p) => p.x != null && p.y != null);
    const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y);
    const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
    const W = 1400, sc = W / Math.max(x1 - x0, y1 - y0, 1e-9);
    const P = (p) => p.x == null || p.y == null ? null : { x: (p.x - x0) * sc, y: (y1 - p.y) * sc, a: 0 };
    const clusters = CL.map((c) => {
      const center = P(c.center) || { x: 0, y: 0 };
      const members = c.members.map((m) => P(m) || center);
      const ys2 = [center.y, ...members.map((m) => m.y)];
      return { cx: center.x, cy: center.y, center, title: { x: center.x, y: Math.min(...ys2) - 14 }, members };
    });
    const noise = NOISE.map((p) => P(p)).map((p) => p || { x: 0, y: 0, a: 0 });
    return { clusters, noise: state.noise ? noise : [] };
  }

  function clusterBox(i) {
    const n = nodes.filter((d) => d.ci === i);
    if (!n.length) return bounds;
    const pad = state.layout === "radial" ? 0 : 30;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    n.forEach((d) => { x0 = Math.min(x0, d.x - (d.ext || 0)); y0 = Math.min(y0, d.y - (d.ext || 0)); x1 = Math.max(x1, d.x + (d.ext || 0)); y1 = Math.max(y1, d.y + (d.ext || 0)); });
    return { x0: x0 - pad, y0: y0 - pad - 20, x1: x1 + pad, y1: y1 + pad };
  }

  // ---------- render
  function render() {
    buildLegend();
    vp.innerHTML = "";
    nodes = [];
    const L = state.layout === "coords" ? coordsLayout() : radialLayout();
    const ink = css("--ink"), ink2 = css("--ink-2"), halo = css("--halo"), muted = css("--muted"), noiseC = css("--noise");
    const q = state.query.trim().toLowerCase();
    const radial = state.layout === "radial";
    svg.classList.toggle("nolabels", !state.labels);

    L.clusters.forEach((g, i) => {
      if (state.hidden.has(i)) return;
      const c = CL[i], col = colorOf(i);
      const grp = el("g", { class: "cluster" + (state.focus != null && state.focus !== i ? " dim" : "") }, vp);
      if (radial) el("circle", { cx: g.cx, cy: g.cy, r: g.foot - 10, fill: col, "fill-opacity": 0.04, stroke: col, "stroke-opacity": 0.25, "stroke-dasharray": "3 4" }, grp);
      // spokes
      const lines = el("g", {}, grp);
      c.members.forEach((m, j) => {
        const p = g.members[j];
        const w = m.prob == null ? 0.6 : 0.3 + 0.5 * m.prob;
        el("line", { x1: g.center.x, y1: g.center.y, x2: p.x, y2: p.y, stroke: col, "stroke-width": 1, "stroke-opacity": w }, lines);
      });
      // members
      c.members.forEach((m, j) => {
        const p = g.members[j];
        const ng = el("g", { class: "pt" }, grp);
        el("circle", { class: "node", cx: p.x, cy: p.y, r: 4, fill: col, stroke: halo, "stroke-width": 1.5 }, ng);
        const label = clip(m.word);
        if (radial) {
          let deg = (p.a * 180) / Math.PI, flip = Math.cos(p.a) < 0;
          if (flip) deg += 180;
          el("text", { class: "ml", x: p.x, y: p.y, dy: "0.35em", dx: flip ? -8 : 8, "text-anchor": flip ? "end" : "start",
            transform: `rotate(${deg.toFixed(2)} ${p.x.toFixed(2)} ${p.y.toFixed(2)})`, fill: ink2, "font-size": FONT }, ng).textContent = label;
        } else {
          el("text", { class: "ml", x: p.x + 6, y: p.y, dy: "0.35em", fill: ink2, "font-size": FONT - 1 }, ng).textContent = label;
        }
        const hit = q && m.word.toLowerCase().includes(q);
        if (hit) ng.classList.add("hit");
        nodes.push({ el: ng, ci: i, p: m, x: p.x, y: p.y, ext: radial ? textW(label) + 10 : 6, hit, kind: "member" });
      });
      // center on top
      const cg = el("g", { class: "pt" }, grp);
      el("circle", { class: "node", cx: g.center.x, cy: g.center.y, r: radial ? 9 : 7, fill: col, stroke: halo, "stroke-width": 2.5 }, cg);
      el("text", { x: g.center.x, y: g.center.y + (radial ? 24 : 20), "text-anchor": "middle", fill: ink, "font-size": 13, "font-weight": 700,
        stroke: halo, "stroke-width": 4, "paint-order": "stroke", "stroke-linejoin": "round" }, cg).textContent = clip(c.center.word, 18);
      const chit = q && c.center.word.toLowerCase().includes(q);
      if (chit) cg.classList.add("hit");
      nodes.push({ el: cg, ci: i, p: c.center, x: g.center.x, y: g.center.y, ext: radial ? g.foot - 10 : 10, hit: chit, kind: "center" });
      // title
      const t = el("text", { x: g.title.x, y: g.title.y, "text-anchor": "middle", fill: ink, "font-size": 12, "font-weight": 600,
        stroke: halo, "stroke-width": 4, "paint-order": "stroke" }, grp);
      t.textContent = `簇 ${c.id} · ${c.size} 个${c.truncated ? `（省略 ${c.truncated}）` : ""}`;
    });

    if (L.noise.length) {
      const grp = el("g", { class: state.focus != null ? "dim" : "" }, vp);
      if (L.noise.titleAt) el("text", { x: L.noise.titleAt.x, y: L.noise.titleAt.y, fill: muted, "font-size": 12, "font-weight": 600 }, grp)
        .textContent = `噪声点（label = -1）· ${NOISE.length} 个`;
      NOISE.forEach((p, j) => {
        const q2 = L.noise[j];
        const ng = el("g", { class: "pt" }, grp);
        el("circle", { class: "node", cx: q2.x, cy: q2.y, r: 3.5, fill: noiseC, stroke: halo, "stroke-width": 1 }, ng);
        if (radial) el("text", { class: "ml", x: q2.x + 8, y: q2.y, dy: "0.35em", fill: muted, "font-size": FONT - 1 }, ng).textContent = clip(p.word, 18);
        const hit = q && p.word.toLowerCase().includes(q);
        if (hit) ng.classList.add("hit");
        nodes.push({ el: ng, ci: -1, p, x: q2.x, y: q2.y, ext: 4, hit, kind: "noise" });
      });
    }

    // bounds
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    nodes.forEach((d) => {
      const e = d.ext || 0, right = d.kind === "noise" && radial ? 170 : e;
      x0 = Math.min(x0, d.x - e); x1 = Math.max(x1, d.x + right);
      y0 = Math.min(y0, d.y - e - (d.kind === "center" ? 24 : 0)); y1 = Math.max(y1, d.y + e);
    });
    if (L.noise.titleAt) y0 = Math.min(y0, L.noise.titleAt.y - 14);
    bounds = nodes.length ? { x0: x0 - 20, y0: y0 - 20, x1: x1 + 20, y1: y1 + 20 } : { x0: 0, y0: 0, x1: 100, y1: 100 };
    applyView();
  }

  // ---------- zoom & pan
  function applyView() {
    vp.setAttribute("transform", `translate(${view.x},${view.y}) scale(${view.k})`);
    svg.classList.toggle("tiny", view.k * FONT < 4.5);
  }
  function zoomTo(b) {
    const r = svg.getBoundingClientRect();
    const w = Math.max(1, b.x1 - b.x0), h = Math.max(1, b.y1 - b.y0);
    view.k = Math.min(r.width / w, r.height / h) * 0.94;
    view.x = r.width / 2 - ((b.x0 + b.x1) / 2) * view.k;
    view.y = r.height / 2 - ((b.y0 + b.y1) / 2) * view.k;
    applyView();
  }
  function fit() { zoomTo(bounds); }
  svg.addEventListener("wheel", (e) => {
    e.preventDefault();
    const r = svg.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
    const f = Math.exp(-e.deltaY * 0.0015), k = Math.max(0.01, Math.min(40, view.k * f));
    view.x = mx - ((mx - view.x) * k) / view.k; view.y = my - ((my - view.y) * k) / view.k; view.k = k;
    applyView();
  }, { passive: false });
  let drag = null;
  svg.addEventListener("pointerdown", (e) => { drag = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y, moved: false }; svg.setPointerCapture(e.pointerId); svg.classList.add("drag"); });
  svg.addEventListener("pointermove", (e) => {
    if (drag) {
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
      view.x = drag.vx + dx; view.y = drag.vy + dy; applyView(); tip.style.display = "none"; return;
    }
    showTip(e);
  });
  svg.addEventListener("pointerup", (e) => {
    svg.classList.remove("drag");
    if (drag && !drag.moved && state.focus != null && !e.target.closest(".pt")) { state.focus = null; render(); }
    drag = null;
  });
  svg.addEventListener("pointerleave", () => { tip.style.display = "none"; });

  // ---------- tooltip
  function showTip(e) {
    const g = e.target.closest && e.target.closest(".pt");
    const d = g && nodes.find((n) => n.el === g);
    if (!d) { tip.style.display = "none"; return; }
    const p = d.p, c = d.ci >= 0 ? CL[d.ci] : null;
    const col = d.ci >= 0 ? colorOf(d.ci) : css("--noise");
    const rows = [["簇", c ? `${c.id}${d.kind === "center" ? "（中心）" : ""}` : "噪声 (-1)"]];
    if (c && d.kind !== "center") rows.push(["簇中心", c.center.word]);
    if (p.prob != null) rows.push(["隶属概率", (+p.prob).toFixed(3)]);
    if (p.outlier != null) rows.push(["离群分数", (+p.outlier).toFixed(3)]);
    if (p.x != null && p.y != null) rows.push(["坐标", `${(+p.x).toFixed(3)}, ${(+p.y).toFixed(3)}`]);
    if (c && d.kind === "center") rows.push(["簇大小", c.size], ["平均概率", c.mean_prob == null ? "—" : (+c.mean_prob).toFixed(3)]);
    if (p.extra) for (const k in p.extra) rows.push([k, clip(String(p.extra[k]), 40)]);
    tip.innerHTML = `<div class="t"><span class="sw" style="background:${col}"></span>${esc(p.word)}</div>` +
      rows.map(([k, v]) => `<div class="r"><span>${esc(k)}</span><span>${esc(v)}</span></div>`).join("");
    const r = svg.getBoundingClientRect();
    tip.style.display = "block";
    let x = e.clientX - r.left + 14, y = e.clientY - r.top + 14;
    if (x + tip.offsetWidth > r.width - 8) x = e.clientX - r.left - tip.offsetWidth - 14;
    if (y + tip.offsetHeight > r.height - 8) y = e.clientY - r.top - tip.offsetHeight - 14;
    tip.style.left = Math.max(4, x) + "px"; tip.style.top = Math.max(4, y) + "px";
  }

  // ---------- controls
  document.querySelectorAll("#layoutSeg button").forEach((b) => b.onclick = () => {
    if (b.disabled) return;
    state.layout = b.dataset.l;
    document.querySelectorAll("#layoutSeg button").forEach((x) => x.classList.toggle("on", x === b));
    render(); state.focus == null ? fit() : zoomTo(clusterBox(state.focus));
  });
  $("showNoise").onchange = (e) => { state.noise = e.target.checked; render(); fit(); };
  $("showLabels").onchange = (e) => { state.labels = e.target.checked; render(); };
  $("search").oninput = (e) => { state.query = e.target.value; render(); };
  $("search").onkeydown = (e) => {
    if (e.key !== "Enter") return;
    const hits = nodes.filter((n) => n.hit);
    if (!hits.length) return;
    const b = hits.reduce((a, n) => ({ x0: Math.min(a.x0, n.x), y0: Math.min(a.y0, n.y), x1: Math.max(a.x1, n.x), y1: Math.max(a.y1, n.y) }), { x0: Infinity, y0: Infinity, x1: -Infinity, y1: -Infinity });
    zoomTo({ x0: b.x0 - 160, y0: b.y0 - 120, x1: b.x1 + 160, y1: b.y1 + 120 });
  };
  $("fit").onclick = fit;

  // ---------- export
  function exportSvg() {
    const b = bounds, w = b.x1 - b.x0, h = b.y1 - b.y0;
    const clone = vp.cloneNode(true);
    clone.setAttribute("transform", `translate(${-b.x0},${-b.y0})`);
    if (!state.labels) clone.querySelectorAll("text.ml").forEach((t) => t.remove());
    const font = getComputedStyle(document.body).fontFamily.replace(/"/g, "'");
    return `<svg xmlns="${NS}" width="${w.toFixed(0)}" height="${h.toFixed(0)}" viewBox="0 0 ${w.toFixed(0)} ${h.toFixed(0)}" font-family="${font}">` +
      `<style>.dim{opacity:.1}.hit circle.node{stroke:${css("--ink")};stroke-width:2.5px}</style>` +
      `<rect width="100%" height="100%" fill="${css("--surface")}"/>${new XMLSerializer().serializeToString(clone)}</svg>`;
  }
  function download(name, blob) {
    const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = name;
    document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  }
  const base = M.file.replace(/\.[^.]+$/, "") + "_hdbscan";
  $("svgBtn").onclick = () => download(base + ".svg", new Blob([exportSvg()], { type: "image/svg+xml" }));
  $("pngBtn").onclick = () => {
    const src = exportSvg(), b = bounds;
    const scale = Math.min(2, 8000 / Math.max(b.x1 - b.x0, b.y1 - b.y0));
    const img = new Image();
    img.onload = () => {
      const c = document.createElement("canvas");
      c.width = Math.ceil((b.x1 - b.x0) * scale); c.height = Math.ceil((b.y1 - b.y0) * scale);
      const ctx = c.getContext("2d"); ctx.scale(scale, scale); ctx.drawImage(img, 0, 0);
      c.toBlob((blob) => download(base + ".png", blob), "image/png");
    };
    img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(src);
  };

  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", render);
  let rt; window.addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(fit, 120); });
  render(); fit();
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
