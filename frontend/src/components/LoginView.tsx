import { useState, type FormEvent } from "react";
import { api } from "../api";
import { setToken } from "../auth";

/** 未登录时的全屏口令页。校验通过后拿到会话 token，交回 App 继续加载工作台。 */
export function LoginView({ onSignedIn }: { onSignedIn: () => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (submitting || !password) return;
    setSubmitting(true);
    setError("");
    try {
      const res = await api.auth.login(password);
      setToken(res.token);
      setPassword("");
      onSignedIn();
    } catch (err) {
      const raw = err instanceof Error ? err.message : String(err);
      let detail = raw;
      try {
        detail = JSON.parse(raw).detail || raw;
      } catch {
        /* keep raw message */
      }
      setError(detail || "登录失败");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="dh-app-shell flex h-full items-center justify-center px-4">
      <form onSubmit={submit} className="card w-full max-w-sm p-6">
        <div className="mb-5 flex items-center gap-2.5">
          <span className="grid h-9 w-9 shrink-0 place-items-center overflow-hidden rounded-lg border border-slate-700 bg-black">
            <img src="/icons/bosun.svg" alt="" className="h-full w-full" />
          </span>
          <div>
            <div className="text-sm font-semibold text-dh-text">Bosun</div>
            <div className="text-[11px] text-slate-400">请输入访问口令</div>
          </div>
        </div>

        <label className="block text-xs text-dh-muted" htmlFor="bosun-password">
          访问口令
        </label>
        <input
          id="bosun-password"
          type="password"
          autoFocus
          autoComplete="current-password"
          className="mt-1.5 w-full rounded-lg border border-dh-bsoft bg-dh-surface px-3 py-2 text-sm text-dh-text"
          value={password}
          onChange={(e) => {
            setPassword(e.target.value);
            if (error) setError("");
          }}
        />

        {error && <div className="mt-2.5 text-xs text-rose-500">{error}</div>}

        <button
          type="submit"
          disabled={submitting || !password}
          className="mt-4 w-full rounded-lg bg-black py-2 text-sm font-medium text-white ring-1 ring-slate-700 disabled:opacity-50"
        >
          {submitting ? "登录中…" : "登录"}
        </button>
      </form>
    </div>
  );
}
