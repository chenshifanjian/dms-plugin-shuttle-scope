
#!/usr/bin/env python3
"""纯函数单测：解析、比对、按层聚合、exclude 前缀匹配、写配置。

不碰网络、不碰真实配置：run  `python3 tests/test_core.py`
"""

import pathlib
import sys
import tomllib
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from shuttle_scope.config import patch_config  # noqa: E402
from shuttle_scope.connect import (  # noqa: E402
    human_seconds,
    normalize_at,
    parse_seconds,
    parse_url,
    schedule_summary,
    set_key,
    toml_list,
    toml_str,
)
from shuttle_scope.connect import _is_within, clean_password, normalize_local  # noqa: E402
from shuttle_scope.core import HOME, ancestor_hit  # noqa: E402
from shuttle_scope.diff import compare, level_view, summarize  # noqa: E402
from shuttle_scope.remote import parse_listing  # noqa: E402

SAMPLE = """  .                                   D        0  Sun Nov  8 12:04:19 2026
  ..                                  D        0  Mon Nov  9 10:25:23 2026
  月亮糖葫芦.3mf                        A 16504915  Tue Nov  3 02:23:33 2026
  代上传                               D        0  Sun Nov  8 17:17:39 2026

\\代上传
  .                                   D        0  Sun Nov  8 17:17:39 2026
  ..                                  D        0  Sun Nov  8 12:04:19 2026
  2026-11-08 08-19-55.mp4              N 10268522063  Sun Nov  8 12:01:55 2026
  2026-11-08 08-25-02.mp4              A 16301532418  Sun Nov  8 12:02:04 2026

\\代下载\\小项目
  .                                   D        0  Wed Nov  4 22:55:46 2026
  ..                                  D        0  Sun Nov  8 17:17:39 2026
  新建文件夹.txt                       A     2032  Wed Nov  4 22:53:30 2026
"""


class Parsing(unittest.TestCase):
    def test_parse_listing(self):
        files, dirs = parse_listing(SAMPLE)
        self.assertEqual(files["月亮糖葫芦.3mf"], 16504915)
        self.assertEqual(files["代上传/2026-11-08 08-19-55.mp4"], 10268522063)
        self.assertEqual(files["代下载/小项目/新建文件夹.txt"], 2032)
        self.assertIn("代上传", dirs)
        self.assertIn("代下载/小项目", dirs)
        self.assertNotIn(".", files)

    def test_parse_listing_base(self):
        files, dirs = parse_listing(SAMPLE, base="待分类")
        self.assertIn("待分类/代上传/2026-11-08 08-25-02.mp4", files)
        self.assertIn("待分类/代下载/小项目", dirs)

    def test_names_with_spaces(self):
        text = "  2026-11-08 08-25-02.mp4  A 12  Sun Nov  8 12:02:04 2026\n"
        files, _ = parse_listing(text)
        self.assertEqual(files["2026-11-08 08-25-02.mp4"], 12)


class Matching(unittest.TestCase):
    def test_ancestor_hit(self):
        ex = ["待分类/代上传", "完成的项目/x"]
        self.assertEqual(ancestor_hit("待分类/代上传", ex), "待分类/代上传")
        self.assertEqual(ancestor_hit("待分类/代上传/a.mp4", ex), "待分类/代上传")
        self.assertEqual(ancestor_hit("待分类/代下载", ex), "")
        self.assertEqual(ancestor_hit("待分类", ex), "")

    def test_prefix_not_partial(self):
        ex = ["待分类/代上"]
        self.assertEqual(ancestor_hit("待分类/代上传", ex), "")


