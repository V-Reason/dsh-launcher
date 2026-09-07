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

# 单次 git 操作超时（秒；vdsh.yaml 的 sync.timeout_seconds，0 = 不限时）。
$TimeoutSeconds = 0
if ($env:VDG_SYNC_TIMEOUT) {
    $parsedTimeout = 0
    if ([double]::TryParse($env:VDG_SYNC_TIMEOUT, [ref]$parsedTimeout) -and $parsedTimeout -gt 0) {
        $TimeoutSeconds = [double]$parsedTimeout
    }
}

# ---------------------------------------------------------------- 工具函数

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
    $captured = & git -C $DshHome @GitArgs 2>&1 | ForEach-Object { "$_" }
    if ($captured) { Write-Host ($captured -join "`n") }
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

    Write-Host "==> DSH 数据目录: $DshHome"

    # 全新机器：目录可能尚不存在（DSH 首次运行才创建），init 负责引导创建。
    if (-not (Test-Path $DshHome)) {
        Write-Host '==> 数据目录不存在，正在创建...'
        New-Item -ItemType Directory -Force -Path $DshHome | Out-Null
    }

    if (-not (Test-IsRepo)) {
        Write-Host '==> 初始化 git 仓库 (main)...'
        if ((Invoke-DshGit @('init', '-b', 'main')) -ne 0) { throw 'git init 失败' }
    } else {
        Write-Host '==> 已是 git 仓库'
    }

    $origin = Get-Origin
    if (-not $origin) {
        if (-not $RemoteUrl) {
            Write-Host '❌ 缺少远程地址。用法: sync-dsh.ps1 init <远程URL>（如 file:///Z:/DataBase/dsh-sync-repo.git）'
            return 2
        }
        Write-Host "==> 添加 origin $RemoteUrl"
        if ((Invoke-DshGit @('remote', 'add', 'origin', $RemoteUrl)) -ne 0) { throw 'git remote add 失败' }
    } else {
        Write-Host "==> 已有 origin: $origin"
        if ($RemoteUrl -and $RemoteUrl -ne $origin) {
            Write-Host "==> 更换 origin: $origin -> $RemoteUrl"
            if ((Invoke-DshGit @('remote', 'set-url', 'origin', $RemoteUrl)) -ne 0) { throw 'git remote set-url 失败' }
        }
    }

    # 远端地址同步持久化到 vdsh.yaml 的 sync.remote（与 `remote set` 一致）：
    # 传 URL 用 URL；未传且已存在 origin 时回填 git 里的地址（自愈旧 init 未持久化的情况）。
    $effectiveUrl = if ($RemoteUrl) { $RemoteUrl } else { (Get-Origin) }
    if ($effectiveUrl) {
        $persisted = Set-VdgConfigRemote $effectiveUrl
        Write-Host ("==> 远端记录: {0}{1}" -f $effectiveUrl, $(if ($persisted) {
                '（已写入 vdsh.yaml 的 sync.remote）'
            } else {
                '（未发现 vdsh.yaml，仅更新 git origin；经 vdsh 调用时自动持久化）'
            }))
    }

    if (-not (Test-Path (Join-Path $DshHome '.gitignore'))) {
        Write-Host '==> 生成 .gitignore（排除规则，会随仓库同步）'
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
            Write-Host '==> 已按 vdsh.yaml 的 sync.gitignore_extra 补写 .gitignore 缺失行'
        }
    }

    # 两端 core.autocrlf 必须一致（默认均 true 即可；若改，两台一起改）。
    $crlf = & git -C $DshHome config core.autocrlf 2>$null
    if (-not $crlf) { $crlf = '(未设置, 默认 true)' }
    Write-Host "==> core.autocrlf = $crlf（两端保持一致）"

    # 配置概况（供核对 vdsh.yaml 生效值）。
    $originNow = Get-Origin
    Write-Host ("==> 配置概况: 远端={0} | 同步范围={1} 项 | .gitignore={2}" -f (
        $(if ($originNow) { $originNow } else { '(未设置)' })),
        $Allowlist.Count,
        $(if (Test-Path (Join-Path $DshHome '.gitignore')) { '已就绪' } else { '缺失（应已在此步生成）' }))

    if ((Invoke-DshGit @('fetch', 'origin') -Animated -Message '获取远端数据…') -ne 0) {
        Write-Host '==> 注意: origin 暂不可达（首次初始化可稍后再试）'
        return 0
    }

    if (-not (Test-RemoteMain)) {
        Write-Host '==> 远端还没有 main 分支：先在主力机执行一次 sync-dsh.ps1 push 建立，或对空裸仓库执行 git push -u origin main。'
        return 0
    }
    $hasHead = (Wait-GitQuiet @('rev-parse', '--verify', '--quiet', 'HEAD')) -eq 0
    if (-not $hasHead) {
        # 本机无提交且远端已有 main：判断目录是否「全新」（只含 init 刚生成的 .gitignore）。
        # 是——用 -f 覆盖后 checkout（目录无其它数据，安全）；否——存在已有 DSH 数据，走 merge/reset。
        $untracked = & git -C $DshHome status --porcelain --untracked-files=all 2>$null
        $onlyGenerated = ($untracked | Where-Object { $_.Trim() -ne '' -and ($_.Trim() -notmatch '^\?\?\s+\.gitignore$') } | Measure-Object).Count -eq 0
        if ($onlyGenerated) {
            Write-Host '==> 数据目录为全新，自动从远端填充数据...'
            if ((Invoke-DshGit @('checkout', '-f', '-b', 'main', 'origin/main')) -ne 0) {
                Write-Host '   自动切换失败，请手动执行（-f 会用仓库版本覆盖 init 生成的 .gitignore，目录无其它数据，安全）:'
                Write-Host "    cd $DshHome; git checkout -f -b main origin/main"
            } else {
                Write-Host '✅ 已完成：远端数据已填入本机，可执行 status 查看。'
            }
        } else {
            Write-Host '==> 本机尚无提交但目录里已有 DSH 数据（尚未入库），二选一:'
            Write-Host "    git merge origin/main                          # 把远端历史并入本地数据"
            Write-Host "    或 git reset --soft origin/main                # 以远端为基线，本地数据作为未提交变更，随后 push"
        }
    } elseif ((Get-DirtyAllowlist).Count -eq 0) {
        Write-Host '==> 本地已就绪；查看状态: sync-dsh.ps1 status'
    } else {
        Write-Host '==> 本机已有 DSH 数据且远端也有历史，请选择:'
        Write-Host "    git merge origin/main                          # 把远端历史并入本地数据"
        Write-Host "    或 git reset --soft origin/main                # 以远端为基线，本地数据作为未提交变更，随后 push"
    }
    return 0
}

