# 发布提效与可选命令行

## 路线选择（2026-10-25 核对）

| 平台 | 本轮采用 | 执行边界 |
|---|---|---|
| X | 免费浏览器流程 | 不为本流程开通付费 API；复用现有可选宝玉预填桥或宿主浏览器工具 |
| B 站 | 浏览器保持可用；包内受控 biliup 适配器默认关闭 | 统一适配器已完成单账号技术片与常规横屏系列真实投稿及独立读回；其他账号、合集和活动仍需分别验收 |
| 抖音 | 创作者网页 | 政务代投稿接口不视为普通个人接口；不伪造机构资质 |
| 微信视频号 | 视频号助手网页 | 与公众号、腾讯视频分别处理；读取真实合集、话题和声明字段 |
| 小红书 | 解限后只预填，作者最终点击 | 受限账号仅交付本地发布包，不启动自动化；不用人手点一次推断可以避免风控 |

抖音依据：[代用户投稿能力](https://developer.open-douyin.com/capacity-center-page/capacity-detail/7224121299067469881)、[入驻问题](https://partner.open-douyin.com/docs/resource/zh-CN/developer/join/issues)。B 站 CLI 使用 [biliup v1.3.4](https://github.com/biliup/biliup/releases/tag/v1.3.4)，其网页/客户端协议实现不等同于平台公开、承诺稳定的投稿 API。

以上是可选路线，不替用户开启新账号或扩大已验收能力。账号的路线偏好、合集映射及禁用状态保存在私有配置。

## 先整理，再并行

1. 一次完成素材摘要、格式、封面、标题、集号、描述、原生话题候选和声明的检查。错误交回生产端，不在上传过程中擅自改定稿。
2. 只留一个平台一个编辑页。用实际页面语义、定位器与可见状态等待；不凭固定坐标重放，不用盲目重复点击补救超时。
3. 普通批次默认串行。宿主工具允许并行、账号状态正常且资源足够时，在原 `batch-run` 加 `--max-parallel 2`。不同平台最多两个工作线程，同平台即使不同账号也串行，避免争抢同一创作者页面。每个线程独立连接同一本地账本；核心账号锁和授权不变。
4. 一个目标完成后独立读回，另一个上传可以继续。结果未知先查询，不能因为另一个平台成功就重传失败项。
5. `batch-resume --max-parallel 2` 仍然只读，不启动尚未开始的目标。

并行参数控制目标执行，不保证宿主浏览器工具本身能并发。宿主不支持时使用默认串行，不改工具接线来绕过限制。

## 可选浏览器预填助手

`scripts/browser_prefill.py` 在已经存在的 `social_adapter` 执行接力内处理上传和填字。它不会建立新发布任务、切换账号、导航、点击任何按钮或发送完成回执。Agent 仍需负责原生话题、合集/活动、封面、原创/AI 声明以及独立读回。

按需读取本节，使用当前宿主允许的浏览器通道。宿主不允许外部 CDP 脚本时，不运行本助手，继续原有宿主浏览器工具。不要因本助手存在就认为所有 Agent 都可直接执行 Playwright。

在私有 profile 中显式设置 `browser_prefill`，默认不存在即关闭。先用 `setup.py --prepare-runtime` 准备本包固定依赖；助手不自行安装或启动 Chrome。配置示例（定位器和身份文本必须现场核实）：

```json
{
  "browser_prefill": {
    "cdp_url": "http://127.0.0.1:9333",
    "recipes": {
      "douyin": {
        "enabled": true,
        "page_path": "/creator-micro/content/post/video",
        "identity_selector": "REPLACE_WITH_VERIFIED_ACCOUNT_ID_SELECTOR",
        "steps": [
          {"action": "upload", "selector": "input[type=file]", "asset_id": "video"},
          {"action": "fill", "selector": "input[placeholder='填写作品标题，为作品获得更多流量']", "field": "title"},
          {"action": "fill", "selector": "[contenteditable=true]", "field": "description"}
        ]
      }
    }
  }
}
```

示例不构成已验收配方；不要把未核实的身份定位器直接启用。每条定位器必须唯一。允许的步骤只有 `upload`、`fill`、`wait_visible`；不接受任意 JS、点击、字段名或来源不明文件。文本只取封存包 `title/text/description`，不包含自动拼接的井号，以免把纯文本误当原生话题。

调用：

```sh
python3 scripts/browser_prefill.py /absolute/path/request.json --profile /absolute/path/profile.private.json
```

该入口会自动使用 `setup.py --prepare-runtime` 准备的固定私有环境，系统 Python 不必安装 Playwright。自定义位置须保存在同一 profile 的 `runtime_dir`，执行时不猜其他 Skill 的环境。

前置条件：当前 `execute` 租约活跃；manifest 与发布包摘要一致；实际 Chrome 命令行的 `--user-data-dir` 与绑定 profile 相同；当前编辑页来源/路径和真实账号身份一致。Chrome 未开 `--enable-automation` 时，由该 CDP 连接返回的实际浏览器 PID 只读查询进程参数；仍无法核对时停止，由原宿主工具接管，不自动重启浏览器。每次写入前复核租约和身份，上传前再验素材摘要。

同一请求一旦开始就写入 `.prefill-attempt.json`，包含步骤耗时和状态，不记正文或 Cookie。再次运行拒绝重传；Agent 先检查同一页面并按核心恢复流程处理。`steps_completed` 只表示这些步骤执行完，不表示上传已处理完成、预填已验收或已发布。切勿据此手工把账本改成完成。

## B 站 CLI 安装、认证及验证

可选工具固定到 Apple Silicon macOS 的 biliup-cli 1.3.4，先验证上游压缩包 SHA-256，提取唯一可执行文件，再保存二进制摘要。每次运行前验证版本和摘要；已有目录/凭证拒绝覆盖。首次配置可用统一安装入口选择包内受控适配器，仍保持发布能力关闭；详见[配置指南](setup.md)。

```sh
python3 scripts/biliup_bridge.py install --directory /private/tools/biliup-1.3.4
python3 scripts/biliup_bridge.py probe --directory /private/tools/biliup-1.3.4
python3 scripts/biliup_bridge.py login --directory /private/tools/biliup-1.3.4 --credentials /private/auth/bilibili.json
python3 scripts/biliup_bridge.py identity --directory /private/tools/biliup-1.3.4 --credentials /private/auth/bilibili.json --expected-uid 123456
```

作者用 B 站手机客户端扫 CLI 的二维码并确认账号。凭证文件权限设为 0600，不上传 Git、不回显。登录只解决认证，不能证明投稿权限、合集资格或字段覆盖；CLI 不要求先申请机构开放平台资格。

`plan` 使用封存 manifest、目标、profile、分区 `tid` 和私有凭证路径，仅输出参数数组：

```sh
python3 scripts/biliup_bridge.py plan --directory /private/tools/biliup-1.3.4 --credentials /private/auth/bilibili.json --manifest /private/manifest.json --target bili --profile /private/profile.json --tid 171
```

`tid` 必须实查，示例不是默认推荐分区。计划只接受显式声明 `platform_metadata.copyright=1` 的单视频自制投稿及可选封面。受控执行器还要求显式布尔 `platform_metadata.ai_generated`；true 时通过 `--extra-fields` 传入 `creation_statement: {"id": 1}`。字幕、合集、活动、额外正文、独立 links 等尚未覆盖时拒绝生成，保持浏览器路线。不要为通过检查删除真实 AI 声明。`biliup_bridge.py plan` 仍只规划参数；真正上传必须由核心 `run` 调用 `bilibili_adapter.py`，不在锁外直接照抄命令。

受控适配器在账号核对后将封存视频与封面复制到本任务私有目录，边复制边计算摘要；不符即在上传前拒绝。CLI 只读取这份快照。启动前保存尝试标记，受理响应一出现就保存 `aid/bvid`，CLI 随后异常也不丢失远端身份。后续 `resume` 只查同一稿，后台与公开读回相符才完成。外层超时终止整个执行组，避免 CLI 留在后台继续投稿；仍须先查询实际结果。

配置默认外层预算2100秒，CLI默认1500秒；外层必须至少比CLI多180秒，否则在上传前拒绝。私有日志可能包含上传签名URL，不回显或提交。上传快照和尝试证据保留到稿件确认、复盘与续跑不再需要时，再按任务归属清理。

启用真实 CLI 上传前，须用新的、已授权的内容完成：登录 UID 核对、实际字段等价性、AI 标识适用性、进入核心授权/锁/未知恢复、保存真实稿件 ID、独立作品页读回及失败反例。不能拿历史已发布内容重复投稿试验。

### 真实试验所得（2026-10-25）

已用一条新的、明确授权的视频走固定版 CLI `--submit web`：上传和投稿接口均成功，独立后台读回确认标题、简介、标签、封面和 `creation_statement.id=1`，随后后台开放浏览，公开作品页核对同一稿件、账号、集名、时长与 AI 标识，并观察播放器实际播放。该证据仅覆盖该账号的单条普通视频，不代表合集、活动和所有账号均已验收。账号、稿件 ID、文件路径与具体证据仅留私有任务账本。

- 从 code=0 的投稿响应先保存 `aid/bvid`，随后按 ID 查询 `/x/vupre/web/archive/view?aid=...`。新稿可能尚未出现在作品列表中；列表未命中不等于失败，不重传。CLI exit=0 同样不能代替保存响应身份。
- “审核中”“复核中”保持 `pending`，只有后台开放与独立公开作品读回相符才登记 `published`。公开查询 HTTP 错误仅说明该次查询不可用，保留已受理身份并只读恢复。
- 核对封面实际内容：首帧可能是空白底或 AI 提示，抽取有品牌和集号的正式画面，不盲目使用第0帧。
- 执行 CLI 子进程仍继承核心账号锁；单次授权范围不扩大到其他集，原始 CLI 输出留私有目录并设0600，日志可能含上传签名地址，不提交公开仓。

字段实现依据：[biliup 固定版源码](https://github.com/biliup/biliup/tree/v1.3.4)；[self-media-uper 的 B站实现](https://github.com/lzy198436/self-media-uper/blob/main/smu/platforms/bilibili.py)仅交叉核对字段设计，未复制其代码。网页协议依然可能变化，逐次独立读回实际声明。

## 小红书 MCP 审计结论

研究源：[xpzouying/xiaohongshu-mcp](https://github.com/xpzouying/xiaohongshu-mcp)，审计固定提交 `7797fd375aba02dcd33afe143f6bee13e5870859`。本轮不安装整个服务，也没有复制其代码。

- 视频发布路径包含最终点击；不能直接当作“作者最后发送”的视频预填接口。
- 视频响应缺少足以独立验收的作品 ID，不能根据 `status` 自报成功认定发布完成。
- 独立 Skill 的预填管线与 MCP 视频接口不是同一条能力，不能混用它们的宣传结论。
- 截至审计，涉及草稿、风控识别、标签定位和缺文件校验的 PR，以及视频原创标识问题仍需逐项核实：[PR #870](https://github.com/xpzouying/xiaohongshu-mcp/pull/870)、[PR #872](https://github.com/xpzouying/xiaohongshu-mcp/pull/872)、[PR #867](https://github.com/xpzouying/xiaohongshu-mcp/pull/867)、[PR #868](https://github.com/xpzouying/xiaohongshu-mcp/pull/868)、[Issue #873](https://github.com/xpzouying/xiaohongshu-mcp/issues/873)。未合入改动不算当前可用能力。

可以借鉴有状态等待、明确登录/验证错误、原生话题实体读回和严格素材检查。未来确需复制代码时保留 Apache-2.0 许可与归属，并先加入禁最终点击、缺文件及结果未知反例。现有问题报告仅说明存在风险，不证明某个操作间隔安全。

## 频率、验证与计量

复用稳定的专用浏览器与登录会话；同账号串行；上传结束再进入下一状态；收到验证、警告、频率限制时暂停该账号，不自动换账号/代理、绕过验证码或反复重试。没有可验证证据支持“随机等待即可低风险”，不把随机鼠标轨迹、伪造指纹和所谓成功率写成安全承诺。

每批复盘记录目标准备、上传等待、人工介入、读回耗时，截图次数和重试次数；区分平台传输等待与 Agent 操作时间。先积累同内容量和网络条件的实测，再宣称提速百分比。失败只保留必要证据，排除登录秘密、签名媒体地址和无关页面内容。
