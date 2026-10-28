# X 提及候选：本地交互式配置

该配置入口仅供主动使用 X 官方提及只读候选的用户；默认不启用、不联网、不购买额度。候选尚未真实账号验收，且查询可能收费。首先阅读 [X 提及能力与费用边界](x-mentions-candidate.md)。

在 Skill 根目录执行以下命令，`<private-config>` 为 Skill 仓库外的绝对私有路径，所在目录权限应为 `0700`：

```sh
python3 scripts/x_mentions_setup.py --config '<private-config>' setup
python3 scripts/x_mentions_setup.py --config '<private-config>' doctor
```

交互中可选择不启用；启用时只询问本地账号标识、X 数字用户 ID、私有账本目录、OAuth access token 所在的环境变量名。不要输入 token 本身。工具将非敏感的变量名与账号绑定写入权限为 `0600` 的私人配置，回读验证；token 由用户另行通过现有私有凭证流程放入该进程环境，不放入配置、命令行、公开仓或输出。检查只确认环境变量有值，不能验证 OAuth 作用域、平台权限或费率。

重复运行 `setup` 会直接读取现有配置，不重问；确需更改配置时在终端使用 `setup --edit`，选择不更改会保留原配置。可用 `python3 scripts/x_mentions_setup.py --config '<private-config>' disable` 禁用，不删除凭据或账本；以后可重新运行 `setup` 启用。非交互环境不等待输入，只说明所需项目并退出。`doctor` 在未配置或禁用时保持静默，不将未启用模块判作故障。

实际查询前，Agent 必须取得用户对本次潜在费用的真实授权，并在 X 控制台核对费率和限额；仅配置本工具或传递标志不能代替授权。之后才可调用：

```sh
python3 scripts/x_mentions_setup.py --config '<private-config>' run --accept-paid-api
```

`run` 将配置中的非密钥字段交给 `x_mentions_candidate.py`，凭据只继承当前进程环境。没有费用确认标志或环境变量时不联网。此入口不创建定时任务、不自动回复、不保证单次费用上限。若不用 X 提及功能，可不配置它，主发布流程不受影响。
