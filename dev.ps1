<#
.SYNOPSIS
    测试数据管理平台 · 一键开发脚本

.DESCRIPTION
    把"起库 -> 建表 -> 灌演示数据 -> 跑测试 -> 起服务 -> 压测"收成一条命令。
    每个动作都幂等，可以反复执行。

    没有它的时候，接手这个项目要记 6 条命令、2 个硬编码路径和 1 个环境变量；
    有了它，`.\dev.ps1` 就能从零跑到"测试全绿"。

.PARAMETER Action
    all     检查环境 -> 初始化数据库 -> 跑测试 -> 端到端冒烟（默认）
    check   只做环境体检：Python / 依赖 / PG 连通性 / 表结构
    init    建库建表 + 演示数据（幂等）
    test    跑 pytest（单元 + 真库集成）
    smoke   起一个临时服务跑真实 HTTP 冒烟，跑完自动关掉
    worker  跑 outbox worker（默认处理到没有到期消息；-Watch 常驻）
    recon   跑日终对账（明细 vs 汇总 vs 失败是否提单）
    bench   五项压测：档期竞争(1000) + 领取竞争(20) + 提交竞争(20) + outbox 故障注入 + 对账注入
    api     启动 API 服务（默认 http://127.0.0.1:8000/docs）
    docs    打印常用地址与示例请求

.PARAMETER Port
    API 端口，默认 8000。

.PARAMETER Concurrency
    档期竞争压测的并发数，默认 1000。

.PARAMETER ClaimConcurrency
    领取竞争 / 提交竞争压测的并发数，默认 20（技术方案验收线就是 20 个人抢一条任务）。

.PARAMETER Watch
    worker 常驻轮询（默认只处理到没有到期消息就退出）。

.PARAMETER Reload
    启动 API 时开启热重载（改代码自动重启）。

.PARAMETER Dsn
    覆盖数据库连接串，默认读环境变量 TDP_DATABASE_URL。

.EXAMPLE
    .\dev.ps1
    从零到测试全绿。

.EXAMPLE
    .\dev.ps1 api -Reload
    起服务并热重载，再打开 http://127.0.0.1:8000/docs

.EXAMPLE
    .\dev.ps1 bench -Concurrency 500
    跑 500 并发档期竞争实验。

.NOTES
    执行策略拦截时用 dev.cmd（只对本进程 Bypass，不改系统设置）。
#>
[CmdletBinding()]
param(
    [ValidateSet('all', 'check', 'init', 'test', 'smoke', 'worker', 'recon', 'bench', 'api', 'docs', 'token', 'sched')]
    [string]$Action = 'all',

    [int]$Port = 8000,
    [int]$Concurrency = 1000,
    [int]$ClaimConcurrency = 20,
    [switch]$Watch,
    [switch]$Reload,
    [string]$Dsn = $env:TDP_DATABASE_URL,
    # dev.cmd token 用：默认给管理员演示账号
    [string]$User = 'admin',
    [string]$Password = 'Admin@12345'
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $Root

# 中文输出：不设 UTF-8，Windows 控制台会把中文（含 JSON 里的中文）打成乱码
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

if ($Dsn) { $env:TDP_DATABASE_URL = $Dsn }

# ====================== 输出 ======================
function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}
function Write-Ok([string]$Message) { Write-Host "  [OK]   $Message" -ForegroundColor Green }
function Write-Note([string]$Message) { Write-Host "  [..]   $Message" -ForegroundColor DarkGray }
function Write-Warn([string]$Message) { Write-Host "  [WARN] $Message" -ForegroundColor Yellow }
function Write-Fail([string]$Message) { Write-Host "  [FAIL] $Message" -ForegroundColor Red }

# ====================== 探测工具 ======================
function Get-PythonPath {
    foreach ($candidate in @($env:TDP_PYTHON, 'python')) {
        if (-not $candidate) { continue }
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    throw ' 找不到 python。请安装 Python 3.10+，或用环境变量 TDP_PYTHON 指定解释器路径。'
}

function Get-DsnPort([string]$Value) {
    if ($Value -and $Value -match ':\s*(\d+)\s*/') { return [int]$Matches[1] }
    return 5433                      # 本机绿色版 PG 端口，见技术方案 2 节
}

function Test-TcpPort([string]$TargetHost, [int]$TcpPort, [int]$TimeoutMs = 800) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $pending = $client.BeginConnect($TargetHost, $TcpPort, $null, $null)
        if (-not $pending.AsyncWaitHandle.WaitOne($TimeoutMs)) { return $false }
        $client.EndConnect($pending)
        return $true
    }
    catch { return $false }
    finally { $client.Close() }
}

