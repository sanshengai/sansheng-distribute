# 来源与许可

`scripts/wechat_api.py` 与 `scripts/wechat_tunnel.py` 改编自 [sansheng-write](https://github.com/sanshengai/sansheng-write)，MIT。完整原始版权声明与适用许可全文均保留在仓根 LICENSE。固定提交、文件哈希和改动说明见 `references/vendor-lock.json`；这些模块随本包发行，不要求另装 Write。

B站可选工具由统一入口下载 [biliup v1.3.4](https://github.com/biliup/biliup/tree/v1.3.4)，上游 MIT 许可见 [LICENSE](https://github.com/biliup/biliup/blob/v1.3.4/LICENSE)。本仓不捆绑其二进制或源码；安装来源、版本和下载摘要固定在 `scripts/biliup_bridge.py`，工具摘要另存私有安装回执。`scripts/bilibili_adapter.py` 为本包实现，读取该固定版的受理输出并独立核验稿件。它使用非官方网页协议，不等同于B站机构开放接口。

源码研究另参考宝玉、SAU、Social Publish MCP、OmniPost（各审阅仓库 MIT）及小红书 MCP（Apache-2.0）。没有复制或捆绑这些项目的可执行文件、服务或上传器；固定提交、审阅文件摘要与采纳边界见 [sources.json](references/sources.json) 和 [upstream-decisions.md](references/upstream-decisions.md)。这些研究来源不会增加用户需要安装的 Skill 或服务。