class Diffs(unittest.TestCase):
    def setUp(self):
        self.local = {"a.txt": 10, "sub/b.txt": 5, "sub/only_local.txt": 1}
        self.remote = {"a.txt": 10, "sub/b.txt": 7, "sub/cloud_only.txt": 3, "other/c.txt": 9}
        self.status = compare(self.local, self.remote)

    def test_compare(self):
        self.assertEqual(self.status["a.txt"], "same")
        self.assertEqual(self.status["sub/b.txt"], "differ")
        self.assertEqual(self.status["sub/only_local.txt"], "local_only")
        self.assertEqual(self.status["sub/cloud_only.txt"], "remote_only")

    def test_level_view_root(self):
        entries, total = level_view("", self.status, self.local, self.remote,
                                    {"sub"}, {"sub", "other"}, ["sub/only_local.txt"])
        by = {e["name"]: e for e in entries}
        self.assertEqual(total, 3)
        self.assertTrue(by["sub"]["dir"])
        self.assertEqual(by["sub"]["remote_only"], 1)
        self.assertEqual(by["sub"]["local_only"], 1)
        self.assertEqual(by["sub"]["differ"], 1)
        self.assertEqual(by["other"]["remote_only"], 1)
        self.assertEqual(by["a.txt"]["status"], "same")

    def test_level_view_leaf(self):
        entries, _ = level_view("sub", self.status, self.local, self.remote, set(), set(), [])
        by = {e["name"]: e for e in entries}
        self.assertEqual(by["b.txt"]["status"], "differ")
        self.assertEqual(by["b.txt"]["size_local"], 5)
        self.assertEqual(by["b.txt"]["size_remote"], 7)
        self.assertEqual(by["cloud_only.txt"]["status"], "remote_only")
        self.assertTrue(by["cloud_only.txt"]["excluded"] is False)

    def test_summarize(self):
        s = summarize("", self.status, self.local, self.remote)
        self.assertEqual((s["remote_only"], s["local_only"], s["differ"], s["same"]), (2, 1, 1, 1))
        self.assertEqual(s["remote_only_bytes"], 12)
        sub = summarize("sub", self.status, self.local, self.remote)
        self.assertEqual(sub["remote_files"], 2)


class ConfigPatch(unittest.TestCase):
    RAW = (
        "[global]\nterminal = \"kitty\"\n\n"
        "[shuttles.moren]\nprofile = \"live\"\nlocal = \"~/Documents/smb\"\n"
        "remote = \"zspace:.\"\nexclude = [\"待分类/代上传\"]\n"
        "\n[profiles.live]\nmode = \"sync\"\n"
    )

    def test_append_item(self):
        text = patch_config(self.RAW, "moren", ["待分类/代上传", "待分类/代下载"])
        self.assertIn('exclude = ["待分类/代上传", "待分类/代下载"]', text)
        self.assertIn("[profiles.live]", text)

    def test_clear_removes_line(self):
        text = patch_config(self.RAW, "moren", [])
        self.assertNotIn("exclude", text)
        self.assertIn("[profiles.live]", text)

    def test_insert_when_missing(self):
        raw = self.RAW.replace('exclude = ["待分类/代上传"]\n', "")
        text = patch_config(raw, "moren", ["a"])
        self.assertIn('exclude = ["a"]', text)

    def test_multiline_list(self):
        raw = self.RAW.replace(
            'exclude = ["待分类/代上传"]',
            'exclude = [\n  "待分类/代上传",\n  "待分类/代下载",\n]')
        text = patch_config(raw, "moren", ["x/y"])
        self.assertIn('exclude = ["x/y"]', text)
        self.assertNotIn("待分类/代下载", text)