function Find-PgBin {
    $candidates = @(
        $env:TDP_PG_BIN,
        'C:\Users\13741\dsh-devtools\pgsql\bin',
        'C:\Program Files\PostgreSQL\16\bin'
    )
    foreach ($dir in $candidates) {
        if ($dir -and (Test-Path -LiteralPath (Join-Path $dir 'pg_ctl.exe'))) { return $dir }
    }
    return $null
}

function Find-PgData {
    $candidates = @(
        $env:TDP_PGDATA,
        'C:\Users\13741\dsh-devtools\pgdata',
        'C:\Program Files\PostgreSQL\16\data'
    )
    foreach ($dir in $candidates) {
        if ($dir -and (Test-Path -LiteralPath (Join-Path $dir 'PG_VERSION'))) { return $dir }
    }
    return $null
}

function Ensure-Postgres {
    if (Test-TcpPort '127.0.0.1' $DbPort) {
        Write-Ok "PostgreSQL 已在监听 127.0.0.1:$DbPort"
        return
    }

    $bin = Find-PgBin
    $data = Find-PgData
    if (-not $bin -or -not $data) {
        throw "PostgreSQL 未在 127.0.0.1:$DbPort 监听，且找不到 pg_ctl/pgdata。请手工启动数据库，或用环境变量 TDP_PG_BIN / TDP_PGDATA 指定路径。"
    }

    Write-Note "未监听 $DbPort，用 pg_ctl 拉起：$data"
    $log = Join-Path $data 'server.log'
    & (Join-Path $bin 'pg_ctl.exe') -D $data -o "-p $DbPort" -l $log -w start 2>&1 |
        ForEach-Object { Write-Note "$_" }

    for ($i = 0; $i -lt 30; $i++) {
        if (Test-TcpPort '127.0.0.1' $DbPort) {
            Write-Ok "PostgreSQL 已启动（端口 $DbPort）"
            return
        }
        Start-Sleep -Milliseconds 500
    }
    throw "PostgreSQL 启动超时，请查看 $log"
}

# ====================== 动作 ======================
# 端口要在函数定义之后算：PowerShell 边解析边执行，函数没定义就调不到
$DbPort = Get-DsnPort $env:TDP_DATABASE_URL
$RedisPort = Get-DsnPort $env:TDP_REDIS_URL
if (-not $env:TDP_REDIS_URL) { $RedisPort = 6380 }

function Find-RedisBin {
    $candidates = @(
        $env:TDP_REDIS_BIN,
        'C:\Users\13741\dsh-devtools\redis\redis-server.exe',
        'C:\Program Files\Redis\redis-server.exe'
    )
    foreach ($exe in $candidates) {
        if ($exe -and (Test-Path -LiteralPath $exe)) { return $exe }
    }
    return $null
}

function Ensure-Redis {
    if (Test-TcpPort '127.0.0.1' $RedisPort) {
        Write-Ok "Redis 已在监听 127.0.0.1:$RedisPort"
        return
    }
    $bin = Find-RedisBin
    if (-not $bin) {
        throw "Redis 未在 127.0.0.1:$RedisPort 监听，且找不到 redis-server.exe。请手工启动，或用环境变量 TDP_REDIS_BIN 指定路径。"
    }
    $conf = Join-Path (Split-Path -Parent $bin) "redis-$RedisPort.conf"
    if (Test-Path -LiteralPath $conf) {
        Write-Note "未监听 $RedisPort，用配置拉起：$conf"
        Start-Process -FilePath $bin -ArgumentList @($conf) -WindowStyle Hidden | Out-Null
    }
    else {
        Write-Note "未监听 $RedisPort，拉起 redis-server（默认配置）"
        Start-Process -FilePath $bin -ArgumentList @('--port', "$RedisPort") -WindowStyle Hidden | Out-Null
    }
    for ($i = 0; $i -lt 20; $i++) {
        if (Test-TcpPort '127.0.0.1' $RedisPort) {
            Write-Ok "Redis 已启动（端口 $RedisPort）"
            return
        }
        Start-Sleep -Milliseconds 300
    }
    throw "Redis 启动超时（端口 $RedisPort）"
}

