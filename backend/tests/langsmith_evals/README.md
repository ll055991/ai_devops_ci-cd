# LangSmith 数据集与自动化评估（教学指引）

本目录把项目原有的「M1 本地 pytest 门禁」搬到 LangSmith 云端，形成一套
**云端数据集 + 可复现实验 + 多指标评估器**的评估体系。

这份文档面向第一次接触 LangSmith 的同学，从「为什么做」讲到「每一步怎么敲命令」，
再到「跑完后在 LangSmith 界面上会看到什么」。全程配合本目录 6 个 Python 文件的真实实现。

---

## 一、目的：这个东西到底是干嘛的

### 1.1 背景：原本的评估长什么样

项目里已经有一套本地评估：`backend/tests/evals/`，用 pytest 跑 10 条 golden 场景，
真调小模型、mock 掉 SSH 和审批，最后用确定性断言打分，产出基线报告
（`.scratch/m1-evals/baseline.md`）。

它的问题是**只有本机可见、只有一份文本报告**：

- 想看「这 10 个场景最近几次跑分别怎么样」，只能翻历史 baseline 文件；
- 想做 A/B（比如换模型、改提示词前后对比），diff 文本很不直观；
- 想让团队其他人看到每条场景的输入、轨迹、每个指标得分，没地方看。

### 1.2 目标：搬到 LangSmith

本目录做的事就是：**把这 10 条场景变成 LangSmith 上的 Dataset（数据集），
把本地断言变成 LangSmith 的 Evaluator（评估器），每次跑实验就把「模型真实行为」
和「参考答案」逐条比对，得分与轨迹全部落到 LangSmith 云端。**

一句话概括：

> 本地 pytest 门禁 = 命令行里的一次性体检；
> LangSmith 实验 = 可持续追踪、可对比、可回溯的体检档案。

### 1.3 和 `tests/evals/` 的关系：**自包含复刻，不是复用**

按需求，本目录**不 import `tests/evals/` 下任何文件**，代码是独立复刻的一份。
两者概念对应关系如下（便于你对照阅读）：

| 本目录文件 | 对应的 tests/evals 里的角色 | 作用 |
| --- | --- | --- |
| `scenarios.py` | 场景定义 | 10 条 golden 场景 + dataset example 构造 |
| `harness.py` | 隔离与执行壳 | FakeSSH、临时库、target function |
| `assertions.py` | 断言库 | 确定性打分的 11 个断言 |
| `evaluators.py` | （原无） | 把断言包装成 LangSmith evaluator |
| `dataset.py` | （原无） | 建数据集、跑云端实验 |
| `langsmith_evals.py` | 原单文件入口 | CLI 命令行入口 |

**代价要提前知道**：改动业务逻辑（工具名、审批名单、SOP 顺序）时，两套评估的
场景/断言需要**同步维护**。这是「不复用」换取「评估链路与单测脚手架解耦」的权衡。

---

## 二、先补课：LangSmith 的几个核心概念

理解下面 6 个词，后面所有内容就通了。

| 概念 | 英文 | 通俗解释 | 在本项目里对应什么 |
| --- | --- | --- | --- |
| 数据集 | Dataset | 一堆测试题目的集合 | `deploy-agent-gate` |
| 样例 | Example | 一道题目，含「输入」和「参考答案」 | 一条 golden 场景 |
| 实验 | Experiment | 拿数据集跑一遍的结果集 | `m1-gate-xxxxxx` |
| 运行 | Run | 实验里「某一道题」的一次执行记录 | 某场景跑出来的轨迹 |
| 评估器 | Evaluator | 给一次运行打分的规则 | `gate_assertions` / `gate_pass` |
| 追踪 | Tracing | 记录 Agent 每一步的调用链 | Agent 运行时的 trace |

它们的关系：

```
Dataset（数据集）
  └── Example ×10（10 道题）
        ├── inputs   ← 交给被测程序的「题面」
        └── outputs  ← 阅卷用的「参考答案」（不会传给被测程序）

Experiment（跑一遍数据集）
  └── Run ×10（每题一条运行记录）
        └── Evaluator 拿 Run 的产出 与 Example.outputs 比对 → 打分
```

两个关键区别，务必记牢：

1. **inputs 会传给被测程序，outputs 不会。** 这是 LangSmith 的硬性保证，
   所以「参考答案」放 outputs 是安全的，不算泄题。
2. **评估器有两种**：
   - Code Evaluator：你写 Python 函数，纯代码算分（本项目用的就是这种，确定性、省钱）；
   - LLM-as-Judge：让大模型当裁判打分（主观题适用，本项目暂未使用）。

   在线评估器签名是 `perform_eval(run)`，离线是 `perform_eval(run, example)`——
   本项目的 evaluator 是**离线**形式（两个参数），只在跑实验时使用。

