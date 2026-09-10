<#
.SYNOPSIS
  최초 1회 설정 — GitHub 토큰을 암호화 저장하고, 브리핑 작업들을 작업 스케줄러에 등록한다.

.DESCRIPTION
  실행 전에 GitHub에서 fine-grained personal access token을 만들어 두세요.
    https://github.com/settings/personal-access-tokens
      Repository access : hkbong902-lang/us-market-brief 만 선택
      Permissions       : Actions = Read and write  (이것만 있으면 됩니다)
      Expiration        : 만료일을 적어두세요. 만료되면 브리핑이 조용히 멈춥니다.

  실행 중 두 가지를 입력받습니다:
    1) 위 GitHub 토큰
    2) 이 PC의 Windows 로그인 암호 — 작업을 LogonType=Password 로 등록해
       로그오프·잠금·로그인 화면 상태에서도 실행되게 하기 위함입니다.
       암호를 저장할 수 없는 계정이면 -Interactive 로 실행하세요
       (예전 동작: 로그온 상태에서만 실행).

  등록되는 작업 두 개:
    US Market Brief Dispatch  화~토 07:20  daily_brief.yml
    KR Market Brief Dispatch  월~금 17:35  kr_brief.yml

  요일이 다른 이유: 미국장은 월~금에 열리지만 마감 후 밤에 브리핑이 만들어져 한국시간으로는
  다음 날 아침에 도착한다(금요일 미국장 → 토요일 아침). 한국장은 당일 15:30에 마감하므로
  같은 날 저녁이다.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\scripts\setup-dispatch.ps1
  powershell -ExecutionPolicy Bypass -File .\scripts\setup-dispatch.ps1 -Register KR
