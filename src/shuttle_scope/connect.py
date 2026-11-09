"""连接与定时：SMB 地址/账号/密码 → Shuttle 配置 + 系统 keyring；同步时间 → profile。

两条边界，写死在代码里：
  * 密码只进 keyring（service=shuttle，username=smb://<user>@<host>/<share>），
    绝不落到 config.toml、日志或 argv（QML 用环境变量递进来，读完即删）。
  * 改地址/账号前先测连（smbclient 真连一次）；测不通就不动配置。
"""

import json
import os
import pathlib
import re
import shutil
import subprocess
import time

from .core import CACHE_DIR, CONF, HOME, fail, out, pick, read_conf, read_doc, rsync_status

SMB = shutil.which("smbclient")
SERVICE = "shuttle"
TRIGGERS = ("realtime", "schedule", "share-available")
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


# ── 解析 ────────────────────────────────────────────────────────────────

def clean_password(text):
    """密码框里粘贴常带结尾换行。测连和存 keyring 必须用同一个值，
    否则会出现「测通了，真挂载认证失败」。"""
    return str(text or "").replace("\r", "").replace("\n", "")


def parse_url(text, user=None):
    """把用户粘贴的地址拆成 host / share / 子路径 / 账号。

    认：smb://host/share[/sub]、//host/share、\\\\host\\share、host/share；
    也认 smb://user@host/share 与 host:445/share。地址里不许带密码。
    """
    s = (text or "").strip()
    if not s:
        raise ValueError("地址不能为空，例如 smb://10.0.0.2/团队文件")
    low = s.lower()
    if low.startswith("smb://"):
        s = s[6:]
    elif s.startswith("\\\\"):
        s = s[2:].replace("\\", "/")
    elif s.startswith("//"):
        s = s[2:]
    s = s.strip("/")
    if not s:
        raise ValueError("地址里缺少主机名")

    authority, _, rest = s.partition("/")
    if "@" in authority:
        who, _, authority = authority.rpartition("@")
        if ":" in who:
            raise ValueError("密码请不要写进地址，填到密码框里（也只进 keyring）")
        user = user or who
    host, port = authority, 0
    if ":" in authority:
        h, _, p = authority.rpartition(":")
        if p.isdigit():
            host, port = h, int(p)
    if not host:
        raise ValueError("地址里缺少主机名")
    parts = [p for p in rest.split("/") if p]
    share = parts[0] if parts else ""
    if not share:
        raise ValueError("地址里要带共享名，例如 smb://192.168.1.10/团队文件-项目文档")
    return {"host": host, "port": port, "share": share, "sub": "/".join(parts[1:]), "user": user or ""}


def url_of(host, share, user, port=0):
    """Shuttle 取 keyring 用的那串：smb://<user>@<host>/<share>（不带端口/密码）。"""
    return f"smb://{user}@{host}/{share}"


def parse_seconds(text):
    """'60s' / '5m' / '1h30m' / '2d' / '90' → 秒；空串 → 0；非法 → ValueError。"""
    s = (text or "").strip()
    if not s:
        return 0
    if s.isdigit():
        return int(s)
    total, i = 0, 0
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*([smhdwSMHDW])", s):
        if m.start() != i:
            raise ValueError(f"时间格式不认识：{text}（用 30s / 5m / 1h / 1d）")
        total += float(m.group(1)) * _UNIT[m.group(2).lower()]
        i = m.end()
    if i != len(s) or total <= 0:
        raise ValueError(f"时间格式不认识：{text}（用 30s / 5m / 1h / 1d）")
    return int(total)


def human_seconds(seconds):
    seconds = int(seconds or 0)
    parts = []
    for unit, name in ((86400, "天"), (3600, "小时"), (60, "分钟"), (1, "秒")):
        if seconds >= unit:
            n, seconds = divmod(seconds, unit)
            parts.append(f"{n} {name}")
    return " ".join(parts) or "0 秒"


def normalize_at(text):
    """'2:00, 14:30' → ['02:00', '14:30']；非法 → ValueError。"""
    out_at = []
    for raw in re.split(r"[,，\s]+", (text or "").strip()):
        if not raw:
            continue
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
        if not m:
            raise ValueError(f"时间点要写成 HH:MM（24 小时制），不认识：{raw}")
        h, mi = int(m.group(1)), int(m.group(2))
        if h > 23 or mi > 59:
            raise ValueError(f"时间点越界：{raw}")
        v = f"{h:02d}:{mi:02d}"
        if v not in out_at:
            out_at.append(v)
    return sorted(out_at)


