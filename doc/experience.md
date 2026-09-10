# 经验与踩坑记录

按主题整理，供排查复现。每条均标注「影响面」与「解决方式」。

## 1. PowerShell 5.1 与 7 的差异（同步脚本最敏感的坑）

### 1.1 UTF-8 BOM 被编辑器/工具剥除 → PS 5.1 全文件乱码报错

**现象**：`sync-dsh.ps1` 经文本编辑工具保存后，PS 5.1 `ParseFile` 报大量语法错误，中文变乱码（`鑾峰彇` 等 GBK 误读痕迹）。
**原因**：PS 5.1 `-File`/`Parser` 对**无 BOM 文件**按 ANSI（GBK）读取；而脚本内嵌中文文案。
**解决**：_每次编辑后_恢复 BOM：

```powershell
$p = 'dsh-data-git-sync\sync-dsh.ps1'
$t = [IO.File]::ReadAllText($p, [Text.UTF8Encoding]::new($false))
[IO.File]::WriteAllText($p, $t, [Text.UTF8Encoding]::new($true))
```

并在提交前用 `[System.Management.Automation.Language.Parser]::ParseFile` 校验。

### 1.2 ConvertFrom-Json 顶层数组：5.1 不拆管道

**现象**：`@($env:X | ConvertFrom-Json)` 在 PS 7 得 N 个元素，在 PS 5.1 得 **1 个元素（Object[]）**，导致 allowlist 覆盖成单元素。
**原因**：5.1 把顶层 JSON 数组作为**单个对象**输出，不展开到管道。
**解决**：解析后统一展平一层：

```powershell
$parsed = @($env:VDG_SYNC_ALLOWLIST | ConvertFrom-Json)
if ($parsed.Count -eq 1 -and $parsed[0] -is [array]) { $parsed = @($parsed[0]) }
```

### 1.3 stderr 转义与 ErrorRecord

**现象**：`& git ... 2>&1 | ForEach-Object { "$_" }` 输出中出现 `System.Management.Automation.RemoteException` 行。
**原因**：PS 5.1 把 git 的 stderr 包成 ErrorRecord；`"$_"` 的 ToString 是类型名。
**解决**：转字符串处理已是最简；属提示性噪音（在真实控制台与重定向下均存在），不影响逻辑。若要更干净，可改成 `ForEach-Object { $_.ToString() }` + 特判 `$_ -is [System.Management.Automation.ErrorRecord]` 时取 `$_.Exception.Message`——目前未做（不改变功能）。

### 1.4 runspace 动画的控制台语义

`Invoke-GitSpinner`（[runspacefactory] + [powershell]::Create）在 PS 5.1/7 均可用；注意：
- `$ps.AddScript($body).AddArgument($DshHome).AddArgument([string[]]$GitArgs)`：**数组以单个参数传入**，scriptblock 内 `param($homeDir, [string[]]$gitArgs)` 接收完整数组（实测 OK）。
- 输出经 `EndInvoke` **一次性**取回（git 输出在动画结束后统一打印）；过程输出不流式——这是与 Python 层动画（逐行流式）的取舍差异。
- 超时/中断：`$ps.Stop()` 包 try/catch；`$ps.Dispose()`/`$runspace.Dispose()` 放 finally。
- **必须与上层互斥**：stdout 被 Python 捕获（`[Console]::IsOutputRedirected = $true`）时跳过动画，否则 `\r` 帧会作为文本混入捕获流。

### 1.5 git 进度输出与非 TTY（「空转」元凶，2026-09）

**现象**：`vdsh sync init` 的「获取远端数据」只有转轮，长时间无任何内容级输出（`git fetch` 实际在跑）；直接终端（TTY）却能看到 `Receiving objects…`。
**原因**：git 的 fetch/push **进度默认只在 stderr 为 TTY 时输出**；stdout/stderr 被捕获（vdsh 管道、重定向）时完全静默，必须显式加 **`--progress`** 才强制输出。非 TTY 下 git 以**换行分隔**输出进度更新（每行一条，可被管道逐行消费）；TTY 下是 `\r` 原地刷新。
**解决**：长操作一律 `git … --progress`（init/pull 的 fetch、push）；配合 PS 侧**逐行流式**（见 1.6）。追加验证方法：本地 `git init --bare` + mirror，构造增量对象后重定向跑 `fetch --progress`，对照不带 `--progress`（无输出）即复现。

