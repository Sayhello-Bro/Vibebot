param(
    [string]$JsonlPath = (Join-Path $PSScriptRoot "live_001.jsonl"),
    [string]$ReplyApiBase = "http://127.0.0.1:5002",
    [string]$DataApiBase = "http://127.0.0.1:5001",
    [string[]]$AccountIds = @("account_001", "account_002", "account_003"),
    [ValidateSet("qwen_only", "hybrid", "vector_only")]
    [string]$AccountMode = "qwen_only",
    [ValidateNotNullOrEmpty()]
    [string]$ProductType = "服飾",
    [string]$StreamId = "",
    [int]$MaxRecords = 3,
    [string]$TestResultRoot = (Join-Path $PSScriptRoot "test_results"),
    [switch]$KeepLiveDatabase,
    [switch]$KeepCandidateChanges
)

$ErrorActionPreference = "Stop"
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Text.UTF8Encoding]::new($false)
. (Join-Path $PSScriptRoot "test_report_helpers.ps1")

$DesiredCandidates = @(
    "有沒有優惠",
    "+1",
    "我來了",
    "這個好",
    "我喜歡",
    "上車囉",
    "有別色嗎",
    "想看細節",
    "尺寸怎麼選",
    "好好看"
)
$DesiredCandidateCount = $DesiredCandidates.Count

$createdIds = [System.Collections.Generic.List[string]]::new()
$defaultSnapshot = @()
$userSnapshot = @()
$processedCount = 0
$replyCount = 0
$failedCount = 0
$wholeTest = [Diagnostics.Stopwatch]::StartNew()
$jsonlStem = [IO.Path]::GetFileNameWithoutExtension($JsonlPath)
if ([string]::IsNullOrWhiteSpace($StreamId)) {
    $sessionStamp = Get-Date -Format "yyyyMMdd_HHmmss_fff"
    $sessionSuffix = [Guid]::NewGuid().ToString("N").Substring(0, 6)
    $StreamId = "${jsonlStem}_${sessionStamp}_${sessionSuffix}"
}
$streamId = $StreamId
$testReport = New-LiveTestReport -Root $TestResultRoot -StreamId $streamId

function Invoke-JsonRequest {
    param(
        [Parameter(Mandatory)] [string]$Method,
        [Parameter(Mandatory)] [string]$Uri,
        [hashtable]$Body
    )

    $parameters = @{
        Method = $Method
        Uri = $Uri
        ContentType = "application/json; charset=utf-8"
    }
    if ($null -ne $Body) {
        $json = $Body | ConvertTo-Json -Depth 30
        $parameters.Body = [Text.Encoding]::UTF8.GetBytes($json)
    }
    Invoke-RestMethod @parameters
}

function Set-DefaultCandidateEnabled {
    param([string]$Text, [bool]$Enabled)

    Invoke-JsonRequest -Method Patch `
        -Uri "$DataApiBase/default_replies/by_text" `
        -Body @{ text = $Text; enabled = $Enabled } | Out-Null
}

function Remove-TestLiveDatabase {
    if ([string]::IsNullOrWhiteSpace($streamId)) {
        return
    }
    try {
        $deleted = Invoke-JsonRequest -Method Delete `
            -Uri "$ReplyApiBase/stream_database" `
            -Body @{ stream_id = $streamId }
        Write-Host "已刪除直播資料庫：$($deleted.database_name)" -ForegroundColor Yellow
    }
    catch {
        Write-Warning "無法刪除直播資料庫（stream_id=$streamId）：$($_.Exception.Message)"
    }
}

function Set-UserCandidateEnabled {
    param([string]$Id, [bool]$Enabled)

    Invoke-JsonRequest -Method Patch `
        -Uri "$DataApiBase/user_input/$Id" `
        -Body @{ enabled = $Enabled } | Out-Null
}

