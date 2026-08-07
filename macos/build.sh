#!/usr/bin/env bash
# 构建 Bosun 状态栏程序（Bosun.app）。只依赖 Xcode Command Line Tools，无需完整 Xcode。
#
#   ./macos/build.sh              仅构建到 macos/build/Bosun.app（后端走本机源码 venv）
#   ./macos/build.sh --install    构建并安装到 /Applications，然后启动
#   ./macos/build.sh --bundle     分发模式：内嵌 PyInstaller 后端，产物免源码运行
#                                 （需先 npm run build 与 pip install pyinstaller）

set -euo pipefail

MACOS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$MACOS_DIR/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
BUILD_DIR="$MACOS_DIR/build"
APP_DIR="$BUILD_DIR/Bosun.app"
PYTHON_BIN="$BACKEND_DIR/.venv/bin/python"
PORT="${BOSUN_BACKEND_PORT:-8770}"
INSTALL=0
BUNDLE=0

fail() { printf '错误：%s\n' "$1" >&2; exit 1; }

for arg in "$@"; do
  case "$arg" in
    --install) INSTALL=1 ;;
    --bundle) BUNDLE=1 ;;   # 分发模式：把 PyInstaller 后端产物内嵌进 app，免源码运行
    *) fail "未知参数：$arg" ;;
  esac
done

command -v xcrun >/dev/null 2>&1 || fail "找不到 xcrun，请先安装 Xcode Command Line Tools：xcode-select --install"
xcrun -f swiftc >/dev/null 2>&1 || fail "找不到 swiftc。"
if [[ "$BUNDLE" -eq 1 ]]; then
  # 分发模式用系统/CI 的 python 跑 PyInstaller，不依赖本机 venv 路径
  PYTHON_BIN="${BOSUN_PYTHON:-$PYTHON_BIN}"
  [[ -x "$PYTHON_BIN" ]] || PYTHON_BIN="$(command -v python3)" || fail "找不到 python3"
  "$PYTHON_BIN" -m PyInstaller --version >/dev/null 2>&1 || fail "缺少 PyInstaller：$PYTHON_BIN -m pip install pyinstaller"
  [[ -f "$ROOT_DIR/frontend/dist/index.html" ]] || fail "缺少 frontend/dist，请先执行 cd frontend && npm run build"
else
  [[ -x "$PYTHON_BIN" ]] || fail "缺少 $PYTHON_BIN，请先安装后端依赖。"
fi

# launchd 不读 shell 配置，这里把后端真正需要的目录固化进 plist。
# ~/.local/bin 提供 claude，homebrew 提供 node/npm/git。
BREW_PREFIX="$( [[ -d /opt/homebrew/bin ]] && echo /opt/homebrew || echo /usr/local )"
BAKED_PATH="$HOME/.local/bin:$BREW_PREFIX/bin:$BREW_PREFIX/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

printf '构建配置：\n'
printf '  python : %s\n' "$PYTHON_BIN"
printf '  backend: %s\n' "$BACKEND_DIR"
printf '  port   : %s\n' "$PORT"
printf '  PATH   : %s\n\n' "$BAKED_PATH"

rm -rf "$BUILD_DIR"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"

# 应用图标以前端品牌图标 frontend/public/icons/bosun.svg 为唯一来源，构建时栅格化，
# 不额外提交二进制资源。qlmanage/iconutil/sips 都是系统自带。
make_icon() {
  local svg="$ROOT_DIR/frontend/public/icons/bosun.svg"
  [[ -f "$svg" ]] || { printf '提示：找不到 %s，跳过图标生成。\n' "$svg"; return 1; }

  local work="$BUILD_DIR/icon"
  local iconset="$work/AppIcon.iconset"
  mkdir -p "$iconset"
  qlmanage -t -s 1024 -o "$work" "$svg" >/dev/null 2>&1
  local raw="$work/$(basename "$svg").png"
  [[ -f "$raw" ]] || { printf '提示：SVG 栅格化失败，跳过图标生成。\n'; return 1; }

  # 裁掉 QuickLook 补出的白角，并按 macOS 约定内缩画面。
  local master="$work/master.png"
  xcrun swiftc -O "$MACOS_DIR/Tools/mkicon.swift" -o "$work/mkicon" 2>/dev/null \
    && "$work/mkicon" "$raw" "$master" 2>/dev/null \
    || { printf '提示：图标合成失败，跳过图标生成。\n'; return 1; }

  local size
  for size in 16 32 64 128 256 512 1024; do
    sips -z "$size" "$size" "$master" --out "$iconset/icon_${size}.png" >/dev/null 2>&1
  done
  # iconutil 要求严格的文件名约定：每个尺寸的 1x 与 2x 两份。
  mv "$iconset/icon_16.png"   "$iconset/icon_16x16.png"
  mv "$iconset/icon_32.png"   "$iconset/icon_16x16@2x.png"
  cp "$iconset/icon_16x16@2x.png" "$iconset/icon_32x32.png"
  mv "$iconset/icon_64.png"   "$iconset/icon_32x32@2x.png"
  mv "$iconset/icon_128.png"  "$iconset/icon_128x128.png"
  mv "$iconset/icon_256.png"  "$iconset/icon_128x128@2x.png"
  cp "$iconset/icon_128x128@2x.png" "$iconset/icon_256x256.png"
  mv "$iconset/icon_512.png"  "$iconset/icon_256x256@2x.png"
  cp "$iconset/icon_256x256@2x.png" "$iconset/icon_512x512.png"
  mv "$iconset/icon_1024.png" "$iconset/icon_512x512@2x.png"

  iconutil -c icns "$iconset" -o "$APP_DIR/Contents/Resources/AppIcon.icns" 2>/dev/null || {
    printf '提示：iconutil 生成 icns 失败，跳过图标。\n'; return 1
  }
  printf '  图标   : 由 %s 生成\n' "${svg#$ROOT_DIR/}"
}

