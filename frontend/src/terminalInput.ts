export const TERMINAL_SUBMIT_KEY = "\r";

export function buildTerminalSubmitSequence(text: string, attachments: string[]): string[] {
  const parts = [...attachments, text.trim()].filter(Boolean);
  if (parts.length === 0) return [];
  return [parts.join(" "), TERMINAL_SUBMIT_KEY];
}