function Invoke-Python([string[]]$Arguments) {
    $python = Get-PythonPath
    & $python @Arguments
    if ($LASTEXITCODE -ne 0) { throw "python $($Arguments -join ' ') 退出码 $LASTEXITCODE" }
}

function Get-DbStatus {
    <# 返回 @{Code=0|2|3; Text='tables=17 indexes=42 db=tdp schema=0002_perf_indexes'} #>
    $python = Get-PythonPath
    $text = (& $python 'scripts/init_db.py' '--check' 2>&1 | Out-String).Trim()
    return @{ Code = $LASTEXITCODE; Text = $text }
}

function Invoke-Check {
    Write-Step '环境体检'

    $python = Get-PythonPath
    $pyVersion = (& $python -c "import sys; print('.'.join(map(str, sys.version_info[:3])))").Trim()
    if ([version]$pyVersion -lt [version]'3.10') {
        throw "Python 版本过低：$pyVersion（需要 3.10+）"
    }
    Write-Ok "Python $pyVersion"

    $null = & $python -c "import fastapi, uvicorn, sqlalchemy, psycopg, alembic, prometheus_client" 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Warn '缺少服务依赖 -> python -m pip install -r requirements.txt'
    }
    else {
        Write-Ok '服务依赖齐全（fastapi / uvicorn / sqlalchemy / psycopg / alembic / prometheus-client）'
    }

    if (-not (Test-TcpPort '127.0.0.1' $DbPort)) {
        Write-Warn "PostgreSQL 未监听 $DbPort -> init / api / bench 会自动尝试拉起"
    }
    else {
        Write-Ok "PostgreSQL 可达 127.0.0.1:$DbPort"
        $status = Get-DbStatus
        switch ($status.Code) {
            0 { Write-Ok "业务库就绪：$($status.Text)" }
            3 { Write-Warn "业务库未初始化（$($status.Text)）-> 执行 .\dev.ps1 init" }
            default { Write-Warn "数据库体检失败：$($status.Text)" }
        }
    }

    if (Test-TcpPort '127.0.0.1' $RedisPort) {
        $cacheStrategy = if ($env:TDP_CACHE_STRATEGY) { $env:TDP_CACHE_STRATEGY } else { 'delete' }
        Write-Ok "Redis 可达 127.0.0.1:$RedisPort（缓存策略 $cacheStrategy）"
    }
    else {
        Write-Warn "Redis 未监听 $RedisPort -> test / smoke / api / bench 会自动尝试拉起；缓存测试会跳过"
    }

    $pyTest = (& $python -c "import pytest; print(pytest.__version__)" 2>&1).Trim()
    if ($LASTEXITCODE -eq 0) { Write-Ok "pytest $pyTest" }
    else { Write-Warn '未安装 pytest -> python -m pip install -r requirements-dev.txt' }
}

function Invoke-Init {
    Write-Step '初始化数据库（建库 + 建表 + 演示数据，幂等）'
    Ensure-Postgres
    Invoke-Python @('scripts/init_db.py', '--seed-demo')
}

function Invoke-Test {
    Write-Step '运行测试（单元 + 真库集成 + 缓存）'
    Ensure-Postgres
    Ensure-Redis

    $python = Get-PythonPath
    $null = & $python -c "import pytest" 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Note '安装开发依赖…'
        Invoke-Python @('-m', 'pip', 'install', '-q', '--disable-pip-version-check', '-r', 'requirements-dev.txt')
    }
    Invoke-Python @('-m', 'pytest')
    Write-Ok '测试全绿'
}

