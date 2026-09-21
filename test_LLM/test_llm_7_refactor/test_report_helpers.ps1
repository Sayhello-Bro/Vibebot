function New-LiveTestReport {
    param(
        [Parameter(Mandatory)] [string]$Root,
        [Parameter(Mandatory)] [string]$StreamId
    )

    $stamp = Get-Date -Format "yyyyMMdd_HHmmss_fff"
    $safeStreamId = $StreamId -replace '[^0-9A-Za-z_.-]', '_'
    $directory = Join-Path $Root "${stamp}_${safeStreamId}"
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    [pscustomobject]@{
        Directory = $directory
        JsonlPath = Join-Path $directory "results.jsonl"
        CsvPath = Join-Path $directory "results.csv"
        SummaryPath = Join-Path $directory "summary.json"
        CsvStarted = $false
    }
}

function Format-CrowdSignalHint {
    param($Generation)
    if ($null -eq $Generation -or $null -eq $Generation.crowd_signal_hints) {
        return "attention=none | keywords=none | viewer_tokens=none | explicit=none"
    }
    $hints = $Generation.crowd_signal_hints
    $counts = @(
        $hints.keyword_counts.PSObject.Properties |
            ForEach-Object { "{0}={1}" -f $_.Name, $_.Value }
    )
    $explicit = @($hints.explicit_patterns)
    $tokens = @($hints.suggested_tokens)
    return "attention={0} | keywords={1} | viewer_tokens={2} | explicit={3}" -f `
        [string]$hints.attention,
        $(if ($counts.Count) { $counts -join "," } else { "none" }),
        $(if ($tokens.Count) { $tokens -join "," } else { "none" }),
        $(if ($explicit.Count) { $explicit -join " / " } else { "none" })
}

function Add-LiveTestReportRow {
    param(
        [Parameter(Mandatory)] [pscustomobject]$Report,
        [Parameter(Mandatory)] [System.Collections.IDictionary]$Row
    )

    $object = [pscustomobject]$Row
    $object | ConvertTo-Json -Depth 20 -Compress |
        Add-Content -LiteralPath $Report.JsonlPath -Encoding UTF8
    if (-not $Report.CsvStarted) {
        $object | Export-Csv -LiteralPath $Report.CsvPath -NoTypeInformation -Encoding UTF8
        $Report.CsvStarted = $true
    }
    else {
        $object | Export-Csv -LiteralPath $Report.CsvPath -NoTypeInformation `
            -Encoding UTF8 -Append
    }
}

function Get-PercentileValue {
    param([double[]]$Values, [double]$Percentile)
    if ($Values.Count -eq 0) { return 0.0 }
    $sorted = @($Values | Sort-Object)
    $index = [Math]::Ceiling(($Percentile / 100.0) * $sorted.Count) - 1
    $index = [Math]::Max(0, [Math]::Min($sorted.Count - 1, $index))
    return [double]$sorted[$index]
}

function Complete-LiveTestReport {
    param(
        [Parameter(Mandatory)] [pscustomobject]$Report,
        [Parameter(Mandatory)] [hashtable]$Counters
    )

    $rows = @()
    if (Test-Path -LiteralPath $Report.JsonlPath) {
        $rows = @(
            Get-Content -LiteralPath $Report.JsonlPath -Encoding UTF8 |
                Where-Object { $_.Trim() } |
                ForEach-Object { $_ | ConvertFrom-Json }
        )
    }
    $eventRows = @($rows | Group-Object turn_id | ForEach-Object { $_.Group[0] })
    $latencies = [double[]]@($eventRows | ForEach-Object { [double]$_.server_ms })
    $summary = [ordered]@{
        completed_at = (Get-Date).ToString("o")
        result_directory = $Report.Directory
        account_rows = $rows.Count
        event_rows = $eventRows.Count
        processed = [int]$Counters.processed
        replies = [int]$Counters.replies
        failures = [int]$Counters.failures
        total_seconds = [Math]::Round([double]$Counters.total_seconds, 3)
        latency_ms = [ordered]@{
            average = if ($latencies.Count) {
                [Math]::Round(($latencies | Measure-Object -Average).Average, 2)
            } else { 0.0 }
            p50 = [Math]::Round((Get-PercentileValue $latencies 50), 2)
            p95 = [Math]::Round((Get-PercentileValue $latencies 95), 2)
            maximum = if ($latencies.Count) {
                [Math]::Round(($latencies | Measure-Object -Maximum).Maximum, 2)
            } else { 0.0 }
        }
    }
    $summary | ConvertTo-Json -Depth 10 |
        Set-Content -LiteralPath $Report.SummaryPath -Encoding UTF8
    return $summary
}
