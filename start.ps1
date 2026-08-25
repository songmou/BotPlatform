 $ErrorActionPreference = "Stop"

$ProjectDir = $PSScriptRoot
$VenvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$VenvPip = Join-Path $ProjectDir ".venv\Scripts\pip.exe"
$ModelEnvFile = Join-Path $ProjectDir "data\system\model.env"

if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    Write-Error "未找到 Python 虚拟环境：$ProjectDir\.venv。请运行：py -m venv .venv；.\.venv\Scripts\python.exe -m pip install -r requirements.txt"
}

if (Test-Path -LiteralPath $VenvPip -PathType Leaf) {
    $Prefix = & $VenvPython -c "import sys; print(sys.prefix)"
    $Expected = [IO.Path]::GetFullPath((Join-Path $ProjectDir ".venv"))
    if ([IO.Path]::GetFullPath($Prefix.Trim()) -ne $Expected) {
        Write-Error "虚拟环境来自其他项目目录，请重新创建 .venv。"
    }
}

& $VenvPython -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) {
    Write-Error "BotPlatform 要求 Python 3.10 或更高版本，请重新创建 .venv。"
}

if (Test-Path -LiteralPath $ModelEnvFile -PathType Leaf) {
    $Lines = @(Get-Content -LiteralPath $ModelEnvFile | Where-Object { $_.Trim() })
    if ($Lines.Count -ne 1 -or $Lines[0] -notmatch '^DEEPSEEK_API_KEY=(.+)$') {
        Write-Error "模型密钥文件只能包含非空的 DEEPSEEK_API_KEY。"
    }
    $env:DEEPSEEK_API_KEY = $Matches[1]
}

& $VenvPython (Join-Path $ProjectDir "main.py") @args
exit $LASTEXITCODE
