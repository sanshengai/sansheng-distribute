# YouTube 官方 API：受控候选

这是**默认关闭、按账号与字段逐项验收**的候选。它调用官方 YouTube Data API 的可恢复上传协议，不沿用网页登录 Cookie。单账号已完成六集公开视频上传与独立读回；包内组件在一条已有视频上重设同一封面、读回并目视核对，同时核验已有合集成员。新合集插入与新视频组合投稿整链尚未真实验收。私密/不公开终点、字幕、额外素材和未映射字段不能静默忽略。

## 本地准备与授权

在自己的 Google Cloud 项目中启用 YouTube Data API，通过 Google OAuth 为**目标频道**取得有上传及频道身份/作品读回权限的短期 access token；具体授权范围、审核与配额以 Google 当时的官方页面和项目实际权限为准。本候选不代办 OAuth 同意或开通项目，也不使用服务账号冒充频道身份。可直接注入短期 access token；也可在用户完成 Google OAuth 授权后，注入 refresh token、client ID 与 client secret，由执行器向 Google 固定令牌端点换取短期 access token。执行器不会持久化或回显这些值，但不会代替用户重新授权。秘密以宿主秘密管理方式注入进程环境，不能提交 Git、写入 profile、清单、命令参数或截图，也不要把真实值输出到终端。不同账号须使用各自的环境变量名。

私有 profile 的配置形状如下，示例均须替换；示例默认不启用任何发布能力：

```json
{
  "accounts": {
    "owner": {
      "platforms": ["youtube"],
      "public_identities": {"youtube": "UC0000000000000000000000"},
      "youtube_token_env": "OWNER_YOUTUBE_ACCESS_TOKEN",
      "youtube_allow_component_write": false
    }
  },
  "adapters": {
    "youtube": {
      "enabled_capabilities": [],
      "execute": ["python3", "${SKILL_ROOT}/scripts/youtube_adapter.py", "execute", "--profile", "/path/to/private/profile.json"],
      "verify": ["python3", "${SKILL_ROOT}/scripts/youtube_adapter.py", "verify", "--profile", "/path/to/private/profile.json"],
      "continue": ["python3", "${SKILL_ROOT}/scripts/youtube_adapter.py", "continue", "--profile", "/path/to/private/profile.json"],
      "component": ["python3", "${SKILL_ROOT}/scripts/youtube_adapter.py", "component", "--profile", "/path/to/private/profile.json"],
      "verify_component": ["python3", "${SKILL_ROOT}/scripts/youtube_adapter.py", "verify_component", "--profile", "/path/to/private/profile.json"],
      "timeout_seconds": 1800
    }
  }
}
```

如使用 refresh 模式，将账号内的 `youtube_token_env` 移除，换成以下三个环境变量**名称**；实际值由宿主秘密管理器注入，不填入 JSON：

```json
{
  "youtube_refresh_token_env": "OWNER_YOUTUBE_REFRESH_TOKEN",
  "youtube_client_id_env": "OWNER_YOUTUBE_CLIENT_ID",
  "youtube_client_secret_env": "OWNER_YOUTUBE_CLIENT_SECRET"
}
```

两种凭据模式互斥。预检仅检查变量名及值是否存在，不向 Google 发请求；每次真实执行或读回先刷新一次 access token。刷新失败不会自行重建上传任务；已开始的任务继续保留原会话和原账本，等授权恢复后只查询或按原授权继续。令牌、刷新响应和上传会话 URL 不写普通日志或任务账本。

私有 profile、命令执行环境和 Python 解释器属于本机可信配置；不要使用不可信人员提供的可执行路径或被篡改的 PATH。适配器路径校验不构成对恶意本机配置的隔离。

`public_identities.youtube` 必须是经本人核对的频道 ID；执行器联网后独立查询 OAuth 当前频道并逐字比对，错账号、无频道或多频道均停止。交接中的 `platform_metadata` 必须明确 `made_for_kids` 布尔值与 `visibility: public`；`altered_content` 是针对平台合成内容声明的布尔值，不能由作者是否使用过 AI 一概推断。可选 `category_id` 为数字字符串；标题、简介、标签与链接按交接执行，正文和描述同时填写且不相同时拒绝，以免遗漏。不要用示例值代替作者的实际选择。

