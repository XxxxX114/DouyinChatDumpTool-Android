# dydump-android

> 视频会被刷走，说过的话不会。

从一台**已 root 的 Android 手机**里取出抖音私信：拉取 → 拼口令 → 解密 SQLCipher → 导出成可读格式。

和微信版最大的不同：**抖音的口令不用猜** —— 库文件名里就带 uid，口令是模板直接拼出来的。

> ⚠️ **仅限你自己的设备和你自己的账号。** 详见文末「合法边界」。

## 最快路径

```bash
pip install -r requirements.txt      # 一次性
python dydump_android.py doctor      # 看看还缺什么
python dydump_android.py             # 进菜单，按提示选
```

Windows 用户可以直接**双击 `start.bat`**，它会自动找 Python、检查依赖、定位 adb，然后进同一个菜单。

没有手机、只想先确认工具本身能跑通？`python selfcheck.py` 会造一份假加密库，
把 解密 → 读库 → 导出 整条链路走一遍。

---

## 特性

- **口令不用爆破**：`encrypted_<uid>_im.db` 的文件名里就有 uid，口令按固定模板拼出来，
  没有微信那种"试遍 imei × uin 组合"的环节
- **纯 Python 解密**：只依赖 `pycryptodome`，**不需要难编译的 `sqlcipher3`**
- **不落手机存储**：`stream` 模式让手机端把 tar 流经 `adb reverse` 直推电脑
- **WAL 正确合并**：带 **salt 代际校验**，只取到最后一个 commit 帧为止
  （不校验 salt 会把上一代的陈旧页盖到新页上，直接把库搞坏）
- **会话名是真昵称**：从 `im_biz` 库的联系人索引里补出来，不是一串光秃秃的数字 uid
- **内容不是裸 JSON**：优先用抖音自己维护的搜索索引，直播分享 / 视频分享 / 评论卡片
  都能给出一句人话
- **无手机自检**：`selfcheck.py` 造一份假加密库，把整条链路跑一遍，不碰真机；
  假数据复刻了真机形态，所以它同时是回归测试（24 项）

---

## 环境要求

| 项      | 说明                                                    |
| ------ | ----------------------------------------------------- |
| Python | 3.8+（开发与实测用 3.13 / 3.14）                              |
| adb    | 可选但推荐。加进 `PATH`、设 `ADB` 环境变量，或直接放到脚本旁 `platform-tools/` |
| 手机     | 需要 root；或在手机上用有 root 权限的文件管理器手动拷数据                     |
| 依赖     | `pycryptodome`（唯一必需项）                                 |

不想手动配 adb？跑一次 `python dydump_android.py adb`，会把官方 platform-tools 下载并解压到脚本旁边。
只用本地已有数据（`local` / `decrypt` / `export`）的话，**可以不装 adb**。

---

## 安装

```bash
git clone https://github.com/<you>/dydump-android.git
cd dydump-android
pip install -r requirements.txt
```

> 懒得记命令？直接双击 `start.bat`（Windows）或 `sh start.sh`（Linux/macOS）。
> 启动器会自动找 Python、检查依赖（缺了会问你要不要装）、找不到 adb 时给出提示，然后进交互菜单。

---

## 文件结构

```
dydump-android/
├── dydump_android.py     主程序：拉取 / 解密 / 读库 / 导出，也是交互菜单
├── start.bat             Windows 双击入口（找 Python -> 查依赖 -> 进菜单）
├── start.sh              Linux / macOS 入口
├── selfcheck.py          造假加密库，不用手机就能验证整条链路（24 项）
├── dystream.sh           手机端脚本：stream 模式的推流端
├── dypull.sh             手机端脚本：把数据拷到 /sdcard/dydump（pull-sd 用）
├── requirements.txt      只有 pycryptodome
├── .gitattributes        .sh 强制 LF —— Android 上 CRLF 会直接报错
└── dump/                 运行后生成，所有产物都在这里（已 gitignore）
```

`dystream.sh` / `dypull.sh` 是**推到手机上执行**的，电脑端不用跑：

```bash
adb push dystream.sh /data/local/tmp/
adb shell "su -c 'sh /data/local/tmp/dystream.sh -p 9000'"
```

