# 结果未知与草稿恢复

超时不等于未提交。先 `status`，再用 `resume-job KEY` 对持久快照只读查询；不要重新运行上传器、换 job_id 或新建空账本。原工程文件清理后仍可查询，但重新提交必须重新校验源文件与授权。

`status` 显示账本上次观察；显式 `resume` / `resume-job` 即使上次已完成，也重新查询同一远端身份。远端已变化时记录当前观察，查询失败则报错，不能把缓存的完成状态当作本次核验成功。普通 `run` 对已完成任务仍保持幂等，不重新提交。

## 公众号建稿成功但响应 ID 丢失

仅当任务为 `submission_unknown`、账本没有远端 ID、适配器已留下真实提交记录、原 AppID 指纹及预期正文时，才可恢复绑定。同一账号别名后来改绑另一个合法 AppID 也会拒绝，不能靠当前凭证有效来证明它是原账号。

1. 从平台后台或官方只读查询取得明确候选草稿 ID，同时核对账号、时间、标题。标题或时间只能用于寻找候选，不能证明绑定正确。结果缺失、多候选无法区分时停留在未知状态，不猜、不重建。
2. 执行以下命令。ID 必须来自实际查询，不能从本地任务 key 生成；此命令不自动搜索列表。

```sh
python3 scripts/publish.py --profile /path/profile.json --state-dir /path/state recover-wechat KEY --remote-id ACTUAL_DRAFT_ID
```

3. 调度器持同一账号锁，核对封存快照，拒绝本账本其他任务已占用的 ID。公众号核验器用指纹绑定的账号调用官方 `draft/get`，比对预期标题等元数据、上传封面 ID、全文、图片与链接顺序；只接受核验通过的候选。本文所说全文核对为内容与顺序等价，不要求平台序列化 HTML 逐字相同。
4. 通过后保留同一草稿 ID，按实际结果进入补媒体、外链修复、视觉检查或完成状态。恢复绑定不等于草稿内容已经完整；后续操作沿用原任务和授权。缺提交记录、错账号、错稿、坏快照或核验器未给出专用恢复确认均拒绝绑定。

这是明确候选的核对与恢复入口，不是跨平台自动找稿服务。相同内容存在多份且无法区分时，需人工核实实际目标；不以“内容看起来相同”宣称找回唯一原稿。执行器与核验器是受信本机程序，仍须经过独立平台验收。

0.2.0 之前的记录没有冻结 AppID，已有 ID 的普通读回保持兼容，但不能声称历史账号身份已被代码冻结；无 ID 的旧记录禁止用新恢复入口自动绑定，不事后补写指纹冒充原始证据。新提交与新认领记录持久保存账号指纹，后续读回均核对。

## 已有 ID 的中断

- 建稿后读回失败：`resume-job KEY`，只查询。
- 公众号媒体插入一半：先 inspect-media 与编辑器读回，保存已经完成槽位的真实对应证据，再 `advance`，只补余下槽位。
- Write 旧稿：使用显式 `export-publish --existing-draft` 及 `adopt`，见 [公众号流程](wechat.md)。不能把旧稿伪造成一次新的未知提交。
- 只有独立查询明确证明 `not_submitted / absence_verified=true` 才允许重试提交；查询异常、空列表、未加载完成均不是该证明。


## X / 微博视频已发送但作品 ID 没有落账

只有账本为 `submission_unknown` 且没有远端 ID、原任务保留唯一实际发送记录和完整源媒体上传证据，才使用以下只读恢复。它不重新点击、上传或建立新任务。

```sh
python3 scripts/publish.py --profile /path/profile.json recover-x KEY --remote-id ACTUAL_POST_ID
python3 scripts/publish.py --profile /path/profile.json recover-weibo KEY --remote-id ACTUAL_POST_ID
```

候选 ID 从真实平台取得；标题、时间和相同文案只能用于寻找候选。X 必须核对同一原片云端媒体 ID、账号、全文、视频元数据及可见播放器。微博必须核对同一上传媒体 ID、原始封面裁剪上传 PID、账号、标题、全文、创作类型、AI 声明及公开播放器；作者编辑页与公开详情也必须相互对应。来源证据缺失时不能从公开作品反向补造。错误候选、其他任务占用的 ID、账号或来源变化均拒绝认领。

