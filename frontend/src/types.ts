export interface Project {
  id: number;
  name: string;
  path: string;
  task_total: number;
  running: number;
  by_status: Record<string, number>;
  open_findings: number;
  manual_open: number;
  auto_open: number;
  fixed_findings: number;
  autopilot: {
    status: string;
    iteration: number;
    max_iterations: number;
    summary: string | null;
  } | null;
  last_analyze_at: number | null;
  audit_skipped: boolean;
  policy_count: number;
}

export type TaskStatus =
  | "draft"
  | "queued"
  | "running"
  | "waiting_input"
  | "paused"
  | "done"
  | "failed"
  | "cancelled"
  | "interrupted";

export type TaskWaitingKind = "permission" | "choice" | "input" | "review";

export interface Task {
  id: number;
  project_id: number;
  engine: "cc" | "codex";
  prompt: string;
  title: string | null;
  priority: number;
  status: TaskStatus;
  auto_approve: number;
  kind: string;
  log_path: string | null;
  exit_code: number | null;
  created_at: number;
  started_at: number | null;
  ended_at: number | null;
  session_uid: string | null;
  tokens: number | null;
  render_mode: "chat" | "terminal";
  elapsed_accum: number;
  original_prompt?: string | null;
  paused_from_status?: TaskStatus | null;
  // 仅 status=waiting_input 时后端附带：true=卡在工具授权(SDK 会话)
  pending_perm?: boolean;
  // 仅 status=waiting_input 时后端附带：review=本轮结束待核对，permission=授权，
  // choice=CLI 选项/确认，input=自由文本输入
  waiting_kind?: TaskWaitingKind | null;
  // 当前 waiting_input 轮次首次进入等待的 Unix 时间；用于通知补发和跨重连去重。
  waiting_since?: number | null;
  // 仅运行中(running/waiting_input)后端附带：true=终端里检测到会话被 /clear 冲掉，
  // 详情页此时才露出「恢复会话」；正常运行不显示，避免误点停掉执行器。
  session_cleared?: boolean;
  report_result?: "done" | "failed" | "needs_input" | null;
  report_summary?: string | null;
}

export interface ReplySuggestion {
  available: boolean;
  text: string;
  reason: string;
  source: "none" | "permission" | "confirm" | "clarify" | "tests" | "blocked" | "default" | "llm";
  confidence: number;
  updated_at: number;
}

export interface SessionHistoryMessage {
  role: "user" | "assistant";
  text: string;
  timestamp: string | null;
}

export interface LocalSession {
  engine: "cc" | "codex";
  session_uid: string;
  title: string;
  prompt: string;
  path: string;
  cwd: string | null;
  created_at: number | null;
  updated_at: number;
  size: number;
  turns: number;
  task_id: number | null;
  task_status: string | null;
}

export interface IssueSource {
  id: number;
  project_id: number;
  name: string;
  type: string;
  enabled: boolean;
  config: Record<string, any>; // 含 has_credential 标记; auth_credential 读取时为空
  last_pull_at: number | null;
  created_at: number;
}

export interface Finding {
  id: number;
  project_id: number;
  source: string;
  severity: string;
  title: string;
  detail: string | null;
  status: string;
  task_id: number | null;
  created_at: number;
}
