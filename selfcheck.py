#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selfcheck.py —— 离线自检：不需要手机、不需要 root，验证加解密链路正确

做的事：
  1. 造一个明文库，转成 SQLCipher 的 reserve 布局（并用 sqlite3 回读校验）
  2. 按 SQLCipher v3 页格式加密，得到 encrypted_<uid>_im.db
  3. 跑 dydump_android.py 的解密，比对内容是否完全一致
  4. 构造一个 -wal，验证 WAL 合并逻辑（含未提交帧的截断）
  5. 跑导出，确认产物存在

用法：
    python selfcheck.py            # 全部检查
    python selfcheck.py -v         # 打印细节
"""

import argparse
import contextlib
import hashlib
import hmac as hmaclib
import io
import os
import shutil
import sqlite3
import struct
import sys


@contextlib.contextmanager
def quiet():
    """把被测函数的日志和预期内的报错吞掉，保持自检报告干净。"""
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        yield

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import dydump_android as D  # noqa: E402

DATA = os.path.join(HERE, "selfcheck_data")
UID = "1234567890"
ME_UID = 1234567890
CONV_A = "0:1:2222:1234567890"      # 对方在前
CONV_B = "0:1:1234567890:3333"      # 我在前
BTREE_PTR = {0x02: 4, 0x05: 2, 0x0A: 4, 0x0D: 2}
BTREE_HDR = {0x02: 12, 0x05: 12, 0x0A: 8, 0x0D: 8}

PASS, FAIL = [], []


def ok(name: str, detail: str = "") -> None:
    PASS.append(name)
    print(f"  [OK ] {name}" + (f"   {detail}" if detail else ""))


def bad(name: str, detail: str = "") -> None:
    FAIL.append(name)
    print(f"  [!! ] {name}" + (f"   {detail}" if detail else ""))


# ── 1. 造明文样本 ──────────────────────────────────────────────────────
def build_plain(path: str) -> None:
    """造一个贴近真机的明文库。

    刻意还原真机上这几个容易踩坑的点：
      · conversation_id 是 "0:1:<对方uid>:<自己uid>" 这种四段文本
      · 对方 uid 在自己前面还是后面不固定（判定"我是谁"就靠这个）
      · 群聊的 conversation_id 是一串纯数字
      · 表情类消息 content 里没有可读文本，得靠 fts_search_msg_biz 补
      · 有 deleted=1 的本地已删消息
    """
    if os.path.exists(path):
        os.remove(path)
    c = sqlite3.connect(path)
    c.executescript("""
        PRAGMA page_size=4096;
        CREATE TABLE conversation_list(
            conversation_id TEXT PRIMARY KEY, short_id INTEGER,
            type INTEGER, member_count INTEGER, updated_time INTEGER);
        CREATE TABLE conversation_core(
            conversation_id TEXT PRIMARY KEY, name TEXT, icon TEXT);
        CREATE TABLE msg(
            msg_uuid TEXT PRIMARY KEY, msg_server_id INTEGER, conversation_id TEXT,
            type INTEGER, sender INTEGER, content TEXT, deleted INTEGER,
            created_time INTEGER);
        CREATE TABLE fts_search_msg_biz(
            msg_uuid TEXT, search_content TEXT, conversation_id TEXT,
            order_index INTEGER, created_time INTEGER, type INTEGER,
            aweme_type INTEGER);
        CREATE TABLE participant(
            user_id INTEGER, conversation_id TEXT, alias TEXT, sec_uid TEXT);
    """)
    convs = [(CONV_A, 2222, "老王", 1, 2), (CONV_B, 3333, "小张", 1, 2)]
    c.executemany("INSERT INTO conversation_list VALUES(?,?,?,?,?)",
                  [(cid, 1000 + i, t, mc, 1759600000 + i * 600)
                   for i, (cid, _p, _n, t, mc) in enumerate(convs)])
    c.executemany("INSERT INTO conversation_core VALUES(?,?,?)",
                  [(cid, n, "http://x/%d.jpg" % i)
                   for i, (cid, _p, n, _t, _mc) in enumerate(convs)])
    c.executemany("INSERT INTO participant VALUES(?,?,?,?)",
                  [(p, cid, None, "sec_" + str(p))
                   for cid, p, _n, _t, _mc in convs])

    rows, fts, mid = [], [], 1
    for cid, peer, who, _t, _mc in convs:
        for k in range(24):
            me = (k % 2 == 0)
            sender = ME_UID if me else peer
            uuid = f"{cid}#{k:02d}"
            if k == 5:                       # 表情：content 挖不出文本
                content = '{"stickers":[{"display_name":"Hi","id":1}]}'
                typ = 15
                fts.append((uuid, "[表情] Hi", cid, mid, 1759600000 + mid * 60, typ, 0))
            else:
                content = f"[{who}] 第 {k + 1} 条测试消息"
                typ = 7
            rows.append((uuid, mid, cid, typ, sender, content,
                         1 if k == 7 else 0, 1759600000 + mid * 60))
            mid += 1
    c.executemany("INSERT INTO msg VALUES(?,?,?,?,?,?,?,?)", rows)
    c.executemany("INSERT INTO fts_search_msg_biz VALUES(?,?,?,?,?,?,?)", fts)
    c.commit()
    c.close()


# ── 2. 造 reserve 布局 ────────────────────────────────────────────────
def shift_page(page: bytes, hdr: int, reserve: int) -> bytearray:
    """把一页 b-tree 的内容区整体上移 reserve 字节（reserve=0 -> reserve=N）。

    需要同步改四处，缺一处 sqlite 就会报 database disk image is malformed：
      1. 内容区本身整体上移
      2. 页头里的"内容区起始偏移"（header+5，2 字节）
      3. 所有单元格指针（它们是以页首为基准的偏移）
      4. 空闲块链：**链头在页头 header+1**，链内每个 next 指针也要一起减
    """
    pt = page[hdr]
    if pt not in BTREE_PTR:
        raise RuntimeError(f"页类型 0x{pt:02x} 不是 b-tree 页")
    cs = int.from_bytes(page[hdr + 5:hdr + 7], "big")
    if cs == 0 or cs > D.PAGE_SIZE:
        cs = D.PAGE_SIZE
    np_ = bytearray(page)

    # 1) 内容区上移
    np_[cs - reserve:D.PAGE_SIZE - reserve] = page[cs:D.PAGE_SIZE]
    np_[D.PAGE_SIZE - reserve:D.PAGE_SIZE] = b"\x00" * reserve

    # 2) 内容区起始偏移
    np_[hdr + 5:hdr + 7] = (cs - reserve).to_bytes(2, "big")

    # 3) 单元格指针
    ncells = int.from_bytes(page[hdr + 3:hdr + 5], "big")
    poff, w = hdr + BTREE_HDR[pt], BTREE_PTR[pt]
    for c in range(ncells):
        o = poff + c * w
        v = int.from_bytes(np_[o:o + w], "big")
        if v:
            np_[o:o + w] = (v - reserve).to_bytes(w, "big")

    # 4) 空闲块链（链头 + 链内 next），必须读原始页的偏移
    fb = int.from_bytes(page[hdr + 1:hdr + 3], "big")
    if fb:
        np_[hdr + 1:hdr + 3] = (fb - reserve).to_bytes(2, "big")
    g = 0
    while fb and g < 1000:
        g += 1
        nxt = int.from_bytes(page[fb:fb + 2], "big")
        np_[fb - reserve:fb - reserve + 2] = (
            (nxt - reserve) if nxt else 0).to_bytes(2, "big")
        fb = nxt
    return np_


def build_plain(path: str, reserve: int = D.RESERVE) -> None:
    """造一个贴近真机的、**原生带 reserve 保留空间**的明文库。

    直接拿 sqlite3 建库再硬转 reserve 是行不通的：页面一旦接近写满，
    上移 reserve 字节后内容就会和单元格指针数组重叠。
    所以这里换个顺序 —— 先只建一张极小的表（page 1 内容少，平移绝对安全），
    把 page 1 平移好、页头第 20 字节改成 reserve，**之后再让 SQLite 打开**。
    此后 SQLite 就按 usable = page_size - reserve 排版，
    后面所有表和索引天生就是带保留空间的布局，一个字节都不用再动。

    顺便还原真机上几个容易踩坑的点：
      · conversation_id 是 "0:1:<对方uid>:<自己uid>" 这种四段文本
      · 对方 uid 在自己前面还是后面不固定（判定"我是谁"就靠这个）
      · 群聊的 conversation_id 是一串纯数字
      · 表情类消息 content 里没有可读文本，得靠 fts_search_msg_biz 补
      · 有 deleted=1 的本地已删消息
    """
    if os.path.exists(path):
        os.remove(path)

    # ── 第一步：一张小表，只为把 page 1 撑出来 ──
    c = sqlite3.connect(path)
    c.execute("PRAGMA page_size=%d" % D.PAGE_SIZE)
    c.execute("CREATE TABLE _seed(x)")
    c.commit()
    c.close()

    raw = bytearray(open(path, "rb").read())
    if len(raw) % D.PAGE_SIZE or len(raw) > 3 * D.PAGE_SIZE:
        raise RuntimeError(f"种子库应只有一两页，实际 {len(raw)} 字节")
    for i in range(len(raw) // D.PAGE_SIZE):
        hdr = 100 if i == 0 else 0
        raw[i * D.PAGE_SIZE:(i + 1) * D.PAGE_SIZE] = shift_page(
            bytes(raw[i * D.PAGE_SIZE:(i + 1) * D.PAGE_SIZE]), hdr, reserve)
    raw[20] = reserve                      # 页头第 20 字节 = 每页保留字节数
    open(path, "wb").write(bytes(raw))

    # ── 第二步：正常建表插数据，SQLite 会按 reserve 布局写盘 ──
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE conversation_list(
            conversation_id TEXT PRIMARY KEY, short_id INTEGER,
            type INTEGER, member_count INTEGER, updated_time INTEGER);
        CREATE TABLE conversation_core(
            conversation_id TEXT PRIMARY KEY, name TEXT, icon TEXT);
        CREATE TABLE msg(
            msg_uuid TEXT PRIMARY KEY, msg_server_id INTEGER, conversation_id TEXT,
            type INTEGER, sender INTEGER, content TEXT, deleted INTEGER,
            created_time INTEGER);
        CREATE TABLE fts_search_msg_biz(
            msg_uuid TEXT, search_content TEXT, conversation_id TEXT,
            order_index INTEGER, created_time INTEGER, type INTEGER,
            aweme_type INTEGER);
        CREATE TABLE participant(
            user_id INTEGER, conversation_id TEXT, alias TEXT, sec_uid TEXT);
    """)
    convs = [(CONV_A, 2222, "老王", 1, 2), (CONV_B, 3333, "小张", 1, 2)]
    c.executemany("INSERT INTO conversation_list VALUES(?,?,?,?,?)",
                  [(cid, 1000 + i, t, mc, 1759600000 + i * 600)
                   for i, (cid, _p, _n, t, mc) in enumerate(convs)])
    c.executemany("INSERT INTO conversation_core VALUES(?,?,?)",
                  [(cid, n, "http://x/%d.jpg" % i)
                   for i, (cid, _p, n, _t, _mc) in enumerate(convs)])
    c.executemany("INSERT INTO participant VALUES(?,?,?,?)",
                  [(p, cid, None, "sec_" + str(p))
                   for cid, p, _n, _t, _mc in convs])

    rows, fts, mid = [], [], 1
    for cid, peer, who, _t, _mc in convs:
        for k in range(24):
            me = (k % 2 == 0)
            sender = ME_UID if me else peer
            uuid = f"{cid}#{k:02d}"
            if k == 5:                       # 表情：content 挖不出文本
                content = '{"stickers":[{"display_name":"Hi","id":1}]}'
                typ = 15
                fts.append((uuid, "[表情] Hi", cid, mid, 1759600000 + mid * 60, typ, 0))
            else:
                content = f"[{who}] 第 {k + 1} 条测试消息"
                typ = 7
            rows.append((uuid, mid, cid, typ, sender, content,
                         1 if k == 7 else 0, 1759600000 + mid * 60))
            mid += 1
    c.executemany("INSERT INTO msg VALUES(?,?,?,?,?,?,?,?)", rows)
    c.executemany("INSERT INTO fts_search_msg_biz VALUES(?,?,?,?,?,?,?)", fts)
    c.execute("DROP TABLE _seed")
    c.commit()
    c.close()
