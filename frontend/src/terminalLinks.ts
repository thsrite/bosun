import type { IBufferLine, IDisposable, ILink, Terminal } from "@xterm/xterm";

const MAX_HARD_WRAPPED_LINK_LINES = 12;
const TUI_HARD_WRAP_RIGHT_MARGIN = 8;
const WEB_LINK_START = /https?:\/\/[^\s"'!*(){}|\\^<>`]*/gi;
const WEB_LINK_CONTINUATION = /^[^\s"'!*(){}|\\^<>`]*/;
const TRAILING_URL_PUNCTUATION = /[:,.!?~\[\]()]$/;

export interface TerminalLinkLine {
  text: string;
  full: boolean;
  isWrapped: boolean;
}

export interface TerminalLinkMatch {
  text: string;
  startLine: number;
  startIndex: number;
  endLine: number;
  endIndex: number;
}

/** TUIs commonly wrap inside a panel a few cells before xterm's physical last column. */
export function reachesTerminalWrapBoundary(lineEndColumn: number, terminalColumns: number): boolean {
  return lineEndColumn >= Math.max(1, terminalColumns - TUI_HARD_WRAP_RIGHT_MARGIN);
}

/** Find URLs split by a TUI's real newline. WebLinksAddon already handles xterm soft wraps. */
export function findHardWrappedWebLinks(lines: TerminalLinkLine[]): TerminalLinkMatch[] {
  const links: TerminalLinkMatch[] = [];

  for (let startLine = 0; startLine < lines.length; startLine += 1) {
    const firstLine = lines[startLine];
    if (!firstLine) continue;
    const regex = new RegExp(WEB_LINK_START.source, WEB_LINK_START.flags);
    let match: RegExpExecArray | null;

    while ((match = regex.exec(firstLine.text))) {
      let text = match[0];
      let endLine = startLine;
      let endIndex = match.index + text.length;
      let sawHardWrap = false;
      let currentLine = firstLine;

      while (
        endIndex === currentLine.text.length &&
        currentLine.full &&
        endLine + 1 < lines.length &&
        endLine - startLine + 1 < MAX_HARD_WRAPPED_LINK_LINES
      ) {
        const nextLine = lines[endLine + 1];
        if (!nextLine) break;
        const continuation = WEB_LINK_CONTINUATION.exec(nextLine.text)?.[0] ?? "";
        if (!continuation) break;
        if (!nextLine.isWrapped) sawHardWrap = true;
        text += continuation;
        endLine += 1;
        endIndex = continuation.length;
        currentLine = nextLine;
      }

      if (!sawHardWrap) continue;
      while (text && TRAILING_URL_PUNCTUATION.test(text)) {
        text = text.slice(0, -1);
        endIndex -= 1;
      }
      try {
        const url = new URL(text);
        if (url.protocol !== "http:" && url.protocol !== "https:") continue;
      } catch {
        continue;
      }
      links.push({
        text,
        startLine,
        startIndex: match.index,
        endLine,
        endIndex,
      });
    }
  }

  return links;
}

function lineEndColumn(line: IBufferLine): number {
  for (let column = line.length - 1; column >= 0; column -= 1) {
    const cell = line.getCell(column);
    if (cell && cell.getWidth() > 0 && (cell.getChars() || cell.getCode() !== 0)) {
      return column + cell.getWidth();
    }
  }
  return 0;
}

function stringIndexToColumn(line: IBufferLine, stringIndex: number): number {
  let offset = 0;
  for (let column = 0; column < line.length; column += 1) {
    const cell = line.getCell(column);
    if (!cell || cell.getWidth() === 0) continue;
    const chars = cell.getChars() || " ";
    if (offset >= stringIndex) return column;
    offset += chars.length;
    if (offset >= stringIndex) return column + cell.getWidth();
  }
  return line.length;
}

export function installHardWrappedWebLinkProvider(
  term: Terminal,
  activate: (event: MouseEvent, uri: string) => void,
): IDisposable {
  return term.registerLinkProvider({
    provideLinks(bufferLineNumber, callback) {
      const requestedLine = bufferLineNumber - 1;
      const firstLine = Math.max(0, requestedLine - MAX_HARD_WRAPPED_LINK_LINES + 1);
      const lastLine = Math.min(
        term.buffer.active.length - 1,
        requestedLine + MAX_HARD_WRAPPED_LINK_LINES - 1,
      );
      const bufferLines: IBufferLine[] = [];
      const lines: TerminalLinkLine[] = [];
      for (let lineIndex = firstLine; lineIndex <= lastLine; lineIndex += 1) {
        const line = term.buffer.active.getLine(lineIndex);
        if (!line) break;
        bufferLines.push(line);
        lines.push({
          text: line.translateToString(true),
          full: reachesTerminalWrapBoundary(lineEndColumn(line), term.cols),
          isWrapped: line.isWrapped,
        });
      }

      const links: ILink[] = findHardWrappedWebLinks(lines)
        .filter(
          (link) =>
            firstLine + link.startLine <= requestedLine &&
            firstLine + link.endLine >= requestedLine,
        )
        .map((link) => ({
          text: link.text,
          range: {
            start: {
              x: stringIndexToColumn(bufferLines[link.startLine]!, link.startIndex) + 1,
              y: firstLine + link.startLine + 1,
            },
            end: {
              x: stringIndexToColumn(bufferLines[link.endLine]!, link.endIndex),
              y: firstLine + link.endLine + 1,
            },
          },
          activate,
        }));
      callback(links.length ? links : undefined);
    },
  });
}
