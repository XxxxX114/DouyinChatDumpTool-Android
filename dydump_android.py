#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dydump_android.py —— Android 抖音私信数据库 提取 / 解密 / 导出（需 root）

和微信版最大的不同：抖音的口令不用"猜"。
    · 库文件名里直接带登录 uid：encrypted_<uid>_im.db
    · 口令是固定模板拼的：  "byte" + uid + "imwcdb" + uid + "dance"
所以只要拿到文件，uid 和口令就都确定了，没有暴破环节。
另外解密用纯 Python 实现，只依赖 pycryptodome，不需要 sqlcipher3。

常见用法:
  %(prog)s                        交互菜单（Windows 双击 start.bat 就是它）
  %(prog)s doctor                 环境自检：Python / 依赖 / adb / 设备
  %(prog)s adb                    自动下载 platform-tools 到脚本旁
  %(prog)s stream                 手机端推流直连电脑（推荐，不落手机存储）
  %(prog)s all                    一条龙：probe -> pull -> decrypt -> export
  %(prog)s local                  数据已在 dump/：decrypt -> export
  %(prog)s export --format csv    换导出格式（html/csv/txt/json/md）

工作目录默认是脚本旁的 dump/，所有产物都落在那里，用 --src 改。
仅限自有设备与自有账号。
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import hmac as hmaclib
import html as htmllib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import time
import zipfile
from collections import Counter, OrderedDict

__version__ = "1.0.0"

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(HERE, "dump")

PKG_MAIN = "com.ss.android.ugc.aweme"
PKG_LITE = "com.ss.android.ugc.aweme.lite"
PKGS = [PKG_MAIN, PKG_LITE]

SD_STAGE = "/sdcard/dydump"
PLATFORM_TOOLS_PAGE = "https://developer.android.com/tools/releases/platform-tools"

# ── SQLCipher v3 参数（抖音 WCDB 2 写盘时使用的值）────────────────────
PAGE_SIZE = 4096
KEY_SIZE = 32
IV_SIZE = 16
BLOCK_SIZE = 16
HMAC_SIZE = 20          # HMAC-SHA1
KDF_ITER = 64000
FAST_KDF_ITER = 2
HMAC_SALT_MASK = 0x3A
FILE_HEADER_SZ = 16
SQLITE_MAGIC = b"SQLite format 3\x00"
RESERVE = 48            # iv(16) + hmac(20) 向上取整到 16 的倍数

# ── 消息类型 ─────────────────────────────────────────────────────────
# 抖音的 msg.type 是个不断膨胀的枚举，这里只列真机上实际见过的值；
# 没见过的值原样保留成 type=NNN，不猜。
MSG_TYPE = {
    0: "系统", 1: "系统提示",
    5: "戳一戳", 7: "文本", 8: "视频分享",
    15: "表情", 17: "语音", 21: "直播分享",
    26: "活动卡片", 27: "图片", 30: "图片",
    66: "撤回", 67: "商品分享", 70: "卡片消息",
    73: "语音通话", 77: "作品分享", 88: "小程序",
    100: "系统", 105: "评论分享", 110: "分享卡片",
    144: "作品卡片", 501: "语音",
    1001: "在线状态", 9992: "系统活动", 9996: "空消息",
    40001: "已读回执",
}
# 这些类型没有人类可读内容，导出时默认丢掉（不是聊天内容）
NOISE_TYPES = {1001, 40001, 9996}

# content 字段是 JSON，按这个优先级从中提取可读文本
CONTENT_JSON_KEYS = (
    "text", "tips", "push_detail", "comment", "display_name",
    "recent_news_card_title", "voip_content", "voip_main_title",
    "content_name", "title", "hint", "desc", "description",
    "activity_slogan", "content",
)


# ==========================================================================
# 输出 / 通用
# ==========================================================================
def _init_stdout() -> None:
    """Windows 控制台默认不是 UTF-8，中文会炸。尽量切到 UTF-8。"""
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def log(msg: str) -> None:
    print(f"  [*] {msg}")


def warn(msg: str) -> None:
    print(f"  [!] {msg}")


def die(msg: str, code: int = 1):
    print(f"  [x] {msg}", file=sys.stderr)
    sys.exit(code)


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fix_remote_path(p: str) -> str:
    """把用户在 Windows 上可能写歪的设备路径修回来。

    git-bash 会把 /data/local/tmp 这种路径改写成 C:/Program Files/Git/data/...，
    所以这里把常见前缀剥掉，恢复成真正的 Android 路径。
    """
    if not p:
        return p
    p = p.replace("\\", "/")
    m = re.search(r"/((?:data|sdcard|storage|system|mnt)/.*)$", p)
    if m:
        return "/" + m.group(1)
    return p


# ==========================================================================
# 环境自检
# ==========================================================================
def _dep_ok(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except Exception:
        return False


def _pip_hint(names: list[str]) -> str:
    return f"{sys.executable} -m pip install {' '.join(names)}"


def check_env(need_device: bool = True) -> bool:
    print()
    print("  " + "=" * 58)
    print("   环境自检")
    print("  " + "=" * 58)

    ok = True

    # Python
    v = sys.version_info
    mark = "OK " if v >= (3, 8) else "!! "
    print(f"  [{mark}] Python {v.major}.{v.minor}.{v.micro}  ({sys.executable})")
    if v < (3, 8):
        ok = False

    # 依赖
    if _dep_ok("Crypto"):
        try:
            import Crypto
            print(f"  [OK ] pycryptodome {Crypto.__version__}")
        except Exception:
            print("  [OK ] pycryptodome")
    else:
        print("  [!! ] 缺少 pycryptodome —— 解密必需")
        print(f"        装一下：{_pip_hint(['-r', 'requirements.txt'])}")
        print("        装不上就加国内源：-i https://pypi.tuna.tsinghua.edu.cn/simple")
        ok = False

    # adb
    adb = find_adb()
    if adb:
        print(f"  [OK ] adb: {adb}")
        try:
            r = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=15)
            devs = [l.split()[0] for l in r.stdout.splitlines()[1:]
                    if l.strip() and l.split()[1:2] == ["device"]]
            if devs:
                print(f"  [OK ] 已连接设备: {', '.join(devs)}")
            else:
                print("  [!! ] 没有已授权的设备（USB 调试开了吗？弹窗点允许了吗？）")
                if need_device:
                    ok = False
        except Exception as e:
            print(f"  [!! ] adb devices 失败: {e}")
    else:
        print("  [!! ] 没找到 adb（platform-tools）")
        print("        只处理本地已有数据可以忽略；要从手机拉就选菜单里的『下载 adb』")
        if need_device:
            ok = False

    print()
    print(f"  {'全部就绪' if ok else '有问题，看上面 [!!] 的行'}")
    print("  " + "=" * 58)
    return ok


# ==========================================================================
# adb
# ==========================================================================
def find_adb() -> str | None:
    for c in ("adb", "adb.exe"):
        p = shutil.which(c)
        if p:
            return p
    local = os.path.join(HERE, "platform-tools", "adb.exe" if os.name == "nt" else "adb")
    if os.path.exists(local):
        return local
    return None


def download_platform_tools() -> str | None:
    """下载 platform-tools 并解压到脚本旁。"""
    import urllib.request
    urls = {
        "nt": f"{PLATFORM_TOOLS_PAGE.rsplit('/', 1)[0]}/releases/platform-tools-latest-windows.zip",
        "posix": "https://dl.google.com/android/repository/platform-tools-latest-linux.zip",
    }
    import platform
    key = "nt" if os.name == "nt" else "posix"
    if os.name != "nt" and platform.system() == "Darwin":
        urls["posix"] = "https://dl.google.com/android/repository/platform-tools-latest-darwin.zip"
    url = urls[key]
    dst = os.path.join(HERE, "platform-tools.zip")
    log(f"下载 {url}")
    try:
        urllib.request.urlretrieve(url, dst)
    except Exception as e:
        warn(f"下载失败: {e}")
        return None
    log("解压 ...")
    try:
        with zipfile.ZipFile(dst) as z:
            z.extractall(HERE)
    except Exception as e:
        warn(f"解压失败: {e}")
        return None
    finally:
        try:
            os.remove(dst)
        except OSError:
            pass
    return find_adb()