---

## 快速开始

先看一眼环境，缺什么它会直说：

```bash
python dydump_android.py doctor
```

或者什么都不带跑，进菜单：

```bash
python dydump_android.py
```

```
    1) 检查环境            Python / 依赖 / adb / 设备 一次看完
    2) 一键全流程          探测 -> 拉取 -> 解密 -> 导出
    3) 处理本地已有数据    解密 -> 导出（数据已经在 dump/ 里）
    4) 手机端推流直连      stream（推荐，不落手机存储）
    5) 中转拉取            pull-sd（手机端 dypull.sh 先拷到 /sdcard/dydump）
    6) 只导出              换格式 html / csv / txt / json / md
    7) 下载 adb            platform-tools 自动解压到脚本旁
    8) 离线自检            selfcheck（不碰设备、不碰数据）
    0) 退出
```

习惯敲命令的话，等价的分步流程：

```bash
# 1) 看看手机上有什么（列出所有 IM 库）
python dydump_android.py probe

# 2) 拉取（三选一，见下一节）
python dydump_android.py stream

# 3) 解密 -> 导出
python dydump_android.py local
```

---

## 三条拉取路线

### stream（推荐）

手机端把数据打成 tar，经 `adb reverse` 通道直接推给电脑，**不落手机存储**。

```bash
# 电脑上先跑，它会建立 adb reverse 并开始监听
python dydump_android.py stream

# 然后在手机的 root 终端里跑
sh /data/local/tmp/dystream.sh -p 9000
```

`dystream.sh` 的参数：

| 参数        | 说明                        |
| --------- | ------------------------- |
| `-p <端口>` | 要和电脑端一致（默认 9000）          |
| `-k`      | 不 force-stop 抖音（默认会先杀掉，保证快照一致） |

> 脚本会自己探测 su 路径（`/system/bin/su`、`/data/adb/ksu/bin/su`、`/data/adb/ap/bin/su`、
> `/debug_ramdisk/su` …），KernelSU / Magisk / APatch 都能用。

### pull（adb shell 能拿到 root 时）

```bash
python dydump_android.py pull
```

> 多数机器上 `adb shell` 够不到 `su`（`/data/adb` 是 0700），这条路会失败。
> 这时改用 stream 或 pull-sd。

### pull-sd（中转，最稳）

手机端先把数据拷到 `/sdcard`，再用普通 `adb pull` 拉回来。

```bash
# 手机上
sh dypull.sh            # 默认拷到 /sdcard/dydump
sh dypull.sh -t         # 顺手打个 tar，方便走 MTP 直接拖

# 电脑上
python dydump_android.py pull-sd
```

---

## 口令是怎么来的

### 口令是模板拼的

WCDB 2 用固定模板拼口令，`<uid>` 就是登录用户的纯数字 ID —— 它同时出现在库文件名里：

```
passphrase = "byte" + <uid> + "imwcdb" + <uid> + "dance"
```

例如 `encrypted_1234567890_im.db` → `byte1234567890imwcdb1234567890dance`

**这不是密码**，是模板字符串，真正的密钥由 KDF 展开。所以拿到文件，uid 和口令就都确定了。

### 密钥展开两步（`key` 和 `hmac_key` 是两把不同的密钥）

```
salt      = 加密文件的前 16 字节（明文存储，随机生成）
key       = PBKDF2-HMAC-SHA1(passphrase, salt, 64000, 32 字节)
hmac_salt = salt 逐字节 XOR 0x3a
hmac_key  = PBKDF2-HMAC-SHA1(key, hmac_salt, 2, 32 字节)   ← 只有 2 次迭代
```

### 页布局（4096 字节 / 页，reserve = 48）

```
第 1 页   [0,16)     盐（明文，不加密）
          [16,4048)  AES-256-CBC 密文（4032 字节）
第 N 页   [0,4048)   AES-256-CBC 密文（4048 字节）
共用      [4048,4064) IV
          [4064,4084) HMAC-SHA1
          [4084,4096) 随机填充
```

