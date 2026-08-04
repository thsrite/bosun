import type { Task } from "./types";

/** 列表/标题要显示的任务描述：任务继续会话后 prompt 只剩追加指令(可能为空)，
 *  原始提示词冻结在 original_prompt，展示一律以它优先。 */
export function taskPromptText(task: Pick<Task, "prompt" | "original_prompt">): string {
  return (task.original_prompt || task.prompt || "").trim();
}