---

## 三、生成物：文件结构与职责

依赖方向是**单向无环**的，读代码时按这个顺序理解即可：

```
scenarios.py   ← 场景与 example（最底层，无内部依赖）
    ↓
harness.py     ← 隔离替身 + target（依赖 scenarios）
    ↓
assertions.py  ← 断言库（依赖 harness 的 Trajectory）
    ↓
evaluators.py  ← LangSmith evaluator 封装（依赖 assertions + harness）
    ↓
dataset.py     ← 客户端 / 建数据集 / 跑实验（依赖 evaluators + harness + scenarios）
    ↓
langsmith_evals.py ← CLI 入口（只做参数分发）
```

各文件一句话说明：

- **`scenarios.py`**：定义 `Scenario` 数据类与 `GATE_SCENARIOS`（10 条）；
  `scenario_to_example()` 把场景转成 dataset example（inputs + outputs）。
- **`harness.py`**：`FakeSSH`（按命令关键字返回假结果）、`eval_settings()`（隔离配置）、
  `patched_eval_env()`（进程内替换模块属性）、`run_scenario_standalone()`（跑一条场景）、
  `target()`（LangSmith 的被测入口）。
- **`assertions.py`**：11 个 `assert_*` 函数 + `evaluate()`，是打分的核心规则。
- **`evaluators.py`**：`gate_assertions`（一次给多个指标）与 `gate_pass`（总分 0/1）。
- **`dataset.py`**：`get_client()`、`create_test_dataset()`、`run_evaluation()`。
- **`langsmith_evals.py`**：CLI 入口，解析 `--local / --upload / --evaluate` 等参数。

---

## 四、完整流程：从零到 LangSmith 实验（Step by Step）

### 前置条件

1. 后端依赖已安装：在 `backend/` 下执行过 `uv sync`。
2. 有内网 LLM 的 `OPENAI_API_KEY`（target 会真调小模型）。
3. 有 LangSmith 的 `LANGSMITH_API_KEY`（只有 `--upload` / `--evaluate` 需要；
   `--local` 不需要联网）。
4. **所有命令都在 `backend/` 目录下执行**（仓库根目录没有 `package.json`，
   也不是 Python 包根）。
5. **中文路径下不要用 `uv run`**，用 `python -m` 方式（见文末排查）。

### Step 0：让环境变量在「当前 shell」里可见

`.env` 是给 pydantic-settings（`Settings` 对象）读的，**不会自动写进 shell 环境**。
而 `dataset.get_client()` 读的是 `os.getenv("LANGSMITH_API_KEY")`，
所以跑云端命令前，必须在当前 PowerShell 会话里显式设置：

```powershell
# 当前会话临时生效（推荐，不污染系统环境）
$env:LANGSMITH_API_KEY = "lsv2_pt_xxxxxxxx"
$env:OPENAI_API_KEY   = "你的内网模型 key"

# 可选：指定实验归属项目（默认落到 default 项目）
$env:LANGSMITH_PROJECT = "deploy-agent"

# 可选：非美区/私有化部署时才需要
# $env:LANGSMITH_ENDPOINT = "https://your-endpoint"
```

> 想逐条核对配置是否正确，先跑 Step 1 的 `--local`：它不读 LangSmith 凭证，
> 能直接验证「模型能调通、断言能跑」。

### Step 1：离线自检 `--local`（不联网、不上传）

先确认 target 与 evaluator 两个新组件对接无误，再谈上云。

```powershell
.venv\Scripts\python -m tests.langsmith_evals.langsmith_evals --local
```

它做的事（见 `langsmith_evals.py` 的 `run_local_check`）：

1. 遍历 10 条场景，调 `target()` 真跑一遍；
2. 用 `SimpleNamespace` 伪造出 LangSmith 的 run/example 形态；
3. 调 `gate_assertions` 打分并打印。

输出形如：

```
离线自检完成：场景=10 | 断言=48 | 失败=0
deploy_happy | 完整性 | OK | 必调工具全部执行: [...]
deploy_happy | 会话归属 | OK | 12 行审计全部归属 eval-deploy_happy
...
```

只要「失败=0」，说明链路可用，可以上云。

### Step 2：上传数据集 `--upload`（首次执行）

把 10 条场景灌进 LangSmith，变成 `deploy-agent-gate` 数据集。

```powershell
.venv\Scripts\python -m tests.langsmith_evals.langsmith_evals --upload
```

幂等策略：**数据集已存在则跳过**，不会重复灌。
如果要改场景后重建（先删旧再建）：

```powershell
.venv\Scripts\python -m tests.langsmith_evals.langsmith_evals --upload --recreate
```

也可以换个数据集名，做多套并存：

