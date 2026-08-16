$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Resolve-PythonExe {
  $candidates = @(
    'py',
    'python',
    'python3',
    "$env:LocalAppData\Programs\Python\Python312\python.exe",
    "$env:LocalAppData\Programs\Python\Python311\python.exe",
    "$env:ProgramFiles\Python312\python.exe",
    "$env:ProgramFiles\Python311\python.exe"
  )

  foreach ($name in $candidates) {
    if (-not $name) { continue }

    $isPath = $name -like '*\\*'
    if ($isPath -and -not (Test-Path $name)) {
      continue
    }

    if (-not $isPath) {
      $cmd = Get-Command $name -ErrorAction SilentlyContinue
      if (-not $cmd) {
        continue
      }
    }

    if ($name -eq 'py') {
      & py -3 --version *> $null
    } else {
      & $name --version *> $null
    }

    if ($LASTEXITCODE -eq 0) {
      return $name
    }
  }

  throw "No working Python 3 executable found. Install Python 3 (https://www.python.org/downloads/windows/) and enable Add python.exe to PATH."
}

$pythonExe = Resolve-PythonExe

if (-not (Test-Path .venv-win)) {
  if ($pythonExe -eq 'py') {
    py -3 -m venv .venv-win
  } else {
    & $pythonExe -m venv .venv-win
  }
}

if (-not (Test-Path .\.venv-win\Scripts\python.exe)) {
  throw "Virtual environment was not created. Confirm Python 3 is installed and rerun this script."
}

& .\.venv-win\Scripts\python.exe -m pip install -r requirements-windows-agent.txt

if (-not $env:WINDOWS_AGENT_ALLOWED_ROOTS) {
  $env:WINDOWS_AGENT_ALLOWED_ROOTS = "$env:USERPROFILE\Documents"
}

if (-not $env:WINDOWS_AGENT_TOKEN) {
  Write-Host "WINDOWS_AGENT_TOKEN is not set. Agent will accept unauthenticated LAN requests."
}

& .\.venv-win\Scripts\python.exe windows_agent.py