function Invoke-Smoke {
    Write-Step "端到端冒烟（真实 HTTP）：http://127.0.0.1:$Port"
    Ensure-Postgres
    Ensure-Redis
    if ((Get-DbStatus).Code -ne 0) {
        Write-Warn '业务库未初始化，先跑 init'
        Invoke-Init
    }

    $python = Get-PythonPath
    $temporary = $null
    if (Test-TcpPort '127.0.0.1' $Port) {
        Write-Note "复用已在 $Port 监听的服务"
    }
    else {
        # 临时起一个服务：这是"部署形态"验证，进程内 TestClient 替代不了
        $outLog = Join-Path $env:TEMP "tdp-api-$Port.out.log"
        $errLog = Join-Path $env:TEMP "tdp-api-$Port.err.log"
        Write-Note "启动临时服务（日志 $outLog）"
        $temporary = Start-Process -FilePath $python -PassThru -WindowStyle Hidden `
            -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$Port") `
            -RedirectStandardOutput $outLog -RedirectStandardError $errLog

        $ready = $false
        for ($i = 0; $i -lt 40; $i++) {
            if (Test-TcpPort '127.0.0.1' $Port) { $ready = $true; break }
            Start-Sleep -Milliseconds 500
        }
        if (-not $ready) {
            throw "临时服务启动超时，请查看 $errLog"
        }
    }

    try {
        Invoke-Python @('scripts/api_smoke.py', '--base-url', "http://127.0.0.1:$Port")
    }
    finally {
        if ($temporary -and -not $temporary.HasExited) {
            Stop-Process -Id $temporary.Id -Force -ErrorAction SilentlyContinue
            Write-Note '临时服务已停止'
        }
    }
}

function Invoke-Bench {
    Write-Step "并发压测：档期 + 领取 + 提交 + outbox 故障注入 + 对账注入 + 缓存一致性"
    Ensure-Postgres
    Ensure-Redis
    if ((Get-DbStatus).Code -ne 0) {
        Write-Warn '业务库未初始化，先跑 init'
        Invoke-Init
    }
    # 压测要跑几十个线程；默认池子（20+20）会被排队主导，测出来的延迟不是业务延迟
    $env:TDP_POOL_SIZE = '40'
    $env:TDP_POOL_OVERFLOW = '40'
    Invoke-Python @('bench/slot_contention.py', '--concurrency', "$Concurrency")
    Invoke-Python @('bench/claim_race.py', '--concurrency', "$ClaimConcurrency")
    Invoke-Python @('bench/execution_submit.py', '--concurrency', "$ClaimConcurrency")
    Invoke-Python @('bench/outbox_fault.py', '--messages', '20')
    Invoke-Python @('bench/recon_inject.py')
    Invoke-Python @('bench/cache_consistency.py', '--readers', '20', '--writers', '20', '--seconds', '8')
    Invoke-Python @('bench/slot_schemes.py', '--concurrency', '100', '--rounds', '2')
    Invoke-Python @('bench/pagination.py', '--rows', '200000', '--iterations', '5')
    Invoke-Python @('bench/multi_worker.py', '--workers', '3', '--messages', '60')
    Invoke-Python @('bench/http_load.py', '--concurrency', '50', '--reads', '1000')
    # 压测会造执行结果，自然会产生 outbox 消息；跑一次 worker 收尾，
    # 这样"跑完压测"之后库里的汇总和明细也是一致的（不留积压）。
    Write-Note '收尾：把压测产生的执行结果汇总掉'
    Invoke-Python @('scripts/outbox_worker.py', '--drain')
    Write-Ok '十项验收实验完成：档期不超占、任务恰好一人领到、提交恰好一次落库、故障后汇总收敛、对账 100% 检出、缓存脏读可测且能收敛、占用三方案 P99 可比、深分页 keyset 平坦、多 worker 不重复处理、HTTP 层并发语义不变'
    Write-Note '注意：压测会故意注入差错，所以全量对账（dev.cmd recon）会报出这些样本 —— 那是它该做的事'
}

function Invoke-Sched {
    Write-Step '定时任务：幂等键清理 / 每日对账 / 死信巡检'
    Ensure-Postgres
    if ((Get-DbStatus).Code -ne 0) {
        Write-Warn '业务库未初始化，先跑 init'
        Invoke-Init
    }
    if ($Watch) {
        Write-Note '常驻模式：每 60s 检查一次到期任务（Ctrl+C 停止）'
        Invoke-Python @('scripts/scheduler.py')
    }
    else {
        Invoke-Python @('scripts/scheduler.py', '--once')
        Write-Ok '三个任务各跑了一轮（advisory lock 保证多副本只执行一次）'
    }
}

