# Report presence only. Never output key values or file contents.
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskPath = Join-Path $taskRoot '.env'
$taskNames = @('OPENAI_API_KEY', 'ANTHROPIC_API_KEY')
$taskFound = @{}
foreach ($taskName in $taskNames) { $taskFound[$taskName] = $false }
if (Test-Path -LiteralPath $taskPath) {
    foreach ($taskLine in [System.IO.File]::ReadLines($taskPath)) {
        $taskParts = $taskLine.Split(@('='), 2)
        if ($taskParts.Length -eq 2 -and $taskFound.ContainsKey($taskParts[0].Trim())) {
            $taskFound[$taskParts[0].Trim()] = -not [string]::IsNullOrWhiteSpace($taskParts[1])
        }
    }
}
foreach ($taskName in $taskNames) {
    [pscustomobject]@{ Variable = $taskName; SavedInLocalFile = $taskFound[$taskName] }
}
$taskLine = $null
$taskParts = $null