class ConnectTest(unittest.TestCase):
    """SMB 地址解析、时间格式、TOML 定点改写 —— 纯字符串，不碰 keyring 与 NAS。"""

    def test_url_forms(self):
        for text in ("smb://10.0.0.2/团队文件",
                     "//10.0.0.2/团队文件",
                     "\\\\10.0.0.2\\团队文件",
                     "smb://10.0.0.2/团队文件/",
                     "10.0.0.2/团队文件"):
            p = parse_url(text)
            self.assertEqual((p["host"], p["share"], p["sub"]), ("10.0.0.2", "团队文件", ""), text)

    def test_url_user_sub_port(self):
        p = parse_url("smb://bob@nas:4458/团队文件/前进的项目/x")
        self.assertEqual(p["host"], "nas")
        self.assertEqual(p["port"], 4458)
        self.assertEqual(p["user"], "bob")
        self.assertEqual(p["sub"], "前进的项目/x")

    def test_set_key_same_value_keeps_line(self):
        raw = '[shares.x]\nhost = "h"    # 别动我\n'
        self.assertEqual(set_key(raw, "shares.x", "host", '"h"'), raw)

    def test_set_key_changes_value(self):
        raw = '[shares.x]\nhost = "h"    # 注释\n'
        got = set_key(raw, "shares.x", "host", '"h2"')
        self.assertIn('"h2"', got)
        self.assertNotIn('"h"\n', got)

    def test_url_rejects(self):
        with self.assertRaises(ValueError):
            parse_url("")
        with self.assertRaises(ValueError):
            parse_url("smb://nas")            # 没共享名
        with self.assertRaises(ValueError):
            parse_url("smb://bob:pw@nas/share")  # 密码不许写地址里

    def test_explicit_user_wins(self):
        self.assertEqual(parse_url("smb://a@nas/s", "b")["user"], "b")

    def test_seconds(self):
        self.assertEqual(parse_seconds("60s"), 60)
        self.assertEqual(parse_seconds("5m"), 300)
        self.assertEqual(parse_seconds("1h30m"), 5400)
        self.assertEqual(parse_seconds("2d"), 172800)
        self.assertEqual(parse_seconds("90"), 90)
        self.assertEqual(parse_seconds(""), 0)
        for bad in ("abc", "10x", "h"):
            with self.assertRaises(ValueError):
                parse_seconds(bad)

    def test_human(self):
        self.assertEqual(human_seconds(60), "1 分钟")
        self.assertEqual(human_seconds(5400), "1 小时 30 分钟")
        self.assertEqual(human_seconds(0), "0 秒")

    def test_at(self):
        self.assertEqual(normalize_at("2:00, 14:30"), ["02:00", "14:30"])
        self.assertEqual(normalize_at("02:00 2:00"), ["02:00"])
        self.assertEqual(normalize_at(""), [])
        for bad in ("25:00", "2:5", "abc"):
            with self.assertRaises(ValueError):
                normalize_at(bad)

    def test_summary(self):
        self.assertIn("手动", schedule_summary([], "60s", "", []))
        self.assertIn("实时", schedule_summary(["realtime"], "30s", "", []))
        s = schedule_summary(["realtime", "schedule"], "60s", "6h", ["02:00"])
        self.assertIn("每 6 小时", s)
        self.assertIn("每天 02:00", s)

    RAW = """[global]
terminal = "kitty --"

[profiles.live]
mode        = "sync"
triggers    = ["realtime", "share-available"] # 注释
poll_interval = "60s"
ignore      = [\n  "a",\n  "b",\n]

[shares.zspace]
type  = "smb"
host  = "10.0.0.1"
user  = "bob"
"""

    def test_set_key_replace_keeps_align(self):
        text = set_key(self.RAW, "profiles.live", "triggers", toml_list(["schedule"]))
        self.assertIn('triggers    = ["schedule"]', text)
        self.assertIn('mode        = "sync"', text)

    def test_set_key_insert(self):
        text = set_key(self.RAW, "profiles.live", "interval", toml_str("6h"))
        self.assertIn('interval = "6h"', text)
        self.assertTrue(text.index("interval") < text.index("[shares.zspace]"))

    def test_set_key_delete(self):
        text = set_key(self.RAW, "shares.zspace", "user", None)
        self.assertNotIn('user  = "bob"', text)
        self.assertIn('host  = "10.0.0.1"', text)

    def test_set_key_multiline(self):
        text = set_key(self.RAW, "profiles.live", "ignore", toml_list(["c"]))
        self.assertIn('["c"]', text)          # 保留原对齐空格
        self.assertNotIn('"a",', text)

    def test_set_key_missing_table(self):
        with self.assertRaises(ValueError):
            set_key(self.RAW, "shares.nope", "host", toml_str("x"))

    def test_roundtrip_parses(self):
        text = self.RAW
        text = set_key(text, "profiles.live", "triggers", toml_list(["realtime", "schedule"]))
        text = set_key(text, "profiles.live", "at", toml_list(["02:00"]))
        doc = tomllib.loads(text)
        self.assertEqual(doc["profiles"]["live"]["triggers"], ["realtime", "schedule"])
        self.assertEqual(doc["profiles"]["live"]["at"], ["02:00"])