function Invoke-Worker {
    Write-Step 'outbox worker：执行结果 → 日报汇总'
    Ensure-Postgres
    if ((Get-DbStatus).Code -ne 0) {
        Write-Warn '业务库未初始化，先跑 init'
        Invoke-Init
    }
    if ($Watch) {
        Write-Note '常驻模式：每 2s 轮询一次，Ctrl+C 停止'
        Invoke-Python @('scripts/outbox_worker.py', '--interval', '2')
    }
    else {
        Write-Note '单次模式：处理到没有到期消息就退出（想看常驻加 -Watch）'
        Invoke-Python @('scripts/outbox_worker.py', '--drain')
    }
}

function Invoke-Recon {
    Write-Step '日终对账：明细 vs 汇总 vs 失败是否提单'
    Ensure-Postgres
    if ((Get-DbStatus).Code -ne 0) {
        Write-Warn '业务库未初始化，先跑 init'
        Invoke-Init
    }
    Invoke-Python @('scripts/recon.py')
}

function Invoke-Api {
    Write-Step "启动 API：http://127.0.0.1:$Port/docs"
    Ensure-Postgres
    if ((Get-DbStatus).Code -ne 0) {
        Write-Warn '业务库未初始化，先跑 init'
        Invoke-Init
    }
    $python = Get-PythonPath
    $argv = @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$Port")
    if ($Reload) { $argv += '--reload' }
    Write-Note '健康检查 /healthz /readyz /metrics；写接口必须带 Idempotency-Key；Ctrl+C 停止'
    & $python @argv
}