微博视频可能先返回转码受理、当次响应没有作品 ID。页面提示“上传成功”不等于已发布；保留原任务，待原媒体的作品出现后核对候选。恢复通过后，普通 `resume` 仍重新查询同一作品，而不是返回旧成功缓存。X/微博使用网页原生请求和独立页面读回，不称为官方开放 API。

## 上传或点击之前失败

`resume` 可在两种已证实的情形返回 `prepared`，之后沿同一任务执行：

- 首次平台选片前失败：限定原记录阶段、同一账号和原窗口，没有文件、上传回执或任何发送标记，独立核对后归档旧尝试，再新建本任务编辑器。窗口丢失、已经开始上传或发送结果未知均不适用。
- X/微博视频已完整上传，但尚未发生原生发送输入：独立核对原编辑器、完整上传证据、封存文案与媒体；随后复用该稿，仅执行一次发送。已经记录发送输入或受理/错误回执时，不能靠空列表推断未发送。典型：`click_error_phase=before_native_input` + 「后台点击目标…被遮挡」= X 话题联想残留空遮罩（已在 `dismiss_topic_suggestions` 修复）；先 `resume KEY` 再 `run`。

- 微博视频已完整上传、但填写前就停止（原窗口编辑器为空、没有封面和任何发送记录）：`resume` 只读核对同一窗口、云端回执和原片元数据后返回 `prepared`，再 `run` 在同一稿填写并只发送一次。已开始填写的不适用。

- 微博视频已填写、封面上传请求已记录但响应漏收：`resume` 只读核对页面封面 PID 的公开原图与封存原图（尺寸及缩略画面）、文案与声明、原视频以及无发送痕迹，然后返回 `prepared`；`run` 在同一稿只发送一次，不重传封面。

恢复只归档本任务的已确认前置失败，保留来源和旧回执。抖音“继续编辑”提示可能属于旧测试稿或作者新稿，应先辨认归属；这不属于自动放弃草稿的授权。


## X 执行器未写尝试记录就停止：作者确认

执行器在第一次持久记录前就失败（例如专用 Chrome 中没有唯一的 x.com 入口页），账本仍会记 `submission_unknown` 并占账号锁，但没有 `.browser-attempt.json`。仅限 X：

1. 只读核对账号时间线，写 JSON：`observed_at`（带时区）、`account_identity`、`query`、`matches`（必须为空）、`evidence_path`。
2. `python3 scripts/confirm_no_attempt.py --state-dir <账本> --key <任务key> --identity @handle --statement '<作者原话与时间>' --confirmed-at <ISO> --timeline <JSON>`。只读账本，只写 `requests/<key>.no-attempt-unsubmitted.json`。
3. `resume` 再次核对请求目录无痕迹后记为未提交（`absence_scope=author_confirmed_no_attempt_record`）并释放锁，之后 `run` 正常执行一次。

账本事件含任何观察、已有远端 ID、请求目录出现尝试记录/归档/素材快照/原生回执等任一文件、时间线有匹配或账号不符都会拒绝。教训：发布前先确认专用 Chrome 里有已登录的 X 后台入口页。

## X 窗口已打开、编辑框未渲染就停止

`owned_window_opened` 阶段（已写尝试记录、未选文件）停止时：执行器现在会先在同一窗口只读自检并写入 `preinput_self_check`。`resume` 重新只读证明原窗口在 `/compose/post`、账号一致、无警告、编辑框为空、无媒体和已选文件；自检通过即记未提交并释放锁。旧尝试没有自检时，需作者确认：先只读核对时间线（无匹配），再 `confirm_unsubmitted.py --listing <时间线JSON>`。有上传事件/错误、发送痕迹或窗口不为空都拒绝；原窗口丢失也须作者确认。根因与处理见 `scripts/x_compose_ready.py`：后台窗口未绘制时每轮做一次原生画面捕获（不激活）再等待，有上限。

## X 选片后上传未开始

`before_native_selection` 且上传事件为空时，`resume` 走已有首次选片前恢复（原窗口为空即记未提交）。执行端已加固：上传前 ffprobe 解码检查、等待期间定时原生画面捕获、执行期间 caffeinate（随进程退出）。若仍复现，不连续重试；先确认屏幕点亮、未锁定再试一次。

