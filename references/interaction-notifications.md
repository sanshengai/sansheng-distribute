# 本机互动提醒候选

收件箱是状态真源；系统通知只是可选提示。本功能默认不启用，主动运行时才检查本地 SQLite，既不联网，也不生成或发送评论。只对已有 `complete` 或 `partial` 同步记录的来源检查，不把空清单当作平台没有评论。尚无同步记录的平台不会触发提醒。

每个平台账号第一次检查只建立本地基线，不对当时已经发现的评论弹窗。之后首次发现的非本人、未处理评论，不论评论发布时间是新是旧，都计入候选；历史分页稍后发现的旧评论也会提醒。人工导入、自评论、已处理评论不提醒。条件相符的多条评论合并成一次提醒，通知内容固定为“发现新的互动，请查看收件箱”，不含平台账号、评论、作者或其他外部文字。

```sh
python3 scripts/interaction_notify.py --state-dir '<private-state-dir>'
python3 scripts/interaction_notify.py --state-dir '<private-state-dir>' --notify-macos
```

第一条检查候选但不弹通知；第二条仅在本次明确启用时向 macOS 提交通知请求。重新执行检查不会为同一条评论重复提交提醒。记录状态为 `requested` 表示系统命令已接受请求，不表示通知实际出现或用户看见；`failed` 表示命令失败，`unknown` 表示结果不明。为避免进程崩溃后重复提醒，在提交请求前先记录尝试；若进程恰在两步之间停止，实际上可能没有弹窗且不会自动重试。因此通知不是可靠送达机制，应定期查看收件箱。系统通知还可能受到 GUI 会话、通知权限、专注模式及电脑睡眠影响。

`watch-youtube` 默认不调用此检查器；用户主动传入 `--notify-macos` 时，每轮只读同步后检查一次，通知故障不停止评论同步。若连续三次全局检查失败导致轮询停止，会额外请求一次不含账号信息的停止提醒；系统是否显示同样无法确认。未安装常驻服务，系统弹窗与云端全天通知均未做真实验收。基线、去重和请求记录与评论一起留在同一个权限受限的私有状态目录，不能将数据库或通知日志提交到公开仓。

Apple 对脚本通知的说明：[Mac Automation Scripting Guide](https://developer.apple.com/library/archive/documentation/LanguagesUtilities/Conceptual/MacAutomationScriptingGuide/DisplayNotifications.html)。