function Show-Docs {
    Write-Step '常用地址与示例请求'
    $base = "http://127.0.0.1:$Port"

    # 请求体放在变量里：PowerShell 单引号字符串内的双引号原样保留，
    # 直接塞进双引号字符串会因为引号配对把 JSON 弄坏（这里踩过一次）。
    $bodyBooking = '{"project_id":1,"vehicle_id":1,"testers_id":1,"slot_date":"2030-01-01","half":0}'
    $bodyDefect = '{"title":"NOA 左转压线","severity":"S1","project_id":1,"actor":"qa"}'
    $bodyClaim = '{"task_id":1,"tester_id":1}'
    $bodyClaimStale = '{"task_id":1,"tester_id":1,"expect_version":0}'
    $idem1 = "@{'Idempotency-Key'='demo-1'}"
    $idem2 = "@{'Idempotency-Key'='demo-2'}"
    $idem3 = "@{'Idempotency-Key'='defect-1'}"
    $idem4 = "@{'Idempotency-Key'='claim-1'}"
    $idem5 = "@{'Idempotency-Key'='claim-stale'}"
    $idem6 = "@{'Idempotency-Key'='exec-1'}"
    $bodyExec = '{"task_id":1,"tester_id":1,"result":"fail","note":"左转压线","defect":{"title":"NOA 左转压线","severity":"S1"}}'

    Write-Host ""
    Write-Host "  文档      $base/docs" -ForegroundColor Gray
    Write-Host "  健康检查  $base/healthz    $base/readyz" -ForegroundColor Gray
    Write-Host "  指标      $base/metrics" -ForegroundColor Gray
    Write-Host "  状态机    $base/v1/defects/status-machine" -ForegroundColor Gray
    Write-Host "  任务池    $base/v1/queue?status=pending" -ForegroundColor Gray
    Write-Host ""
    Write-Host '  # 1) 预约档期（同 key 重放 -> replay=true，不会重复占）' -ForegroundColor Gray
    Write-Host "  Invoke-RestMethod -Method Post '$base/v1/bookings' -Headers $idem1 -ContentType 'application/json' -Body '$bodyBooking'" -ForegroundColor Gray
    Write-Host ""
    Write-Host '  # 2) 换 key 抢同一档期 -> 409 slot_not_available' -ForegroundColor Gray
    Write-Host "  Invoke-RestMethod -Method Post '$base/v1/bookings' -Headers $idem2 -ContentType 'application/json' -Body '$bodyBooking'" -ForegroundColor Gray
    Write-Host ""
    Write-Host '  # 3) 提问题单，再试 open -> closed（应 409 illegal_transition）' -ForegroundColor Gray
    Write-Host "  Invoke-RestMethod -Method Post '$base/v1/defects' -Headers $idem3 -ContentType 'application/json' -Body '$bodyDefect'" -ForegroundColor Gray
    Write-Host ""
    Write-Host '  # 4) 领取任务（第二次同 key 重放；换人抢则 409 already_claimed）' -ForegroundColor Gray
    Write-Host "  Invoke-RestMethod -Method Post '$base/v1/queue/claim' -Headers $idem4 -ContentType 'application/json' -Body '$bodyClaim'" -ForegroundColor Gray
    Write-Host ""
    Write-Host '  # 5) 拿旧版本号重领 -> 409 version_conflict（乐观锁防 ABA）' -ForegroundColor Gray
    Write-Host "  Invoke-RestMethod -Method Post '$base/v1/queue/claim' -Headers $idem5 -ContentType 'application/json' -Body '$bodyClaimStale'" -ForegroundColor Gray
    Write-Host ""
    Write-Host '  # 6) 提交执行结果（同事务写 executions + outbox；失败带出问题单）' -ForegroundColor Gray
    Write-Host "  Invoke-RestMethod -Method Post '$base/v1/executions' -Headers $idem6 -ContentType 'application/json' -Body '$bodyExec'" -ForegroundColor Gray
    Write-Host ""
    Write-Note '演示数据已灌好：project_id / vehicle_id / testers_id 用 1 即可；任务池里已有 pending 任务'
    Write-Note '提交结果前必须先 claim 那条 task（body 里的 task_id 要换成你领到的那条）'
    Write-Host ""
    Write-Host '  # 0) 先换 token（业务接口默认要 Bearer；演示账号见 dev.cmd token）' -ForegroundColor Gray
    Write-Host "  `$token = (Invoke-RestMethod -Method Post '$base/v1/auth/token' -ContentType 'application/json' -Body '{`"username`":`"admin`",`"password`":`"Admin@12345`"}').access_token" -ForegroundColor Gray
    Write-Host "  `$auth = @{ Authorization = `"Bearer `$token`" }   # 下面每个请求都和 Idempotency-Key 合并使用" -ForegroundColor Gray
}

function Invoke-Token {
    Write-Step '签发一个 JWT（演示/联调用）'
    $env:PYTHONUTF8 = '1'
    $python = Get-PythonPath
    & $python -c @"
import sys
sys.path.insert(0, '.')
from app.infra import auth
from app.service import users

username, password = sys.argv[1], sys.argv[2]
try:
    user = users.authenticate(username, password)
except Exception as exc:
    print(f'登录失败：{exc}')
    raise SystemExit(1)
token, ttl = auth.issue_token(user.username, user.role)
print(token)
print(f'# username={user.username} role={user.role} ttl={ttl}s', file=sys.stderr)
"@ $User $Password
    if ($LASTEXITCODE -ne 0) { throw '签发失败：先用 dev.cmd init --seed-demo 灌演示账号' }
    Write-Note "用法：`$token = (上面那行)；然后请求头加 @{ Authorization = `"Bearer `$token`" }"
    Write-Note '演示账号：admin/Admin@12345（可写可管理）、tester/Tester@12345（可写）、viewer/Viewer@12345（只读）'
}

# ====================== 主流程 ======================
$exitCode = 0
try {
    switch ($Action) {
        'check' { Invoke-Check }
        'init' { Invoke-Init }
        'test' { Invoke-Test }
        'smoke' { Invoke-Smoke }
        'worker' { Invoke-Worker }
        'recon' { Invoke-Recon }
        'bench' { Invoke-Bench }
        'api' { Invoke-Api }
        'docs' { Show-Docs }
        'token' { Invoke-Token }
        'sched' { Invoke-Sched }
        'all' {
            Invoke-Check
            Invoke-Init
            Invoke-Test
            Invoke-Smoke
            Write-Step '完成'
            Write-Ok '第 2 周进度：档期占用 + 问题单状态机 + 任务领取 + 执行结果/outbox + 一键脚本'
            Write-Note '下一步：.\dev.ps1 api    然后打开 http://127.0.0.1:8000/docs'
            Write-Note '跑 worker：.\dev.ps1 worker   （把积压的执行结果汇总成日报）'
            Write-Note '验收实验：.\dev.ps1 bench    （四项并发/故障实验）'
        }
    }
}
catch {
    Write-Fail $_.Exception.Message
    $exitCode = 1
}

exit $exitCode
