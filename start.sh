#!/usr/bin/env sh
# Android 抖音私信记录提取 / 解密 / 导出 —— Linux / macOS 启动器
# 用法:  sh start.sh          （不带参数进交互菜单）
#        sh start.sh doctor   （直接跑某个步骤，参数原样透传）
set -u

cd "$(dirname "$0")" || exit 1

echo
echo "  ============================================================"
echo "   Android 抖音私信记录提取 / 解密 / 导出"
echo "  ============================================================"
echo

# ---------- 1. 找 Python ----------
PY=""
for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then
        if "$c" -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >/dev/null 2>&1; then
            PY="$c"
            break
        fi
    fi
done
if [ -z "$PY" ]; then
    echo "  [!] 没找到 Python 3.8+"
    echo "      macOS:  brew install python"
    echo "      Debian/Ubuntu:  sudo apt install python3 python3-pip python3-venv"
    echo "      Fedora:  sudo dnf install python3 python3-pip"
    exit 1
fi
echo "  [*] Python $("$PY" -c 'import sys;print(sys.version.split()[0])')  ($(command -v "$PY"))"

# ---------- 2. 检查依赖 ----------
if ! "$PY" -c "import Crypto" >/dev/null 2>&1; then
    echo "  [!] 缺少 Python 依赖（pycryptodome），需要联网装一次。"
    printf "      现在安装吗？[Y/n] "
    read -r ans || ans=""
    case "$ans" in
        n|N|no|NO) ;;
        *)
            if ! "$PY" -m pip install -r requirements.txt; then
                echo
                echo "  [!] 直连失败，换清华源再试一次 ..."
                "$PY" -m pip install -r requirements.txt \
                    -i https://pypi.tuna.tsinghua.edu.cn/simple
            fi
            if ! "$PY" -c "import Crypto" >/dev/null 2>&1; then
                echo
                echo "  [!] 依赖还是没装上。手动跑一下看具体报错："
                echo "      $PY -m pip install -r requirements.txt"
                echo "      系统 Python 不允许直接装的话，用虚拟环境："
                echo "      $PY -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt"
                exit 1
            fi
            echo "  [*] 依赖装好了"
            ;;
    esac
fi

# ---------- 3. 检查 adb（只是提醒，没装也能用本地数据） ----------
if ! command -v adb >/dev/null 2>&1 && [ ! -x "./platform-tools/adb" ]; then
    echo "  [!] 没找到 adb（platform-tools）"
    echo "      只处理本地已有数据可以忽略；要从手机拉数据就装一下："
    echo "        · 在下面的菜单里选 7) 下载 adb"
    echo "        · macOS:  brew install --cask android-platform-tools"
    echo "        · 或手动下载 https://developer.android.com/tools/releases/platform-tools"
fi

# ---------- 4. 进菜单 ----------
echo
"$PY" dydump_android.py "$@"
rc=$?
echo
[ "$rc" -ne 0 ] && echo "  [!] 退出码 $rc"
exit "$rc"