- **HMAC 覆盖范围** = 密文 + IV + 页号（**4 字节小端**），所以篡改或重排页都会被发现
- **reserve** = iv(16) + hmac(20) = 36，向上取整到 16 的倍数 = **48**
- 加密方案：`SQLCipher v3 (AES-256-CBC, HMAC-SHA1, PBKDF2-SHA1 64000, 4096 page)`

### 一个关键小技巧

SQLite 文件头**第 20 字节**就是"每页保留字节数"。解密后把它改成 48，
标准 `sqlite3` 就能直接打开解密结果 —— **不需要重排页内容**。

---

## 解密

### HMAC 是最好的"正确性探针"

uid 或参数猜错，第 2 页的 HMAC 立刻失败。所以**不用肉眼比对乱码**：
HMAC 全通过就说明密钥、页布局、页号字节序全部正确。反之会直接报错退出，不会写出错文件。

### WAL 合并：必须校验 salt 代际

SQLite 在 WAL 模式下，最近的消息可能还在 `-wal` 文件里没落盘。
本项目会解析 `-wal`（32 字节头 + 每帧 24 字节帧头 + 一整页），
**只应用到最后一个 commit 帧为止**，未提交的脏数据丢弃。

但真机上踩过一个很坑的雷：**WAL 是环形日志**。checkpoint 完会换一代新的 salt
从文件头部重写，旧代的帧还留在文件里，但已经作废了。

```
WAL 头 [16,20) 和 [20,24) 是两段 salt（大端）
逐帧比对帧头 [8,12) / [12,16) 的 salt
  · 匹配   → 这一帧属于当代，参与合并
  · 不匹配 → 上一代残留，丢掉
全部不匹配 → 整条 WAL 没有任何待合并内容，直接忽略（这是常态，不是错误）
```

合并后库大小以**最后一帧的 dbsize** 为准（WAL 做过截断时尾页要丢掉）。

> 顺带一提：这也是识别"哪个账号还在用"的可靠信号 ——
> 谁的 `-wal` 里有匹配当代 salt 的帧，谁就是当前登录的账号。

### 解密是增量的

库没变就跳过，加 `--force` 强制重解。

---

## 读库

### 内容：优先用官方生成的"可搜索文本"

`msg.content` 是 JSON，而且形态随版本变。更省事的是抖音自己维护的
`fts_search_msg_biz.search_content` —— 它对**所有消息类型**都给了一句人类可读的话：

| type    | `content` 原文                              | `search_content`  |
| ------- | ----------------------------------------- | ----------------- |
| 21 直播分享 | `{"aweType":0,"cover_url":{...}}`         | `某某的直播间`         |
| 8 视频分享  | `{"anchor_info":{"icons":[...]}}`         | `某条视频的文案…` |
| 105 评论分享 | `{"aweType":10500,"comment":"<评论原文>"}` | `<评论原文>，带昵称后缀` |

所以读取顺序是：**`search_content` → 解析 `content` JSON → 纯文本原样**。
三级都取不到就显示 `（图片）` 这样的占位，**绝不把一坨 JSON 塞进聊天记录**。

### 昵称：藏在 im_biz 库的联系人索引里

单聊在抖音本地库里**不存对方昵称**，`conversation_core.name` 是空的。
如果直接拿 `conversation_id` 里的 uid 当名字，导出来就是一串纯数字 uid。

真正的昵称在另一个库里：`encrypted_im_biz_<uid>.db`（元数据库，没有 `msg` 表）。
它有一张 `fts_contact_index_table_content`：

| 列                   | 内容          |
| ------------------- | ----------- |
| `docid`             | **就是对方的 uid** |
| `c0fts_remark_name` | 你给 ta 的备注名  |
| `c1fts_nick_name`   | ta 的抖音昵称    |
| `c2fts_dy_id`       | ta 的抖音号     |

字段值是 `<名字> <名字> <拼音/分段…>` 的形式（例如
`"某某某 某某某 moumoumou mmm ..."`），
所以要取**最长**的那个"重复两遍"的前缀才是原名 —— 名字本身可能带空格。

> ⚠️ 不要直接查 `fts_contact_index_table` 虚表本体：它用字节自研的
> `mmicu` 分词器，标准 sqlite3 打不开（报 `unknown tokenizer: mmicu`）。
> 读它的 content 影子表就行。

### 明文库也要单独捡

