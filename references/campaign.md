# 多平台批量任务

同一封存 manifest 的多个 `targets` 可以组成一次批量任务，保持各平台的账号、终点、授权和独立读回。默认串行；可选 `--max-parallel 2` 让不同平台最多两个目标并行，同平台保持串行。一个目标失败后继续其他目标，同账号未知提交仍受既有锁保护。启用前读[提效与可选命令行](efficient-dispatch.md)。

## 调用接口

Python 模块 `scripts/campaign.py` 复用 `publish_core.Store`，供 CLI 和当前 Agent 调用：

```python
prepare_campaign(manifest_path, store, target_ids=None)
authorize_campaign(campaign_id, store, statement, target_ids=None)
run_campaign(campaign_id, profile, store, target_ids=None, resume=False, max_parallel=1)
campaign_status(campaign_id, store)
```

`prepare` 返回 `campaign_id`。`target_ids=None` 表示本次批量任务内全部目标，传列表则限于该子集；空列表、重复或越界目标拒绝。授权语句必须来自实际用户，并且选择范围与其授权相符；函数只保存证据，不理解自然语言批准范围，不允许 Agent 自造授权。

批量定义保存在状态目录 `campaigns/<campaign_id>.json`，ID 绑定完整封存内容和所选目标。各平台真实状态仍以原 `publish.sqlite3` 为准，没有第二份发布账本。原 manifest 删除后，可用 `campaign_id` 查看和恢复已经开始的任务。尚未启动的目标仍需源素材存在，不能把恢复快照当成素材副本。

## 执行与恢复

- 正常运行：`prepared` 目标执行；已开始目标只查询；已经到达约定终点的目标跳过。
- `resume=True`：只读恢复，`prepared` 不执行。即使查询证明未提交并重置为 `prepared`，本轮也不会再次发送。
- 结果未知：保留原状态、账号锁和错误，禁止重复创建。公众号缺 ID 时按[恢复流程](recovery.md)绑定核实后的明确候选。
- 已有草稿待补媒体：报告 `next_action=advance`，按公众号流程完成同一稿；批量查询不代替编辑步骤。
- 小红书始终只到草稿或预填，最后点击由作者完成。受限账号仍由原适配器限制，不因批量模式放开。

报告含每个目标的真实状态、远端 ID/链接、错误、下一步动作和需要作者介入的提示，并统计状态。`all_endpoints_reached=true` 只表示所有目标达到各自约定终点；其中可以包括草稿或预填，不能称作全部公开发布。

`outcomes` 区分本轮执行、查询、已完成跳过、尚未开始和失败。失败信息写入当前批量任务的本地记录；后续成功查询会清掉旧错误，账本发生新状态后也不展示旧状态的错误。登录/验证码由平台要求时再找作者，不让作者为成功目标重复确认。
