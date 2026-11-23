# PowerShell 版（Windows 原生）：一行跑完整的 Linux 测试，root 与 dsh 各一次。结束码跟测试结果一致。
#   powershell -File deploy\draw\test-container\run_linux_tests.ps1 [-Samples C:\...\samples]
# 映像不存在时：先 docker load -i whaledraw-test-0.2.2.tar.gz。环境变数 STAGE_DIR、TESTS 的意思见 run_linux_tests.sh。
# 注意：这个档案要存成「带 BOM 的 UTF-8」，Windows PowerShell 5.1 才不会把中文注解读成乱码、连 param 一起吞掉。
param([string]$Samples = "")
$ErrorActionPreference = "Stop"
$stage = if ($env:STAGE_DIR) { $env:STAGE_DIR } else { Split-Path -Parent $PSScriptRoot }
$dockerArgs = @("run", "--rm", "-v", "${stage}:/stage:ro")
if ($Samples) { $dockerArgs += @("-v", "${Samples}:/samples:ro") }
if ($env:TESTS) { $dockerArgs += @("-e", "TESTS=$($env:TESTS)") }
$dockerArgs += "whaledraw-test:0.2.2"
& docker @dockerArgs
exit $LASTEXITCODE
