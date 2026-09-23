# 02 — 用量查询接口与请求链路

**What to build:** 按 thread 查用量明细 + 总量；对话入口解析出 thread_id 后即写入每请求通道；进程退出时关闭用量库连接。

**Blocked by:** None — can start immediately.

**Status:** ready-for-agent

- [ ] `GET /api/agent/usage?thread_id=` 返回明细行 + 该 thread 总量（input/output/total/calls）
- [ ] 查询失败返回 500 结构化错误（沿用审计查询接口风格）
- [ ] 每请求 thread_id 写入后，主链子链的调用都能归属（无孤儿行）
- [ ] lifespan 退出时关闭用量库单例连接