## 抖音首次上传前尝试：原窗口已丢失时的作者确认

`browser_preinput` 正常要求原自建窗口仍在，才能证明未选文件。若窗口已被关闭，而尝试记录仍是 `owned_window_opened`、`phase=upload`、`video=None`、上传事件为空且无任何发送痕迹，可由作者书面确认未提交：

1. 先在同一账号只读核对作品列表，把结果写成 JSON：`observed_at`（带时区）、`account_identity`、`query`、`matches`（必须为空）、`evidence_path`。
2. `python3 scripts/confirm_unsubmitted.py --state-dir <账本> --key <任务key> --statement '<作者原话与时间>' --confirmed-at <ISO> --listing <JSON>`。只写 `requests/<key>.browser-attempt.json.author-unsubmitted.json`，不改账本。
3. `resume` 只读核对后记为未提交（`absence_scope=author_confirmed_before_first_platform_upload`），之后 `run` 归档原尝试并在新窗口执行一次。

确认文件绑定原尝试摘要；尝试记录变化、列表有匹配、任务已有远端 ID 或非结果未知都会拒绝。此路径目前只开放抖音；已开始上传或填写的尝试不适用。教训：首次上传前失败时保留原窗口，不要在收尾时关闭。


## 抖音上传回执 MD5 为 null

原生 CommitUploadInner 回执可能返回 `VideoMeta.Md5=null`。此时 `douyin_upload.receipt` 不比 MD5，而要求：播放器 `currentSrc` 读回字节的 SHA-256 与封存原片一致，回执 Size 与本地大小一致，Width/Height/Duration 与预览一致，Uri 对应已核对的预览对象。任一不符仍停止，不重传。非 null 的 MD5 仍须与本地一致。旧尝试若因此停在 `native_video_commit_unrecorded`，回执已被判为未记录（video_upload_errors 非空），不能事后补认；按“已上传未填写”或新尝试处理前先确认无重复作品。


## 抖音已上传未发送：作者确认释放

条件全部满足才释放：作者确认文件（原话、带时区时间）；同账号只读作品列表 `matches=[]`；账本无远端 ID；尝试记录状态为 `owned_window_opened/attempted`、`phase=upload`，无 `submission_attempted_at`、`native_input_checkpointed_at`、`accepted` 等任何发送字段；原窗口若存在须在上传/编辑页。步骤：只读列表核对 → `confirm_unsubmitted.py` → `resume`（记未提交）→ `run`（归档为 `.released-*`、关闭该任务自有窗口、新上传一次）。

## 无尝试记录的抖音任务

抖音执行器在导航前写尝试记录；若 `resume` 发现既无记录也无任何归档，说明执行器未触及平台，直接记为未提交。存在任何同 key 归档时走归档规则，不适用此捷径。

## 回执校验失败的留证

抖音 CommitUploadInner 回执不合规时，`video_upload_diagnostics[].native_identity_candidate` 保存白名单字段，供作者决定；它不自动转成已核验回执。

## 抖音已上传未填写：原窗口续填

条件全部满足才放行：回执字段（VID、大小、宽高、时长、对象）与本地一致；播放器字节 SHA-256 读回一致；无任何发送痕迹；原窗口仍在上传后编辑器；作品列表无匹配并已写作者确认。流程：查重读回 → `confirm_unsubmitted` → `resume`（verify，状态 `uploaded_resume_verified`）→ `run`（原窗口填写、只发送一次、关闭自有窗口、返回 pending 待读回）。任一条件不满足即拒绝，不重新上传。

## 抖音锚点标签丢失

锚点被其他任务关闭时，自动以后台方式新开创作者中心锚点继续，用后关闭；不前置窗口。

若续填核对失败（如预览签名过期无法读回字节），作者可选择释放：`confirm_unsubmitted.py --release` → `resume`（释放核对）→ `run`（关闭原窗口、归档、重新上传并立即读回字节）。

## 抖音上传核对（作者决定，2026-11-01）

只信原生上传回执：视频 ID、大小、宽高、时长、存储对象逐项与本地一致；播放器仅读元数据（宽高、时长、对象路径），不读预览字节。发布后读回作品时长与封面。依据：预览服务器只给脚本部分字节（整文件 403，前缀长度不稳定）。任一不符拒绝、不重传。今后不再用预览字节读回做核对，也不对预览服务器做探测。