function Sync-Push {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    Write-Host "==> 推送 DSH 数据 ($DshHome)"
    if (-not (Test-IsRepo)) {
        Write-Host '❌ 尚未初始化。先用: sync-dsh.ps1 init <远程URL>'
        return 4
    }
    if (-not (Get-Origin)) {
        Write-Host '❌ 未配置 origin。用: sync-dsh.ps1 init <远程URL>'
        return 4
    }

    $paths = Get-ExistingAllowlist
    if ($paths.Count -eq 0) {
        throw '同步清单中没有任何存在的路径（检查 $DSH_HOME 下的 sessions/ 等目录）'
    }
    Write-Host "==> 暂存: $($paths -join ', ')"
    $addArgs = @('add', '-A', '--') + $paths
    if ((Invoke-DshGit $addArgs) -ne 0) { throw 'git add 失败' }

    $staged = @(& git -C $DshHome diff --cached --name-only -- $paths)
    if ($staged.Count -eq 0) {
        Write-Host '✅ 没有变更需要推送（工作区与远端一致）。'
        return 0
    }

    $message = 'sync: {0} ({1} 个文件)' -f (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss'), $staged.Count
    Write-Host "==> 提交: $message"
    $commitArgs = @('-c', "user.name=$CommitName", '-c', "user.email=$CommitEmail", 'commit', '-m', $message)
    if ((Invoke-DshGit $commitArgs) -ne 0) { throw 'git commit 失败' }

    Write-Host '==> 推送...'
    if ((Invoke-DshGit @('push') -Animated -Message '推送中…') -ne 0) {
        # 上游未建立（全新远端分支）：带 -u 再试
        if ((Invoke-DshGit @('push', '-u', 'origin', 'main') -Animated -Message '推送中…') -ne 0) {
            Write-Host '   ⚠ 若错误为「non-fast-forward / 远端有更新」：另一台机器已推送过，请先执行 sync-dsh.ps1 pull 合并后再 push。'
            throw 'git push 失败：请确认远端可达（共享盘/内网穿透已挂载）'
        }
    }
    Write-Host ("✅ 推送完成。 (耗时 {0:N1}s)" -f $sw.Elapsed.TotalSeconds)
    return 0
}

function Sync-Pull {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    Write-Host "==> 拉取 DSH 数据 ($DshHome)"
    if (-not (Test-IsRepo)) {
        Write-Host '❌ 尚未初始化。先用: sync-dsh.ps1 init <远程URL>'
        return 4
    }
    if (-not (Get-Origin)) {
        Write-Host '❌ 未配置 origin。用: sync-dsh.ps1 init <远程URL>'
        return 4
    }

    $dirty = Get-DirtyAllowlist
    if ($dirty.Count -gt 0) {
        $preview = @($dirty | Select-Object -First 5 | ForEach-Object { $_.Substring(3) })
        Write-Host "⚠️ 工作区有 $($dirty.Count) 个未提交变更（如 $($preview -join ', ')）。"
        Write-Host '   先执行 sync-dsh.ps1 push 提交本地变更，再 pull。'
        return 3
    }

    if ((Invoke-DshGit @('fetch', 'origin') -Animated -Message '拉取远端更新…') -ne 0) {
        throw 'fetch 失败：请确认远端可达（共享盘/内网穿透已挂载）'
    }
    if (-not (Test-RemoteMain)) {
        Write-Host '❌ 远端 origin/main 不存在：请先在主力机执行一次 push。'
        return 3
    }

    # 先尝试纯快进（常态：两端交替使用、无分叉）。
    if ((Invoke-DshGit @('merge', '--ff-only', 'origin/main') -Animated -Message '快进合并…') -eq 0) {
        Write-Host ("✅ 拉取完成（快进）。 (耗时 {0:N1}s)" -f $sw.Elapsed.TotalSeconds)
        return 0
    }

    # 本地无提交（全新副机）。
    if ((Wait-GitQuiet @('rev-parse', '--verify', '--quiet', 'HEAD')) -ne 0) {
        Write-Host '==> 本地还没有提交；全新副机执行:'
        Write-Host "    cd $DshHome; git checkout -b main origin/main"
        return 3
    }

    # 本地与远端各有提交：常规合并。
    Write-Host '==> 本地与远端有分叉，尝试常规合并...'
    $message = 'sync: merge {0}' -f (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss')
    if ((Invoke-DshGit @('merge', '-m', $message, 'origin/main') -Animated -Message '合并远端历史…') -eq 0) {
        Write-Host ("✅ 拉取完成（合并）。 (耗时 {0:N1}s)" -f $sw.Elapsed.TotalSeconds)
        return 0
    }

    Write-Host '⚠️ 拉取产生冲突（合并未提交，仓库处于冲突中间态）。'
    Write-Host '   处理方式（二选一）：'
    Write-Host "   1) 回滚:      git -C $DshHome merge --abort"
    Write-Host '   2) 解决后再提交：二进制/会话文件建议把远端版本另存为 <文件>.remote-fork 再 git add 两侧文件'
    Write-Host '   详细步骤见 doc/native-git-sync.md 的「冲突处理」章节。'
    return 3
}

function Sync-Status {
    Write-Host "==> DSH 数据同步状态 ($DshHome)"
    if (-not (Test-IsRepo)) {
        Write-Host "❌ $DshHome 不是 git 仓库。先用: sync-dsh.ps1 init <远程URL>"
        return 4
    }

    $origin = Get-Origin
    $branch = (& git -C $DshHome symbolic-ref --short HEAD 2>$null) | Select-Object -First 1
    Write-Host ("仓库: {0}" -f $DshHome)
    Write-Host ("分支: {0}   远端: {1}" -f $(if ($branch) { $branch.Trim() } else { '(尚无提交)' }), $(if ($origin) { $origin } else { '(未配置)' }))

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
        $preview = @($dirty | Select-Object -First 10 | ForEach-Object { $_.Substring(3) })
        Write-Host ("待推送: {0} 个变更（{1}{2}）" -f $dirty.Count, ($preview -join ', '), $(if ($dirty.Count -gt 10) { ' …' } else { '' }))
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
    Write-Host "==> 远端仓库位置 ($DshHome)"
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
