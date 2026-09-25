import type { IBufferLine, Terminal } from "@xterm/xterm";
import { reachesTerminalWrapBoundary } from "./terminalLinks.ts";

interface SelectionLine {
  text: string;
  fullText: string;
  endColumn: number;
  wrapped: boolean;
}

const LIST_ITEM = /^( *)(?:[-*+•] |\d+[.)] )/u;
const STRUCTURAL = /^\s*(?:[-*+•] |\d+[.)] |#{1,6} |[>|│┃┌└├╭╰`~]|\$ |❯|›)/u;
const CJK = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]/u;

function lineEndColumn(line: IBufferLine): number {
  for (let col = line.length - 1; col >= 0; col -= 1) {
    const cell = line.getCell(col);
    if (cell && cell.getWidth() > 0 && cell.getChars().trim()) return col + cell.getWidth();
  }
  return 0;
}

function prose(text: string): boolean {
  // Exclude code, commands and tables before considering prose continuation.
  return !/[{};=|`]|(?:^|\s)(?:const|let|var|return|import|export|def|class|if|for|while)\b/u.test(text)
    && (CJK.test(text) || /[A-Za-z]{2,} [A-Za-z]{2,}/u.test(text));
}

function joiner(left: string, right: string): string {
  // CJK layout may split inside a word. Latin prose needs its inter-word space.
  if (CJK.test(left.slice(-1)) || CJK.test(right[0] ?? "")) return "";
  return /[-/\s]$/u.test(left) ? "" : " ";
}

/** Preserve physical selection bounds; only remove identifiable presentation wraps. */
export function getTerminalSelectionText(term: Terminal): string {
  const range = term.getSelectionPosition();
  const original = term.getSelection();
  if (!range || !original) return original;
  const lines: SelectionLine[] = [];
  const buffer = term.buffer.active;
  let standard = "";
  for (let row = range.start.y; row <= range.end.y; row += 1) {
    const line = buffer.getLine(row);
    if (!line) break;
    let start = row === range.start.y ? range.start.x : 0;
    let end = row === range.end.y ? range.end.x : term.cols;
    // A touch endpoint can land on either half of a double-width character.
    const raw = line.translateToString(true, start, end).replace(/\u00a0/g, " ");
    standard += (row > range.start.y && !line.isWrapped ? "\n" : "") + raw;
    if (start > 0 && line.getCell(start)?.getWidth() === 0) start -= 1;
    if (end > 0 && end < term.cols && line.getCell(end)?.getWidth() === 0) end += 1;
    lines.push({
      text: line.translateToString(true, start, end).replace(/\u00a0/g, " "),
      fullText: line.translateToString(true),
      endColumn: lineEndColumn(line),
      wrapped: line.isWrapped,
    });
  }
  // xterm supports rectangular selections too. Never turn a rectangle into whole rows.
  if (original.replace(/\r\n/g, "\n") !== standard) return original;
  return formatTerminalSelectionLines(lines, term.cols);
}

export function formatTerminalSelectionLines(lines: SelectionLine[], columns: number): string {
  let result = "";
  let continuationIndent: number | null = null;
  let fenced = false;
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    const previous = lines[i - 1];
    const full = line.fullText;
    const marker = LIST_ITEM.exec(full);
    const indent = full.length - full.trimStart().length;
    const fence = /^\s*(?:```|~~~)/u.test(full);
    if (fence) fenced = !fenced;
    const hardWrap = previous && !line.wrapped && !fenced && !fence
      && continuationIndent !== null && indent === continuationIndent
      && full.trim() && previous.text.trim()
      && !STRUCTURAL.test(full) && prose(full.trim())
      && reachesTerminalWrapBoundary(previous.endColumn, columns);
    if (i === 0 || line.wrapped) result += line.text;
    else if (hardWrap) {
      const next = line.text.trimStart();
      result += joiner(result, next) + next;
    } else result += "\n" + line.text;
    if (!hardWrap && !line.wrapped) {
      continuationIndent = !fenced && prose(full)
        ? marker ? marker[0].length : indent <= 2 && !STRUCTURAL.test(full) ? indent : null
        : null;
    }
  }
  return result;
}

/** Own plain desktop drags in mouse-tracking TUIs; clicks and modified gestures stay with the TUI. */
export function installTerminalDragCopy(term: Terminal, copy: () => void): () => void {
  const host = term.element;
  if (!host) return () => {};
  let down: MouseEvent | null = null;
  let anchor: { x: number; y: number } | null = null;
  let dragging = false;
  let replaying = false;
  const cellAt = (event: MouseEvent) => {
    const screen = host.querySelector(".xterm-screen");
    if (!screen) return null;
    const rect = screen.getBoundingClientRect();
    return {
      x: Math.max(0, Math.min(term.cols - 1, Math.floor((event.clientX - rect.left) / rect.width * term.cols))),
      y: term.buffer.active.viewportY + Math.max(0, Math.min(term.rows - 1, Math.floor((event.clientY - rect.top) / rect.height * term.rows))),
    };
  };
  const onDown = (event: MouseEvent) => {
    if (replaying || event.button !== 0 || event.shiftKey || event.altKey || event.ctrlKey || event.metaKey
        || term.modes.mouseTrackingMode === "none"
        || window.matchMedia("(pointer: coarse)").matches) return;
    anchor = cellAt(event);
    if (!anchor) return;
    down = event;
    dragging = false;
    event.preventDefault();
    event.stopImmediatePropagation();
    term.focus();
    term.clearSelection();
  };
  const onMove = (event: MouseEvent) => {
    if (!down || !anchor) return;
    event.stopImmediatePropagation();
    if (!dragging && Math.hypot(event.clientX - down.clientX, event.clientY - down.clientY) < 4) return;
    dragging = true;
    event.preventDefault();
    const end = cellAt(event);
    if (!end) return;
    const a = anchor.y * term.cols + anchor.x;
    const b = end.y * term.cols + end.x;
    const start = Math.min(a, b);
    term.select(start % term.cols, Math.floor(start / term.cols), Math.abs(b - a) + 1);
  };
  const onUp = (event: MouseEvent) => {
    if (!down || event.button !== 0) return;
    if (dragging) onMove(event);
    event.stopImmediatePropagation();
    const start = down;
    down = null;
    anchor = null;
    if (dragging) copy();
    else {
      // Delay only the mouse protocol, not DOM click/link activation.
      replaying = true;
      try {
        start.target?.dispatchEvent(new MouseEvent("mousedown", start));
        start.target?.dispatchEvent(new MouseEvent("mouseup", event));
      } finally { replaying = false; }
    }
    dragging = false;
  };
  const cancel = () => { down = null; anchor = null; dragging = false; };
  host.addEventListener("mousedown", onDown, true);
  document.addEventListener("mousemove", onMove, true);
  document.addEventListener("mouseup", onUp, true);
  window.addEventListener("blur", cancel);
  return () => {
    host.removeEventListener("mousedown", onDown, true);
    document.removeEventListener("mousemove", onMove, true);
    document.removeEventListener("mouseup", onUp, true);
    window.removeEventListener("blur", cancel);
  };
}
