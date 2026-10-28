---
name: sansheng-distribute
description: 将已有文章、图文和视频交接到指定社交平台，处理公众号媒体插入、草稿、发布、核验与续跑。用于发布与分发任务；文章创作、排版及视频制作仍由各自内容 Skill 负责。
metadata:
  version: "0.14.2"
---

# 叁笙分发

当前为实验版。接收生产端定稿和媒体位置，按指定平台、账号、终点执行；平台能力逐项验收，不因安装 Skill 就开启发送。

优化批量上传、复用浏览器步骤、试用 B 站 CLI 或评估小红书 MCP 时，按需读[发布提效与可选命令行](references/efficient-dispatch.md)。两平台并行须显式启用；小红书仍由作者最终点击，受限账号只交付本地包。B站受控适配器已接入核心授权、账号锁、摘要快照与独立读回；统一适配器已完成单账号技术片及常规横屏系列真实投稿与独立读回，不能据此自动开启其他账号或未覆盖字段；审核中保持 pending。

读取私有 profile 的 `distribution_preferences` 后再选择路线与并行数；该字段是作者偏好，不能扩大 `accounts`、已验收能力或授权终点。未配置的平台不因偏好表中出现就自动登录或启用。

首次安装读 [配置与诊断](references/setup.md)；多目标任务读 [批量分发](references/campaign.md)；社交平台发布包与执行读 [平台适配](references/social-adapters.md)。先读 [平台能力与证据](references/platforms.md)。为视频准备各平台标题、简介、话题、相关活动、声明、合集，以及每次分发复盘时读 [平台文案](references/platform-copywriting.md)；接收新内容读 [交接与运行](references/handoff.md)；公众号操作读 [公众号流程](references/wechat.md)；浏览器接力读 [Agent 浏览器执行](references/browser-execution.md)；超时或无远端 ID 读 [恢复流程](references/recovery.md)；安装、能力试验与跨 Agent 验收读 [验收方法](references/acceptance.md)。不默认加载全部历史材料。

分发后在对话中按平台简述状态；系列多集优先按[批量分发](references/campaign.md)中的 `series-report` 生成精简看板，每个平台账号一行、每集一个状态格，并说明有证据的原因和建议。普通任务可用 `batch-report` 查看详细信息。明确区分账本快照、草稿、待作者操作和已核验公开；无记录不等于未发布，不创建假记录凑看板。

了解本地支持的交接类型与已配置规则时，运行 `python3 scripts/publish.py platform-guide <平台>`，按需读[平台交接查询](references/platform-guide.md)；它不联网，也不证明平台能力已验收。扩展路线和竞品取舍见[分发架构](references/architecture.md)。

视频改标题或换封面后再分发，先用[同视频候选查询](references/duplicate-check.md)检查同一私有账本；候选或无记录均不代替平台核验。需要呈现封面、合集、声明、话题的已核验情况时，按[分项观察](references/component-results.md)记录证据，未核验不能写成成功。若作者明确配置节奏，按[发布间隔](references/pacing.md)执行；本地间隔不代表平台规则或风控保证。

多目标首次执行前，用 `batch-preflight <manifest>` 一次收齐所选平台的本地阻塞，再准备和执行；已有任务沿原账本查询恢复。它不联网、不建任务或授权，本地通过不代表登录或发布通过。API路线先用已验证的适配器；没有可用接口的平台复用已有浏览器流程，不因提效任务默认扩大到新发送引擎。

显式选用包内浏览器引擎时读 [内置引擎](references/browser-engines.md)：X 与微博的文字、多图、视频，以及抖音视频已做真实预填与只读续跑；X/微博视频核对原片字节和原生云端回执，微博另验封面、类型/声明，抖音另验横竖封面、原生话题、声明和指定合集。预填终点不发送，临时编辑会话每次复用重新核对。

X 单 MP4 已完成真实系列发送与独立读回，微博单视频已完成真实发送及缺失作品 ID 的候选恢复；这些单账号实测不自动开放其他账号。文字、多图和抖音发送仍分别按验收证据判断；各自支持范围和未覆盖字段见内置引擎。首次试验按 [验收方法](references/acceptance.md) 绑定具体内容，不以已有作品读回代替本次发送验收。

先解析当前 `SKILL.md` 所在真实目录作为 Skill 根；下列 `scripts/...` 路径相对该目录，不相对用户工程。核心需 macOS/Linux、Python 3.10+ 及执行本机命令的工具（使用 POSIX 文件锁）；不能据此宣称支持 Windows。核心仅用标准库，平台另有依赖：公众号使用本包内置官方 API（旧配置的显式 Write 模块仍兼容），补媒体还需当前 Agent 的浏览器能力。

