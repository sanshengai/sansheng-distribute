# 安装与私有配置

本体需要 macOS 或 Linux、Python 3.10+ 和 Agent 的本机命令工具。核心采用标准库；不需要 作者工作区、作者的目录结构或 Write，就能校验交接、管理任务和续跑。浏览器发布需要当前 Agent 已获准的浏览器工具；公众号 API 草稿使用包内 `scripts/wechat_api.py`，不需要安装 Write。

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

普通浏览器路线仍使用 Agent 接力。需要试验包内 X 文字/多图、微博图文或抖音视频预填时，显式增加对应的 `--browser-engine x|weibo|douyin` 和 `--browser-cdp-url <实际本机端点>`；依赖准备、输入边界与临时编辑会话读回见 [内置引擎](browser-engines.md)。不会自动启用媒体或发送。

## 选用 B站受控 CLI

Apple Silicon macOS 可在首次初始化选择包内适配器；不需要浏览器目录或其他发布 Skill：

```sh
python3 '<skill-root>/setup.py' --init --platform bilibili --bilibili-cli \
  --profile '/private/directory/profile.json' --public-identity 'bilibili=123456' \
  --bilibili-tid 171 \
  --bilibili-tool-dir '/private/tools/biliup-1.3.4' \
  --bilibili-credentials '/private/auth/bilibili.json'
python3 '<skill-root>/setup.py' --profile '/private/directory/profile.json' --prepare-tools
```

UID与分区均须实查，示例不是推荐取值。工具和凭证路径可省略，分别使用本包私有数据与配置目录。初始化只写配置，不安装、登录或发布；缺少工具/登录时诊断未就绪是预期结果。`--prepare-tools` 只为已选CLI路线安装固定工具，有效既有工具只核对，不覆盖。未选路线时不下载。

随后由作者扫码登录一次：

```sh
python3 '<skill-root>/scripts/biliup_bridge.py' login \
  --directory '/private/tools/biliup-1.3.4' --credentials '/private/auth/bilibili.json'
python3 '<skill-root>/setup.py' --profile '/private/directory/profile.json' --doctor
```

已有登录文件不覆盖。执行时重新核对实际UID；`ffprobe`须在本机可调用，用于读回时长。初始化保持 `enabled_capabilities=[]`，新适配器只有离线合同和真实只读证据，首次上传仍按精确验收授权执行。当前只支持自制单视频、可选封面、标题/简介/标签与AI声明；合集、活动、字幕等不静默省略，见[执行边界](efficient-dispatch.md)。浏览器路线原有配置不自动改写。

新建公众号配置默认使用包内模块，不扫描相邻 Write 或其他 Agent 的入口。旧配置显式 `--write-scripts` 继续使用原模块；指定路径缺失时拒绝，不悄悄改用内置模块。未选公众号不检查其凭证或 API 模块。

## 可选浏览器运行环境

选择 X、微博、抖音、视频号或小红书后，可用同一个入口准备固定版浏览器依赖：

```sh
python3 '<skill-root>/setup.py' --profile '/private/directory/profile.json' --prepare-runtime
python3 '<skill-root>/setup.py' --profile '/private/directory/profile.json' --runtime-status
python3 '<skill-root>/setup.py' --profile '/private/directory/profile.json' --browser-status
```

依赖装到私有独立环境；不修改系统 Python，不下载另一份 Chrome，不安装其他发布 Skill。只选核心、公众号或 B站时不安装这组依赖。版本与下载哈希见 `browser-requirements.lock`；已有有效环境复用，已有异常环境保留并拒绝覆盖。需要不同位置时，新安装使用 `--init --runtime-dir`，已有配置先在同一私有 profile 中设置 `runtime_dir`，再准备依赖。安装命令不悄悄改写配置；与 profile 不一致的安装位置会在创建环境前拒绝。

专用 Chrome 已运行后，在 profile 中配置 `browser_runtime.cdp_url`，例如 `http://127.0.0.1:9333`，并保留与该实例一致的绝对路径 `browser_profile`。旧 `browser_prefill.cdp_url` 兼容。端口从实际专用实例核对，不从陈旧端口文件或日常浏览器猜测。

`--browser-status` 仅连接本机明确端点，核对浏览器实际启动参数与 profile，并统计各平台页面数量；不导航、不登录、不上传。返回成功仅证明连接与实例正确，不证明账号登录或发布权限。多个平台共用 profile 时写入串行；只读检查不关闭或重启 Chrome。预填配方继续遵守当前 Agent 的浏览器工具政策。

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

## 公众号内置模块与白名单出口

新模块读取 `SANSHENG_DISTRIBUTE_CACHE_DIR`、`SANSHENG_DISTRIBUTE_WECHAT_API_BASE`、`SANSHENG_DISTRIBUTE_WECHAT_TUNNEL` 和 `SANSHENG_DISTRIBUTE_WECHAT_TUNNEL_KEY`；原有同名 `SANSHENG_WRITE_*` 变量继续作为后备。默认 token 缓存在私有 `~/.cache/sansheng-distribute/`，不写进 Skill。

本机出口不在公众号白名单时，可使用明确配置的固定白名单出口，配置在 `~/.config/sansheng-distribute/wechat-tunnel.json`；旧 `~/.config/sansheng-write/wechat-tunnel.json` 兼容。格式为 `{"host":"your-host","user":"publisher","port":22,"key":"/private/key"}`，不含公众号秘密。`always` 读取此配置强制使用出口，`off` 只直连；首次 SSH 主机信任须由用户正常建立。没有配置时明确报错，不要求安装 Write。

包内模块已通过隔离单包、本地 HTTP 建稿与独立读回验证；这项测试不等于新增真实账号的音视频播放器插入或正式发表验收。
