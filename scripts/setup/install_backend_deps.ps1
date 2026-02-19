$ErrorActionPreference = "Stop"

$python = "venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
  Write-Error "venv\Scripts\python.exe not found. Create the virtual environment first."
}

Write-Host "[1/2] Installing core backend dependencies..."
& $python -m pip install -r backend\requirements.txt
if ($LASTEXITCODE -ne 0) {
  Write-Host "[ERROR] pip install -r backend\\requirements.txt failed with exit code $LASTEXITCODE"
  Exit $LASTEXITCODE
}

Write-Host "[2/2] Installing fyers-apiv3 without legacy pinned deps (Python 3.14 safe)..."
& $python -m pip install fyers-apiv3==3.1.10 --no-deps
if ($LASTEXITCODE -ne 0) {
  Write-Host "[ERROR] pip install fyers-apiv3==3.1.10 --no-deps failed with exit code $LASTEXITCODE"
  Exit $LASTEXITCODE
}

Write-Host "[OK] Backend dependencies installed."
