# 社交平台发布包与实际执行

此模块有两种真实执行方式：当前 Agent 使用其许可的浏览器工具执行六个平台；可选宝玉 X 预填桥在账号核验后调用锁定上游脚本。两者都必须独立读回，均不能仅凭进程退出或工作单生成报告平台成功。本模块未进行真实账号上传验收；默认能力保持 disabled。

## 发布包

`python3 scripts/platform_pack.py manifest.json --target x-main --profile profile.json --output pack.json` 只做本地整理和检查，不产生外部写入。

目标可填写：

| 字段 | 约定 |
| --- | --- |
| `title` / `text` / `description` | 独立字符串，原样保留；不同文案不自动互相覆盖 |
| `tags` | 非空字符串数组；组装微博话题为 `#话题#`，其他平台为 `#话题`；页面有话题选择器时须实际选中 |
| `links` | `[{"label":"视觉看板","url":"https://..."}]`；只允许无凭证 HTTP(S) 地址。不能据此推断平台允许可点击外链 |
| `cover_asset_id` | 引用本目标 `asset_ids` 中 image |
| `subtitle_asset_ids` | 引用本目标 subtitle 素材；未填时保留本目标全部字幕，显式漏掉任何字幕会拒绝 |
| `platform_metadata` | 平台所需分区、原创/转载声明、可见性等，由生产端或作者提供，不凭空填写 |

发布包保留 `original_target` 和全部素材哈希。`composed_text` 将 text（为空才取 description）、话题与链接分段组装；独立 description 仍保留，Agent 不能只粘贴 composed_text 就忽略其他字段。长文章正文继续通过 document 交接，不改成短帖或线程。正文内插图仍按 document 与原稿核对。

规格从私有 `profile.platform_policies.<platform>` 提供，未配置不猜测平台当前上限：

```json
{
  "platform_policies": {
    "bilibili": {
      "required_fields": ["title"],
      "require_cover": true,
      "require_subtitles": false,
      "allowed_extensions": {"subtitle": [".srt"]}
    }
  }
}
```

可选 `max_lengths` 对 title/text/description/composed_text 按 Python Unicode 码点长度检查；这是本地编辑约束，不冒充平台实际字符计数。`max_assets` 按 image/video/audio/subtitle 检查。超限、缺项或错误类型交回生产端修订，绝不截断或静默跳过。格式、封面、字幕在页面上不支持时报告该限制，不能未经授权重新剪片或省略。

## Agent 执行适配器

Profile 增加公开身份与专用浏览器路径（示例值须换为实际值；不含密码）：

```json
{
  "browser_profile": "/absolute/path/dedicated-chrome-profile",
  "accounts": {"owner": {"platforms": ["x"], "restricted": false,
    "public_identities": {"x": "@actual_handle"}}},
  "adapters": {"x": {
    "enabled_capabilities": [],
    "execute": ["python3", "/absolute/skill/scripts/social_adapter.py", "execute", "--profile", "/absolute/profile.json"],
    "verify": ["python3", "/absolute/skill/scripts/social_adapter.py", "verify", "--profile", "/absolute/profile.json"],
    "timeout_seconds": 2100
  }}
}
```

沿用 `agent_adapter.py reply` 的 nonce、证据哈希和账号锁协议。后台 `run` 启动后，通过 `social_adapter.py inspect --request ...` 读取活跃接力及 `.social.json` 工作单，按当下真实页面执行。回应增加：

- 每阶段 `observed_account_identity`：实际观察到的稳定公开账号身份，必须等于配置；不能把本地 alias 当作平台身份。
- 完成读回时 `verified_pack_digest`：当前工作单 `pack_digest`。
- 完成读回时 `verified_asset_ids`：核对全部素材，包括封面和字幕，不能漏项。

以上是约束 Agent 回执的合同，不会自动证明观察真实，证据仍须来自本阶段实际页面。脚本只驱动接力与核对，不假称自带各平台上传 API。没有可恢复编辑身份的临时页面不能报告 `prefill_ready`；留在 pending 并说明只能保留当前页面/本地包。只读恢复可在账号停止写入后继续查询，但必须保留首次执行记录中的账号身份、专用 profile 与完整发布包；修改同别名指向的账号后恢复会被拒绝，不会重建为新账号。首次执行绑定缺失时不能自动认领当前账号，须先找回原执行证据。

