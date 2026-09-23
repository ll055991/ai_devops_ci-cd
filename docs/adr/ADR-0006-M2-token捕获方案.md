# ADR-0006：M2 token 捕获方案（callback 完整形态）

- 日期：2026-09-16
- 状态：已接受（修订 ADR-0004 的捕获点）
- 会话：grill-with-docs（M2 规划）

## 背景

ADR-0004 定"在 `DeployApprovalMiddleware.after_model` 顺手记"。细化时发现
该点只覆盖主链：子Agent 是独立嵌套图（自有中间件栈），而 code/build/monitor
三专员的多轮工具循环才是 token 大头，漏掉则账本是糊涂账。

## 决策

1. **callback 挂模型实例，不挂 config**：deepagents 调子Agent 是裸
   `subagent.ainvoke(state)`，父级 config 的 callbacks 穿透不进去。
   主模型 + 4 专员各持独立模型实例，各绑带专员名的 usage callback。
2. **thread_id 经 ContextVar**：模型实例跨会话共享，callback 无法从调用
   参数区分归属；api.py 每请求写入，callback 读取（沿用子Agent thread
   穿透的同一模式）。
3. **`aon_llm_end` 读 `llm_output.token_usage` 写库**，写失败不阻断主流程。
   用量字段已探针验证：网关返回完整 `usage_metadata`（input/output/total）。
4. **表 `llm_usage`**：id, thread_id, agent（main/四专员）, model,
   input_tokens, output_tokens, total_tokens, created_at（id 顺序即轮次）。
5. **查询**：`GET /api/agent/usage?thread_id=` 返回明细行 + 该 thread 总量。
6. **只记 tokens，不换算钱**（沿用 ADR-0004）。

## 后果

- 5 个模型实例（连接池复制，量级可接受）。
- streaming 下每模型调用一行；重试会重复计（M2 接受，备注已知）。
- M2 完成标准：任意 thread 的 token 明细可查 + 总量正确。
- 否决项：after_model 顺手记（漏子链）、config 挂 callback（穿透不进去）。
