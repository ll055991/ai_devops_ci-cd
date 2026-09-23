# Evals 基线报告（M1）

- 时间：2026-09-16T17:50:27
- 模型：Qwen3.6-35B-A3B
- 场景：1，断言：6，失败：0

| 场景 | 指标 | 通过 | 说明 |
|---|---|---|---|
| deploy_happy | 完整性 | ✅ | 必调工具全部执行: ['git_pull_code', 'build_docker_image', 'start_container', 'check_service_health'] |
| deploy_happy | 会话归属 | ✅ | 11 行审计全部归属 eval-deploy_happy |
| deploy_happy | 审批召回 | ✅ | 审批名单调用全部触发中断 |
| deploy_happy | SOP顺序 | ✅ | 顺序约束全部满足 |
| deploy_happy | 参数合法 | ✅ | 无校验失败调用，workspace/container_name 全部合法 |
| deploy_happy | 最终状态 | ✅ | 最终状态 healthy |