```powershell
.venv\Scripts\python -m tests.langsmith_evals.langsmith_evals --upload --dataset m1-gate-exp
```

> 场景是唯一事实源（`GATE_SCENARIOS`），`TEST_CASES` 只是它的镜像。
> **改了场景必须 `--upload --recreate`**，否则云端还是旧题目。

### Step 3：跑云端实验 `--evaluate`

```powershell
# 全量（10 条，真调模型，耗时较长）
.venv\Scripts\python -m tests.langsmith_evals.langsmith_evals --evaluate

# 冒烟：只跑前 2 条，避免首跑就把慢场景全跑一遍
.venv\Scripts\python -m tests.langsmith_evals.langsmith_evals --evaluate --limit 2

# 只本地跑、不上传结果（调试 target 时用）
.venv\Scripts\python -m tests.langsmith_evals.langsmith_evals --evaluate --no-upload-results
```

结束时日志会打印实验 URL：

```
实验完成：m1-gate | url=https://smith.langchain.com/o/.../projects/.../r/xxxx
```

### Step 4：命令速查表

| 命令 | 联网 | 作用 |
| --- | --- | --- |
| `--local`（或**不带任何参数**） | 否 | 离线自检，验证链路 |
| `--upload` | 是 | 上传 golden 场景为数据集（存在即跳过） |
| `--upload --recreate` | 是 | 删旧数据集后重建（改场景后必须） |
| `--evaluate` | 是 | 跑全量实验并上传结果 |
| `--evaluate --limit N` | 是 | 只跑前 N 条（冒烟） |
| `--evaluate --no-upload-results` | 是 | 实验只在本地跑，不上传 |
| `--dataset NAME` | 是 | 指定数据集名（默认 `deploy-agent-gate`） |

`backend/` 目录下，命令前缀统一是：

```powershell
.venv\Scripts\python -m tests.langsmith_evals.langsmith_evals <参数>
```

---

## 五、跑完后，在 LangSmith 上会看到什么

登录 LangSmith 后，主要看两个页面。

### 5.1 Datasets & Experiments 页面：数据集

- 会看到名为 **`deploy-agent-gate`** 的数据集，共 **10 条** example。
- 点开任意一条，能看到：

  **inputs（题面，会传给 target）**：

  ```json
  {
    "scenario_id": "deploy_happy",
    "message": "请部署 develop 分支，仓库 http://gitlab.example.com/demo/app.git，workspace 用 /data/deploy/workspace",
    "follow_ups": ["继续，完成后续部署步骤", "请给出最终部署结论"],
    "required_tools": ["git_pull_code", "build_docker_image", "start_container", "check_service_health"],
    "decisions": [{"type": "approve"}],
    "ssh_routes": [],
    "ssh_raise": [],
    "seed": null
  }
  ```

  **outputs（参考答案，阅卷用，绝不传给 target）**：

  ```json
  {
    "required_tools": ["git_pull_code", "build_docker_image", "start_container", "check_service_health"],
    "expect": {"approval_recall": true, "order": true, "args": true, "final_status": "healthy"}
  }
  ```

  > 说明：`required_tools` **同时出现在 inputs 和 outputs**，但不是泄题。
  > inputs 侧那份是 target 的**控制信号**——用来决定「继续追问下一轮」还是「提前结束」，
  > 模型上下文里只看得到 `message`（见 `harness.run_scenario_standalone` 的循环），
  > 模型看不到这个字段。这样云端 target 的轮次行为才和本地 pytest 一致。

### 5.2 Experiments 页面：实验

- 每次 `--evaluate` 会生成一个实验，名字形如 **`m1-gate-abc123`**（前缀 + 随机后缀）。
- 实验表格是「行 = 每条场景，列 = 每个评估指标」，一目了然。
- **指标列**（对本项目而言）：

  | 指标列 | 含义 |
  | --- | --- |
  | `完整性` | 必调工具是否全部实际执行 |
  | `会话归属` | 审计记录是否都归到当前 thread_id（无孤儿） |
  | `审批召回` | 审批名单内的调用是否都触发过中断 |
  | `SOP顺序` | 工具调用顺序是否符合硬约束 |
  | `参数合法` | 有无 validation_error、参数是否命中白名单 |
  | `最终状态` | 部署记录状态是否符合预期 |
  | `未调用` | 指定工具确实没被调用 |
  | `无审批` | 全程无审批中断 |
  | `无落库` | 非跟踪链路不产生部署记录 |
  | `审计{tool_name}` | 存在指定「工具 + 状态」的审计行 |
  | `SSH未执行` | 指定 SSH 命令确实没执行（风控短路证据） |
  | `gate_pass` | 该场景全部断言通过 = 1，否则 0（对比看这一列即可） |

  打分规则见 `evaluators.py`：每个指标 `score` 取 0/1，`comment` 里带人类可读的原因
  （例如「未跑完：缺少 ['start_container']」）。