抖音的 `databases/` 目录里塞了几百个 sqlite 文件，其中一部分 IM 库
**根本没加密**，文件名也不带 `encrypted_` 前缀 —— 例如
`mi_pigeon_<uid>_aid<aid>_im.db`。它们和主库是同一套表结构、同样有聊天数据，
不挑出来就会整块丢掉。

工具会做两道筛子：文件头是 `SQLite format 3`，**且**里面真有
`msg` + `conversation_list` 表。只靠"是不是 sqlite"会把
`aweme.db` / `logdb.db` / `ut.db` 之类无关库全捞进来。

### 已删除的消息要留着

`msg.deleted = 1` 的消息，**内容和时间在本地库里都还在**，只是界面上不显示了。
做备份就该留着，所以默认导出并打上"已删除"标记（`--no-deleted` 可关掉）。

### 表结构

| 表                                                                | 内容                                          |
| ---------------------------------------------------------------- | ------------------------------------------- |
| `conversation_list`                                              | 会话列表（`type=1` 单聊 / `type=2` 群聊，`participant` 列是群成员 JSON） |
| `conversation_core`                                              | 会话信息（**群昵称在这里**，单聊是空的）                      |
| `msg`                                                            | 消息记录                                        |
| `fts_search_msg_biz`                                             | 每条消息的可读文本                                   |
| `fts_contact_index_table_content`（在 `encrypted_im_biz_<uid>.db`） | 联系人：昵称 / 抖音号 / 备注                            |

---

## 导出格式

| `--format` | 产物         | 说明                      |
| ---------- | ---------- | ----------------------- |
| `html`     | `名字.html`  | **推荐**。气泡样式，区分收发，支持深色模式 |
| `csv`      | `名字.csv`   | utf-8-sig，带"方向"和"已删除"列  |
| `txt`      | `名字.txt`   | 一行一条，带类型标记              |
| `json`     | `名字.json`  | 结构化，带真实时间戳和账号           |
| `md`       | `名字.md`    | Markdown 列表             |

每次导出都会额外写一份 `export/<格式>/index.html` 总览（按消息数排序，可点进详情）。

```bash
python dydump_android.py export --format html
python dydump_android.py export --format csv --min-msgs 50    # 过滤小会话
python dydump_android.py export --format html --no-deleted    # 不带已删除消息
```

多账号（换过号、登录过多个账号）会**自动合并**，总览页多一列"账号"；
主库和 `sub_` 库的重叠消息按 `msg_uuid` 去重。

---

## 实测记录：真机上的坑

下面这些是拿一份真实库调出来的，只看文档想不到，不处理会**静默丢数据或直接解出损坏的库**。

| 现象                                    | 真相                             | 处理                          |
| ------------------------------------- | ------------------------------ | --------------------------- |
| 合了 WAL 反而 `database disk image is malformed` | WAL 是环形日志，文件里全是**上一代作废帧**      | 逐帧校验 salt 代际                 |
| `PRAGMA integrity_check` 报 `unknown tokenizer: mmicu` | 抖音 FTS 虚表用自研分词器，**不是数据损坏**     | 读 content 影子表；或临时剔掉虚表再校验     |
| 会话名是一串数字                              | 单聊本地库**不存对方昵称**                | 从 `im_biz` 库的联系人索引补            |
| 直播 / 视频 / 评论消息导出成一坨 JSON             | `content` 是 JSON，且形态随版本变       | 优先读 `fts_search_msg_biz.search_content` |
| 漏掉一整个库的聊天数据                           | 部分 IM 库**根本没加密**，文件名也不带 `encrypted_` | 按"魔数 + 真有 IM 表"两道筛子捡        |
| 自造样本一加数据就损坏                           | 手工平移 reserve 布局时漏了空闲块链头指针      | 见下                          |

### 1. WAL 是环形日志，salt 必须校验

实测某台机器上一个 1018 帧的 `-wal`，帧的 salt 全是 `0x1a2b3c4e` / `0x1a2b3c4c`…，
而 WAL 头的 salt 是 `0x1a2b3c4f` —— **一帧都不匹配，整条 WAL 全是陈旧数据**。