## X 时间线复核滞后
新帖受理后主页时间线可能滞后几秒到几分钟。时间线零命中时按约 45 秒间隔重查（最多 6 次），不重发；出现别的 ID 或多个命中才算不符、要停止。skill 自己的作品页读回才是权威结果。

## 抖音编辑器填了一半、从未发送

条件：无任何发送痕迹，阶段停在 fields / topic_N_*（未到声明、合集、封面、定时），回执已记录，作者确认 + 只读列表无匹配。流程：查重 → `confirm_unsubmitted` → `resume`（只读核对）→ `run`（同窗清空本任务标题/简介 → 重填 → 只发送一次 → 读回）。后台窗口用整窗焦点模拟，不切前台；断言 visible+focused 不满足即停。

## 浏览器重启后的抖音编辑页

会话恢复会改变标签 ID。续跑时若原 ID 不存在，只绑定唯一一个预览对象与记录一致的已恢复编辑页；否则拒绝，不重传。

## 抖音封面读回中断

阶段停在 cover_*_readback 且无发送痕迹：核对声明与合集已选、视频回执一致 → `resume` → `run` 只重读已保存封面（容差每边 max(2px,0.3%)），整体读回后发送一次；不重开封面、不重传。

## 抖音点击前检查失败（从未点击）

记录显示 click_error_phase=before_native_input、无 native_input_checkpointed_at、native_response_count=0、无受理/回执：证明未点击。查重无匹配 → 作者确认 → `resume` → `run`（同窗整体读回 → 滚动到发布按钮 → 只点一次 → 读回）。

## 抖音多行简介读回不符（「*」行首）

根因：fill() 写入字面换行 → 提交时 "\n*"。已修（write_text 用原生 Enter）。已发布作品修复：作品管理 → 编辑作品 → 键盘全选删除简介 → 用 skill 的 fill_description 重填 → 拦截 /web/api/media/update/desc/ 请求，仅当 caption 无「*」且与原文一致才放行，否则中止 → 提交修改 → 读回 → `resume` 转 published。修改已发布作品需作者授权，一次一集、间隔不均匀。

## 抖音「原创」声明

网页端自主声明单选列表中没有「原创」；不选、不臆造。AI 声明按事实选「内容由AI生成」。

## X 起锚页不唯一
报「平台页面不是唯一」且没有尝试记录（任务仍 prepared）时，说明在接触 X 前就停了，可在间隔后重跑。起锚页现已允许多个同上下文 /home 标签；不要去关别人的标签。

## 抖音封面保存后读回「未改变或发生额外裁切」（cover_*_readback）

2026-11-01 S4E02：封面弹窗保存后，读回在预览图仍是保存前的图片时就开始核对，于是报错；当时还没有点发布。已修：读回会等预览 src 与保存前不同、并且已加载后，再按 within() 容差核对尺寸；超时报「抖音封面保存后预览未更新」。
恢复：先只读查看原窗口里的封面（竖封面 3:4 约 1078×1440，在容差内）和作品列表（没有该集）。然后按作者授权依次执行：`confirm_unsubmitted.py`（不加 --release，记录原话、时间和列表核对）→ `resume`（应为 prepared）→ `run`。run 只重读已保存的封面（readback_only），不重传视频、不重开封面弹窗，再按 SANSHENG_DOUYIN_DWELL 停留后点一次发布。


## 微博视频：选片后执行器退出、未填写未发送（2026-11-01 S4E07）

根因：微博把视频分块分流到区域上传主机（upbj.video.weibocdn.com），旧捕获只认 up.video.weibocdn.com，分块回执缺失 → 云端回执判不完整 → 停在 `selected_once`。已修主机白名单。
遗留尝试处理：只读核对原窗口（仍在 /upload/channel、标题为空）和主页（无该集）→ `confirm_unsubmitted.py --state-dir … --key … --statement … --confirmed-at … --listing …` → `resume`（not_submitted，账本回 prepared）→ `run`（关闭原窗口、归档 `.released-*`、新上传一次）。有任何填写（native_video/cover/字段）或发送痕迹一律拒绝。