#>
[CmdletBinding()]
param(
    [ValidateSet("All", "US", "KR")]
    [string]$Register = "All",
    # 07:20 KST — 겨울(EST) 미국장 마감 06:00 KST 기준 80분 뒤. 실행 5분을 더해도
    # 07:25 도착으로, 08:00 마감까지 35분 여유가 남는다.
    [string]$UsAt = "07:20",
    # 17:35 KST — KRX 정규장 마감(15:30) 후 2시간 5분.
    [string]$KrAt = "17:35",
    [switch]$SkipToken,
    # 예전 동작(로그온 상태에서만 실행)으로 등록한다. Windows 암호를 저장할 수 없는
    # 계정(PIN 전용 등)에서만 쓴다.
    [switch]$Interactive
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$dispatch = Join-Path $scriptDir "dispatch-brief.ps1"
if (-not (Test-Path $dispatch)) { throw "dispatch-brief.ps1 을 찾을 수 없습니다: $dispatch" }

$tokenFile = Join-Path $env:USERPROFILE ".us-market-brief\gh-token.txt"

# ── 1) 토큰 저장 (두 작업이 같은 토큰을 쓴다) ────────────────────────────────
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

# ── 1-b) Windows 계정 암호 ──────────────────────────────────────────────────
#   작업을 LogonType=Password 로 등록하기 위해 필요하다. 이게 없으면 작업은
#   Interactive 로 등록되고, PC 전원이 켜져 있어도 대화형 로그온 세션이 없으면
#   (로그오프, 업데이트 재부팅 후 로그인 화면 방치) 트리거가 조용히 건너뛴다.
#   실제로 같은 PC의 다른 스케줄 작업이 이 이유로 이틀 연속 누락된 적이 있다
#   (2026-09-09~10). ★ S4U(암호 없이 로그오프 실행)는 쓸 수 없다 — 위에서 저장한
#   토큰이 DPAPI 사용자 스코프라 S4U 토큰으로는 복호화되지 않는다. Password 여야 한다.
$taskUser = "$env:USERDOMAIN\$env:USERNAME"
$winPassword = $null
if (-not $Interactive) {
    Write-Host ""
    Write-Host "이 PC의 Windows 로그인 암호를 입력하세요 (화면에 표시되지 않습니다)."
    Write-Host "  계정: $taskUser"
    Write-Host "  작업 스케줄러에만 저장되며, 로그오프·잠금 상태에서도 브리핑이 나갑니다."
    Write-Host "  Microsoft 계정이면 PIN 이 아니라 계정 암호입니다."
    $secPw = Read-Host -AsSecureString "Windows 암호"
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secPw)
    try   { $winPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    if ([string]::IsNullOrWhiteSpace($winPassword)) {
        throw "암호가 비어 있습니다. 암호를 저장할 수 없는 계정이라면 -Interactive 로 다시 실행하세요(로그온 상태에서만 동작)."
    }
}

# ── 2) 작업 등록 ─────────────────────────────────────────────────────────────
function Register-BriefTask {
    param(
        [string]$TaskName,
        [string]$Workflow,
        [string]$At,
        [string[]]$Days,
        [string]$Description
    )

    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument (
        "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"{0}`" -Workflow {1}" `
            -f $dispatch, $Workflow)

    $trigger = New-ScheduledTaskTrigger -Weekly -At $At -DaysOfWeek $Days

    $settings = New-ScheduledTaskSettingsSet `
        -WakeToRun `
        -StartWhenAvailable `
        -DontStopOnIdleEnd `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
        -MultipleInstances IgnoreNew
    # WakeToRun                 : 절전 상태면 깨워서 실행 (완전히 꺼져 있으면 불가)
    # StartWhenAvailable        : 그 시각에 PC가 꺼져 있었다면 켜진 직후 따라잡아 실행
    # AllowStartIfOnBatteries   : 이 둘은 기본값이 '배터리면 실행 안 함'이다. UPS나 노트북에서
    # DontStopIfGoingOnBatteries: 전원이 배터리로 바뀌는 순간 작업이 조용히 건너뛰어진다 —
    #                             브리핑이 안 온 이유가 전원 상태라는 것은 아무 데도 안 남는다.

    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "  기존 '$TaskName' 제거 후 재등록합니다." -ForegroundColor Yellow
    }
    if ($winPassword) {
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
            -Settings $settings -Description $Description `
            -User $taskUser -Password $winPassword | Out-Null
    } else {
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
            -Settings $settings -Description $Description | Out-Null
    }

    # 등록 결과를 다시 읽어 확인한다. 인자를 넘겼다는 사실은 결과가 아니다.
    $lt = (Get-ScheduledTask -TaskName $TaskName).Principal.LogonType
    Write-Host ("  등록: {0,-26} {1,-16} {2}   LogonType={3}" -f `
        $TaskName, ($Days -join ","), $At, $lt) -ForegroundColor Green
    if ($winPassword -and "$lt" -ne "Password") {
        Write-Host "  경고: LogonType 이 Password 가 아닙니다 — 로그오프 상태에서 실행되지 않습니다." `
            -ForegroundColor Red
    }
}

Write-Host ""
if ($Register -in @("All", "US")) {
    Register-BriefTask -TaskName "US Market Brief Dispatch" -Workflow "daily_brief.yml" `
        -At $UsAt -Days Tuesday, Wednesday, Thursday, Friday, Saturday `
        -Description "미국 시장 브리핑 워크플로를 아침에 즉시 실행시킨다 (화~토)"
}
if ($Register -in @("All", "KR")) {
    Register-BriefTask -TaskName "KR Market Brief Dispatch" -Workflow "kr_brief.yml" `
        -At $KrAt -Days Monday, Tuesday, Wednesday, Thursday, Friday `
        -Description "한국 시장 브리핑 워크플로를 장 마감 후 즉시 실행시킨다 (월~금)"
}

Write-Host ""
Get-ScheduledTask | Where-Object { $_.TaskName -like "*Brief Dispatch" } |
    Get-ScheduledTaskInfo |
    Select-Object TaskName, NextRunTime, LastRunTime, LastTaskResult |
    Format-Table -AutoSize

if ($winPassword) {
    Write-Host "※ LogonType=Password 로 등록했습니다. 로그오프·잠금·로그인 화면 상태에서도" -ForegroundColor Yellow
    Write-Host "   실행됩니다. 다만 PC를 완전히 끄면(빠른 시작이 켜져 있으면 '시스템 종료'가" -ForegroundColor Yellow
    Write-Host "   여기 해당) 웨이크 타이머가 동작하지 않아 실행되지 않습니다." -ForegroundColor Yellow
    Write-Host "   Windows 로그인 암호를 바꾸면 저장된 암호가 낡아 작업이 실패합니다 —" -ForegroundColor Yellow
    Write-Host "   그때는 이 스크립트를 다시 실행하세요." -ForegroundColor Yellow
} else {
    Write-Host "※ -Interactive 로 등록했습니다. 이 작업들은 '로그온 상태'에서만 실행됩니다." -ForegroundColor Yellow
    Write-Host "   화면이 잠긴 것은 괜찮지만, 로그아웃하거나 PC를 끄면 실행되지 않습니다." -ForegroundColor Yellow
}
Write-Host ""
Write-Host "지금 바로 시험 실행 — 등록만으로는 작동을 확인할 수 없습니다:" -ForegroundColor Cyan
Write-Host "  Start-ScheduledTask -TaskName 'US Market Brief Dispatch'"
Write-Host "  Start-ScheduledTask -TaskName 'KR Market Brief Dispatch'"
Write-Host "  Get-Content `"$env:USERPROFILE\.us-market-brief\dispatch.log`" -Tail 20"
Write-Host "로그에 '성공:' 이 찍히고 텔레그램이 와야 등록이 끝난 것입니다." -ForegroundColor Cyan
