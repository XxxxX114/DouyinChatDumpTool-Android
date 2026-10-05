#!/system/bin/sh
# ==============================================================
# dystream.sh -- 以 root 运行，把抖音 IM 数据 tar 流直接推给电脑
#
# 不落手机存储：tar 输出直接走 adb reverse 建立的通道进电脑。
#
# 顺序：
#   1) 电脑上先跑   python dydump_android.py stream
#   2) 手机上再跑   sh dystream.sh              <- 本脚本
#
# 参数：
#   -p <端口>   目标端口，要和电脑端一致（默认 9000）
#   -k          不 force-stop 抖音（默认会先杀掉，保证快照一致）
#   -h          帮助
# ==============================================================

PORT=9000
KILL=1
SAVED="$@"

PKGS="com.ss.android.ugc.aweme com.ss.android.ugc.aweme.lite"

usage() { sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
    case "$1" in
        -p) PORT="$2"; shift 2 ;;
        -k) KILL=0; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "[!] 未知参数: $1"; usage; exit 1 ;;
    esac
done

say() { echo "[*] $*"; }
err() { echo "[!] $*" >&2; }

# ---------- root ----------
SU_CANDIDATES="su /system/bin/su /system/xbin/su /sbin/su \
/data/adb/ksu/bin/su /data/adb/ap/bin/su /debug_ramdisk/su /data/local/tmp/su"

SU_CMD=""
try_root() {
    for c in $SU_CANDIDATES; do
        if command -v "$c" >/dev/null 2>&1 || [ -x "$c" ]; then
            r=$("$c" -c id 2>/dev/null)
            case "$r" in *uid=0*) SU_CMD="$c"; return 0 ;; esac
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
    err "拿不到 root。请在 MT 管理器 / KernelSU 管理器的终端里跑本脚本。"
    exit 1
fi

# ---------- 找包与库 ----------
say "扫描抖音数据库..."
TARGETS=""
for PKG in $PKGS; do
    D="$PKG/databases"
    [ -d "/data/data/$D" ] || continue
    FILES=$(ls -1 "/data/data/$D" 2>/dev/null | grep -E '^encrypted_.*\.db(-wal|-shm)?$')
    [ -z "$FILES" ] && continue
    say "  $PKG"
    for f in $FILES; do
        echo "      $f  ($(wc -c < "/data/data/$D/$f" 2>/dev/null || echo 0) bytes)"
    done
    # 顺手带上 shared_prefs（里面有 uid，可作交叉验证）
    if [ -d "/data/data/$PKG/shared_prefs" ]; then
        TARGETS="$TARGETS $D $PKG/shared_prefs"
    else
        TARGETS="$TARGETS $D"
    fi
done

if [ -z "$TARGETS" ]; then
    err "没找到任何 encrypted_*.db"
    err "确认抖音装过并登录过，且已授予 root。"
    exit 1
fi

# ---------- 停抖音 ----------
if [ "$KILL" = "1" ]; then
    say "force-stop 抖音（保证快照一致）"
    for PKG in $PKGS; do
        am force-stop "$PKG" 2>/dev/null
    done
    sleep 2
fi

# ---------- 等电脑端就绪 ----------
# 电脑端会一直 accept 到收到非空流为止，所以这里的预检（产生 0 字节连接）
# 不会把机会用掉，可以放心重试。
say "等待电脑端就绪 ..."
i=0
READY=0
while [ "$i" -lt 60 ]; do
    if nc -w 2 127.0.0.1 "$PORT" </dev/null >/dev/null 2>&1; then
        READY=1
        break
    fi
    i=$((i + 1))
    if [ $((i % 5)) = 0 ]; then
        echo "    还在等 ... (${i}s)"
    fi
    sleep 1
done

if [ "$READY" != "1" ]; then
    err "连不上 127.0.0.1:$PORT"
    err "电脑端跑起来了吗？:  python dydump_android.py stream -p $PORT"
    exit 1
fi
say "通道就绪"

# ---------- 推流 ----------
say "开始传输（大库可能要几分钟）..."
cd /data/data || exit 1

# tar 里保留 <包名>/databases/... 结构，电脑端会自动压平
tar -cf - $TARGETS 2>/dev/null | nc 127.0.0.1 "$PORT"
RC=$?

if [ "$RC" = "0" ]; then
    say "传输完成，去电脑端看结果"
else
    err "传输返回码 $RC"
    exit 1
fi
