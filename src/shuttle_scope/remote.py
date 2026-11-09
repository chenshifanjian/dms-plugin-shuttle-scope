"""远端（NAS SMB 共享）索引：用 smbclient 递归列目录，凭据来自系统 keyring。

为什么不挂载：Shuttle 自己走原生 SMB2/3，不需要 mount.cifs；本工具只是“顺路”读一次
共享的文件清单，用来和本地对比。密码从 keyring 取（service=shuttle，
username=smb://<user>@<host>/<share>），落到 0600 的临时 auth 文件，进程结束即删。
"""

import os
import pathlib
import re
import shutil
import subprocess
import time

from .core import CACHE_DIR, REMOTE_CACHE, fail, fmt_bytes, out, read_doc, write_json_atomic

SMB = shutil.which("smbclient")
TTL = 600

#   name                                 A 16301532418  Sun Nov  8 12:02:04 2026
_LINE = re.compile(
    r"^\s+(?P<name>.+?)\s+(?P<attr>[ADNHRST]{1,8})\s+(?P<size>\d+)\s+"
    r"(?P<date>\w{3}\s+\w{3}\s+\d+\s+[\d:]+\s+\d{4})\s*$"
)


def share_of(sh):
    """[shuttles.*].remote = "zspace:." -> (host, share, user, 子路径)。"""
    remote = str(sh.get("remote", ""))
    name, _, sub = remote.partition(":")
    s = (read_doc().get("shares") or {}).get(name) or {}
    return s.get("host", ""), s.get("share", ""), s.get("user", ""), sub.strip("/")


def keyring_password(host, share, user):
    """按 Shuttle 自己用的用户名取密码；取不到就抛错（不猜、不落盘）。"""
    url = f"smb://{user}@{host}/{share}"
    try:
        p = subprocess.run(["secret-tool", "lookup", "service", "shuttle", "username", url],
                           capture_output=True, text=True, timeout=15)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"secret-tool 调用失败: {e}") from e
    pw = (p.stdout or "").strip("\n")
    if p.returncode != 0 or not pw:
        raise RuntimeError(f"keyring 里没有 {url} 的密码（shuttle 自己能同步说明它另有一份）")
    return pw


def _auth_file(user, password):
    runtime = pathlib.Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")
    path = runtime / f"shuttle-scope-{os.getpid()}.auth"
    path.write_text(f"username = {user}\npassword = {password}\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def parse_listing(text, base=""):
    """解析 `recurse ON; ls` 的输出 -> (files{rel:size}, dirs{rel})。

    递归时每个子目录前有一行 `\\父\\子`（反斜杠分隔，相对起始目录）。
    """
    files, dirs = {}, {}
    cur = base
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if not line.strip():
            continue
        if line.startswith("\\"):
            rel = line.strip()[1:].replace("\\", "/")
            cur = f"{base}/{rel}" if base else rel
            dirs[cur] = 0
            continue
        m = _LINE.match(line)
        if not m:
            continue
        name = m.group("name").strip()
        if name in (".", ".."):
            continue
        rel = f"{cur}/{name}" if cur else name
        if "D" in m.group("attr"):
            dirs[rel] = 0
        else:
            files[rel] = int(m.group("size"))
    return files, dirs


def list_share(host, share, user, timeout=300):
    """递归列整个共享，返回 (files, dirs, seconds)。"""
    if not SMB:
        raise RuntimeError("没装 smbclient（pacman -S smbclient）")
    pw = keyring_password(host, share, user)
    auth = _auth_file(user, pw)
    try:
        cmd = [SMB, f"//{host}/{share}", "-A", str(auth), "-c", "recurse ON; prompt OFF; ls"]
        t0 = time.time()
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0:
            msg = (p.stderr or p.stdout or "").strip().splitlines()
            raise RuntimeError(f"smbclient 失败: {msg[-1] if msg else p.returncode}")
        files, dirs = parse_listing(p.stdout or "")
        return files, dirs, round(time.time() - t0, 1)
    finally:
        try:
            auth.unlink()
        except OSError:
            pass


def load_cache(host="", share=""):
    import json

    try:
        d = json.loads(REMOTE_CACHE.read_text())
    except Exception:  # noqa: BLE001
        return None
    if host and d.get("host") and d["host"] != host:
        return None
    if share and d.get("share") and d["share"] != share:
        return None
    return d


def save_cache(host, share, files, dirs):
    write_json_atomic(REMOTE_CACHE, {
        "at": int(time.time()),
        "host": host,
        "share": share,
        "count": len(files),
        "files": files,
        "dirs": sorted(dirs),
    })


def ensure_index(sh, refresh=False, allow_stale=True):
    """拿远端索引：缓存新鲜就直接用；否则重列（refresh=True 强制重列）。"""
    host, share, user, _sub = share_of(sh)
    if not host or not share:
        raise RuntimeError("配置里 [shares.*] 缺 host/share，无法定位共享")
    cache = load_cache(host, share)
    fresh = bool(cache) and (time.time() - cache.get("at", 0) < TTL)
    if cache and fresh and not refresh:
        return cache, False
    if cache and not refresh and allow_stale:
        # 网络慢/离线时先用旧索引，别让面板空着
        try:
            files, dirs, secs = list_share(host, share, user)
        except Exception:
            cache["stale"] = True
            return cache, False
        save_cache(host, share, files, dirs)
        cache = load_cache(host, share)
        cache["fresh_seconds"] = secs
        return cache, True
    try:
        files, dirs, secs = list_share(host, share, user)
    except Exception as e:  # noqa: BLE001
        if cache:
            cache["stale"] = True
            cache["warning"] = str(e)
            return cache, False
        raise
    save_cache(host, share, files, dirs)
    cache = load_cache(host, share)
    cache["fresh_seconds"] = secs
    return cache, True


def cmd_remote(args):
    """调试用：只列远端，输出统计。"""
    from .core import pick

    name, sh, _local = pick(None, args[0] if args else None)
    refresh = "--refresh" in args
    cache, listed = ensure_index(sh, refresh=refresh)
    files = cache.get("files") or {}
    total = sum(files.values())
    out({"ok": True, "shuttle": name, "listed": listed, "at": cache.get("at"),
         "files": len(files), "dirs": len(cache.get("dirs") or []),
         "bytes": total, "size": fmt_bytes(total), "seconds": cache.get("fresh_seconds"),
         "stale": bool(cache.get("stale")), "warning": cache.get("warning", "")})
