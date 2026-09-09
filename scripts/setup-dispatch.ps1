<#
.SYNOPSIS
  최초 1회 설정 — GitHub 토큰을 암호화 저장하고, 작업 스케줄러에 등록한다.

.DESCRIPTION
  실행 전에 GitHub에서 fine-grained personal access token을 만들어 두세요.
    https://github.com/settings/personal-access-tokens
      Repository access : hkbong902-lang/us-market-brief 만 선택
      Permissions       : Actions = Read and write  (이것만 있으면 됩니다)
      Expiration        : 만료일을 적어두세요. 만료되면 브리핑이 조용히 멈춥니다.

  요일이 화~토인 이유: 미국장은 월~금에 열리고, 마감 후 밤에 브리핑이 만들어져
  한국시간으로는 다음 날 아침에 도착한다. 금요일 미국장 → 토요일 아침.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\scripts\setup-dispatch.ps1
#>
[CmdletBinding()]
param(
    [string]$TaskName = "US Market Brief Dispatch",
    # 07:20 KST — 겨울(EST) 미국장 마감 06:00 KST 기준 80분 뒤. 실행 5분을 더해도
    # 07:25 도착으로, 08:00 마감까지 35분 여유가 남는다.
    [string]$At = "07:20",
    [switch]$SkipToken
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$dispatch = Join-Path $scriptDir "dispatch-brief.ps1"
if (-not (Test-Path $dispatch)) { throw "dispatch-brief.ps1 을 찾을 수 없습니다: $dispatch" }

$tokenFile = Join-Path $env:USERPROFILE ".us-market-brief\gh-token.txt"

# ── 1) 토큰 저장 ─────────────────────────────────────────────────────────────
if (-not $SkipToken) {
    Write-Host ""
    Write-Host "GitHub fine-grained token 을 붙여넣으세요 (화면에 표시되지 않습니다)."
    Write-Host "  필요 권한: Actions = Read and write / 대상: hkbong902-lang/us-market-brief"
    $secure = Read-Host -AsSecureString "Token"
    if ($secure.Length -eq 0) { throw "토큰이 비어 있습니다." }

    $dir = Split-Path $tokenFile -Parent
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
    # ConvertFrom-SecureString 은 DPAPI로 현재 사용자에 묶어 암호화한다.
    # 다른 계정이나 다른 PC에서는 복호화되지 않는다.
    $secure | ConvertFrom-SecureString | Set-Content -Path $tokenFile -Encoding utf8
    Write-Host "토큰을 암호화해 저장했습니다: $tokenFile" -ForegroundColor Green
}

# ── 2) 작업 스케줄러 등록 ────────────────────────────────────────────────────
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument ("-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"{0}`"" -f $dispatch)

$trigger = New-ScheduledTaskTrigger -Weekly -At $At `
    -DaysOfWeek Tuesday, Wednesday, Thursday, Friday, Saturday

$settings = New-ScheduledTaskSettingsSet `
    -WakeToRun `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -MultipleInstances IgnoreNew
# WakeToRun        : 절전 상태면 깨워서 실행 (완전히 꺼져 있으면 불가)
# StartWhenAvailable: 그 시각에 PC가 꺼져 있었다면 켜진 직후 따라잡아 실행

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "기존 작업을 제거하고 다시 등록합니다." -ForegroundColor Yellow
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "미국 시장 브리핑 워크플로를 매일 아침 즉시 실행시킨다 (화~토)" | Out-Null

Write-Host ""
Write-Host "등록 완료: '$TaskName'  화~토 $At" -ForegroundColor Green
Get-ScheduledTask -TaskName $TaskName |
    Get-ScheduledTaskInfo |
    Select-Object TaskName, NextRunTime, LastRunTime, LastTaskResult |
    Format-List

Write-Host "지금 바로 시험 실행하려면:" -ForegroundColor Cyan
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "실행 기록:"
Write-Host "  Get-Content `"$env:USERPROFILE\.us-market-brief\dispatch.log`" -Tail 20"