class Adb:
    """极简 adb 封装，只在需要时提权。"""

    def __init__(self, su: str | None = None):
        self.adb = find_adb()
        if not self.adb:
            die("找不到 adb。跑 `python dydump_android.py adb` 自动下载，"
                "或手动装 platform-tools。")
        self.su = su or "su"
        self.serial = None

    def raw(self, args, check: bool = True, timeout: int = 120):
        cmd = [self.adb] + (["-s", self.serial] if self.serial else []) + list(args)
        r = subprocess.run(cmd, capture_output=True, text=True,
                           errors="replace", timeout=timeout)
        if check and r.returncode != 0:
            raise RuntimeError(f"adb {' '.join(args)} 失败: {r.stderr.strip()}")
        return r

    def shell(self, cmd: str, root: bool = False, check: bool = True, timeout: int = 120) -> str:
        if root:
            cmd = f"{self.su} -c {shq(cmd)}"
        return self.raw(["shell", cmd], check=check, timeout=timeout).stdout

    def detect(self) -> "Adb":
        out = self.raw(["devices"]).stdout
        devs = [l.split()[0] for l in out.splitlines()[1:] if l.strip()]
        if not devs:
            die("没有已授权的设备。检查 USB 调试与弹窗授权。")
        self.serial = devs[0]
        if len(devs) > 1:
            warn(f"检测到多台设备，使用 {self.serial}")
        return self

    def detect_device(self) -> "Adb":
        return self.detect()

    def has_root(self) -> bool:
        for probe in ("id", f"{self.su} -c id"):
            try:
                out = self.shell(probe, root=False, check=False, timeout=20)
                if "uid=0" in out:
                    return True
            except Exception:
                pass
        return False


def shq(s: str) -> str:
    """给 shell 用的安全单引号。"""
    return "'" + s.replace("'", "'\\''") + "'"


# ==========================================================================
# 设备侧：找库
# ==========================================================================
def uid_from_name(name: str) -> str | None:
    """从文件名推断 uid。

    抖音 IM 库的命名不止一种（真机上见到的）：
        encrypted_<uid>_im.db
        encrypted_sub_<uid>_im.db
        sub_encrypted_<uid>_im_customer_box.db
        encrypted_<uid>_im_fts_split.db
        encrypted_<uid>_im_message_0.db
        encrypted_<uid>_im_customer_box.db
        encrypted_<uid>_im_kv_v2.db
        encrypted_<uid>_aid1383_im.db
        encrypted_im_biz_<uid>.db
        encrypted_mi_pigeon_<uid>_aid1383_im.db
    """
    base = os.path.basename(name)
    for pat in (r"encrypted_im_biz_(\d+)",
                r"encrypted_mi_pigeon_(\d+)",
                r"encrypted_(?:sub_)?(\d+)"):
        m = re.search(pat, base)
        if m:
            return m.group(1)
    return None


def find_douyin(adb: Adb) -> list[dict]:
    """返回 [{pkg, db_dir, uid, files:[(名字, 字节数)]}]"""
    found = []
    installed = adb.shell("pm list packages", check=False)
    for pkg in PKGS:
        if pkg not in installed:
            continue
        db_dir = f"/data/data/{pkg}/databases"
        listing = adb.shell(f"ls -l {db_dir} 2>/dev/null", root=True, check=False)
        if not listing.strip():
            warn(f"{pkg}: 读不到 {db_dir}（权限不足或没建库）")
            continue
        files = []
        for line in listing.splitlines():
            parts = line.split()
            if len(parts) < 8:
                continue
            name, size = parts[-1], parts[-5] if len(parts) >= 8 else "0"
            if not re.match(r"^encrypted_.*\.db(-wal|-shm)?$", name):
                continue
            try:
                sz = int(size)
            except ValueError:
                sz = 0
            files.append((name, sz))
        if not files:
            warn(f"{pkg}: databases 里没有匹配到 IM 库")
            continue
        uid = None
        for n, _ in files:
            uid = uid_from_name(n)
            if uid:
                break
        found.append({"pkg": pkg, "db_dir": db_dir, "uid": uid, "files": files})
    return found


def print_found(found: list[dict]) -> None:
    if not found:
        die("没找到抖音的 IM 库。装过抖音并登录过吗？库在 /data/data/<包名>/databases/。")
    for f in found:
        print()
        print(f"  包名: {f['pkg']}")
        print(f"  目录: {f['db_dir']}")
        print(f"  uid : {f['uid'] or '（没从文件名认出，解密时要手动 -u 指定）'}")
        for n, sz in f["files"]:
            print(f"    {n:<42s} {human_size(sz)}")


# ==========================================================================
# 取数据：pull（adb 直拉）/ pull-sd（中转）/ stream（推流）
# ==========================================================================
def pull(adb: Adb, dest: str) -> None:
    found = find_douyin(adb)
    print_found(found)
    if not adb.has_root():
        die("拿不到 root。抖音的库在 app 私有目录，必须 root。\n"
            "    已 root 的设备请在 KernelSU / MT 管理器的终端里跑 dypull.sh。")

    for f in found:
        pkg = f["pkg"]
        out = os.path.join(dest, pkg)
        os.makedirs(out, exist_ok=True)
        log(f"拉取 {pkg} ...")
        stage = f"/data/local/tmp/_dydump_{int(time.time())}"
        adb.shell(f"rm -rf {stage}; mkdir -p {stage}", root=True)
        for n, _ in f["files"]:
            adb.shell(f"cp -f {f['db_dir']}/{n} {stage}/", root=True, check=False)
        adb.shell(f"chmod -R 666 {stage}/*", root=True, check=False)
        r = adb.raw(["pull", stage + "/.", out], check=False)
        adb.shell(f"rm -rf {stage}", root=True, check=False)
        if r.returncode != 0:
            warn(f"adb pull 失败: {r.stderr.strip()}")
            continue
        for n, sz in f["files"]:
            p = os.path.join(out, n)
            if os.path.exists(p):
                log(f"  OK  {n}  ({human_size(os.path.getsize(p))})")
            else:
                warn(f"  缺失 {n}")
    log(f"完成，数据在 {dest}")


def pull_sd(adb: Adb, dest: str, remote: str = SD_STAGE) -> None:
    """手机端 dypull.sh 已把数据拷到 /sdcard/dydump，这里 adb pull 回来。"""
    remote = fix_remote_path(remote)
    log(f"从设备 {remote} 拉取 ...")
    r = adb.raw(["pull", remote + "/.", dest], check=False)
    if r.returncode != 0:
        die(f"拉取失败：{r.stderr.strip()}\n"
            f"    手机上跑过 dypull.sh 了吗？目录是 {remote} 吗？")
    n = 0
    for root, _, fs in os.walk(dest):
        for f in fs:
            p = os.path.join(root, f)
            n += 1
            log(f"  {os.path.relpath(p, dest)}  ({human_size(os.path.getsize(p))})")
    log(f"完成，{n} 个文件 -> {dest}")


def _extract_tar(path: str, dest: str) -> int:
    os.makedirs(dest, exist_ok=True)
    n = 0
    with tarfile.open(path, "r:") as tf:
        for m in tf.getmembers():
            if not m.isfile():
                continue
            # 防目录穿越
            safe = os.path.normpath(m.name).lstrip("/\\")
            if safe.startswith(".."):
                continue
            tf.extract(m, dest)
            n += 1
    return n


def _flatten_into(src_root: str, dest: str) -> int:
    """把解包出来的嵌套目录压平到 dest/<包名>/ 下。"""
    n = 0
    for root, _, files in os.walk(src_root):
        for f in files:
            full = os.path.join(root, f)
            rel = os.path.relpath(full, src_root)
            parts = rel.split(os.sep)
            # 找包名段
            pkg = next((p for p in parts if p.startswith("com.ss.android.ugc.aweme")), None)
            outdir = os.path.join(dest, pkg) if pkg else dest
            os.makedirs(outdir, exist_ok=True)
            shutil.move(full, os.path.join(outdir, f))
            n += 1
    return n


