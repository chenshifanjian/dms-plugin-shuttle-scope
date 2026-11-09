"""本地目录占用统计与缓存（sizes.json，按相对路径为键）。"""

import json
import os
import time

from .core import CACHE_DIR, SIZES_CACHE, fail, fmt_bytes, pick, rel_join, write_json_atomic


def load_sizes():
    try:
        return json.loads(SIZES_CACHE.read_text())
    except Exception:  # noqa: BLE001
        return {}


def save_sizes(d):
    write_json_atomic(SIZES_CACHE, d)


def measure(path):
    """递归统计字节数与文件数（不跟随软链）。"""
    total = 0
    files = 0
    stack = [str(path)]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    if e.is_symlink():
                        continue
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    elif e.is_file(follow_symlinks=False):
                        total += e.stat(follow_symlinks=False).st_size
                        files += 1
                except OSError:
                    continue
    return total, files


def cmd_sizes(args):
    path = ""
    if "--path" in args:
        i = args.index("--path")
        if i + 1 < len(args):
            path = args[i + 1].strip().strip("/")
        del args[i:i + 2]
    name, sh, local = pick(None, args[0] if args else None)
    force = "--force" in args
    sizes = load_sizes()
    changed = 0
    base = (local / path) if path else local
    if base.is_dir():
        for e in sorted(os.scandir(base), key=lambda x: x.name):
            try:
                if not e.is_dir(follow_symlinks=False):
                    continue
                mtime = int(e.stat(follow_symlinks=False).st_mtime)
            except OSError:
                continue
            key = rel_join(path, e.name)
            c = sizes.get(key) or {}
            if force or c.get("bytes") is None or c.get("mtime") != mtime or time.time() - c.get("at", 0) > 21600:
                b, f = measure(e.path)
                sizes[key] = {"bytes": b, "files": f, "mtime": mtime, "at": int(time.time())}
                changed += 1
    save_sizes(sizes)
    from .core import out

    out({"ok": True, "path": path, "changed": changed,
         "sizes": {k: {"bytes": v["bytes"], "files": v["files"], "size": fmt_bytes(v["bytes"])}
                   for k, v in sizes.items()}})
