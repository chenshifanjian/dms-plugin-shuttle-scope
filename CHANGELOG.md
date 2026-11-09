# Changelog

## 0.5.0 — 2026-11-09

- 弹窗高度改为**按内容自适应**：`manualPanelH`（用户拖底边，持久化到插件状态）> `autoPanelH`（`min(col.childrenRect.height, maxPanelH - chromeH) + chromeH`，偶数对齐、3px 防抖）> 700 兜底。
- 底部手柄可拖拽，松手才写状态文件（不再每帧落盘）；双击手柄回到自动高度。
- 修：拖动起手记录 `implicitHeight` 导致面板被拉回自动高度。
- `同步时间` 区块默认收起，面板更短；展开后随内容长高。
- 修：测连与写 keyring 用了不同的密码（粘贴尾换行 → 测通、真挂载认证失败），统一走 `clean_password()`。
- 修：`sync` 子命令未捕获 `TimeoutExpired` 会吐 traceback，面板解析崩；改为单行 JSON 报错。
- 修：`local set` 在旧 `local` 为空时把 `Path("")` 当成 `.`，误判「目录没变」。
- 单测 36 → 38。

## 0.4.0 — 2026-11-09

- 连接视图新增**本机目录**输入框 + `保存`（`local set`）：同值不写盘、不存在自动建目录、空目录警告「会整批下载云端文件」、新旧目录互为上下级或嵌套默认拒绝（`--force` 放行）。
- 修：`contentHeight` 用 `col.implicitHeight` 恒为 0，内容超出被静默裁切 → 改 `col.childrenRect.height`。
- 单测 29 → 36。

## 0.3.0 — 2026-11-09

- 新增第三个视图**连接**：面板内改 SMB 地址 / 账号 / 密码，以及同步时间（`triggers` / `poll_interval` / `interval` / `at`）。
- `share set` 先 `smbclient` 真连一次，连不上不动配置；密码只进 keyring，`config.toml` 只写 host/share/user。
- 密码通道：QML `environment` → `--password-env` → `os.environ.pop` → `secret-tool` stdin，不进 argv、不落文件。
- 单测 13 → 29。

## 0.2.0 — 2026-11-09

- 项目化：`src/shuttle_scope/` 拆成七个模块，`bin/shuttle-scope` + `scripts/install.sh`（符号链接部署）+ `tests/`。
- 新增**云端差异**只读视图：`smbclient` 递归列共享 → `remote.json`（TTL 600s）→ 与本地 `os.walk` 比大小。

## 0.1.0 — 2026-11-09

- 第一个可用版本：DMS 状态栏 pill + 弹窗，逐层下钻勾选同步范围，写 Shuttle 的 `exclude`（备份 / 复核 / 回滚，约 1 秒热加载）。