Google 同意页返回 `127.0.0.1` 时若显示连接被拒绝，表示浏览器没有连接到该次授权对应的本机回调监听；点击同意不等于本包已收到凭据。检查启动授权的进程仍在运行、回调地址属于同一台机器和同一次会话，必要时重新启动完整授权流程；不要让作者把地址栏中的授权码、令牌或客户端密钥发到对话里。合集写入的权限范围与视频上传不完全相同，按[官方合集插入权限](https://developers.google.com/youtube/data/reference/rest/v3/playlistItems/insert)核对；遇到权限不足保留原视频和组件状态，不重新上传。

## 已有视频补封面与合集

组件接力默认关闭。为对应账号在私有 profile 中显式设置 `youtube_allow_component_write: true` 并配置上面的两个组件命令；仍需记录用户对该具体任务的实际授权。只有同一账本里已核验的原视频能接力，先确认视频和目标频道，不能按相似标题猜测。组件清单使用以下结构，填真实封面文件的绝对路径和 SHA-256、已核对的合集 ID；未指定的项目为 `null`，合集位置为从零开始的整数或 `null`。该文件属于私有交接数据，不应放入公开仓：

```json
{"cover": {"id": "cover", "absolute_path": "/private/cover.png", "sha256": "<64位小写SHA-256>"}, "playlist_id": "<已核对的合集ID>", "playlist_position": null}
```

用原视频任务 key 创建组件任务，使用返回的组件 key 记录已有授权，然后分别执行需要的 `cover`、`playlist`。下例仅展示命令结构，授权语句必须是实际用户授权，不能复制示例冒充授权：

```sh
python3 scripts/publish.py --profile /private/profile.json --state-dir /private/state youtube-component-prepare ORIGINAL_JOB_KEY --spec /private/component.json
python3 scripts/publish.py --profile /private/profile.json --state-dir /private/state youtube-component-authorize COMPONENT_KEY --statement '<实际授权原话>'
python3 scripts/publish.py --profile /private/profile.json --state-dir /private/state youtube-component-run COMPONENT_KEY --part cover
python3 scripts/publish.py --profile /private/profile.json --state-dir /private/state youtube-component-run COMPONENT_KEY --part playlist
python3 scripts/publish.py --profile /private/profile.json --state-dir /private/state youtube-component-status COMPONENT_KEY
```

组件结果未知或受理后需要核验时，使用 `youtube-component-verify COMPONENT_KEY --part cover` 或 `--part playlist` 只读查询，不能再次提交。封面读回后的 `cover_visual_evidence` 给出受限来源的图片和摘要，由实际查看图片的作者或 Agent 比对原封面及集数，再以 `youtube-cover-review --help` 所列参数记录观察；这是观察者的视觉判断，不是远端原图字节校验。需要撤销时使用 `youtube-component-revoke COMPONENT_KEY`；撤销不抹掉已有未知状态。合集若需要写入，OAuth 必须具备对应的频道管理权限。

## 新视频、封面和合集的组合任务

组合入口接收一次完整封存清单，保留指定封面和合集；普通 `run` 仍拒绝带组件的 YouTube 目标，不能静默省略。完成账号与组件配置并取得用户对完整清单的授权后，按下列顺序执行；组合运行会先做本地预检：

```sh
python3 scripts/publish.py --profile /private/profile.json --state-dir /private/state youtube-bundle-prepare MANIFEST --target TARGET
python3 scripts/publish.py --profile /private/profile.json --state-dir /private/state authorize MANIFEST --target TARGET --statement '<实际授权原话>'
python3 scripts/publish.py --profile /private/profile.json --state-dir /private/state youtube-bundle-run JOB_KEY
python3 scripts/publish.py --profile /private/profile.json --state-dir /private/state youtube-bundle-status JOB_KEY
```

准备命令返回原任务 key。`youtube-bundle-run` 会写入平台；视频完成读回后才分别处理指定组件，并单独报告视频、封面和合集的状态。封面需通过上面的视觉复核，复核后再次运行同一组合任务以收尾；收尾时重新读取视频、合集及平台缩略图，缩略图摘要必须与视觉复核时一致。该比对不保证各客户端的缓存均已刷新。缺字段、账号受限或结果未知时不能宣称整包完成。网络中断时先查询原任务；只有确认继续**同一上传会话**且已有授权时才执行 `youtube-bundle-continue JOB_KEY`，不会自动创建新投稿。

先按 [交接](handoff.md)封存、`pack` 和 `preflight`。本地预检核对命令、账号配置、令牌环境变量存在、源文件及目标字段，不联网，不证明 token 有效或平台有投稿权限。真实首次公开投稿只在作者已有明确授权时，使用 [首次公开验收](acceptance.md)中的 `first_publication_runs` 精确绑定一次清单、频道、作品、终点和有效期，并记录实际授权。即使公开视频上传成功，平台处理、审查与匿名可见也可能延迟；不能把受理当成发布完成。

## 原会话继续与保留数据

首次执行前，程序在私有状态目录创建完整视频快照并校验 SHA-256，至少需要**一个额外视频大小**的可用空间；随后生成仅本机可读的上传会话记录。它们均包含私有内容，尤其 session URL 可被视为凭据，不能放入公开仓、账本报告或发给他人。保留这些文件和原账本直至任务完成并确认无后续恢复或验收需要；清理前先核实消费者，不能只因为空间不足就删除唯一恢复点。

如果上传中断，正常 `resume` 只查询原会话，不上传；不会按标题猜测作品或重新创建会话。确认继续原投稿时，以原状态目录、原 profile 和账本中的**原任务 key**运行：

```sh
python3 scripts/publish.py --profile /path/to/private/profile.json --state-dir /path/to/private/state continue-youtube ORIGINAL_JOB_KEY
```

这条命令会向原会话继续写入，不是只读操作。它必须通过原用户授权、同一账号锁、原任务快照、频道身份、素材摘要和当前平台能力检查；先向原会话查询已接收位置，再从那个位置续传。没有可信会话、会话过期或创建会话响应丢失时保持结果未知，不能自动重新建单。当前统一核心在续传前仍要求生产端原片存在且未改变：如果原片已经删除或变更，先保留私有快照和账本，不能强行重发。

## 验证边界

完成返回视频 ID 后先保存原任务，再独立读回频道、标题、简介、标签、受众、隐私、已提供的合成声明、处理状态与匿名 oEmbed；内容不一致、处理未完成或匿名读回不足时不报告已发布。YouTube 不提供本路径上的远端原片 SHA-256；**本地快照哈希与同一上传会话绑定，不等于平台对远端原片做了哈希验证**。单账号 OAuth 刷新、六集视频上传及单条既有视频的封面组件已有真实证据；错误恢复、其他账号、字幕、配额耗尽、新合集插入及新视频组合整链不能据此认定已通过。

依据：[YouTube 可恢复上传协议](https://developers.google.com/youtube/data/guides/using_resumable_upload_protocol)、[videos.insert](https://developers.google.com/youtube/data/reference/rest/v3/videos/insert)、[videos.list](https://developers.google.com/youtube/data/reference/rest/v3/videos/list)。
