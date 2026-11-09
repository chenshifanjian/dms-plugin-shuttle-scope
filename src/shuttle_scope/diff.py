"""云端 ⇄ 本地差异：远端索引 vs 本地索引，按目录层聚合，供面板逐层展示。

语义：
    remote_only  云端有、本地没有 —— 典型场景：本地删了但该处已被排除，Shuttle 不会再拉回来
    local_only   本地有、云端没有 —— 还没传上去，或被排除期间新建的
    differ       两边都有但大小不同（只比大小，不比内容；SMB 的 mtime 跨时区不可靠）
    same         两边同名同大小
目录行显示的是它整棵子树的聚合计数，所以「待分类」这一层就能看出云里多几个文件。
"""

import fnmatch
import os

from .core import ancestor_hit, fail, fmt_bytes, out, pick, read_doc, rel_join
from .remote import ensure_index, share_of

META_NAMES = {".stignore", ".DS_Store", "Thumbs.db", ".shuttle-versions", ".stfolder", ".stversions"}
DEFAULT_IGNORE = ["~$*", ".DS_Store", "*.tmp", "Thumbs.db"]


def ignore_patterns(sh):
    prof = sh.get("profile") or ""
    pats = list(DEFAULT_IGNORE)
    try:
        p = (read_doc().get("profiles") or {}).get(prof) or {}
        pats += list(p.get("ignore") or [])
    except Exception:  # noqa: BLE001
        pass
    return pats


def is_meta(name, patterns):
    if name in META_NAMES or name.startswith(".sync-conflict"):
        return True
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def is_meta_path(rel, patterns):
    """任一节是 Shuttle 元数据/配置忽略项（.shuttle-versions、~$xxx 之类）就跳过。"""
    return any(is_meta(seg, patterns) for seg in rel.split("/") if seg)


def build_local_index(root, patterns):
    """os.walk 一遍，返回 (files{rel:size}, dirs{rel})；不跟软链、跳过 Shuttle 元数据。"""
    files, dirs = {}, set()
    if not root.is_dir():
        return files, dirs
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if not is_meta(d, patterns)]
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        for d in dirnames:
            dirs.add(rel_join(rel_dir, d))
        for f in filenames:
            if is_meta(f, patterns):
                continue
            p = os.path.join(dirpath, f)
            try:
                if os.path.islink(p) or not os.path.isfile(p):
                    continue
                files[rel_join(rel_dir, f)] = os.stat(p, follow_symlinks=False).st_size
            except OSError:
                continue
    return files, dirs


def compare(local_files, remote_files):
    """-> {rel: status}，只含文件。"""
    st = {}
    for rel, size in local_files.items():
        rs = remote_files.get(rel)
        if rs is None:
            st[rel] = "local_only"
        elif rs == size:
            st[rel] = "same"
        else:
            st[rel] = "differ"
    for rel in remote_files:
        if rel not in local_files:
            st[rel] = "remote_only"
    return st


def _child_of(rel, prefix):
    """path 直接属于 prefix 的那一段名字（不是更深层）。"""
    tail = path_after(rel, prefix)
    if not tail:
        return ""
    i = tail.find("/")
    return tail if i < 0 else tail[:i]


def path_after(rel, prefix):
    if not prefix:
        return rel
    if rel == prefix:
        return ""
    if rel.startswith(prefix + "/"):
        return rel[len(prefix) + 1:]
    return ""


