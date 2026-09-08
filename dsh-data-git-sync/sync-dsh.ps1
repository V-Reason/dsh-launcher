#Requires -Version 5.1
<#
.SYNOPSIS
  DSH 数据同步（原生 Git 方案）：在 DSH 进程之外，用原生 git 命令同步 $DSH_HOME 数据。

.DESCRIPTION
  本脚本替代已废弃的 dsh-data-sync 插件：直接在终端运行 git，不经过 DSH 会话，
  因此不会产生「同步命令本身写入 sessions/」的自指残差，也不依赖 Web UI。

  同步范围（allowlist，相对 $DSH_HOME）：
    .gitignore  sessions/  profiles/web/  storages/  attachments/  memories/
    settings.yaml  .agent-presets/  cordis.patch.yml
  缺失的路径自动跳过；排除规则见 $DSH_HOME/.gitignore（随仓库同步，两端一致）。

  退出码（供 vdsh --sync 等调用方分支判断）：
    0 成功（含「无变更可推送」）   1 硬失败（git 命令失败等）
    2 用法错误                   3 被阻塞（脏工作区 / 冲突中间态 / 远端 main 未建立）
    4 未初始化（无仓库 / 无 origin / 数据目录不存在，可跳过）

  输出风格（步骤行在直接终端与经 vdsh 调用时都可见）：
    步骤 → 动作 ｜ 成功 ✓ 结果 ｜ 错误 ✗ 原因 ｜ 警告 ⚠ 说明
    push 反映推送内容（提交/文件数），pull 反映拉取内容（远端新增提交/文件数）。
    status 的「待推送」按行列出（最多 10 条，其余显示截断提示）；
    fetch/push 以 --progress 执行，经 vdsh（stdout 被捕获）时逐行流式显示实时进度。

.PARAMETER CommandArgs
  第一个参数为子命令（push / pull / status / init / remote / help），其余为参数：
    init [远程URL]   初始化数据仓库：git init、添加 origin、生成 .gitignore、fetch
                      （成功后把远端地址写入 vdsh.yaml 的 sync.remote）
    push             暂存 allowlist 变更 → 提交（DSH Sync 身份）→ 推送
    pull             先尝试快进，必要时常规合并；冲突/脏工作区给出指引
    status           查看 vs 远端的前后差异、待推送文件、最近提交
    remote [set URL] 查看或设置远端仓库位置（同步写入 vdsh.yaml 的 sync.remote）
    help             显示用法

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File sync-dsh.ps1 status
  powershell -ExecutionPolicy Bypass -File sync-dsh.ps1 push
  powershell -ExecutionPolicy Bypass -File sync-dsh.ps1 init file:///Z:/DataBase/dsh-sync-repo.git
#>

param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CommandArgs
)

# 使用 Continue：git 的 stderr（如 autocrlf 提示）在 PowerShell 5.1 下经 2>&1 会变成
# ErrorRecord；Stop 会把这些非致命提示当作终止错误。显式 throw 仍由底部 catch 处理。
$ErrorActionPreference = 'Continue'

# 直接把控制台输出切到 UTF-8：不经 .cmd 的 chcp、从任意控制台直接 `powershell -File` 调用时
# 中文与 git 输出不乱码。环境拒绝（如重定向/旧控制台）时静默忽略，不影响功能。
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
} catch {
    # 忽略：控制台编码不可改时维持默认。
}

# ---------------------------------------------------------------- 基础配置

