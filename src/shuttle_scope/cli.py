"""命令行入口：state / ls / diff / sizes / toggle / sync / control / status。"""

import subprocess
import sys

from . import __version__
from .config import cmd_toggle
from .connect import (cmd_local_set, cmd_schedule_set, cmd_schedule_show, cmd_share_set,
                      cmd_share_show)
from .core import CONF, SHUTTLE, fail, out, pick, rsync_status
from .diff import cmd_diff
from .sizes import cmd_sizes, load_sizes
from .tree import cmd_ls, list_dir


def cmd_state(args):
    name, sh, local = pick(None, args[0] if args else None)
    exclude = list(sh.get("exclude") or [])
    include = list(sh.get("include") or [])
    prof = sh.get("profile") or None
    profiles = _profiles()
    prof_settings = profiles.get(prof, {}) if prof else {}
    entries = list_dir(local, "", exclude, load_sizes())
    out({
        "ok": True,
        "version": __version__,
        "shuttle": name,
        "local": str(local),
        "remote": sh.get("remote", ""),
        "profile": prof_settings.get("mode", ""),
        "triggers": prof_settings.get("triggers", []),
        "exclude": exclude,
        "include": include,
        "status": rsync_status(name),
        "entries": entries,
        "conf": str(CONF),
    })


def _profiles():
    from .core import read_doc

    return read_doc().get("profiles") or {}


def cmd_sync(args):
    name, _sh, _local = pick(None, args[0] if args else None)
    try:
        p = subprocess.run([SHUTTLE, "sync", name], capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        # 超时不能抛 traceback：面板只认单行 JSON
        fail(f"shuttle sync 超过 120 秒没跑完（大文件或 NAS 慢），已放弃这次调用")
    except OSError as e:
        fail(f"调不起 shuttle：{e}")
    out({"ok": p.returncode == 0, "code": p.returncode,
         "output": ((p.stdout or "") + (p.stderr or "")).strip()[:400]})


def cmd_control(args):
    if not args:
        fail("用法: shuttle-scope control <sync|pause|resume|enable|disable> [shuttle]")
    verb = args[0]
    if verb not in ("sync", "pause", "resume", "enable", "disable"):
        fail(f"不支持的动词: {verb}")
    name = args[1] if len(args) > 1 else pick(None, None)[0]
    try:
        p = subprocess.run([SHUTTLE, verb, name], capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        fail(f"shuttle {verb} 超过 180 秒没跑完，已放弃这次调用")
    except OSError as e:
        fail(f"调不起 shuttle：{e}")
    out({"ok": p.returncode == 0, "code": p.returncode,
         "output": ((p.stdout or "") + (p.stderr or "")).strip()[:400],
         "status": rsync_status(name)})


USAGE = """shuttle-scope <命令>，所有输出都是单行 JSON：
  state [shuttle]                同步状态 + 顶层条目
  ls <相对路径> [shuttle]        列某一层的直接子项（面板下钻用）
  diff [相对路径] [--refresh]    云端 ⇄ 本地差异（--refresh 强制重扫共享）
  sizes [--path <相对路径>] [--force]   统计占用并写缓存
  toggle <相对路径> [shuttle]    翻转某一项的排除状态
  share show                     当前 SMB 地址/账号（密码只看“存没存”）
  share set --url <地址> [--user 账号] [--password-env VAR] [--no-test]
                                 改地址/账号/密码：先测连，再写配置 + keyring
  local set --path <本机目录> [--force]
                                 改同步落地目录；空目录会提示整批下载
                                 上下级/嵌套路径默认拒绝，确认后加 --force
  schedule show                  同步时间（触发方式/间隔/每天几点）
  schedule set --triggers a,b [--poll 60s] [--interval 6h] [--at "02:00,14:30"]
                                 空串＝删掉该项；triggers 空＝只手动
  sync | control <动词> | status
  remote [--refresh]             只看云端索引（调试）
版本 {v}""".format(v=__version__)


def main(argv=None):
    """任何没预料到的异常都转成单行 JSON，别把 traceback 甩给面板。"""
    try:
        return _main(argv)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        fail(f"内部错误：{type(e).__name__}: {e}")


def _main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0 if argv else 1
    cmd, args = argv[0], argv[1:]
    if cmd == "state":
        cmd_state(args)
    elif cmd == "ls":
        cmd_ls(args)
    elif cmd == "diff":
        cmd_diff(args)
    elif cmd == "sizes":
        cmd_sizes(args)
    elif cmd == "toggle":
        cmd_toggle(args)
    elif cmd == "sync":
        cmd_sync(args)
    elif cmd == "control":
        cmd_control(args)
    elif cmd == "share":
        sub = args[0] if args else "show"
        if sub == "show":
            cmd_share_show(args[1:])
        elif sub == "set":
            cmd_share_set(args[1:])
        else:
            fail(f"未知子命令: share {sub}")
    elif cmd == "local":
        sub = args[0] if args else "show"
        if sub == "show":
            cmd_share_show(args[1:])          # share show 里就带 local
        elif sub == "set":
            cmd_local_set(args[1:])
        else:
            fail(f"未知子命令: local {sub}")
    elif cmd == "schedule":
        sub = args[0] if args else "show"
        if sub == "show":
            cmd_schedule_show(args[1:])
        elif sub == "set":
            cmd_schedule_set(args[1:])
        else:
            fail(f"未知子命令: schedule {sub}")
    elif cmd == "status":
        name, _sh, _local = pick(None, args[0] if args else None)
        out({"ok": True, "shuttle": name, "status": rsync_status(name)})
    elif cmd == "remote":
        from .remote import cmd_remote

        cmd_remote(args)
    elif cmd == "version":
        out({"ok": True, "version": __version__})
    else:
        fail(f"未知命令: {cmd}")


if __name__ == "__main__":
    sys.exit(main())