- `seal` 锁定生产端已选文件和位置；不决定媒体插哪里，也不代表审核/发送授权。
- `check` 核对文件、哈希、媒体位置、平台目标。
- `media-inspect <manifest> --target <目标>` 使用本机可选的 ffprobe 只读检查封存视频的实际宽高、旋转、时长和大小；不转换素材、不连接平台，也不证明符合平台规则。缺少 ffprobe 时会明确提示。
- `preflight` 在联网写入前核对可执行程序、适配器脚本，以及公众号的 HTML、封面、内置或显式旧 API 模块和账号凭证绑定；不请求 token、不上传。`run` 自动执行此检查。
- `pack` 输出平台专用发布包，保留标题、正文、话题、链接、封面和字幕；规格不符交回修改，不静默截断。
- `batch-prepare / batch-authorize / batch-run / batch-resume / batch-status` 逐目标记账；失败继续其他账号目标，结果未知只查询，已完成不重发。批量准备不等于批量授权。
- `prepare` 建立本地任务；`authorize` 记录已有实际用户授权，绑定版本和目标，不编造批准或重复询问。
- `run` 调用执行器，立即记录远端身份，再调用独立读回器。
- `resume` 只查询已开始任务，不重复提交；`status` 查看真实状态。
- `continue-youtube <key>` 在原授权与账号锁内继续同一 YouTube 上传会话，会写入平台；区别于只读 `resume`。[YouTube 官方 API](references/youtube-api.md)默认关闭，单账号公开视频上传已有真实验收；其他账号和分项能力按各自证据判断。
- `youtube-bundle-*` 为新 YouTube 视频、封面和合集建立分项组合任务；`youtube-component-*` 可接力已核验的视频，封面视觉复核单独留证。命令、授权及实测范围见 [YouTube 官方 API](references/youtube-api.md)；未知结果只读核查，不重传视频。
- `metrics --key <任务key>` 查询同一账本的执行/核验计量，不操作平台；复盘效率时按需读 [耗时计量](references/metrics.md)。缺样本不当作零耗时。
- `recover-wechat / recover-x / recover-weibo` 对无 ID 的未知提交，用明确候选 ID 经对应平台独立读回后绑定同一任务；不新建、不重发。公众号核对官方草稿，X/微博视频另核对原上传媒体及来源。候选不明或核对失败继续保持未知，见恢复流程。
- `advance` 仅在公众号草稿已读回、明确待补媒体、补完整网址或视觉检查时继续同一稿，复用当前版本授权；不与只读恢复混用。
- `adopt` 只读认领 Write 显式导出的旧公众号草稿，锁定同一远端 ID；迁移包禁止新建，旧占位按 `legacy_anchor` 与上下文核对。详见[公众号迁移](references/wechat.md)。
- `return-write` 对同一草稿重新读回，生成绑定当前原稿、媒体和观察证据的 Write 凭证；由 Write `import-publish` 消费。重验保留旧证据，不重新建稿；正式文章收尾仍须另验。

先运行 `python3 setup.py --show-profile` 核对实际配置与账本；新安装使用 `sansheng-distribute` 目录，既有 `sandy-publish` 默认路径和变量继续识别。新旧账本并存时明确选定，不自动迁移。`python3 setup.py --doctor` 只做本地检查。所选浏览器模块使用 `--prepare-runtime` 准备固定依赖，`--runtime-status` 检查版本，`--browser-status` 只读核对专用 Chrome；详见配置指南。秘密不进交接包、Git 或输出。

## 不可丢失的边界

1. Write 决定排版和媒体位置，Video 提供成片；按 `media_slots` 插入，不猜位置、不重排文章。零个或多个匹配先修交接。
2. 区分上传素材、插入播放器、发表、群发。普通素材 ID 不冒充原生播放器 ID；API 按账号和媒体类型实测，不支持时操作编辑器可见功能补齐。
3. 草稿不自动升级公开发布。先准备具体可审阅内容，一次明确授权内连续执行；登录/平台验证确需本人时才接手。
4. 小红书最后点击由作者完成，受限账号只准备本地包；腾讯视频 `tencent_video`、视频号 `wechat_channels`、公众号 `wechat` 分别建目标，不能互相替代。
5. 结果未知先查询，不能重复上传/点击。成功要有账号、远端 ID 和内容读回；上传完成、受理、审核中均不叫发布完成。
6. 每次分发必须复盘：交付前按 [分发复盘](references/retrospective.md) 核对结果并总结；可复用且有证据的经验更新对应规则。没有新经验也记录已复核，不为积累条目重复造规则。
7. 浏览器遵循当前 Agent 的电脑工具约束，核对专用 profile；默认后台执行，不主动激活 Chrome 或切换前台标签。本人扫码、验证等确需交互时再提示；通道失败不切作者日常浏览器，不换技术绕过限制。

支付宝生活号按[生活号工作流程](references/alipay.md)使用；其真实自动投稿与稳定作品身份读回尚未完成验收。

新增适配器先锁上游版本与许可，再实现执行、独立查询；只填表的脚本不能登记为完整发布能力。未验收能力保持 disabled；已有授权的首次草稿/预填试验使用精确 `acceptance_runs`；首次公开投稿使用验收方法中的独立 `first_publication_runs`，绑定单次内容、账号、终点及有效期，不临时开放整个平台。

## 名称与兼容

正式名称为「叁笙分发」`sansheng-distribute`，作为通用能力公开；实验阶段按平台能力表使用。旧 `sandy-publish/scripts/` 只保留脚本转发入口，不再有独立 Skill 入口。

更名保留既有默认配置、状态目录以及 `SANDY_PUBLISH_PROFILE` / `SANDY_PUBLISH_STATE` 环境变量；新旧脚本读取同一账本，不迁移或复制任务。指定了 `--state-dir` / `--profile` 的工程继续使用原值。不要因改名新建空账本或重复建稿。

职责：Write / Video 等生产端负责内容成品和媒体位置；本 Skill 负责平台分发，不负责 Skill 开源或网站部署。
