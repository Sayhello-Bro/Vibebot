param(
    [string]$JsonlPath = (Join-Path $PSScriptRoot "live_001.jsonl"),
    [string]$ReplyApiBase = "http://127.0.0.1:5002",
    [string]$DataApiBase = "http://127.0.0.1:5001",
    [ValidateNotNullOrEmpty()]
    [string]$ProductType = "服飾",
    [string]$StreamId = "",
    [int]$MaxRecords = 3,
    [string]$TestResultRoot = (Join-Path $PSScriptRoot "test_results"),
    [switch]$KeepLiveDatabase,
    [switch]$KeepCandidateChanges
)

$ErrorActionPreference = "Stop"
$testParameters = @{
    JsonlPath = $JsonlPath
    ReplyApiBase = $ReplyApiBase
    DataApiBase = $DataApiBase
    AccountIds = @(
        "account_001",
        "account_002",
        "account_003",
        "account_004",
        "account_005"
    )
    AccountMode = "hybrid"
    ProductType = $ProductType
    MaxRecords = $MaxRecords
    TestResultRoot = $TestResultRoot
}
if (-not [string]::IsNullOrWhiteSpace($StreamId)) {
    $testParameters.StreamId = $StreamId
}
if ($KeepLiveDatabase) {
    $testParameters.KeepLiveDatabase = $true
}
if ($KeepCandidateChanges) {
    $testParameters.KeepCandidateChanges = $true
}

& (Join-Path $PSScriptRoot "test_three_accounts.ps1") @testParameters