def schedule_summary(triggers, poll, interval, at):
    """给人看的一句话。"""
    bits = []
    if "realtime" in triggers:
        bits.append(f"实时（每 {human_seconds(parse_seconds(poll) or 60)}查一次共享）")
    if "schedule" in triggers:
        sub = []
        if interval:
            sub.append(f"每 {human_seconds(parse_seconds(interval))}")
        if at:
            sub.append("每天 " + "、".join(at))
        bits.append("定时" + ("：" + " · ".join(sub) if sub else "（还没设时间）"))
    if "share-available" in triggers:
        bits.append("共享上线时补跑")
    return "；".join(bits) if bits else "手动（只有点「立即同步」才跑）"


# ── TOML 文本手术（保留注释与对齐） ──────────────────────────────────────

def _section_bounds(lines, table):
    head = re.compile(r"^\s*\[%s\]\s*(?:#.*)?$" % re.escape(table))
    start = next((i for i, ln in enumerate(lines) if head.match(ln.strip())), None)
    if start is None:
        raise ValueError(f"找不到 [{table}]")
    end = start + 1
    while end < len(lines) and not re.match(r"^\s*\[", lines[end]):
        end += 1
    return start, end


def set_key(raw, table, key, literal):
    """改/加/删 [table] 里的一行。literal=None 表示删掉。返回新文本。"""
    lines = raw.splitlines(keepends=True)
    start, end = _section_bounds(lines, table)
    pat = re.compile(r"^\s*%s\s*=" % re.escape(key))
    hit = next((i for i in range(start + 1, end) if pat.match(lines[i])), None)
    if hit is None:
        if literal is None:
            return raw
        at = end
        while at - 1 > start and lines[at - 1].strip() == "":
            at -= 1
        line = f"{key} = {literal}\n"
        if not lines[at - 1].endswith("\n"):
            line = "\n" + line
        return "".join(lines[:at] + [line] + lines[at:])
    last = hit
    depth = lines[hit].count("[") - lines[hit].count("]")
    while depth > 0 and last + 1 < end:
        last += 1
        depth += lines[last].count("[") - lines[last].count("]")
    if literal is None:
        return "".join(lines[:hit] + lines[last + 1:])
    same = re.match(r"^\s*%s\s*=\s*%s\s*(?:#.*)?$" % (re.escape(key), re.escape(literal)),
                    lines[hit].rstrip("\n"))
    if same:
        return raw                       # 值和现在一样：连注释都不动
    # 保留 key 与 = 之间的对齐空格
    m = re.match(r"^(\s*%s\s*=\s*)" % re.escape(key), lines[hit])
    return "".join(lines[:hit] + [m.group(1) + literal + "\n"] + lines[last + 1:])


def toml_str(s):
    return json.dumps(s, ensure_ascii=False)


def toml_list(items):
    return "[" + ", ".join(toml_str(x) for x in items) + "]"


def _prune_backups(keep=20):
    try:
        for old in sorted(CACHE_DIR.glob("config.*.bak"))[:-keep]:
            old.unlink()
    except OSError:
        pass