def level_view(rel, status, local_files, remote_files, local_dirs, remote_dirs,
               exclude, limit=400):
    """列 rel 的直接子项，目录给子树聚合计数，文件给自身状态。"""
    agg = {}   # child -> counters
    for p, s in status.items():
        child = _child_of(p, rel)
        if not child:
            continue
        c = agg.setdefault(child, {"remote_only": 0, "local_only": 0, "differ": 0, "same": 0,
                                   "remote_only_bytes": 0, "local_only_bytes": 0,
                                   "differ_bytes": 0, "files": 0})
        is_file = "/" not in path_after(p, rel)
        if is_file:
            c["files"] += 1
        c[s] += 1
        if s == "remote_only":
            c["remote_only_bytes"] += remote_files.get(p, 0)
        elif s == "local_only":
            c["local_only_bytes"] += local_files.get(p, 0)
        elif s == "differ":
            c["differ_bytes"] += remote_files.get(p, 0)

    names = set(agg)
    dir_names = {_child_of(d, rel) for d in (local_dirs | remote_dirs)} - {""}
    names |= dir_names

    entries = []
    for name in names:
        path = rel_join(rel, name)
        is_dir = name in dir_names
        # 同名既是文件又是目录时按目录处理（SMB 上不该出现，出现了也不崩）
        c = agg.get(name) or {"remote_only": 0, "local_only": 0, "differ": 0, "same": 0,
                              "remote_only_bytes": 0, "local_only_bytes": 0,
                              "differ_bytes": 0, "files": 0}
        if not is_dir:
            s = status.get(path, "")
            if not s:
                continue
            hit = ancestor_hit(path, exclude)
            entries.append({
                "name": name, "path": path, "dir": False,
                "status": s,
                "size_local": local_files.get(path, 0),
                "size_remote": remote_files.get(path, 0),
                "size": fmt_bytes(remote_files.get(path, local_files.get(path, 0))),
                "excluded": bool(hit), "inherited": bool(hit) and hit != path,
            })
            continue
        hit = ancestor_hit(path, exclude)
        entries.append({
            "name": name, "path": path, "dir": True,
            "remote_only": c["remote_only"], "local_only": c["local_only"],
            "differ": c["differ"], "same": c["same"], "files": c["files"],
            "remote_only_bytes": c["remote_only_bytes"],
            "local_only_bytes": c["local_only_bytes"],
            "differ_bytes": c["differ_bytes"],
            "excluded": bool(hit), "inherited": bool(hit) and hit != path,
            "in_local": path in local_dirs, "in_remote": path in remote_dirs,
            "diff_count": c["remote_only"] + c["local_only"] + c["differ"],
        })

    def sort_key(e):
        if e["dir"]:
            return (0, -(e.get("diff_count") or 0), e["name"].casefold())
        return (1, 0 if e.get("status") == "same" else -1, e["name"].casefold())

    entries.sort(key=sort_key)
    return entries[:limit], len(entries)


def summarize(rel, status, local_files, remote_files):
    """当前层整棵子树的差异账。"""
    s = {"remote_only": 0, "local_only": 0, "differ": 0, "same": 0,
         "remote_only_bytes": 0, "local_only_bytes": 0, "differ_bytes": 0,
         "remote_files": 0, "local_files": 0}
    prefix = rel + "/" if rel else ""
    for p, st in status.items():
        if prefix and not p.startswith(prefix):
            continue
        s[st] += 1
        if st == "remote_only":
            s["remote_only_bytes"] += remote_files.get(p, 0)
        elif st == "local_only":
            s["local_only_bytes"] += local_files.get(p, 0)
        elif st == "differ":
            s["differ_bytes"] += remote_files.get(p, 0)
    s["remote_files"] = sum(1 for p in remote_files if not prefix or p.startswith(prefix))
    s["local_files"] = sum(1 for p in local_files if not prefix or p.startswith(prefix))
    return s


def cmd_diff(args):
    rel = ""
    refresh = "--refresh" in args
    quick = "--quick" in args
    positional = [a for a in args if not a.startswith("--")]
    if "--path" in args:
        i = args.index("--path")
        if i + 1 < len(args):
            rel = args[i + 1].strip().strip("/")
            positional = [x for x in positional if x != rel]
    elif positional:
        rel = positional.pop(0).strip().strip("/")
    name, sh, local = pick(None, positional[0] if positional else None)
    if not local.is_dir():
        fail(f"本地同步根不存在: {local}")
    exclude = list(sh.get("exclude") or [])

    try:
        cache, listed = ensure_index(sh, refresh=refresh, allow_stale=True)
    except Exception as e:  # noqa: BLE001
        fail(f"读不到云端清单: {e}")

    remote_files = cache.get("files") or {}
    remote_dirs = set(cache.get("dirs") or [])
    patterns = ignore_patterns(sh)
    remote_files = {p: s for p, s in remote_files.items() if not is_meta_path(p, patterns)}
    remote_dirs = {d for d in remote_dirs if not is_meta_path(d, patterns)}
    local_files, local_dirs = build_local_index(local, patterns)
    status = compare(local_files, remote_files)

    entries, total_entries = level_view(rel, status, local_files, remote_files,
                                        local_dirs, remote_dirs, exclude)
    summary = summarize(rel, status, local_files, remote_files)
    host, share, _user, _sub = share_of(sh)
    # 这一层是被哪条排除规则盖住的（含恰好命中自身）
    hit = ""
    if rel:
        for p in exclude:
            q = p.strip("/")
            if q and (rel == q or rel.startswith(q + "/")):
                hit = q
                break
    out({
        "ok": True,
        "shuttle": name,
        "path": rel,
        "exclude": exclude,
        "excluded": bool(hit),
        "excluded_by": hit,
        "share": f"{host}/{share}",
        "remote": {
            "at": cache.get("at"),
            "age": int(__import__("time").time() - cache.get("at", 0)),
            "files": len(remote_files),
            "dirs": len(remote_dirs),
            "bytes": sum(remote_files.values()),
            "stale": bool(cache.get("stale")),
            "warning": cache.get("warning", ""),
            "listed_now": listed,
            "seconds": cache.get("fresh_seconds"),
        },
        "summary": summary,
        "entries": entries,
        "entries_total": total_entries,
        "local_root": str(local),
    })
