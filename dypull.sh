#!/system/bin/sh
# ==============================================================
# dypull.sh -- 在手机上把抖音 IM 数据搬到中转目录，供电脑端拉取
#
# 在 Termux / KernelSU 终端 / MT 管理器的终端里跑：
#     sh dypull.sh
#
# 参数：
#     -o <目录>   输出目录（默认自动探测 /sdcard/dydump）
#     -k          不 force-stop 抖音（默认会先杀掉，保证数据一致）
#     -t          额外打一个 tar 包，方便走 MTP 直接拖走
#     -h          帮助
#
# 跑完在电脑上执行：
#     python dydump_android.py pull-sd
# ==============================================================

SAVED="$@"
OUT=""
KILL=1
TARBALL=0
PKGS="com.ss.android.ugc.aweme com.ss.android.ugc.aweme.lite"

usage() { sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
    case "$1" in
        -o) OUT="$2"; shift 2 ;;
        -k) KILL=0; shift ;;
        -t) TARBALL=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "[!] 未知参数: $1"; usage; exit 1 ;;
    esac
done

say() { echo "[*] $*"; }
err() { echo "[!] $*" >&2; }

# ---------- 0. root ----------
# adb shell 通常够不到 su（/data/adb 是 0700），所以在终端里跑这个脚本时
# 逐个候选路径试一遍，谁能出 uid=0 就用谁。
SU_CANDIDATES="su /system/bin/su /system/xbin/su /sbin/su \
/data/adb/ksu/bin/su /data/adb/ap/bin/su /debug_ramdisk/su /data/local/tmp/su"

SU_CMD=""
try_root() {
    for c in $SU_CANDIDATES; do
        if command -v "$c" >/dev/null 2>&1 || [ -x "$c" ]; then
            r=$("$c" -c id 2>/dev/null)
            case "$r" in
                *uid=0*) SU_CMD="$c"; return 0 ;;
            esac
        fi
    done
    return 1
}

if [ "$(id -u)" = "0" ]; then
    say "已经是 root"
elif try_root; then
    say "用 $SU_CMD 提权（手机上弹窗就点允许）"
    SELF="$0"
    case "$SELF" in /*) ;; *) SELF="$(pwd)/$SELF" ;; esac
    exec "$SU_CMD" -c "sh '$SELF' $SAVED"
else
    err "拿不到 root。已试过:"
    for c in $SU_CANDIDATES; do err "    $c"; done
    echo
    err "从 adb shell 够不到 su 是正常的（/data/adb 权限 0700）。"
    err "请在下面这些地方跑本脚本，它们能向 KernelSU/Magisk 申请授权："
    err "  · MT 管理器的 root 终端 / 文件管理"
    err "  · KernelSU 管理器自带的终端"
    err "  · 你的提权脚本拿到的 root shell"
    exit 1
fi

# ---------- 1. 选输出目录 ----------
if [ -z "$OUT" ]; then
    if [ -d /sdcard ]; then
        OUT=/sdcard/dydump
    elif [ -d "$HOME/storage/shared" ]; then
        OUT="$HOME/storage/shared/dydump"
    else
        OUT=/data/local/tmp/dydump
    fi
fi
say "输出目录: $OUT"

# ---------- 2. 停抖音 ----------
if [ "$KILL" = "1" ]; then
    say "force-stop 抖音（避免复制到一半数据库还在写）"
    for PKG in $PKGS; do
        am force-stop "$PKG" 2>/dev/null
    done
    sleep 2
fi

# ---------- 3. 逐包复制 ----------
cpf() {
    # 复制并校验大小
    s="$1"; t="$2"
    [ -f "$s" ] || return 0
    if cp -f "$s" "$t" 2>/dev/null; then
        a=$(wc -c < "$s" 2>/dev/null || echo 0)
        b=$(wc -c < "$t" 2>/dev/null || echo 0)
        if [ "$a" = "$b" ]; then
            echo "    OK   ${s##*/}  ($a bytes)"
        else
            err "大小不一致 ${s##*/}: $a -> $b"
        fi
    else
        err "复制失败: ${s##*/}"
    fi
}

GOT=0
for PKG in $PKGS; do
    D="/data/data/$PKG/databases"
    [ -d "$D" ] || continue
    FILES=$(ls -1 "$D" 2>/dev/null | grep -E '^encrypted_.*\.db(-wal|-shm)?$')
    [ -z "$FILES" ] && continue

    GOT=1
    say "── $PKG ──"
    DEST="$OUT/$PKG/databases"
    mkdir -p "$DEST" || { err "无法创建 $DEST，换个 -o 路径"; exit 1; }

    for f in $FILES; do
        cpf "$D/$f" "$DEST/$f"
    done

    # shared_prefs 里有 uid，可作交叉验证
    if [ -d "/data/data/$PKG/shared_prefs" ]; then
        say "复制 shared_prefs ..."
        DESTP="$OUT/$PKG/shared_prefs"
        mkdir -p "$DESTP"
        for f in /data/data/"$PKG"/shared_prefs/*.xml; do
            [ -f "$f" ] || continue
            cpf "$f" "$DESTP/${f##*/}"
        done
    fi
done

if [ "$GOT" != "1" ]; then
    err "没找到任何 encrypted_*.db"
    err "确认抖音装过并登录过，且已授予 root。"
    exit 1
fi

# ---------- 4. 让 adb shell 读得到 ----------
chmod -R a+rX "$OUT" 2>/dev/null

# ---------- 5. 可选打包 ----------
if [ "$TARBALL" = "1" ]; then
    T="${OUT}.tar"
    say "打包 -> $T"
    rm -f "$T" 2>/dev/null
    ( cd "$OUT" && tar -cf "$T" . ) || err "打包失败"
    [ -f "$T" ] && echo "    OK   $T  ($(wc -c < "$T") bytes)"
fi

# ---------- 6. 汇总 ----------
echo
echo "=============================================================="
echo "完成。$OUT 结构："
echo "=============================================================="
find "$OUT" -maxdepth 3 -name 'encrypted_*' 2>/dev/null | sed 's|^|  |'
echo
echo "占用: $(du -sh "$OUT" 2>/dev/null | cut -f1)"
echo
echo "下一步（电脑上）："
echo "  python dydump_android.py pull-sd"
if [ "$OUT" != "/sdcard/dydump" ]; then
    echo "  注意：输出目录不是默认的，要加 --from"
    echo "  python dydump_android.py pull-sd --from $OUT"
fi
echo "  python dydump_android.py decrypt"
echo "  python dydump_android.py export --format html"
echo "=============================================================="
