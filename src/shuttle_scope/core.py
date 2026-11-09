"""公共基础：路径、配置读取、`shuttle status` 解析、体积格式化。"""

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

HOME = pathlib.Path.home()
CONF = pathlib.Path(os.environ.get("SHUTTLE_CONFIG", HOME / ".config/shuttle/config.toml"))
CACHE_DIR = pathlib.Path(os.environ.get("XDG_CACHE_HOME", HOME / ".cache")) / "shuttle-scope"
SIZES_CACHE = CACHE_DIR / "sizes.json"
REMOTE_CACHE = CACHE_DIR / "remote.json"
SHUTTLE = shutil.which("shuttle") or str(HOME / ".local/bin/shuttle")


def out(obj, code=0):
    """所有命令的单行 JSON 出口。"""
    print(json.dumps(obj, ensure_ascii=False))
    sys.exit(code)


def fail(msg, **kw):
    kw.update({"ok": False, "error": msg})
    out(kw, 1)


def read_conf():
    import tomllib

    if not CONF.exists():
        fail(f"配置文件不存在: {CONF}")
    raw = CONF.read_text(encoding="utf-8")
    try:
        doc = tomllib.loads(raw)
    except Exception as e:  # noqa: BLE001
        fail(f"配置解析失败: {e}")
    return raw, doc


def read_doc():
    import tomllib

    try:
        return tomllib.loads(CONF.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def pick(_doc=None, name=None):
    """选中一个 shuttle：显式参数 → $SHUTTLE_NAME → moren → 字典序第一个。"""
    raw, doc = read_conf()
    shuttles = doc.get("shuttles") or {}
    if not shuttles:
        fail("配置里没有 [shuttles.*]")
    name = name or os.environ.get("SHUTTLE_NAME") or ("moren" if "moren" in shuttles else sorted(shuttles)[0])
    if name not in shuttles:
        fail(f"没有 shuttle: {name}")
    sh = shuttles[name]
    local = pathlib.Path(os.path.expanduser(str(sh.get("local", ""))))
    return name, sh, local


def rsync_status(name):
    """`shuttle status` 的一行文本 -> 粗略结构。"""
    try:
        p = subprocess.run([SHUTTLE, "status"], capture_output=True, text=True, timeout=25)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "raw": ""}
    raw = (p.stdout or p.stderr or "").strip()
    line = ""
    for ln in raw.splitlines():
        if ln.startswith(name):
            line = ln.strip()
            break
    line = line or raw.splitlines()[0] if raw else ""
    st = {"raw": line, "mode": "", "state": "", "net": "", "size": "", "files": "", "last": ""}
    if line:
        toks = line.split()
        if len(toks) >= 3:
            st["mode"] = toks[2]
        states = ("idle", "syncing", "running", "scanning", "paused", "disabled", "error", "held")
        for t in toks:
            if t in states and not st["state"]:
                st["state"] = t
            if t in ("online", "offline"):
                st["net"] = t
        m = re.search(r"([\d.]+)\s*([KMGT]?B)\b", line)
        if m:
            st["size"] = f"{m.group(1)} {m.group(2)}"
        for i, t in enumerate(toks):
            if re.fullmatch(r"\d{1,3}(?:,\d{3})+|\d+", t) and not (
                i + 1 < len(toks) and toks[i + 1] in ("B", "KB", "MB", "GB", "TB")
            ):
                st["files"] = t
        if st["files"] in ("", st["mode"]):
            st["files"] = ""
        m3 = re.search(r"(just now|today|\d+[mhd] ago|\d{4}-\d\d-\d\d.*)$", line)
        if m3:
            st["last"] = m3.group(1)
    st["ok"] = True
    return st


def fmt_bytes(n):
    """十进制（1000 进位），跟 `shuttle status` 的口径对齐。"""
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000.0


def rel_join(parent, name):
    return f"{parent}/{name}" if parent else name


def ancestor_hit(rel, exclude):
    """rel 自身或任一祖先在 exclude 里？返回命中的那一项（空串＝没命中）。"""
    parts = rel.split("/")
    for i in range(1, len(parts) + 1):
        p = "/".join(parts[:i])
        if p in exclude:
            return p
    return ""


def write_json_atomic(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
