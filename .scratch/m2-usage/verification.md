# M2 出口验证报告（ticket 03）

- 时间：2026-09-16
- 模型：Qwen3.6-35B-A3B
- 对应完成标准：任意 thread 明细可查 + 总量正确

## Step 1：生产链路冒烟（只读）

- 请求：`POST /api/agent/chat`（"列出所有容器"，thread `t-m2-smoke3`）
- 结果：`GET /api/agent/usage?thread_id=t-m2-smoke3` → 2 行，均为 main，
  总量 23035 = 12196 + 10839（分项求和一致）
- 结论：api 写入 → callback 落库 → 查询，全链路通

## Step 2：专员覆盖（evals deploy_happy，mock SSH）

- 运行：`pytest tests/evals -m gate -k deploy_happy`（通过，48s）
- 用量：14 行，thread 全部为 `eval-deploy_happy`（无孤儿行），
  专员覆盖 main + code-agent + build-agent（不全是 main），
  总量 164242 = 明细求和（SUMS_MATCH=True）
- 单次完整部署约 16.4 万 tokens；input 逐轮从 ~1 万涨到 ~1.5 万
  （系统提示词 + 29 工具 schema + 历史累积所致）

## 判定（ticket 03 四条）

1. 明细行数 > 0：✅（Step1 计 2 行，Step2 计 14 行）
2. agent 覆盖主链 + 子链：✅（main/code-agent/build-agent）
3. 总量 = 明细求和：✅（两步均一致）
4. 证据记入本报告：✅

## 修复记录（验证中发现并已修）

1. callback 挂模型实例但生产零行 → 根因：dispatch 只找同步名
   `on_llm_end`，`aon_llm_end` 永不被调用；改覆盖同步名的 async 版本。
2. 触发后全 0 → 根因：流式下用量在末 chunk 的消息里，
   `llm_output.token_usage` 为空；改优先读 `usage_metadata`。
3. 仍全 0 → 根因：`stream_usage` 默认 False，网关不发用量；
   `build_chat_model` 加 `stream_usage=True`。
4. 以上三处均有回归测试（dispatch 缝走 MockTransport 真实路径，
   不再直调 callback 方法）。
