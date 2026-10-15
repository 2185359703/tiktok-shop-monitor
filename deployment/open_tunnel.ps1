param([ValidateRange(0, 65535)][int]$ProxyPort = 0)
$ErrorActionPreference = 'Stop'
$projectDirectory = Split-Path -Parent $PSScriptRoot
$metadata = Join-Path $projectDirectory 'data/samples/deployment/tunnel.json'
foreach ($port in 18080,18090,18072) {
    if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
        throw "本机端口 $port 已被占用，请核对后选择其他转发端口。"
    }
}
$sshArguments = @('-N','-C','-o','BatchMode=yes','-o','ExitOnForwardFailure=yes','-o','ServerAliveInterval=30','-o','ServerAliveCountMax=3')
if ($ProxyPort) {
    if (-not (Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $ProxyPort -State Listen -ErrorAction SilentlyContinue)) {
        throw "本机代理端口 $ProxyPort 没有监听；未修改 SSH 全局配置。"
    }
    $proxyPython = Join-Path $projectDirectory '.venv/Scripts/python.exe'
    $proxyBridge = Join-Path $PSScriptRoot 'ssh_proxy_bridge.py'
    $proxyArgument = '"ProxyCommand=\"' + $proxyPython + '\" \"' + $proxyBridge + '\" %h %p --proxy-port ' + $ProxyPort + '"'
    $sshArguments += @('-o', $proxyArgument)
}
$sshArguments += @('-L','127.0.0.1:18080:127.0.0.1:8000','-L','127.0.0.1:18090:127.0.0.1:9090','-L','127.0.0.1:18072:127.0.0.1:15672','google-vps')
$sshProcess = Start-Process -FilePath 'C:\Windows\System32\OpenSSH\ssh.exe' -ArgumentList $sshArguments -WindowStyle Hidden -PassThru -RedirectStandardError (Join-Path $projectDirectory 'data/samples/deployment/tunnel.stderr.log')
@{pid=$sshProcess.Id;ssh_alias='google-vps';proxy_port=$ProxyPort;api_url='http://127.0.0.1:18080';prometheus_url='http://127.0.0.1:18090';rabbitmq_url='http://127.0.0.1:18072'} | ConvertTo-Json | Set-Content -LiteralPath $metadata -Encoding utf8
Write-Output "SSH 转发进程：$($sshProcess.Id)；API：http://127.0.0.1:18080"
