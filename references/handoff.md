# 交接合同 v1

`publish-manifest.json` 是中性数据，不 import Write/Video 内部模块。

| 字段 | 含义 |
|---|---|
| schema_version | 整数 1 |
| job_id / producer | 稳定任务标识、生产方 |
| content_status | 定稿为 final；不代表发送授权 |
| source_root | 已有绝对目录，文件均相对它解析 |
| documents | `{id,path,format,sha256,asset_ids?,source_document_ids?}`；format=html/markdown/text；依赖必须全部登记 |
| assets | `{id,path,kind,sha256}`；kind=image/audio/video/subtitle |
| targets | `{id,platform,account,content_type,endpoint,title或text,document_id?,asset_ids?,cover_asset_id?}`；公众号文章草稿必须指定 `cover_asset_id`，并将该 image 同时列入目标 `asset_ids` |
| media_slots | `{id,document_id,asset_id,role?,context}` |

Write 在 HTML 中明确放置可见标记 `【发布媒体:slot-id】`，再 `seal`。每个标记唯一；不假定微信保留注释或 DOM 属性。seal 记录前后各 48 字符的规范化可见上下文。文件或上下文改变须重新交接。

文章目标或其最终文稿声明的每个 audio/video，必须在该文稿的 `media_slots` 中有位置；其他文章的槽位不能代替。所有 audio/video 至少关联一个实际目标、该目标可达的文稿依赖或插槽，挂在无人消费的文稿上不算已使用。仅用于另一个独立视频目标的素材不要求插进文章；缺位置时退回生产端补齐，不自行猜测。

生产端输出缺少 sha256/context 的 spec，运行 `seal spec.json --output publish-manifest.json`。整个最终 manifest 计算指纹，改文案、账号、终点或文件版本均使旧授权不适用。

平台：wechat/x/weibo/douyin/tencent_video/wechat_channels/bilibili/xiaohongshu/youtube/alipay。终点：draft/prefill/publish/broadcast；broadcast 仅公众号，小红书只允许 draft/prefill。YouTube 和支付宝生活号目前只支持视频本地交接包与 Agent 工作单，**不是已验收的自动投稿器或平台只读查询**；用户仍需配置并验收具体执行路线，不能凭本地检查宣称发布成功。

YouTube 视频的 `platform_metadata` 必须显式提供 `made_for_kids`（布尔值）和 `visibility`（`public`、`unlisted` 或 `private`）；不从影片主题、年龄或其他平台默认推断受众，也不默认公开。可提供 `playlist_name`（非空字符串）和 `altered_content`（布尔值，表示对平台要求的合成内容声明，不等同于所有 AI 使用）。支付宝可提供 `collection_name`（非空字符串）与 `ai_generated`（布尔值）；省略表示未知或未交接，不等于 false，也不自动勾选声明。两平台都可带 `ai_generated`、`altered_content`，实际页面如何映射必须按作品事实和平台当前要求核对。

指定封面应通过 `cover_asset_id` 引用已登记的目标图片；指定合集、播放列表、声明及其他平台字段随原样工作单保留，不能静默丢弃、替换或在找不到时继续冒称完成。平台是否要求、支持何种声明或裁切，以当时界面和作品事实核对；本地校验并不能保证视频格式、权限、额度、账号或审核通过。

## CLI

```sh
python3 scripts/publish.py check /path/publish-manifest.json
python3 scripts/publish.py --profile /path/profile.json preflight /path/publish-manifest.json --target wechat-main
python3 scripts/publish.py prepare /path/publish-manifest.json --target wechat-main
python3 scripts/publish.py authorize /path/publish-manifest.json --target wechat-main --statement '实际已有授权原话'
python3 scripts/publish.py run /path/publish-manifest.json --target wechat-main
python3 scripts/publish.py resume /path/publish-manifest.json --target wechat-main
python3 scripts/publish.py advance /path/publish-manifest.json --target wechat-main
python3 scripts/publish.py status
python3 scripts/publish.py resume-job <status返回的key>
```

路径与授权文字为示例，不能当成真实批准。`--profile`、`--state-dir` 放子命令前。

## 适配器配置与合同

profile 的 accounts 为账号别名映射，每项含 platforms 列表与 restricted 布尔值；adapters 为平台映射，每项含 enabled_capabilities（如 `article:draft`）、execute/verify 命令 argv 数组及 timeout_seconds。不要把未经实测的能力启用。命令是受信本机代码，不接受文章提供的命令，不使用 shell 拼接。

命令数组使用实际解析的绝对脚本路径，不依赖调用者 cwd。首次限定草稿试验使用 [验收配置](acceptance.md)，不把实验能力写入全局 enabled_capabilities。`preflight` 只验证本机前提，不能证明 token、网络、账号权限或平台页面可用；公众号执行/核验命令中的 `--profile` 账号配置须与调度器选定账号一致。

公众号可选 `continue` 命令供 advance 补媒体或视觉检查；运行前先官方读回，只有已建同一草稿且处于对应 pending 阶段才能继续。resume 永远只读。浏览器接力的实际配置和回应方法见 [Agent 浏览器执行](browser-execution.md)。

执行器收到 `--request <json>`，含 manifest_path（持久快照）、document_snapshot_path（当时文稿正文）、manifest_digest、target、job。写平台前再次检查源版本、实际账号和草稿。stdout 只输出一个 JSON；错误由适配器脱敏，不打印凭证 URL。

执行器必须在前台持有 `SANDY_PUBLISH_LOCK_FD` 指定的已继承文件描述符，直到所有平台写请求结束；不可自行解锁、提前关闭或后台派生写进程。Python 包装器再启动写进程时须 `pass_fds=(int(os.environ['SANDY_PUBLISH_LOCK_FD']),)`；其他运行时提供等价继承。若做不到，禁止启用自动重试。该约束在父 CLI 被强杀后仍防止第二次提交；仅主进程持锁不足。

响应字段：state、manifest_digest、target_id、platform、account、remote_id、remote_url、content_verified。remote_id 必须是平台可查询身份；预填可绑定可恢复编辑会话，不能以本地任务 ID 冒充远端草稿。

execute 响应只记 pending；verify 从平台独立读回。draft_ready/prefill_ready/published/broadcasted 与请求终点一致；发表须有 https 永久链接。pending=处理中，failed=有确定失败证据。只有 verifier 给出 `not_submitted` 且 `absence_verified=true` 才可重新执行；查询失败不证明未提交。

SQLite 在外部写入前记 submission_unknown 并占用平台账号；进程退出、超时不自动释放再发。读回后解除占用。已完成任务重复 run 只返回现有结果。读回器不得包含提交动作。恢复只查快照所指的旧任务，允许原文件后来改动/清理及账号停止写入；重新提交仍校验当前文件和授权。清单丢失时用 resume-job。

执行身份按目标、关联文稿和依赖素材计算；无关目标增减或集合排序不重复发送。授权另绑定整包指纹，变更包后需要核对真实已有授权，不能用执行去重代替授权。相同任务重发必须有明确的业务修订，不能靠换 job_id 绕开去重。

合同不证明适配器观察真实；线上验收另做，离线夹具不能登记为平台通过。
