# 多平台批量任务

同一封存 manifest 的多个 `targets` 可以组成一次批量任务，保持各平台的账号、终点、授权和独立读回。默认串行；可选 `--max-parallel 2` 让不同平台最多两个目标并行，同平台保持串行。一个目标失败后继续其他目标，同账号未知提交仍受既有锁保护。启用前读[提效与可选命令行](efficient-dispatch.md)。

## 调用接口

批量发布前先一次收齐本地阻塞，避免某个平台上传后才发现另一个平台的配置或素材有问题：

```sh
python3 scripts/publish.py --profile /private/profile.json batch-preflight /private/publish-manifest.json
```

`--target` 可重复指定子集。报告逐个列出 `local_ready` 和错误，包含就绪/阻塞数量；有阻塞时退出码为2，仍保留全部所选目标的报告。原稿摘要等共享输入不合法时整体拒绝。该命令复用实际单目标预检，不建账本或任务、不授权、不连接浏览器或平台；本地通过仍不证明登录、权限或发送可用。修复后继续原任务，不新建空账本。已有未知或待审核任务应走恢复，不能用预检通过替代原任务查询。

Python 模块 `scripts/campaign.py` 复用 `publish_core.Store`，供 CLI 和当前 Agent 调用：

```python
prepare_campaign(manifest_path, store, target_ids=None)
preflight_campaign(manifest_path, profile, target_ids=None)
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

X、微博视频已完成同一真实批次的预填与只读续跑。首次因竖屏视频误配横屏封面，微博被拦住而X独立完成；核对原编辑器后微博保留为待处理。修正封面形成新封存批次，仍使用同一账本：X按原身份只读重验，仅新版微博目标执行，随后两端恢复均无媒体上传。该证据覆盖平台文案差异、封面与声明、逐目标结果、双工作线程和共享浏览器写入串行；不覆盖合集、活动、公开发送或上传同时传输。后者分别以对应平台证据为准。

`outcomes` 区分本轮执行、查询、已完成跳过、尚未开始和失败。失败信息写入当前批量任务的本地记录；后续成功查询会清掉旧错误，账本发生新状态后也不展示旧状态的错误。登录/验证码由平台要求时再找作者，不让作者为成功目标重复确认。
