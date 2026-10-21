# 🫀 心脏电生理每日论文检索

每天自动从 PubMed / Europe PMC / Crossref / OpenAlex / medRxiv 抓取**心脏电生理（EP）**领域的最新文献，用 DeepSeek 生成中文五段式解读、并把英文摘要翻译成中文，渲染成一个可以直接打开的静态日报页面。

**零服务器**：全部跑在 GitHub Actions 上，页面托管在 GitHub Pages，数据放在独立的 `data` 分支，前端通过 `raw.githubusercontent.com` 直读 —— 不需要买服务器，也不需要域名。

---

## 目录

- [它做什么](#它做什么)
- [相关性过滤](#相关性过滤)
- [快速开始（本地运行）](#快速开始本地运行)
- [部署到 GitHub（每天自动跑）](#部署到-github每天自动跑)
- [仓库结构](#仓库结构)
- [配置说明](#配置说明)
- [常见问题](#常见问题)

---

## 它做什么

```
PubMed / Europe PMC / Crossref / OpenAlex / medRxiv
        │  ① 采集（daily_ep）
        │     ↳ 去重
        │     ↳ 非研究性文献过滤（pub_types）：勘误/撤稿通知在这里被丢弃
        │     ↳ 相关性过滤：与电生理无关的论文在这里被丢弃
        │     ↳ 排序 → max_papers 截断
        ▼
   data/YYYY-MM-DD.jsonl          原始元数据（标题、作者、期刊、摘要、DOI…）
        │  ② AI 中文解读 + 摘要翻译（ai/enhance.py，DeepSeek）
        ▼
   data/YYYY-MM-DD_AI_enhanced_Chinese.jsonl
        │  ③ 渲染（render/build_html.py）
        ▼
   index.html                     单页站点（前端按日期 fetch 数据）
```

抓取范围由 `config.yaml` 控制：**9 本 EP 核心期刊的期刊白名单 + 22 个临床主题词**，覆盖房颤与消融、室性心律失常、器械治疗、影像与标测、遗传与基础五个亚专业。

> 检索命中后还会经过两层筛选，详见[相关性过滤](#相关性过滤)：
> 1. **非研究性文献过滤** —— 日报里**不会出现勘误（Erratum）、撤稿（Retraction）通知**这类没有独立内容的条目。
> 2. **相关性过滤** —— 把「摘要里只是顺带提了一句房颤」的论文丢掉。

日报里的每篇文献会给：

- 标题、期刊、日期、作者、亚专业标签
- **AI 中文解读五段**：一句话结论（tldr）、研究动机、方法、结果、结论
- PubMed / DOI / PDF 链接（存在才显示）
- 摘要可折叠（默认收起）；**英文摘要下面是 AI 翻译的中文摘要**，同样是折叠块，
  逐句忠实翻译、保留原文分段与统计量；没有摘要的社论会显示「暂无摘要」而不是空白

---

## 相关性过滤

检索命中后要过**两道**筛选，它们在采集层依次执行：

```
检索命中 ──► ① 非研究性文献过滤（pub_types）──► ② 相关性评分 ──► 进 AI / 页面
```

### ① 非研究性文献过滤

**为什么单列一层**：相关性判断的是「这篇文献与电生理论不相关」，而这里判断的是「**这根本不是一篇有内容的论文**」。两者正交 —— 一篇勘误通知即使标题里全是 ablation/AF，也毫无阅读价值。

实测曾经漏进日报的例子：一条 `Correction: Machine learning for enhancing the ablation outcomes...`，摘要只有 `[This corrects the article DOI: 10.3389/...]`，靠标题里的 ablation 拿到 0.54 分，读者点开只有一个 DOI 指针。

两档规则：

| 类型 | 处理 |
| --- | --- |
| **勘误 / 撤稿（硬过滤）**：`Published Erratum`、`Erratum`、`Retraction of Publication`、`Retracted Publication`、`Corrected and Republished Article`、`Expression of Concern`、`Retraction` | **无条件丢弃** |
| **社论 / 评论 / 通讯（软过滤）**：`Comment`、`Editorial`、`Letter`、`News`、`Newspaper Article`、`Congress`、`Overall`、`Preprint` 等 | **有真实摘要才保留**；摘要为空、或只是 `[This corrects the article DOI...]` / `[This retracts ...]` 这类纯指针 → 丢弃 |

> ⚠️ **期刊白名单也不豁免** —— 白名单期刊的勘误照样丢弃。
>
> ⚠️ **空摘要规则只对「软类型」生效，不适用于 `Journal Article`**。普通期刊论文里有大量正常研究因 PubMed 尚未标引而暂时没有摘要，其中包含必须保留的电生理论文（例如 optogenetic、AI 心腔容积、ICD 宽 QRS 心律等），对全类型套用「空摘要即丢」会直接误杀它们。

日志形如：`过滤非研究性文献 N 篇（A 篇 → B 篇）`。

**结论：日报里不会出现勘误、更正、撤稿通知。**

### ② 相关性评分

#### 为什么需要

检索式用的是 `[tiab]`（标题 + 摘要）**无语境子串匹配**。只要摘要里出现过 "atrial fibrillation"、"defibrillator"、"ablation" 这类词，论文就会被 PubMed 命中 —— 哪怕它的主题其实是**缺血性卒中、骨科创伤、气道消融、皮瓣重建、医学教学**。

实测占比不低：**约四到五成命中最终是噪声**（保留率大致 5~6 成）。典型误入的论文：

| 误入的论文 | 为什么会被命中 |
| --- | --- |
| 缺血性卒中 ×3 | 摘要提了一句「患者合并房颤」 |
| `airway ablation in a rat model of COPD` | 气道消融，被 "ablation" 词组命中 |
| 下肢缺血坏疽 | 合并房颤的周围血管病例 |
| 头颈皮瓣重建 | 摘要列出合并症 |
| 儿科急诊模拟教学 | 靠 "defibrillator" 命中 |
| 教室拥挤与心率变异性 | 靠 "electrocardiogram" 命中 |

**只靠检索式解决不了这个问题。** 一个自然的想法是加心脏锚定 —— `(主题词) AND (heart OR cardiac ...)` —— 但实测被否掉了：只能滤掉一小部分，**最差的几条全部留下**，因为它们摘要里本来就含有 "cardiac" / "heart"。所以最终改成了加权评分。

#### 怎么工作

`daily_ep/relevance.py` 给每篇论文打一个 **0–1 的相关性分**，`score >= threshold` 才保留。评分由四类信号加权而成：

- **强信号加分**（+0.42/个，单个即可超过阈值）：房颤、室颤、室速、导管消融、ICD、标测、Brugada、起搏、CRT 等短语级模式
- **中性信号加分**（+0.08）：泛化但仍然相关的词
- **心脏语境加分**（+0.12）：出现 heart / cardiac / arrhythmia 等
- **负信号扣分**（−0.30/个，最恶劣的重罚 −0.55）—— 负信号分两类，见下表

负信号在**同一句子内**判定，避免「一句无关 + 一句相关」被叠加误伤。

#### 负信号为什么要分两类

边界是：**这个词描述的是研究对象本身，还是仅仅出现在研究对象旁边？**

| 类别 | 触发条件 | 包含 | 为什么这样分 |
| --- | --- | --- | --- |
| **始终生效** | 命中即扣分 | 卒中、下肢缺血、非心脏部位消融、机器学习消融实验、血管器械、心衰、皮瓣重建 | 这类词说明文献研究的**本身就是别的器官/系统**，即使标题带 EP 词也只是「共病视角」 |
| **条件触发** | 仅当**标题没有 EP 诊断词**时才扣分 | 高血压、教学管理（含裸词 survey）、结构性干预、HRV 仅作测量工具、营养补充剂 | 这些是「合并症 / 研究类型」类信号，词本身只是背景（如「62% 合并高血压」），**标题已声明 EP 诊断时不该扣分** |

举例：标题 "Structural heart disease **and atrial fibrillation**" 时，`structural_intervention` 不再扣分 —— 因为标题写的才是研究对象，摘要里的背景提及不该推翻它。反过来，标题没有任何 EP 诊断词时，儿科模拟教学、IBD 的 HRV 研究仍会被正常滤掉。

> 注：代码里 `hypertension_only` / `education_admin` / `structural_intervention` 由常量 `CONDITIONAL_NEGATIVE_NAMES` 统一管控；`hrv_as_metric` / `nutrition_supplement` 走同一判据（标题无 EP 诊断才生效），但由单独的函数实现。

此外还有三道**防误判机制**：位置加权、incidental 降权、标题非心脏部位规则 —— 详见下一节。

> ⚠️ **期刊白名单命中的论文直接放行**，不参与评分。来自 `config.yaml` 里那 9 本 EP 核心期刊的论文本身就是目标领域（哪怕是基础的影像/遗传子方向），不应该被评分误杀。

### 评分是怎么算的

只看「有没有关键词」是不够的 —— **关键词出现在什么位置、以什么语气出现**，决定了它是不是研究对象。下面四条一起把噪声压下去。

#### 1. 位置加权

同一个信号出现在不同位置，分量不同（**同一信号取最大权，不重复计分**）：

| 位置 | 权重 | 依据 |
| --- | --- | --- |
| 标题 | **1.0** | 标题写的就是研究对象 |
| 摘要首句 | **0.8** | 通常是研究目的 / 背景主句 |
| 其余句子 | **0.35** | 多出现在结果讨论里的附带提及 |

#### 2. 强信号饱和累加

多个强信号不是线性叠加，而是按 **1.0 → 0.6 → 0.4** 递减累加。这样「房颤 + 起搏器」两条顺手带过的词不会直接顶到满分 1.0。

#### 3. incidental（伴随提及）降权 ×0.3

句子里出现下面这类短语时，说明该疾病词只是**既往史 / 合并症 / 排除标准 / 对照对象**，不是研究对象，命中强信号时额外乘 **0.3**：

`was noted` · `were noted` · `incidental` · `history of` · `medical history` · `comorbid*` · `concomitant*` · `coexisting` · `were excluded` / `excluding` · `no history of` · `without prior` · `unlike` · `compared with patients` 等（共 12 条模式）。

#### 4. 标题非心脏部位规则

如果**标题**里出现非心脏解剖部位或术式，**且标题中没有任何心脏词** → 直接 `score = 0`。

覆盖部位包括：`iliac` / `femoropopliteal` / `carotid` / `endograft` / `stent graft` / `flap` / `head and neck` / `airway` / `bronchial` / `renal artery` / `thyroid` / `prostate` / `hepatic` 等。

**为什么这条规则最值钱 —— 一个真实例子：**

> **`Collapse and Thrombosis of an Iliac Branch Endovascular Device Following Blunt Force Trauma`**
>
> 这是一篇**髂动脉支架**的病例报告，标题里没有任何心脏词。但摘要里有：
> - "Atrial fibrillation **was noted** on admission electrocardiogram"（心律失常只是入院时的附带发现）
> - "the patient **was started on a pacemaker** for intermittent bradycardia"（起搏器是为心动过缓装的）
>
> **引入这三道机制之前：曾被判 1.00（满分）** —— 旧逻辑只看「有没有词」，`atrial fibrillation` 和 `pacemaker` 两条强信号直接顶格。
>
> **现在：0.00，被正确丢弃** —— 这条由**标题部位规则**直接短路判定（标题含 `Iliac` 且无任何心脏词），根本走不到加分环节。
> 即使去掉标题规则，另外两道机制也会拦住它：AF 只出现在摘要**其余句**中（位置权 0.35，不是标题的 1.0），最佳加权后仅 0.35，×0.42 = **0.147**，远达不到 0.35 阈值。

这个例子说明：**光数关键词会把「合并房颤的外科病例」当成电生理论文**，必须同时看位置和语境。

#### 心脏语境硬门槛（可关闭）

`relevance.require_cardiac_context`（默认 `true`）是一道**硬门槛**：非白名单论文全文若无任何心脏语境词，直接判无关（`score = 0`），reason 标注 `无心脏语境(require_cardiac_context)`。

它是**必要但不充分**条件 —— 有心脏语境不等于相关（卒中论文摘要里也有 "cardiac"），真正把关的仍是加权评分；这道门槛只负责挡住「通篇没有心脏语境却有强信号词」的漏网（例如器械/材料学论文里的 ablation）。

> 实测开关差异：一篇只讲猝死遗传检测、全文无心脏词的论文，关闭时得分 **0.68（保留）**，开启时 **0.00（丢弃）**。

#### 最后一步

所有加分扣分累加后，**分数截断到 `[0, 1]`**，再与 `threshold` 比较决定去留。

### 怎么知道它丢了什么

被丢弃的条目会打印原因，**这正是调阈值的依据**。每次采集结束都会输出：

```
相关性过滤：N 篇 → M 篇（丢弃 K 篇，其中白名单期刊直通 J 篇）
  丢弃明细（阈值未达标 K 篇）：
    ✗ [0.00] Acute ischemic stroke despite NOAC therapy in atrial fibrillation...
          原因: 强信号:af; 负信号:stroke,hypertension_only
    ✗ [0.24] Supraclavicular artery island flap for head and neck reconstruction...
          原因: 强信号:af; 负信号:flap_recon
```

日志里 `强信号:af` 说明它确实抓到了 AF 这个强信号，`负信号:stroke` 说明同时命中了卒中负信号并把分数压到阈值以下 —— 这就是「提了一句房颤的卒中论文」被正确识别的方式。注意 `←` 是 `daily_ep.relevance` 写 INFO 日志时的分隔符，命令行直接输出时显示为「原因:」。

> 想看 INFO 级别的完整日志（包括每条丢弃原因），加 `--verbose` 运行：`python -m daily_ep.collect --days 2 --verbose`。

### 怎么调

改 `config.yaml` 里的 `relevance` 段，**不用改代码**：

| 字段 | 默认 | 含义 |
| --- | --- | --- |
| `relevance.enabled` | `true` | 设为 `false` 完全关闭过滤（页面会重新出现噪声，仅调试用） |
| `relevance.threshold` | `0.35` | 保留门槛：分数 ≥ 它才留下。**调低 = 更宽，调高 = 更严** |
| `relevance.whitelist_bypass` | `true` | 白名单期刊直通；设为 `false` 则白名单期刊也要按分数过滤 |
| `relevance.require_cardiac_context` | `true` | **硬门槛**：非白名单论文必须含心脏语境词（heart / cardiac / arrhythm\* / tachycardi\* / supraventricular / conduction 等），否则直接判无关（score=0） |

**经验值**：`0.25` 宽松（宁可多留）· `0.35` 默认（实测标定）· `0.5` 严格（只留强电生理主题）。

> **觉得漏掉了你关心的论文？** 把 `threshold` 调到 `0.25` 再跑一次，对比日志里那篇论文的分数和丢弃原因 —— 它的分数就在 `✗ [0.xx]` 里，一眼能看出离阈值差多少。

### 某天篇数特别少？

可能是当天 PubMed 在该领域确实没有多少新文献（周末、节假日尤其明显），也可能阈值偏严。**先看采集日志**：

1. `源 pubmed: N 篇` —— 是原始命中数，少的话说明检索式本身那天就没什么东西
2. `相关性过滤：A 篇 → B 篇` —— A 到 B 的落差大，说明是过滤在起作用
3. 逐条看 `丢弃明细` 的分数和原因 —— 如果被丢的都是真电生理论文，那就是阈值偏严，调到 `0.25`

想完全关掉过滤做对照，把 `relevance.enabled` 设为 `false` 跑一次即可。

---

## 快速开始（本地运行）

### 1. 环境要求

- Python **3.9+**
- 网络可访问 `eutils.ncbi.nlm.nih.gov`、`www.ebi.ac.uk`（以及 `api.deepseek.com`，如果用 AI）

### 2. 安装依赖

```bash
git clone <你的仓库地址>
cd 心脏电生理论文每日检索

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -e ".[ai]"             # 带 AI；只要元数据可用 pip install -e .
```

> 只抓元数据、不做 AI 解读的话，`--no-ai` 模式**不需要** `openai` 包，`pip install -e .` 即可。

### 3. 跑起来

```bash
# 抓最近 2 天 + AI 中文解读 + 渲染 index.html
export OPENAI_API_KEY=sk-你的key
./run.sh

# 不想要 AI 解读（免费、快、不需要 Key）
./run.sh --no-ai

# 抓最近 7 天
./run.sh --days 7

# 以某个历史日期为基准采集（补那天的数据用）
./run.sh --date 2026-10-19

# 查看全部参数
./run.sh --help
```

> **`--date` 不只是重命名输出文件**，它把整个采集窗口的**基准日**定为该日期：
> `--date 2026-10-18 --days 2` 抓的是 **10-17 ~ 10-18** 两天的文献，输出到
> `data/2026-10-18.jsonl`。不传则用 UTC 今天。补历史某天的日报时用它。

跑完打开根目录的 `index.html` 即可：

```bash
open index.html        # macOS
# start index.html     # Windows
# xdg-open index.html  # Linux
```

### 4. 手动分步运行（调试用）

`run.sh` 只是把下面三步串起来，出问题时可以逐步排查：

```bash
# ① 采集
python -m daily_ep.collect --days 2 --out data/2026-10-19.jsonl

# ② AI 增强（中文五段式解读 + 英文摘要的中文翻译）
python ai/enhance.py --data data/2026-10-19.jsonl

# 只补中文摘要，不重新生成五段式解读（回填历史数据用；默认就地更新）
python ai/enhance.py --data data/2026-10-19_AI_enhanced_Chinese.jsonl --translate-only

# ③ 渲染
python -m render.build_html \
  --data-dir data \
  --out index.html
```

### 5. 本地运行的环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `OPENAI_API_KEY` | 无 | **AI 解读必填**；不填会自动降级为 `--no-ai`，不会报错 |
| `OPENAI_BASE_URL` | `https://api.deepseek.com` | ⚠️ **不带 `/v1` 后缀**，DeepSeek 官方文档就是这么规定的 |
| `MODEL_NAME` | `deepseek-flash` | ⚠️ 不要用 `deepseek-chat`，那已不是现行模型名 |
| `LANGUAGE` | `Chinese` | 决定 AI 产物文件名后缀 |
| `NCBI_API_KEY` | 无 | 可选。填了 PubMed 速率从 3 次/秒 提到 10 次/秒 |
| `NCBI_EMAIL` | 无 | 可选。NCBI 要求带上联系方式，建议填；同时用作 Crossref/OpenAlex 的 polite pool 标识 |
| `OPENALEX_API_KEY` | 无 | OpenAlex 数据源**强烈建议**。无 key 时与整个 IP 共享 \$0.10/天匿名额度，耗尽后整天 429；免费 key 有独立 \$1/天额度，在 https://openalex.org/settings/api 申请 |

---

## 部署到 GitHub（每天自动跑）

> ⚠️ **GitHub Pages 免费额度只对 public 仓库开放。** 想让 Pages 免费托管，仓库**必须是 public**。如果仓库设为 private，Pages 需要 GitHub Pro / Team / Enterprise 才能用；这种情况下你仍然可以每天自动生成数据，只是得自己在本地 `git pull` 后打开页面。

### 步骤 1：把代码推到自己的仓库

```bash
git init
git add .
git commit -m "feat: 心脏电生理每日论文检索"
git branch -M main
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git push -u origin main
```

### 步骤 2：配置 Secrets（敏感信息，加密存储）

打开仓库 → **Settings → Secrets and variables → Actions → Secrets → New repository secret**，逐个添加：

| Secret 名称 | 是否必需 | 值 |
| --- | --- | --- |
| `OPENAI_API_KEY` | **必需**（否则自动降级为无 AI） | 你的 DeepSeek API Key，形如 `sk-xxxxxxxx` |
| `OPENAI_BASE_URL` | 可选 | 留空即可，默认就是 `https://api.deepseek.com`（**不要**加 `/v1`） |
| `NCBI_API_KEY` | 可选 | [NCBI 申请](https://www.ncbi.nlm.nih.gov/account/)的 API Key，可把速率提到 10 次/秒 |

> 注意：Secrets 是**加密**的，名字区分大小写，填完不可再查看原值（只能覆盖）。
> `OPENAI_BASE_URL` 通常**不用建**，除非你要换成别的兼容 OpenAI 协议的服务。

### 步骤 3：配置 Variables（非敏感信息，明文可见）

同一页面切到 **Variables → New repository variable**：

| Variable 名称 | 是否必需 | 建议值 |
| --- | --- | --- |
| `MODEL_NAME` | 建议设置 | `deepseek-flash`（⚠️ 不用 `deepseek-chat`） |
| `LANGUAGE` | 建议设置 | `Chinese` |
| `EMAIL` | 建议设置 | 你的邮箱，用于 commit 署名 |
| `NAME` | 建议设置 | 你的名字，用于 commit 署名 |

> 这些变量**都有默认值**，一个都不填也能跑（默认 `deepseek-flash` / `Chinese` / `github-actions[bot]`）。
> 但 commit 署名建议填成你自己的，方便在提交历史里认出来。

### 步骤 4：允许 Actions 写仓库

仓库 → **Settings → Actions → General → Workflow permissions**，选择：

- ✅ **Read and write permissions**

workflow 里的 `permissions: contents: write` 已经声明了写权限，但这个仓库级开关如果是只读，推送 `data` 分支仍会被拒绝。

### 步骤 5：触发一次，确认能跑通

仓库 → **Actions** → 左侧选「**心脏电生理每日论文**」→ 右侧 **Run workflow**：

| 输入 | 说明 |
| --- | --- |
| `days` | 抓最近 N 天，默认 `2` |
| `no_ai` | 选 `true` 只抓元数据不调 AI（省 token，先验证链路用） |

建议**第一次先选 `no_ai = true`**，几十秒就能验证采集 → 渲染 → 推送整条链路，确认无误后再跑带 AI 的。

首次运行会自动创建 `data` 孤儿分支（只有 `data/*.jsonl` 和 `assets/file-list.txt`，**没有代码**）。

之后工作日每天 **UTC 22:00（北京次日 06:00）** 自动运行。选这个时间是刻意的：PubMed 的 `[edat]`（入库日）集中在 UTC 凌晨更新，此时抓当天最全。

### 步骤 6：开启 GitHub Pages

仓库 → **Settings → Pages**：

- **Source**：`Deploy from a branch`
- **Branch**：**`data`**，目录 `/ (root)`
- **Save**

> ⚠️ 为什么是 `data` 分支而不是 `main`：`index.html` 只是一个**壳**（约 60KB），
> 里面的论文数据由前端按日期 `fetch("data/<日期>_AI_enhanced_Chinese.jsonl")` 加载。
> 因此页面和数据必须**同源**——都发布在 `data` 分支，相对路径 fetch 才能成功
> （也就不用配 CORS，不需要写死仓库名）。`main` 分支只放代码。

### 单页架构说明

站点只有**一个** `index.html`，没有「每天一个归档页」：

- 日期列表在构建时扫描 `data/` 目录得到，直接写进页面
- 默认显示最新一天；选其它日期时**原地切换**，不跳转、不刷新
- 当前日期反映在 URL 的 `#2026-10-01` 上，可直接分享或收藏
- 每天的数据按需加载（单日 8–86 篇，几十到几百 KB），首屏始终很快

日期选择器同时支持**日期区间**：

- 面板里切到「区间」后，先点起始日、再点结束日即可；也可用「近 7 天 / 近 14 天」快捷
- 单日模式下按住 `Shift` 点击某天，可从当前日直接扩选到该日
- 区间内**所有论文聚合展示**，按亚专业分组；同一篇论文出现在多天时按 PMID → DOI →
  标题+期刊去重，只保留最新收录日，并在卡片上标注「MM-DD 收录」
- 顶栏显示区间文案与覆盖天数，统计区有「覆盖 N 天」chip；主题/期刊/关键词筛选都作用在去重后的区间结果上
- 区间也写进 URL：`#2026-10-01~2026-10-05`，可直接分享或刷新恢复

> 早期版本给每天生成独立的 `archive/<日期>.html`，需要两套模板，结果历史页和首页
> 界面不一致。改成单页后只剩一套渲染逻辑，从根本上消除了这个不一致。

等 1~2 分钟，访问：

```
https://<你的用户名>.github.io/<仓库名>/
```

页面数据来自 `data` 分支：

```
https://raw.githubusercontent.com/<你的用户名>/<仓库名>/data/assets/file-list.txt
```

### 步骤 7（可选）：补跑历史日期的 AI 解读

如果 `data` 分支里有历史日期的原始 `.jsonl` 但还没 AI 解读，用「**补跑历史 AI 解读**」workflow：

**Actions → 补跑历史 AI 解读 → Run workflow**，参数：

| 输入 | 说明 |
| --- | --- |
| `days` | 本次处理几个待补日期，默认 `1`；填 `0` 处理全部 |
| `only_date` | 只处理某一天，如 `2026-10-19`（设置后忽略 `days`） |
| `max_workers` | 并发数，默认 `4` |

默认一次只补一天，避免一次失败丢掉一整批工作。

> 只想给**已有的** AI 产物补中文摘要（不重新生成五段式解读）时，不用走 workflow，
> 本地跑 `python ai/enhance.py --data data/<日期>_AI_enhanced_Chinese.jsonl --translate-only`
> 即可：只补缺失的 `summary_zh` 字段，已有译文默认跳过（`--force` 可强制重译）。

---

## 仓库结构

```
.
├── .github/workflows/
│   ├── daily.yml              # 每天定时：采集 → AI → 渲染 → 推送 data 分支
│   └── backfill.yml           # 手动补跑历史日期的 AI 解读
├── daily_ep/                  # 采集层
│   ├── schema.py              #   Paper 记录定义（采集/AI/渲染的唯一契约）
│   ├── config.py              #   读 config.yaml 的检索段
│   ├── relevance.py           #   相关性评分：强信号/负信号加权，滤掉无关论文
│   ├── relevance_config.py    #   读 config.yaml 的 relevance 段
│   ├── pubtypes.py            #   非研究性文献过滤：勘误/撤稿/空摘要社论
│   ├── http.py                #   带重试退避的 HTTP 客户端
│   ├── dedup.py               #   DOI → PMID → 标题指纹 跨源去重
│   ├── collect.py             #   CLI 入口
│   └── sources/               #   pubmed.py / europepmc.py / crossref.py / openalex.py / medrxiv.py
├── ai/
│   ├── enhance.py             # AI 中文解读 + 摘要翻译 CLI（DeepSeek）
│   ├── structure.py           #   输出结构校验
│   ├── json_repair.py         #   LLM 返回 JSON 的容错修复
│   ├── runtime.py             #   DeepSeek 客户端构造与重试
│   └── prompts/               #   system/user.txt（解读）+ translate_system/user.txt（翻译）
├── render/
│   ├── build_html.py          # 渲染 CLI
│   └── templates/             # Jinja2 模板（自包含样式，无 CDN）
├── tests/                     # pytest 单测（不打网络）
├── config.yaml                # ⭐ 检索范围：期刊白名单 / 主题词 / 亚专业规则
├── run.sh                     # 本地一键运行
├── pyproject.toml             # 依赖声明
├── data/                      # 本地运行产物（git 忽略；云端在 data 分支）
├── assets/file-list.txt       # 数据清单（由 run.sh / workflow 重建）
└── index.html                 # 运行后生成
```

### 双分支设计

| 分支 | 内容 | 说明 |
| --- | --- | --- |
| `main` | 代码 | 你平时改的就是这个 |
| `data` | **只有** `data/*.jsonl` + `assets/file-list.txt` | 孤儿分支，由 workflow 自动创建和推送 |

为什么不放在同一个分支？因为数据每天新增、累积很快，混在 `main` 里会让代码仓库迅速膨胀，clone 一次要等很久。分开之后代码仓库始终轻量，而前端通过 `raw.githubusercontent.com` 按需读取数据。

---

## 配置说明

**改检索范围只需要动 `config.yaml`，不用碰代码。**

| 字段 | 作用 |
| --- | --- |
| `journals` | 期刊白名单（`[jour]` 检索），共 9 本 |
| `topics` | 主题词，在标题/摘要里匹配（`[tiab]`），共 22 个 |
| `topic_rules` | 亚专业标签映射，**按声明顺序匹配，先命中者优先**（具体的写在宽泛的前面，如 `pulsed field ablation` 要放在 `ablation` 之前） |
| `relevance` | 相关性过滤开关与阈值，见[相关性过滤](#相关性过滤) |
| `date_field` | 日期字段，默认 `edat` |
| `lookback_days` | 除今天外再回溯几天 |
| `max_papers` | 单日上限，防止极端日期生成过大页面 |
| `sources` | 数据源开关与优先级 |
| `journal_issn` | 期刊白名单 → ISSN 映射（Crossref/OpenAlex 按 ISSN 精确查期刊） |

五个数据源的分工（失败互相隔离，去重按优先级保留元数据更全的一条）：

| 源 | 日期维度 | 定位 |
|---|---|---|
| PubMed | 入库日 `edat` | 主源：标引全、有出版类型 |
| Europe PMC | 首次出版日 | 补 PubMed 入库延迟 + OA PDF |
| Crossref | 出版日 | 抢出版社上线即注册的 online-first，按 ISSN 查白名单期刊 |
| OpenAlex | 出版日 | 免费全覆盖库，主题词搜 title+abstract，兜住非 PubMed 收录期刊 |
| medRxiv | 投稿日 | EP 预印本（Cardiovascular Medicine 分区） |

> ⚠️ **两个不要改回去的参数（都有实测依据）**：
> 1. `date_field: edat` —— 实测 EP 期刊近 2 天 `[dp]` 命中 **0** 篇、`[edat]` 命中 **16** 篇。`[dp]` 是印刷日期，在版文章常被标成未来日期，用它做日报会漏掉当天全部内容。
> 2. 检索走 `[jour]` + `[tiab]`，**不要用 `[MeSH]`** —— 实测 `"cardiac electrophysiology"[MeSH]` 近 30 天 0 篇、两年仅 46 篇，因为新文章在 PubMed 里常处于未标引（Publisher）状态。

> ⚠️ **不要把裸词 `ablation` / `pacing` 加回 `topics`**（实测：`ablation` 命中 36 篇，绝大多数是肿瘤消融、Barrett 食管消融，甚至机器学习论文的「消融实验」；`pacing` 还会命中 lupus 的质性研究，因为英文 pacing 也指「踱步」）。要心脏场景请用 `catheter ablation` / `cardiac pacing` / `leadless pacing` 这类组合词。

环境变量 `JOURNALS` / `TOPICS` / `LOOKBACK_DAYS` / `MAX_PAPERS` 可以覆盖配置，云端不改文件就能调。

---

## 常见问题

<details>
<summary><b>1. 忘记配 OPENAI_API_KEY 会怎样？</b></summary>

不会失败。workflow 和 `run.sh` 都会**自动降级为 `--no-ai`**：照常采集元数据、照常渲染页面，只是没有中文解读，页面上显示英文摘要。

</details>

<details>
<summary><b>2. AI 步骤中途挂了，这一天的数据会丢吗？</b></summary>

不会。workflow 里 AI 失败会自动重试一次 `--no-ai` 降级路径，元数据照常发布。要补回中文解读，事后用「补跑历史 AI 解读」workflow 处理那一天即可。

</details>

<details>
<summary><b>3. 为什么 base_url 是 <code>https://api.deepseek.com</code> 而不是 <code>.../v1</code>？</b></summary>

DeepSeek 官方文档规定的就是不带 `/v1` 后缀（SDK 会自动补 `/chat/completions`）。写成 `https://api.deepseek.com/v1` 在部分 SDK 版本下会拼出 `/v1/v1/chat/completions` 导致 404。

</details>

<details>
<summary><b>4. 为什么模型是 <code>deepseek-flash</code> 而不是 <code>deepseek-chat</code>？</b></summary>

`deepseek-chat` 已不是现行模型名，继续使用会报模型不存在。本项目固定用 `deepseek-flash`。

</details>

<details>
<summary><b>5. 采集到 0 篇是出错了吗？</b></summary>

不一定是。周末、节假日、PubMed 入库延迟都可能导致 0 篇，这种情况是合法的，workflow 会打警告但不会失败。

但**如果连续几个工作日都是 0 篇**，就要检查：`config.yaml` 的期刊/主题词是否被改坏，或者 PubMed 的检索语法是否变更。

</details>

<details>
<summary><b>6. 为什么 PubMed 命中很多，最后页面只显示一部分？</b></summary>

因为有**相关性过滤**。PubMed 的 `[tiab]` 是无语境子串匹配，只要摘要里出现 "atrial fibrillation"、"defibrillator" 就会命中，所以会混进卒中、骨科、气道消融、医学教学这类论文（实测约占四到五成）。这些论文在采集阶段就被 `daily_ep/relevance.py` 按相关性分数丢弃了，不会进入 AI 和页面。

想看它具体丢了哪些、为什么丢，采集日志里有逐条明细：

```
相关性过滤：N 篇 → M 篇（丢弃 K 篇，其中白名单期刊直通 J 篇）
    ✗ [0.00] Acute ischemic stroke despite NOAC therapy in atrial fibrillation...
          原因: 强信号:af; 负信号:stroke,hypertension_only
```

说明：这篇确实含 AF 强信号，但同时命中 `stroke` 负信号，被正确判定为噪声。

**觉得漏了你关心的论文**，就把 `config.yaml` 的 `relevance.threshold` 从默认 `0.35` **调低到 `0.25`**（调低 = 更宽）。想彻底关掉，设 `relevance.enabled: false`。详见[相关性过滤](#相关性过滤)。

</details>

<details>
<summary><b>7. 为什么日报里没有勘误 / 更正类文章？</b></summary>

因为采集层有一层**非研究性文献过滤**（`daily_ep/pubtypes.py`），按 PubMed 的 `pub_types` 字段判定，在相关性评分之前执行：

- **勘误 / 撤稿类一律丢弃**：`Published Erratum`、`Erratum`、`Retraction of Publication`、`Retracted Publication`、`Corrected and Republished Article`、`Expression of Concern`、`Retraction`。即使发表在核心期刊上也照样丢 —— **白名单不豁免**，因为这类条目本身没有独立内容。
- **社论 / 评论 / Letter / News / Congress / Preprint 等**：**有真实摘要才保留**。部分社论有独立观点，值得读；但摘要为空、或只是 `[This corrects the article DOI: ...]` / `[This retracts ...]` 这种纯指针的，等于没有内容，丢弃。

这样处理的必要性：一条 `Correction: Machine learning for enhancing the ablation outcomes...` 的摘要只有一句 `[This corrects the article DOI: 10.3389/...]`，但它标题里全是 ablation/AF，纯按相关性打分能拿到 0.54 分直接过关 —— 读者点开却只有一个 DOI 指针。

日志形如：`过滤非研究性文献 N 篇（A 篇 → B 篇）`。

> 注意：**「摘要为空即丢」只对社论/评论这类软类型生效**，不适用于普通 `Journal Article` —— 后者有大量论文因 PubMed 尚未标引而暂时没有摘要，其中包含必须保留的电生理论文，一刀切会误杀。

</details>

<details>
<summary><b>8. 推送 data 分支失败 / 报权限错误</b></summary>

依次检查：

1. 仓库 **Settings → Actions → General → Workflow permissions** 是否选了 **Read and write permissions**
2. workflow 文件里是否有 `permissions: contents: write`
3. 如果是**受保护分支规则**限制了 `data` 分支，需要放宽

workflow 本身已经做了 3 次 `pull --rebase` 重试兜底，偶发的远端领先会自动恢复。

</details>

<details>
<summary><b>9. 两个 workflow 同时跑会冲突吗？</b></summary>

不会。`daily.yml` 和 `backfill.yml` **共用同一个 concurrency group（`daily-ep-data`）**，且 `cancel-in-progress: false` —— 后触发的会**排队等待**而不是取消前一个，保证每天的内容都不丢。

</details>

<details>
<summary><b>10. GitHub Pages 免费吗？</b></summary>

**public 仓库免费**。private 仓库使用 Pages 需要 GitHub Pro / Team / Enterprise 订阅。所以想白嫖 Pages，仓库必须是 public（也意味着你的 Key 配置走 Secrets，不会泄露，但**页面内容是公开的**）。

</details>

<details>
<summary><b>11. 页面打开是空的 / 一直加载</b></summary>

多半是 `data` 分支还没有数据，或文件名对不上。检查：

```bash
# 确认 data 分支存在且有数据
git ls-remote --heads origin data
curl -sI https://raw.githubusercontent.com/<用户名>/<仓库名>/data/assets/file-list.txt
```

first run 或 `days` 太小时，当天可能确实没有新文献。

</details>

<details>
<summary><b>12. 每天大概花多少钱？</b></summary>

取决于当天文献量（通常每天十几到几十篇）和 DeepSeek 的现行定价。

每篇论文会调用两次模型：一次生成五段式解读，一次翻译英文摘要。不想多花摘要翻译的 token，可以加 `--no-translate`；想完全免费就把 `no_ai` 设为 `true`，或用 `./run.sh --no-ai`，只抓元数据不调 API。

</details>

<details>
<summary><b>13. 能改成别的领域（比如冠心病、心衰）吗？</b></summary>

可以，只改 `config.yaml`：把 `journals` 换成目标领域期刊，`topics` 换成对应临床术语，`topic_rules` 换成想要的亚专业分组。代码一行都不用动。

</details>

---

## 免责声明

本项目仅做文献元数据的自动聚合与 AI 辅助摘要，**不提供医疗建议**。AI 生成的中文解读可能存在偏差或错误，**临床决策请务必以原文和指南为准**。所有文献版权归原作者与出版方所有。
