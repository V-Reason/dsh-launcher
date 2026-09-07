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

## 2. 编码问题

### 2.1 Python 重定向到 gbk 文件时 UnicodeEncodeError

**现象**：子进程输出含 `✅`（U+2705，不在 GBK），经 `print` 写重定向 stdout 崩溃。
**解决**：入口统一 `sys.stdout/stderr.reconfigure(encoding="utf-8", errors="replace")`（`console.setup_streams`）。PS 侧 `[Console]::OutputEncoding = UTF8` 保证管道内是 UTF-8，两端一致。

### 2.2 非交互/重定向的动画静默

`Spinner` 以 `sys.stdout.isatty()` 判定；`say()` 非 TTY 直接 `print(line)`。因此**重定向输出绝不含 `\r`/控制字符**（有回归测试断言）。

### 2.3 sync-dsh.ps1 必须 UTF-8 带 BOM（2026-09 真实踩坑）

**现象**：`vdsh sync …` 报满屏 `UnexpectedToken`（PS 5.1「字符串缺少终止符/缺少"}"」），脚本 37/104/117/118/129 行全线报错。
**原因**：`sync_host()` 走 Windows PowerShell 5.1；`.ps1` 无 BOM 时按 ANSI（GBK）解码，中文注释/字符串全部乱码，括号引号错位 → 解析器在字符串中途崩掉（如动画帧串 `⠋⠙⠹…` 丢失收尾引号）。
**解决**：脚本保持 **UTF-8 with BOM**；本次即因用 UTF-8 无 BOM 的编辑器改写脚本丢掉了 BOM（`git diff` 只见首行多出 `﻿` BOM 字符）。launcher 侧 `vdsh/features/sync.py` 已在执行前检查 BOM，缺失时给出明确报错而非解析墙。

## 3. YAML 配置

### 3.1 双引号标量内反斜杠

`repo: "T:\deepseek-harness"`（文件里单反斜杠）→ 解析报 `found unknown escape character 'd'`。
**解决**：模板里路径用**裸标量**（无引号）；程序写值时统一 `json.dumps(value, ensure_ascii=False)`（JSON 字符串即合法 YAML 双引号标量，反斜杠会被转义为 `\\`）。

### 3.2 raw 字符串首字符坑（本项目真实踩到）

**现象**：`TEMPLATE = r"""\` + 换行 → 生成的文件首字符是**字面 `\`** → YAML 解析失败 → 全部配置回退默认（用户改的值全被忽略，且无任何错误指向模板）。
**解决**：raw 三引号串首行直接写内容（`r"""# vdsh.yaml...`）；禁止以 `\` 续行开头。这类「整体静默回退」最阴险——`vdsh config` 要能看到警告（现在 load_settings 会打印「无法解析」）。

### 3.3 文本补丁 vs 全量重写

`remote set` 若整体 dump 配置会毁掉用户注释。`patch_values`（Python）与 `Set-VdgConfigRemote`（PS）都做**行级替换**：匹配 `^空格+键:` 行、保留其余、值 JSON 引号化；键不存在则段内/文件尾追加。两份实现保持语义等价——**改动时须同步**。

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

**解决（launcher）**：启动时把 pwsh 命令输出全流重定向到 `%TEMP%\vdsh-web.log` → 轮询截获 URL 行作为就绪信号 → `requests.Session` 跟随 303 完成 token→cookie 交换 → 打开带 token 的 URL（浏览器首访换 cookie）、复用 Session 调 `/api/workspace.create`；`probe_harness` 识别 401 正文为「auth（已在运行）」。已运行实例若由启动器启动，日志里仍是当前进程的 token，可复用；否则只能提示用户用 DSH 窗口打印的 URL。

### 7.2 sync init 不持久化远端（「yaml 没生效」错觉）

**现象**：`vdsh sync init <URL>` 后 `vdsh config` 的 `sync.remote` 仍为空，但 push/pull 全部正常——git `origin` 才是实际来源，vdsh.yaml 的 remote 仅作无参回退，故「没生效」的观感与「正常」并存。
**解决**：`Sync-Init` 成功后调用 `Set-VdgConfigRemote`（与 `remote set` 同一文本级写入函数，保留注释）；重跑 init 传新 URL 会 set-url；无参 init 从 git origin 回填。两个入口（CLI/菜单）都走同一函数，不会再出现「只改一边」。
