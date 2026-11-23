# 在固定镜像里执行记忆召回基线（Windows PowerShell）。
#   powershell -File deploy\eval\recall\run.ps1 [-Mode run|twice|ablate|scale|test] [-FishDir ...] -OpenccDir ... [-Out 目录] [-Extra '--ledger 800 --memory 50']
# -Extra 把其余参数原样交给 run.mjs（例如 --only c1,c2、--ledger N、--memory N、--memstyle all）。
# 镜像不存在时先建：docker build -t recall-baseline:1 -f deploy\eval\recall\Dockerfile deploy\eval\recall
# 挂载：整个 deploy 目录（唯读；被测程式 deploy\social 与 harness 都在里面）、鱼记忆 1.3.7、OpenCC 1.4.2（都唯读，harness 会核对 sha256）。
param(
	[string]$Mode = "run",
	[string]$FishDir = "",
	[string]$OpenccDir = "",
	[string]$Out = "",
	[string]$Extra = ""
)
$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
if (-not $FishDir) { $FishDir = Join-Path $repo ".cache\dsh-fish-memory" }
if (-not $OpenccDir) { throw "要用 -OpenccDir 指到已审阅的 opencc-js 1.4.2 套件目录（里面有 dist\esm\t2cn.js）" }
$mounts = @("-v", "$($repo)\deploy:/deploy:ro", "-v", "${FishDir}:/fish:ro", "-v", "${OpenccDir}:/opencc:ro")
if ($Out) { New-Item -ItemType Directory -Force $Out | Out-Null; $mounts += @("-v", "${Out}:/out") }
$base = @("run", "--rm") + $mounts + @("-e", "SOCIAL_DIR=/deploy/social", "--workdir", "/deploy/eval/recall", "--entrypoint", "node", "recall-baseline:1")
$extraArgs = if ($Extra) { $Extra -split ' ' } else { @() }
$json = if ($Out) { @("--json", "/out/result.json") } else { @() }
switch ($Mode) {
	"run" { & docker @base run.mjs @json @extraArgs }
	"twice" { & docker @base run.mjs --twice @json @extraArgs }
	"ablate" { & docker @base run.mjs --ablate @json @extraArgs }
	"scale" { & docker @base scale.mjs @json @extraArgs }
	"test" { & docker @base --test harness.test.mjs }
	default { throw "未知模式 $Mode" }
}
exit $LASTEXITCODE
