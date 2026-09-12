<#
.SYNOPSIS
  최초 1회 설정 — GitHub 토큰을 암호화 저장하고, 브리핑 작업들을 작업 스케줄러에 등록한다.

.DESCRIPTION
  실행 전에 GitHub에서 fine-grained personal access token을 만들어 두세요.
    https://github.com/settings/personal-access-tokens
      Repository access : 'Only select repositories' → hkbong902-lang/us-market-brief
                          ★ 이것을 먼저 해야 한다. 기본값 'Public Repositories (read-only)' 에서는
                            Permissions 섹션이 비활성이라 아래를 설정할 수 없다. 그리고 이 저장소는
                            공개라서 읽기는 통과하므로 토큰이 정상처럼 보인다(dispatch 만 403).
      Permissions       : Actions = Read and write  (Metadata: Read-only 는 자동 부여)
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
    [switch]$Interactive,
    # 토큰 검증에 쓴다.
    [string]$Repo = "hkbong902-lang/us-market-brief"
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$dispatch = Join-Path $scriptDir "dispatch-brief.ps1"
if (-not (Test-Path $dispatch)) { throw "dispatch-brief.ps1 을 찾을 수 없습니다: $dispatch" }

$tokenFile = Join-Path $env:USERPROFILE ".us-market-brief\gh-token.txt"

