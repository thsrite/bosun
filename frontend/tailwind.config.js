/** @type {import('tailwindcss').Config} */
export default {
  // hover: 只在真悬停设备生效（v4 默认行为）。iOS 会在触摸手势收尾时给落点元素
  // 合成 hover 态：滚动终端的手指停在底部按键栏上方时，Esc 等键的 hover 深色态
  // 被套上又清掉，表现为按键闪烁。触屏本就不该有 hover 反馈，整体关闭。
  future: { hoverOnlyWhenSupported: true },
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: [
          "-apple-system",
          "BlinkMacSystemFont",
          '"SF Pro Text"',
          '"Segoe UI"',
          "system-ui",
          '"PingFang SC"',
          "sans-serif",
        ],
        mono: ['"SF Mono"', "ui-monospace", "Menlo", "monospace"],
      },
    },
  },
  plugins: [],
};
