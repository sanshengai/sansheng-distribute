# 支付宝生活号网页评论接入

适用于已授权的专用浏览器。Agent 读取评论管理的可见界面，再由本地脚本校验、导入统一收件箱；脚本本身不操作浏览器、不联网、不发送。单账号已实测根评论在刷新后保持相同 DOM 标识，已有本人回复可以随原对话显示；不据此承诺官方 API 或长期稳定性。

## 读取步骤

1. 按[浏览器执行规则](browser-execution.md)确认专用实例和当前生活号。登录过渡、账号选择或空表仍在加载时不能判为无评论。
2. 在评论管理选择“按评论”，限定当前可见的 `[role="tabpanel"][aria-hidden="false"]`。隐藏的“按内容”面板也可能保留相同 DOM，跨面板读取会重复。
3. 展开每条原评的已有回复，记录原文、作者、页面时间和明确父子关系。同作品的另一条本人留言不能当作对该网友的回复。
4. 从原评的 `id="commentContent_<32位十六进制>"` 取得完整 DOM 标识；从可见作品选择器的 `data-row-key` 取得作品标识及完整标题，按唯一标题匹配。标题重名或对不上就停止，不猜作品。
5. 保存当前可见的根评论和子回复，再重新加载页面，独立读取根评论的标识及原文作 `id_recheck`。未展开、分页不足或列表仍在加载不能写成“没有回复/全账号已查完”。每次只接入已实际覆盖的范围。

这些是网页标识，不是已确认的 API ID。保留 `commentContent_` 前缀，禁止用本地文字摘要替代。当前年从带时区的观察时间推断，仅接受最近180天的页面时间；更早或年份不确定的记录改走人工核对。

## 私有采集文件

文件0600、父目录0700；不含 Cookie、令牌、浏览器内部状态。字段严格匹配，示例中的标识必须替换为页面实际值：

```json
{
  "url": "https://c.alipay.com/page/content-creation/comment?appId=<生活号ID>",
  "account_name": "当前生活号名称",
  "observed_at": "<UTC ISO时间>",
  "works": [{"key": "<作品选择器data-row-key>", "title": "完整作品标题"}],
  "id_recheck": [{"id": "<根评论完整DOM标识>", "text": "原评全文"}],
  "items": [{
    "dom_id": "<根评论完整DOM标识>",
    "body": "原评全文",
    "author_name": "网友名称",
    "time_text": "10-29 08:30",
    "is_own": false,
    "work_title": "完整作品标题",
    "replies_expanded": true,
    "replies": [{
      "dom_id": "<子回复完整DOM标识>",
      "body": "已有回复全文",
      "author_name": "当前生活号名称",
      "time_text": "10-29 08:31",
      "is_own": true
    }]
  }]
}
```

```sh
python3 scripts/interaction_alipay_capture.py \
  --state-dir /path/to/private-state \
  --input /path/to/capture.private.json \
  --account owner --channel '<生活号ID>' --account-name '当前生活号名称'
```

页面URL、生活号ID、名称、刷新后的根评论标识及原文、作品唯一匹配必须全部通过。空输入会拒绝，不能借此记录同步成功。脚本重复导入按原标识去重；有冲突沿现有收件箱校验拒绝。

## 审核与发送

导入后使用[统一互动审核页](interaction-review.md)。显示作品标题及网页读取时间；在原对话观察到本人回复的根评论标为“网页已有本人回复”，不进入新草稿队列。名称比对只用于整理本人留言，并非经过平台认证的作者身份或 API 回执。

其余留言可准备本地草稿、局部修改和批量确认，再点“交给 Agent 发送”。待办保存在本机，不需要向聊天窗口另传确认文件；当前 Agent 会话负责在专用浏览器执行，离线时不会自动发出。确认、提交待办、平台发送、网页核对是不同状态，不能记为 API 已核验。

Agent 先读取待办，核对当前账号、作品、原评及已有本人回复。填入封存文案后、最终点击平台回复前领取该条任务；领取会冻结同一原评的其他发送批次。点击后刷新原对话、展开回复并重新采集，使用真实子回复标识完成核对。作品映射可复用已有实际选择器记录，须明确记录其观察时间；原评和新回复必须重新读取。不要通过终端、未公开接口或注入请求代替浏览器点击。

```sh
python3 scripts/interaction_review_handoff.py --state-dir /path/to/private-state list
python3 scripts/interaction_review_handoff.py --state-dir /path/to/private-state claim --batch '<确认批次>' --draft '<草稿标识>'
# 浏览器发送后重新采集，并运行 interaction_alipay_capture.py 接入读回证据
python3 scripts/interaction_review_handoff.py --state-dir /path/to/private-state verify --batch '<确认批次>' --draft '<草稿标识>' --reply-id '<真实子回复DOM标识>'
# 点击中断或结果不明：冻结原尝试，重新读回，不再点击发送
python3 scripts/interaction_review_handoff.py --state-dir /path/to/private-state unknown --batch '<确认批次>' --draft '<草稿标识>'
```

上述CLI只处理本地待办和证据，不联网、不操作浏览器。`verify`要求确认后领取的原尝试，以及发送后观察到的本人回复、原评归属和逐字一致的正文；旧页面记录、其他留言下的同文案或结果不明都不能当作成功。当前没有审核页自主驱动浏览器或24小时采集器。

`tests/test_interaction_alipay_capture.py` 验证真实导入路径、去重、已有对话、审核状态和人工路线，及账号、原文、作品、空输入和重复面板的拒绝情况。测试通过不代表平台权限或界面永不改变。