# ── 1) 토큰 저장 (두 작업이 같은 토큰을 쓴다) ────────────────────────────────
if (-not $SkipToken) {
    # ★ 안내문과 입력 프롬프트를 분리한다 (2026-09-11 사고).
    #   안내를 프롬프트에 붙여 두면 안내문 자체가 값으로 붙여넣어진다. 실제로 발생했고,
    #   입력이 화면에 표시되지 않아 그 자리에서는 드러나지 않은 채 저장됐다.
    Write-Host ""
    Write-Host "GitHub fine-grained token 이 필요합니다." -ForegroundColor Cyan
    Write-Host "  발급 : https://github.com/settings/personal-access-tokens"
    Write-Host ""
    Write-Host "  ★ 순서가 중요합니다 (2026-09-12 사고). 1) 을 먼저 하지 않으면 2) 가 불가능합니다."
    Write-Host "  1) Repository access : 'Only select repositories' 선택 후 $Repo 추가"
    Write-Host "     기본값인 'Public Repositories (read-only)' 로 두면 Permissions 섹션이"
    Write-Host "     비활성이라 Actions 권한을 줄 수 없습니다. 공개 저장소라 읽기는 통과하므로"
    Write-Host "     토큰이 멀쩡해 보이지만 dispatch(POST)는 403 으로 죽습니다."
    Write-Host "  2) Permissions > Repository permissions > Actions = Read and write"
    Write-Host "     (Metadata: Read-only 가 자동으로 붙습니다 - 정상입니다)"
    Write-Host ""
    # ★ 붙여넣는 자리 근처에 '값처럼 생긴 문자열'을 두지 않는다 (2026-09-11 결정).
    #   안내와 프롬프트를 분리하는 것만으로는 부족하다 — 형태를 알려주려고 적은 예시일수록
    #   진짜 값과 닮아서, 그 예시 자체가 붙여넣기 후보가 된다. 프롬프트 라벨에 토큰
    #   접두사를 쓰면 라벨이 곧 미끼다. 형태는 값이 아니라 서술로만 주고, 접두사 같은
    #   구체적 형태 정보는 실패 메시지 쪽에 둔다(아래 검증 블록). 그러면 틀렸을 때만
    #   보이고, 맞힐 때는 미끼가 되지 않는다.
    Write-Host "아래에는 토큰 값만 붙여넣으십시오 — 위 안내문이 아닙니다." -ForegroundColor Yellow
    Write-Host "(공백 없는 한 줄. 화면에는 표시되지 않습니다.)" -ForegroundColor Yellow
    $secure = Read-Host -AsSecureString "토큰"
    if ($secure.Length -eq 0) { throw "토큰이 비어 있습니다." }

    # ★ 저장 전에 검증한다 (2026-09-11 사고).
    #   검증하지 않으면 잘못된 값이 조용히 저장되고, 다음 정시 실행에서야 실패가
    #   드러난다. 그 사이 브리핑 하루가 통째로 날아간다. 형식 검사만으로는 부족해
    #   실제 API 호출까지 한다 — 만료·권한 부족도 여기서 걸린다.
    $bstrT = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try   { $plainTok = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstrT) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstrT) }

    if ($plainTok -match "\s") {
        throw "토큰에 공백이나 줄바꿈이 들어 있습니다. 안내문을 붙여넣지 않았는지 확인하세요. 저장하지 않았습니다."
    }
    if ($plainTok -notmatch "^(github_pat_|ghp_|gho_|ghs_)") {
        throw "토큰 형식이 아닙니다(github_pat_ 또는 ghp_ 로 시작해야 합니다). 저장하지 않았습니다."
    }

    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $hdrT = @{
        Authorization          = "Bearer $plainTok"
        Accept                 = "application/vnd.github+json"
        "User-Agent"           = "us-market-brief-dispatcher"
        "X-GitHub-Api-Version" = "2022-11-28"
    }
    # ── (1) 읽기 확인 ──
    $wfList = $null
    try {
        $wfList = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/actions/workflows" `
            -Headers $hdrT -TimeoutSec 30
    } catch {
        $plainTok = $null
        throw ("토큰이 거부되었습니다(읽기): $($_.Exception.Message)`n" +
               "  401 이면 토큰 값이 잘못되었거나 만료된 것이고, 404 면 Repository access 가 $Repo 를 포함하지 않은 것입니다.`n" +
               "  저장하지 않았습니다. 토큰을 다시 확인해 이 스크립트를 재실행하세요.")
    }

    # ── (2) ★ 쓰기 확인 — 읽기만 검증하면 안 된다 (2026-09-12 사고) ──
    #   종전에는 (1) 만 하고 통과시켰다. 그런데 dispatch 는 POST .../dispatches 이고
    #   이것은 Actions: Read and write 를 요구한다. Read 만 있는 토큰이 (1) 을 통과해
    #   저장됐고, 다음 정시 실행에서 403 으로 죽었다 — 검증이 실제로 실패할 작업보다
    #   약한 작업을 테스트한 탓이다.
    #
    #   ★ 일부러 존재하지 않는 ref 로 POST 한다. 권한 검사가 ref 해석보다 먼저이므로
    #     권한이 있으면 422(ref 없음), 없으면 403 이 온다. 즉 워크플로를 실행시키지
    #     않고 쓰기 권한만 판정할 수 있다. 되돌리지 말 것.
    $probeWf = ($wfList.workflows | Select-Object -First 1)
    if (-not $probeWf) {
        $plainTok = $null
        throw "저장소 $Repo 에 워크플로가 없습니다. 경로를 확인하세요. 저장하지 않았습니다."
    }
    $probeUri = "https://api.github.com/repos/$Repo/actions/workflows/$($probeWf.id)/dispatches"
    $probeBody = '{"ref":"zz-permission-probe-nonexistent"}'
    $writeOk = $false
    try {
        $null = Invoke-RestMethod -Uri $probeUri -Method Post -Headers $hdrT `
            -Body $probeBody -ContentType 'application/json' -TimeoutSec 30
        # 204 가 오면 존재하지 않는 ref 로 실행이 시작된 것이다(이례적). 권한은 확실하다.
        $writeOk = $true
        Write-Host "주의: 권한 탐침이 실행을 시작했을 수 있습니다. Actions 탭을 확인하세요." -ForegroundColor Yellow
    } catch {
        $code = $null
        if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
        if ($code -eq 422) {
            $writeOk = $true          # 권한 OK, ref 만 없는 것 — 의도한 결과다
        } elseif ($code -eq 403) {
            $plainTok = $null
            throw ("토큰에 쓰기 권한이 없습니다(HTTP 403).`n" +
                   "  읽기는 통과했지만 dispatch(POST)가 거부됐습니다.`n" +
                   "  해당 토큰을 https://github.com/settings/personal-access-tokens 에서 열어,`n" +
                   "  아래를 이 순서로 확인하십시오:`n" +
                   "   (1) Repository access 가 'Public Repositories (read-only)' 인지 -- 그러면 Permissions`n" +
                   "       섹션이 비활성이라 Actions 항목 자체가 보이지 않습니다. 'Only select repositories'`n" +
                   "       로 바꾸고 $Repo 를 선택하십시오. 공개 저장소는 읽기가 통과하므로 이 상태에서도`n" +
                   "       토큰이 멀쩡해 보입니다 -- 403 의 가장 흔한 원인입니다.`n" +
                   "   (2) 그다음 Permissions > Repository permissions > Actions 를 'Read and write' 로.`n" +
                   "  ★ 권한만 바꾸면 토큰 값은 그대로이므로 새로 발급할 필요가 없습니다.`n" +
                   "  저장하지 않았습니다. 변경 후 이 스크립트를 재실행하세요.")
        } else {
            $plainTok = $null
            throw ("토큰 쓰기 권한을 확인할 수 없습니다(HTTP $code): $($_.Exception.Message)`n" +
                   "  저장하지 않았습니다.")
        }
    }
    if ($writeOk) {
        Write-Host "토큰 확인 완료: $Repo 의 Actions 에 읽기·쓰기 모두 가능합니다." -ForegroundColor Green
    }
    $plainTok = $null

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

    # ★ 먼저 제거하지 않는다 (2026-09-11). -Force 로 덮어쓴다.
    #   Unregister 후 Register 하면, Windows 암호가 틀려 Register 가 throw 할 때
    #   ($ErrorActionPreference='Stop' 이므로 즉시 중단) 이미 기존 작업이 사라진
    #   상태가 된다 - 재등록을 시도했다가 작동하던 작업까지 잃는다. -Force 는
    #   성공할 때만 교체하므로 실패 시 기존 등록이 그대로 남는다.
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Write-Host "  기존 '$TaskName' 을 덮어씁니다(실패하면 기존 등록이 유지됩니다)." -ForegroundColor Yellow
    }
    if ($winPassword) {
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
            -Settings $settings -Description $Description `
            -User $taskUser -Password $winPassword -Force | Out-Null
    } else {
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
            -Settings $settings -Description $Description -Force | Out-Null
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
