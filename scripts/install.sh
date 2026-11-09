#!/usr/bin/env bash
# 把项目里的代码/插件装到本机会用的位置（全部用符号链接，改项目即改运行中的东西）
set -euo pipefail
P="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLUG="$HOME/.config/DankMaterialShell/plugins/shuttleScope"
BIN="$HOME/.local/bin/shuttle-scope"

mkdir -p "$PLUG" "$(dirname "$BIN")"

# 1) CLI 启动器
if [ -e "$BIN" ] && [ ! -L "$BIN" ]; then
  mv "$BIN" "$BIN.old-$(date +%Y%m%d%H%M%S)"
fi
ln -sfn "$P/bin/shuttle-scope" "$BIN"
chmod +x "$P/bin/shuttle-scope"

# 2) DMS 插件三件套
for f in plugin.json ShuttleScopeWidget.qml ShuttleScopeSettings.qml; do
  if [ -e "$PLUG/$f" ] && [ ! -L "$PLUG/$f" ]; then
    mv "$PLUG/$f" "$PLUG/$f.old-$(date +%Y%m%d%H%M%S)"
  fi
  ln -sfn "$P/plugin/$f" "$PLUG/$f"
done

# 3) 让 DMS 丢掉 QML 缓存，不然改了看不到
rm -rf "$HOME/.cache/quickshell/qmlcache" 2>/dev/null || true
systemctl --user reset-failed dms 2>/dev/null || true
systemctl --user restart dms

echo "installed:"
ls -l "$BIN"
ls -l "$PLUG"
"$BIN" version