def stream(adb: Adb, dest: str, port: int = 9000, timeout: int = 3600) -> None:
    """手机上以 root 跑 dystream.sh，tar 流经 adb reverse 直接进电脑。

    方向是「手机主动连电脑」：电脑先监听，手机上再跑脚本，避免时序问题。
    会一直 accept 到收到非空流为止 —— 手机端可以先做连通性预检
    （预检产生一个 0 字节连接），也天然容忍重试。
    """
    import socket

    os.makedirs(dest, exist_ok=True)
    tmp = os.path.join(dest, "_stream.tar")

    adb.raw(["reverse", "--remove-all"], check=False)
    adb.raw(["reverse", f"tcp:{port}", f"tcp:{port}"])
    log(f"adb reverse tcp:{port} tcp:{port} 已建立")

    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(8)

    print()
    print("  " + "=" * 58)
    print("   现在在手机上跑（root 终端）：")
    print(f"       sh /data/local/tmp/dystream.sh -p {port}")
    print(f"   等待手机连过来 ...（最多 {timeout // 60} 分钟）")
    print("  " + "=" * 58)
    print()

    deadline = time.time() + timeout
    total = 0
    try:
        while time.time() < deadline:
            srv.settimeout(max(1.0, deadline - time.time()))
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                break
            log("手机已连接，接收中 ...")
            n = last = 0
            with open(tmp, "wb") as f:
                while True:
                    chunk = conn.recv(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    n += len(chunk)
                    if n - last >= (64 << 20):
                        last = n
                        print(f"\r    已接收 {human_size(n)}", end="", flush=True)
            try:
                conn.close()
            except OSError:
                pass
            if n > 512:
                total = n
                break
            log("收到一个空连接（多半是手机端的预检），继续等 ...")
    finally:
        srv.close()
        adb.raw(["reverse", "--remove-all"], check=False)

    print()
    if total == 0:
        die("没收到数据。手机上跑 dystream.sh 了吗？端口对得上吗？")
    log(f"接收完成，共 {human_size(total)}")

    staging = os.path.join(dest, "_stream")
    shutil.rmtree(staging, ignore_errors=True)
    n = _extract_tar(tmp, staging)
    os.remove(tmp)
    log(f"解包 {n} 个文件")
    m = _flatten_into(staging, dest)
    shutil.rmtree(staging, ignore_errors=True)
    log(f"整理 {m} 个文件 -> {dest}")


# ==========================================================================
# 解密：纯 Python SQLCipher v3（含 WAL 合并）
# ==========================================================================
def passphrase_for_uid(uid: str) -> str:
    """抖音 WCDB 2 的口令模板。"""
    if not uid or not uid.isdigit():
        raise ValueError(f"uid 必须是非空纯数字，收到 {uid!r}")
    return f"byte{uid}imwcdb{uid}dance"


def derive_keys(passphrase: str, salt: bytes):
    """返回 (key, hmac_key)。两把是不同的密钥。"""
    key = hashlib.pbkdf2_hmac("sha1", passphrase.encode(), salt, KDF_ITER, KEY_SIZE)
    hmac_salt = bytes(b ^ HMAC_SALT_MASK for b in salt)
    hmac_key = hashlib.pbkdf2_hmac("sha1", key, hmac_salt, FAST_KDF_ITER, KEY_SIZE)
    return key, hmac_key


def _decrypt_page(page: bytes, pgno: int, key: bytes, hmac_key: bytes):
    """解密单页，返回 (明文页, hmac 是否通过)。"""
    from Crypto.Cipher import AES

    offset = FILE_HEADER_SZ if pgno == 1 else 0
    body = page[offset:]
    size = len(body) - RESERVE
    ct = body[:size]
    iv = body[size:size + IV_SIZE]
    mac = body[size + IV_SIZE:size + IV_SIZE + HMAC_SIZE]

    expect = hmaclib.new(hmac_key, ct + iv + struct.pack("<I", pgno),
                         hashlib.sha1).digest()
    ok = hmaclib.compare_digest(expect, mac)

    plain = AES.new(key, AES.MODE_CBC, iv).decrypt(ct)
    if pgno == 1:
        out = bytearray(SQLITE_MAGIC + plain + b"\x00" * RESERVE)
    else:
        out = bytearray(plain + b"\x00" * RESERVE)
    return bytes(out), ok


def _parse_wal(wal_path: str, key: bytes, hmac_key: bytes):
    """解析 SQLCipher 的 -wal，返回 ({pgno: 明文页}, 有效帧数, 库页数)。

    WAL 里每一帧都存一整页（同样加密），页号在帧头里。

    关键点 1：**帧的 salt 必须和 WAL 头的 salt 一致**。WAL 是个环形日志，
    每次 checkpoint 完会换一代 salt 从头上重写，旧代的帧还留在文件里但已失效。
    不校验 salt 就会把陈旧页盖到新页上，直接把库搞坏。
    关键点 2：只取到最后一个 commit 帧为止，之后是没提交完的脏数据。
    """
    data = open(wal_path, "rb").read()
    if len(data) < 32:
        return {}, 0, 0
    magic = int.from_bytes(data[0:4], "big")
    if magic not in (0x377F0682, 0x377F0683):
        warn(f"{os.path.basename(wal_path)}: WAL 魔数不对，跳过")
        return {}, 0, 0
    page_size = int.from_bytes(data[8:12], "big") or PAGE_SIZE
    if page_size != PAGE_SIZE:
        warn(f"WAL 页大小 {page_size} 与预期 {PAGE_SIZE} 不一致，跳过")
        return {}, 0, 0
    hdr_s1 = int.from_bytes(data[16:20], "big")
    hdr_s2 = int.from_bytes(data[20:24], "big")

    frame_sz = 24 + page_size
    raw, pos, stale = [], 32, 0
    while pos + frame_sz <= len(data):
        pgno = int.from_bytes(data[pos:pos + 4], "big")
        dbsize = int.from_bytes(data[pos + 4:pos + 8], "big")
        fs1 = int.from_bytes(data[pos + 8:pos + 12], "big")
        fs2 = int.from_bytes(data[pos + 12:pos + 16], "big")
        if fs1 == hdr_s1 and fs2 == hdr_s2:
            raw.append((pgno, dbsize, data[pos + 24:pos + 24 + page_size]))
        else:
            stale += 1
        pos += frame_sz

    if not raw:
        # 全是被 checkpoint 作废的上一代帧 —— WAL 其实没有待合并内容
        return {}, 0, 0
    if stale:
        # 日志环回绕后，新代帧写在文件开头，旧代残留在尾部
        pass

    last_commit = -1
    for i, (_, dbsize, _) in enumerate(raw):
        if dbsize:
            last_commit = i
    if last_commit < 0:
        return {}, 0, 0
    db_size = raw[last_commit][1]
    raw = raw[:last_commit + 1]

    out, bad = {}, 0
    for pgno, _, page in raw:
        if pgno == 0:
            continue
        try:
            plain, ok = _decrypt_page(page, pgno, key, hmac_key)
        except Exception:
            bad += 1
            continue
        if not ok:
            bad += 1
            continue
        out[pgno] = plain
    if bad:
        warn(f"WAL 中有 {bad} 帧校验失败（可能截断），已跳过")
    return out, len(raw), db_size


def decrypt_db(path: str, out_path: str | None = None, uid: str | None = None,
                verbose: bool = True) -> str:
    """解密一个 encrypted_*.db，返回明文路径。"""
    if not _dep_ok("Crypto"):
        die(f"缺少 pycryptodome，无法解密。\n    {_pip_hint(['-r', 'requirements.txt'])}")

    name = os.path.basename(path)
    uid = uid or uid_from_name(name)
    if not uid:
        die(f"无法从文件名推断 uid: {name}\n    用 --uid 手动指定（就是 encrypted_<uid>_im.db 里那串数字）")

    data = open(path, "rb").read()
    if len(data) < PAGE_SIZE:
        die(f"{name} 太小（{len(data)} 字节），不是完整的 IM 库")

    salt = data[:FILE_HEADER_SZ]
    key, hmac_key = derive_keys(passphrase_for_uid(uid), salt)

    if verbose:
        log(f"{name}")
        log(f"  uid={uid}  口令={passphrase_for_uid(uid)!r}")
        log(f"  盐={salt.hex()}")

    total = len(data) // PAGE_SIZE
    pages: dict[int, bytes] = OrderedDict()
    bad = 0
    for i in range(total):
        pgno = i + 1
        plain, ok = _decrypt_page(data[i * PAGE_SIZE:(i + 1) * PAGE_SIZE],
                                  pgno, key, hmac_key)
        if not ok:
            bad += 1
        pages[pgno] = plain

    if bad == total:
        die(f"{name}: 全部 {total} 页 HMAC 校验失败 → uid 不对，或者不是抖音 IM 库")

    if verbose:
        log(f"  {total} 页，HMAC 通过 {total - bad}/{total}"
            + ("" if not bad else f"（{bad} 页失败，文件可能被截断）"))

    # ── 合并 WAL ──
    maxpg = max(pages) if pages else 0
    w = path + "-wal"
    if os.path.exists(w):
        if verbose:
            log(f"  发现 {os.path.basename(w)}，合并中 ...")
        wp, nf, wal_dbsize = _parse_wal(w, key, hmac_key)
        if wp:
            pages.update(wp)
            # 最后一帧的 dbsize 是提交后的权威库大小，WAL 做过截断时尾页要丢掉
            if wal_dbsize:
                maxpg = wal_dbsize
                if verbose:
                    log(f"  合并 {nf} 帧（{len(wp)} 页生效，提交后库大小 {wal_dbsize} 页）")
            elif verbose:
                log(f"  合并 {nf} 帧（{len(wp)} 页生效）")
        elif verbose:
            log("  WAL 里全是被 checkpoint 作废的旧代帧，无需合并")
    out = bytearray()
    for pgno in range(1, maxpg + 1):
        out += pages.get(pgno, b"\x00" * PAGE_SIZE)
    if len(out) < PAGE_SIZE:
        die(f"{name}: 解密结果为空")
    out[20] = RESERVE      # 每页保留字节数，让标准 sqlite3 能直接打开

    ps = int.from_bytes(out[16:18], "big")
    if ps != PAGE_SIZE or out[21] != 64:
        die(f"{name}: 解密后头部字段异常（页大小={ps}）→ uid 不对")

    if out_path is None:
        out_path = os.path.join(os.path.dirname(os.path.abspath(path)),
                                name.replace(".db", ".plain.db"))
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(out)
    if verbose:
        log(f"  -> {os.path.relpath(out_path, WORK) if out_path.startswith(WORK) else out_path}"
            f"  ({human_size(len(out))})")
    return out_path


def plain_path_for(enc_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(enc_path)),
                        os.path.basename(enc_path).replace(".db", ".plain.db"))


