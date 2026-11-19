"""由当前持棒角色在 Bosun 所在机器调用，测试进程只在指定 SSH 测试机运行。"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile

from app import acceptance, db

# 标记写入独立的最后一行。SSH 掉线、版本漂移和环境问题均不能产生通过回执。
REMOTE = r"""
set -eu
cd "$1"
expected=$2
check_runner=$3
shift 3
if [ "$check_runner" = yes ] && pgrep -f '[R]unner.Worker' >/dev/null; then
    echo '构建机正在执行任务，请稍后再测'
    exit 2
fi
verify() {
    test "$(git rev-parse HEAD)" = "$expected" && git diff --quiet HEAD --
}
verify || { echo '远端候选提交或工作树与提交不符'; exit 2; }
extras=$(git ls-files --others --exclude-standard -- :/ )
test -z "$extras" || { echo '请在本轮干净的测试副本执行，将测试输入放在仓库外或提交到候选版本'; exit 2; }
code=0
"$@" </dev/null || code=$?
verify || { echo '测试期间代码版本发生变化'; exit 2; }
printf '\nBOSUN_TEST_EXIT=%s\n' "$code"
"""


def run(task_id, host, directory, version, items, command):
    step = db.query_one(
        "SELECT s.*,r.status run_status,r.current_position,r.definition_snapshot,p.path "
        "FROM orchestration_step_run s JOIN orchestration_run r ON r.id=s.run_id "
        "JOIN project p ON p.id=r.project_id WHERE s.task_id=?", (task_id,),
    )
    if (not step or step["position"] not in (2, 3) or step["current_position"] != step["position"]
            or step["run_status"] not in ("running", "waiting_input")
            or not json.loads(step["definition_snapshot"]).get("remote_checks")):
        raise ValueError("仅当前持棒的开发或质量角色可运行此检查")
    source = db.query_one("SELECT output_artifact FROM orchestration_step_run WHERE run_id=? AND position=1",
                          (step["run_id"],))
    spec = acceptance.record(source[0])
    if not items or not set(items) <= acceptance.plan(spec).keys():
        raise ValueError("请指定任务单中实际覆盖的验收项")
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", version):
        raise ValueError("需要候选提交的完整 SHA")
    subprocess.run(["git", "cat-file", "-e", version + "^{commit}"], cwd=step["path"], check=True)
    if not command or not directory.startswith("/") or not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.@:-]*", host):
        raise ValueError("需要测试机、远程绝对目录和实际测试命令")
    with db._lock, db.get_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO delivery_check(step_id,attempt,version,items,host,command,state,detail) "
            "SELECT s.id,s.attempt,?,?,?,?,'running','' FROM orchestration_step_run s "
            "JOIN orchestration_run r ON r.id=s.run_id WHERE s.id=? AND s.attempt=? "
            "AND s.position=r.current_position AND r.status IN ('running','waiting_input')",
            (version, json.dumps(items), host, shlex.join(command), step["id"], step["attempt"]),
        )
        if not cursor.rowcount:
            raise ValueError("本次交接或返工已发生，请使用当前角色和轮次")
        receipt = cursor.lastrowid
    # 本机只接收文本。限制保存的日志长度，避免把大输出留在编排数据库。
    state, detail = "error", "执行中断"
    try:
        with tempfile.TemporaryFile() as log:
            check_runner = "yes" if host.rsplit("@", 1)[-1] == "10.0.0.50" else "no"
            args = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "--", host,
                    shlex.join(["sh", "-s", "--", directory, version, check_runner, *command])]
            proc = subprocess.run(args, input=REMOTE.encode(), stdout=log, stderr=subprocess.STDOUT)
            log.seek(max(0, log.tell() - 2000))
            output = log.read().decode(errors="replace").strip()
            lines = output.splitlines()
            marker = re.fullmatch(r"BOSUN_TEST_EXIT=(\d+)", lines[-1]) if lines else None
            if proc.returncode == 0 and marker:
                code = int(marker.group(1))
                state = "passed" if code == 0 else "error" if code in (126, 127) else "failed"
                detail = f"退出码 {code}\n" + "\n".join(lines[:-1])
            else:
                detail = f"远程环境未完成验证（{proc.returncode}）\n{output}"
    finally:
        db.execute("UPDATE delivery_check SET state=?,detail=? WHERE id=?", (state, detail, receipt))
    print(f"回执 #{receipt}\n{detail}")
    return 0 if state == "passed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--dir", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--item", action="append", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    try:
        sys.exit(run(int(os.environ["BOSUN_TASK_ID"]), args.host, args.dir, args.version, args.item, command))
    except (KeyError, ValueError, OSError, subprocess.SubprocessError) as exc:
        sys.exit(str(exc))