def _write_conf(raw, text, what, name):
    """备份 → 原子替换 → 复核；失败回滚。文本没变就完全不落盘。"""
    if text == raw:
        return rsync_status(name), None
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S") + "-%06d" % (time.time_ns() // 1000 % 1000000)
    backup = CACHE_DIR / f"config.{stamp}.bak"
    backup.write_text(raw, encoding="utf-8")
    tmp = CONF.with_suffix(".toml.tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        tmp.replace(CONF)
    except Exception:
        CONF.write_text(raw, encoding="utf-8")
        raise
    time.sleep(1.0)
    st = rsync_status(name)
    raw_st = (st.get("raw") or "").lower()
    if "config" in raw_st and ("error" in raw_st or "invalid" in raw_st or "reject" in raw_st):
        CONF.write_text(raw, encoding="utf-8")
        fail(f"Shuttle 拒绝了新配置（{what}），已回滚")
    _prune_backups()
    return st, backup


def _reload_daemon():
    """配置真的改了才叫守护进程重读：realtime watcher 是启动时按配置挂的，
    不重启的话它还盯着旧目录，新目录要等下次重启才被盯上。"""
    try:
        p = subprocess.run(["systemctl", "--user", "try-restart", "shuttle.service"],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0


# ── keyring ────────────────────────────────────────────────────────────

def keyring_lookup(url):
    try:
        p = subprocess.run(["secret-tool", "lookup", "service", SERVICE, "username", url],
                           capture_output=True, text=True, timeout=15)
    except Exception:  # noqa: BLE001
        return None
    pw = p.stdout or ""
    pw = pw[:-1] if pw.endswith("\n") else pw
    return pw or None


def keyring_store(url, password):
    password = clean_password(password)     # 末尾换行会一起存进去
    subprocess.run(["secret-tool", "clear", "service", SERVICE, "username", url],
                   capture_output=True, timeout=15)
    p = subprocess.run(["secret-tool", "store", "--label", f"shuttle: {url}",
                        "service", SERVICE, "username", url],
                       input=password.encode(), capture_output=True, timeout=20)
    if p.returncode != 0:
        raise RuntimeError(f"写 keyring 失败：{(p.stderr or b'').decode(errors='replace').strip()}")


def keyring_clear(url):
    subprocess.run(["secret-tool", "clear", "service", SERVICE, "username", url],
                   capture_output=True, timeout=15)


# ── 真连一次 ────────────────────────────────────────────────────────────

def test_smb(host, share, user, password, port=0, timeout=30):
    """smbclient 列一眼共享根目录。返回 (ok, 说明)。密码只经 0600 文件，用完即删。"""
    if not SMB:
        return True, "没装 smbclient，跳过实测"
    runtime = pathlib.Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")
    auth = runtime / f"shuttle-scope-{os.getpid()}-{int(time.time())}.auth"
    # smbclient 的 -A 文件不认引号，原样写；账号/密码里的换行会拼出额外一行，先掐掉
    user = clean_password(user)
    password = clean_password(password)
    try:
        auth.write_text(f"username = {user}\npassword = {password}\n", encoding="utf-8")
        auth.chmod(0o600)
        target = f"//{host}/{share}" if not port else f"//{host}:{port}/{share}"
        p = subprocess.run([SMB, target, "-A", str(auth), "-c", "ls"], capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"连 {host} 超时（{timeout} 秒没回音：地址/网络/防火墙）"
    except OSError as e:
        return False, f"跑 smbclient 失败：{e}"
    finally:
        try:
            auth.unlink()
        except OSError:
            pass
    text = ((p.stdout or "") + (p.stderr or "")).strip()
    if p.returncode == 0:
        n = sum(1 for ln in text.splitlines() if re.search(r"\s[ADNHRST]{1,8}\s+\d+\s+\w{3}\s", ln))
        return True, f"连接正常（共享里看到 {n} 项）"
    low = text.lower()
    if "logon_failure" in low or "access_denied" in low:
        return False, "账号或密码不对（NT_STATUS_LOGON_FAILURE）"
    if "bad_network_name" in low or "object_name_not_found" in low:
        return False, f"这台上没有名为「{share}」的共享"
    if "connection refused" in low or "timed out" in low or "no route" in low:
        return False, f"连不上 {host}（地址/网络/防火墙）"
    first = next((ln for ln in text.splitlines() if "smb.conf" not in ln), "连不上")
    return False, first[:160]


# ── share ──────────────────────────────────────────────────────────────

def _target():
    name, sh, local = pick(None, os.environ.get("SHUTTLE_NAME") or None)
    remote = str(sh.get("remote", ""))
    entry, _, sub = remote.partition(":")
    doc = read_doc()
    s = (doc.get("shares") or {}).get(entry)
    if not s:
        fail(f"[shuttles.{name}].remote 指向的共享 {entry} 在 [shares] 里不存在")
    sub = sub.strip("/")
    return name, sh, local, entry, s, "" if sub == "." else sub


def cmd_share_show(args):
    name, sh, local, entry, s, sub = _target()
    url = url_of(s.get("host", ""), s.get("share", ""), s.get("user", ""))
    out({
        "ok": True,
        "shuttle": name,
        "entry": entry,
        "type": s.get("type", "smb"),
        "host": s.get("host", ""),
        "port": s.get("port", 0) or 0,
        "share": s.get("share", ""),
        "user": s.get("user", ""),
        "sub": sub,
        "url": url,
        "local": str(local),
        "password_stored": bool(keyring_lookup(url)),
        "profile": sh.get("profile", ""),
    })


def cmd_share_set(args):
    url_in = _flag(args, "--url")
    if not url_in:
        fail("用法: shuttle-scope share set --url smb://host/share [--user 账号] [--password-env VAR] [--no-test]")
    user_in = _flag(args, "--user") or ""
    pw_env = _flag(args, "--password-env") or ""
    pw = os.environ.get(pw_env, "") if pw_env else ""
    if pw_env and pw_env in os.environ:
        del os.environ[pw_env]  # 读完就删，别让它活到日志/子进程里
    pw = clean_password(pw)
    test = "--no-test" not in args

    name, sh, local, entry, s, sub = _target()
    try:
        parsed = parse_url(url_in, user_in or s.get("user", ""))
    except ValueError as e:
        fail(str(e))
    host, share, new_sub = parsed["host"], parsed["share"], parsed["sub"]
    user = (user_in or parsed["user"] or s.get("user", "")).strip()
    if not user:
        fail("账号不能为空")
    port = parsed["port"] or 0
    old_url = url_of(s.get("host", ""), s.get("share", ""), s.get("user", ""))
    new_url = url_of(host, share, user, port)
    old_pw = keyring_lookup(old_url)

    # 1) 先用“新密码，或 keyring 里已有的新地址密码”真连一次
    probe_pw = pw or keyring_lookup(new_url) or (old_pw if new_url == old_url else None)
    probe_note = ""
    if test:
        if not probe_pw:
            fail("改了地址/账号就得给一次密码：keyring 里没有新地址的凭据")
        ok, probe_note = test_smb(host, share, user, probe_pw, port)
        if not ok:
            fail(f"连接失败，配置没动：{probe_note}")

    raw, doc = read_conf()
    table = f"shares.{entry}"
    text = raw
    text = set_key(text, table, "type", toml_str("smb"))
    text = set_key(text, table, "host", toml_str(host))
    text = set_key(text, table, "share", toml_str(share))
    text = set_key(text, table, "user", toml_str(user))
    text = set_key(text, table, "port", str(port) if port else None)
    # 地址里带子路径 → 改 remote 的冒号后半截；不带就保留原来的
    if new_sub and new_sub != sub:
        text = set_key(text, f"shuttles.{name}", "remote", toml_str(f"{entry}:{new_sub}"))

    import tomllib
    want = {"host": host, "share": share, "user": user}
    try:
        check = tomllib.loads(text)
        got = (check.get("shares") or {}).get(entry) or {}
    except Exception as e:  # noqa: BLE001
        fail(f"改完的配置解析失败，没写盘：{e}")
    for k, v in want.items():
        if got.get(k) != v:
            fail(f"改完复核不一致（{k}），没写盘")

    st, backup = _write_conf(raw, text, "SMB 地址", name)
    try:
        if pw:
            keyring_store(new_url, pw)
        if old_url != new_url and old_pw and not _url_in_use(old_url):
            keyring_clear(old_url)
    except Exception as e:  # noqa: BLE001
        CONF.write_text(raw, encoding="utf-8")
        if old_pw:
            keyring_store(old_url, old_pw)
        fail(f"写 keyring 失败，配置已回滚：{e}")

    restarted = _reload_daemon() if backup else False
    out({
        "daemon_restarted": restarted,
        "ok": True, "shuttle": name, "entry": entry, "host": host, "port": port,
        "share": share, "user": user, "sub": new_sub or sub, "url": new_url,
        "password_stored": bool(keyring_lookup(new_url)), "tested": test,
        "message": "已保存" + ("，" + probe_note if probe_note else ""), "status": st, "backup": str(backup or ""),
    })


def _url_in_use(url):
    """keyring 里这条地址还有别的 [shares.*] 在用吗？"""
    for s in (read_doc().get("shares") or {}).values():
        if url_of(s.get("host", ""), s.get("share", ""), s.get("user", "")) == url:
            return True
    return False


# ── local（本机目录） ───────────────────────────────────────────────────

def _is_within(child, parent):
    """child 是不是 parent 里面的路径（两端都要是绝对路径）。"""
    try:
        pathlib.Path(child).relative_to(pathlib.Path(parent))
        return True
    except ValueError:
        return False


def normalize_local(text, old=None, force=False):
    """用户填的本机目录 → 规范绝对路径；明显危险的写法直接拒绝。

    拒绝：空、相对路径、根目录、家目录、指向文件；
    以及新旧目录互为上下级（会把旧同步目录套进去 / 变成嵌套同步），
    这两种只有加 --force 才放行。
    """
    s = (text or "").strip().rstrip("/")
    if not s:
        raise ValueError("本机目录不能为空，例如 /home/你/Documents/smb")
    p = pathlib.Path(os.path.expanduser(s))
    if not p.is_absolute():
        raise ValueError(f"要用绝对路径，别写相对路径：{s}")
    p = pathlib.Path(os.path.realpath(str(p)))
    if str(p) == "/":
        raise ValueError("不能把根目录当同步目录，会把整台机器都推到 NAS 上")
    if p == pathlib.Path(HOME):
        raise ValueError("不能用家目录当同步目录，会把整个家都推到 NAS 上")
    if p.exists() and not p.is_dir():
        raise ValueError(f"{p} 已经是个文件，不是目录")
    if old and not force:
        o = pathlib.Path(os.path.realpath(str(old)))
        if o != p and _is_within(o, p):
            raise ValueError("新目录是当前目录的上级，会把旧同步目录一起套进去；确认要这么做加 --force")
        if o != p and _is_within(p, o):
            raise ValueError("新目录在当前同步目录里面，会变成嵌套同步；确认要这么做加 --force")
    return p


def _remote_count():
    """云端索引里有多少个文件（只读缓存，不联网）；拿不到返回 0。"""
    try:
        d = json.loads((CACHE_DIR / "remote.json").read_text(encoding="utf-8"))
        return int(d.get("count") or 0)
    except Exception:  # noqa: BLE001
        return 0


def cmd_local_set(args):
    """改 [shuttles.<name>].local —— 同步落地的本机目录。"""
    path_in = _flag(args, "--path")
    force = "--force" in args
    name, sh, _local, _entry, _s, _sub = _target()
    old_raw = str(sh.get("local") or "").strip()
    old = pathlib.Path(os.path.expanduser(old_raw)) if old_raw else None
    try:
        new = normalize_local(path_in, old, force)
    except ValueError as e:
        fail(str(e))

    old_real = pathlib.Path(os.path.realpath(str(old))) if old is not None else None
    if old_real is not None and new == old_real:
        out({"ok": True, "shuttle": name, "local": str(new), "old": str(old_real),
             "changed": False, "created": False, "warning": "",
             "message": "目录没变，配置没动", "status": rsync_status(name), "backup": ""})

    created = False
    if not new.exists():
        try:
            new.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            fail(f"建目录失败，配置没动：{e}")
        created = True

    warning = ""
    try:
        empty = not any(new.iterdir())
    except OSError:
        empty = False
    if empty:
        n = _remote_count()
        warning = ("新目录是空的，保存后 shuttle 会把 NAS 上的文件整批下载到这里"
                   + (f"（云端索引里有 {n} 个文件）" if n else "")
                   + "；不想整批下载就先在目录里放好你自己的东西")

    raw, _doc = read_conf()
    table = f"shuttles.{name}"
    text = set_key(raw, table, "local", toml_str(str(new)))

    import tomllib
    try:
        got = (tomllib.loads(text).get("shuttles") or {}).get(name) or {}
    except Exception as e:  # noqa: BLE001
        fail(f"改完的配置解析失败，没写盘：{e}")
    if str(got.get("local", "")) != str(new):
        fail("改完复核不一致（local），没写盘")

    st, backup = _write_conf(raw, text, "本机目录", name)
    restarted = _reload_daemon() if backup else False
    out({
        "ok": True, "shuttle": name, "local": str(new), "old": str(old_real or ""),
        "daemon_restarted": restarted,
        "changed": True, "created": created, "warning": warning,
        "message": ("已建目录并保存本机目录" if created else "已保存本机目录"),
        "status": st, "backup": str(backup or ""),
    })


# ── schedule ───────────────────────────────────────────────────────────

def _profile():
    name, sh, _local = pick(None, None)
    prof = sh.get("profile") or ""
    if not prof:
        fail("[shuttles.%s] 没写 profile，定时只能设在 profile 上" % name)
    doc = read_doc()
    return name, prof, (doc.get("profiles") or {}).get(prof) or {}


def cmd_schedule_show(args):
    name, prof, p = _profile()
    triggers = [t for t in (p.get("triggers") or []) if t in TRIGGERS]
    poll = str(p.get("poll_interval") or "60s")
    interval = str(p.get("interval") or "")
    at = [str(x) for x in (p.get("at") or [])]
    out({
        "ok": True, "shuttle": name, "profile": prof, "triggers": triggers,
        "poll_interval": poll, "interval": interval, "at": at,
        "summary": schedule_summary(triggers, poll, interval, at),
        "next": _next_run(triggers, poll, interval, at),
    })


def _next_run(triggers, poll, interval, at):
    """大致下一次：只对“定时 + 每天 HH:MM”给出准确答案，其余给习惯描述。"""
    if "schedule" not in triggers:
        return ""
    if at:
        now = time.localtime()
        today = [t for t in at if t > time.strftime("%H:%M", now)]
        if today:
            return f"今天 {today[0]}"
        return f"明天 {at[0]}"
    if interval:
        return f"每 {human_seconds(parse_seconds(interval))}"
    return ""


def cmd_schedule_set(args):
    name, prof, p = _profile()
    cur_triggers = [t for t in (p.get("triggers") or []) if t in TRIGGERS]
    cur_poll = str(p.get("poll_interval") or "60s")
    cur_interval = str(p.get("interval") or "")
    cur_at = [str(x) for x in (p.get("at") or [])]

    trig_in = _flag(args, "--triggers")
    triggers = []
    if trig_in is not None:
        for t in re.split(r"[,，\s]+", trig_in.strip()):
            if not t or t in ("none", "manual"):
                continue
            if t not in TRIGGERS:
                fail(f"不认识的触发方式：{t}（只有 realtime / schedule / share-available）")
            if t not in triggers:
                triggers.append(t)
    else:
        triggers = cur_triggers

    poll = _flag(args, "--poll")
    poll = cur_poll if poll is None else poll.strip()
    interval = _flag(args, "--interval")
    interval = cur_interval if interval is None else interval.strip()
    at_in = _flag(args, "--at")

    try:
        if poll and parse_seconds(poll) < 5:
            fail("检查间隔至少 5 秒，不然会一直占着 NAS")
        if poll:
            poll = f"{parse_seconds(poll)}s"
        if interval:
            if parse_seconds(interval) < 60:
                fail("定时间隔至少 1 分钟")
        at = cur_at if at_in is None else normalize_at(at_in)
    except ValueError as e:
        fail(str(e))
    if "schedule" in triggers and not interval and not at:
        fail("选了「定时」，就至少给一个「每隔」或一个「每天几点」")

    raw, _doc = read_conf()
    table = f"profiles.{prof}"
    text = set_key(raw, table, "triggers", toml_list(triggers))
    text = set_key(text, table, "poll_interval", toml_str(poll) if poll else None)
    text = set_key(text, table, "interval", toml_str(interval) if interval else None)
    text = set_key(text, table, "at", toml_list(at) if at else None)

    import tomllib
    try:
        got = (tomllib.loads(text).get("profiles") or {}).get(prof) or {}
    except Exception as e:  # noqa: BLE001
        fail(f"改完的配置解析失败，没写盘：{e}")
    if [t for t in (got.get("triggers") or [])] != triggers:
        fail("改完复核不一致（triggers），没写盘")

    st, backup = _write_conf(raw, text, "同步时间", name)
    out({
        "ok": True, "shuttle": name, "profile": prof, "triggers": triggers,
        "poll_interval": poll, "interval": interval, "at": at,
        "summary": schedule_summary(triggers, poll, interval, at),
        "next": _next_run(triggers, poll, interval, at),
        "status": st, "backup": str(backup or ""),
    })


def _flag(args, name):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
        fail(f"{name} 后面要跟一个值")
    for a in args:
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None