不校验 salt 就会把这些陈旧页盖到新页上，sqlite 直接报
`database disk image is malformed`。

这个坑已经固化成回归测试（`selfcheck.py` 里的"WAL 陈旧代帧被整体忽略"）。

### 2. `unknown tokenizer: mmicu` 不是数据损坏

抖音的 FTS 虚表用了自研分词器，标准 sqlite3 打不开。
`PRAGMA integrity_check` 会去解析 FTS 虚表的 schema，所以也会跟着报错 ——
**库本身是好的**。工具的读库逻辑会自动跳过这类表。

### 3. 单聊不存昵称，群聊才存

- 单聊：`conversation_core.name` 为空，`participant.alias` 也多为空 →
  只能靠 `im_biz` 库的联系人索引（命中率约 85%，没互关的查不到，属正常）
- 群聊：`conversation_core.name` 就是群名，`conversation_list.participant` 是成员 JSON

### 4. `msg.type` 是个不断膨胀的枚举

```
1 系统提示   5 戳一戳    7 文本     8 视频分享   15 表情
17 语音      21 直播分享  26 活动卡片 27 图片      30 图片
67 商品分享  70 卡片消息  73 语音通话 77 作品分享  88 小程序
105 评论分享 110 分享卡片 144 作品卡片 501 语音
1001 在线状态  40001 已读回执  9996 空消息
```

`1001` / `40001` / `9996` 是纯状态位、没有任何内容，默认丢弃（`--keep-noise` 可保留）。
**版本迭代会变**，程序对未知值原样保留成 `type=N`，不会丢数据。

### 5. 自造样本：不要把密实的库硬转 reserve 布局

`selfcheck.py` 要造一个"原生带 reserve 保留空间"的明文库。
一开始的做法是"建一个 reserve=0 的库再手工平移 48 字节"，结果必然损坏：

- 页面接近写满时，内容区上移 48 字节会和单元格指针数组**重叠**
- 页头 `header+1` 的**空闲块链头指针**也忘了跟着平移

正确做法：先只建一张极小的表，手工平移 page 1 并把页头第 20 字节改成 48，
**之后才让 SQLite 打开** —— 此后 SQLite 就按 `usable = page_size - reserve` 排版，
后面所有表和索引天生带保留空间。

（若确实要手工平移一页，必须同步改四处：内容区、页头"内容区起始偏移"（header+5）、
所有单元格指针、**空闲块链的链头（header+1）和链内 next**。）

### 6. 一批旧版遗留库解不开

`encrypted_<uid>_aid1383_im.db`、`encrypted_mi_pigeon_*_aid1383_im.db` 这批用的是
另一套加密，**密钥不来自 uid**。

实测 1155 种口令模板组合、KDF 迭代 ∈ {1, 2, 4000, 64000, 256000}、
HMAC ∈ {sha1, sha256, sha512}、密钥长度 ∈ {16, 32}、
页大小 ∈ {1024, 4096}、reserve ∈ {0, 16, 32, 48, 80} —— 全部不中。
很可能密钥存在 Android Keystore 里，**离线无解**。

它们是迁移前的历史库，不影响主库数据。工具会明确列出来并跳过，不会静默失败。

> 它们的 `-mbak` 文件倒是有用：结构是 `[12 字节头][16 字节 salt][zlib 压缩的 schema 备份]`，
> `zlib.decompress(d[28:])` 就能读出旧版 `CREATE TABLE` 语句。

---

## 命令参考

```
python dydump_android.py                      交互菜单
python dydump_android.py doctor               环境自检：Python / 依赖 / adb / 设备
python dydump_android.py adb                  自动下载 platform-tools
python dydump_android.py probe                列出设备上的抖音 IM 库
python dydump_android.py pull                 adb 直拉（需 adb 能 root）
python dydump_android.py pull-sd              从中转目录拉（手机端先跑 dypull.sh）
python dydump_android.py stream               手机推流直连（手机端跑 dystream.sh）
python dydump_android.py decrypt              解密 dump/ 下所有 encrypted_*.db
python dydump_android.py export               导出（--format html|csv|txt|json|md）
python dydump_android.py local                解密 + 导出
python dydump_android.py all                  探测 + 拉取 + 解密 + 导出
python selfcheck.py                           离线自检（不需要手机）
```

