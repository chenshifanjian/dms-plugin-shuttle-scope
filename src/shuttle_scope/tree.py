"""目录枚举：给面板逐层下钻用（目录优先、排除继承、可选统计占用）。"""

import os
import pathlib
import time

from .core import ancestor_hit, fail, fmt_bytes, out, pick, rel_join
from .sizes import load_sizes, measure, save_sizes


def list_dir(local_root, rel, exclude, sizes, measure_missing=False):
    """列出 local_root/rel 的直接子项（目录优先），带排除/继承状态与占用。"""
    base = (local_root / rel) if rel else local_root
    entries = []
    if not base.is_dir():
        return entries
    try:
        kids = sorted(os.scandir(base),
                      key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.casefold()))
    except OSError as e:
        fail(f"读不了本地目录 {base}: {e}")
    for e in kids:
        try:
            is_dir = e.is_dir(follow_symlinks=False)
        except OSError:
            continue
        name = e.name
        path = rel_join(rel, name)
        hit = ancestor_hit(path, exclude)
        # 点开头的是 Shuttle 元数据（.stignore 之类），只在它自己已被排除时才显示
        if name.startswith(".") and not hit:
            continue
        nested = any(x.startswith(path + "/") for x in exclude)
        c = sizes.get(path) or {}
        if not is_dir:
            try:
                stt = e.stat(follow_symlinks=False)
                c = {"bytes": stt.st_size, "files": 1}
            except OSError:
                c = {}
        elif measure_missing and not c:
            b, f = measure(e.path)
            try:
                mtime = int(e.stat(follow_symlinks=False).st_mtime)
            except OSError:
                mtime = 0
            c = {"bytes": b, "files": f, "mtime": mtime, "at": int(time.time())}
            sizes[path] = c
        entries.append({
            "name": name,
            "path": path,
            "dir": is_dir,
            "excluded": bool(hit),
            "inherited": bool(hit) and hit != path,
            "nested": nested,
            "bytes": c.get("bytes", 0),
            "files": c.get("files", 0),
            "measured": bool(c),
            "size": fmt_bytes(c["bytes"]) if c.get("bytes") else "",
        })
    return entries


def cmd_ls(args):
    if not args:
        fail("用法: shuttle-scope ls <相对路径> [shuttle] [--no-measure]")
    rel = args[0].strip().strip("/")
    if ".." in pathlib.PurePosixPath(rel or ".").parts:
        fail("非法路径")
    rest = [a for a in args[1:] if not a.startswith("--")]
    name, sh, local = pick(None, rest[0] if rest else None)
    exclude = list(sh.get("exclude") or [])
    sizes = load_sizes()
    entries = list_dir(local, rel, exclude, sizes, measure_missing="--no-measure" not in args)
    save_sizes(sizes)
    hit = ancestor_hit(rel, exclude) if rel else ""
    out({
        "ok": True,
        "shuttle": name,
        "path": rel,
        "parent": "/".join(rel.split("/")[:-1]) if rel else "",
        "exclude": exclude,
        "excluded": bool(hit),
        "excluded_by": hit if (hit and hit != rel) else "",
        "entries": entries,
    })
