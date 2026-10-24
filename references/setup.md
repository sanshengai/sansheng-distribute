# 安装与私有配置

本体需要 macOS 或 Linux、Python 3.10+ 和 Agent 的本机命令工具。核心采用标准库；不需要 作者工作区、作者的目录结构或 Write，就能校验交接、管理任务和续跑。浏览器发布需要当前 Agent 已获准的浏览器工具；公众号 API 草稿额外复用 `sansheng-write/scripts/wechat_api.py`。

所有命令中的 `<skill-root>` 是当前 `SKILL.md` 所在真实目录。不要把用户工程当前目录当成 Skill 根。

```sh
python3 '<skill-root>/setup.py' --show-profile
python3 '<skill-root>/setup.py'
python3 '<skill-root>/setup.py' --doctor
```

交互引导逐个平台说明用途、依赖并询问是否配置，只向已选模块询问信息。非 TTY 且未给 `--init` 时输出需要提供的项目并退出；已有配置则直接诊断，不重复询问、不覆盖。诊断仅核对本地文件、运行环境、账号指纹；不会登录、请求 token、上传或发布，也不能证明平台实测通过。

## 无人值守初始化

不选平台时只建立核心配置，不检查或告警未选模块：

```sh
python3 '<skill-root>/setup.py' --init
```

选择一个可选模块时，例如公众号：

```sh
python3 '<skill-root>/setup.py' --init \
  --profile '/private/directory/profile.json' \
  --platform wechat --account editorial \
  --write-scripts '/installed/sansheng-write/scripts' \
  --wechat-env-file '/private/directory/wechat.env' \
  --wechat-appid-sha256 '<confirmed-appid-sha256>'
```

AppSecret 只放环境变量或私有 env 文件，不传命令行参数、不存 profile。微信读取 `WECHAT_APP_ID`、`WECHAT_APP_SECRET`；指定 env 文件优先于进程环境变量。profile 仅存确认账号的 AppID SHA-256 指纹。新 profile 使用文件权限 `0600`，初始化后回读核对；已有文件一律拒绝覆盖，也不复制或迁移账本。

`--platform` 可重复指定 `wechat`、`x`、`weibo`、`xiaohongshu`、`douyin`、`bilibili`、`tencent_video`、`wechat_channels`。选择只登记 `configured_modules`，用于本地配置与诊断；所有 `enabled_capabilities` 仍为空，`acceptance_runs` 也为空。它不是发布授权，也不把未验收的平台变成可用能力。真实草稿/预填试验仍遵守[验收方法](acceptance.md)。

社交模块还需要专用浏览器目录与已确认的公开账号身份。例如首次配置 X：

```sh
python3 '<skill-root>/setup.py' --init --platform x \
  --browser-profile '/private/directory/dedicated-browser' \
  --public-identity 'x=@your_handle'
```

公开身份存为 `accounts.<alias>.public_identities.<platform>`，浏览器目录存为 `browser_profile`。引导不会创建浏览器目录、登录或读取 cookie。缺失这些项时仍可建立待配置文件，但 `configuration_ready=false`，诊断返回未就绪；补齐同一文件后再跑 `--doctor`。本地路径和公开身份齐备也不证明实际页面已登录或账号匹配，执行时仍要重新观察。

缺少微信依赖时，引导先发现本体相邻的 `sansheng-write` 或四家 Agent 的既有 Skill 入口；显式 `--write-scripts` 指向缺失模块时直接拒绝，不悄悄换成另一个目录。未安装则按 `sansheng-write` 的公开安装说明安装后，显式填写实际 `scripts` 路径。未选公众号时完全不检查 Write。

## 路径与原账本

配置路径按以下顺序：

1. 命令行 `--profile`。
2. `SANSHENG_DISTRIBUTE_PROFILE`。
3. 兼容变量 `SANDY_PUBLISH_PROFILE`。
4. 现存的 `~/.config/sandy-publish/profile.json`，否则新默认 `~/.config/sansheng-distribute/profile.json`。

状态路径按以下顺序：`--state-dir` → `SANSHENG_DISTRIBUTE_STATE` → `SANDY_PUBLISH_STATE` → 选中 profile 的 `state_dir` → 现存旧默认 `~/.local/state/sandy-publish`，否则新默认 `~/.local/state/sansheng-distribute`。已有 profile 未声明 `state_dir` 且两个状态目录均不存在时保留旧默认，避免更名改变既有配置含义。新建 profile 会显式记录选中的状态目录。

两个默认 profile 同时存在，必须显式指定配置；两个默认状态目录同时存在而未明确状态来源时，必须显式指定原账本。不能依据文件时间、目录大小或内容多少自动挑一个，也不能因改名新建空库。显式路径和环境变量是操作者的选择；setup 不替操作者迁移任务。

profile 内的相对路径以 profile 所在目录解析，包括 `state_dir`、`wechat_env_file`、适配器脚本以及 `--profile` / `--write-scripts` 参数。`${SKILL_ROOT}` 表示本体真实根目录，新建适配器使用该标记，移动本体后仍可解析。CLI 与环境变量中的相对路径按调用方当前目录解析，自动化建议传绝对路径。参数以 argv 数组存储，含空格路径不需要额外塞 shell 引号。

`--show-profile` 仅输出实际路径及来源，不输出账号配置或秘密。`--doctor` 仅检查 `configured_modules`、既有启用能力或精确验收条目涉及的平台，以及核心环境；不会遍历未选模块。

## 四家 Agent

使用各 Agent 官方 Skill 安装方式，将同一个本体注册到其 Skill 入口；重新启动会话后验证能发现 `sansheng-distribute`。这里不代替各家安装器，也不擅自创建或重写软链接。本机已有受管入口时继续使用共享同步流程，不能因为安装说明又复制一份同名本体。能发现 Skill 不代表已完成该 Agent 的浏览器执行验收。
