# 近期同步后的周期拟稿

`scripts/interaction_draft_cycle.py` 接在现有评论采集之后，周期检查同一收件箱，再调用已有模型拟稿。它不读取平台、不授权或发送、不安装定时任务。安装 Skill 不会启用它；模型服务和评论处理范围须沿用作者已确认的配置。

## 私有策略与输入

策略、作品索引、模型配置均放仓外私有文件，沿用 `0600` 权限。作品索引和模型配置见[模型建议](interaction-suggestions.md)，不能换成未绑定作品或凭据值。先准备策略，确认启用后再把 enabled 设为 true：

```json
{
  "schema": "interaction-draft-cycle.v1",
  "enabled": false,
  "platform": "youtube",
  "account": "creator",
  "channel_id": "ACTUAL_CHANNEL_ID",
  "max_source_age_seconds": 3600,
  "max_requests_per_cycle": 3,
  "max_requests_per_day": 12
}
```

每个物理平台频道分别记预算与暂停状态，换本地账号别名不会清零。每轮最多 1–20 次、每日最多 1–100 次模型请求，按北京时间自然日计数；这是本执行器的模型请求预算，不含其他程序、平台采集或 token 总量。请求前先记账，失败、中断也占用该次额度，不能因为没有收到结果就当作没请求过。

只有该账号来源处于 complete，最近成功时间带时区、未在未来且不超过策略时效时才拟稿。complete 仍只代表读取器声明的范围，不会扩大平台覆盖。失败、部分读取、人工导入、旧数据和缺失来源均等待新同步。每次模型请求前再检查来源和策略；作品摘要缺失则保留 needs_context，不猜内容。本人、已处理和已核验回复仍由原草稿队列排除。

## 一次周期与有限观察

```sh
python3 scripts/interaction_draft_cycle.py --state-dir /private/interaction-state --policy /private/draft-policy.json run --work-index /private/works.json --model-config /private/model.json
python3 scripts/interaction_draft_cycle.py --state-dir /private/interaction-state --policy /private/draft-policy.json watch --work-index /private/works.json --model-config /private/model.json --interval-seconds 1800 --cycles 49
python3 scripts/interaction_draft_cycle.py --state-dir /private/interaction-state --policy /private/draft-policy.json status
```

watch 必须指定有限轮数（1–10000，默认 1），间隔至少 300 秒，默认 1800 秒。它与只读采集进程分别运行，不重启原巡检；使用同一收件箱和已有缓存，无新留言或相同上下文时不请求模型。运行记录使用 `draft-<platform>` 命名空间，与平台读取的运行记录区分。49 轮 / 30 分钟只是配置，必须核对真实时间、轮次、进程和终止状态，才算持续运行验收。

每周期重新读私有策略、模型配置和作品索引；每次模型请求前再读策略。作者停用或变更策略后停止后续请求，已经在途的模型调用仍需等待结果并核对原文。账号绑定变化停止进程，不自动换号。status 只检查本机锁与记录，策略已停用时也能查询。

## 模型失败与中断

提供方错误、结果无效或模型请求后进程中断会暂停该频道的周期拟稿。原评论采集不受影响，其他频道可继续。暂停状态和每日计数保留在同一 SQLite，重启进程不会自动重试；检查请求记录、缓存、草稿及故障后，才显式恢复：

```sh
python3 scripts/interaction_draft_cycle.py --state-dir /private/interaction-state --policy /private/draft-policy.json reset --reason "已检查提供方故障及原有结果"
```

reset 不清空预算、不删除原建议、草稿或发送回执，也不授权发送。整个流程始终 `authorized=false/sent=false`；后续发送仍走独立审核与对应平台发送器。不得把拟稿轮次、缓存命中或模型分类当成公开回复成功。

已完成离线周期、缓存、预算、策略停用、失败及中断反例；实际 YouTube 收件箱的一次周期为零待处理、零模型请求。有限本机观察与真实网友内容质量、持续运行和云端接管仍须分别留证。
