"""写配置：只改 [shuttles.<name>] 下的 exclude 一行，备份 + 复核 + 失败回滚。"""

import json
import pathlib
import re
import time

from .core import CACHE_DIR, CONF, ancestor_hit, fail, out, pick, read_conf, rsync_status


def patch_config(raw, shuttle, values):
    """把 [shuttles.<shuttle>] 里的 exclude 改成 values（空列表则删掉这一行）。"""
    lines = raw.splitlines(keepends=True)
    sec = re.compile(r"^\s*\[shuttles\.%s\]\s*$" % re.escape(shuttle))
    start = next((i for i, ln in enumerate(lines) if sec.match(ln.strip())), None)
    if start is None:
        raise SystemExit(f"找不到 [shuttles.{shuttle}]")
    end = start + 1
    while end < len(lines) and not re.match(r"^\s*\[", lines[end]):
        end += 1
    key = None
    for k in range(start + 1, end):
        if re.match(r"^\s*exclude\s*=", lines[k]):
            key = k
            break
    fmt = "exclude = [%s]\n" % ", ".join(json.dumps(v, ensure_ascii=False) for v in values)
    if key is not None:
        last = key
        depth = lines[key].count("[") - lines[key].count("]")
        while depth > 0 and last + 1 < end:
            last += 1
            depth += lines[last].count("[") - lines[last].count("]")
        new = lines[:key] + ([fmt] if values else []) + lines[last + 1:]
    else:
        if not values:
            return raw
        at = end
        while at - 1 > start and lines[at - 1].strip() == "":
            at -= 1
        new = lines[:at] + [fmt] + lines[at:]
    return "".join(new)


def cmd_toggle(args):
    if not args:
        fail("用法: shuttle-scope toggle <相对路径>")
    target = args[0].strip().strip("/")
    parts = pathlib.PurePosixPath(target or ".").parts
    if not target or any(p in ("", ".", "..") for p in parts):
        fail("非法路径")
    name, sh, local = pick(None, args[1] if len(args) > 1 else None)
    if list(sh.get("include") or []):
        fail("该 shuttle 用了 include 白名单（更窄的选择集），请先在 TUI 里清除 include")
    if not (local / target).exists():
        fail(f"本地没有这一项: {target}")
    exclude = list(sh.get("exclude") or [])
    hit = ancestor_hit(target, exclude)
    if hit and hit != target:
        fail(f"上级目录已被排除（{hit}），要单独保留它请先取消上级")
    if hit == target:
        kept = [x for x in exclude if not (x == target or x.startswith(target + "/"))]
        action = "included"
    else:
        kept = exclude + [target]
        action = "excluded"
    raw, _doc = read_conf()
    kept = sorted(set(kept))
    text = patch_config(raw, name, kept)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    backup = CACHE_DIR / f"config.{time.strftime('%Y%m%d-%H%M%S')}.bak"
    backup.write_text(raw, encoding="utf-8")
    tmp = CONF.with_suffix(".toml.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(CONF)
    time.sleep(1.2)
    st = rsync_status(name)
    if "error" in st.get("raw", "").lower() and "config" in st.get("raw", "").lower():
        CONF.write_text(raw, encoding="utf-8")
        fail(f"配置被 Shuttle 拒绝，已回滚: {st.get('raw')}")
    out({"ok": True, "action": action, "exclude": kept, "status": st, "backup": str(backup)})
