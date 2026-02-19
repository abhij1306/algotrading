# PowerShell script to check for console.log statements in production code
# Excludes test files and stories

$ErrorActionPreference = "Stop"
$violations = @()

# Get all TypeScript/TSX files excluding test files and stories
$files = Get-ChildItem -Path "frontend" -Include "*.ts","*.tsx" -Recurse -File |
    Where-Object {
        $_.FullName -notmatch "node_modules" -and
        $_.FullName -notmatch "\.next" -and
        $_.FullName -notmatch "out" -and
        $_.FullName -notmatch "\.test\." -and
        $_.FullName -notmatch "\.stories\."
    }

foreach ($file in $files) {
    $content = Get-Content $file.FullName -Raw
    $lines = Get-Content $file.FullName

    for ($i = 0; $i -lt $lines.Count; $i++) {
        $line = $lines[$i]
        if ($line -match "console\.log\(") {
            $relativePath = $file.FullName -replace [regex]::Escape($PWD.Path + "\"), ""
            $violations += "${relativePath}:$($i + 1): Found console.log() - use console.error() or console.warn() instead"
        }
    }
}

if ($violations.Count -gt 0) {
    Write-Host "Console.log violations found:" -ForegroundColor Red
    Write-Host ""
    foreach ($violation in $violations) {
        Write-Host "  $violation" -ForegroundColor Yellow
    }
    Write-Host ""
    Write-Host "Fix: Replace console.log() with console.error() or console.warn()" -ForegroundColor Cyan
    exit 1
}

exit 0
