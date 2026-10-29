# 模型回复建议

`scripts/interaction_suggest.py` 把已入收件箱的评论及绑定作品摘要交给显式配置的模型，结果写回原有草稿队列。它不会授权、发送或启用自动回复。模型只接收有预算的 JSON，没有浏览器、文件、网络工具或平台凭据。

## 启用与数据

默认未启用。可以继续由当前 Agent 阅读[回复上下文](interaction-context.md)后拟稿，不配置模型服务也不影响发布、同步或已有草稿。需要脚本持续拟稿时，先确认所选模型服务、实际模型、费用及评论内容的处理范围；不要将无关私人信息和凭据放进作品摘要。

私有配置文件需 `0600`、当前用户持有且不能是末级符号链接。仅引用已有环境变量，不在配置里填写密钥：

```json
{
  "enabled": false,
  "base_url_env": "REPLY_MODEL_BASE_URL",
  "api_key_env": "REPLY_MODEL_API_KEY",
  "model": "YOUR_CONFIRMED_MODEL",
  "max_tokens": 2000,
  "timeout_seconds": 60,
  "disable_thinking": false
}
```

确认启用后把 `enabled` 设为 `true`。Base URL 必须是所选服务的 HTTPS Chat Completions 基址；脚本追加 `/chat/completions`，不跟随重定向、不自动切换供应商、不重试。使用供应商要求的真实模型和入口，不把某家测试通过外推到全部兼容服务。可选 `disable_thinking=true` 仅用于确实支持 `thinking.type=disabled` 的服务；未确认时保留 false。预算允许 2000–8000，超时允许 10–180 秒。空输出、截断、工具调用或无效 JSON 都拒绝入草稿。

## 准备与查看

```sh
python3 scripts/interaction_suggest.py --state-dir /path/to/private-state prepare --platform youtube --account owner --channel ACTUAL_CHANNEL_ID --comment ACTUAL_COMMENT_ID --content-file /path/to/work.private.json --model-config /path/to/model.private.json
python3 scripts/interaction_suggest.py --state-dir /path/to/private-state show --key SUGGESTION_KEY --include-text
```

作品文件格式沿用回复上下文；原评必须有稳定身份并仍为待处理的他人评论。上下文最多 12000 JSON 字符，不静默截断必要原文；摘要过长时先处理交接。`show` 不请求模型或平台，默认不输出回复正文及原因，加 `--include-text` 才在本机查看。

## 整理一批新留言

同账号可交接私有作品摘要索引，字段为 `{"schema":"interaction-work-index.v1","works":[作品摘要对象]}`。每个作品使用前述完整格式，不能混入其他平台、账号或频道，也不能重复作品身份。随后运行：

```sh
python3 scripts/interaction_suggest.py --state-dir /path/to/private-state batch --platform youtube --account owner --channel ACTUAL_CHANNEL_ID --work-index /path/to/works.private.json --model-config /path/to/model.private.json --limit 5
```

`--limit` 约束本轮实际模型请求，范围 1–20，默认 5；已有建议和作者草稿不占预算，因而不会始终挡住后面的新留言。缺作品摘要的留言返回 `needs_context`，不猜内容、不调用模型；本人、已处理或已有核验回复回执的评论不入选。一次最多检查本地最早 1000 条待办，超量时 `inbox_scan_truncated=true`，不能据此宣称已处理全部积压。请求失败后停止本轮模型调用，保留已完成项；重跑复用同一结果。该命令不进行平台同步、不自动排期、不授权或发送，需与只读巡检分别启用。

统一互动看板按账号概括拟回复、转人工与建议跳过，详情折叠。默认不含模型正文和原因，`--include-text` 才输出私有详情。看板中的历史建议不保证作品摘要或对话仍未变化，发送前仍需复核。

模型只返回 `action/category/text/reason`。`reply` 保存原有本地草稿；`review` 保存待判断建议，`skip` 保存跳过建议，两者都不会把原评论标记已处理。投诉、隐私、交易和版权类别强制转为 review；模型分类仍可能错误，不能据其分类自行取得发送资格。所有结果均保持 `needs_review=true/authorized=false/sent=false`，后续有限自动模式须另外实现和验收。

周期检查新留言并拟稿时，按需读[周期拟稿](interaction-draft-cycle.md)：它增加近期成功同步校验、每轮与每日预算、持久暂停和有限观察；不把拟稿接成自动发送。

## 复用与恢复

相同原评、作品、必要对话、覆盖警告及模型配置复用已有建议，不重复调用模型。空轮询只改变最近同步时间，不应使草稿缓存失效。作品或对话内容变化时不复用旧建议；已有作者草稿不覆盖，返回 existing_draft 供核对。每个账号的拟稿持有同机独占锁，不阻塞其他账号的正常同步。

模型请求期间原评论变化、已处理或已有核验回复时拒绝入草稿；最后保存草稿再次核对原评论摘要。建议先保存再交接草稿，进程在二者之间退出后可从同一建议恢复，不重复调用模型。请求失败不保存有效建议，下一次显式重试可能再次消耗模型额度，应先查看错误和现有记录。

建议保存在同一私有 SQLite 的 `interaction_suggestions`，记录上下文身份、模型、输出、用量和时间。建议与草稿不是平台回执；发送继续沿用已验收的[发送队列](interaction-youtube-queue.md)，重新读取平台原评论和已有回复。私有配置、数据库和原始模型验收资料不进公开仓。

已完成离线程序验收，以及一个已配置模型服务的三个合成样本真实调用：作品知识问题生成草稿、索密指令跳过、版权投诉转人工；均未向社交平台写入。真实网友回复质量、批量接入、持续拟稿和其他模型服务仍须分别验收，不因模块存在宣称自动互动完成。