### 5.3 单条 Run 详情

点开表格里任意一行，能看到：

- **Outputs**：`{"scenario_id", "final_text", "trajectory"}`。
  其中 `trajectory` 是完整轨迹——`calls`（原子工具调用序列）、
  `approvals`（审批中断记录）、`audit_rows`（审计行）、`deployment`（部署状态）、
  `ssh_calls`（实际执行的 SSH 命令）、`final_text`（模型最后一轮回复）。
- **Feedback / Scores**：每个评估器给的分与评语。
- 若 Agent 运行时开了 tracing，还能顺着 `thread_id`（`eval-<scenario_id>`）找到调用链。

> 注意：本目录的 `eval_settings()` 构造的 `Settings` 未开启 `langsmith_tracing`，
> 所以 **target 内部的 Agent 运行默认不会额外上报 trace**。实验记录本身由 SDK 写入，
> 不受影响。这是有意为之：避免评估运行污染生产的 trace 项目。

---

## 六、关键设计说明：为什么这么做

理解这几条，能帮你在改动时不踩坑。

1. **实验必须串行（`max_concurrency=1`）**
   target 的隔离靠「模块级全局属性替换」（`patched_eval_env`），并发会让场景 A 的
   FakeSSH / 审批 mock / 临时库被场景 B 覆盖，产生串台假结果。所以 `dataset.py`
   里写死 `max_concurrency=1`，**不要为了快而调大**。

2. **隔离用「手动替换 + finally 还原」，而不是 pytest 的 monkeypatch**
   因为 LangSmith 的 `aevaluate` 里 target 与 evaluator **同进程执行**，
   monkeypatch 的本质也只是「改模块属性」。换成上下文管理器后，无需 pytest
   夹具也能复现隔离。共替换 7 处：4 个 store 单例 + 2 个 SSH 入口 + 1 个审批中断。

3. **有 7 处替换，少一处都会读生产库**
   工具（`tools_module`）与中间件（`middleware_module`）各自 import 了
   `get_deployment_store`，只 patch 一处会导致 `rollback` 等工具读到生产库。
   改动时若新增 import 入口，记得同步补 patch。

4. **LLM 不可用时**：`run_scenario_standalone` 直接抛
   `RuntimeError("评估需要 OPENAI_API_KEY（内网端点）")`，让该例**如实记为失败**，
   而不是像 pytest 那样 skip。云端实验里失败就是失败，不静默。

5. **自包含的取舍**：不与 `tests/evals` 共享代码，好处是评估链路独立演进，
   代价是场景/断言双份维护（见 1.3）。

---

## 七、常见问题排查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `RuntimeError: 评估需要 OPENAI_API_KEY` | 当前 shell 没有内网模型 key | 按 Step 0 设置 `$env:OPENAI_API_KEY` |
| 上传/实验时报鉴权失败或走默认项目 | 只配了 `.env`，shell 里没有 `LANGSMITH_API_KEY` | `$env:LANGSMITH_API_KEY = "..."` |
| 日志显示「数据集已存在，跳过上传」 | 幂等保护 | 改过场景请加 `--recreate` |
| `ModuleNotFoundError: No module named 'deploy_agent'` | venv 的 `deploy_agent.pth` 路径映射失效 | 在 `backend/` 下重新 `uv sync` |
| 中文路径下 `uv run` 报错 | uv trampoline 无法解析脚本路径 | 用 `.venv\Scripts\python -m ...` |
| 结果看起来「串台」 | 有人调大了并发 | 恢复 `max_concurrency=1` |

---

## 八、一页速查

```powershell
# 0) 进入 backend 并准备环境变量
cd backend
$env:LANGSMITH_API_KEY = "lsv2_pt_xxxxxxxx"
$env:OPENAI_API_KEY   = "你的内网模型 key"

# 1) 离线自检（不联网）
.venv\Scripts\python -m tests.langsmith_evals.langsmith_evals --local

# 2) 首次上传数据集
.venv\Scripts\python -m tests.langsmith_evals.langsmith_evals --upload

# 3) 改了场景后重建数据集
.venv\Scripts\python -m tests.langsmith_evals.langsmith_evals --upload --recreate

# 4) 冒烟（前 2 条）
.venv\Scripts\python -m tests.langsmith_evals.langsmith_evals --evaluate --limit 2

# 5) 全量实验
.venv\Scripts\python -m tests.langsmith_evals.langsmith_evals --evaluate
```

跑完 Step 5，去 LangSmith 的 **Datasets & Experiments** 页面，
打开数据集 `deploy-agent-gate`，就能看到最新实验每一条场景的得分、评语与完整轨迹。