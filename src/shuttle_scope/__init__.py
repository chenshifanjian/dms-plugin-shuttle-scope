"""shuttle-scope: 读写 Shuttle 的同步范围，并比对云端（SMB 共享）与本地文件的差异。

模块划分：
    config  —— 读写 ~/.config/shuttle/config.toml（备份、回滚、用 `shuttle status` 复核）
    sizes   —— 本地目录占用统计与缓存
    tree    —— 目录枚举、排除继承判定
    remote  —— 通过 smbclient 列出 NAS 一侧的索引（凭据取自 keyring）
    diff    —— 比对本地索引与远端索引，按层聚合
    connect —— SMB 地址/账号/密码（keyring）、本机目录（shuttles.*.local）与同步时间（profile.triggers/interval/at）
    cli     —— 命令行分发
"""

__version__ = "0.5.0"
