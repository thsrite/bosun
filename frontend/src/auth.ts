/** 访问口令的会话 token：存 localStorage，注入到所有 API 请求与 WebSocket。 */
const TOKEN_KEY = "bosun.auth-token.v1";

let token: string | null = null;
try {
  token = localStorage.getItem(TOKEN_KEY);
} catch {
  /* 隐私模式下 localStorage 可能不可用，退化为内存态 */
}

const listeners = new Set<() => void>();

export function getToken(): string | null {
  return token;
}

export function setToken(next: string | null): void {
  token = next && next.length > 0 ? next : null;
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* 同上 */
  }
  listeners.forEach((fn) => fn());
}

/** 会话失效（401 / WS 4401）时调用：清 token 并通知 App 弹回登录页。 */
export function onUnauthorized(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function authHeaders(base?: HeadersInit): HeadersInit | undefined {
  if (!token) return base;
  return { ...(base as Record<string, string> | undefined), authorization: `Bearer ${token}` };
}

const WS_AUTH_SUBPROTOCOL = "bosun.auth";

/** WebSocket 鉴权子协议。
 *
 * 浏览器的 WebSocket 无法设置自定义请求头，只能用 query 参数或子协议传 token。
 * query 会被服务端 access log 连同 URL 记进日志文件，所以这里走子协议——
 * 它只存在于握手头里，不落日志。服务端会回选 bosun.auth 完成握手。
 */
export function wsProtocols(): string[] | undefined {
  return token ? [WS_AUTH_SUBPROTOCOL, token] : undefined;
}

export const WS_UNAUTHORIZED = 4401;
