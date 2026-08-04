import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./index.css";

/** 全局错误边界：任一渲染异常不再白屏，把错误显示到页面(手机也能看)。 */
class ErrorBoundary extends React.Component<
  { children: React.ReactNode },
  { error: Error | null; info: string }
> {
  state = { error: null as Error | null, info: "" };
  static getDerivedStateFromError(error: Error) {
    return { error, info: "" };
  }
  componentDidCatch(error: Error, info: React.ErrorInfo) {
    this.setState({ error, info: info.componentStack || "" });
  }
  render() {
    if (this.state.error) {
      return (
        <div style={{ padding: 20, fontFamily: "ui-monospace, Menlo, monospace", color: "#b91c1c" }}>
          <h2 style={{ marginBottom: 8 }}>页面渲染出错</h2>
          <pre style={{ whiteSpace: "pre-wrap", fontSize: 13, color: "#7f1d1d" }}>
            {String(this.state.error?.stack || this.state.error)}
          </pre>
          <pre style={{ whiteSpace: "pre-wrap", fontSize: 12, color: "#9a3412", marginTop: 12 }}>
            {this.state.info}
          </pre>
          <button
            style={{ marginTop: 12, padding: "6px 12px", borderRadius: 8, background: "#000", color: "#e5edf7", border: "1px solid #334155" }}
            onClick={() => location.reload()}
          >
            刷新重试
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </React.StrictMode>
);
