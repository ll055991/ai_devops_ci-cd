# 01 — 专员模型实例与用量 callback 装配

**What to build:** 主模型与 4 专员各持独立模型实例，各绑带专员名的用量 callback；伪造一次模型调用结果，库里多一行且专员/thread/用量正确（覆盖 `build_model_for_agent` 构造参数）。

**Blocked by:** None — can start immediately.

**Status:** ready-for-agent

- [ ] 主模型与 code/build/deploy-planner/monitor 专员各有独立模型实例，不再共享
- [ ] 每个实例的用量 callback 绑定了正确的专员名
- [ ] callback 写失败只记日志不抛；无 thread 归属时跳过，不写孤儿行
- [ ] 子Agent 规格通过模型字段使用各自实例（不用回退共享实例）