# $DSH_HOME：优先环境变量，缺省 ~/.dsh（与 DSH 默认一致）。
# 经 vdsh 调用时，启动器按 vdsh.yaml 的 sync.data_dir 注入 DSH_HOME（env 仍优先）。
$DshHome = if ($env:DSH_HOME) { $env:DSH_HOME } else { Join-Path $HOME '.dsh' }
# 兼容 ~ 写法（如 ~/.dsh）：PS 5.1 下字符串拼接/传给 git 时不会自动展开波浪号。
if ($DshHome.StartsWith('~')) {
    $DshHome = Join-Path $HOME $DshHome.Substring(1).TrimStart('\', '/')
}

# 同步 allowlist（相对 $DSH_HOME）。.gitignore 随同步，保证两端排除规则一致。
# 经 vdsh 调用时可被 vdsh.yaml 的 sync.allowlist 覆盖（JSON 数组环境变量）。
$Allowlist = @('.gitignore', 'sessions', 'profiles/web', 'storages', 'attachments', 'memories', 'settings.yaml', '.agent-presets', 'cordis.patch.yml')
if ($env:VDG_SYNC_ALLOWLIST) {
    $parsed = @($env:VDG_SYNC_ALLOWLIST | ConvertFrom-Json)
    # PS 5.1 的 ConvertFrom-Json 对顶层数组输出单个 Object[]（不拆管道），
    # 而 PS 7 会拆开；统一展平一层，保证两版行为一致。
    if ($parsed.Count -eq 1 -and $parsed[0] -is [array]) {
        $parsed = @($parsed[0])
    }
    if ($parsed.Count -gt 0) { $Allowlist = $parsed }
}

# 提交者身份：两端必须一致（-c 注入，不改仓库配置）；可被 vdsh.yaml 覆盖。
$CommitName = if ($env:VDG_SYNC_COMMIT_NAME) { $env:VDG_SYNC_COMMIT_NAME } else { 'DSH Sync' }
$CommitEmail = if ($env:VDG_SYNC_COMMIT_EMAIL) { $env:VDG_SYNC_COMMIT_EMAIL } else { 'dsh-sync@local' }

# .gitignore 追加内容（vdsh.yaml 的 sync.gitignore_extra；空 = 不追加）。
$GitIgnoreExtra = if ($env:VDG_SYNC_GITIGNORE_EXTRA) { $env:VDG_SYNC_GITIGNORE_EXTRA } else { '' }

# 内置必备排除规则（与 GitIgnoreExtra 无关，始终保证存在）。
# .dsh-module-fallback：dsh 的模块回退目录（本机链接/proxy，可再生缓存），
# Windows git 会把 junction 展开成真实内容入库，副机检出后 dsh 启动报错。
$BuiltinIgnoreRules = @('profiles/*/.dsh-module-fallback/')

# 模块回退目录（相对 $DshHome）——见上面的 BuiltinIgnoreRules 说明。
$ModuleFallbackPath = 'profiles/web/.dsh-module-fallback'

# 确保 .gitignore 中存在内置必备排除规则（幂等；.gitignore 缺失时不给初值，由 init 生成）。
function Ensure-BuiltinIgnoreRules {
    $gitignorePath = Join-Path $DshHome '.gitignore'
    if (-not (Test-Path $gitignorePath)) { return }
    $existing = [System.IO.File]::ReadAllText($gitignorePath, [System.Text.UTF8Encoding]::new($false))
    $missing = @($BuiltinIgnoreRules | Where-Object { $existing -notmatch [regex]::Escape($_) })
    if ($missing.Count -gt 0) {
        $block = "`n# dsh 模块回退目录不入库（内置规则）：Windows git 会把 junction 展开入库，副机检出后 dsh 启动报错。"
        [System.IO.File]::AppendAllText(
            $gitignorePath,
            $block + "`n" + ($missing -join "`n") + "`n",
            [System.Text.UTF8Encoding]::new($false))
        Write-Step ('补写 .gitignore 内置排除规则: {0}' -f ($missing -join ', '))
    }
}

# 模块回退目录是否仍被 git 跟踪（junction 展开入库的历史残留）。
function Test-ModuleFallbackTracked {
    $files = @(& git -C $DshHome ls-files -- $ModuleFallbackPath 2>$null)
    return $files.Count -gt 0
}

# 把历史入库的模块回退目录移出版本库（仅索引，不动工作区；dsh 会自动重建）。
# 返回 $true 表示有改动需要提交；无跟踪/无变化返回 $false。
function Repair-ModuleFallbackTracking {
    if (-not (Test-ModuleFallbackTracked)) { return $false }
    Ensure-BuiltinIgnoreRules
    Write-Host ("⚠ 发现 .dsh-module-fallback 已被（误）跟踪（{0}，Windows git 把 junction 展开入库）；" -f $ModuleFallbackPath)
    Write-Host '  正在把它移出版本库（工作区文件保留；本次提交会把删除记录同步，副机拉取后自动清理并由 dsh 重建）…'
    if ((& git -C $DshHome rm -r --cached --quiet -- $ModuleFallbackPath 2>$null) -ne 0) {
        Write-Host '   ⚠ 移出索引失败，请手动执行:'
        Write-Host "     git -C $DshHome rm -r --cached -- $ModuleFallbackPath"
        return $true
    }
    return $true
}

# 单次 git 操作超时（秒；vdsh.yaml 的 sync.timeout_seconds，0 = 不限时）。
$TimeoutSeconds = 0
if ($env:VDG_SYNC_TIMEOUT) {
    $parsedTimeout = 0
    if ([double]::TryParse($env:VDG_SYNC_TIMEOUT, [ref]$parsedTimeout) -and $parsedTimeout -gt 0) {
        $TimeoutSeconds = [double]$parsedTimeout
    }
}

# ---------------------------------------------------------------- 工具函数

function Write-Step {
    <#
    .SYNOPSIS
        步骤行（→ 动作）：与 Invoke-DshGit 的 -Message 不同，此说明在「直接终端」
        与「经 vdsh（stdout 被捕获）」两种运行方式下都可见——阶段进度不能只藏在
        PS 层动画的 -Message 里（vdsh 调用时该动画按设计静默）。
    #>
    param([string]$Message)
    Write-Host "→ $Message"
}

function Write-Ok {
    <# 成功汇总行（✓ 结果）。#>
    param([string]$Message)
    Write-Host "✓ $Message"
}

function Write-Err {
    <# 错误行（✗ 原因）。#>
    param([string]$Message)
    Write-Host "✗ $Message"
}

function Get-ShortDir {
    <#
    .SYNOPSIS
        数据目录短形式：位于 $HOME 下时显示为 ~\…（跨机操作时仍能认出是哪台机器，
        又不占满一行）；其余路径原样返回。
    #>
    param([string]$Path)
    $homeDir = $HOME.TrimEnd('\')
    if ($Path -and $Path.StartsWith($homeDir + '\')) {
        return '~' + $Path.Substring($homeDir.Length)
    }
    return $Path
}

function Invoke-DshGit {
    <#
    .SYNOPSIS
        在 $DshHome 仓库上执行 git：输出（含 stderr）以纯文本透传到终端，
        函数只返回退出码（唯一管道输出），避免 PowerShell 5.1 把 git stderr
        渲染成 ErrorRecord 噪音。
    .DESCRIPTION
        -Animated：git 在独立 runspace 中执行，主线程重绘「转轮 + 消息 + 秒数」
        进度行（与 vdsh 启动同风格）；仅当 stdout 未重定向（真实交互终端）时启用，
        防止与上层（vdsh_launcher 的捕获 + 动画）叠加。默认消息可经 -Message 覆盖。
    #>
    param(
        [string[]]$GitArgs,
        [switch]$Animated,
        [string]$Message = '同步中…'
    )
    if ($Animated -and -not [Console]::IsOutputRedirected) {
        return Invoke-GitSpinner -GitArgs $GitArgs -Message $Message
    }
    # stdout 被捕获（如经 vdsh / 重定向）时：逐行流式透传，不缓冲到结束——
    # 长操作（fetch/push 已加 --progress）的实时进度在两种调用方式下都可见。
    & git -C $DshHome @GitArgs 2>&1 | ForEach-Object { Write-Host ("$_") }
    return $LASTEXITCODE
}

function Invoke-GitSpinner {
    <#
    .SYNOPSIS
        带动画执行 git：在独立 runspace 会话态中运行 git，主线程循环重绘
        「⠋ 消息 秒数」进度行（与 vdsh 启动动画同风格），结束后一次性显示
        git 输出并返回其退出码。
    .DESCRIPTION
        仅在真实交互终端被调用（Invoke-DshGit 已检查 IsOutputRedirected）；
        git 输出在 runspace 内被收集，动画行结束才统一打印，互不干扰。
    #>
    param(
        [Parameter(Mandatory = $true)][string[]]$GitArgs,
        [Parameter(Mandatory = $true)][string]$Message
    )

    $frames = if ($env:VDG_ANIMATION_FRAMES) { $env:VDG_ANIMATION_FRAMES } else { '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏' }
    $tickMs = 120
    if ($env:VDG_ANIMATION_FPS) {
        $parsedFps = 0
        if ([double]::TryParse($env:VDG_ANIMATION_FPS, [ref]$parsedFps) -and $parsedFps -gt 0) {
            $tickMs = [int](1000 / $parsedFps)
        }
    }
    $runspace = [runspacefactory]::CreateRunspace()
    $runspace.Open()
    $ps = [powershell]::Create()
    $ps.Runspace = $runspace
    # git 在独立会话态执行：stderr 并入 stdout（2>&1）转纯文本，结果与退出码打包返回。
    $body = 'param($homeDir, [string[]]$gitArgs) ' +
            '$o = & git -C $homeDir @gitArgs 2>&1 | ForEach-Object { "$_" }; ' +
            '[pscustomobject]@{ Out = ($o -join "`n"); Code = $LASTEXITCODE }'
    $null = $ps.AddScript($body).AddArgument($DshHome).AddArgument([string[]]$GitArgs)

    $async = $ps.BeginInvoke()
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $i = 0
    try {
        while (-not $async.IsCompleted) {
            if ($TimeoutSeconds -gt 0 -and $sw.Elapsed.TotalSeconds -gt $TimeoutSeconds) {
                try { $ps.Stop() } catch { }
                Write-Host ("`r" + (' ' * 80) + "`r") -NoNewline
                throw ('同步超时：已超过 {0:N0} 秒仍无结果，已终止（远端可能不可达）。' -f $TimeoutSeconds)
            }
            $elapsed = [int]$sw.Elapsed.TotalSeconds
            Write-Host ("`r{0} {1} {2}s" -f $frames[$i % $frames.Length], $Message, $elapsed) -NoNewline
            $i++
            Start-Sleep -Milliseconds $tickMs
        }
        Write-Host ("`r" + (' ' * 80) + "`r") -NoNewline
        $result = @($ps.EndInvoke($async))
    } finally {
        try { $ps.Stop() } catch { }
        $ps.Dispose()
        $runspace.Dispose()
    }
    if ($result.Count -gt 0 -and $result[0].Out) { Write-Host $result[0].Out }
    if ($result.Count -gt 0 -and $null -ne $result[0].Code) { return [int]$result[0].Code }
    return 1
}

function Wait-GitQuiet {
    <#
    .SYNOPSIS
        静默执行 git（不输出），返回退出码。
    #>
    param([string[]]$GitArgs)
    & git -C $DshHome @GitArgs *> $null
    return $LASTEXITCODE
}

function Test-IsRepo {
    return (Wait-GitQuiet @('rev-parse', '--git-dir')) -eq 0
}

function Test-RemoteMain {
    return (Wait-GitQuiet @('rev-parse', '--verify', '--quiet', 'origin/main')) -eq 0
}

function Get-Origin {
    $out = & git -C $DshHome remote get-url origin 2>$null
    if ($LASTEXITCODE -eq 0 -and $out) { return ($out | Select-Object -First 1) }
    return $null
}

function Get-ExistingAllowlist {
    <#
    .SYNOPSIS
        返回 allowlist 中实际存在的路径（git add 对不存在的 pathspec 会报错）。
    #>
    return @($Allowlist | Where-Object { Test-Path (Join-Path $DshHome $_) })
}

function Get-DirtyAllowlist {
    <#
    .SYNOPSIS
        返回 allowlist 内的脏条目（porcelain -z 原文，含未跟踪/已删除）。空数组 = 干净。
    #>
    $paths = Get-ExistingAllowlist
    if ($paths.Count -eq 0) { return @() }
    $out = & git -C $DshHome status --porcelain -z -- $paths 2>$null
    if ($null -eq $out -or $out.Count -eq 0) { return @() }
    # -z 输出为 NUL 分隔的整串；PowerShell 按行捕获时合并为单条记录，按 NUL 拆回。
    $joined = ($out | ForEach-Object { $_ }) -join ''
    return @($joined -split "`0" | Where-Object { $_.Length -gt 0 })
}

function Show-Usage {
    Write-Host '用法: sync-dsh.ps1 <init [远程URL] | push | pull | status | remote [set <URL>] | help>'
    Write-Host '      sync-dsh.cmd  （双击 = 菜单）'
    Write-Host '      vdsh sync <上面任意子命令>  （安装 dsh-launcher 后）'
}

# ---------------------------------------------------------------- 子命令

function Sync-Init {
    <#
    .SYNOPSIS
        一次性初始化：git init、添加 origin、生成 .gitignore、fetch。
        从零副机 / 已有数据副机的后续步骤见 doc/native-git-sync.md。
        返回退出码（0 = 完成或待重试，2 = 缺少远程地址，1 = git 错误）。
    #>
    param([string]$RemoteUrl)

    Write-Step ('初始化数据仓库（{0}）' -f (Get-ShortDir $DshHome))

    # 全新机器：目录可能尚不存在（DSH 首次运行才创建），init 负责引导创建。
    if (-not (Test-Path $DshHome)) {
        Write-Step '数据目录不存在，正在创建…'
        New-Item -ItemType Directory -Force -Path $DshHome | Out-Null
    }

    if (-not (Test-IsRepo)) {
        Write-Step '初始化 git 仓库（main）…'
        if ((Invoke-DshGit @('init', '-b', 'main')) -ne 0) { throw 'git init 失败' }
    } else {
        Write-Step 'git 仓库已就绪（main）'
    }

    $origin = Get-Origin
    if (-not $origin) {
        if (-not $RemoteUrl) {
            Write-Err '缺少远程地址。用法: sync-dsh.ps1 init <远程URL>（如 file:///Z:/DataBase/dsh-sync-repo.git）'
            return 2
        }
        Write-Step ('添加远端 origin: {0}' -f $RemoteUrl)
        if ((Invoke-DshGit @('remote', 'add', 'origin', $RemoteUrl)) -ne 0) { throw 'git remote add 失败' }
    } else {
        if ($RemoteUrl -and $RemoteUrl -ne $origin) {
            Write-Step ('更换 origin: {0} → {1}' -f $origin, $RemoteUrl)
            if ((Invoke-DshGit @('remote', 'set-url', 'origin', $RemoteUrl)) -ne 0) { throw 'git remote set-url 失败' }
        } else {
            Write-Step ('远端 origin: {0}' -f $origin)
        }
    }

    # 远端地址同步持久化到 vdsh.yaml 的 sync.remote（与 `remote set` 一致）：
    # 传 URL 用 URL；未传且已存在 origin 时回填 git 里的地址（自愈旧 init 未持久化的情况）。
    $effectiveUrl = if ($RemoteUrl) { $RemoteUrl } else { (Get-Origin) }
    if ($effectiveUrl) {
        $persisted = Set-VdgConfigRemote $effectiveUrl
        Write-Host ("→ 远端记录: {0}（{1}）" -f $effectiveUrl, $(if ($persisted) {
                '已写入 vdsh.yaml 的 sync.remote'
            } else {
                '未发现 vdsh.yaml，仅更新 git origin；经 vdsh 调用时自动持久化'
            }))
    }

    if (-not (Test-Path (Join-Path $DshHome '.gitignore'))) {
        Write-Step '生成 .gitignore（排除规则，会随仓库同步）'
        # 用 WriteAllText 写 UTF-8 无 BOM：PS 5.1 的 Set-Content -Encoding UTF8 会带 BOM，
        # git 对 .gitignore 首行的裸 BOM 处理不可靠（首行注释可能失效）。
        $gitignoreContent = @'
# DSH 数据同步：永不提交的内容（原生 Git 方案）
.credentials.yaml
*.log
logs/
*.lock
.dsh-data-sync/
llm-*/
profiles/node_modules/
# profile 依赖的 node_modules 不入库：两端各自 pnpm install（版本由 pnpm-lock.yaml 锁定）
profiles/web/node_modules/
# dsh 模块回退目录不入库：Windows git 会把 junction/链接展开成真实内容入库，
# 副机检出后 dsh 启动报「exists and is not a symlink or dsh-managed module proxy」。
# 该目录由 dsh 按本机安装自动重建（链接/proxy），不是需要同步的数据。
profiles/*/.dsh-module-fallback/
'@
        if ($GitIgnoreExtra) {
            $gitignoreContent = $gitignoreContent.TrimEnd() + "`n" + $GitIgnoreExtra.TrimEnd() + "`n"
        }
        [System.IO.File]::WriteAllText(
            (Join-Path $DshHome '.gitignore'),
            $gitignoreContent,
            [System.Text.UTF8Encoding]::new($false))
    } elseif ($GitIgnoreExtra) {
        # 已存在的 .gitignore：按行幂等补写缺失的额外规则（不覆盖用户已有内容）。
        $gitignorePath = Join-Path $DshHome '.gitignore'
        $existing = [System.IO.File]::ReadAllText($gitignorePath, [System.Text.UTF8Encoding]::new($false))
        $missingLines = @($GitIgnoreExtra -split "`r?`n" |
            ForEach-Object { $_.Trim() } |
            Where-Object { $_ -and -not $existing.Contains($_) })
        if ($missingLines.Count -gt 0) {
            [System.IO.File]::AppendAllText(
                $gitignorePath,
                "`n" + ($missingLines -join "`n") + "`n",
                [System.Text.UTF8Encoding]::new($false))
            Write-Step '已按 vdsh.yaml 的 sync.gitignore_extra 补写 .gitignore 缺失行'
        }
    }
    # 内置必备排除规则（无论 .gitignore 新建还是已存在，都幂等确保存在）。
    Ensure-BuiltinIgnoreRules

    # 两端 core.autocrlf 必须一致（默认均 true 即可；若改，两台一起改）。
    $crlf = & git -C $DshHome config core.autocrlf 2>$null
    if (-not $crlf) { $crlf = '(未设置, 默认 true)' }
    Write-Step ('core.autocrlf = {0}（两端保持一致）' -f $crlf)

    # 配置概况（供核对 vdsh.yaml 生效值）。
    $originNow = Get-Origin
    Write-Step ('配置概况: 远端={0} | 同步范围={1} 项 | .gitignore={2}' -f (
        $(if ($originNow) { $originNow } else { '(未设置)' })),
        $Allowlist.Count,
        $(if (Test-Path (Join-Path $DshHome '.gitignore')) { '已就绪' } else { '缺失（应已在此步生成）' }))

    Write-Step '获取远端数据…'
    if ((Invoke-DshGit @('fetch', '--progress', 'origin') -Animated -Message '获取远端数据…') -ne 0) {
        Write-Host '→ 注意: origin 暂不可达（首次初始化可稍后再试）'
        return 0
    }

    if (-not (Test-RemoteMain)) {
        Write-Host '→ 远端还没有 main 分支：先在主力机执行一次 sync-dsh.ps1 push 建立，或对空裸仓库执行 git push -u origin main。'
        return 0
    }
    $hasHead = (Wait-GitQuiet @('rev-parse', '--verify', '--quiet', 'HEAD')) -eq 0
    if (-not $hasHead) {
        # 本机无提交且远端已有 main：判断目录是否「全新」（只含 init 刚生成的 .gitignore）。
        # 是——用 -f 覆盖后 checkout（目录无其它数据，安全）；否——存在已有 DSH 数据，走 merge/reset。
        $untracked = & git -C $DshHome status --porcelain --untracked-files=all 2>$null
        $onlyGenerated = ($untracked | Where-Object { $_.Trim() -ne '' -and ($_.Trim() -notmatch '^\?\?\s+\.gitignore$') } | Measure-Object).Count -eq 0
        if ($onlyGenerated) {
            Write-Step '数据目录为全新，自动从远端填充数据…'
            if ((Invoke-DshGit @('checkout', '-f', '-b', 'main', 'origin/main')) -ne 0) {
                Write-Host '   自动切换失败，请手动执行（-f 会用仓库版本覆盖 init 生成的 .gitignore，目录无其它数据，安全）:'
                Write-Host "    cd $DshHome; git checkout -f -b main origin/main"
            } else {
                Write-Ok '完成：远端数据已填入本机，可执行 status 查看。'
            }
        } else {
            Write-Host '→ 本机尚无提交但目录里已有 DSH 数据（尚未入库），二选一:'
            Write-Host "    git merge origin/main                          # 把远端历史并入本地数据"
            Write-Host "    或 git reset --soft origin/main                # 以远端为基线，本地数据作为未提交变更，随后 push"
        }
    } elseif ((Get-DirtyAllowlist).Count -eq 0) {
        Write-Step '本地已就绪；查看状态: sync-dsh.ps1 status'
    } else {
        Write-Host '→ 本机已有 DSH 数据且远端也有历史，请选择:'
        Write-Host "    git merge origin/main                          # 把远端历史并入本地数据"
        Write-Host "    或 git reset --soft origin/main                # 以远端为基线，本地数据作为未提交变更，随后 push"
    }
    return 0
}

function Sync-Push {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    Write-Step ('推送 DSH 数据（{0}）' -f (Get-ShortDir $DshHome))
    if (-not (Test-IsRepo)) {
        Write-Err '尚未初始化。先用: sync-dsh.ps1 init <远程URL>'
        return 4
    }
    if (-not (Get-Origin)) {
        Write-Err '未配置 origin。用: sync-dsh.ps1 init <远程URL>'
        return 4
    }

    $paths = Get-ExistingAllowlist
    if ($paths.Count -eq 0) {
        throw '同步清单中没有任何存在的路径（检查 $DSH_HOME 下的 sessions/ 等目录）'
    }
    # 卫生修复：模块回退目录曾被（误）跟踪时移出版本库（仅索引，不动工作区文件）。
    # 必须排在 git add 之前：.gitignore 补写 → rm --cached → 本次提交含删除记录。
    $repairedFallback = Repair-ModuleFallbackTracking
    Write-Step '暂存变更…'
    $addArgs = @('add', '-A', '--') + $paths
    if ((Invoke-DshGit $addArgs) -ne 0) { throw 'git add 失败' }

    $staged = @(& git -C $DshHome diff --cached --name-only -- $paths)
    if ($staged.Count -eq 0) {
        Write-Ok '无变更可推送（工作区与远端一致）。'
        return 0
    }

    $message = 'sync: {0} ({1} 个文件)' -f (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss'), $staged.Count
    Write-Step ('提交 {0} 个文件（{1}）' -f $staged.Count, $message)
    $commitArgs = @('-c', "user.name=$CommitName", '-c', "user.email=$CommitEmail", 'commit', '-m', $message)
    if ((Invoke-DshGit $commitArgs) -ne 0) { throw 'git commit 失败' }

    Write-Step '推送 origin/main…'
    if ((Invoke-DshGit @('push', '--progress') -Animated -Message '推送中…') -ne 0) {
        # 上游未建立（全新远端分支）：带 -u 再试
        if ((Invoke-DshGit @('push', '-u', 'origin', 'main', '--progress') -Animated -Message '推送中…') -ne 0) {
            Write-Host '   ⚠ 若错误为「non-fast-forward / 远端有更新」：另一台机器已推送过，请先执行 sync-dsh.ps1 pull 合并后再 push。'
            throw 'git push 失败：请确认远端可达（共享盘/内网穿透已挂载）'
        }
    }
    Write-Ok ('完成（{0:N1}s）' -f $sw.Elapsed.TotalSeconds)
    return 0
}

function Sync-Pull {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    Write-Step ('拉取 DSH 数据（{0}）' -f (Get-ShortDir $DshHome))
    if (-not (Test-IsRepo)) {
        Write-Err '尚未初始化。先用: sync-dsh.ps1 init <远程URL>'
        return 4
    }
    if (-not (Get-Origin)) {
        Write-Err '未配置 origin。用: sync-dsh.ps1 init <远程URL>'
        return 4
    }

    $dirty = Get-DirtyAllowlist
    if ($dirty.Count -gt 0) {
        # 与 status 同款逐行列出（最多 10 条），一眼看出待处理内容。
        Write-Host ("⚠ 工作区有 {0} 个未提交变更，先执行 push，再 pull:" -f $dirty.Count)
        $shown = [Math]::Min($dirty.Count, 10)
        foreach ($entry in ($dirty | Select-Object -First $shown)) {
            Write-Host ('  - {0}' -f $entry.Substring(3))
        }
        if ($dirty.Count -gt $shown) {
            Write-Host ('  … 以及另外 {0} 个（完整明细: git -C {1} status --short）' -f ($dirty.Count - $shown), $DshHome)
        }
        # 针对性指引：变更集中在 .dsh-module-fallback（junction 展开/本机链接与 HEAD 不一致）时，
        # 该目录是 dsh 可再生缓存，整体删除后 pull 最干净（dsh 启动时自动重建）。
        $fallbackDirty = @($dirty | Where-Object { $_.Substring(3) -match '\.dsh-module-fallback' })
        if ($fallbackDirty.Count -gt 0) {
            Write-Host ''
            Write-Host ('  → 提示: 变更集中在 .dsh-module-fallback（模块回退缓存）。该目录由 dsh 自动重建，' +
                '可整体删除后再 pull:')
            Write-Host ("    Remove-Item -Recurse -Force '$DshHome\profiles\web\.dsh-module-fallback'")
            Write-Host '    删除后执行 vdsh 启动，dsh 会重建模块回退缓存。'
        }
        return 3
    }

    Write-Step '获取远端更新…'
    if ((Invoke-DshGit @('fetch', '--progress', 'origin') -Animated -Message '获取远端更新…') -ne 0) {
        throw '获取远端失败：请确认远端可达（共享盘/内网穿透已挂载）'
    }
    if (-not (Test-RemoteMain)) {
        Write-Err '远端 origin/main 不存在：请先在主力机执行一次 push。'
        return 3
    }

    # 本地无提交（全新副机）：不比对差异，直接给指引。
    if ((Wait-GitQuiet @('rev-parse', '--verify', '--quiet', 'HEAD')) -ne 0) {
        Write-Step '本地还没有提交；全新副机执行:'
        Write-Host "    cd $DshHome; git checkout -b main origin/main"
        return 3
    }

    # 远端相对本地的差异（提交数/文件数）→ 进度说明（与 push 的「推送内容」对称）。
    $statsKnown = $false
    $ahead = 0; $behind = 0; $fileCount = 0
    $aheadBehind = (& git -C $DshHome rev-list '--left-right' '--count' 'HEAD...origin/main' 2>$null) | Select-Object -First 1
    if ($aheadBehind -and (($aheadBehind.Trim()) -match '^(\d+)\s+(\d+)$')) {
        $ahead = [int]$Matches[1]
        $behind = [int]$Matches[2]
        $statsKnown = $true
    }

    if ($statsKnown -and $behind -eq 0) {
        # 已是最新：跳过 merge（避免 git 的「Already up to date.」噪音）。
        if ($ahead -gt 0) {
            Write-Ok ("已是最新（本地领先远端 {0} 提交，勿忘 push）。" -f $ahead)
        } else {
            Write-Ok '已是最新（与远端一致）。'
        }
        Write-Host ("  耗时 {0:N1}s" -f $sw.Elapsed.TotalSeconds)
        return 0
    }
    if ($statsKnown) {
        $fileCount = @(& git -C $DshHome diff --name-only 'HEAD...origin/main' 2>$null |
            Where-Object { $_.Trim() -ne '' }).Count
        Write-Step ('远端新增 {0} 提交 · {1} 个文件' -f $behind, $fileCount)
    } else {
        Write-Step '远端有更新，开始合并…'
    }

    # 本地与远端都有提交：常规合并（先于快进尝试，避免先失败再回退的两段噪音）。
    if ($statsKnown -and $ahead -gt 0) {
        Write-Step '本地与远端都有新提交，常规合并…'
        $message = 'sync: merge {0}' -f (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss')
        if ((Invoke-DshGit @('merge', '-m', $message, 'origin/main') -Animated -Message '合并远端历史…') -ne 0) {
            Write-Host '⚠ 拉取产生冲突（合并未提交，仓库处于冲突中间态）。'
            Write-Host '   回滚: git merge --abort；解决: 处理冲突后 git add + git commit。'
            Write-Host '   详见 dsh-data-git-sync/docs/native-git-sync.md「冲突处理」。'
            return 3
        }
        Write-Ok ('拉取完成（合并 · {0} 提交 · {1} 文件 · {2:N1}s）' -f $behind, $fileCount, $sw.Elapsed.TotalSeconds)
        return 0
    }

    # 常态（一端交替使用、无分叉）：纯快进。
    Write-Step '快进合并…'
    if ((Invoke-DshGit @('merge', '--ff-only', 'origin/main') -Animated -Message '快进合并…') -eq 0) {
        if ($statsKnown) {
            Write-Ok ('拉取完成（快进 · {0} 提交 · {1} 文件 · {2:N1}s）' -f $behind, $fileCount, $sw.Elapsed.TotalSeconds)
        } else {
            Write-Ok ('拉取完成（快进 · {0:N1}s）' -f $sw.Elapsed.TotalSeconds)
        }
        return 0
    }

    # 快进失败（历史无关/极端状态）：再试常规合并。
    Write-Step '快进不可用，尝试常规合并…'
    $message = 'sync: merge {0}' -f (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss')
    if ((Invoke-DshGit @('merge', '-m', $message, 'origin/main') -Animated -Message '合并远端历史…') -eq 0) {
        Write-Ok ('拉取完成（合并 · {0:N1}s）' -f $sw.Elapsed.TotalSeconds)
        return 0
    }

    Write-Host '⚠ 拉取产生冲突（合并未提交，仓库处于冲突中间态）。'
    Write-Host '   回滚: git merge --abort；解决: 处理冲突后 git add + git commit。'
    Write-Host '   详见 dsh-data-git-sync/docs/native-git-sync.md「冲突处理」。'
    return 3
}

function Sync-Status {
    Write-Step ('同步状态（{0}）' -f (Get-ShortDir $DshHome))
    if (-not (Test-IsRepo)) {
        Write-Err ('{0} 不是 git 仓库。先用: sync-dsh.ps1 init <远程URL>' -f $DshHome)
        return 4
    }

    $origin = Get-Origin
    $branch = (& git -C $DshHome symbolic-ref --short HEAD 2>$null) | Select-Object -First 1
    Write-Host ('分支: {0}  远端: {1}' -f $(if ($branch) { $branch.Trim() } else { '(尚无提交)' }), $(if ($origin) { $origin } else { '(未配置)' }))

    $tracking = $false
    if ((Wait-GitQuiet @('rev-parse', '--verify', '--quiet', 'HEAD')) -eq 0 -and (Test-RemoteMain)) {
        $lr = (& git -C $DshHome rev-list '--left-right' '--count' 'HEAD...origin/main' 2>$null) | Select-Object -First 1
        if ($lr -and ($lr.Trim() -match '^(\d+)\s+(\d+)$')) {
            Write-Host ("上游: origin/main — 本地领先 {0} 提交 / 落后 {1} 提交" -f $Matches[1], $Matches[2])
            $tracking = $true
        }
    }
    if (-not $tracking) {
        Write-Host '上游: （未建立 origin/main 跟踪；首次 push 会自动建立）'
    }

    $dirty = Get-DirtyAllowlist
    if ($dirty.Count -gt 0) {
        # 逐行列出（最多 10 条）：长列表单行拼接难读，逐行也便于直接复制路径。
        $shown = [Math]::Min($dirty.Count, 10)
        if ($dirty.Count -gt $shown) {
            Write-Host ("待推送: {0} 个变更（显示前 {1} 个）:" -f $dirty.Count, $shown)
        } else {
            Write-Host ('待推送: {0} 个变更:' -f $dirty.Count)
        }
        foreach ($entry in ($dirty | Select-Object -First $shown)) {
            Write-Host ('  - {0}' -f $entry.Substring(3))
        }
        if ($dirty.Count -gt $shown) {
            Write-Host ('  … 以及另外 {0} 个（完整明细: git -C {1} status --short）' -f ($dirty.Count - $shown), $DshHome)
        }
    } else {
        Write-Host '待推送: 无'
    }

    $lastLog = (& git -C $DshHome log -1 --format='%h %s (%ad)' --date=format:'%Y-%m-%d %H:%M' 2>$null) | Select-Object -First 1
    if ($lastLog) { Write-Host "最近提交: $lastLog" }
    return 0
}

# ---------------------------------------------------------------- 远端仓库位置（vdsh.yaml 持久化）

function Get-VdgConfigPath {
    # vdsh.yaml 位于本目录的上一级（dsh-launcher 根）。独立使用本同步包时可能不存在。
    return Join-Path (Split-Path $PSScriptRoot -Parent) 'vdsh.yaml'
}

function Get-VdgConfigRemote {
    <#
    .SYNOPSIS
        读取 vdsh.yaml 中 sync.remote 的值（未设置/文件缺失/解析失败返回 $null）。
    #>
    $path = Get-VdgConfigPath
    if (-not (Test-Path $path)) { return $null }
    $text = [System.IO.File]::ReadAllText($path, [System.Text.UTF8Encoding]::new($false))
    $match = [regex]::Match($text, '(?m)^\s+remote:\s*(.*)$')
    if (-not $match.Success) { return $null }
    $value = $match.Groups[1].Value.Trim()
    # 值后可能带行尾注释（模板行有 `# 说明`）：先剥掉再解析引号，避免误判不一致。
    $value = ($value -replace '\s+#.*$', '').Trim()
    if ($value -eq '') { return $null }
    if ($value.StartsWith('"')) {
        try { return ($value | ConvertFrom-Json) } catch { return $value.Trim('"') }
    }
    return $value
}

function Set-VdgConfigRemote {
    <#
    .SYNOPSIS
        文本级把远端 URL 写入 vdsh.yaml 的 sync.remote（保留注释与其它键），
        值以 ConvertTo-Json 转义成合法 YAML 双引号标量。成功返回 True。
    #>
    param([string]$Url)
    $path = Get-VdgConfigPath
    if (-not (Test-Path $path)) { return $false }
    $escaped = $Url | ConvertTo-Json -Compress
    $text = [System.IO.File]::ReadAllText($path, [System.Text.UTF8Encoding]::new($false))
    $linePattern = '(?m)^(\s+remote:\s*)[^\r\n]*'
    $lineMatch = [regex]::Match($text, $linePattern)
    if ($lineMatch.Success) {
        $text = $text.Substring(0, $lineMatch.Index) +
                $lineMatch.Groups[1].Value + $escaped +
                $text.Substring($lineMatch.Index + $lineMatch.Length)
    } else {
        $syncLine = [regex]::Match($text, '(?m)^sync:\s*$')
        $block = '  remote: ' + $escaped + "`n"
        if ($syncLine.Success) {
            $pos = $syncLine.Index + $syncLine.Length
            $text = $text.Substring(0, $pos) + "`n" + $block + $text.Substring($pos)
        } else {
            $text = $text.TrimEnd() + "`n`nsync:`n" + $block
        }
    }
    [System.IO.File]::WriteAllText($path, $text, [System.Text.UTF8Encoding]::new($false))
    return $true
}

function Sync-Remote {
    <#
    .SYNOPSIS
        查看/设置远端仓库位置。set <URL>：更新 git origin（无 origin 则添加），
        并持久化到 vdsh.yaml 的 sync.remote（文件存在时）。
    #>
    param([string]$Url)
    if ($Url) {
        if (-not (Test-IsRepo)) {
            Write-Host '❌ 尚未初始化。先用: sync-dsh.ps1 init <远程URL>'
            return 4
        }
        $args = if (Get-Origin) { @('remote', 'set-url', 'origin', $Url) } else { @('remote', 'add', 'origin', $Url) }
        if ((Invoke-DshGit $args) -ne 0) { throw 'git remote 更新失败' }
        $persisted = Set-VdgConfigRemote $Url
        Write-Host ("✅ 远端已更新: {0}{1}" -f $Url, $(if ($persisted) {
                '（已写入 vdsh.yaml 的 sync.remote）'
            } else {
                '（未发现 vdsh.yaml，仅更新 git origin；经 vdsh 调用时自动持久化）'
            }))
        return 0
    }
    $origin = Get-Origin
    $configured = Get-VdgConfigRemote
    Write-Step ('远端仓库位置（{0}）' -f (Get-ShortDir $DshHome))
    Write-Host ("git origin : {0}" -f $(if ($origin) { $origin } else { '(未配置)' }))
    Write-Host ("vdsh.yaml  : {0}" -f $(if ($configured) { $configured } else { '(未设置)' }))
    if (-not $origin) {
        Write-Host '   设置: vdsh sync remote set <URL>（或 sync-dsh.ps1 remote set <URL>）'
        Write-Host '   初始化: vdsh sync init <URL>'
    } elseif ($configured -and $configured -ne $origin) {
        Write-Host '   ⚠ git origin 与 vdsh.yaml 不一致（git 生效；可用 remote set 统一）'
    }
    return 0
}

# ---------------------------------------------------------------- 交互菜单（双击启动器）

function Show-Menu {
    <#
    .SYNOPSIS
        无参数运行时的交互菜单（sync-dsh.cmd 双击进入）。所有文案在 UTF-8 BOM 脚本内，
        cmd 不承担任何中文内容，避免乱码。
    #>
    while ($true) {
        Write-Host ''
        Write-Host '  =========================================='
        Write-Host '    DSH 数据同步工具（小白版）'
        Write-Host '  =========================================='
        Write-Host '    [1] 查看状态   status   看看两边差多少'
        Write-Host '    [2] 推送数据   push     把本机数据存入仓库（收工前用）'
        Write-Host '    [3] 拉取数据   pull     把仓库数据搬到本机（开工前用）'
        Write-Host '    [4] 初始化     init     第一次使用才需要'
        Write-Host '    [5] 设置远端   remote   查看/更换仓库地址'
        Write-Host '    [H] 帮助      用法说明'
        Write-Host '    [Q] 退出'
        Write-Host '  =========================================='
        $choice = Read-Host '  请输入数字选择'
        switch ($choice.Trim().ToLowerInvariant()) {
            '1' { Sync-Status | Out-Null }
            '2' { Sync-Push | Out-Null }
            '3' { Sync-Pull | Out-Null }
            '4' {
                $prompt = '  请输入数据仓库地址（主力机 file:///T:/DataBase/dsh-sync-repo.git；副机 file:///Z:/DataBase/dsh-sync-repo.git）'
                $url = Read-Host $prompt
                if ($url.Trim()) { Sync-Init -RemoteUrl $url.Trim() | Out-Null }
            }
            '5' {
                $prompt = '  请输入远端仓库地址（留空 = 查看当前设置）'
                $url = Read-Host $prompt
                Sync-Remote -Url $url.Trim() | Out-Null
            }
            'h' { Show-Usage }
            'q' { return }
            default { Write-Host '  输入无效，请输入 1-5、H 或 Q。' }
        }
    }
}

# ---------------------------------------------------------------- 分发

# 无参数 = 交互菜单（双击 .cmd 即进入）；带参数 = 直接执行子命令。
$action = if ($CommandArgs.Count -gt 0) { $CommandArgs[0].ToLowerInvariant() } else { 'menu' }
# 重要：if 语句作为管道输出会把「单元素数组」解包成标量，直接 @(...) 套 if 会丢掉数组形态，
# 导致 init 的 URL（$rest[0]）被取成首字符。这里显式构造成 [string[]]。
$rest = @()
if ($CommandArgs.Count -gt 1) {
    $rest = [string[]]$CommandArgs[1..($CommandArgs.Count - 1)]
}

# 数据目录不存在时：init 允许创建（全新机器引导），其余命令直接报错（退出码 4 = 可跳过）；
# 菜单/帮助模式下让用户自行处理。
if (-not (Test-Path $DshHome) -and $action -notin @('init', 'menu', 'help')) {
    Write-Host "❌ 数据目录不存在：$DshHome"
    Write-Host '   请先确认本机已安装并运行过 DSH；全新机器请用 init 初始化。'
    exit 4
}

$code = 1
try {
    switch ($action) {
        'init'   { $code = Sync-Init -RemoteUrl $rest[0] }
        'push'   { $code = Sync-Push }
        'pull'   { $code = Sync-Pull }
        'status' { $code = Sync-Status }
        'remote' {
            # remote [set <URL>]；也兼容 remote <URL>（省略 set）。
            if ($rest.Count -gt 2) { Show-Usage; $code = 2 }
            elseif ($rest.Count -eq 2) { $code = Sync-Remote -Url $rest[1] }
            elseif ($rest.Count -eq 1 -and $rest[0] -eq 'set') {
                Write-Host '用法: vdsh sync remote set <远程URL>'
                $code = 2
            } else { $code = Sync-Remote -Url '' }
        }
        'help'   { Show-Usage; $code = 0 }
        'menu'   { Show-Menu; $code = 0 }
        default  { Show-Usage; $code = 2 }
    }
} catch {
    Write-Host "❌ $($_.Exception.Message)" -ForegroundColor Red
    $code = 1
}
exit $code
