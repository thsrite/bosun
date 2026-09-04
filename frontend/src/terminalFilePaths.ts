/** 从终端某一行的某一列取出被双击到的文件路径。
 *
 * 终端里的路径是命令行/模型打印出来的任意字符串，这里只负责「取出用户点的那一段」，
 * 是否允许读取由后端按任务工作目录判定（见 backend/app/task_files.py）。
 */

/** 路径 token 的边界：空白、引号、以及 TUI 常用的竖线框线。 */
const BOUNDARY = /[\s"'`|│┃║]/;
/** 成对包裹符：点在里面时整段取出（这样带空格的路径也能取全）。 */
const QUOTES = ['"', "'"];
const LEADING_TRIM = /^[([{<]+/;
const TRAILING_TRIM = /[)\]}>,;.:!?、，。；：]+$/;
/** 编译器/栈回溯常见的 path:line 或 path:line:col 后缀。 */
const LINE_COL_SUFFIX = /:\d+(?::\d+)?$/;

function quotedSpanAt(line: string, column: number): string | null {
  for (const quote of QUOTES) {
    let from = line.indexOf(quote);
    while (from !== -1) {
      const to = line.indexOf(quote, from + 1);
      if (to === -1) break;
      if (column > from && column < to) return line.slice(from + 1, to);
      from = line.indexOf(quote, to + 1);
    }
  }
  return null;
}

function tidy(raw: string): string {
  let text = raw.replace(LEADING_TRIM, "").replace(TRAILING_TRIM, "");
  text = text.replace(LINE_COL_SUFFIX, "");
  return text.replace(TRAILING_TRIM, "");
}

function looksLikePath(text: string): boolean {
  if (!text) return false;
  // http(s):// 交给已有的链接处理，别当本地文件读
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(text)) return false;
  return text.includes("/") || /\.[A-Za-z0-9]+$/.test(text);
}

/** 返回该列上的文件路径；点在空白处、或那段文本不像路径时返回 null。 */
export function extractPathAt(line: string, column: number): string | null {
  if (column < 0 || column >= line.length) return null;
  const char = line[column];
  if (char === undefined || BOUNDARY.test(char)) return null;

  const quoted = quotedSpanAt(line, column);
  if (quoted !== null) {
    const text = quoted.trim();
    return looksLikePath(text) ? text : null;
  }

  let start = column;
  while (start > 0 && !BOUNDARY.test(line[start - 1] as string)) start -= 1;
  let end = column;
  while (end + 1 < line.length && !BOUNDARY.test(line[end + 1] as string)) end += 1;

  const text = tidy(line.slice(start, end + 1));
  return looksLikePath(text) ? text : null;
}
