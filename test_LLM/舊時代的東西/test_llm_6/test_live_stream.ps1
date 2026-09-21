param(
    [string]$JsonlPath = (Join-Path $PSScriptRoot "live_001.jsonl"),
    [string[]]$AccountIds = @("viewer_001"),
    [string]$ReplyApiBase = "http://127.0.0.1:5002",
    [string]$DataApiBase = "http://127.0.0.1:5001",
    [switch]$FromStart
)

$ErrorActionPreference = "Stop"
$script:Passed = 0
$script:Warnings = 0
$script:Failed = 0

function Write-Result {
    param([string]$Status, [string]$Name, [string]$Detail = "")
    $color = switch ($Status) {
        "PASS" { "Green" }
        "WARN" { "Yellow" }
        default { "Red" }
    }
    Write-Host ("[{0}] {1} {2}" -f $Status, $Name, $Detail) -ForegroundColor $color
    if ($Status -eq "PASS") { $script:Passed++ }
    elseif ($Status -eq "WARN") { $script:Warnings++ }
    else { $script:Failed++ }
}

function Invoke-JsonPost {
    param([string]$Uri, [hashtable]$Payload)
    $json = $Payload | ConvertTo-Json -Depth 20
    Invoke-RestMethod -Method Post -Uri $Uri -ContentType "application/json; charset=utf-8" `
        -Body ([Text.Encoding]::UTF8.GetBytes($json))
}

Write-Host "STT JSONL -> Live Stream Auto Reply integration test" -ForegroundColor Cyan
Write-Host "JSONL     : $JsonlPath"
Write-Host "Reply API : $ReplyApiBase"
Write-Host "Data API  : $DataApiBase"
Write-Host "From start: $($FromStart.IsPresent)"

if (-not (Test-Path -LiteralPath $JsonlPath -PathType Leaf)) {
    Write-Result FAIL "STT JSONL exists" "File not found: $JsonlPath"
    exit 1
}

$resolvedJsonlPath = (Resolve-Path -LiteralPath $JsonlPath).Path
try {
    $firstLine = Get-Content -LiteralPath $resolvedJsonlPath -Encoding UTF8 -TotalCount 1
    $firstObject = $firstLine | ConvertFrom-Json
    if ($firstObject.raw_text -or $firstObject.resolved_text) {
        Write-Result PASS "STT JSONL format" "raw_text/resolved_text found"
    } else {
        Write-Result FAIL "STT JSONL format" "First record has no raw_text or resolved_text"
    }
} catch {
    Write-Result FAIL "STT JSONL format" $_.Exception.Message
}

try {
    $dataHealth = Invoke-RestMethod "$DataApiBase/health"
    if ($dataHealth.status -in @("ok", "success")) { Write-Result PASS "Data API health" }
    else { Write-Result FAIL "Data API health" "status=$($dataHealth.status)" }
} catch {
    Write-Result FAIL "Data API health" $_.Exception.Message
}

$replyHealth = $null
try {
    $replyHealth = Invoke-RestMethod "$ReplyApiBase/health"
    if ($replyHealth.status -eq "ok") {
        Write-Result PASS "Reply API health"
        if ([int]$replyHealth.max_reply_chars -eq 7) { Write-Result PASS "Generated length setting" "7" }
        else { Write-Result FAIL "Generated length setting" "actual=$($replyHealth.max_reply_chars)" }
        if ([double]$replyHealth.generation_cooldown_seconds -eq 60) {
            Write-Result PASS "Generation cooldown setting" "60 seconds"
        } else {
            Write-Result FAIL "Generation cooldown setting" "actual=$($replyHealth.generation_cooldown_seconds)"
        }
    } else { Write-Result FAIL "Reply API health" "status=$($replyHealth.status)" }
} catch {
    Write-Result FAIL "Reply API health" $_.Exception.Message
}

try {
    $reload = Invoke-RestMethod -Method Post "$ReplyApiBase/reload_replies"
    if ($reload.status -eq "success") {
        Write-Result PASS "Reload reply cache" "cached=$($reload.cached_reply_count)"
    } else { Write-Result FAIL "Reload reply cache" }
} catch {
    Write-Result FAIL "Reload reply cache" $_.Exception.Message
}

try {
    $query = if ($FromStart.IsPresent) { "?from_start=true" } else { "" }
    $processResponse = Invoke-JsonPost "$ReplyApiBase/process$query" @{
        file_path = $resolvedJsonlPath
        account_ids = $AccountIds
    }

    if ($processResponse.status -in @("success", "partial_success")) {
        Write-Result PASS "Direct STT JSONL processing" `
            "processed=$($processResponse.processed_count), errors=$($processResponse.error_count)"
    } else {
        Write-Result FAIL "Direct STT JSONL processing" "status=$($processResponse.status)"
    }

    $scan = $processResponse.scanned_files | Select-Object -First 1
    if ($scan.file -eq $resolvedJsonlPath) {
        Write-Result PASS "JSONL source path" $scan.file
    } else {
        Write-Result FAIL "JSONL source path" "API returned: $($scan.file)"
    }

    if ([int]$processResponse.processed_count -eq 0) {
        Write-Result WARN "New STT records" "No new lines. Append STT output or run with -FromStart."
    } else {
        $policyCount = @($processResponse.results | Where-Object { $null -ne $_.policy }).Count
        if ($policyCount -eq [int]$processResponse.processed_count) {
            Write-Result PASS "Policy applied to STT records" "count=$policyCount"
        } else {
            Write-Result FAIL "Policy applied to STT records" `
                "policy=$policyCount, processed=$($processResponse.processed_count)"
        }

        $generated = @($processResponse.results | ForEach-Object { $_.account_results } |
            Where-Object { $_.selected.source -eq "qwen_generated" })
        $tooLong = @($generated | Where-Object { ([string]$_.reply).Length -gt 7 })
        if ($tooLong.Count -eq 0) {
            Write-Result PASS "Generated STT replies <= 7 chars" "generated=$($generated.Count)"
        } else {
            Write-Result FAIL "Generated STT replies <= 7 chars" "too_long=$($tooLong.Count)"
        }

        $cooldownRows = @($processResponse.results | Where-Object {
            [double]$_.generation_cooldown_remaining -gt 0 -and $null -eq $_.generation
        })
        if ($generated.Count -gt 0 -and $cooldownRows.Count -gt 0) {
            Write-Result PASS "60-second per-stream generation cooldown" "blocked=$($cooldownRows.Count)"
        } elseif ($generated.Count -eq 0) {
            Write-Result WARN "60-second per-stream generation cooldown" `
                "No successful Qwen generation in this batch"
        } else {
            Write-Result WARN "60-second per-stream generation cooldown" `
                "No later eligible record occurred during the cooldown window"
        }

        $latest = $processResponse.results | Select-Object -Last 1
        Write-Host ""
        Write-Host "Latest processed STT result:" -ForegroundColor Cyan
        $latest | ConvertTo-Json -Depth 20
    }
} catch {
    Write-Result FAIL "Direct STT JSONL processing" $_.Exception.Message
}

Write-Host ""
Write-Host ("Summary: PASS={0}, WARN={1}, FAIL={2}" -f $script:Passed, $script:Warnings, $script:Failed) `
    -ForegroundColor Cyan
if ($script:Failed -gt 0) { exit 1 }
exit 0