| 平台 | 当前可执行方式 | 不应冒充的能力 |
| --- | --- | --- |
| X | Agent 浏览器；可选锁定宝玉 text/images/video 预填桥 | 上游退出 0 不是发帖成功；文章不走普通帖桥 |
| 微博 | Agent 浏览器填稿、按目标提交和独立查询 | 宝玉微博脚本仅预填，不是完整发布适配器 |
| 抖音 | Agent 创作者后台上传和查询 | SAU upload-video 会提交，没有草稿模式，不能用于 draft/prefill |
| B 站 | Agent 投稿、字幕与稿件管理查询 | SAU CLI 的 submitted 文本没有统一远端回执，不能直接映射 published |
| 腾讯视频 | Agent 腾讯视频创作平台 | SAU `tencent` 是视频号，不能替代腾讯视频 |
| 小红书 | 账号未受限时 Agent 预填/草稿，作者最后点击 | 受限账号不执行，不能自动公开发布 |

## 可选宝玉 X 预填桥

适用已安装的完整、干净上游仓，固定 commit `1567581c26ec29f4216c6e6835415bf30343b0e3`，MIT。不会自动下载或安装依赖，不执行从帖子内容获得的脚本或 shell。

给 `adapters.x` 增加：

```json
{"upstream_prefill": {
  "provider": "baoyu-x",
  "repository": "/absolute/path/baoyu-skills",
  "runtime": "/absolute/path/bun"
}}
```

只承接 endpoint=prefill 和 text/images/video。不承接独立封面、字幕、额外平台字段、文章、不同的 text/description 或独立标题。此时删掉该可选配置、改由 Agent 完整操作；不要为调用脚本删掉作者字段。

1. 检查上游 Git 根、固定 HEAD、干净状态和脚本白名单。前导 `-` 或首尾空白文案拒绝：上游把它们作为参数或 trim，可能误读 `--submit` 或改变内容。
2. 第一阶段工作单 `authorize-upstream-prefill`。Agent 先核实账号、专用 profile 和宿主允许使用该浏览器通道。回应 pending，增加 `upstream_prefill_ready=true`、`browser_mode_authorized=true`、`observed_browser_profile`；此阶段可以不提供 remote_id，不能伪造身份。既有用户授权持续有效，不重复确认；若宿主明确禁止 CDP，不用此桥。
3. 适配器继承 `SANDY_PUBLISH_LOCK_FD`，以参数数组运行 `x-browser.ts` 或 `x-video.ts`，永不传 `--submit`。执行前记 `upstream_started`；超时或非零退出先查现有编辑稿，不重复调用。上游 Chrome 可能继续处理已发出的上传，未知状态不自动重试。
4. 脚本正常退出后开启新 nonce 的 `observe-upstream-prefill` 阶段，Agent 重新观察实际稿件并登记 pending。随后发布核心开启独立 verify，完整读回后才可报告 prefill_ready。桥不会自动升级为公开发送。

直接把宝玉脚本当成完整适配器不可行：锁定 `x-browser.ts` 184–191 行和 `x-video.ts` 177–185 行只点击并输出文本，没有远端 ID 或账号绑定；`weibo-post.ts` 204–205 行明确仅预填。SAU 锁定 commit `3847fff01f5434a8a32829833163cd6723cabc59` 的 `sau_commands/douyin.py` 186–208 行选择即时/定时提交，`uploader/douyin_uploader/main.py` 1096–1098 行点击发布；`sau_commands/bilibili.py` 138 行仅输出 submitted。故未将它们的 stdout 转成成功回执。

源码核对日期：2026-10-24。锁定来源见 [sources.json](sources.json)。没有把上游实现复制进本仓；桥调用用户实际安装的上游。未完成真实账号验收的平台依然不能登记为 verified。
