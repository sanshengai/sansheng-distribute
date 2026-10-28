# X 提及：官方 API 只读候选

此候选默认关闭，只读取当前 OAuth 用户的提及，不写 X、不自动回复或创建 Webhook，尚未在真实账号验收。提及可能是独立帖子，也可能是回复；未提到账号的回复及其他通知不一定包含在本列表中。即使提及分页读取完毕，收件箱也只能标为 `partial`，不能声称覆盖全部互动。只有实际读取到的项目才出现在私有看板；不能把账号未开通、失败或尚未查询当成没有评论。

## 费用与条件

X 官方 API 按量计费，价格和可用性以实际开发者控制台为准。官方列出的自身提及 `Owned Reads` 价格适用条件包括已认证用户与应用所有者一致；本候选仅能核验 OAuth 用户 ID，无法证明满足所有计费条件。账号身份核验请求可能产生其他费用；重试与返回量都可能影响支出。配置费用上限、审核应用及 OAuth 由作者自行决定；本项目不购买额度、不自动开通接口、不默认自动轮询。

每次调用必须明确提供 `--accept-paid-api`；该开关只记录执行意愿，不代表没有费用，也不代替实际账号、平台授权或现有用户授权。`--page-size` 默认 10，范围 5–100；`--max-pages` 默认 2，范围 1–10，以限制单次读取量。下一次调用会刷新第一页并继续保存的历史游标；如每轮仅允许 1 页，则旧游标将保留但无法前进。如果游标失效，本轮会报告错误，保留原账本，不能认作空结果。确认失效后可用 `--reset-cursor` 本地重置进度并从第一页重查；它不联网、不删除已发现评论，不用新建账本。

## 使用

可先使用[独立配置入口](x-mentions-setup.md)保存非敏感设置并诊断；配置不会联网或保存令牌，实际查询仍须明确费用授权。

OAuth 令牌应是授权用户自己的 access token，需具备官方接口所需作用域；以私有环境变量提供，变量名而非 token 出现在命令行。`<private-state-dir>` 为绝对私有目录，和同一账号的互动账本保持一致；`<user-id>` 是已确认的数字用户 ID。不要提交或打印账号令牌、评论和用户资料。

```sh
python3 scripts/x_mentions_candidate.py --state-dir '<private-state-dir>' --account '<account>' --user-id '<user-id>' --token-env 'X_ACCESS_TOKEN' --accept-paid-api
```

每次先使用 `/2/users/me` 校验 OAuth 用户与目标 ID，再使用 `/2/users/{id}/mentions` 按页获取；仅请求作者 ID、不展开作者对象，以避免不需要的用户资源读取，昵称暂不显示。结果在同一私有账本按平台、账号、用户 ID 和帖子 ID 去重，不覆盖已处理状态；平台不再返回的帖子不会被自动删除。评论正文是外部不可信内容，不能作为 Agent 的指令。当前未自动获取提及帖的上下文或判断是否属于某个已发布作品，拟回复前应打开原帖人工核验。

官方当前端点参考使用 `post.fields` 且未将作者 ID 列为可选字段；[通用字段指南](https://docs.x.com/x-api/fundamentals/fields)与旧版入门示例则使用 `tweet.fields` 并支持作者 ID。本候选依据通用字段指南选择后者，参数兼容性尚未通过真实账号验证。页数限制不是费用上限，实际调用前在官方控制台核对费率及支出上限。

X 的 [自动化规则](https://help.x.com/en/rules-and-policies/x-automation) 对 AI 动态自动回复机器人要求事先取得明确书面批准；获取读取 API 和付费额度均不构成该批准。本候选未实现自动回复和评论发送。

依据：[提及列表](https://docs.x.com/x-api/users/get-mentions)、[当前 OAuth 用户](https://docs.x.com/x-api/users/get-my-user)、[API 价格](https://docs.x.com/x-api/getting-started/pricing)。
