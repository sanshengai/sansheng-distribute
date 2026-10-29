# YouTube 评论丢失身份后的只读查询

只有原计划存在一次发送尝试、回执仍为 `unknown` 且缺少完整远端身份时使用。已有评论 ID 与评论串 ID 时继续原 `reconcile`；未发送、已核验或计划不符均在解析凭据前拒绝。不要为试验此功能修改成功回执或重复发送真实评论。

```sh
python3 scripts/youtube_comment_recovery.py --profile /path/to/profile.json --account owner --state-dir /path/to/private-state --key ORIGINAL_PLAN_KEY --max-pages 3
```

沿用原评论状态目录与频道授权；历史账号仅别名变化时可加 `--source-account ORIGINAL_ALIAS`，不能跨频道。该命令只对平台做 GET，不调用模型、不授权、不发送，不改变原回执；查询证据写为新的私有 `comment-recovery-*.json`。

回复只查原父评论下的子回复；顶层评论只查原视频。默认最多 3 页，可选 1–5 页，每页 100 项。匹配本人频道、完整原文、父级、已知的部分身份及原尝试前 60 秒至后 10 分钟的发表时间；未来时间不计入。最多独立核验 5 个候选，逐一另查原评论和评论串，核对实际作品、父级、作者与正文。列表分页契约来自 [comments.list](https://developers.google.com/youtube/v3/docs/comments/list) 与 [commentThreads.list](https://developers.google.com/youtube/v3/docs/commentThreads/list)。

返回 `pages_complete` 仅说明本次有限列表是否读完；页数或核验预算耗尽、重复游标与读取失败须按对应原因保留。窗口外、已删除、审核中或未进入所读页面的内容可能查不到，零条不证明没发送。单个候选也可能来自作者手动发送，多条更不能推断。所有结果保持 `state=unknown/needs_review=true`，不得自动绑定为本次成功或据此解除重发冻结。

当前完成离线实际路径与反例验收；真实无 ID 故障尚未出现，未以已有成功内容冒充真实恢复验收。
