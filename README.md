# NVDRS 分组拆分工具

把已经按年龄段切好的 NVDRS CSV（`18-30`、`31-40`、`41-50`、`51-60`、`61-70`），
再按两个维度拆分：

1. **circumstance known** —— 布尔化（`Yes` → `True`，`No` → `False`）
2. **occupation** —— 四分类：`construction` / `non_construction` / `non_workforce` / `military`

## 快速开始

```bash
pip install pandas

# 1) 先看脚本在你的文件里认出了哪些列、各列有哪些取值（不写任何文件）
python nvdrs_split.py --input path/to/your/csvs/ --inspect

# 2) 确认列名对了，再正式跑
python nvdrs_split.py --input path/to/your/csvs/ --output-dir out/
```

`--input` 可以是一个目录（读取其中所有 `*.csv`），也可以是若干个文件：

```bash
python nvdrs_split.py --input data/nvdrs_18-30.csv data/nvdrs_31-40.csv --output-dir out/
```

年龄段标签直接从文件名里解析（匹配 `18-30`、`18_30`、`18 to 30` 这类模式）；
认不出来就用文件名本身作为标签。

## 输出结构

```
out/
├── labeled/
│   └── 18_30_labeled.csv                 原始列 + 4 个派生列
├── splits/
│   └── 18_30/
│       ├── construction__circumstance_known_yes.csv
│       ├── construction__circumstance_known_no.csv
│       ├── non_construction__circumstance_known_yes.csv
│       ├── ...                            （4 类 × 2 布尔 = 8 个文件/年龄段）
│       └── military__circumstance_known_undetermined.csv
├── summary_counts.csv                     长表：年龄段 × 职业类 × 各布尔计数
├── summary_crosstab_circumstance_known_yes.csv   宽表：年龄段 × 职业类（仅 known=Yes）
├── occupation_group_audit.csv             每个职业文本 → 归到哪类、命中哪条规则、几行
└── unmapped_occupations.csv               只命中兜底规则的职业值，需人工复核
```

`labeled/` 里加的 4 列：

| 列 | 含义 |
|---|---|
| `circumstance_known_bool` | `True` / `False` / `<NA>` |
| `occupation_group` | 四分类之一，或 `unclassified` |
| `occupation_group_rule` | 这行是靠哪条规则定的（见下） |
| `age_band` | 从文件名解析出的年龄段 |

`splits/` 各文件是原数据的一个**无重叠、无遗漏的划分**（测试里有断言校验）。

## 分类规则

### circumstance known

脚本自动寻找列名（忽略大小写/空格/下划线）：`CircumstancesKnown`、`CircumstanceKnown`
等；找不到就用 `--circumstance-col` 显式指定。

- `Yes` / `Y` / `True` / `1` / `是` → `True`
- `No` / `N` / `False` / `0` / `否` → `False`
- **其他一切**（空值、`Unknown`、`Not available`…）→ `<NA>`，**不会**被当成 `False`

`<NA>` 的行默认单独写成 `*_known_undetermined.csv`，方便你先看清有多少这种行；
确认要丢掉就加 `--drop-unknown-circumstance`。

### occupation 四分类

判定按阶段进行，先命中先定；每个阶段内部的优先级是
**military > construction > non_workforce > non_construction**。

**阶段 1：Census 2010 职业编码**（`OccupationCode` 等列，NVDRS 用的就是这套）

| 组 | 编码范围 |
|---|---|
| `military` | 9800–9830（Military Specific Occupations） |
| `construction` | 6200–6765（建筑工种及其一线主管） |
| `non_workforce` | 9840（从未工作 / 近 5 年无工作经历）、9920（失业无工作经历） |
| `non_construction` | 其余任何 < 9800 的有效民用编码 |

6800–6940（extraction，采掘业）**默认不算** construction。如果你的定义要把采掘
并进来，加 `--include-extraction`。

**阶段 2：职业自由文本**关键词匹配（编码缺失或落在无效区间时）。
先判"未知"类取值（`Unknown`、`N/A`…），再按 military → construction →
non_workforce 顺序匹配关键词。

**阶段 3：行业列**（可选）。前两阶段没定下来时，若行业编码为 `0770`（Census 2010
的 Construction）或行业文本含 `construction`，归入 `construction`。

**阶段 4：兜底**。职业文本非空但没命中任何关键词 → `non_construction`
（即"有职业、但不是建筑也不是军人"），并记入 `unmapped_occupations.csv`。
职业信息完全缺失 → `unclassified`。

`occupation_group_rule` 列会写明是哪一步定的：`occupation_code`、
`occupation_keyword`、`industry_code`、`industry_keyword`、`fallback_text`、
`occupation_unknown_value`、`missing_occupation`。

### 几个需要你确认的口径判断

这些是我按常见做法定的默认口径，不同意就改（改法见下一节）：

- **`military` = 现役**。`veteran`、`retired military` 这类文本归到 `non_workforce`
  （已退出劳动力），不算 military。NVDRS 另有专门的退伍军人变量，不在本脚本处理范围。
- **`non_workforce` = 不在劳动力人口中**：失业、退休、学生、家务、残障无法工作、
  被收容/服刑、无偿劳动等。
- **`non_construction` = 在职但非建筑、非军人**的民用职业。
- 四类互斥，加上 `unclassified` 后覆盖全部行。
- `painter` 默认算 construction（NVDRS 文本里多指建筑油漆工）。如果你的数据里
  艺术类画家不少，从关键词表里删掉它。

## 调整规则

导出默认规则、改完再喂回去：

