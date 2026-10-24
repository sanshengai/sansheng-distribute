# 公众号原生媒体

优先验证公开 API；不支持时 API 建稿后由 Agent 操作编辑器。2026-10-24 已在一个真实账号完成测试草稿的混合链验收；不外推为所有账号或公开发布已通过。

当前 `scripts/wechat_adapter.py` 实现 API 草稿建立、独立读回和基于实际观察回执的原生媒体核对；公众号模块可选依赖 Write 的 `wechat_api.py`。凭证以 AppID 指纹绑定明确账号。带媒体槽的稿件先返回 `pending / native_media_insertion_required`，由 `advance` 交给 Agent 在编辑器补齐，再通过 API 核对位置与内容。发表与群发适配尚未完成。

API 列表成功但为空，只能证明该请求连通；不能据此推断没有草稿、已获得播放器样本或具有发表权限。

1. Write 输出最终插槽、素材哈希、正文版本。发布端验证后上传正文图片/封面并建稿。
2. 取得草稿 ID 立即记录，读回失败只重试读回；补媒体失败不能重新建整篇。
3. 读回检查唯一占位；编辑器核对账号、草稿和上下文，上传/选媒体、等待转码、插入原生播放器。
4. 成功后仅删对应占位，不整篇粘贴。保存后核验播放器身份/位置、其他内容和视觉排版。
5. 播放器存在、加载、播放内容分别验收；不声称已核对未取得的远端字节。
6. 按授权留草稿或发表；发表与群发分开。最终文章再读回，受理/审核中不报完成。

普通 media_id 不能代替 voice_encode_fileid/视频播放器标识。播放器身份必须来自该账号的真实上传与读回，不猜 ID；能力按公开契约及最小测试草稿验证。

### 外链被平台过滤

2026-10-24 真实测试中，`draft/add` 接收成功，但 `draft/get` 的正文仅保留外链标题，`a href` 被去掉。严格读回因此拒绝成功，不能把链接丢失当作无害排版差异。此为该账号的实测，不推断所有公众号都无外链权限。

账号 profile 可显式配置 `external_link_policy: "visible_url"`：提交前把外部 HTTP(S) 链接变为“原文字（完整网址）”，保留样式内文和网址，记录转换；微信文章链接保持原样。默认 `strict`，不自动删除链接。Write 应在生产端先采用同样的可见网址格式再导出，现有 Write 回导仍严格按原稿核对，不会悄悄接受新增文字。

对于已经建好的历史测试稿，若**唯一差异是外部锚标签被去掉**，此策略返回 `external_link_repair_required`，`advance` 只补这些确切网址，不重建、不整篇覆盖。正文、图片、元数据或网址的其他差异仍拒绝；补完后重新官方读回。原预期正文和执行阶段证据保留，缺网址的稿件始终不能报告成功。

分别验证短媒体上传、原生组件保留、长音频/视频插入、最终发表。保存重新打开及播放、位置都正确才启用 API 路由。已有组件保留成功不证明新素材能自动产生播放器。

媒体插入后不使用旧 HTML 覆盖草稿。作者改正文导致版本失配时重新装配，不按旧坐标继续。

## 补媒体回执

运行 `advance` 后按 [浏览器接力](browser-execution.md) 操作。插入并保存、重新打开后，调用 `wechat_adapter.py inspect-media --request ... --profile ... --write-scripts ...`，它只读官方草稿，将真实 HTML 存入私有 `.native-snapshot.json`，输出已观察组件的哈希和身份属性。不能按样例构造组件/ID。

将每个实际组件与上传的源文件关联，写请求旁的 `<key>.native-media.json`，包含 manifest_digest、remote_id 和 components 数组。每项：slot_id、kind、asset_sha256、remote_id_attribute、remote_media_id、component_sha256、upload_evidence，以及 saved / reopened / loaded / played 四项实际观察结果。组件指纹、身份取刚刚的官方读回；源哈希取已验过的素材。上传证据记录实际文件选择及身份对应，不能仅凭同名或封面认定。

每插入并保存一个组件，就原子保存该槽的映射。部分槽已完成时，verifier 比对已插组件和剩余占位，返回 `media_work`：insert 只补剩余槽，verify_existing_player 只核验已有播放器，不能重复插入。若中断发生在保存后、回执前，先只读 inspect-media 并在编辑器确认源素材对应，补部分回执后再 advance；不根据位置猜媒体身份。

接力回应后 API verifier 会重新读取正文，核对组件身份、实际音频/视频类型和数量，将组件还原成对应 slot 标记，再比对全文、图片、链接的顺序。未知媒体标签不猜解析，先补真实样本适配。回执中的播放观察由 Agent 负责真实性；代码只校验其绑定和完整性，不声称自动证明听到的内容或远端文件字节一致。

