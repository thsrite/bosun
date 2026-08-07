# Bosun 任务状态回调（Windows / PowerShell 版，与 report.sh 同一契约）。
# 用法: powershell -File report.ps1 -Status <done|failed|needs_input> -Summary "<一句话>" [-NeedsReply]
param(
    [string]$Status = "done",
    [string]$Summary = "",
    [switch]$NeedsReply
)

# 守卫：不是 Bosun 派发的会话 → 静默 no-op。
if (-not $env:BOSUN_TASK_ID -or -not $env:BOSUN_API) { exit 0 }

# 先把同一份汇报留在 agent 终端，再回调 Bosun。
Write-Output "Bosun 汇报 [$Status]: $Summary"

$payload = @{
    result       = $Status
    summary      = $Summary
    needs_reply  = [bool]$NeedsReply
    reporter_pid = $PID
} | ConvertTo-Json -Compress

$headers = @{}
if ($env:BOSUN_TASK_TOKEN) { $headers["Authorization"] = "Bearer $($env:BOSUN_TASK_TOKEN)" }

try {
    Invoke-RestMethod -Method Post -TimeoutSec 10 `
        -Uri "$($env:BOSUN_API)/api/tasks/$($env:BOSUN_TASK_ID)/report" `
        -ContentType "application/json; charset=utf-8" `
        -Headers $headers -Body ([System.Text.Encoding]::UTF8.GetBytes($payload)) | Out-Null
    Write-Output "回报已送达。请紧接着把本轮完整结论正文作为你最后一条消息打印出来再停下（不得再调工具）；summary 只是回执，用户只看正文。"
} catch {
    Write-Error "Bosun 回报失败($($_.Exception.Message))：状态没同步到工作台，请直接告知用户。"
    exit 1
}