class PasswordAndLocal(unittest.TestCase):
    def test_clean_password_strips_newlines(self):
        self.assertEqual(clean_password("abc123\n"), "abc123")
        self.assertEqual(clean_password("ab\r\nc"), "abc")
        self.assertEqual(clean_password(None), "")
        # 中间的空格/特殊字符不动（密码里可以有空格）
        self.assertEqual(clean_password(" a b "), " a b ")

    def test_normalize_local_without_old(self):
        # 配置里还没有 local 时传 None，不能崩
        p = normalize_local("~/Documents/smb", None)
        self.assertTrue(str(p).endswith("/Documents/smb"))
        self.assertTrue(p.is_absolute())


class LocalPath(unittest.TestCase):
    """本机目录：展开 ~、拒绝危险路径、拒绝新旧互为上下级。"""

    def test_expand_and_slash(self):
        p = normalize_local("~/Documents/smb2/")
        self.assertEqual(p, HOME / "Documents" / "smb2")

    def test_rejects(self):
        for bad in ("", "   ", "Documents/smb", "/"):
            with self.assertRaises(ValueError):
                normalize_local(bad)
        with self.assertRaises(ValueError):
            normalize_local(str(HOME))            # 家目录
        with self.assertRaises(ValueError):
            normalize_local("~")

    def test_reject_file(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            f = pathlib.Path(d) / "a.txt"
            f.write_text("x", encoding="utf-8")
            with self.assertRaises(ValueError):
                normalize_local(str(f))

    def test_ancestor_blocked(self):
        old, new = "/home/u/Documents/smb", "/home/u/Documents"
        with self.assertRaises(ValueError) as e:
            normalize_local(new, old)
        self.assertIn("上级", str(e.exception))
        self.assertEqual(normalize_local(new, old, force=True), pathlib.Path(new))

    def test_descendant_blocked(self):
        old, new = "/home/u/Documents/smb", "/home/u/Documents/smb/sub"
        with self.assertRaises(ValueError) as e:
            normalize_local(new, old)
        self.assertIn("嵌套", str(e.exception))
        self.assertEqual(normalize_local(new, old, force=True), pathlib.Path(new))

    def test_is_within(self):
        self.assertTrue(_is_within("/a/b/c", "/a/b"))
        self.assertFalse(_is_within("/a/b", "/a/b/c"))
        self.assertFalse(_is_within("/a/bb", "/a/b"))

    def test_local_key_unchanged_keeps_comment(self):
        raw = '[shuttles.moren]\nlocal = "/home/u/Documents/smb"  # 我的同步目录\n'
        self.assertEqual(set_key(raw, "shuttles.moren", "local",
                                 toml_str("/home/u/Documents/smb")), raw)
        text = set_key(raw, "shuttles.moren", "local", toml_str("/home/u/other"))
        self.assertIn('local = "/home/u/other"', text)
        self.assertNotIn("# 我的同步目录", text)
        self.assertEqual(tomllib.loads(text)["shuttles"]["moren"]["local"], "/home/u/other")


if __name__ == "__main__":
    unittest.main(verbosity=2)