纯图文和带媒体稿都另需整体视觉检查。媒体回执中增加 `layout_review: {reviewed:true,content_sha256:"inspect-media 返回的实际正文指纹",evidence:[{path:"实际观察记录的绝对路径",sha256:"文件哈希"}]}`。观察须覆盖手机预览、标题层级、图片/卡片及媒体前后排版。缺该检查返回 visual_review_required；正文改变后旧视觉回执失效。代码的文字等价不代替审美和布局验收。

已有内置 Write 草稿不可直接再 run 新建。Write 的 `export-publish --existing-draft <实际ID>` 生成显式迁移包后，先 `prepare`，再 `adopt <manifest> --target <id>`。认领只读官方草稿，不需要新增发送授权；核验失败保留同一 ID 和恢复状态，不重新建稿。同账号同一草稿不能被另一交接任务认领。后续 `advance` 仍要求绑定当前版本的实际写入授权。

迁移包绑定旧回执、当前原稿/素材、旧图片身份及预期正文。旧可见占位由 `legacy_anchor` 交给编辑器；读回器只在内存中还原成标准槽位再核对全文，不回写整篇。找不到或不唯一的旧位置、变化的正文/图片/链接先停；注释位置不迁移。已有原生组件先 inspect-media，取得源文件对应证据后补映射再核验，不能重复插入。迁移目前通过离线跨生产端/消费端测试，真实平台未验收。

## 回传 Write

```sh
python3 scripts/publish.py --profile /path/profile.json --state-dir /path/state return-write JOB_KEY --article-dir /path/article --write-scripts /path/sansheng-write/scripts
python3 /path/sansheng-write/scripts/pipeline.py --dir /path/article import-publish --receipt /path/返回的write-receipt.json
```

第一步持同一账号锁，重新调用官方读回器；账本曾为 draft_ready 也不能跳过。核对当前封存、原生媒体及视觉证据，再把组件还原为原稿位置，仅在内存中比对 Write 正文。第二步校验文件与证据哈希并更新 Write 发布阶段。旧回执、旧草稿 ID 及原稿保留；每次回传使用独立证据目录，重验不破坏已导入凭证。不要手动改名回执。

已导入外部凭证的文章再次调用 Write `release-to-draft` 只验证同一结果，不调用旧上传器。相同草稿和音频源复用原交接单，保持其哈希。正文、素材或观察证据变化会拒绝成功。回传只代表已核验草稿，不代表已正式发布、群发或完成官网归档；正式文章中视频与双音频的统一 finalize 仍待真实样本适配和验收。

官方参考：
- https://developers.weixin.qq.com/doc/service/api/material/permanent/api_addmaterial
- https://developers.weixin.qq.com/doc/service/api/draftbox/draftmanage/api_draft_add
- https://developers.weixin.qq.com/doc/service/api/public/api_freepublish_submit

## 已验证组件与排版注意

官方 `draft/get` 将音频保存为 `mp-common-mpaudio / voice_encode_fileid`，视频保存为带 `data-mpvid=wxv_…` 的官方播放器 `iframe`。识别器只接受已观察的官方域名、路径、模板、动作、ID 一致性与类型组合；普通 iframe 继续拒绝。不能用这个样本臆造新 ID。

可见网址所在段落应左对齐；微信默认两端对齐会将短标签拉散。长网址可自然换行，手机预览检查溢出与标点。修整只改对应段落，保存后重新取得实际正文哈希与视觉证据。媒体标签也可能因再次保存改变序列化，先核对身份，再刷新实际观察，不能盲沿用旧组件哈希。

播放证据为浏览器内音频/视频实际播放进度、时长、readyState 和无媒体错误，加可见预览；没有声称在手机微信客户端测试、完整听完或证明远端文件字节相同。测试稿保留草稿，未发表、未群发。真实 Write 旧稿迁移及回导、正式文章 finalize 与其他平台仍另行验收。

补充 API 实测：在同一测试草稿上，用官方 `draft/update` 写回刚读出的完整文章（含真实组件及封面参数），返回 errcode=0；随后 `draft/get` 正文逐字节相同，两个组件哈希不变，编辑器重开后音视频再次播放。只证明该账号当前既有组件可经 API 保留，不证明普通素材 ID 可直接组装播放器，也不证明本地文件全 API 上传/插入或正式发表已通过。官方契约：[更新草稿](https://developers.weixin.qq.com/doc/service/api/draftbox/draftmanage/api_draft_update.html)。