# ── 3. 加密 ───────────────────────────────────────────────────────────
def encrypt_pages(plain: bytearray, salt: bytes, key: bytes, hmac_key: bytes):
    from Crypto.Cipher import AES
    n = len(plain) // D.PAGE_SIZE
    out = bytearray()
    for i in range(n):
        pgno = i + 1
        page = bytes(plain[i * D.PAGE_SIZE:(i + 1) * D.PAGE_SIZE])
        off = D.FILE_HEADER_SZ if pgno == 1 else 0
        body = page[off:]
        size = len(body) - D.RESERVE
        iv = os.urandom(D.IV_SIZE)
        ct = AES.new(key, AES.MODE_CBC, iv).encrypt(body[:size])
        mac = hmaclib.new(hmac_key, ct + iv + struct.pack("<I", pgno),
                          hashlib.sha1).digest()
        pad = os.urandom(D.RESERVE - D.IV_SIZE - D.HMAC_SIZE)
        out += (salt if pgno == 1 else b"") + ct + iv + mac + pad
    return bytes(out)


def build_wal(pages_enc: dict, page_size: int = D.PAGE_SIZE,
              commit_on: set | None = None,
              salt=(0x11223344, 0x55667788),
              frame_salt=None) -> bytes:
    """按 SQLite WAL 格式拼一个 -wal。

    pages_enc:  {pgno: 加密后的整页}
    commit_on:  哪些帧标成 commit（db_size_after_commit 非 0）
    salt:       WAL 头的 salt，决定哪些帧算"当代"
    frame_salt: 帧的 salt；默认与 salt 相同。传别的值就能造出
                "checkpoint 之后残留的上一代帧" —— 那正是把库搞坏的那种。
    """
    commit_on = commit_on or set()
    fs = salt if frame_salt is None else frame_salt
    head = struct.pack(">IIIIIIII",
                       0x377F0682, 3007000, page_size, 0,
                       salt[0], salt[1], 0, 0)
    out = bytearray(head)
    for pgno in sorted(pages_enc):
        dbsize = 100 if pgno in commit_on else 0
        out += struct.pack(">IIIIII", pgno, dbsize, fs[0], fs[1], 0, 0)
        out += pages_enc[pgno]
    return bytes(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="抖音 IM 解密链路离线自检")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--keep", action="store_true", help="保留 selfcheck_data/")
    a = ap.parse_args()

    print()
    print("  " + "=" * 58)
    print("   离线自检 —— 不需要手机 / 不需要 root")
    print("  " + "=" * 58)
    print()

    if not D._dep_ok("Crypto"):
        bad("pycryptodome 可用", "缺少依赖，先 pip install -r requirements.txt")
        return 1
    ok("pycryptodome 可用")

    shutil.rmtree(DATA, ignore_errors=True)
    os.makedirs(DATA, exist_ok=True)
    pkgdir = os.path.join(DATA, "com.ss.android.ugc.aweme", "databases")
    os.makedirs(pkgdir, exist_ok=True)

    # ---- 造明文（原生 reserve 布局）----
    plain_path = os.path.join(DATA, "sample_plain.db")
    build_plain(plain_path)
    conv = bytearray(open(plain_path, "rb").read())
    if conv[20] != D.RESERVE:
        bad("明文样本带 reserve 布局", f"页头第 20 字节 = {conv[20]}，应为 {D.RESERVE}")
        return 1

    conv_path = os.path.join(DATA, "_converted.db")
    open(conv_path, "wb").write(bytes(conv))
    c = sqlite3.connect(conv_path)
    n_msg = c.execute("SELECT count(*) FROM msg").fetchone()[0]
    n_conv = c.execute("SELECT count(*) FROM conversation_list").fetchone()[0]
    integ = c.execute("PRAGMA integrity_check").fetchone()[0]
    c.close()
    if (n_msg, n_conv, integ) == (48, 2, "ok"):
        ok("明文样本布局正确", f"reserve={conv[20]} 回读 msg={n_msg} 会话={n_conv} integrity=ok")
    else:
        bad("明文样本布局正确", f"msg={n_msg} 会话={n_conv} integrity={integ}")
        return 1

    # ---- 加密 ----
    salt = os.urandom(D.FILE_HEADER_SZ)
    key, hmac_key = D.derive_keys(D.passphrase_for_uid(UID), salt)
    enc = encrypt_pages(conv, salt, key, hmac_key)
    enc_path = os.path.join(pkgdir, f"encrypted_{UID}_im.db")
    open(enc_path, "wb").write(enc)
    ok("生成加密库", f"{os.path.basename(enc_path)}  {len(enc)} 字节")

    # 口令模板
    if D.passphrase_for_uid(UID) == f"byte{UID}imwcdb{UID}dance":
        ok("口令模板正确")
    else:
        bad("口令模板正确", D.passphrase_for_uid(UID))

    # ---- 解密 ----
    try:
        with quiet():
            dec_path = D.decrypt_db(enc_path, verbose=False)
    except SystemExit as e:
        bad("解密成功", f"退出码 {e.code}")
        return 1
    d = sqlite3.connect(dec_path)
    got = d.execute("SELECT count(*) FROM msg").fetchone()[0]
    integ = d.execute("PRAGMA integrity_check").fetchone()[0]
    sample = d.execute("SELECT content FROM msg ORDER BY msg_server_id LIMIT 1").fetchone()[0]
    d.close()
    if got == 48 and integ == "ok":
        ok("解密结果完整", f"msg={got} integrity={integ}")
    else:
        bad("解密结果完整", f"msg={got} integrity={integ}")

    # 逐页比对：解密结果应与转换后的明文库逐字节一致
    if open(dec_path, "rb").read() == bytes(conv):
        ok("逐字节还原一致")
    else:
        bad("逐字节还原一致", "解密结果与原始明文不符")

    # ---- 错误 uid 必须被拒 ----
    try:
        with quiet():
            D.decrypt_db(enc_path, out_path=os.path.join(DATA, "_bad.db"),
                         uid="9999999999", verbose=False)
        bad("错误 uid 被拒绝", "居然解密成功了")
    except SystemExit:
        ok("错误 uid 被拒并报错退出")
    except Exception as e:
        ok("错误 uid 被拒", type(e).__name__)

    # ---- WAL 合并 ----
    # 构造 WAL：第 2、3 页用同样的内容（等价于"重写但没改数据"），
    # 最后一页标成未提交（dbsize=0），应被丢弃。
    npages = len(enc) // D.PAGE_SIZE
    frames = {}
    for pgno in (2, 3):
        off = (pgno - 1) * D.PAGE_SIZE
        frames[pgno] = enc[off:off + D.PAGE_SIZE]
    off = (npages - 1) * D.PAGE_SIZE
    frames[npages] = enc[off:off + D.PAGE_SIZE]      # 未提交帧
    wal = build_wal(frames, commit_on={2, 3})
    wal_path = enc_path + "-wal"
    open(wal_path, "wb").write(wal)

    pages, nfr, _dbsz = D._parse_wal(wal_path, key, hmac_key)
    if nfr == 2 and set(pages) == {2, 3}:
        ok("WAL 合并：未提交帧被截断", f"解析 {nfr} 帧，生效 {sorted(pages)}")
    else:
        bad("WAL 合并：未提交帧被截断", f"解析 {nfr} 帧，生效 {sorted(pages)}")

    try:
        dec2 = None
        with quiet():
            dec2 = D.decrypt_db(enc_path, out_path=os.path.join(DATA, "_wal.db"),
                                verbose=False)
        d2 = sqlite3.connect(dec2)
        n2 = d2.execute("SELECT count(*) FROM msg").fetchone()[0]
        ic2 = d2.execute("PRAGMA integrity_check").fetchone()[0]
        d2.close()
        if n2 == 48 and ic2 == "ok":
            ok("带 WAL 解密仍完整", f"msg={n2} integrity={ic2}")
        else:
            bad("带 WAL 解密仍完整", f"msg={n2} integrity={ic2}")
    except SystemExit as e:
        bad("带 WAL 解密仍完整", f"退出码 {e.code}")

    # ★ 回归测试：整条 WAL 都是上一代残留帧（salt 对不上）时，必须整体忽略。
    #   这是真机上真实踩过的坑 —— 不校验 salt 就会把陈旧页盖到新页上，
    #   结果 sqlite 直接报 database disk image is malformed。
    stale = build_wal(frames, commit_on={2, 3},
                      salt=(0xAAAAAAAA, 0xBBBBBBBB),
                      frame_salt=(0x11223344, 0x55667788))
    open(wal_path, "wb").write(stale)
    pages_s, nfr_s, _ = D._parse_wal(wal_path, key, hmac_key)
    if nfr_s == 0 and not pages_s:
        ok("WAL 陈旧代帧被整体忽略", "salt 不匹配 -> 0 帧生效")
    else:
        bad("WAL 陈旧代帧被整体忽略", f"居然解析出 {nfr_s} 帧")

    with quiet():
        dec3 = D.decrypt_db(enc_path, out_path=os.path.join(DATA, "_stale.db"),
                            verbose=False)
    if open(dec3, "rb").read() == bytes(conv):
        ok("陈旧 WAL 不影响解密结果", "逐字节与原始明文一致")
    else:
        bad("陈旧 WAL 不影响解密结果", "结果被陈旧帧污染了")

    # 全未提交的 WAL 应被整体丢弃
    wal2 = build_wal(frames, commit_on=set())
    open(wal_path, "wb").write(wal2)
    pages2, nfr2, _ = D._parse_wal(wal_path, key, hmac_key)
    if nfr2 == 0 and not pages2:
        ok("WAL 全未提交时整体丢弃")
    else:
        bad("WAL 全未提交时整体丢弃", f"解析 {nfr2} 帧")
    os.remove(wal_path)

    # ---- 读库 ----
    try:
        rd = D.Reader([dec_path])
        talkers = rd.talkers()
        names = sorted(rd.name_of(c) for c, _ in talkers)
        if len(rd.msgs) == 48 and len(talkers) == 2 and names == ["小张", "老王"]:
            ok("读库解析正确", f"{len(rd.msgs)} 条 / 会话 {names}")
        else:
            bad("读库解析正确", f"{len(rd.msgs)} 条 / 会话 {names}")

        # "我是谁" 靠 conversation_id 推断，不能靠硬编码
        me = [m for m in rd.msgs if m["is_me"]]
        acct = {m["account"] for m in rd.msgs}
        if len(me) == 24 and acct == {UID}:
            ok("自己 uid 判定正确", f"我方 {len(me)} 条 / 账号 {sorted(acct)}")
        else:
            bad("自己 uid 判定正确", f"我方 {len(me)} 条 / 账号 {sorted(acct)}")

        # 表情消息 content 里没文本，必须由 fts_search_msg_biz 补上
        sticker = [m for m in rd.msgs if m["type"] == 15]
        if sticker and all(m["content"] == "[表情] Hi" for m in sticker):
            ok("搜索索引补全内容", f"{len(sticker)} 条表情读到 {sticker[0]['content']!r}")
        else:
            bad("搜索索引补全内容", f"读到 {[m['content'] for m in sticker]}")

        gone = [m for m in rd.msgs if m["deleted"]]
        if len(gone) == 2:
            ok("已删除消息被标记保留", f"{len(gone)} 条")
        else:
            bad("已删除消息被标记保留", f"{len(gone)} 条")

        rd2 = D.Reader([dec_path], keep_deleted=False)
        if len(rd2.msgs) == 46:
            ok("--no-deleted 生效", f"{len(rd2.msgs)} 条")
        else:
            bad("--no-deleted 生效", f"{len(rd2.msgs)} 条")

        if rd.account_of(CONV_A) == UID and rd.is_group(CONV_A) is False:
            ok("会话元信息正确", f"账号 {rd.account_of(CONV_A)} / 单聊")
        else:
            bad("会话元信息正确", f"账号 {rd.account_of(CONV_A)}")
    except Exception as e:
        bad("读库解析正确", f"{type(e).__name__}: {e}")
        rd = None

    # ---- 导出 ----
    try:
        if rd is None:
            raise RuntimeError("读库失败，跳过导出")
        for fmt in D.EXPORTERS:
            outdir = os.path.join(DATA, "export", fmt)
            os.makedirs(outdir, exist_ok=True)
            with quiet():
                D.export(rd, DATA, fmt, 1)
            n = len([f for f in os.listdir(outdir) if f != "index.html"])
            if n == 2:
                ok(f"导出 {fmt}")
            else:
                bad(f"导出 {fmt}", f"只产出 {n} 个文件")
    except SystemExit as e:
        bad("导出", f"退出码 {e.code}")
    except Exception as e:
        bad("导出", f"{type(e).__name__}: {e}")

    # ---- 明文库自动发现 ----
    try:
        plain_pkg = os.path.join(pkgdir, "mi_pigeon_999_aid350593_im.db")
        shutil.copy2(dec_path, plain_pkg)
        found = D.find_plain_dbs(DATA)
        if any(os.path.basename(p) == os.path.basename(plain_pkg) for p in found):
            ok("明文库自动发现", f"{len(found)} 个")
        else:
            bad("明文库自动发现", f"只找到 {[os.path.basename(p) for p in found]}")
    except Exception as e:
        bad("明文库自动发现", f"{type(e).__name__}: {e}")

    # ---- 汇总 ----
    print()
    print("  " + "=" * 58)
    if FAIL:
        print(f"   {len(PASS)} 项通过，{len(FAIL)} 项失败")
        for f in FAIL:
            print(f"     - {f}")
        rc = 1
    else:
        print(f"   全部 {len(PASS)} 项通过 ✓  链路可用")
        rc = 0
    print("  " + "=" * 58)
    print()

    if not a.keep and not FAIL:
        shutil.rmtree(DATA, ignore_errors=True)
    else:
        print(f"  自检数据保留在: {DATA}")
        print()

    return rc


if __name__ == "__main__":
    sys.exit(main())