function Initialize-TestCandidates {
    Write-Host "讀取目前候選狀態..." -ForegroundColor Cyan
    $script:defaultSnapshot = @(
        (Invoke-RestMethod "$DataApiBase/default_replies?limit=500").items
    )
    $script:userSnapshot = @(
        (Invoke-RestMethod "$DataApiBase/user_input?limit=500").items
    )

    foreach ($item in $script:defaultSnapshot) {
        $shouldEnable = $DesiredCandidates -contains [string]$item.text
        if ([bool]$item.enabled -ne $shouldEnable) {
            Set-DefaultCandidateEnabled -Text ([string]$item.text) `
                -Enabled $shouldEnable
        }
    }

    $defaultTexts = @(
        $script:defaultSnapshot | ForEach-Object { [string]$_.text }
    )
    foreach ($item in $script:userSnapshot) {
        $shouldEnable = (
            $DesiredCandidates -contains [string]$item.text -and
            $defaultTexts -notcontains [string]$item.text
        )
        if ([bool]$item.enabled -ne $shouldEnable) {
            Set-UserCandidateEnabled -Id ([string]$item.id) `
                -Enabled $shouldEnable
        }
    }

    $knownTexts = @(
        $script:defaultSnapshot | ForEach-Object { [string]$_.text }
        $script:userSnapshot | ForEach-Object { [string]$_.text }
    )

    foreach ($text in $DesiredCandidates) {
        if ($knownTexts -contains $text) {
            continue
        }
        $created = Invoke-JsonRequest -Method Post `
            -Uri "$DataApiBase/user_input" `
            -Body @{
                text = $text
                weight = 1.0
                enabled = $true
                multi_output = ($text -eq "+1")
            }
        $script:createdIds.Add([string]$created.item.id)
    }

    $cache = Invoke-RestMethod "$ReplyApiBase/candidate_cache"
    $enabledTexts = @($cache.items | ForEach-Object { [string]$_.text })
    $unexpected = @($enabledTexts | Where-Object { $DesiredCandidates -notcontains $_ })
    $missing = @($DesiredCandidates | Where-Object { $enabledTexts -notcontains $_ })
    $duplicate = @(
        $enabledTexts | Group-Object | Where-Object { $_.Count -gt 1 }
    )

    if (
        $unexpected.Count -gt 0 -or
        $missing.Count -gt 0 -or
        $duplicate.Count -gt 0 -or
        $enabledTexts.Count -ne $DesiredCandidateCount
    ) {
        throw (
            "候選同步失敗。缺少：{0}；多出：{1}；重複：{2}；總數：{3}" -f `
            ($missing -join "、"),
            ($unexpected -join "、"),
            (($duplicate | ForEach-Object { $_.Name }) -join "、"),
            $enabledTexts.Count
        )
    }

    Write-Host "目前啟用候選句（revision=$($cache.candidate_revision)）：" `
        -ForegroundColor Green
    foreach ($text in $DesiredCandidates) {
        Write-Host "  - $text"
    }
}

function Restore-Candidates {
    if ($KeepCandidateChanges) {
        Write-Host "保留測試候選變更。" -ForegroundColor Yellow
        return
    }

    Write-Host "恢復測試前候選狀態..." -ForegroundColor Cyan
    foreach ($item in $script:defaultSnapshot) {
        try {
            Set-DefaultCandidateEnabled -Text ([string]$item.text) `
                -Enabled ([bool]$item.enabled)
        }
        catch {
            Write-Warning "無法恢復預設候選 '$($item.text)'：$($_.Exception.Message)"
        }
    }

    foreach ($item in $script:userSnapshot) {
        try {
            Set-UserCandidateEnabled -Id ([string]$item.id) `
                -Enabled ([bool]$item.enabled)
        }
        catch {
            Write-Warning "無法恢復自訂候選 '$($item.text)'：$($_.Exception.Message)"
        }
    }

    foreach ($id in $script:createdIds) {
        try {
            Invoke-RestMethod -Method Delete "$DataApiBase/user_input/$id" |
                Out-Null
        }
        catch {
            Write-Warning "無法刪除測試建立的候選 id=$id：$($_.Exception.Message)"
        }
    }

    try {
        Invoke-RestMethod -Method Post "$ReplyApiBase/reload_replies" | Out-Null
    }
    catch {
        Write-Warning "候選已恢復，但直播快取手動刷新失敗：$($_.Exception.Message)"
    }
}

try {
    if ($AccountIds.Count -ne 3) {
        throw "本測試必須剛好提供三個 AccountIds。"
    }
    if (-not (Test-Path -LiteralPath $JsonlPath -PathType Leaf)) {
        throw "找不到 JSONL：$JsonlPath"
    }

    Write-Host "檢查服務..." -ForegroundColor Cyan
    $dataHealth = Invoke-RestMethod "$DataApiBase/health"
    $replyHealth = Invoke-RestMethod "$ReplyApiBase/health"
    Write-Host "資料 API：$($dataHealth.status)"
    Write-Host (
        "直播 API：{0}，Qwen={1}，model={2}" -f `
        $replyHealth.status, $replyHealth.qwen_enabled, $replyHealth.chat_model
    )
    Write-Host (
        "CLS記憶：enabled={0}，ready={1}，model={2}" -f `
        [bool]$replyHealth.cls_memory.enabled,
        [bool]$replyHealth.cls_memory.ready,
        [string]$replyHealth.cls_memory.model
    )
    if ($null -eq $replyHealth.cls_memory) {
        throw "直播 API 尚未提供 CLS 狀態，請先重啟更新後的 live_stream_llm.py。"
    }
    if ([bool]$replyHealth.cls_memory.enabled -and -not [bool]$replyHealth.cls_memory.ready) {
        throw "CLS 已啟用但初始化失敗：$($replyHealth.cls_memory.error)"
    }

    if (-not [bool]$replyHealth.qwen_enabled) {
        throw "ENABLE_QWEN=false，qwen_only 測試無法執行。請設為 true 後重啟直播 API。"
    }
    if (@($replyHealth.account_modes) -notcontains $AccountMode) {
        throw "直播 API 尚未支援帳號模式。請先更新並重啟 live_stream_llm.py。"
    }
    if ($AccountMode -ne "vector_only" -and [double]$replyHealth.generation_cooldown_seconds -gt 0) {
        throw "GENERATION_COOLDOWN_SECONDS 必須為 0，否則部分測試資料不會呼叫 Qwen。"
    }
    if ($AccountMode -eq "hybrid" -and [Math]::Abs([double]$replyHealth.similarity_threshold - 0.8) -gt 0.0001) {
        throw "Hybrid 測試要求 SIMILARITY_THRESHOLD=0.8，現在是 $($replyHealth.similarity_threshold)。"
    }
    Initialize-TestCandidates

    Write-Host ""
    Write-Host "開始讀取：$JsonlPath" -ForegroundColor Cyan
    Write-Host "直播場次ID：$streamId" -ForegroundColor Cyan
    Write-Host "帳號：$($AccountIds -join '、')"
    Write-Host "所有帳號模式：$AccountMode"
    Write-Host ""

    $accountModes = @{}
    foreach ($accountId in $AccountIds) {
        $accountModes[$accountId] = $AccountMode
    }

    $lineNumber = 0
    foreach ($line in Get-Content -LiteralPath $JsonlPath -Encoding UTF8) {
        $lineNumber++
        if (-not $line.Trim()) {
            continue
        }

        try {
            $row = $line | ConvertFrom-Json
            $rawText = [string]$row.raw_text
            if (-not $rawText.Trim()) {
                continue
            }
            if ($MaxRecords -gt 0 -and $processedCount -ge $MaxRecords) {
                break
            }

            $requestTimer = [Diagnostics.Stopwatch]::StartNew()
            $response = Invoke-JsonRequest -Method Post `
                -Uri "$ReplyApiBase/match" `
                -Body @{
                    raw_text = $rawText
                    stream_id = $streamId
                    account_ids = $AccountIds
                    account_modes = $accountModes
                    product_type = $ProductType
                    entities = $row.entities
                    turn_id = "${streamId}:${lineNumber}"
                }
            $requestTimer.Stop()

            $result = $response.result
            if ($AccountMode -eq "qwen_only") {
                if ([bool]$result.vector_search_performed) {
                    throw "qwen_only 不應執行向量搜尋，但 API 回報已執行。"
                }
                if (
                    -not [bool]$result.qwen_called -and
                    -not [bool]$result.model_input_pending
                ) {
                    throw "qwen_only 應呼叫 Qwen，但 API 回報未呼叫。"
                }
                if (
                    [bool]$result.qwen_called -and
                    [int]$result.reference_candidate_count -ne $DesiredCandidateCount
                ) {
                    throw "Qwen 應收到 $DesiredCandidateCount 句候選，實際為 $($result.reference_candidate_count)。"
                }
                if (
                    [bool]$result.qwen_called -and
                    [int]$result.generation.candidate_count_sent -ne $DesiredCandidateCount
                ) {
                    throw "實際送入Qwen的候選應為 $DesiredCandidateCount 句，目前為 $($result.generation.candidate_count_sent)。"
                }
            }
            $processedCount++
            Write-Host (
                "========== 第 {0} 筆（JSONL 行 {1}）==========" -f `
                $processedCount, $lineNumber
            ) -ForegroundColor Cyan
            if ([bool]$result.qwen_called) {
                Write-Host "主播（本次實際送入模型的合併內容）：$([string]$result.model_input_text)"
            }
            elseif ([bool]$result.model_input_pending) {
                Write-Host "主播片段（已暫存，尚未送模型）：$rawText"
            }
            else {
                Write-Host "主播：$rawText"
            }
            Write-Host "產品種類：$($result.product_type)"
            Write-Host (
                "向量執行={0}，向量候選={1}，模型參考候選={2}，Qwen呼叫={3}，模型累計字數={4}/{5}，等待合併={6}" -f `
                [bool]$result.vector_search_performed,
                [int]$result.candidate_count,
                [int]$result.reference_candidate_count,
                [bool]$result.qwen_called,
                [int]$result.model_input_char_count,
                [int]$result.model_min_input_chars,
                [bool]$result.model_input_pending
            )
            Write-Host (
                "全體哄台={0}，哄台內容={1}" -f `
                [bool]$result.crowd_response,
                $(if ($null -ne $result.crowd_token) { [string]$result.crowd_token } else { "無" })
            )
            Write-Host "哄台預掃描：$(Format-CrowdSignalHint $result.generation)" `
                -ForegroundColor DarkGray
            Write-Host (
                "CLS保存={0} | 場次片段={1} | 歷史掃描={2} | 帶入歷史={3} | CLS時間={4:N2} ms | 記憶搜尋={5:N2} ms" -f `
                [bool]$result.cls_memory.stored,
                [int]$result.cls_memory.cache_count,
                [int]$result.cls_memory.history_count,
                [int]$result.cls_memory.retrieved_count,
                [double]$result.cls_elapsed_ms,
                [double]$result.memory_read_elapsed_ms
            ) -ForegroundColor DarkGray

            if ([bool]$result.model_input_pending) {
                Write-Host "尚未累積滿 $([int]$result.model_min_input_chars) 字：本筆不呼叫模型，也不輸出觀眾留言。" -ForegroundColor Yellow
            }
            foreach ($accountResult in @($result.account_results)) {
                if ([bool]$result.model_input_pending) {
                    continue
                }
                if ([string]$accountResult.mode -ne $AccountMode) {
                    throw "帳號 $($accountResult.account_id) 模式不符：$($accountResult.mode)"
                }
                if (
                    $AccountMode -eq "qwen_only" -and
                    [string]$accountResult.reply_source -notin @(
                        "qwen", "qwen_ignore", "qwen_crowd", "model_input_pending"
                    )
                ) {
                    throw "帳號 $($accountResult.account_id) 並非由 Qwen 決定：$($accountResult.reply_source)"
                }
                if ([bool]$accountResult.has_reply) {
                    $replyCount++
                }
                $evidence = if ($accountResult.model_result.evidence_text) {
                    [string]$accountResult.model_result.evidence_text
                }
                elseif ($accountResult.selected.matched_current_fragment) {
                    [string]$accountResult.selected.matched_current_fragment
                }
                else { "無" }
                $validationReason = if ($accountResult.model_result.validation_reason) {
                    [string]$accountResult.model_result.validation_reason
                }
                else { "無" }
                Write-Host (
                    "帳號={0} | 模式={1} | 來源={2} | 留言={3} | 回覆片段={4} | 判定原因={5} | 處理時間={6:N2} ms" -f `
                    [string]$accountResult.account_id,
                    [string]$accountResult.mode,
                    [string]$accountResult.reply_source,
                    [string]$accountResult.reply,
                    $evidence,
                    $validationReason,
                    [double]$result.elapsed_ms
                )
            }
            foreach ($accountResult in @($result.account_results)) {
                $reportEvidence = if ($accountResult.model_result.evidence_text) {
                    [string]$accountResult.model_result.evidence_text
                }
                elseif ($accountResult.selected.matched_current_fragment) {
                    [string]$accountResult.selected.matched_current_fragment
                }
                else { "" }
                Add-LiveTestReportRow -Report $testReport -Row ([ordered]@{
                    timestamp = (Get-Date).ToString("o")
                    turn_id = "${streamId}:${lineNumber}"
                    stream_id = $streamId
                    jsonl_line = $lineNumber
                    raw_text = $rawText
                    model_input_text = [string]$result.model_input_text
                    account_id = [string]$accountResult.account_id
                    mode = [string]$accountResult.mode
                    style = [string]$accountResult.style
                    reply_source = [string]$accountResult.reply_source
                    reply = [string]$accountResult.reply
                    evidence = $reportEvidence
                    similarity = if ($null -ne $accountResult.selected.similarity) {
                        [double]$accountResult.selected.similarity
                    } else { $null }
                    model_input_pending = [bool]$result.model_input_pending
                    qwen_called = [bool]$result.qwen_called
                    crowd_response = [bool]$result.crowd_response
                    crowd_attention = [string]$result.generation.crowd_signal_hints.attention
                    crowd_signal = Format-CrowdSignalHint $result.generation
                    cls_cache_count = [int]$result.cls_memory.cache_count
                    cls_retrieved_count = [int]$result.cls_memory.retrieved_count
                    cls_ms = [double]$result.cls_elapsed_ms
                    memory_read_ms = [double]$result.memory_read_elapsed_ms
                    vector_ms = [double]$result.vector_search_elapsed_ms
                    qwen_ms = if ($null -ne $result.generation) {
                        [double]$result.generation.elapsed_ms
                    } else { 0.0 }
                    server_ms = [double]$result.elapsed_ms
                    powershell_ms = [double]$requestTimer.Elapsed.TotalMilliseconds
                    learned_example_count = [int]$result.learned_example_count
                })
            }
            $timingText = (
                "伺服器總時間={0:N2} ms | PowerShell請求時間={1:N2} ms | 向量時間={2:N2} ms" -f `
                [double]$result.elapsed_ms,
                [double]$requestTimer.Elapsed.TotalMilliseconds,
                [double]$result.vector_search_elapsed_ms
            )
            Write-Host $timingText -ForegroundColor DarkGray

            if ($null -ne $result.generation) {
                Write-Host (
                    "Qwen批次結果={0} 筆 | Qwen時間={1:N2} ms | 輸入token={2} | 輸出token={3} | 傳入候選={4}" -f `
                    [int]$result.generation.result_count,
                    [double]$result.generation.elapsed_ms,
                    [int]$result.generation.ollama_metrics.prompt_eval_count,
                    [int]$result.generation.ollama_metrics.eval_count,
                    [int]$result.generation.candidate_count_sent
                ) -ForegroundColor DarkGray
            }
            Write-Host (
                "學習資料庫={0} | 樣本={1}/{2} | 向量啟用={3} | 本次新增={4} | 命中候選={5}" -f `
                [string]$result.live_database,
                [int]$result.learned_example_count,
                [int]$result.minimum_vector_examples,
                [bool]$result.learning_ready,
                [int]$result.learned_example_storage.stored_count,
                [int]$result.learned_candidates_selected
            ) -ForegroundColor DarkGray
            Write-Host ""
        }
        catch {
            $failedCount++
            Write-Warning "第 $lineNumber 行處理失敗：$($_.Exception.Message)"
        }
    }
}
finally {
    $wholeTest.Stop()
    $reportSummary = Complete-LiveTestReport -Report $testReport -Counters @{
        processed = $processedCount
        replies = $replyCount
        failures = $failedCount
        total_seconds = $wholeTest.Elapsed.TotalSeconds
    }
    if (-not $KeepLiveDatabase) {
        Remove-TestLiveDatabase
    }
    Restore-Candidates
    Write-Host ""
    Write-Host "========== 測試摘要 ==========" -ForegroundColor Green
    Write-Host "成功處理主播發言：$processedCount"
    Write-Host "產生帳號留言數：$replyCount"
    Write-Host "失敗筆數：$failedCount"
    Write-Host ("測試總時間：{0:N2} 秒" -f $wholeTest.Elapsed.TotalSeconds)
    Write-Host "測試成果：$($testReport.Directory)" -ForegroundColor Green
    Write-Host (
        "延遲摘要：average={0:N2} ms，p50={1:N2} ms，p95={2:N2} ms" -f `
        [double]$reportSummary.latency_ms.average,
        [double]$reportSummary.latency_ms.p50,
        [double]$reportSummary.latency_ms.p95
    )
}