### 1.6 PS 管道的流式 vs 缓冲

**现象**：`& git … 2>&1 | ForEach-Object { "$_" }` 收集后再 `Write-Host ($captured -join "`n")` —— 进度只能在命令结束看到**最后一批**；git 长时间不退出时一无所有。
**原因**：PowerShell 管道本来就是**逐条流式**处理记录，缓冲是收集变量造成的（也影响下层的 `Invoke-GitSpinner` runspace：输出一次性取回，见 1.4）。
**解决**：`… | ForEach-Object { Write-Host ("$_") }` 直接透传即流式；需要「先看全部再决策」时才收集。**不要**为了「最后统一打印」而缓冲长命令输出——那会把进度全吞掉。

## 2. 编码问题

### 2.1 Python 重定向到 gbk 文件时 UnicodeEncodeError

**现象**：子进程输出含 `✅`（U+2705，不在 GBK），经 `print` 写重定向 stdout 崩溃。
**解决**：入口统一 `sys.stdout/stderr.reconfigure(encoding="utf-8", errors="replace")`（`console.setup_streams`）。PS 侧 `[Console]::OutputEncoding = UTF8` 保证管道内是 UTF-8，两端一致。

### 2.2 非交互/重定向的动画静默

`Spinner` 以 `sys.stdout.isatty()` 判定；`say()` 非 TTY 直接 `print(line)`。因此**重定向输出绝不含 `\r`/控制字符**（有回归测试断言）。

### 2.3 sync-dsh.ps1 必须 UTF-8 带 BOM（2026-09 真实踩坑）

**现象**：`vdsh sync …` 报满屏 `UnexpectedToken`（PS 5.1「字符串缺少终止符/缺少"}"」），脚本 37/104/117/118/129 行全线报错。
**原因**：`sync_host()` 走 Windows PowerShell 5.1；`.ps1` 无 BOM 时按 ANSI（GBK）解码，中文注释/字符串全部乱码，括号引号错位 → 解析器在字符串中途崩掉（如动画帧串 `⠋⠙⠹…` 丢失收尾引号）。
**解决**：脚本保持 **UTF-8 with BOM**；本次即因用 UTF-8 无 BOM 的编辑器改写脚本丢掉了 BOM（`git diff` 只见首行多出 `﻿` BOM 字符）。launcher 侧 `vdsh/features/sync.py` 已在执行前检查 BOM，缺失时给出明确报错而非解析墙。**注意任何文本编辑工具都可能在重写时剥 BOM**（2026-09 再踩：AI 编码工具的 file 修改/写入 API 保存后 BOM 丢失）——每次编辑后按 §1.1 幂等恢复一次。

## 3. YAML 配置

### 3.1 双引号标量内反斜杠

`repo: "T:\deepseek-harness"`（文件里单反斜杠）→ 解析报 `found unknown escape character 'd'`。
**解决**：模板里路径用**裸标量**（无引号）；程序写值时统一 `json.dumps(value, ensure_ascii=False)`（JSON 字符串即合法 YAML 双引号标量，反斜杠会被转义为 `\\`）。

### 3.2 raw 字符串首字符坑（本项目真实踩到）

