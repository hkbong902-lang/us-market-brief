<#
.SYNOPSIS
  GitHub Actions 워크플로를 workflow_dispatch로 즉시 실행시킨다.

.DESCRIPTION
  호스팅 크론(on.schedule)은 실행 시각을 보장하지 않는다. 실측에서 117~128분씩
  밀렸고, 그 지연은 성공으로 기록되므로 어디에도 신호가 남지 않는다. 도착 마감이
  있는 브리핑에는 쓸 수 없다는 뜻이다.

  workflow_dispatch는 대기열을 타지 않고 몇 초 안에 시작하므로, 정확한 시각은
  이 PC의 작업 스케줄러가 맡고 GitHub은 실행만 담당하게 한다.

  토큰은 파일에 평문으로 두지 않는다. Windows DPAPI로 암호화해 저장하며,
  같은 사용자 계정이 같은 PC에서만 복호화할 수 있다.

.NOTES
  최초 1회 설정은 setup-dispatch.ps1 을 실행할 것.
#>
[CmdletBinding()]
param(
    [string]$Repo = "hkbong902-lang/us-market-brief",
    [string]$Workflow = "daily_brief.yml",
    [string]$Ref = "main",
    [string]$TokenFile = (Join-Path $env:USERPROFILE ".us-market-brief\gh-token.txt"),
    [string]$LogFile = (Join-Path $env:USERPROFILE ".us-market-brief\dispatch.log"),
    # 기동 직후 네트워크가 아직 안 붙었을 수 있어 몇 번 다시 시도한다.
    [int]$MaxAttempts = 5,
    [int]$RetryDelaySeconds = 60
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Write-Log {
    param([string]$Message)
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Output $line
    $dir = Split-Path $LogFile -Parent
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
    Add-Content -Path $LogFile -Value $line -Encoding utf8
    # 로그가 무한정 자라지 않게 최근 500줄만 남긴다.
    $all = Get-Content $LogFile -ErrorAction SilentlyContinue
    if ($all -and $all.Count -gt 500) {
        Set-Content -Path $LogFile -Value ($all | Select-Object -Last 500) -Encoding utf8
    }
}

if (-not (Test-Path $TokenFile)) {
    Write-Log "실패: 토큰 파일이 없습니다 ($TokenFile). setup-dispatch.ps1 을 먼저 실행하세요."
    exit 1
}

try {
    # ★ .Trim() 필수 (2026-09-11 실측)
    #   setup-dispatch.ps1 은 Set-Content 로 저장하므로 파일 끝에 항상 개행이 붙는다.
    #   -Raw 는 그 개행까지 읽어오고, ConvertTo-SecureString 은 16진 문자열만 받으므로
    #   "Input string was not in a correct format" 으로 죽는다. DPAPI 나 LogonType 과는
    #   무관한, 순전히 읽기 방식의 문제다. 되돌리지 말 것.
    $secure = (Get-Content $TokenFile -Raw).Trim() | ConvertTo-SecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    $token = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
} catch {
    # 원인을 단정하지 않는다. 예전에는 "다른 사용자/PC에서 만든 파일"이라고 단정하고
    # setup-dispatch.ps1 재실행을 처방했는데, 실제 원인이 파일 형식 문제였을 때는
    # 재실행해도 같은 파일이 다시 만들어져 고쳐지지 않는다. 예외 메시지를 그대로 남긴다.
    Write-Log "실패: 토큰 복호화 불가 — $($_.Exception.Message)"
    Write-Log "  → 다른 사용자/PC에서 만든 파일이면 setup-dispatch.ps1 을 재실행하세요."
    Write-Log "  → 'not in a correct format' 이면 파일 내용/형식 문제이며 재실행으로 고쳐지지 않습니다."
    exit 1
}

$uri = "https://api.github.com/repos/$Repo/actions/workflows/$Workflow/dispatches"
$headers = @{
    Authorization          = "Bearer $token"
    Accept                 = "application/vnd.github+json"
    "User-Agent"           = "us-market-brief-dispatcher"
    "X-GitHub-Api-Version" = "2022-11-28"
}
$body = @{ ref = $Ref } | ConvertTo-Json -Compress

for ($i = 1; $i -le $MaxAttempts; $i++) {
    try {
        Invoke-RestMethod -Uri $uri -Method Post -Headers $headers -Body $body `
            -ContentType "application/json" -TimeoutSec 60 | Out-Null
        Write-Log "성공: $Workflow dispatch 요청 완료 (시도 $i/$MaxAttempts)"
        exit 0
    } catch {
        $msg = $_.Exception.Message
        $code = $null
        if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
        # 401/403/404 는 재시도해도 달라지지 않는다 — 토큰 권한이나 경로 문제다.
        if ($code -eq 401 -or $code -eq 403 -or $code -eq 404) {
            Write-Log "실패(재시도 안 함): HTTP $code - $msg"
            Write-Log "  → 토큰 권한(Actions: Read and write)과 저장소 경로를 확인하세요."
            exit 1
        }
        Write-Log "실패(시도 $i/$MaxAttempts): $msg"
        if ($i -lt $MaxAttempts) { Start-Sleep -Seconds $RetryDelaySeconds }
    }
}

Write-Log "최종 실패: $MaxAttempts 회 시도 후에도 dispatch 하지 못했습니다."
exit 1