def _is_fresh(enc_path: str) -> bool:
    """已解出的明文库是否比加密库（含 -wal/-shm）新。"""
    out = plain_path_for(enc_path)
    if not os.path.exists(out):
        return False
    newest = os.path.getmtime(enc_path)
    for s in ("-wal", "-shm"):
        p = enc_path + s
        if os.path.exists(p):
            newest = max(newest, os.path.getmtime(p))
    return os.path.getmtime(out) >= newest


# 明文库的文件名特征。抖音的 databases/ 目录里塞了几百个杂七杂八的 sqlite 文件
# （aweme.db / logdb.db / ut.db …），光看"是不是 sqlite"会把它们全捞进来。
PLAIN_DB_NAME_HINT = re.compile(r"(^|_)im(_|\.|$)|pigeon", re.I)


def _has_im_tables(path: str) -> bool:
    """确认这个明文库真的是 IM 库（有 msg + conversation_list 两张表）。"""
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            names = {r[0].lower() for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            con.close()
    except Exception:
        return False
    return "msg" in names and "conversation_list" in names


def find_plain_dbs(src: str) -> list[str]:
    """找出 dump/ 里**本来就没加密**的 IM 库。

    抖音有些库（例如 mi_pigeon_*_aid<aid>_im.db）是明文 SQLite，
    文件名也不带 encrypted_ 前缀。它们和主库是同一套表结构、同样有聊天数据，
    不挑出来就会整块丢掉。

    两道筛子，缺一不可：
      1. 文件头是 "SQLite format 3"（排除加密库和残缺文件）
      2. 文件名像 IM 库 **且** 里面真有 msg + conversation_list 表
        —— 只靠文件名会把 aweme.db 之类无关库带进来
    """
    out = []
    for root, _, files in os.walk(src):
        for f in files:
            if not f.endswith(".db") or f.endswith(".plain.db"):
                continue
            if f.startswith("encrypted_"):
                continue
            if not PLAIN_DB_NAME_HINT.search(f):
                continue
            p = os.path.join(root, f)
            try:
                if os.path.getsize(p) < PAGE_SIZE:
                    continue
                with open(p, "rb") as fh:
                    if fh.read(16) != SQLITE_MAGIC:
                        continue
            except OSError:
                continue
            if _has_im_tables(p):
                out.append(p)
    return sorted(out)


def decrypt_dir(src: str, uid: str | None = None, force: bool = False) -> list[str]:
    """把 dump/ 下所有 encrypted_*.db 解密，返回明文路径列表。

    返回值 = 新解出来的明文库 + dump/ 里本来就没加密的库。
    """
    targets = []
    for root, _, files in os.walk(src):
        for f in files:
            # 只认加密库；跳过我们自己的产物 encrypted_*.plain.db 及各种临时文件
            if not f.startswith("encrypted_") or not f.endswith(".db"):
                continue
            if f.endswith(".plain.db") or f.endswith(".db-wal") or f.endswith(".db-shm"):
                continue
            if f.endswith("-mbak.db") or f.endswith("-journal"):
                continue
            targets.append(os.path.join(root, f))

    if not targets:
        extra = find_plain_dbs(src)
        if extra:
            log(f"没有加密库；发现 {len(extra)} 个明文库，直接使用")
            return extra
        die(f"{src} 下没有 encrypted_*.db。先 pull / stream 把数据弄过来。")

    log(f"发现 {len(targets)} 个加密库")
    out, skipped, failed = [], 0, []
    for t in sorted(targets):
        if not force and _is_fresh(t):
            out.append(plain_path_for(t))
            skipped += 1
            continue
        try:
            out.append(decrypt_db(t, uid=uid))
        except SystemExit:
            failed.append(os.path.basename(t))
            warn(f"{os.path.basename(t)} 解密失败，跳过")
    if skipped:
        log(f"其中 {skipped} 个已解出且未变化，跳过（加 --force 强制重解）")

    plain = find_plain_dbs(src)
    if plain:
        log(f"另有 {len(plain)} 个明文库（无需解密），一并纳入导出")
        out.extend(plain)
    if failed:
        log(f"有 {len(failed)} 个库解不开（多为旧版遗留库，密钥非 uid 派生）：")
        for n in failed[:8]:
            log(f"    · {n}")
        if len(failed) > 8:
            log(f"    · … 另有 {len(failed) - 8} 个")
    if not out:
        die("没有任何库可用")
    return out


# ==========================================================================
# 读库
# ==========================================================================
CONV_ID_KEYS = ["conversation_id", "conv_id", "cid", "session_id"]
MSG_ID_KEYS = ["msg_id", "message_id", "server_id", "id", "order_in_conversation"]
TIME_KEYS = ["created_time", "create_time", "timestamp", "time", "sort_time", "created_at"]
CONTENT_KEYS = ["content", "text", "body", "message"]
TYPE_KEYS = ["type", "msg_type", "message_type"]
SENDER_KEYS = ["sender", "from_uid", "sender_uid", "uid", "from_user"]
NAME_KEYS = ["name", "nickname", "remark", "alias", "display_name", "title"]


def _pick(cols: list[str], keys: list[str]) -> str | None:
    low = {c.lower(): c for c in cols}
    for k in keys:
        if k in low:
            return low[k]
    for c in cols:
        for k in keys:
            if k in c.lower():
                return c
    return None


def _fmt_ts(v) -> str:
    if v in (None, ""):
        return ""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return str(v)
    for div in (1, 1000, 1_000_000):
        t = n / div
        if 1_000_000_000 < t < 4_000_000_000:
            return datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")
    return str(v)


def _fts_name(s) -> str:
    """从抖音 FTS 索引那串"名字 名字 拼音 拼音 …"里抠出原始名字。

    抖音把联系人索引写成 `<名字> <名字> <拼音/分段...>` 的形式，
    名字本身可能带空格（"小 梅"），所以不能简单取第一个空格前的内容。
    这里找**最长**的那个 k，使得前 k 个字符后面跟一个空格、再接一遍自己。
    """
    s = (s or "").strip()
    if not s:
        return ""
    n = len(s)
    for cut in range(n // 2, 0, -1):
        if s[cut:cut + 1] == " " and s[:cut] == s[cut + 1:cut + 1 + cut]:
            return s[:cut]
    return s.split(" ")[0]


def read_contact_index(con) -> dict[str, dict]:
    """读一张库里的联系人索引，返回 {uid: {"name":…, "dy_id":…}}。

    这张表是 `fts_contact_index_table_content`，是抖音 IM 的**联系人搜索索引**，
    放在 encrypted_im_biz_<uid>.db 里（那是个元数据库，没有 msg 表）。
    它的 docid 就是对方的 uid —— 真机上 50 个单聊能命中 43 个，
    命中后就能把"会话 <一串数字 uid>"换成对方的真实昵称。

    注意：不要用这张表的 FTS5 虚表本体，它用自定义分词器（mmicu）打不开；
    直接读它的 content 影子表就行。
    """
    out: dict[str, dict] = {}
    for t in ("fts_contact_index_table_content",):
        try:
            cols = [d[0] for d in con.execute(f'SELECT * FROM "{t}" LIMIT 0').description]
        except Exception:
            continue
        low = {c.lower(): c for c in cols}
        c_doc = low.get("docid")
        c_rem = low.get("c0fts_remark_name")
        c_nick = low.get("c1fts_nick_name")
        c_dy = low.get("c2fts_dy_id")
        if not c_doc or not (c_rem or c_nick):
            continue
        sel = ", ".join(f'"{c}"' for c in (c_doc, c_rem, c_nick, c_dy) if c)
        try:
            for row in con.execute(f'SELECT {sel} FROM "{t}"'):
                d = dict(zip([c for c in (c_doc, c_rem, c_nick, c_dy) if c], row))
                uid = str(d.get(c_doc))
                if not uid or uid == "None":
                    continue
                name = _fts_name(d.get(c_rem)) or _fts_name(d.get(c_nick))
                dy = _fts_name(d.get(c_dy)) if c_dy else ""
                if name or dy:
                    out.setdefault(uid, {"name": name, "dy_id": dy})
        except Exception:
            continue
    return out


def _json_text(raw) -> str:
    """从 content 的 JSON 里挖出人类可读的那一句；挖不到返回空串。

    抖音各版本 content 的形态差别很大：
      · 老版本直接存纯文本
      · 新版本是 JSON，文本在 "text"，系统提示在 "tips"
      · 分享类在 "push_detail" / "content_name"，表情在 stickers[].display_name
    所以这里按优先级逐个试，而不是只认 "text"。
    """
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    if not s.startswith("{"):
        return s
    try:
        obj = json.loads(s)
    except Exception:
        return ""
    if not isinstance(obj, dict):
        return ""
    for k in CONTENT_JSON_KEYS:
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (int, float)) and k in ("text", "comment"):
            return str(v)
    st = obj.get("stickers")
    if isinstance(st, list) and st and isinstance(st[0], dict):
        n = st[0].get("display_name")
        if n:
            return f"[表情] {n}"
    return ""


class Reader:
    """把若干个明文库读成统一的 会话 / 消息 / 联系人 结构。

    内容优先取 fts_search_msg_biz.search_content —— 那是抖音自己生成的
    "可搜索文本"，对直播分享、视频分享、评论卡片这些非文本消息也能给出
    一句人类可读的话，比硬解 content 里的 JSON 靠谱得多。取不到才回退到 JSON。
    """

    def __init__(self, paths: list[str], keep_deleted: bool = True,
                 keep_noise: bool = False, contact_paths: list[str] | None = None):
        import sqlite3

        self.convs: dict[str, str] = {}        # conversation_id -> 显示名
        self.conv_meta: dict[str, dict] = {}   # conversation_id -> 元信息
        self.contacts: dict[str, str] = {}     # uid -> 昵称/备注
        self.contact_dy: dict[str, str] = {}   # uid -> 抖音号
        self.accounts: dict[str, str] = {}     # 明文库名 -> 登录 uid
        self.msgs: list[dict] = []
        self.deleted_skipped = 0
        self.noise_skipped = 0
        self.dupes_skipped = 0
        self._tables: list[str] = []
        self._src: list[str] = []
        self._seen: set = set()

        # 先扫联系人索引。它藏在 im_biz 那种没有 msg 表的元数据库里，
        # 不先拿到手，会话名就只能显示成一串 uid。
        for p in list(contact_paths or []) + list(paths):
            base = os.path.basename(p)
            try:
                con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
                con.text_factory = lambda b: b.decode("utf-8", "replace")
            except Exception:
                continue
            try:
                for uid, info in read_contact_index(con).items():
                    if info.get("name"):
                        self.contacts.setdefault(uid, info["name"])
                    if info.get("dy_id"):
                        self.contact_dy.setdefault(uid, info["dy_id"])
            finally:
                con.close()

        for p in paths:
            base = os.path.basename(p)
            try:
                con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
                con.text_factory = lambda b: b.decode("utf-8", "replace")
            except Exception as e:
                warn(f"打开 {base} 失败: {e}")
                continue
            self._src.append(base)
            try:
                self._load(con, base, keep_deleted, keep_noise)
            except Exception as e:
                warn(f"{base} 读取中断: {type(e).__name__}: {e}")
            finally:
                con.close()

        self.msgs.sort(key=lambda m: (m.get("ts_raw") or 0, m.get("_seq", 0)))

    # -- 内部 ------------------------------------------------------------
    def _tables_of(self, con) -> list[str]:
        return [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")]

    def _cols(self, con, t: str) -> list[str]:
        """取列名。抖音有自定义 FTS 分词器（mmicu），那些表打不开，返回空表跳过。"""
        try:
            return [d[0] for d in con.execute(f'SELECT * FROM "{t}" LIMIT 0').description]
        except Exception:
            return []

    def _self_uid(self, con, base: str) -> str | None:
        """判定这个库里"我"是谁。

        最可靠的是文件名里的 uid（库就是以登录 uid 命名的）。万一文件名认不出，
        就从 conversation_id 的 `0:1:<A>:<B>` 里推断：**在 A、B 两个位置都反复
        出现的那个 uid 就是自己**（单聊里自己既可能在前也可能在后）。
        """
        hint = uid_from_name(base)
        try:
            p3, p4 = Counter(), Counter()
            for (cid,) in con.execute("SELECT conversation_id FROM conversation_list"):
                parts = str(cid).split(":")
                if len(parts) == 4:
                    p3[parts[2]] += 1
                    p4[parts[3]] += 1
            both = set(p3) & set(p4)
            if both:
                guess = max(both, key=lambda u: p3[u] + p4[u])
                if hint and hint in both:
                    return hint
                return guess
        except Exception:
            pass
        return hint

    def _search_index(self, con, tables: list[str]) -> dict[str, str]:
        """msg_uuid -> 可读文本。抖音把这条索引单独放一张表里。"""
        idx: dict[str, str] = {}
        for t in tables:
            if "search" not in t.lower() and "fts_msg" not in t.lower():
                continue
            cols = self._cols(con, t)
            if not cols:
                continue
            low = {c.lower(): c for c in cols}
            k = low.get("msg_uuid")
            v = low.get("search_content") or low.get("c2fts_search_content") or low.get("c0search_content")
            if not k or not v:
                continue
            try:
                for uu, sc in con.execute(f'SELECT "{k}", "{v}" FROM "{t}"'):
                    if uu and sc and str(sc).strip():
                        idx.setdefault(str(uu), str(sc).strip())
            except Exception:
                continue
        return idx

    def _load(self, con, base: str, keep_deleted: bool, keep_noise: bool) -> None:
        tables = self._tables_of(con)
        self._tables.extend(tables)
        self_uid = self._self_uid(con, base)
        if self_uid:
            self.accounts[base] = self_uid

        # 1) 会话：名字 + 类型 + 成员数
        meta_tabs = {}
        for t in tables:
            if "conversation" not in t.lower():
                continue
            cols = self._cols(con, t)
            cid = _pick(cols, CONV_ID_KEYS)
            if not cid:
                continue
            nm = _pick(cols, NAME_KEYS)
            if nm:
                try:
                    for i, n in con.execute(f'SELECT "{cid}", "{nm}" FROM "{t}"'):
                        if i is not None and n and str(n).strip():
                            self.convs[str(i)] = str(n).strip()
                except Exception:
                    pass
            ctype = _pick(cols, ["type", "conversation_type", "conv_type"])
            cmem = _pick(cols, ["member_count", "member_num", "membercount"])
            if ctype:
                try:
                    for row in con.execute(
                            f'SELECT "{cid}", "{ctype}"'
                            + (f', "{cmem}"' if cmem else "")
                            + f' FROM "{t}"'):
                        key = str(row[0])
                        m = self.conv_meta.setdefault(key, {})
                        try:
                            m["type"] = int(row[1])
                        except (TypeError, ValueError):
                            pass
                        if cmem and len(row) > 2:
                            try:
                                m["member_count"] = int(row[2])
                            except (TypeError, ValueError):
                                pass
                        m.setdefault("account", self_uid or "")
                        m.setdefault("src", base)
                except Exception:
                    pass

        # 2) 联系人备注（participant.alias 多半是空的，但有人改了备注就有）
        for t in tables:
            if not re.search(r"user|contact|friend|participant", t, re.I):
                continue
            cols = self._cols(con, t)
            key = _pick(cols, ["user_id", "uid", "id", "sec_uid", "douyin_id"])
            nm = _pick(cols, ["alias", "remark", "name", "nickname", "display_name"])
            if not key or not nm:
                continue
            try:
                for k, n in con.execute(f'SELECT "{key}", "{nm}" FROM "{t}"'):
                    if k is not None and n and str(n).strip():
                        self.contacts.setdefault(str(k), str(n).strip())
            except Exception:
                continue

        # 3) 消息
        best, best_score = None, 0
        for t in tables:
            if re.search(r"fts|search|docsize|segdir|segments|_stat$", t, re.I):
                continue
            cols = self._cols(con, t)
            score = 0
            if re.search(r"\bmsg|message|chat", t, re.I):
                score += 10
            if any(k in [c.lower() for c in cols] for k in ("content", "text", "body")):
                score += 10
            if _pick(cols, CONV_ID_KEYS):
                score += 5
            if score > best_score:
                best, best_score = t, score
        if not best or best_score < 10:
            warn(f"{base}: 没找到明显的消息表，跳过")
            return

        cols = self._cols(con, best)
        c_conv = _pick(cols, CONV_ID_KEYS)
        c_time = _pick(cols, TIME_KEYS)
        c_text = _pick(cols, CONTENT_KEYS)
        c_type = _pick(cols, TYPE_KEYS)
        c_send = _pick(cols, SENDER_KEYS)
        c_mid = _pick(cols, MSG_ID_KEYS)
        c_uuid = "msg_uuid" if "msg_uuid" in [c.lower() for c in cols] else None
        c_del = "deleted" if "deleted" in [c.lower() for c in cols] else None
        if not c_conv:
            warn(f"{base}.{best}: 没有会话 id 列，跳过")
            return

        idx = self._search_index(con, tables)

        want = [c for c in (c_conv, c_time, c_text, c_type, c_send, c_mid, c_del, c_uuid) if c]
        sel = ", ".join(f'"{c}"' for c in want)
        order = (f' ORDER BY "{c_time}"' if c_time
                 else (f' ORDER BY "{c_mid}"' if c_mid else ""))

        sk_del = sk_noise = sk_dup = 0
        for seq, row in enumerate(con.execute(f'SELECT {sel} FROM "{best}"{order}')):
            d = dict(zip(want, row))

            if c_del is not None and str(d.get(c_del) or "0") not in ("0", "", "None"):
                if not keep_deleted:
                    sk_del += 1
                    continue

            raw_type = d.get(c_type)
            try:
                t = int(raw_type) if raw_type is not None else None
            except (TypeError, ValueError):
                t = None
            if not keep_noise and t in NOISE_TYPES:
                sk_noise += 1
                continue

            cid = str(d.get(c_conv, ""))
            uuid = str(d.get(c_uuid) or "") if c_uuid else ""
            raw = d.get(c_text)

            # 内容三级回退：搜索索引 -> content JSON -> 纯文本原样
            text = idx.get(uuid, "") if uuid else ""
            if not text:
                text = _json_text(raw)
            if not text and raw is not None:
                s = str(raw).strip()
                # 老版本会直接把文本存进来；新版本是 JSON，挖不出可读文本时
                # 宁可留空（由 _body 显示"（图片）"这种占位），也不要把
                # 一大坨 JSON 塞进聊天记录里
                if s and not s.startswith("{"):
                    text = s

            sender = "" if d.get(c_send) is None else str(d.get(c_send))
            key = (cid, uuid) if uuid else (cid, seq, text[:40])
            if key in self._seen:
                sk_dup += 1
                continue
            self._seen.add(key)

            self.msgs.append({
                "conv": cid,
                "conv_name": self.name_of(cid),
                "type": t,
                "type_name": MSG_TYPE.get(t, f"type={t}" if t is not None else ""),
                "content": text,
                "sender": sender,
                "sender_name": "我" if sender == self_uid else (self.contacts.get(sender) or sender),
                "is_me": bool(self_uid) and sender == self_uid,
                "deleted": bool(c_del is not None
                                and str(d.get(c_del) or "0") not in ("0", "", "None")),
                "time": _fmt_ts(d.get(c_time)),
                "ts_raw": d.get(c_time) or 0,
                "account": self_uid or "",
                "src": base,
                "_seq": seq,
            })
        self.deleted_skipped += sk_del
        self.noise_skipped += sk_noise
        self.dupes_skipped += sk_dup

    # -- 对外 ------------------------------------------------------------
    def talkers(self) -> list[tuple[str, int]]:
        c = Counter(m["conv"] for m in self.msgs)
        return sorted(c.items(), key=lambda kv: -kv[1])

    def messages(self, conv: str) -> list[dict]:
        return [m for m in self.msgs if m["conv"] == conv]

    def name_of(self, conv: str) -> str:
        """会话显示名。

        群聊用 conversation_core.name；单聊抖音本地没存对方昵称，
        就用 conversation_id 里那个"不是我"的 uid 去联系人索引里查。
        """
        n = self.convs.get(conv)
        if n:
            return n
        parts = str(conv).split(":")
        if len(parts) == 4:
            self_uid = self.conv_meta.get(conv, {}).get("account")
            a, b = parts[2], parts[3]
            peer = b if a == self_uid else a
            return self.contacts.get(peer) or peer or conv
        return f"会话 {conv}"

    def peer_of(self, conv: str) -> str:
        """单聊会话里对方的 uid（群聊返回空串）。"""
        parts = str(conv).split(":")
        if len(parts) != 4:
            return ""
        self_uid = self.conv_meta.get(conv, {}).get("account")
        a, b = parts[2], parts[3]
        return b if a == self_uid else a

    def dy_id_of(self, conv: str) -> str:
        return self.contact_dy.get(self.peer_of(conv), "")

    def account_of(self, conv: str) -> str:
        return self.conv_meta.get(conv, {}).get("account", "") or ""

    def is_group(self, conv: str) -> bool:
        return self.conv_meta.get(conv, {}).get("type", 1) != 1


# ==========================================================================
# 导出
# ==========================================================================
def safe_name(s: str, fallback: str = "conv") -> str:
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", s or "").strip(" .")
    return (s or fallback)[:60]


def _body(m: dict) -> str:
    c = (m.get("content") or "").replace("\r\n", "\n").strip()
    if c:
        return c
    return f"（{m.get('type_name') or '空消息'}）"


def _tags(m: dict) -> list[str]:
    """消息上的小标签：非文本类型 + 已删除。

    抖音本地库里 deleted=1 的消息**内容和时间都还在**，只是界面上不显示了。
    做备份就该留着，但要标出来，免得日后误读。

    正文为空时不加类型标签 —— 那时 _body() 会显示"（图片）"这样的占位，
    再加一个"图片"标签就重复了。
    """
    out = []
    tn = m.get("type_name") or ""
    if tn and tn != "文本" and (m.get("content") or "").strip():
        out.append(tn)
    if m.get("deleted"):
        out.append("已删除")
    return out


def export_html(rd: Reader, conv: str, msgs: list[dict], outdir: str, name: str,
           stem: str | None = None) -> str:
    e = htmllib.escape
    rows = []
    prev_day = None
    for m in msgs:
        day = (m.get("time") or "")[:10]
        if day and day != prev_day:
            rows.append(f'<div class="day">{e(day)}</div>')
            prev_day = day
        who = "me" if m.get("is_me") else "them"
        if m.get("deleted"):
            who += " gone"
        tags = "".join(
            f'<span class="tag{" gone" if t == "已删除" else ""}">{e(t)}</span>'
            for t in _tags(m))
        rows.append(
            f'<div class="row {who}"><div class="bubble">{tags}{e(_body(m))}'
            f'<div class="meta">{e(m.get("time") or "")}</div></div></div>'
        )
    dy = rd.dy_id_of(conv)
    dy_html = f" · 抖音号 {e(dy)}" if dy else ""
    doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(name)} - 抖音聊天记录</title>
<style>
:root{{color-scheme:light dark}}
*{{box-sizing:border-box}}
body{{margin:0;font:15px/1.65 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
background:#f5f5f7;color:#1d1d1f}}
header{{position:sticky;top:0;background:rgba(255,255,255,.86);backdrop-filter:blur(12px);
border-bottom:1px solid #e3e3e6;padding:14px 20px}}
header h1{{margin:0;font-size:17px;font-weight:600}}
header .sub{{font-size:12px;color:#86868b;margin-top:2px}}
main{{max-width:820px;margin:0 auto;padding:20px 16px 60px}}
.day{{text-align:center;font-size:12px;color:#86868b;margin:22px 0 14px}}
.row{{display:flex;margin:8px 0}}
.row.me{{justify-content:flex-end}}
.bubble{{max-width:min(72%,560px);padding:9px 13px;border-radius:14px;background:#fff;
border:1px solid #e8e8ec;white-space:pre-wrap;word-break:break-word}}
.row.me .bubble{{background:#f7e9ee;border-color:#e9d3da}}
.tag{{display:inline-block;font-size:11px;padding:1px 6px;border-radius:5px;
background:#eef1f6;color:#5b6b82;margin-right:6px;vertical-align:1px}}
.meta{{font-size:11px;color:#a1a1a6;margin-top:5px}}
.row.gone .bubble{{opacity:.5}}
.tag.gone{{background:#f6e2e2;color:#a44}}
@media (prefers-color-scheme:dark){{
body{{background:#0e0e10;color:#e8e8ea}}
header{{background:rgba(22,22,24,.86);border-color:#2c2c2e}}
.bubble{{background:#1c1c1e;border-color:#2c2c2e}}
.row.me .bubble{{background:#33222a;border-color:#4a2f3a}}
.tag{{background:#26262a;color:#9aa7bb}}
.tag.gone{{background:#3a2426;color:#d08a8a}}
}}
</style></head><body>
<header><h1>{e(name)}</h1>
<div class="sub">抖音私信 · {len(msgs)} 条消息 · 账号 {e(rd.account_of(conv) or "?")}{dy_html} · 会话 {e(conv)}</div></header>
<main>{''.join(rows)}</main></body></html>"""
    p = os.path.join(outdir, f"{stem or safe_name(name)}.html")
    with open(p, "w", encoding="utf-8") as f:
        f.write(doc)
    return p


def export_csv(rd: Reader, conv: str, msgs: list[dict], outdir: str, name: str,
           stem: str | None = None) -> str:
    import csv
    p = os.path.join(outdir, f"{stem or safe_name(name)}.csv")
    with open(p, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["时间", "方向", "类型", "发送者", "内容", "已删除"])
        for m in msgs:
            w.writerow([m.get("time", ""),
                        "我" if m.get("is_me") else (m.get("sender_name") or "对方"),
                        m.get("type_name", ""), m.get("sender", ""), _body(m),
                        "是" if m.get("deleted") else ""])
    return p


def export_txt(rd: Reader, conv: str, msgs: list[dict], outdir: str, name: str,
           stem: str | None = None) -> str:
    p = os.path.join(outdir, f"{stem or safe_name(name)}.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write(f"# {name}  （{len(msgs)} 条）\n")
        f.write(f"# 账号 {rd.account_of(conv)} · 会话 {conv}\n\n")
        for m in msgs:
            who = "我" if m.get("is_me") else (m.get("sender_name") or "对方")
            mark = "".join(f"[{t}]" for t in _tags(m))
            f.write(f"[{m.get('time','')}] {who}: {mark}{_body(m)}\n")
    return p


def export_json(rd: Reader, conv: str, msgs: list[dict], outdir: str, name: str,
           stem: str | None = None) -> str:
    p = os.path.join(outdir, f"{stem or safe_name(name)}.json")
    data = {
        "conversation": {"id": conv, "name": name, "count": len(msgs),
                         "account": rd.account_of(conv),
                         "is_group": rd.is_group(conv)},
        "messages": [{k: v for k, v in m.items() if not k.startswith("_")} for m in msgs],
    }
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return p


def export_md(rd: Reader, conv: str, msgs: list[dict], outdir: str, name: str,
           stem: str | None = None) -> str:
    p = os.path.join(outdir, f"{stem or safe_name(name)}.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write(f"# {name}\n\n- 消息数：{len(msgs)}\n"
                f"- 账号：`{rd.account_of(conv)}`\n- 会话 ID：`{conv}`\n\n")
        for m in msgs:
            who = "**我**" if m.get("is_me") else f"**{m.get('sender_name') or '对方'}**"
            mark = "".join(f"`{t}` " for t in _tags(m))
            f.write(f"- `{m.get('time','')}` {who} {mark}{_body(m)}\n")
    return p


EXPORTERS = {
    "html": export_html, "csv": export_csv, "txt": export_txt,
    "json": export_json, "md": export_md,
}


def write_index(rows: list[tuple], outdir: str, fmt: str) -> str:
    """总览页。rows = [(显示名, 消息数, 账号, 文件名stem), ...]"""
    p = os.path.join(outdir, "index.html")
    e = htmllib.escape
    accounts = sorted({r[2] for r in rows if r[2]})
    multi = len(accounts) > 1
    tr = "".join(
        f'<tr><td>{e(str(n))}</td>'
        + (f'<td class="mono">{e(str(a))}</td>' if multi else "")
        + f'<td class="num">{c}</td>'
        f'<td><a href="{e(stem)}.{fmt}">{e(stem)}.{fmt}</a></td></tr>'
        for n, c, a, stem in rows)
    head = ('<tr><th>会话</th>'
            + ('<th>账号</th>' if multi else '')
            + '<th>消息数</th><th>文件</th></tr>')
    acct_line = ""
    if multi:
        acct_line = "<p>涉及 " + str(len(accounts)) + " 个账号：" + \
            "、".join(f"<code>{e(a)}</code>" for a in accounts) + "</p>"
    doc = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>抖音聊天记录 · 总览</title><style>
:root{{color-scheme:light dark}}
body{{font:15px/1.6 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;margin:0;
background:#f5f5f7;color:#1d1d1f;padding:32px}}
h1{{font-size:20px}}table{{border-collapse:collapse;width:100%;max-width:860px;background:#fff;
border-radius:10px;overflow:hidden}}
th,td{{text-align:left;padding:10px 14px;border-bottom:1px solid #ececf0}}
th{{background:#fafafc;font-weight:600;font-size:13px}}
td.num{{text-align:right;font-variant-numeric:tabular-nums}}
td.mono{{font-family:ui-monospace,Consolas,monospace;font-size:13px;color:#666}}
a{{color:#c2416b;text-decoration:none}}a:hover{{text-decoration:underline}}
code{{background:#eef0f4;padding:1px 5px;border-radius:4px;font-size:13px}}
@media (prefers-color-scheme:dark){{
body{{background:#0e0e10;color:#e8e8ea}}table{{background:#1a1a1c}}
th{{background:#222226}}th,td{{border-color:#2c2c2e}}td.mono{{color:#9a9aa0}}
code{{background:#26262a}}}}
</style></head><body>
<h1>抖音聊天记录 · 总览</h1>
<p>共 {len(rows)} 个会话 / {sum(r[1] for r in rows)} 条消息</p>
{acct_line}
<table>{head}{tr}</table>
</body></html>"""
    with open(p, "w", encoding="utf-8") as f:
        f.write(doc)
    return p


def export(rd: Reader, src: str, fmt: str = "html", min_msgs: int = 1,
           only: list[str] | None = None) -> None:
    outdir = os.path.join(src, "export", fmt)
    os.makedirs(outdir, exist_ok=True)
    talkers = rd.talkers()
    if only:
        want = set(only)
        talkers = [t for t in talkers if t[0] in want or rd.name_of(t[0]) in want]
    talkers = [t for t in talkers if t[1] >= min_msgs]
    if not talkers:
        die("没有满足条件的会话可导出（试试 --min-msgs 0）")

    log(f"共 {len(talkers)} 个会话，导出为 {fmt}")
    rows = []
    used: Counter = Counter()
    for cid, _ in talkers:
        msgs = rd.messages(cid)
        if not msgs:
            continue
        name = rd.name_of(cid)
        acct = rd.account_of(cid)
        # 不同账号里可能有同名会话（比如都叫 12345），撞名就加后缀
        base = safe_name(name)
        used[base] += 1
        stem = base if used[base] == 1 else f"{base}-{used[base]}"
        p = EXPORTERS[fmt](rd, cid, msgs, outdir, name, stem=stem)
        log(f"  {len(msgs):>6} 条 -> {os.path.relpath(p, src)}")
        rows.append((name, len(msgs), acct, stem))
    idx = write_index(rows, outdir, fmt)
    log(f"共 {len(rows)} 个会话 / {sum(r[1] for r in rows)} 条消息")
    log(f"总览: {idx}")
    log(f"全部完成，输出目录: {outdir}")


# ==========================================================================
# 交互菜单
# ==========================================================================
MENU = """
    1) 检查环境            Python / 依赖 / adb / 设备 一次看完
    2) 一键全流程          探测 -> 拉取 -> 解密 -> 导出
    3) 处理本地已有数据    解密 -> 导出（数据已经在 dump/ 里）
    4) 手机端推流直连      stream（推荐，不落手机存储）
    5) 中转拉取            pull-sd（手机端 dypull.sh 先拷到 /sdcard/dydump）
    6) 只导出              换格式 html / csv / txt / json / md
    7) 下载 adb            platform-tools 自动解压到脚本旁
    8) 离线自检            selfcheck（不碰设备、不碰数据）
    0) 退出
"""


def _ask(prompt: str, default: str = "") -> str:
    try:
        v = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return v or default


def _run_selfcheck() -> None:
    """跑一遍 selfcheck.py —— 纯离线，验解密器自身逻辑对不对。"""
    p = os.path.join(HERE, "selfcheck.py")
    if not os.path.isfile(p):
        warn(f"找不到 {p}")
        return
    try:
        subprocess.run([sys.executable, p], cwd=HERE, check=False)
    except Exception as e:
        warn(f"自检没能跑起来: {e}")


def interactive(args) -> None:
    while True:
        print()
        print("  " + "=" * 58)
        print("   Android 抖音私信 提取 / 解密 / 导出")
        print("  " + "=" * 58)
        print(MENU)
        c = _ask("  选一个 [0-8]: ")
        print()
        if c in ("0", "q", "Q", ""):
            return
        if c == "8":
            _run_selfcheck()
            _ask("\n  回车继续 ...")
            continue
        mapping = {
            "1": "doctor", "2": "all", "3": "local", "4": "stream",
            "5": "pull-sd", "6": "export", "7": "adb",
        }
        step = mapping.get(c)
        if not step:
            warn("没这个选项")
            continue
        try:
            run(step, args)
        except SystemExit:
            pass
        except Exception as e:
            warn(f"执行出错: {e}")
        _ask("\n  回车继续 ...")


# ==========================================================================
# 入口
# ==========================================================================
def main() -> None:
    _init_stdout()
    ap = argparse.ArgumentParser(
        prog="dydump_android.py",
        description="Android 抖音私信数据库 提取 / 解密 / 导出（需 root）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("常见用法:")[1].split("工作目录")[0].strip(),
    )
    ap.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("step", nargs="?", default=None,
                    choices=["doctor", "adb", "probe", "pull", "pull-sd", "stream",
                             "decrypt", "export", "local", "all"],
                    help="要执行的步骤；不填就进交互菜单")
    ap.add_argument("--src", default=WORK, help=f"工作目录（默认 {WORK}）")
    ap.add_argument("--from", dest="remote", default=SD_STAGE,
                    help=f"pull-sd 从设备哪个目录拉（默认 {SD_STAGE}）")
    ap.add_argument("--port", type=int, default=9000, help="stream 用哪个端口（默认 9000）")
    ap.add_argument("--timeout", type=int, default=3600, help="stream 等待手机连接的秒数")
    ap.add_argument("--uid", help="解密时手动指定 uid（默认从文件名推断）")
    ap.add_argument("--su", dest="su_path", help="手动指定 su 路径")
    ap.add_argument("--format", default="html", choices=list(EXPORTERS), help="导出格式")
    ap.add_argument("--min-msgs", type=int, default=1, help="少于该条数的会话跳过")
    ap.add_argument("--force", action="store_true",
                    help="解密时忽略增量缓存，强制重解所有库")
    ap.add_argument("--no-deleted", action="store_true",
                    help="不导出本地已删除的消息（默认导出并标记）")
    ap.add_argument("--keep-noise", action="store_true",
                    help="保留在线状态/已读回执这类无内容的系统消息（默认丢弃）")
    args = ap.parse_args()

    if args.step is None:
        interactive(args)
    else:
        run(args.step, args)


def run(step: str, args) -> None:
    """执行一个步骤。CLI 和交互菜单都走这里，保证行为一致。"""
    if step == "doctor":
        check_env(need_device=True)
        return

    if step == "adb":
        p = download_platform_tools()
        if not p:
            die(f"自动下载失败，请手动下载后解压到脚本旁：{PLATFORM_TOOLS_PAGE}")
        log(f"adb 就绪: {p}")
        return

    src = os.path.abspath(args.src)
    os.makedirs(src, exist_ok=True)
    args.remote = fix_remote_path(args.remote)
    log(f"工作目录: {src}")

    if step in ("decrypt", "export", "local", "all") and not _dep_ok("Crypto"):
        die("缺少 pycryptodome，无法解密。\n"
            f"    装一下：{_pip_hint(['-r', 'requirements.txt'])}\n"
            f"    完整自检：python {os.path.basename(__file__)} doctor")

    adb = None

    def get_adb():
        nonlocal adb
        if adb is None:
            adb = Adb(args.su_path).detect()
        return adb

    if step == "stream":
        stream(get_adb(), src, args.port, args.timeout)
        return

    if step == "pull-sd":
        pull_sd(get_adb(), src, args.remote)
        return

    if step in ("probe", "pull", "all"):
        a = get_adb()
        if step == "probe":
            print_found(find_douyin(a))
            return
        pull(a, src)

    plains = None
    if step in ("decrypt", "local", "all"):
        plains = decrypt_dir(src, uid=args.uid, force=args.force)

    if step in ("export", "local", "all"):
        if plains is None:
            plains = []
            for root, _, files in os.walk(src):
                for f in files:
                    if f.endswith(".plain.db"):
                        plains.append(os.path.join(root, f))
            plains.extend(find_plain_dbs(src))
            if not plains:
                die("没有明文库，先执行 decrypt")
        # 只留真正有 IM 表的：fts_split / kv_v2 这类读不出消息，
        # 放进来只会刷一屏"没找到消息表"的告警。
        # 但联系人索引在 im_biz 那种元数据库里，所以原始列表要留着当联系人源。
        all_plains = plains
        keep = [p for p in all_plains if _has_im_tables(p)]
        if len(keep) != len(all_plains):
            log(f"跳过 {len(all_plains) - len(keep)} 个非 IM 库（仅用于补联系人）")
        if not keep:
            die("没有可读的 IM 库")
        rd = Reader(sorted(keep),
                    keep_deleted=not getattr(args, "no_deleted", False),
                    keep_noise=getattr(args, "keep_noise", False),
                    contact_paths=sorted(all_plains))
        log(f"读到 {len(rd.msgs)} 条消息 / {len(rd.talkers())} 个会话")
        if rd.accounts:
            log(f"涉及账号 {len(set(rd.accounts.values()))} 个: "
                + "、".join(sorted(set(rd.accounts.values()))))
        if rd.deleted_skipped:
            log(f"跳过已删除 {rd.deleted_skipped} 条（加 --no-deleted 之前的默认）")
        if rd.noise_skipped:
            log(f"跳过无内容系统消息 {rd.noise_skipped} 条（--keep-noise 可保留）")
        if rd.dupes_skipped:
            log(f"去重 {rd.dupes_skipped} 条（主库/sub 库有重叠）")
        export(rd, src, args.format, args.min_msgs)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  已中断")
        sys.exit(130)
