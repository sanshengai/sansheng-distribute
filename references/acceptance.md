# 验收与跨 Agent 使用

## 分层验收

1. **发现**：各 Agent 新进程能列出同一 Skill，软链接解析到同一本体。旧会话可能缓存旧版本，磁盘入口存在不是实际加载证据。
2. **执行**：各 Agent 读取本体，实际通过自身命令工具运行 `publish.py --help` 和无网络的本地检查；记录工具调用、退出码与输出。模型仅口头复述命令不算通过。
3. **合同**：正反例覆盖文件版本、媒体位置、授权目标、幂等、锁、超时恢复、回执绑定、Write 交接与回导。关键闸门应通过去掉校验后的变异实验，证明反例会失败。
4. **平台**：逐平台、账号、内容类型、终点做真实草稿/预填和独立读回。公众号另验媒体对应、保存重开、播放、手机宽度布局、正式 Write 回导。公开发布与群发单独验收，草稿成功不自动放行。

Claude Code、Codex、Antigravity、Grok Build 共用中性 manifest、Python CLI 与账本；只要宿主有合规本机进程工具即可调用核心。浏览器工具名称和权限依宿主而异，见 [浏览器接力](browser-execution.md)。CLI 可执行不能证明该宿主已能上传、插媒体和读取平台结果。

核心支持 macOS/Linux Python 3.10+；`fcntl` 与文件描述符继承要求 POSIX。Windows 暂未实现兼容层。公众号还依赖可用的 Write `wechat_api.py`、已绑定账号的私有凭证及实际网络权限；需要编辑器时另需专用 Chrome 与宿主浏览器能力。

## 首次能力试验

示例配置默认 `enabled_capabilities: []`。已有明确草稿/预填试验授权时，在该次私有 profile 增加精确范围，不改全局能力开关：

```json
{
  "acceptance_runs": [{
    "manifest_digest": "check 返回的实际封存指纹",
    "target_id": "清单里的目标 ID",
    "platform": "wechat",
    "account": "明确账号别名",
    "endpoint": "draft"
  }]
}
```

全部字段必须匹配当前清单，且终点只能 draft/prefill；publish/broadcast 不能用 `acceptance_runs` 开放，受限账号也不能绕过。仍需 `authorize` 记录本次实际已有授权，不能把示例文字当批准。私有试验配置不提交 Git；验收完成后移除该项，保留账本与证据。

先 `preflight`，再 `prepare / authorize / run`。`run` 内部也执行同样预检。只读恢复不要求重新开放写入能力，原稿被清理也不阻止读回。平台通过后，仅按已验收的账号范围与终点启用能力；不能因一个账号草稿成功便启用所有账号发表。

## 首次公开投稿试验

已有用户明确授权时，可在私有 profile 使用独立 `first_publication_runs`。每项必须且只能包含：`manifest_digest`、`target_id`、`platform`、`account`、`content_type`、`endpoint`、`expires_at`、`acknowledge_publication`。前六项精确绑定本次封存清单，endpoint 为 `publish`；expires_at 为含时区的 ISO 时间，剩余有效期不得超过24小时；acknowledge_publication 必须是布尔 true。

仅支持 X、微博、抖音、B站、腾讯视频的首次公开投稿，不开放公众号群发或小红书最终点击，不支持视频号冒用腾讯视频能力。此配置仍需真实 `authorize`、账号限制检查、执行锁与独立读回；不能作为授权来源，也不自动升级其他账号或后续内容的发布能力。试验完成后移除该项，保留账本和实际远端身份；审核中保持 pending。

## 重复运行本地验证

从 Skill 根执行：

```sh
python3 -m unittest discover -s tests -q
python3 scripts/publish.py --help
```

测试夹具放扫描树之外的临时目录，自动清理；不得在 Skill 内留下包含假 SKILL.md 的测试目录。平台证据保留请求范围、远端身份、实际读回及未验证边界，不记录凭证。