```bash
python nvdrs_split.py --dump-rules > my_rules.json
# 编辑 my_rules.json
python nvdrs_split.py --input data/ --output-dir out/ --rules my_rules.json
```

`--rules` 是**增量覆盖**：只写你要改的键，其余沿用默认。`code_ranges` 和 `keywords`
这两个字典按**子键**合并，所以可以只覆盖其中一项；但被覆盖的那一项是**整体替换**
（列表不做合并），要写全你想要的完整列表。仓库里
`config/occupation_rules.default.json` 是默认值的副本，可直接当模板拷贝需要的列表。

比如把采掘业并入建筑（只需覆盖两个子键）：

```json
{
  "code_ranges": {
    "construction": [[6200, 6940]],
    "construction_extraction": []
  }
}
```

而要把 `painter` 从建筑关键词里去掉，就得把 `keywords.construction` 整个列表
（从默认 JSON 里拷出来、删掉 `painter` 和 `painting contractor`）写进覆盖文件。

**建议流程**：跑完先看 `unmapped_occupations.csv` 和 `occupation_group_audit.csv`，
把确实属于建筑/军人/非劳动力却掉进兜底的取值补进关键词表，再重跑一次。

## 全部参数

| 参数 | 说明 |
|---|---|
| `--input` | CSV 文件或目录，可多个 |
| `--output-dir` | 输出目录，默认 `out` |
| `--inspect` | 只报告检测到的列和取值分布，不写文件 |
| `--encoding` | 输入编码，默认 `utf-8`（失败自动回退 `latin-1`） |
| `--circumstance-col` | 手动指定 circumstance known 列 |
| `--occupation-col` | 手动指定职业文本列 |
| `--occupation-code-col` | 手动指定职业编码列 |
| `--industry-col` / `--industry-code-col` | 手动指定行业列（可选） |
| `--include-extraction` | 把 6800–6940 采掘业算作 construction |
| `--drop-unknown-circumstance` | 剔除 circumstance 既非 Yes 也非 No 的行 |
| `--no-keep-unclassified` | 不输出 `unclassified` 的拆分文件 |
| `--write-empty` | 空子集也写文件（默认跳过） |
| `--rules` | 规则覆盖 JSON |
| `--dump-rules` | 打印默认规则 JSON 后退出 |

## 核对布尔列：`verify_circumstance.py`

分批核对原始列 `circumstance_known_c`（Yes/No）与派生列 `circumstance_known_bool`
（TRUE/FALSE）是否一致。文件是**分块（chunked）读**的，几十万行也不占内存。

```bash
# 每 1000 行一批，走完整个文件
python verify_circumstance.py --input out/labeled/18_30_labeled.csv

# 只打印有问题的批次（文件大时最实用）
python verify_circumstance.py --input out/labeled/ --mismatches-only

# 翻页看：每批 500 行，一次只看 2 批，按提示用 --start-row 继续
python verify_circumstance.py --input file.csv --batch-size 500 --max-batches 2

# 把不一致的行和取值配对表写出来
python verify_circumstance.py --input out/labeled/ --output-dir report/
```

每批输出「原始值 × 布尔值」的计数，标 `[OK]` 或 `[N MISMATCH]`；不一致的行直接列出
行号、ID、两列取值、以及应该是什么。最后给全量交叉表和准确率。有不一致时退出码为 1，
可以直接串进脚本里。

判定标准就是映射规则本身：`Yes`→`TRUE`，`No`→`FALSE`，**其他一切**（空值、`Unknown`）
→`UNDETERMINED`（即 pandas 的 `<NA>`）。注意报告里用 `UNDETERMINED` 而不是 `<NA>` 或
`NA` —— 后两者是 pandas 的默认缺失值标记，写进 CSV 再读回来会变成 NaN。

如果你拆分时用了 `--rules` 改过 Yes/No 的取值集合，核对时传同一个文件：
`--rules my_rules.json`。

| 参数 | 说明 |
|---|---|
| `--input` | CSV 文件或目录，可多个 |
| `--raw-col` / `--bool-col` | 手动指定两列（默认自动识别） |
| `--id-col` | 不一致行旁边显示的标识列（默认找 IncidentID） |
| `--batch-size` | 每批行数，默认 1000 |
| `--start-row` / `--max-batches` | 翻页 |
| `--show-rows` | 每批最多打印几行不一致，默认 10 |
| `--mismatches-only` | 只打印有问题的批次 |
| `--output-dir` | 输出 `circumstance_mismatches.csv` 和 `circumstance_value_pairs.csv` |

## 测试

用合成数据（不含任何真实 NVDRS 记录）做端到端校验：

```bash
python tests/make_sample_data.py tests/sample_data   # 可选，单独生成样例
PYTHONPATH=tests python tests/test_nvdrs_split.py
python tests/test_verify_circumstance.py
```

`test_nvdrs_split.py` 覆盖：编码路径与关键词路径的分类正确性、`Yes/No/Unknown/空`
的布尔映射、8 个拆分文件齐全、拆分对原数据无重叠无遗漏、汇总计数自洽、兜底复核文件
内容、`--inspect` / `--drop-unknown-circumstance` / `--include-extraction` 各模式可运行。

`test_verify_circumstance.py` 会**故意注入 3 处错误**（Yes 配成 FALSE、No 配成 TRUE、
Unknown 被当成 FALSE），断言脚本恰好抓到这 3 行、行号正确、分块读不丢行，以及干净文件
报 100%。

## 注意

本仓库不包含任何 NVDRS 数据。NVDRS 是限制性使用数据，请不要把数据文件提交进来
（`.gitignore` 已排除 `*.csv` 与常见数据目录）。
