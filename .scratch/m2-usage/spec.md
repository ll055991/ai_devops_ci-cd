# Spec：M2 LLM 成本可观测（token 账本）

- status: ready-for-agent
- 依据：ADR-0004（只记 tokens）、ADR-0006（callback 完整形态）
- 术语：`docs/词汇表.md`（llm_usage、usage_metadata、callback 捕获、专员维度）

## Problem Statement

用户（开发者）对一次部署烧了多少 token 毫无概念：哪个专员最烧钱、换模型省多少、小模型是否够用，全靠猜。没有账本，M3 的模型路由优化无从下手。

## Solution

token 账本：每次模型调用记一行（thread × 模型 × 专员 × 用量），查询接口按 thread 给明细 + 总量。主链子链全捕获，不漏子Agent 的多轮工具循环。

## User Stories

1. 作为开发者，我想要查任意一次部署（thread）的 token 明细（轮次 × 模型 × 专员 × 用量），以便知道钱烧在哪。
2. 作为开发者，我想要看到该 thread 的总量（input/output/total），以便一句话说出单次部署成本。
3. 作为开发者，我想要按专员分摊用量（main / code-agent / build-agent / deploy-planner / monitor-agent），以便定位最烧钱的环节。
4. 作为开发者，我想要子Agent 的模型调用也被记录且归属同一 thread，以便账本没有黑户。
5. 作为开发者，我想要用量记录失败不阻断主流程，以便可观测性永远不拖累部署。
6. 作为开发者，我想要换模型后账本自动按新模型名分开记，以便对比不同模型的成本。
7. 作为值班工程师，我想要线上出问题时按 thread_id 拉出完整用量时间线，以便排查异常烧钱。
8. 作为开发者，我想要用量查询接口与审计查询接口风格一致（明细 + 总量），以便前端一次接完。
9. 作为开发者，我想要新表不污染现有库表结构，以便部署状态与审计不受影响。
10. 作为开发者，我想要无 LLM 凭证时相关测试自动跳过、有凭证也不依赖网络抖动（伪造调用结果测写入），以便 CI 稳定。
11. 作为发布负责人，我想要 M2 完成标准可验证（任意 thread 明细可查 + 总量正确），以便里程碑有明确出口。

## Implementation Decisions

- 新增用量存储模块：`llm_usage` 表 + record / list / totals 方法，单例 + lifespan 关闭（沿用审计存储的建表/单例/关闭模式）。
- Schema（决策密集，内联）：
  `llm_usage(id INTEGER PK, thread_id TEXT, agent TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER, created_at TEXT)`，按 thread_id 建索引；id 顺序即轮次。
- 新增用量 callback：构造时绑定专员名；`aon_llm_end` 从调用结果的 `token_usage` 取 prompt/completion/total 写库；thread_id 从每请求 ContextVar 读；写失败只记日志不抛。
- 模型实例策略：主模型 + 4 专员各持独立模型实例（同参数分别构造），各绑带专员名的 callback；子Agent 规格通过模型字段指定各自实例（deepagents 未指定则回退共享实例，会丢专员维度）。
- thread_id 注入：对话入口解析出 thread_id 后即写入 ContextVar（请求任务上下文内，子链可见）。
- 查询接口：`GET /api/agent/usage?thread_id=` 返回明细行 + 该 thread 总量；失败返回 500 结构化错误（沿用审计查询接口风格）。
- 只记 tokens，不换算金额；streaming 下每模型调用一行；重试重复计为已知接受项。
- 不引入外部评测/可观测框架；用量字段已探针验证网关返回完整。

## Testing Decisions

- 好测试只断言外部行为：伪造的调用结果进 callback → 库里多一行且字段正确；调查询接口 → 明细 + 总量正确；不断言 langchain 内部调用链。
- 三个 seam（全部离线，无需 LLM 凭证、无网络）：
  1. callback 缝：伪造调用结果（含用量）调 `aon_llm_end`，断言落库行（专员名/thread/用量正确）与写失败不抛。
  2. 存储缝：临时库 record / list 过滤 / totals 求和。
  3. 路由缝：沿用审计路由测试模式（应用内传输 + 临时库替换单例），断言明细与总量。
- 先验：审计存储的临时库测试模式、审计查询路由测试模式、用量 ContextVar 回退测试模式。
- 出厂装配不断言 langchain 内部；专员-模型绑定关系由 callback 构造参数覆盖。

## Out of Scope

- 金额换算与费率表（以后）
- 模型路由、小模型替换等优化动作（M3+）
- 前端用量展示页
- 实时用量推送/告警
- 重试去重（已知接受重复计）
- 生产流量回放、红队等非 M2 事项

## Further Notes

- 与 ADR-0004/0006 一致；M2 完成标准 = 任意 thread 明细可查 + 总量正确（见路线图）。
- 否决项重申：after_model 顺手记（漏子链）、config 挂 callback（穿透不进子Agent）。
