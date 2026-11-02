# Skill 双向同步规则

## 角色

- **唯一执行副本与真源**：作者 Mac 上的 作者本机的 Skill 目录（下称真源）（`~/.claude/skills`、`~/.codex/skills` 是指向它的软链）。
- **中转**：GitHub `origin`（main）。只接受 fast-forward；禁止 force push。
- **其他克隆**（如 Grok Bot box 上的克隆）：只读镜像，不在其中编辑；需要时 `git pull --ff-only`。

## 谁在何时改

- 作者（Codex/Claude）与 Grok Bot 都只在 Mac 真源里改；改之前先 `python3 scripts/skill_sync.py status`。
- 同一时间若另一个任务正在改某组文件（如 `scripts/douyin_*`），其他任务只新增文件或改文档，不碰这些文件。
- 不 `stash/checkout/reset` 他人的未提交改动；工作区混有来源不明改动时，先列清单问作者再提交。

## 提交约定

- 小提交，一事一提交；提交信息前缀区分来源：`[write] …`（Grok Bot）、`[codex] …`（作者 Codex）、`[claude] …`。
- 作者归属：Grok Bot 提交用 `--author "Grok Bot (write) <…>"`；作者自己的提交用默认身份。
- 长期分支需要时用前缀 `write/…`、`codex/…`；合回 main 只用 fast-forward 或作者确认的合并。

## 推送前检查（skill_sync.py push 自动执行）

1. `git fetch`；本地落后或分叉即拒绝，先 `pull --ff-only` 或请作者处理。
2. 工作区必须干净（未提交改动先提交或请作者认领）。
3. 全套单元测试通过（`python3 -m pytest -q`，不开浏览器）。
4. 脱敏扫描通过：将要推送的提交与已跟踪文件中不得出现 cookie/token、`*.private.json`、私有绝对路径（`/Users/<name>/`）、账号 UID、合集/播放列表 ID、回执文件。
5. 只推 fast-forward（`git push origin HEAD:main`，无 `--force`）。

## 冲突

- 落后：`skill_sync.py pull`（仅 `--ff-only`）；失败说明分叉，不自动 rebase/merge，交作者决定。
- 两边同时改了同一文件：以真源为准；镜像丢弃本地改动后重新 pull（镜像本就不该有改动）。

## 镜像保持最新

- 镜像侧运行 `python3 scripts/skill_sync.py status --repo <镜像路径>`：报告是否落后；落后则 `skill_sync.py pull --repo <镜像路径>`。
- 真源推送后通知其他 Agent 拉取；镜像不作为执行副本使用。
