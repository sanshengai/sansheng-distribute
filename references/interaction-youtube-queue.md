# YouTube 草稿与受控发送

仅用于有稳定平台 ID、已进入统一收件箱的网友评论。复用 [YouTube 评论候选](youtube-comments.md) 的计划、授权和回执，未新建独立发送账本。此接力经过离线接口替身验证；真实网友队列发送、持续运行和云端接管仍待验收。本人测试继续使用原评论候选，不伪装成网友草稿。

## 入口

先用 [互动流程](interaction.md) 的 `interaction_drafts.py prepare` 拟定回复。该步骤不联网、不授权。以下所有命令必须使用同一私有 profile、账号和状态目录；`DRAFT_KEY` 是草稿 key，不是原评论候选的计划 key。

```sh
python3 scripts/interaction_youtube_queue.py --profile /path/to/profile.json --account owner --state-dir /path/to/private-state prepare --key DRAFT_KEY
python3 scripts/interaction_youtube_queue.py --profile /path/to/profile.json --account owner --state-dir /path/to/private-state authorize --key DRAFT_KEY --user-statement-file /path/to/approval.txt
python3 scripts/interaction_youtube_queue.py --profile /path/to/profile.json --account owner --state-dir /path/to/private-state send --key DRAFT_KEY
python3 scripts/interaction_youtube_queue.py --profile /path/to/profile.json --account owner --state-dir /path/to/private-state reconcile --key DRAFT_KEY
python3 scripts/interaction_youtube_queue.py --profile /path/to/profile.json --account owner --state-dir /path/to/private-state status --key DRAFT_KEY
```

`prepare`、`authorize` 和 `status` 为本地操作；`send` 向平台公开回复；`reconcile` 仅请求平台查询。授权文件为权限 `0600` 的私有文件，记录实际用户决定；填入一句文字不构成授权。已有会话授权覆盖该账号、作品、目标及回复时沿用，不重复询问；没有授权不能自行生成批准记录。

## 发送检查

- 草稿、账号、作品、原评论、评论串和文案摘要必须匹配。原文或归属变化时停止。
- 平台重查原评论作者、正文和父级，并检查子回复。顶层评论已有本人回复时停止；针对网友后续子回复，可接受更早的本人回复，但其后已有本人回复时停止。
- 子回复每页最多 100 条，最多检查 10 页；未查完就停止，不能假定没找到等于没有。子回复时间或身份不足也停止。
- YouTube 的回复仍挂在顶层评论下。回复子评论时，队列保存被回应的子评论身份，收件箱只标该条已回复，不误标整个评论串。
- 同一状态目录、同一频道的评论发送串行执行。锁不能协调不同主机或不同状态目录，也不保证人工操作与 API 检查之间没有竞态。

这些检查用于拒绝错误目标及重复尝试，不是平台事务或防封保证。模型拟稿、定时调度、跨平台发送和无人值守授权不由此脚本自动开启。

## 未知结果与看板

发送前写入尝试记录；取得平台评论 ID 后立即保存，再独立读回。重跑 `send` 复用原尝试，不再次 POST。收件箱写回失败只影响本地展示，不能为修复看板另发一条。

有平台 ID、首次读回失败时，`reconcile` 可沿原计划恢复为 `verified`。没有平台 ID 时返回 `missing_remote_identity` 并保持冻结，尚不自动匹配相似文案或恢复重发。出现发送尝试后，禁止撤销草稿或重新授权覆盖状态；丢失草稿投影后改文案也不能绕过既有尝试。

`reconciliation=verified` 且 `currently_verified=true` 才表示本次平台查询匹配。`read_failed` 的当前可见性未知；`not_verified` 表示本次未核对上。历史 `state=verified` 会保留，不等于回复仍然公开。`status` 不联网，仅查看原回执。

统一 HTML 看板将已绑定的 YouTube 回复列入发送队列，区分待授权、待发送、结果不明和历史核验；未绑定的其他草稿仍列为本地草稿。看板是快照，不是实时控制台。