make_icon || true

# 发现已安装的 Bosun PWA（Chrome 安装的 PWA 位于 ~/Applications/Chrome Apps.localized）。
# 找不到不算失败：运行时会按目录名再找一次，再不行才退回默认浏览器开地址。
# 分发模式不烘焙：构建机的 PWA id 对用户机器没有意义。
PWA_BUNDLE_ID=""
if [[ "$BUNDLE" -eq 0 ]]; then
  for candidate in "$HOME/Applications/Chrome Apps.localized/"*.app; do
    [[ -d "$candidate" ]] || continue
    case "$(basename "$candidate")" in
      *Bosun*|*bosun*)
        PWA_BUNDLE_ID="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$candidate/Contents/Info.plist" 2>/dev/null || true)"
        ;;
    esac
  done
  printf '  PWA    : %s\n' "${PWA_BUNDLE_ID:-未发现（将退回默认浏览器）}"
fi

# 分发模式：先把后端打成 PyInstaller onedir，再内嵌进 app 的 Resources/backend。
if [[ "$BUNDLE" -eq 1 ]]; then
  printf '  后端   : PyInstaller 打包中…\n'
  "$PYTHON_BIN" -m PyInstaller "$ROOT_DIR/packaging/bosun.spec" --noconfirm \
    --workpath "$BUILD_DIR/pyi-work" --distpath "$BUILD_DIR/pyi-dist" >/dev/null \
    || fail "PyInstaller 打包失败"
  cp -R "$BUILD_DIR/pyi-dist/bosun" "$APP_DIR/Contents/Resources/backend"
  printf '  后端   : 已内嵌 %s\n' "Contents/Resources/backend"
fi

APP_VERSION="$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' "$BACKEND_DIR/app/version.py")"
APP_VERSION="${APP_VERSION:-0.0.0}"

cat > "$BUILD_DIR/Config.swift" <<EOF
// 由 macos/build.sh 生成，请勿手改。
enum BosunConfig {
    static let python = "$PYTHON_BIN"
    static let backendDir = "$BACKEND_DIR"
    static let port = $PORT
    static let path = "$BAKED_PATH"
    static let pwaBundleID = "$PWA_BUNDLE_ID"
    static let executableFallback = "/Applications/Bosun.app/Contents/MacOS/Bosun"
    static let bundledBackend = $([[ "$BUNDLE" -eq 1 ]] && echo true || echo false)
}
EOF

# 显式钉最低部署版本：swiftc 默认按构建机系统版本，若在新系统上构建会拒绝老系统运行
xcrun swiftc -O -whole-module-optimization \
  -target "$(uname -m)-apple-macos13.0" \
  "$MACOS_DIR/Sources/main.swift" "$BUILD_DIR/Config.swift" \
  -o "$APP_DIR/Contents/MacOS/Bosun"

cat > "$APP_DIR/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Bosun</string>
  <key>CFBundleDisplayName</key><string>Bosun</string>
  <key>CFBundleExecutable</key><string>Bosun</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundleIdentifier</key><string>com.thsrite.bosun.menubar</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$APP_VERSION</string>
  <key>CFBundleVersion</key><string>$APP_VERSION</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <!-- 只驻留菜单栏：无 Dock 图标、无主窗口 -->
  <key>LSUIElement</key><true/>
</dict>
</plist>
EOF

# 本机自签（ad-hoc）。本地编译产物不带 quarantine 属性，不会触发 Gatekeeper 拦截。
codesign --force --sign - "$APP_DIR" >/dev/null 2>&1 || printf '提示：ad-hoc 签名失败，本机运行通常仍不受影响。\n'

printf '已构建：%s\n' "$APP_DIR"

if [[ "$INSTALL" -eq 1 ]]; then
  # 先停掉可能在跑的旧实例，避免替换 bundle 时新旧图标并存。
  pkill -x Bosun 2>/dev/null || true
  rm -rf /Applications/Bosun.app
  cp -R "$APP_DIR" /Applications/Bosun.app
  # 同路径替换 app 时 Launch Services 常沿用旧图标缓存，重新注册一次强制刷新。
  LSREGISTER=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
  [[ -x "$LSREGISTER" ]] && "$LSREGISTER" -f /Applications/Bosun.app >/dev/null 2>&1
  touch /Applications/Bosun.app
  open /Applications/Bosun.app
  printf '已安装到 /Applications/Bosun.app 并启动，请查看菜单栏右侧。\n'
else
  printf '安装请执行：%s --install\n' "$0"
fi