**现象**：`TEMPLATE = r"""\` + 换行 → 生成的文件首字符是**字面 `\`** → YAML 解析失败 → 全部配置回退默认（用户改的值全被忽略，且无任何错误指向模板）。
**解决**：raw 三引号串首行直接写内容（`r"""# vdsh.yaml...`）；禁止以 `\` 续行开头。这类「整体静默回退」最阴险——`vdsh config` 要能看到警告（现在 load_settings 会打印「无法解析」）。

### 3.3 文本补丁 vs 全量重写

`remote set` 若整体 dump 配置会毁掉用户注释。`patch_values`（Python）与 `Set-VdgConfigRemote`（PS）都做**行级替换**：匹配 `^空格+键:` 行、保留其余、值 JSON 引号化；键不存在则段内/文件尾追加。两份实现保持语义等价——**改动时须同步**。

### 3.4 键后的行尾注释会污染文本级读取（2026-09）

**现象**：`vdsh sync remote` 显示 `vdsh.yaml: file:///T:/…git" # 默认远端；…` 且误报「git origin 与 vdsh.yaml 不一致」。
**原因**：文本级读取用 `(?m)^\s+remote:\s*(.*)$` 捕获**整行**——模板/手工配置常在值后跟 `# 说明`，注释被当成值的一部分（值带引号时 `ConvertFrom-Json` 失败 → 折返 `Trim('"')` 仍留注释）。
**解决**：先剥行尾注释（`-replace '\s+#.*$', ''`）再判断引号/JSON 反解。`Get-VdgConfigRemote`（PS）与 `dsh_cli.py`（Python）都有此逻辑，**两份实现须同步**（与 3.3 同约定）。`patch_values` 写值时不会带注释（整行替换），不要反过来依赖「读取端容忍注释」。

## 4. 环境 / 沙箱限制（仅供自测参考）

### 4.1 git 的 msys 信号管道（Win32 error 5）

**现象（仅 DSH 沙箱会话）**：`git fetch/push/ls-remote` 报 `*** fatal error - couldn't create signal pipe, Win32 error 5`（`sh.exe` 无法建命名管道）。
**结论**：这是沙箱约束（named pipe 受限 + 权限），**用户真实终端无此限制**。在沙箱内测试网络型 git 操作不可行；用本地 `file:///` 极端路径也只能测到本地 stage/commit。推送等网络步骤需要 `danger-full-access` 批准。

### 4.2 taskkill 返回码检查

**现象**：超时守护线程调 `taskkill /T /F` 后直接 return，但 taskkill 在受限环境可能失败（退出码非 0）→ 子进程未被杀（曾实测等了 8s 才自然结束）。
**解决**：`subprocess.run(...).returncode == 0` 才 return，否则回退 `proc.kill()`。真实环境 taskkill 正常，此回退仅防御。

### 4.3 管道吞退出码

`cmd | Select-Object` 或嵌套管道会污染/清空 `$LASTEXITCODE` 显示。断言退出码时**不要接管道**：`python x.py; echo $LASTEXITCODE`。

## 5. 动画并发设计（Spinner）

- 线程每次重绘前**持锁**检查 `_paused/_stop`；`say()` 顺序：置暂停 → 持锁（擦除 + 打印子进程行 + `\n`）→ 解除暂停。动画循环恢复后在新行重绘，永不覆盖数据行。
- 竞态结论：锁内检查 + 主线程持锁写入，两个入口串行化；`say()` 不等线程确认（最大 1 帧延迟，视觉无感）。
- `finish()`：置 stop → 持锁擦除 → join；非 TTY 直接打印文本。
- **帧率**：模块级 `_FPS/_FRAMES` 经 `configure()` 从 vdsh.yaml 设置，构造时快照（实例级一致）。非法值静默忽略——测试断言时注意 `frames` 是 tuple 不是 str。
- PS 侧动画：帧串/帧率经 `VDG_ANIMATION_*` 传入，缺省 120ms 与内置帧串（与 Python 默认一致）。

## 6. 交互与 Read-Host

- `vdsh sync`（无参菜单）走 `subprocess.call` 继承 stdio——**不能**改走捕获管道，否则脚本内 `Read-Host` 失效。
- 向导的交互判定：`sys.stdin.isatty()`；non-TTY/EOF/KeyboardInterrupt 一律降级（模板 + 提示），**不得**阻塞任何命令。
- 菜单 `[5] 设置远端` 与 CLI `vdsh sync remote set <URL>` 均调 `Sync-Remote`：CLI 传 `$rest[1]`（`set` 是 `$rest[0]`，曾接错成 `$rest[0]` 把 URL 写成字面 `set`——回归测试要覆盖）。

## 7. 其它

- **就绪标记**：`window.__DSH_BOOT__` 优先、旧标题 `DeepSeek Harness` 兼容回退——改判定时两者都要考虑。0.1.3-alpha.1 起新增**认证 URL 行**通道（`dsh web: <url>`，见 7.1），优先级高于 HTTP 探测。
- **`--patch` 位置**：必须位于 `web` 之后所有 app 参数**之前**（CLI enablePositionalOptions 会把首个位置参数后的选项透传 app）；`--no-open` 与 `--trusted-host` 的相对顺序有注释说明，不要随意调整。
- **种子文件**：`workspace-seed.mjs`/`seed.yml` 是生成物（gitignored），`ensure_seed_patch` 幂等重建；改种子内容要同时改 `WORKSPACE_SEED_MJS` 常量（源码内的重建源）。
- **同步脚本缺省值 = 配置默认值**：`vdsh.yaml` 的 DEFAULTS 与 `sync-dsh.ps1` 的硬编码默认（allowlist/身份/帧串）必须一致——两边是同一套语义的不同入口。

### 7.1 DSH 0.1.3-alpha.1 浏览器会话认证（2026-09 适配）

**现象**：`vdsh` 对已运行实例报「端口 3080 已被占用但未识别为 Harness」；全新启动等满超时后报「启动失败」（DSH 实际正常）；浏览器裸 `http://127.0.0.1:3080/` 得到 **401**，正文 `dsh web authentication required; reopen the URL printed by dsh web.`。插件层同源问题见 `$DSH_HOME\_TMP\error.txt` 的 `settingsNamespace`（那是插件适配问题，另一条线）。

**原因（platform 源码）**：8-30 重构后 `dsh web` 对根页面与 `/api` 实施浏览器会话认证（`packages/client/connection/src/browser-auth.ts`）：`GET /?token=<per-process launch token>` → 303 → 干净 `/` + HttpOnly cookie；无 token/无 cookie 一律 401。token 由服务器进程生成，**无法预先得知**；其「URL 行」`dsh web: http://127.0.0.1:3080/?token=…` 在 Loader 树结算后打印到 stdout（`--no-open` 也打印，`printUrl` 默认 true）。

**解决（launcher）**：启动时把 pwsh 命令输出全流重定向到 `%TEMP%\vdsh-web.log` → 轮询截获 URL 行作为就绪信号 → `requests.Session` 跟随 303 完成 token→cookie 交换 → 打开带 token 的 URL（浏览器首访换 cookie）、复用 Session 调 `/api/workspace/create`；`probe_harness` 识别 401 正文为「auth（已在运行）」。已运行实例若由启动器启动，日志里仍是当前进程的 token，可复用；否则只能提示用户用 DSH 窗口打印的 URL。

**RPC 契约（2026-09 修正，二次启动告警的根因）**：端点恒为 `/api/<namespace>/<method>`，报文为 `{"type":"client-request","rpcId":…,"method":"<namespace>/<method>","payload":{"args":{"request":{…}}}}`（`args` 下的键 = 方法形参名；`@Remote('create') create(request)` → `args.request`）。点号端点（`/api/workspace.create`）与裸 `payload:{path}` 自 RPC 通道引入（2026-08-07）起就不存在，只会得到 **404 纯文本 `not found`**。全新启动走 `--patch` 种子插件，不经过这条 RPC，所以只有「实例已在运行」才会暴露。

**排错识别法**：`Expecting value: line 1 column 1 (char 0)`（`resp.json()` 对非 JSON 正文的报错）= 端点或报文写错，**不是**业务失败。现在这类失败会直接打印 `HTTP <code>：<正文摘要>`（`_body_snippet`），不再让 JSON 解析异常当门面。

### 7.2 sync init 不持久化远端（「yaml 没生效」错觉）

**现象**：`vdsh sync init <URL>` 后 `vdsh config` 的 `sync.remote` 仍为空，但 push/pull 全部正常——git `origin` 才是实际来源，vdsh.yaml 的 remote 仅作无参回退，故「没生效」的观感与「正常」并存。
**解决**：`Sync-Init` 成功后调用 `Set-VdgConfigRemote`（与 `remote set` 同一文本级写入函数，保留注释）；重跑 init 传新 URL 会 set-url；无参 init 从 git origin 回填。两个入口（CLI/菜单）都走同一函数，不会再出现「只改一边」。**2026-09 补充**：`vdsh sync init` 无参在 TTY 下现在走配置向导（回车确认默认值后行为等价）；非 TTY（脚本化调用）仍是「回填/回退 sync.remote」原逻辑，不受影响。

### 7.3 「运行中的实例未带 --trusted-host」假告警（2026-09 修正）

**现象**：DSH 已在运行时再执行 `vdsh`，即使实例启动时确实带了 `--trusted-host`（tailnet 来自 `vdsh.yaml` 的 `launcher.tailnet`），仍打印「运行中的实例未带 --trusted-host，手机访问会 403；请关闭后重启」。

**原因**：`_open_existing` 原先只判断 `tailnet is not None`，无条件告警——它从未核实运行实例是否真的信任该域名。

**解决**：改用 **Host 围栏探测**（`tailnet_is_trusted`）：用请求头 `Host: <域名>` 打 `/api/<任意端点>`，围栏在认证与端点分发**之前**生效，因此无需 cookie —— 实测「已信任 → 401/404，未信任 → 403」（`packages/client/connection/src/api-request-trust.ts`：Host 既非 loopback 也不在 `trustedHosts` → 403）。**只有明确 403 才告警**，探测失败/未知一律静默（宁缺勿假）。探测用「传给 `--trusted-host` 的同一个字符串」，与启动参数同源。

### 7.4 `vdsh update plugin` 的 `[WARN]` 是伪报吗？（2026-09-10，源码 + 实测确认）

**现象**：更新时逐行打印
`[WARN] HEAD https://github.com/<owner>/<repo> error (ECONNRESET|ETIMEDOUT). Will retry in 500 milliseconds. 2 retries left.`（每个 git 依赖 2 条，退避 500ms→1s）
以及 `[WARN] Issues with peer dependencies found. Run "pnpm peers check" to list them.`，最后仍然成功（`插件已是最新`、退出码 0），让人怀疑是不是被吞掉的错误。

**结论**：两类都是 pnpm 自己打印的**非致命**提示（不是 vdsh 的输出——vdsh 只用 `vdsh ·`/`vdsh ⚠`/`vdsh ✗`），且**底层网络条件是真的**：本机 `github.com:443` 被阻断。

**根因（pnpm 11.21 源码，`…\AppData\Roaming\npm\node_modules\pnpm\dist\pnpm.mjs`）**：

1. HEAD 只有一处来源——`isRepoPublic()`（解析 `github:` 依赖时判断仓库是否公开）：
   `fetchWithDispatcher(httpsUrl.replace(/\.git$/,''), {method:'HEAD', redirect:'manual', retry:{retries:2, factor:2, minTimeout:500, maxTimeout:2000}})`，
   失败被 `catch { return false }` **吞掉**；它只决定走 codeload tarball 还是 git 解析（`tarball: repoIsPublic ? hosted.tarball : void 0`），
   重试次数与退避和日志逐字吻合（pnpm 默认 `fetch-retries: 2`）。
2. 打印者是 `reportRequestRetry` → `formatWarn()`（即 `[WARN] …`），纯提示。
3. peer 提示出自 `formatWarn('Issues with peer dependencies found…')`；缺 peer 是 **DSH 的设计**：`packages/boot/app-boot/src/profile.ts`
   的 `PROFILE_PNPM_WORKSPACE` 给每个 profile 写 `nodeLinker: hoisted` + `autoInstallPeers: false`，注释写明「missing peers … fall through to the
   healed profiles/node_modules installation fallback」——保证所有插件共用安装层那一份 cordis，而不是各装一份。
   `pnpm peers check` 实测 16 项缺 peer（`dsh-better-sidebar` 的 14 个 `@deepseek-ai/*` + `react-icons` 的 `react`/`react-dom`），
   全部在 `$DSH_HOME/profiles/node_modules/{@deepseek-ai/*,react,react-dom}` 里有链接 → 运行时解析得到。
4. `Packages: -2`（含下面的 `--` 条）也不是错误：pnpm `statsForCurrentPackage` 的安装统计行，表示本次从 `node_modules` 清掉 2 个过期条目；
   当时 `package.json`/`pnpm-lock.yaml` 未被改写，5/5 声明依赖均在位。

**实测（2026-09-10）**：`github.com:443` TCP **FAIL**；`github.com:22`、`codeload.github.com:443`（GET 200/572ms）、`api.github.com:443`、
`ssh.github.com:443`、`registry.npmjs.org:443`（200/295ms）全部 OPEN；node `fetch(..., {method:'HEAD'})` 打三个仓库 URL 全部 TimeoutError；
`git ls-remote https://github.com/…` 21s 后 `Could not connect to server`，而 `git@github.com:…`（SSH）4.3s 成功并返回 lockfile 记录的 commit。

**影响与边界**：正确性不受影响（探测失败 → 回退 git/SSH 解析，lockfile 因此记为 `git+ssh://…#<sha>`）；代价是每次更新多十几秒 + 吓人的 WARN。
彻底消除靠用户侧放行或配 `HTTPS_PROXY`（`no_proxy`/`https-proxy` 亦受支持，见 pnpm 的 `EnvHttpProxyAgent`）；vdsh 侧只做「让输出自解释」，不改 pnpm 行为。

**复现**：`Test-NetConnection github.com -Port 443`（False）+ `Test-NetConnection codeload.github.com -Port 443`（True）足以定性。

### 7.5 git 依赖「版本号没变 = 没更新」是假阴性（2026-09-10 修复）

**现象**：旧 `_update_plugin` 用 `node_modules/<name>/package.json` 的 `version` 比较前后差异。对 `github:` 规格的依赖，
上游推了新 commit 但没升 `version` 时，会打印「插件已是最新（无版本变化）」——**实际已经换了代码**，用户完全看不出来。

**根因**：git 依赖的「装的是哪个版本」不由 `version` 字段决定，而由 commit 决定；lockfile（`importers['.'].dependencies[name].version`）与磁盘都不在 `version` 字段里体现 commit。

**解决（`vdsh/profile_state.py`，三重证据）**：

1. `package.json` 的 `dependencies`/`dsh.profile.bundles`；
2. `pnpm-lock.yaml` 的 `importers['.'].dependencies`（PyYAML 缺失时降级跳过，不报错）；
3. **磁盘解析身份**：`node_modules/.modules.yaml` 的 `hoistedLocations` 键 `<name>@<version>` 或 `<name>@git+ssh://…#<commit>`（`nodeLinker: hoisted` 下的权威记录）。
   比较用 `identity()`：git → `('git', commit)`，其余 → `('semver', 版本)`；展示用 `0.7.0@da602d1` 这种形式。
   两者**同类才比对**，否则（降级路径）跳过——避免拿 semver 去比 git 造成假失败。

**同时暴露的第二个坑**：装 ≠ 生效。判生效方式四态（`profile 层` / `预设挂载` / `普通依赖` / `未激活`）。
实例：`dsh-study-buddy` 的 `package.json` **没有** `dsh.bundle`，因此 `dsh plugin` 的 `reconcilePlugins()` 不会把它加进 `dsh.profile.bundles`——
但它自带 `cordis.patch.yml` 并由用户预设 `~/.dsh/.agent-presets/study/agent.cordis.yml` 的 `name: dsh-study-buddy` 行挂载，属**正常**形态。
若把它误报成告警，就重演了「假告警」的老毛病；因此「未见引用」只做 ⚠ 且限定在插件形状的包（名字 `dsh-` 前缀或自带补丁文件），
扫描引用时**排除包自己的补丁文件**（否则任何自带 `cordis.patch.yml` 的插件都会自我证明被使用）。

**判定键解析**：`hoistedLocations` 的键不能用 `rsplit('@')`——`dsh-at-file@git+ssh://git@github.com/x.git#sha` 里还有 `@`。
规则：名字以 `@` 开头（scoped）时取首个 `/` 之后的第一个 `@`，否则取首个 `@`。

## 8. 跨机路径与启动失败检测（2026-09）

### 8.1 硬编码绝对路径：跨机复制 launcher 的「必挂点」

**现象**：副机 `dsh web` 报 `Cannot find module 'T:\deepseek-harness\apps\cli\lib\bin.js'`；`vdsh` 报「未找到 Harness 仓库（T:\…）」。
**原因**：`dsh.cmd` 硬编码 `node "T:\…" `、`config.py DEFAULT_REPO`/`settings.py TEMPLATE` 带主力机路径；副机复制 launcher 目录（含 vdsh.yaml）后全部残留。
**解决（解析链，三处一致）**：`DSH_REPO`（显式，最高）→ `vdsh.yaml launcher.repo` → `REPO_CANDIDATES`（`C:\deepseek-harness`、`T:\deepseek-harness` 等本机候选，见 `config.py`）。launch 在配置值无效且**非 DSH_REPO 覆盖**时探测并 `patch_values` 自愈；向导默认值同样先探测。**规则：显式环境变量永不覆盖**。
**注意**：`REPO_CANDIDATES` 是硬编码列表，换新安装位置/新机型记得加候选，或后续改为自动探测。

### 8.2 `pwsh -NoExit` 陷阱：子进程崩溃但句柄不退出

**现象**：node 启动即崩溃（CLI 丢失等），`dsh web` 直启立刻看到报错，`vdsh` 却空转到 180s 超时。
**原因**：`pwsh -NoExit -Command 'node … *> 日志'` 中 node 退出后 pwsh **仍存活**（-NoExit 的意义），`Popen.poll()` 永远为 None → 拉不走进程，只能靠超时。且输出全被重定向到日志，窗口里空无一物。
**解决**：去掉 `-NoExit`（window 随 node 退出自动关闭）；`spawn_server` 返回 Popen 句柄，`wait_until_ready(proc=…)` 在就绪前检测 `proc.poll() != None` → 立即终止并打印 `WEB_URL_LOG` 尾部（`_log_tail`，20 行）。启动前另做 node/CLI 产物预检，把常见失败挡在 spawn 之前。
**边界**：检测只覆盖 launcher 自己拉起的进程；「starting」分支（端口被占，无句柄）仍是 30s 预算 + 超时提示，不做日志启发式（防插件正常告警误判）。

### 8.3 file:// / 本地路径的远端解析怪癖

- `file:///Z:/…`：盘符在 `path`（`/Z:/…`），需去首斜杠 → `Z:\…`；
- `file://Z:/…`（漏第三个斜杠）：盘符出现在 **netloc**（`Z:`），要按 `netloc + path` 处理，否则被当 UNC 拼成 `\\Z:\…`；
- `file://host/share/…`：netloc 是主机 → `\\host\share\…`；
- `X:\…`、`/X:/…`：归一化为 `file:///X:/…` 交给 git（git 接受裸 UNC `\\server\share\…` 原样）；
- `http(s)://…`：不做本机存在性校验（fetch 实际验证），向导仅语法检查并明示。
- 「符合预期」的判定：`HEAD` + `objects/`（+`refs/`）存在 = 裸仓库；含 `.git/` = 普通仓库（提示建议裸仓库，不拦截）。

### 8.4 交互向导的输入契约（vdsh sync init 无参）

- 判定 `sys.stdin.isatty()`：TTY 才进向导；非 TTY/EOF/KeyboardInterrupt 一律降级（回退 `sync.remote` 或用法错误），**不阻塞脚本化调用**。
- 每项 `s`/`skip`/`q` = 跳过该项（不写配置）；EOF/Ctrl+C = 取消整个向导（返回 0，不执行 init）；校验失败重试 3 次后放弃该项并明确提示。
- 校验策略是「重试提示」而非「拦截放行」：走不过校验的远端，用户可另带 URL 直跑 `vdsh sync init <URL>` 绕过。
- 写回走 `patch_values`（保留注释）；持续化的键：`launcher.repo`、`sync.data_dir`、`sync.remote`。

### 8.5 Windows git 会把 junction **展开入库**：`.dsh-module-fallback` 的「非链接」报错（2026-09，副机启动失败根因）

**现象**：副机 `vdsh`（dsh web 启动）报：
`Error: dsh: C:\Users\…\.dsh\profiles\web\.dsh-module-fallback\node_modules\@codemirror\commands exists and is not a symlink or dsh-managed module proxy; remove it so dsh can manage the installation fallback`。

**根因链（真实数据验证）**：
1. dsh 的模块回退目录 `profiles/web/.dsh-module-fallback/node_modules` 在主力机上是 **junction**（指向 `profiles/web/node_modules` 的 pnpm 安装，dsh 启动时自动愈合/重建）；
2. Windows 上 git **不认识 directory junction 的链接语义**：`core.symlinks` 默认关闭时把 junction 当**普通目录递归**，于是 12618 个真实的包文件被 `git add` 入库（`.gitignore` 只写了 `profiles/web/node_modules/`，漏了 `.dsh-module-fallback/`）；
3. 副机 `checkout` 出来的就是真实目录/文件（无链接属性）→ dsh 启动 `healProfileModuleFallback → ensureSymlink` 校验「is symlink 或 dsh-managed proxy」失败 → 抛出上面的错误，`vdsh` 立即呈现。

**判定规则（app-boot 的 ensureSymlink，自愈与之对齐）**：
- 是 symlink/junction（reparse point）→ 保留；
- 是 dsh proxy：目录内含 `package.json` 且 `dsh.moduleFallback.targets` 键存在（`void 0` 之外都算，含 null/[]）→ 保留；
- 其余（真实目录/文件）→ 删除；空 @scope 壳 → 删除。

**修复（三处，各司其职）**：
1. **同步卫生（防再次入库）**：`sync-dsh.ps1` 把 `profiles/*/.dsh-module-fallback/` 作为**内置必备规则**（模板 + 已存在 `.gitignore` 幂等补写 `Ensure-BuiltinIgnoreRules`，不依赖 `gitignore_extra`）；`Sync-Push` 在 `git add` 前检测历史误跟踪（`git ls-files`）→ `git rm -r --cached`（**只动索引，工作区文件保留**）→ 本次提交带删除记录，副机拉取后自动清理。本次已在主力机同步仓库执行并推送（`fcbe48c`，12618 文件移出）。
2. **启动自愈（vdsh 双通道）**：新增 `vdsh/module_fallback.py::heal_module_fallback(data_dir)`（纯标准库），`vdsh/features/launch.py`（启动前）与 `dsh_cli.py`（转发前）都调用；只删「非链接非 proxy」条目，合法 junction/proxy 一律不动；失败不阻断启动（dsh 自身报错是兜底）。
3. **拉取指引**：`Sync-Pull` 脏区提示检测到 `.dsh-module-fallback` 变更时，给出「整体删除该缓存目录（dsh 自动重建）再 pull」的命令。

**关键坑提炼**：
- **junction ≠ 普通目录**：`os.path.islink()` 对 junction 返回 False（Python 3.8-3.12）；用 `os.lstat().st_reparse_tag` / `st_file_attributes & 0x400`（reparse point）或 `os.path.isjunction`（3.12+）判断；测试脚本同样踩过。
- **git 会把 reparse point 当目录递归提交**（非 TTY 也如此），所以「同步排除规则」必须在 allowlist 之外与 `.gitignore` **双保险**；已经入库的只能 `git rm -r --cached` 一次性移出（`--cached` 只改索引、不动工作区，主机 junction 安全）。
- **修复顺序**：先让 dsh 重建（启动自愈）再把仓库层面的删除提交同步过去；若副机先 `pull` 拿到删除记录，工作区的真实目录会被 git 正常移除（本次提交即如此）。

### 8.6 DSH 会话按「工作区绝对路径」组织：跨机同步「文件在、UI 不显示」（2026-09，已搁置）

**现象**：副机 `vdsh sync pull` 后 `sessions/` 数据文件齐备，但副机 DSH 界面没有主力机的聊天记录。

**根因（源码 + 磁盘证据）**：
- `sessions/` 目录键 = `projectKey(cwd)`：`packages/session/session-persistence-jsonl/src/format.ts`
  把工作区**绝对路径**（含盘符）折算成 `--slug--`（分隔符/盘符 → `-`，非安全字符 → `~XXXX`）。
  实测 `T:\Open-Source\dsh-launcher` → `sessions/--T-Open-Source-dsh-launcher--/`；
  中文路径 → `--T-~6742~4E03~6742~516B-…--`。
- `storages/workspace.json` 的 `tables.workspaces[].path` 也是**绝对路径**（`T:\…`、`C:\…`），
  workspace 的 sessionIds 归属绑定在这些路径键上。
- 副机 cwd 是另一台机器的路径（`C:\_TMP`…）→ `projectKey(cwd)` 与同步来的键集合**不重合**；
  副机按自己的键查询，主力机键区间不参与 → UI 空。Git 只能按路径名搬文件，**改不了键**。

**结论**：这不是同步脚本/launcher 的缺陷，是 DSH 数据模型「会话数据只属于本机工作区绝对路径」
与「跨机镜像」语义冲突；launcher 侧强修（键重命名/重映射/扁平化）成本 > 收益 → 功能已搁置
（2026-09 第三波收尾，见 `devlog.md` 第三波、`design.md` §5）。

**沉淀为排查顺序**：出现「文件在、UI 不显示」→ 先检查**路径相关键**（目录名是否 = `projectKey(cwd)`、
注册表 `path` 是否绝对路径、两端路径是否一致），再怀疑数据损坏。将来恢复该功能的候选路径：
DSH 按 workspace id（而非绝对路径）检索会话；副机把主力机路径重映射成相同绝对路径
（`subst`/junction 挂同盘符）；launcher 按 session id 聚合（不推荐，长期维护成本高）。
