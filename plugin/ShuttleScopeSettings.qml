import QtQuick
import qs.Common
import qs.Widgets
import qs.Modules.Plugins

PluginSettings {
    id: root
    pluginId: "shuttleScope"

    StyledText {
        width: parent.width
        text: "Shuttle 同步范围"
        font.pixelSize: Theme.fontSizeLarge
        font.weight: Font.Bold
        color: Theme.surfaceText
    }

    StyledText {
        width: parent.width
        text: "状态栏弹出面板里的勾选会写进 ~/.config/shuttle/config.toml 的 exclude（Shuttle 约 1 秒热加载）。"
        font.pixelSize: Theme.fontSizeSmall
        color: Theme.surfaceVariantText
        wrapMode: Text.WordWrap
    }

    StringSetting {
        settingKey: "helperPath"
        label: "辅助脚本"
        description: "shuttle-scope 的路径或命令名，负责读写配置并调用 shuttle。"
        defaultValue: "shuttle-scope"
    }

    SliderSetting {
        settingKey: "idleSeconds"
        label: "空闲刷新间隔"
        description: "面板收起时多久查一次同步状态；展开时固定 3 秒。"
        defaultValue: 15
        minimum: 5
        maximum: 120
        unit: "s"
        leftIcon: "schedule"
    }
}
