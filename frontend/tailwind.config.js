/** @type {import('tailwindcss').Config} */
export default {
  // hover: 只在真悬停设备生效（v4 默认行为）。iOS 会在触摸手势收尾时给落点元素
  // 合成 hover 态：滚动终端的手指停在底部按键栏上方时，Esc 等键的 hover 深色态
  // 被套上又清掉，表现为按键闪烁。触屏本就不该有 hover 反馈，整体关闭。
  future: { hoverOnlyWhenSupported: true },
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      // Bosun 暗色设计 token：与 index.css 的 --dh-* 变量一一对应，
      // 组件一律用 bg-dh-surface / text-dh-muted 等语义类，不再写浅色类靠覆盖层翻转。
      colors: {
        // RGB 三元组 + <alpha-value>：让 bg-dh-surface/80 这类透明度修饰符可用（毛玻璃底色）
        dh: {
          bg: "rgb(var(--dh-bg-rgb) / <alpha-value>)",
          soft: "rgb(var(--dh-bg-soft-rgb) / <alpha-value>)",
          surface: "rgb(var(--dh-surface-rgb) / <alpha-value>)",
          s2: "rgb(var(--dh-surface-2-rgb) / <alpha-value>)",
          hover: "rgb(var(--dh-surface-hover-rgb) / <alpha-value>)",
          border: "rgb(var(--dh-border-rgb) / <alpha-value>)",
          bsoft: "rgb(var(--dh-border-soft-rgb) / <alpha-value>)",
          text: "rgb(var(--dh-text-rgb) / <alpha-value>)",
          tsoft: "rgb(var(--dh-text-soft-rgb) / <alpha-value>)",
          muted: "rgb(var(--dh-muted-rgb) / <alpha-value>)",
          m2: "rgb(var(--dh-muted-2-rgb) / <alpha-value>)",
          accent: "var(--dh-accent)",
        },
      },
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
