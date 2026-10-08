#!/usr/bin/env bash
# 给 Playwright 自带的 Chromium 补上发行版的运行库，不往系统里装任何东西。
#
# 精简的 Linux 上 `<chromium> --version` 会直接报 libasound.so.2 找不到，整套离线
# 浏览器回归于是停在启动阶段，一个断言都跑不到。这里把发行版的 libasound2 解到
# `.tmp/playwright-libs/`（`dpkg-deb -x`，不需要 root，也不改系统配置），
# `tests/browser.ts` 会在这个目录存在时把它接上测试进程的 LD_LIBRARY_PATH。
#
# 用法（仓库根目录或任意位置都可以）：
#   bash life-circle-demo/scripts/ensure-browser-libs.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LIBS="$ROOT/.tmp/playwright-libs"
DEBS="$LIBS/debs"
ROOTFS="$LIBS/root"

if find "$ROOTFS" -name 'libasound.so.2' -print -quit 2>/dev/null | grep -q .; then
  echo "browser libs already extracted: $ROOTFS"
  exit 0
fi

command -v dpkg-deb >/dev/null 2>&1 || {
  echo "dpkg-deb not found: this helper only serves Debian/Ubuntu hosts." >&2
  echo "Provide the shared libraries yourself under $ROOTFS, then rerun the suite." >&2
  exit 1
}
command -v apt-get >/dev/null 2>&1 || {
  echo "apt-get not found: cannot fetch libasound2 automatically." >&2
  exit 1
}

mkdir -p "$DEBS" "$ROOTFS"
# 只下载，不安装：装到系统里需要 root，也不该由测试脚本决定。
(cd "$DEBS" && apt-get download libasound2t64 libasound2-data)
for deb in "$DEBS"/*.deb; do
  dpkg-deb -x "$deb" "$ROOTFS"
done

find "$ROOTFS" -name 'libasound.so.2' -print -quit | grep -q . || {
  echo "extracted but libasound.so.2 is still missing under $ROOTFS" >&2
  exit 1
}
echo "browser libs extracted to $ROOTFS"
