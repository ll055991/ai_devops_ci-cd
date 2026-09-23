# 03 — M2 出口验证

**What to build:** 真实跑一次完整部署流程，验证任意 thread 的 token 明细可查且总量正确（需网关可用），证据记入报告。

**Blocked by:** 01 专员模型实例与用量 callback 装配，02 用量查询接口与请求链路.

**Status:** ready-for-agent

- [ ] 真实流程跑完后，查询接口返回该 thread 明细行数大于 0
- [ ] 明细覆盖主链与子链专员（agent 维度不全是 main）
- [ ] 总量等于明细求和
- [ ] 验证证据记入 M2 报告（通过即达 M2 完成标准）