| 参数             | 说明                                           |
| -------------- | -------------------------------------------- |
| `--src`        | 改工作目录                                        |
| `--uid`        | 手动指定 uid（文件名认不出来时用）                          |
| `--force`      | 忽略增量缓存，强制重解所有库                               |
| `--format`     | 导出格式 `html` / `csv` / `txt` / `json` / `md`   |
| `--min-msgs`   | 少于该条数的会话跳过                                   |
| `--no-deleted` | 不导出本地已删除的消息                                  |
| `--keep-noise` | 保留在线状态 / 已读回执这类无内容系统消息                       |
| `--port`       | 推流端口                                         |

---

## 疑难排查

### `全部 N 页 HMAC 校验失败`

uid 不对（文件名被改过？用 `--uid` 指定），或者这根本不是抖音 IM 库。

### `unknown tokenizer: mmicu`

正常现象，不是数据损坏。抖音 FTS 虚表用了自研分词器，标准 sqlite3 打不开。
工具已自动跳过，不影响消息读取。

### 某几个库提示「解密失败，跳过」

多半是 `*_aid1383_*` 这类旧版遗留库。实测其密钥**不是 uid 派生**的，
离线解不开（见 [实测记录 6](#6-一批旧版遗留库解不开)）。不影响主库数据。

### 会话名是一串数字

该 uid 不在联系人索引里（没互关 / 没聊过）。属正常，不影响消息。

### 装了 root 管理端，但 adb shell 拿不到 root

抖音的库在 app 私有目录，`/data/adb` 是 0700，`adb shell` 够不到 `su`。
改用 `stream` 或 `pull-sd`，在**手机上的 root 终端**里跑脚本。

### 只拿到一半消息 / 最新消息缺失

检查 `-wal` 文件有没有一起拉过来。推流和中转都会带上，`adb pull` 单拉 `.db` 会漏。

### Git Bash 把设备路径改写了

`/data/local/tmp` 会被改写成 `C:/Program Files/Git/data/...`。
程序内部已经做了还原，但你自己敲 `adb` 命令时注意加 `MSYS_NO_PATHCONV=1`。

### 提示找不到 adb

```bash
python dydump_android.py adb        # 自动下载 platform-tools 到脚本旁
```

### 依赖装不上

只需要 `pycryptodome`，纯 Python wheel，正常网络都能装。
本项目**不需要** `sqlcipher3` —— 解密是自己实现的。

### 不需要手机也能验证链路

```bash
python selfcheck.py
```

预期看到 `全部 24 项通过 ✓`。它覆盖：明文样本布局、加解密往返、错误 uid 拒绝、
WAL 合并 / 陈旧代帧忽略 / 未提交截断、读库解析、自己 uid 判定、搜索索引补内容、
已删除标记、明文库发现、5 种导出。

---

## 已知限制

- **媒体文件不在库里**：图片 / 语音 / 视频只存 CDN 引用，CDN 签名约 1 年过期，
  过期后需要重新从网络侧抓取
- **旧版遗留库解不开**：见 [实测记录 6](#6-一批旧版遗留库解不开)
- **昵称命中率约 85%**：没互关、没聊过的联系人查不到昵称，只能显示 uid
- **必须 root**：库在 app 私有目录，没有免 root 路线
- 抖音版本升级可能更换加密策略，工具失效属常态，先小范围验证
- 明文库含全部聊天记录且**无任何加密**，注意保管

---

## 合法边界

本项目只用于**导出你自己设备上、你自己账号的聊天记录**。

请勿用于：

- 破解、窃取他人账号或聊天记录
- 破解商业软件牟利
- 任何违反当地法律法规的用途

使用者需自行承担因使用本工具产生的一切法律责任。
作者不对任何滥用行为负责。

---

## 致谢

- 加密参数与文件名约定参考 [chinleez/aweme_db_decrypt](https://github.com/chinleez/aweme_db_decrypt)（Rust 实现，跨平台含 Android arm64）
- 项目骨架与交互形态参考同目录的 `WeChatDumpTool-Android`

## License

[MIT](LICENSE)

---

> **视频会被刷走，说过的话不会。**
