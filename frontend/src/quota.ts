/** 兼容现有调用入口：订阅用量仅供展示，不再确认或拦截执行。 */
export async function guardQuota(_engine?: string): Promise<boolean> {
  return true;
}
