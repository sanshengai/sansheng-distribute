# 微博原生评论观察接入

这是本地校验与导入路线，不是官方 API 客户端，也不是定时采集器。2026-10-29 在单账号专用 Chrome 的“收到的评论”页观察到原生 JSON 响应，两个历史样本完成身份、父级、去重及历史基线入库实测；未验收自动发现、翻页全覆盖或统一队列发送。

同日发现[微博官方助手只读试验](weibo-official-probe.md)的新评论契约，待凭据和真账号验收。原生页面观察可用于交叉核对它的范围，不因官方包新增功能就绕过验收。

## 获取与身份

只使用当前 Agent 获准的专用浏览器。进入原生消息页，通过已有浏览器调试能力观察页面自身加载的 `https://weibo.com/ajax/message/cmt` 响应；不导出 Cookie，不抓取其他站点或账号。需要登录、验证码或工具拒绝时保留阻塞，不切换日常 Chrome 或其他技术绕过。

先核对页面 UID 与私有发布配置，再保存最小观察文件 `weibo-comment-capture.v1`。账号别名和 UID 不能仅从观察文件自行认领。原始响应 `ok=1` 和列表可读只证明本次请求成功。

| 观察字段 | 本次原生响应中的来源 |
| --- | --- |
| `id` | 当前评论 `idstr` |
| `root_id` | 当前评论 `rootidstr` |
| `parent_id` | 子评论的 `reply_comment.idstr`；根评论为 `null` |
| `work_id` / `work_author` | `status.idstr` / `status.user.idstr` |
| `author_id` / `author_name` | 当前评论 `user.idstr` / 昵称 |
| `text_html` / `created_at` | 当前评论 `text` / `created_at` |
| `restrict_state` / `disable_reply` / `cut_tail` | 原值保留，不用缺省值代替缺失字段 |
| `parent` | 嵌套父级的 ID、根 ID、作者、时间及原文；没有为 `null` |
| `total` / `next_cursor` / `missing` | `data.total_number` / `data.next_cursor` 的字符串值 / `data.miss_count` |

`source_url` 为上述已观察的固定来源，`observed_at` 为本次观察的带时区 ISO 时间，`account`、`uid` 为已核对的账号。ID 始终保留十进制字符串。网页日期链接中的 `rid` 可能是直接父评论；JSON 自身的 `rid` 也不是评论 ID。两者均不得当作当前评论的发送目标。字段缺失或关系不清时停止转换，不猜测。

观察文件只含收件评论及必要父级，不保存身份凭据、完整账号配置或整篇作品。作品 `status.text` 可能只是长文预览，不当作完整作品摘要。

## 本地接入

私有配置示例（放在公开 Skill 仓外，权限 `0600`）：

```json
{
  "schema": "weibo-comment-config.v1",
  "account": "已配置的账号别名",
  "uid": "原配置及页面确认的UID",
  "not_before": "2026-10-29T00:00:00+08:00"
}
```

```sh
python3 scripts/weibo_comments_capture.py \
  --state-dir '<原私有收件箱目录>' \
  --config '<私有配置绝对路径.json>' \
  --capture '<本次最小观察绝对路径.json>'
```

脚本核对来源、账号及本人作品、当前 ID/根 ID/直接父级、重复和环、时间、HTML 正文与限制标志。受限、禁止回复、正文截断、空输入或身份冲突均在入库前拒绝，保留原观察交人工。相同 ID 的作者、作品、父级、正文或时间变化拒绝整批覆盖。

首次入库时，早于 `not_before` 的他人评论记录为历史基线：不自动拟稿、不计已回复。重复导入不改变既有处理状态，也不会回头把已有待办批量标成历史。确需重新处理旧留言，由作者明确选择；不能靠移动截止时间抹掉待办。

来源保持 `manual`，没有平台成功同步时间。即使游标为零、记录数等于声明总数，也不宣称全量查完。周期拟稿要求近期完整同步，因此不能凭本次导入开启自动处理。

嵌套父级是上下文证据，其自身的父级未必已返回，不造一条假根评论入库。完整父级信息保留在私有观察文件；上下文整理提示 `direct_parent_missing` 时，Agent 查原观察再拟稿，不直接进入自动发送。

回执只给导入数、原观察摘要哈希、游标及覆盖边界，不包含正文，不授权或发送。看板继续区分历史基线、待处理与已核验回复。
