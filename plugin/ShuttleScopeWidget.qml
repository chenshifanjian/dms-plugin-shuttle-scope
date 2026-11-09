import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Common
import qs.Services
import qs.Widgets
import qs.Modules.Plugins

// 状态栏 pill + 弹窗：勾选哪些顶层目录参与 Shuttle 的 NAS 双向同步。
// 真正写配置的是 ~/.local/bin/shuttle-scope（Python），本文件只负责显示与点击。
PluginComponent {
    id: root

    property var entries: []
    property var excl: []
    property var st: ({})
    property bool loaded: false
    property string error: ""
    property string busyName: ""
    property bool panelOpen: false
    property bool measuredOnce: false
    // 目录下钻：curPath 是相对同步根的路径（"" 表示顶级）
    property string curPath: ""
    property var curEntries: []
    property string navBusy: ""
    // 视图：scope=勾选同步范围，diff=云端 ⇄ 本地差异
    property string mode: "scope"
    property var diffData: ({})
    property var diffEntries: []
    property bool diffBusy: false
    property string diffError: ""
    property bool diffScanned: false

    // ── 连接（SMB 地址/账号/密码）与同步时间 ──────────────────────────
    property string connUrlInput: ""
    property string connUserInput: ""
    property string connPwInput: ""
    property string connHost: ""
    property string connShare: ""
    property string connSub: ""
    property string connUser: ""
    property string connLocal: ""
    property bool connPwStored: false
    property string connMsg: ""
    property bool connBusy: false
    property bool connOk: false

    // 本机目录（同步落地的文件夹）单独一条，跟 NAS 连接是两件事
    property string connLocalInput: ""
    property string locMsg: ""
    property bool locBusy: false
    property bool locOk: false
    property bool locWarn: false

    // ── 面板高度：默认跟着内容走（内容多高窗口多高，最多到屏幕 90%）──────
    // manualPanelH = 0 表示自适应；拖底边会把它固定成具体像素并存进插件状态
    property real manualPanelH: 0
    property real autoPanelH: 0          // 内容自己量出来的高度（由弹窗里的 flick 推上来）
    property bool schedOpen: false        // 「同步时间」默认收起，展开后面板自己长高
    readonly property real maxPanelH: {
        var sh = (parentScreen && parentScreen.height) ? parentScreen.height : 1200;
        return Math.max(360, Math.round(sh * 0.9) - 40);
    }

    function setPanelH(h, persist) {
        var v = (h && h > 0) ? Math.max(320, Math.min(Math.round(h), root.maxPanelH)) : 0;
        if (v === root.manualPanelH) return;
        root.manualPanelH = v;
        // 拖动过程中每帧都写盘太浪费，等松手再存（persist === false 表示先不存）
        if (persist === undefined || persist) root.savePanelH(v);
    }

    function savePanelH(v) {
        if (pluginService && pluginService.savePluginState)
            pluginService.savePluginState("shuttleScope", "panelHeight", v);
    }

    property var triggersSel: []
    property string schedProfile: ""
    property string pollInput: "60s"
    property string intervalInput: ""
    property string atInput: ""
    property string schedSummary: ""
    property string schedNext: ""
    property string schedMsg: ""
    property bool schedBusy: false
    property bool schedOk: false

    Process {
        id: connProc
        command: []
        environment: ({})
        stdout: StdioCollector { id: connOut }
        stderr: StdioCollector { id: connErr }
        onExited: root.onConnExit(connOut.text + connErr.text)
    }

    Process {
        id: locProc
        command: []
        environment: ({})
        stdout: StdioCollector { id: locOut }
        stderr: StdioCollector { id: locErr }
        onExited: root.onLocExit(locOut.text + locErr.text)
    }

    Process {
        id: schedProc
        command: []
        environment: ({})
        stdout: StdioCollector { id: schedOut }
        stderr: StdioCollector { id: schedErr }
        onExited: root.onSchedExit(schedOut.text + schedErr.text)
    }

    // helper 卡住（NAS 不通、smbclient 挂住）时，Process 可能一直不退出，
    // 按钮就会永远停在“保存中…”。到点直接收摊，别让界面假死。
    Timer {
        id: connWatch
        interval: 60000
        running: root.connBusy
        onTriggered: {
            if (!root.connBusy) return;
            connProc.running = false;
            connProc.environment = {};
            root.connBusy = false;
            root.connOk = false;
            root.connMsg = "等了 60 秒没回音，已放弃这次保存（NAS 不通或 smbclient 挂住）";
        }
    }

    Timer {
        id: locWatch
        interval: 30000
        running: root.locBusy
        onTriggered: {
            if (!root.locBusy) return;
            locProc.running = false;
            root.locBusy = false;
            root.locOk = false;
            root.locWarn = false;
            root.locMsg = "等了 30 秒没回音，已放弃这次保存";
        }
    }

    Timer {
        id: schedWatch
        interval: 30000
        running: root.schedBusy
        onTriggered: {
            if (!root.schedBusy) return;
            schedProc.running = false;
            root.schedBusy = false;
            root.schedOk = false;
            root.schedMsg = "等了 30 秒没回音，已放弃这次保存";
        }
    }

    property string helper: (pluginData.helperPath && pluginData.helperPath.length > 0)
                            ? pluginData.helperPath : "shuttle-scope"
    readonly property int idleSeconds: pluginData.idleSeconds || 15

    readonly property var runningStates: ["running", "syncing", "scanning"]
    readonly property bool syncing: runningStates.indexOf(st.state || "") !== -1
    readonly property bool paused: (st.state || "") === "paused" || (st.state || "") === "disabled"
    readonly property bool broken: error.length > 0 || (st.state || "") === "error"
    readonly property int excludedCount: excl ? excl.length : 0

    function human(bytes) {
        if (!bytes || bytes <= 0) return "0 B";
        // 用 1000 进制，跟 `shuttle status` 的口径一致（它显示 144.7 GB），
        // 否则面板会出现「144.7 GB」与「参与同步 134.8 GB」两个数对不上。
        var u = ["B", "KB", "MB", "GB", "TB"];
        var i = 0;
        var v = bytes;
        while (v >= 1000 && i < u.length - 1) {
            v /= 1000;
            i += 1;
        }
        return (i === 0 ? v.toFixed(0) : v.toFixed(1)) + " " + u[i];
    }

    function includedBytes() {
        var t = 0;
        for (var i = 0; i < entries.length; i += 1) {
            if (!entries[i].excluded) t += (entries[i].bytes || 0);
        }
        return t;
    }

    function stateText() {
        if (broken) return "同步异常";
        if (paused) return "已暂停";
        var s = st.state || "";
        if (s === "syncing") return "正在传输…";
        if (s === "scanning") return "正在扫描…";
        if (s === "running") return "同步中…";
        if (s === "idle") return "空闲";
        if (s === "held") return "等待共享";
        return s.length > 0 ? s : "读取中…";
    }

    function timeText() {
        var t = st.last || "";
        if (t === "just now") return "刚刚";
        return t;
    }

    function statusLine() {
        if (error.length > 0) return error;
        if (!loaded) return "读取中…";
        var bits = [];
        if (st.files) bits.push(st.files + " 个文件");
        if (st.size) bits.push(st.size);
        if (st.net === "offline") bits.push("共享不可达");
        if (excludedCount > 0) {
            bits.push("不同步 " + excludedCount + " 项");
            // 有子级排除时，顶层体积无法准确扣减，干脆不报"参与同步"，避免给出错数
            var deepExcl = false;
            for (var i = 0; i < excl.length; i += 1) {
                if (excl[i].indexOf("/") !== -1) deepExcl = true;
            }
            if (!deepExcl && includedBytes() > 0) bits.push("参与同步 " + human(includedBytes()));
        }
        if (timeText()) bits.push(timeText());
        return bits.join(" · ");
    }

    function entrySub(m) {
        var bits = [];
        var n = nestedCount(m.path);
        if (m.inherited) bits.push("上级已排除");
        if (m.bytes) bits.push(human(m.bytes));
        if (m.files) bits.push(m.files + " 个文件");
        if (bits.length === 0) bits.push(m.measured ? "空目录" : "未统计");
        if (m.dir && n > 0) bits.push(n + " 项子级已排除");
        var head = (m.excluded && !m.inherited) ? "不同步" : "";
        return head.length > 0 ? head + " · " + bits.join(" · ") : bits.join(" · ");
    }

    function refresh() {
        Proc.runCommand(undefined, [helper, "state"], function (output, code) {
            if (!output) {
                root.error = "读取失败（exit " + code + "）";
                return;
            }
            var d = null;
            try {
                d = JSON.parse(output);
            } catch (e) {
                root.error = "输出无法解析";
                return;
            }
            if (!d || !d.ok) {
                root.error = (d && d.error) ? d.error : "未知错误";
                return;
            }
            root.error = "";
            root.entries = d.entries || [];
            root.excl = d.exclude || [];
            root.st = d.status || {};
            root.loaded = true;
            root.refreshList();
        });
    }

    // 当前层级的内容（下钻时列子目录，顶级时与 entries 相同）
    function refreshList() {
        Proc.runCommand(undefined, [helper, "ls", root.curPath.length > 0 ? root.curPath : "/"],
                        function (output, code) {
            if (!output) return;
            var d = null;
            try {
                d = JSON.parse(output);
            } catch (e) {
                return;
            }
            if (!d || !d.ok) return;
            root.curEntries = d.entries || [];
            if (d.exclude) root.excl = d.exclude;
        });
    }

    function enterPath(p) {
        if (!p || p.length === 0 || navBusy.length > 0) return;
        root.curPath = p;
        root.curEntries = [];
        root.navBusy = p;
        root.refreshList();
        navClearTimer.restart();
        if (root.mode === "diff") root.refreshDiff(false);
    }

    function goUp() {
        if (root.curPath.length === 0) return;
        var parts = root.curPath.split("/");
        parts.pop();
        root.curPath = parts.join("/");
        root.curEntries = [];
        root.refreshList();
        if (root.mode === "diff") root.refreshDiff(false);
    }

    function jumpTo(p) {
        root.curPath = p;
        root.curEntries = [];
        root.refreshList();
        if (root.mode === "diff") root.refreshDiff(false);
    }

    // ── 云端 ⇄ 本地差异 ─────────────────────────────────────────────────
    function switchMode(m) {
        root.mode = m;
        if (m === "diff") root.refreshDiff(false);
        if (m === "connect") root.loadConnect();
        if (m !== "connect") root.connPwInput = "";
    }

    // ── 连接：读现状 / 存地址账号密码 / 存同步时间 ──────────────────────
    function loadConnect() {
        if (root.connBusy || root.schedBusy) return;
        root.connMsg = "";
        root.schedMsg = "";
        root.locMsg = "";
        Proc.runCommand(undefined, [helper, "share", "show"], function (output, code) {
            var d = null;
            try { d = JSON.parse(output); } catch (e) {}
            if (d && d.ok) root.applyConn(d);
            else root.connMsg = d && d.error ? d.error : "读取连接配置失败";
        });
        Proc.runCommand(undefined, [helper, "schedule", "show"], function (output, code) {
            var d = null;
            try { d = JSON.parse(output); } catch (e) {}
            if (d && d.ok) root.applySchedule(d);
            else root.schedMsg = d && d.error ? d.error : "读取同步时间失败";
        });
    }

    function applyConn(d) {
        root.connHost = d.host || "";
        root.connShare = d.share || "";
        root.connSub = d.sub || "";
        root.connLocal = d.local || "";
        root.connPwStored = !!d.password_stored;
        root.connUrlInput = "smb://" + (d.host || "") + "/" + (d.share || "")
                            + (d.sub ? "/" + d.sub : "");
        root.connUser = d.user || "";
        root.connUserInput = d.user || "";
        // share set 的返回里没有 local，别用它把用户正在填的目录清空
        if (d.local !== undefined && d.local !== null && d.local !== "") {
            root.connLocal = d.local;
            root.connLocalInput = d.local;
        }
    }

    function applySchedule(d) {
        root.schedProfile = d.profile || "";
        root.triggersSel = d.triggers || [];
        root.pollInput = d.poll_interval || "60s";
        root.intervalInput = d.interval || "";
        root.atInput = (d.at || []).join(", ");
        root.schedSummary = d.summary || "";
        root.schedNext = d.next || "";
    }

    function hasTrigger(t) {
        return (root.triggersSel || []).indexOf(t) !== -1;
    }

    function toggleTrigger(t) {
        var a = (root.triggersSel || []).slice();
        var i = a.indexOf(t);
        if (i === -1) a.push(t); else a.splice(i, 1);
        root.triggersSel = a;
    }

    // 一键预设：把「实时」「定时」与间隔/时间点一次填好
    function preset(kind) {
        if (kind === "off") {
            root.triggersSel = [];
            root.intervalInput = "";
            root.atInput = "";
        } else if (kind === "daily") {
            root.triggersSel = ["realtime", "schedule"];
            root.intervalInput = "";
            root.atInput = "02:00";
        } else if (kind === "6h") {
            root.triggersSel = ["realtime", "schedule"];
            root.atInput = "";
            root.intervalInput = "6h";
        } else if (kind === "12h") {
            root.triggersSel = ["realtime", "schedule"];
            root.atInput = "";
            root.intervalInput = "12h";
        }
    }

    function saveConnection() {
        if (root.connBusy) return;
        if (root.connUrlInput.trim().length === 0) {
            root.connOk = false;
            root.connMsg = "地址不能为空";
            return;
        }
        // 地址里写了账号就顺带用上，省得填两遍
        var u = root.connUserInput.trim();
        var m = root.connUrlInput.match(/^smb:\/\/([^@\/]+)@/);
        if (m && u.length === 0) u = m[1];
        root.connBusy = true;
        root.connMsg = "正在连接 NAS 验证…";
        root.connOk = false;
        var pw = root.connPwInput;
        root.connPwInput = "";           // 界面上不留密码
        connProc.environment = { "SHUTTLE_SCOPE_PW": pw };
        connProc.command = [helper, "share", "set",
                            "--url", root.connUrlInput.trim(),
                            "--user", u,
                            "--password-env", "SHUTTLE_SCOPE_PW"];
        connProc.running = true;
    }

    function onConnExit(text) {
        root.connBusy = false;
        connProc.environment = {};      // 密码用完即从进程环境里抹掉
        var d = null;
        try { d = JSON.parse(text); } catch (e) {}
        if (!d) { root.connOk = false; root.connMsg = "没有返回结果（helper 异常）"; return; }
        if (!d.ok) { root.connOk = false; root.connMsg = d.error || "保存失败"; return; }
        root.connOk = true;
        root.connPw = "";               // 存进去了就别留在内存和界面上
        root.connMsg = (d.message || "已保存") + (d.password_stored ? " · 密码已在 keyring" : "")
                     + (d.daemon_restarted === false ? " · 守护进程没重启成功，可能要手动重启 shuttle" : "");
        root.applyConn(d);
        root.connUserInput = d.user || "";
        root.refresh();
    }

    function saveLocal() {
        if (root.locBusy) return;
        var p = root.connLocalInput.trim();
        if (p.length === 0) {
            root.locOk = false;
            root.locWarn = false;
            root.locMsg = "本机目录不能为空，例如 /home/你/Documents/smb";
            return;
        }
        root.locBusy = true;
        root.locOk = false;
        root.locWarn = false;
        root.locMsg = "正在保存…";
        locProc.command = [helper, "local", "set", "--path", p];
        locProc.running = true;
    }

    function onLocExit(text) {
        root.locBusy = false;
        var d = null;
        try { d = JSON.parse(text); } catch (e) {}
        if (!d) { root.locOk = false; root.locWarn = false; root.locMsg = "没有返回结果（helper 异常）"; return; }
        if (!d.ok) { root.locOk = false; root.locWarn = false; root.locMsg = d.error || "保存失败"; return; }
        root.locOk = true;
        root.locWarn = !!d.warning;
        root.locMsg = d.warning ? d.warning : (d.message || "已保存");
        if (d.local) {
            root.connLocal = d.local;
            root.connLocalInput = d.local;
        }
        root.refresh();
    }

    function saveSchedule() {
        if (root.schedBusy) return;
        root.schedBusy = true;
        root.schedMsg = "正在保存…";
        root.schedOk = false;
        schedProc.command = [helper, "schedule", "set",
                             "--triggers", (root.triggersSel || []).join(","),
                             "--poll", root.pollInput.trim(),
                             "--interval", root.intervalInput.trim(),
                             "--at", root.atInput.trim()];
        schedProc.running = true;
    }

    function onSchedExit(text) {
        root.schedBusy = false;
        var d = null;
        try { d = JSON.parse(text); } catch (e) {}
        if (!d) { root.schedOk = false; root.schedMsg = "没有返回结果（helper 异常）"; return; }
        if (!d.ok) { root.schedOk = false; root.schedMsg = d.error || "保存失败"; return; }
        root.schedOk = true;
        root.schedMsg = "已生效";
        root.applySchedule(d);
    }

    function trigText() {
        if (root.hasTrigger("realtime")) return "实时";
        if (root.hasTrigger("schedule")) return "定时";
        return "手动";
    }

    // rescan=true 会真的重扫整个共享（约 15 秒），所以给足超时
    function refreshDiff(rescan) {
        if (root.diffBusy) return;
        root.diffBusy = true;
        var args = [helper, "diff", root.curPath.length > 0 ? root.curPath : "/"];
        args.push(rescan ? "--refresh" : "--quick");
        Proc.runCommand(undefined, args, function (output, code) {
            root.diffBusy = false;
            if (!output) {
                root.diffError = "读取失败（exit " + code + "）";
                return;
            }
            var d = null;
            try {
                d = JSON.parse(output);
            } catch (e) {
                root.diffError = "输出无法解析";
                return;
            }
            if (!d || !d.ok) {
                root.diffError = (d && d.error) ? d.error : "未知错误";
                return;
            }
            root.diffError = "";
            root.diffScanned = true;
            root.diffData = d;
            root.diffEntries = d.entries || [];
        }, 0, rescan ? 180000 : 45000);
    }

    function diffSummary() {
        var d = root.diffData || {};
        return d.summary || {};
    }

    // 顶部那块差异总账：云端多几个、本地多几个、大小不同几个
    function diffHeadline() {
        if (root.diffError.length > 0) return root.diffError;
        var s = root.diffSummary();
        if (s.remote_only === undefined) return root.diffBusy ? "正在比对…" : "点「重扫云端」拉一份清单";
        var bits = [];
        if (s.remote_only > 0) bits.push("云端多 " + s.remote_only + " 个 · " + human(s.remote_only_bytes));
        if (s.local_only > 0) bits.push("本地多 " + s.local_only + " 个 · " + human(s.local_only_bytes));
        if (s.differ > 0) bits.push("大小不同 " + s.differ + " 个");
        if (bits.length === 0) return "这一层与云端一致（" + (s.same || 0) + " 个文件）";
        return bits.join("；");
    }

    function diffNote() {
        var d = root.diffData || {};
        var r = d.remote || {};
        var bits = [];
        if (root.diffBusy) bits.push("读取中…");
        else if (r.stale) bits.push("云端清单是旧的（" + Math.round((r.age || 0) / 60) + " 分钟前）");
        else if (r.age !== undefined) bits.push("云端清单 " + (r.age < 90 ? "刚刚" : Math.round(r.age / 60) + " 分钟前") + "更新");
        if (r.files) bits.push("云端 " + r.files + " 个文件 · " + human(r.bytes || 0));
        if (d.excluded) bits.push("这一层不同步（" + (d.excluded_by || d.path) + "）");
        return bits.join(" · ");
    }

    // 目录行：子树里云端多/本地多/不同各几个；文件行：它自己的状态
    function diffEntrySub(m) {
        if (!m) return "";
        if (!m.dir) {
            // 右侧已有「仅云端/仅本地/不同」徽标，这里只写大小，不重复
            if (m.status === "remote_only") return human(m.size_remote || 0);
            if (m.status === "local_only") return human(m.size_local || 0);
            if (m.status === "differ") return "本地 " + human(m.size_local || 0) + " / 云端 " + human(m.size_remote || 0);
            return human(m.size_remote || m.size_local || 0);
        }
        var bits = [];
        if (m.remote_only > 0) bits.push("云端多 " + m.remote_only + (m.remote_only_bytes ? " · " + human(m.remote_only_bytes) : ""));
        if (m.local_only > 0) bits.push("本地多 " + m.local_only + (m.local_only_bytes ? " · " + human(m.local_only_bytes) : ""));
        if (m.differ > 0) bits.push("大小不同 " + m.differ);
        if (bits.length === 0) bits.push(m.in_remote ? "一致（" + (m.files || 0) + " 个文件）" : "本地空目录");
        if (!m.in_remote) bits.push("云端没有这个目录");
        return bits.join(" · ");
    }

    function diffColor(m) {
        if (!m) return Theme.surfaceVariantText;
        if (!m.dir && m.status === "same") return Theme.surfaceVariantText;
        if (m.dir && (m.diff_count || 0) === 0) return Theme.surfaceVariantText;
        return Theme.primary;
    }

    function diffEmptyText() {
        if (root.diffBusy) return "正在比对云端与本地…";
        if (root.diffEntries.length === 0) return "这一层没有可显示的内容";
        return "";
    }

    // 右侧小标签：只在“有问题”的行上出现
    function diffBadge(m) {
        if (!m) return "";
        if (!m.dir) {
            if (m.status === "remote_only") return "仅云端";
            if (m.status === "local_only") return "仅本地";
            if (m.status === "differ") return "不同";
            return "";
        }
        var n = m.diff_count || 0;
        return n > 0 ? String(n) : "";
    }

    // 当前层级下已有多少条排除记录（用于提示"部分子项已排除"）
    function nestedCount(p) {
        if (!root.excl || !p) return 0;
        var n = 0;
        for (var i = 0; i < root.excl.length; i += 1) {
            if (root.excl[i].indexOf(p + "/") === 0) n += 1;
        }
        return n;
    }

    Timer {
        id: navClearTimer
        interval: 1200
        onTriggered: root.navBusy = ""
    }

    function measure() {
        Proc.runCommand(undefined, [helper, "sizes", "--path", root.curPath.length > 0 ? root.curPath : "/"],
                        function (output, code) {
            root.measuredOnce = true;
            root.refresh();
        });
    }

    function toggleEntry(path) {
        if (busyName.length > 0) return;
        busyName = path;
        Proc.runCommand(undefined, [helper, "toggle", path], function (output, code) {
            root.busyName = "";
            var d = null;
            try {
                d = JSON.parse(output);
            } catch (e) {
                d = null;
            }
            if (!d || !d.ok) {
                root.error = (d && d.error) ? d.error : ("切换失败（exit " + code + "）");
            } else {
                root.error = "";
            }
            root.refresh();
        });
    }

    function control(verb) {
        Proc.runCommand(undefined, [helper, "control", verb], function (output, code) {
            root.refresh();
        });
    }

    function openTui() {
        Proc.runCommand(undefined, ["kitty", "--single-instance", "--class", "shuttle", "--",
                                    "shuttle", "open", "shuttles"], function (output, code) {});
    }

    Component.onCompleted: refresh()

    // 拖动过的窗口高度记在插件状态里；pluginService 在 onCompleted 时可能还没绑好
    Timer {
        interval: 500
        running: true
        repeat: false
        onTriggered: {
            if (pluginService && pluginService.loadPluginState) {
                var v = pluginService.loadPluginState("shuttleScope", "panelHeight", 0);
                if (v > 0) root.manualPanelH = v;
            }
        }
    }

    // 展开时 3 秒一次，收起且空闲时按设置间隔；同步中始终快查。
    Timer {
        interval: (root.panelOpen || root.syncing) ? 3000 : Math.max(5, root.idleSeconds) * 1000
        running: true
        repeat: true
        triggeredOnStart: false
        onTriggered: root.refresh()
    }

    // ── 状态栏 pill ───────────────────────────────────────────────────────
    horizontalBarPill: Component {
        Row {
            spacing: Theme.spacingXS

            DankIcon {
                name: root.broken ? "cloud_off" : root.syncing ? "cloud_sync" : "cloud_done"
                size: Theme.iconSize - 6
                color: root.broken ? Theme.error : (root.syncing ? Theme.primary : Theme.surfaceText)
                anchors.verticalCenter: parent.verticalCenter
            }

            StyledText {
                text: root.excludedCount > 0 ? ("−" + root.excludedCount) : ""
                visible: root.excludedCount > 0
                color: Theme.primary
                font.pixelSize: Theme.fontSizeSmall
                anchors.verticalCenter: parent.verticalCenter
            }
        }
    }

    verticalBarPill: Component {
        Column {
            spacing: 1

            DankIcon {
                name: root.broken ? "cloud_off" : root.syncing ? "cloud_sync" : "cloud_done"
                size: Theme.iconSize - 8
                color: root.broken ? Theme.error : (root.syncing ? Theme.primary : Theme.surfaceText)
                anchors.horizontalCenter: parent.horizontalCenter
            }

            StyledText {
                text: root.excludedCount > 0 ? ("−" + root.excludedCount) : ""
                visible: root.excludedCount > 0
                color: Theme.primary
                font.pixelSize: Theme.fontSizeSmall
                anchors.horizontalCenter: parent.horizontalCenter
            }
        }
    }

    // ── 弹窗：勾选同步范围 ────────────────────────────────────────────────
    popoutContent: Component {
        PopoutComponent {
            id: barPopout
            headerText: root.mode === "diff" ? "云端与本地差异"
                        : root.mode === "connect" ? "连接与同步时间" : "Shuttle 同步范围"
            showCloseButton: true

            Component.onCompleted: {
                root.panelOpen = true;
                if (!root.measuredOnce) root.measure();
            }
            Component.onDestruction: root.panelOpen = false

            DankFlickable {
                id: popFlick
                width: parent.width
                // 面板外面还套着 header + 拖拽条 + 两圈内边距，这些不算内容
                readonly property real chromeH: barPopout.headerHeight + barPopout.detailsHeight
                                                 + gripItem.height + PopoutMetrics.contentPadding * 2
                height: root.manualPanelH > 0
                        ? Math.max(180, root.manualPanelH - chromeH)
                        : Math.max(260, Math.min(col.childrenRect.height, root.maxPanelH - chromeH))
                contentHeight: Math.max(height, col.childrenRect.height)

                // 自适应高度：谁也别猜，直接量内容。量到的是「内容 + header + 拖拽条 + 内边距」，
                // 推给 root 让 popoutHeight 跟着变；差 3px 以内不动，免得来回抖。
                readonly property real wantH: (col.childrenRect.height > 0)
                        ? Math.min(col.childrenRect.height, root.maxPanelH - chromeH) + chromeH : 0
                onWantHChanged: {
                    var v = Math.max(340, Math.round(wantH / 2) * 2);
                    if (wantH > 0 && Math.abs(v - root.autoPanelH) > 3) root.autoPanelH = v;
                }
                clip: true
                boundsBehavior: Flickable.StopAtBounds

                ColumnLayout {
                    id: col
                    width: parent.width
                    spacing: Theme.spacingS

                    Rectangle {
                        Layout.fillWidth: true
                        implicitHeight: statusRow.implicitHeight + Theme.spacingM * 2
                        radius: Theme.cornerRadius
                        color: Theme.surfaceContainerHigh

                        RowLayout {
                            id: statusRow
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.top: parent.top
                            anchors.margins: Theme.spacingM
                            spacing: Theme.spacingM

                            DankIcon {
                                name: root.broken ? "cloud_off" : root.syncing ? "cloud_sync" : "cloud_done"
                                size: Theme.iconSize
                                color: root.broken ? Theme.error : (root.syncing ? Theme.primary : Theme.surfaceText)
                                Layout.alignment: Qt.AlignTop
                            }

                            Column {
                                Layout.fillWidth: true
                                spacing: 2

                                StyledText {
                                    width: parent.width
                                    text: root.stateText()
                                    font.pixelSize: Theme.fontSizeMedium
                                    font.weight: Font.Medium
                                    color: root.broken ? Theme.error : Theme.surfaceText
                                }

                                StyledText {
                                    width: parent.width
                                    text: root.statusLine()
                                    font.pixelSize: Theme.fontSizeSmall
                                    color: Theme.surfaceVariantText
                                    wrapMode: Text.Wrap
                                }
                            }
                        }
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        Layout.topMargin: Theme.spacingS
                        spacing: Theme.spacingS

                        DankButton {
                            text: "立即同步"
                            iconName: "sync"
                            enabled: !root.syncing
                            onClicked: root.control("sync")
                        }

                        DankButton {
                            text: root.paused ? "继续" : "暂停"
                            iconName: root.paused ? "play_arrow" : "pause"
                            onClicked: root.control(root.paused ? "resume" : "pause")
                        }

                        DankButton {
                            text: "面板"
                            iconName: "open_in_new"
                            onClicked: root.openTui()
                        }
                    }

                    // 两个视图：勾选同步范围 / 只看云端与本地的差异
                    RowLayout {
                        Layout.fillWidth: true
                        Layout.topMargin: Theme.spacingS
                        spacing: Theme.spacingS

                        DankButton {
                            text: "同步范围"
                            iconName: "checklist"
                            opacity: root.mode === "scope" ? 1.0 : 0.55
                            onClicked: root.switchMode("scope")
                        }

                        DankButton {
                            text: "云端差异"
                            iconName: "compare_arrows"
                            opacity: root.mode === "diff" ? 1.0 : 0.55
                            onClicked: root.switchMode("diff")
                        }

                        DankButton {
                            text: "连接"
                            iconName: "settings_ethernet"
                            opacity: root.mode === "connect" ? 1.0 : 0.55
                            onClicked: root.switchMode("connect")
                        }

                        Item { Layout.fillWidth: true }

                        StyledText {
                            visible: root.mode === "diff"
                            text: "只读，不改同步范围"
                            font.pixelSize: Theme.fontSizeSmall
                            color: Theme.surfaceVariantText
                        }
                    }

                    // ── 连接与同步时间（只在这里改 NAS 地址/账号/密码与定时）──
                    ColumnLayout {
                        Layout.fillWidth: true
                        Layout.topMargin: Theme.spacingS
                        visible: root.mode === "connect"
                        spacing: Theme.spacingXS

                        Rectangle {
                            Layout.fillWidth: true
                            implicitHeight: connCol.implicitHeight + Theme.spacingM * 2
                            radius: Theme.cornerRadius
                            color: Theme.surfaceContainerHigh

                            Column {
                                id: connCol
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.top: parent.top
                                anchors.margins: Theme.spacingM
                                spacing: 2

                                StyledText {
                                    width: parent.width
                                    text: root.connHost.length > 0
                                          ? (root.connHost + " / " + root.connShare)
                                          : "还没读到连接配置"
                                    font.pixelSize: Theme.fontSizeMedium
                                    font.weight: Font.Medium
                                    color: Theme.surfaceText
                                    elide: Text.ElideMiddle
                                }

                                StyledText {
                                    width: parent.width
                                    visible: root.connHost.length > 0
                                    text: "账号 " + root.connUser
                                          + " · " + (root.connPwStored ? "密码在 keyring" : "密码未存")
                                          + (root.connSub.length > 0 ? " · 子目录 " + root.connSub : "")
                                    font.pixelSize: Theme.fontSizeSmall
                                    color: Theme.surfaceVariantText
                                    elide: Text.ElideMiddle
                                }

                            }
                        }

                        DankTextField {
                            Layout.fillWidth: true
                            labelText: "SMB 地址"
                            placeholderText: "smb://192.168.1.10/共享名"
                            text: root.connUrlInput
                            onTextChanged: if (text !== root.connUrlInput) root.connUrlInput = text
                        }

                        DankTextField {
                            Layout.fillWidth: true
                            labelText: "账号"
                            placeholderText: "NAS 用户名"
                            text: root.connUserInput
                            onTextChanged: if (text !== root.connUserInput) root.connUserInput = text
                        }

                        DankTextField {
                            Layout.fillWidth: true
                            labelText: "密码（留空＝不改）"
                            placeholderText: "只写进系统 keyring"
                            echoMode: TextInput.Password
                            showPasswordToggle: true
                            text: root.connPwInput
                            onTextChanged: if (text !== root.connPwInput) root.connPwInput = text
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: Theme.spacingS

                            DankTextField {
                                Layout.fillWidth: true
                                labelText: "本机目录（同步落地的文件夹）"
                                placeholderText: "/home/你/Documents/smb"
                                text: root.connLocalInput
                                onTextChanged: if (text !== root.connLocalInput) root.connLocalInput = text
                            }

                            DankButton {
                                text: root.locBusy ? "保存中…" : "保存"
                                iconName: "folder"
                                enabled: !root.locBusy
                                onClicked: root.saveLocal()
                            }
                        }

                        StyledText {
                            Layout.fillWidth: true
                            visible: root.locMsg.length > 0
                            text: root.locMsg
                            font.pixelSize: Theme.fontSizeSmall
                            color: root.locOk ? (root.locWarn ? Theme.warning : Theme.primary) : Theme.error
                            wrapMode: Text.Wrap
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: Theme.spacingS

                            DankButton {
                                text: root.connBusy ? "连接中…" : "保存并测试"
                                iconName: "wifi_tethering"
                                enabled: !root.connBusy
                                onClicked: root.saveConnection()
                            }

                            StyledText {
                                Layout.fillWidth: true
                                text: root.connMsg
                                font.pixelSize: Theme.fontSizeSmall
                                color: root.connMsg.length === 0 ? Theme.surfaceVariantText
                                       : (root.connOk ? Theme.primary : Theme.error)
                                wrapMode: Text.Wrap
                            }
                        }

                        StyledText {
                            Layout.fillWidth: true
                            text: "保存前先真连一次 NAS，连不上就不改配置；密码只进 keyring。本机目录只改落地文件夹，空目录会整批下载云端文件。窗口太长/太短就拖底边，双击底边回到自动。"
                            font.pixelSize: Theme.fontSizeSmall
                            color: Theme.surfaceVariantText
                            wrapMode: Text.Wrap
                        }

                        // ── 同步时间（默认收起，展开后面板自己长高）──────────
                        RowLayout {
                            Layout.fillWidth: true
                            Layout.topMargin: Theme.spacingS
                            spacing: Theme.spacingS

                            StyledText {
                                Layout.fillWidth: true
                                text: "同步时间" + (root.schedProfile.length > 0 ? "（" + root.schedProfile + "）" : "")
                                font.pixelSize: Theme.fontSizeMedium
                                font.weight: Font.Medium
                                color: Theme.surfaceText
                                elide: Text.ElideRight
                            }

                            DankButton {
                                text: root.schedOpen ? "收起" : "展开"
                                iconName: root.schedOpen ? "expand_less" : "expand_more"
                                onClicked: root.schedOpen = !root.schedOpen
                            }
                        }

                        // 收起时用一行摘要代替整块表单
                        StyledText {
                            Layout.fillWidth: true
                            visible: !root.schedOpen
                            text: root.schedSummary.length > 0
                                  ? root.schedSummary + (root.schedNext.length > 0 ? " · 下次 " + root.schedNext : "")
                                  : "正在读取…"
                            font.pixelSize: Theme.fontSizeSmall
                            color: Theme.surfaceVariantText
                            wrapMode: Text.Wrap
                        }

                        ColumnLayout {
                            id: schedBox
                            Layout.fillWidth: true
                            visible: root.schedOpen
                            spacing: Theme.spacingXS

                        StyledText {
                            Layout.fillWidth: true
                            text: root.schedSummary.length > 0
                                  ? root.schedSummary + (root.schedNext.length > 0 ? " · 下次 " + root.schedNext : "")
                                  : "正在读取…"
                            font.pixelSize: Theme.fontSizeSmall
                            color: Theme.surfaceVariantText
                            wrapMode: Text.Wrap
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: Theme.spacingS

                            DankButton {
                                text: "实时"
                                iconName: "bolt"
                                opacity: root.hasTrigger("realtime") ? 1.0 : 0.55
                                onClicked: root.toggleTrigger("realtime")
                            }

                            DankButton {
                                text: "定时"
                                iconName: "schedule"
                                opacity: root.hasTrigger("schedule") ? 1.0 : 0.55
                                onClicked: root.toggleTrigger("schedule")
                            }

                            DankButton {
                                text: "上线补跑"
                                iconName: "cloud_download"
                                opacity: root.hasTrigger("share-available") ? 1.0 : 0.55
                                onClicked: root.toggleTrigger("share-available")
                            }
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: Theme.spacingXS

                            DankButton {
                                text: "每天 02:00"
                                opacity: 0.85
                                onClicked: root.preset("daily")
                            }

                            DankButton {
                                text: "每 6 小时"
                                opacity: 0.85
                                onClicked: root.preset("6h")
                            }

                            DankButton {
                                text: "每 12 小时"
                                opacity: 0.85
                                onClicked: root.preset("12h")
                            }

                            DankButton {
                                text: "只手动"
                                opacity: 0.85
                                onClicked: root.preset("off")
                            }
                        }

                        RowLayout {
                            id: schedInputs
                            Layout.fillWidth: true
                            spacing: Theme.spacingXS

                            DankTextField {
                                Layout.fillWidth: true
                                labelText: "每隔"
                                placeholderText: "留空＝不按"
                                text: root.intervalInput
                                onTextChanged: if (text !== root.intervalInput) root.intervalInput = text
                            }

                            DankTextField {
                                Layout.fillWidth: true
                                labelText: "每天几点"
                                placeholderText: "留空＝不按"
                                text: root.atInput
                                onTextChanged: if (text !== root.atInput) root.atInput = text
                            }

                            DankTextField {
                                Layout.fillWidth: true
                                labelText: "查共享间隔"
                                placeholderText: "60s"
                                text: root.pollInput
                                onTextChanged: if (text !== root.pollInput) root.pollInput = text
                            }
                        }

                        RowLayout {
                            id: schedSaveRow
                            Layout.fillWidth: true
                            spacing: Theme.spacingS

                            DankButton {
                                text: root.schedBusy ? "保存中…" : "保存同步时间"
                                iconName: "save"
                                enabled: !root.schedBusy
                                onClicked: root.saveSchedule()
                            }

                            StyledText {
                                Layout.fillWidth: true
                                text: root.schedMsg
                                font.pixelSize: Theme.fontSizeSmall
                                color: root.schedMsg.length === 0 ? Theme.surfaceVariantText
                                       : (root.schedOk ? Theme.primary : Theme.error)
                                wrapMode: Text.Wrap
                            }
                        }

                        StyledText {
                            Layout.fillWidth: true
                            Layout.bottomMargin: Theme.spacingS
                            text: "每隔/每天几点都填＝都生效；「上线补跑」＝NAS 刚连上补一次；只手动＝只有点「立即同步」才跑。"
                            font.pixelSize: Theme.fontSizeSmall
                            color: Theme.surfaceVariantText
                            wrapMode: Text.Wrap
                        }
                        }   // schedBox
                    }

                    // 差异总账：云端多几个、本地多几个、大小不同几个
                    Rectangle {
                        Layout.fillWidth: true
                        visible: root.mode === "diff"
                        implicitHeight: diffCol.implicitHeight + Theme.spacingM * 2
                        radius: Theme.cornerRadius
                        color: Theme.surfaceContainerHigh

                        Column {
                            id: diffCol
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.top: parent.top
                            anchors.margins: Theme.spacingM
                            spacing: 2

                            StyledText {
                                width: parent.width
                                text: root.diffHeadline()
                                font.pixelSize: Theme.fontSizeMedium
                                font.weight: Font.Medium
                                color: root.diffError.length > 0 ? Theme.error : Theme.surfaceText
                                wrapMode: Text.Wrap
                            }

                            StyledText {
                                width: parent.width
                                text: root.diffNote()
                                font.pixelSize: Theme.fontSizeSmall
                                color: Theme.surfaceVariantText
                                wrapMode: Text.Wrap
                            }
                        }
                    }

                    // 位置栏：顶级时给说明，进入子层后给返回 + 当前路径
                    RowLayout {
                        Layout.fillWidth: true
                        Layout.topMargin: Theme.spacingS
                        visible: root.curPath.length > 0 && root.mode !== "connect"
                        spacing: Theme.spacingS

                        DankButton {
                            text: "返回"
                            iconName: "arrow_back"
                            onClicked: root.goUp()
                        }

                        StyledText {
                            Layout.fillWidth: true
                            text: root.curPath
                            font.pixelSize: Theme.fontSizeSmall
                            color: Theme.primary
                            elide: Text.ElideLeft
                        }
                    }

                    StyledText {
                        Layout.fillWidth: true
                        Layout.topMargin: Theme.spacingS
                        visible: root.mode === "scope" && root.curPath.length === 0
                        text: "勾选 = 参与同步（本机 ⇄ NAS）。点目录名可进入下一层再选；取消勾选只是不再同步它，两边的副本都保留。"
                        font.pixelSize: Theme.fontSizeSmall
                        color: Theme.surfaceVariantText
                        wrapMode: Text.Wrap
                    }

                    Repeater {
                        model: root.mode === "diff" ? root.diffEntries
                               : (root.mode === "scope" ? root.curEntries : [])

                        delegate: Rectangle {
                            Layout.fillWidth: true
                            implicitHeight: rowBody.implicitHeight + Theme.spacingS * 2
                            radius: Theme.cornerRadius
                            color: rowMouse.containsMouse ? Theme.surfaceContainerHigh : "transparent"
                            opacity: (root.busyName === modelData.path || root.navBusy === modelData.path) ? 0.5 : 1

                            MouseArea {
                                id: rowMouse
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: (modelData.dir || root.mode === "scope") ? Qt.PointingHandCursor : Qt.ArrowCursor
                                enabled: root.busyName.length === 0 && root.navBusy.length === 0
                                onClicked: {
                                    if (modelData.dir) root.enterPath(modelData.path);
                                    else if (root.mode === "scope") root.toggleEntry(modelData.path);
                                }
                            }

                            RowLayout {
                                id: rowBody
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.top: parent.top
                                anchors.margins: Theme.spacingS
                                spacing: Theme.spacingS

                                DankIcon {
                                    name: modelData.dir ? "folder" : "description"
                                    size: Theme.iconSize - 6
                                    color: root.mode === "diff"
                                           ? root.diffColor(modelData)
                                           : ((modelData.excluded || modelData.inherited) ? Theme.surfaceVariantText : Theme.primary)
                                    Layout.alignment: Qt.AlignVCenter
                                }

                                Column {
                                    Layout.fillWidth: true
                                    spacing: 1

                                    StyledText {
                                        width: parent.width
                                        text: modelData.name
                                        font.pixelSize: Theme.fontSizeMedium
                                        color: (root.mode === "scope" && (modelData.excluded || modelData.inherited))
                                               ? Theme.surfaceVariantText : Theme.surfaceText
                                        elide: Text.ElideMiddle
                                    }

                                    StyledText {
                                        width: parent.width
                                        text: root.mode === "diff" ? root.diffEntrySub(modelData) : root.entrySub(modelData)
                                        font.pixelSize: Theme.fontSizeSmall
                                        color: root.mode === "diff" ? root.diffColor(modelData) : Theme.surfaceVariantText
                                        elide: Text.ElideRight
                                    }
                                }

                                StyledText {
                                    visible: root.mode === "diff" && root.diffBadge(modelData).length > 0
                                    text: root.diffBadge(modelData)
                                    font.pixelSize: Theme.fontSizeSmall
                                    color: Theme.primary
                                    Layout.alignment: Qt.AlignVCenter
                                }

                                DankIcon {
                                    visible: modelData.dir
                                    name: "chevron_right"
                                    size: Theme.iconSize - 6
                                    color: Theme.surfaceVariantText
                                    Layout.alignment: Qt.AlignVCenter
                                }

                                Item {
                                    visible: root.mode === "scope"
                                    implicitWidth: checkIcon.implicitWidth + Theme.spacingXS * 2
                                    implicitHeight: checkIcon.implicitHeight + Theme.spacingXS * 2
                                    Layout.alignment: Qt.AlignVCenter

                                    DankIcon {
                                        id: checkIcon
                                        anchors.centerIn: parent
                                        name: modelData.excluded ? "check_box_outline_blank" : "check_box"
                                        size: Theme.iconSize - 4
                                        color: (modelData.excluded || modelData.inherited) ? Theme.surfaceVariantText : Theme.primary
                                        opacity: modelData.inherited ? 0.45 : 1
                                    }

                                    // 复选框单独可点：目录行点空白处是进入，点这里是勾选
                                    MouseArea {
                                        anchors.fill: parent
                                        enabled: !modelData.inherited && root.busyName.length === 0
                                        cursorShape: Qt.PointingHandCursor
                                        onClicked: root.toggleEntry(modelData.path)
                                    }
                                }
                            }
                        }
                    }

                    StyledText {
                        Layout.fillWidth: true
                        visible: root.mode !== "connect"
                                 && (root.mode === "diff" ? root.diffEntries.length === 0 : root.curEntries.length === 0)
                        text: root.mode === "diff"
                              ? root.diffEmptyText()
                              : (root.navBusy.length > 0 ? "正在读取…" : "这一层没有子项")
                        font.pixelSize: Theme.fontSizeSmall
                        color: Theme.surfaceVariantText
                        wrapMode: Text.Wrap
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Theme.spacingS
                        visible: root.mode !== "connect"

                        DankButton {
                            visible: root.mode === "diff"
                            text: "重扫云端"
                            iconName: "refresh"
                            onClicked: root.refreshDiff(true)
                        }

                        DankButton {
                            visible: root.mode === "scope"
                            text: "重新统计占用"
                            iconName: "calculate"
                            onClicked: root.measure()
                        }

                        Item { Layout.fillWidth: true }

                        StyledText {
                            visible: root.mode === "scope"
                            text: root.excludedCount > 0 ? ("已排除 " + root.excludedCount + " 项") : "全部同步"
                            font.pixelSize: Theme.fontSizeSmall
                            color: root.excludedCount > 0 ? Theme.primary : Theme.surfaceVariantText
                        }
                    }

                    StyledText {
                        Layout.fillWidth: true
                        Layout.bottomMargin: Theme.spacingS
                        visible: root.mode !== "connect"
                        text: root.mode === "diff"
                              ? "只比文件大小（同名不同尺寸＝「不同」）。云端清单缓存在本机，点「重扫云端」才重读 NAS（约 15 秒）。"
                              : "占用为本地磁盘口径；排除后如需腾空间，请自行删除对应目录。"
                        font.pixelSize: Theme.fontSizeSmall
                        color: Theme.surfaceVariantText
                        wrapMode: Text.Wrap
                    }
                }
            }

            // 底边：上下拖＝固定高度，双击＝回到「跟着内容走」
            Item {
                id: gripItem
                width: parent.width
                height: 14

                MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.SizeVerCursor
                    property real sceneY0: 0
                    property real h0: 0
                    onPressed: (m) => { sceneY0 = m.sceneY; h0 = barPopout.height; }
                    onPositionChanged: (m) => {
                        if (!pressed) return;
                        var d = m.sceneY - sceneY0;
                        if (Math.abs(d) < 4) return;
                        root.setPanelH(h0 + d, false);   // 拖动中不写盘
                    }
                    onReleased: root.savePanelH(root.manualPanelH)
                    onDoubleClicked: root.setPanelH(0)
                }

                Row {
                    anchors.centerIn: parent
                    spacing: 3
                    Repeater {
                        model: 3
                        Rectangle {
                            width: 18; height: 3; radius: 1.5
                            color: Theme.surfaceVariantText
                            opacity: 0.45
                        }
                    }
                }
            }
        }
    }

    popoutWidth: 400
    // 真正的高度由 popoutContent 里的 flickable 撑出来（PluginPopout 会把
    // contentHeight 绑到内容的 implicitHeight），这里只是打开前的一个初值
    popoutHeight: root.manualPanelH > 0 ? root.manualPanelH
                                         : (root.autoPanelH > 0 ? root.autoPanelH : 700)

}