## 抖音后台标签超时（upload / fields 阶段 TimeoutError，未点发布）

S4E03 是播放器就绪等待超时，S4E08 是账号核对的临时首页标签超时。两次的根因相同：后台标签没有持有焦点模拟，被 Chrome 节流。已修：上传前先 ensure 焦点，就绪等待分段并在段间重新确认焦点（wait_player_ready）；账号核对的临时标签也先 hold，等待放宽到 30 秒。
恢复：先只读核对 submission_attempted_at 为空、作品列表里没有该集。若原生上传回执完整（vid、大小、宽高、时长与源一致），执行 `confirm_unsubmitted.py`（不加 --release）→ `resume`（应为 prepared）→ `run`，在原窗口续填，不重传。只有作者选择释放时才加 --release，并且只关闭该集自己的旧编辑标签。

## 视频号：未知提交无作品 ID（2026-11-02，S3E08）

条件：账本 `submission_unknown`、无远端 ID；执行标记或补完记录显示曾经点击发表（有原生 post_create 成功回执），或需要证明从未发表。
1. 只读打开视频号助手作品管理列表，按封存标题找候选；同标题多条或不同 ID 即停止，不猜。
2. 已发出：`python3 scripts/publish.py --profile <profile> recover-wechat_channels KEY --remote-id export/<作品ID>`。verify 接力重新读取列表：账号、作品 ID、描述全文、短标题、合集 ID、原创、原生媒体 MD5 与封存原片一致，再取原生分享链接与 3:4 封面比对，回 `published` + `recovery_verified=true` + `source_md5_match=true`。普通 `resume-job` 不能绑定新 ID（会被拒绝）。
3. 未发出：列表无匹配且标记 `submit_clicked=false` 时按作者确认路径（`confirm_unsubmitted.py`）处理；空列表或未加载完不算证据。
4. 账本外补完（作者授权在原标签补完并点发表）也走第 2 步，只读认领，不重发。

## X / 微博入口页丢失

同一专用 Chrome 上其他执行器可能关掉 x.com/home 或 weibo.com 首页标签。发布与读回前先 `select_page`；失败时 `browser_runtime.home_anchor` 以 `background=true, focus=false` 新开入口页（不前台），URL 不符拒绝。仍失败才停平台并记录。

## YouTube 上传后读回 pending

上传后 videos.list 空 items 或处理中元数据未落定，适配器返回 pending 而不是失败；稍后 `resume` 再读回。处理完成后字段仍不符才是真错误；频道不符直接报错。不要因 pending 重发。

## 抖音「后台窗口未进入可见/聚焦模拟」（2026-11-03 S5E02，upload 阶段、未选文件）

根因：`douyin_focus.ensure` 只检查一次，且按 id(page) 缓存的 CDP 会话可能建于跨域导航/渲染进程切换之前，已失效却不会重建，于是直接停。已修：与 X `ensure_focus` 相同，校验失败即 detach 并重建会话、等待后重试（默认 4 次，有界、逐次记录），从不前置窗口。恢复：尝试记录为 `attempted/phase=upload/video=None`、上传事件为空、无发送字段时，按「抖音首次上传前尝试」走作者确认 → `resume`（prepared）→ `run`。

## 视频号：接力从未回应的结果未知任务（作者确认释放，2026-11-04 S5E06）

现象：`run` 起执行接力后，driver 在打开发表页时断网（ERR_INTERNET_DISCONNECTED），接力超时关闭（`status=closed, action=execute`），账本停在 `submission_unknown`、无远端 ID。普通 `resume` 只能走 `recover-wechat_channels` 认领候选作品，无法证明未提交。
条件全部满足才释放：接力目录只有 `task.json`/`lease.lock`（无回应、无回执）；请求目录除 `.json/.social.json/.agent` 外无痕迹；账本事件只有空 `submission_unknown`；同账号只读作品列表 `matches=[]`；作者书面确认（原话、带时区时间）。
步骤：只读列表核对 → `python3 scripts/channels_unsubmitted.py --state-dir … --key … --identity <视频号ID> --statement … --confirmed-at … --listing …` → `publish.py resume`（记 not_submitted，账本回 prepared）→ `run` 正常执行一次。确认后接力记录再有任何变化即拒绝。
