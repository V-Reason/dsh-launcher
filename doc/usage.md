# 完整使用参考

日常用法见根目录 README「快速开始」；本文是详尽参考。

## 命令行

```text
vdsh [工作目录]                    启动 dsh web 并打开浏览器（默认功能）
    --tailnet xxx.ts.net          手机经 Tailscale 访问（自动加 --trusted-host）
    --sync                        启动服务前自动拉取 DSH 数据（sync pull）
vdsh build                         直接执行仓库构建（pnpm run build，带动画）
vdsh sync <子命令>                 数据同步（init/push/pull/status/remote；无参 = 交互菜单）
vdsh update <dsh | plugin>         更新 Harness 本体 / 更新 profile 插件（分开执行）
vdsh config                        查看生效配置
vdsh setup                         重跑首次配置向导
vdsh doctor                        环境自检（只读）
vdsh help / -h / --help          用法
```

### 启动（launch，默认功能）

| 情况 | 行为 |
|---|---|
| 全新启动（端口空闲） | 可选 `--sync`/`launcher.auto_pull` 自动 pull → 按需构建 → 弹最小化 pwsh 窗口启动 → 就绪轮询 → 打开浏览器 |
| 实例已在运行 | 注册当前目录到该实例（RPC），打开浏览器，跳过同步与构建 |
| 端口被占但未就绪 | 等待其完成（默认 30s 预算），而非拉起第二个实例 |

要点：

- 服务窗口**最小化启动、不抢焦点**（CREATE_NEW_CONSOLE + SW_SHOWMINNOACTIVE）；点任务栏图标呼出看日志 / Ctrl+C 停止。node 退出后窗口自动关闭。
- **启动失败即时反馈**：启动前预检 node / CLI 产物（缺失直接报错，不再空转到超时）；子进程在就绪前退出时立即终止并打印日志尾部（用 `dsh web` 直启能看到报错、用 vdsh 却空转等待的问题由此修复）。
- 就绪判定以页面中的 `window.__DSH_BOOT__` 引导清单为主标记（0.1.1 起标题品牌化，旧标题 `DeepSeek Harness` 仅作兼容回退）。
- 浏览器只由启动器在就绪后打开**一次**：服务端以 `--no-open` 关闭其自带自动打开（0.1.1 起 web app 默认自开，会重复）。
- 工作区种子：经 `--patch <seed.yml>` 注入 `workspace-seed.mjs`（启动器目录下生成），把启动目录幂等注册为 Web UI 工作区；`launcher.workspace_seed: false` 可关闭。
- 实例已在运行：经 Connection RPC（`/api/workspace/create`）把当前目录注册进该实例（幂等，成功无输出、失败只告警）；**仅在探测到运行实例确实不信任所配 tailnet 域名时**才提示「未带 --trusted-host」（Host 围栏 403 判定，见 experience.md §7.3）。
- 构建：产物缺失或 `apps/cli/src`、`apps/web/src` 的 mtime 新于产物时，询问 `pnpm run build`；非交互输入（EOF）默认不构建。
- 动画：启动就绪、构建、`sync push/pull/init`、`--sync` 共用同一款转轮动画（帧 + 秒数，8fps 默认）；非 TTY/重定向自动静默。经 vdsh 调用同步时，脚本进度（fetch/push 的 git 对象传输、步骤行）逐行流式显示，转轮消息跟随最近一步。

### 配置（vdsh setup / config）

首次运行执行默认功能时若 `vdsh.yaml` 不存在，运行交互向导（提问 DSH 安装目录——必填校验、数据目录、Tailscale 域名）；非交互环境自动生成默认模板并提示。`vdsh setup` 可随时重跑；`vdsh config` 打印生效值与来源标注。

## 配置（vdsh.yaml）

位于启动器目录（`T:\Open-Source\dsh-launcher\vdsh.yaml`），首次运行自动生成带注释模板，**删除某行 = 该键回退内置默认**。优先级：命令行参数 > 环境变量（`DSH_REPO`/`DSH_TAILNET_HOST`/`DSH_HOME`）> vdsh.yaml > 内置默认。

| 键 | 默认 | 说明 |
|---|---|---|
| `launcher.repo` | `T:\deepseek-harness` | DSH 安装目录（Harness 仓库根，须含 package.json）；向导/`DSH_REPO` 优先；配置无效时自动探测本机候选（`C:\deepseek-harness` 等） |
| `launcher.tailnet` | （空） | 默认 Tailscale 域名（`--tailnet`/`DSH_TAILNET_HOST` 优先） |
| `launcher.startup_timeout_seconds` | 180 | 服务就绪等待上限 |
| `launcher.starting_budget_seconds` | 30 | 端口被占但未就绪的等待上限 |
| `launcher.poll_gap_seconds` | 0.5 | 就绪轮询间隔 |
| `launcher.open_browser` | true | false = 就绪后不自动开浏览器（仍打印地址） |
| `launcher.workspace_seed` | true | false = 不注入工作区种子插件 |
| `launcher.auto_pull` | false | true = 每次启动前自动拉取 DSH 数据（等同每次加 `--sync`） |
| `animation.fps` | 8 | TTY 动画帧率（1-60） |
| `animation.frames` | `⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏` | 动画帧序列 |
| `animation.quiet` | true | true = TTY 下折叠 pnpm 的低价值行（进度/重试/统计）并改打 `→ …` 进度行，转轮只显示当前任务与秒数；false = 全量输出（排障用）；非 TTY 恒为全量 |
| `sync.data_dir` | （空） | DSH 数据目录；支持 `~` 展开（如 `~/.dsh`）；空 = `DSH_HOME` → `~/.dsh` |
| `sync.remote` | （空） | 默认远端；`vdsh sync init <URL>`（成功时）与 `remote set` 都会写入此处，无参 `vdsh sync init` 时使用 |
| `sync.allowlist` | 9 项列表 | 同步范围（相对数据目录）；含用户预设 `.agent-presets/` 与全局配置层 `cordis.patch.yml`，不存在自动跳过 |
| `sync.gitignore_extra` | （空） | 追加进自动生成的 `.gitignore`；已有文件按缺失行幂等补写。另有**内置必备规则**（`profiles/*/.dsh-module-fallback/`）与 `gitignore_extra` 无关、始终存在 |
| `sync.commit_name` | `DSH Sync` | 提交者身份（两端必须一致） |
| `sync.commit_email` | `dsh-sync@local` | 同上 |
| `sync.timeout_seconds` | 0 | 0 = 不限时；>0 时 fetch/push 等超过即终止（按硬失败退出码 1） |

未知键/非法值：警告并回退该键默认；YAML 整体解析失败：全部回退默认并提示。

### 配置生效范围（重要）

`sync.*`、`animation.*` 通过「环境桥」注入 `sync-dsh.ps1` **仅在经 `vdsh sync …` / `vdsh --sync` 调用时生效**；直接运行 `sync-dsh.ps1` 或双击 `sync-dsh.cmd` 时脚本使用内置默认（无 vdsh.yaml 感知）。若需直接路径也生效，调用前手动设置 `DSH_HOME` 等环境变量亦可（见 experience.md 的环境桥说明）。

## 手机访问（Tailscale Serve）

1. Tailscale 控制台启用 Serve（按 `tailscale serve` 提示链接）。
2. PC 执行一次（配置持久化）：`tailscale serve --bg http://127.0.0.1:3080`。
3. 启动带域名：`vdsh --tailnet v-reason-tx.tail1145de.ts.net`（自动加 `--trusted-host`，否则手机端 API 一律 403）。
4. 手机：Tailscale App 登录同账号 → 打开 `https://v-reason-tx.tail1145de.ts.net`。

> 安全：`--trusted-host` 意味着同 tailnet 内可访问该域名的设备拥有完整控制权；不要开 Funnel。

## 数据同步（vdsh sync）

```powershell
vdsh sync status                 # 状态：领先/落后、待推送文件（逐行列出）、最近提交
vdsh sync push                   # 收工前：暂存变更 → 提交 → 推送
vdsh sync pull                   # 开工前：快进优先，分叉时合并；冲突给出指引
vdsh sync init [URL]             # 一次性初始化（无参 + 交互终端 → 配置向导；
                                 #   非交互回退 sync.remote；成功后把地址写入 vdsh.yaml）
vdsh sync remote [set <URL>]     # 查看/设置远端（更新 git origin 并写入 vdsh.yaml）
vdsh sync                        # 交互菜单（[1-5] 状态/推送/拉取/初始化/远端）
vdsh --sync                      # 启动服务前自动 pull（仅实例未运行时；失败只告警）
                                 # 也可配置 launcher.auto_pull: true 每次启动自动 pull
```

- **`vdsh sync init` 无参（交互终端）= 副机接入向导**：依次询问 DSH 安装目录（`launcher.repo`，校验含 `package.json`）、DSH 数据目录（`sync.data_dir`，校验绝对路径/可创建）、远端裸仓库地址（`sync.remote`，`file://` 与本地盘路径归一化并校验存在性与裸仓库形态，`http(s)` 仅语法校验），确认后写入 vdsh.yaml 再执行初始化；每项回车用默认值、`s` 跳过、Ctrl+C 取消。跨机复制 launcher 时残留的另一台机器路径（如 `T:/deepseek-harness`、`file:///T:/DataBase/...`）会在向导中明确提示并默认为本机探测值。
- 设计背景（为什么插件做不到）、同步范围、冲突处理见 `dsh-data-git-sync/docs/native-git-sync.md`；小白教程见 `dsh-data-git-sync/docs/beginner-guide.md`。
- 输出风格：步骤 `→ 动作`、成功汇总 `✓ 结果`、错误 `✗ 原因`、警告 `⚠ …`；数据目录按 `~/.dsh` 短形式显示一次，不再输出完整路径清单。`status` 的「待推送」按行列出（最多 10 条，其余给截断提示）；`push` 反映推送内容（提交/文件数），`pull` 同样反映拉取内容（远端新增 N 提交 · M 文件），无更新时直接提示「已是最新」并跳过合并。fetch/push 以 `--progress` 执行并经 vdsh 逐行流式显示实时进度。
- 插件与插件配置在默认同步范围内：profile 插件（`profiles/web/` 清单文件与 `cordis.patch.yml`）、用户预设（`.agent-presets/`）、全局配置层（`cordis.patch.yml`）、插件运行数据（`storages/`）与设置（`settings.yaml`）；`profiles/web/node_modules/` 与 `profiles/*/.dsh-module-fallback/` 不入库（后者是内置必备规则，Windows git 会把 junction 展开入库导致副机启动报错，详见 `experience.md` §8.5），副机需 `pnpm install`、模块回退缓存由 dsh 自动重建。API 密钥（`.credentials.yaml`）永不入库。
- ⚠️ **聊天记录（`sessions/`）跨机可见性已搁置（2026-09 第三波）**：文件会随仓库同步，但 DSH 按本机工作区绝对路径组织会话（`sessions/<projectKey(cwd)>/`），跨机路径不同即对不上，副机 UI 不显示主力机聊天记录。这是 DSH 数据模型限制，非同步脚本可修；恢复条件见 `design.md` §5。不影响设置/插件/预设/附件/记忆的同步。
- 数据目录：`DSH_HOME` → `sync.data_dir` → `~/.dsh`；`DSH_HOME` 与 `sync.data_dir` 均支持 `~` 写法，使用时自动展开为主目录绝对路径。
- 退出码语义：`0` 成功 / `1` 硬失败（含 timeout 超时）/ `2` 用法错误 / `3` 被阻塞（脏工作区、冲突、远端 main 未建立）/ `4` 未初始化（可跳过）；`vdsh --sync` 依此只告警、不阻塞启动。
- 规则：**两台电脑不要同时干活**：A 收工 `push` → B 开工 `pull`；DSH 空闲时再同步。

## 更新（vdsh update）

```powershell
vdsh update dsh                   # 更新 Harness 本体：git fetch + 合并 upstream + pnpm install + build
vdsh update plugin [web]          # 更新 profile 插件依赖（默认 web）：dsh plugin --profile <p> update --latest
```

- **分开执行**：`dsh` 更新 Harness 检出（`launcher.repo`，须为 git 检出且配置了上游分支）；`plugin` 更新 `$DSH_HOME/profiles/<p>` 的插件依赖（官方 dsh CLI 通路，新版本声明 `dsh.bundle` 会自动激活为 profile 层）。
- 两者在 **dsh web 运行中**都会询问（`[y/N]`，非交互/EOF 默认中止）：Windows 下运行中的服务会锁定文件，且更新的版本需要重启才生效。
- `update dsh`：仓库有未提交改动也会询问确认；动作顺序为 fetch → merge（本地有提交时常规合并，冲突中止并提示）→ `pnpm install` → `pnpm run build`，全程带动画，结束显示新旧版本号。
- **失败时看什么（不受输出简约约束）**：`update dsh` 的每一步都会给出 exit code 与下一步，不会只留一句「请手动检查」。
  - `git fetch` 失败 → 指向网络/远端可达性；合并失败分两种：**本地有领先提交**（真冲突，提示 `git status`）与
    **本地无领先提交却无法快进**（上游被 force push/重建，提示 `git log --oneline HEAD..<upstream>` 与
    `git reset --hard <upstream>` 两条路），不会再笼统地说「请检查冲突」。
  - `pnpm install` 失败 → exit code + 是否命中网络类指纹（指向 `HTTPS_PROXY`）。
  - `pnpm run build` 失败 → 补打**末尾 15 行**子进程输出（构建工具的报错都在尾部）+ **完整构建日志路径**
    `%TEMP%\vdsh-build.log`（成功即删，只保留最近一次失败现场），并点明「代码已更新到最新、仅构建未完成」，
    避免把「已拉到新代码但构建没过」误读成「更新没生效」。
  - 命令本身起不来（pnpm 被安全软件拦截/文件被占用）→ `vdsh ✗ 无法启动命令（…）`，不再是 Python traceback；
    子进程退出后若后代进程仍抱着输出句柄不放，10 分钟无输出即按卡死终止（不会永久挂住）。
- `update plugin` 使用 `update --latest`（忽略 package.json 版本范围，取各插件最新版并回写）；结束后只提示「重启 dsh web 后生效」。
  （副机同步不含在提示里：需要时自己 `vdsh sync push` 把新的 `package.json`/`pnpm-lock.yaml` 推过去。）
- **更新时看到什么**：运行期间只有两种行——转轮行 `⠋ 更新插件 47s`（**当前任务性质 + 已等待秒数**，原地刷新）
  与 `→ …` 进度行（把 pnpm 的进度/重试翻译成一句人读的进度，如 `→ 解析 174 · 复用 7`、`→ 网络重试 1`，
  按 3 秒节流，长停滞期也看得到动静）。其余 pnpm 噪声（统计行、成功回执、peer 提示等）折叠不打印；
  命令真失败时，被折叠的行会原样补打，排查信息不丢。要看逐行全量：`animation.quiet: false`，或把输出重定向到文件（非 TTY 恒为全量）。
- **更新后校验（结论可信）**：结束打**两行**——结论 `vdsh ✓ 插件已是最新（5 个依赖校验通过）`，耗时单独一行 `vdsh · 用时 1m46s`
  （有更新时结论列出 `名字 旧→新`，git 依赖显示成 `0.7.0@da602d1→0.8.0@b1c9f2e`，所以「版本号没变但 commit 变了」也算更新）。
  判定用三重证据：`package.json` 声明 ↔ `pnpm-lock.yaml` 记录 ↔ 磁盘 `node_modules/.modules.yaml` 的解析身份。
  生效方式分四态——`profile 层`（声明 `dsh.bundle.patch` 且已在 `dsh.profile.bundles`）/ `预设挂载`（未声明 bundle 但被预设引用，
  如 `dsh-study-buddy` 由 `~/.dsh/.agent-presets/study` 挂载）/ `普通依赖`（未见引用，仅作库）/ `未激活`（声明了却没进 bundles → **不会生效**）；
  **逐插件明细在 `vdsh doctor` 里看**，结论行不展开解释。
  校验不通过（依赖缺失、未激活、磁盘与 lockfile 不一致）会逐条 `vdsh ✗` 并以退出码 1 结束；降级情形（缺 PyYAML、
  无 `hoistedLocations`）只 `vdsh ⚠`，不误判。若 `dsh plugin` 退出码为 0 却完全没有 pnpm 运行标记，会补一句
  `vdsh ⚠ 未见 pnpm 运行标记：本次可能未真正执行更新`——此时结论只说明状态没变。
- `vdsh doctor` 可体检已安装插件与 DSH 版本是否适配。

## 输出约定与常见告警（所有命令同一套）

vdsh 的终端输出只有四种东西——**转轮、进度、节点、结论**；解释性长文归文档与 `vdsh doctor`。

| 元素 | 形式 | 含义 | 归属 |
|---|---|---|---|
| 转轮 | `⠋ 更新插件 47s` | 当前**任务性质** + 已等待秒数（原地刷新；不显示 pnpm 内部计数） | vdsh |
| 进度 | `→ 解析 174 · 复用 7` / `→ 网络重试 1` | 子进程步骤，或翻译后的 pnpm 进度（按 3 秒节流） | vdsh 转发/翻译 |
| 节点 | `vdsh · 更新 5 个插件依赖（profiles/web）…` / `vdsh · 用时 1m46s` | 开始做什么 / 收尾数据 | vdsh |
| 结论 | `vdsh ✓ 插件已是最新（5 个依赖校验通过）` / `vdsh ✗ …` / `vdsh ⚠ …` | 成功 / 失败 / 提示（成功结论 + 耗时是两行） | vdsh |
| （第三方） | `> dsh@0.1.3 build` / `vite …` / `1a2b3c4..5d6e7f8` / `[WARN] …` | 子进程的真实输出：构建工具、git、pnpm 的自有格式 | 各自工具 |

命令对应的形态：

- `vdsh` / `vdsh build` / `vdsh update dsh` / `vdsh update plugin`：转轮 + `→` 进度 + `vdsh ✓` 结论 + `vdsh · 用时 …`。
- `vdsh sync <子命令>`：由 `sync-dsh.ps1` 自己打印（`→ 步骤`、`✓/⚠/✗ 结论`、`  耗时 2.3s`）——它是可脱离 vdsh 独立运行的脚本，
  保留自有文案；vdsh 只把转轮文案对齐成任务性质（`推送 DSH 数据`），并在启动前自动拉取时补一句结论。
- **报告类**（`vdsh config`、`vdsh doctor`、`vdsh sync status`）：不打转轮，直接用各自的分组/`✓ ✗ ⚠ —` 报告格式。
- **向导类**（首次运行向导、`vdsh setup`、`vdsh sync init` 无参、构建前确认、更新前确认）：保留提问与缩进提示，
  这类是人机对话，不套用上述格式；`dsh.cmd`（官方 CLI 转发壳）同理，前缀用 `dsh`。

失败时**诊断不受此约定限制**：exit code、下一步命令、`vdsh ✗` 明细、被折叠行的补打都会完整给出。

### 输出里的 `[WARN]` 怎么看

`[WARN]` 前缀是 **pnpm 自己打印的**；vdsh 的输出一律带上表的 `vdsh ·`/`vdsh ✓`/`vdsh ✗`/`vdsh ⚠` 或转轮、`→` 前缀（TTY 下这些 `[WARN]` 通常已被折叠成 `→ 网络重试 N` 之类的一行进度）。两类常见 `[WARN]` 都属预期，更新已成功：

- `[WARN] HEAD https://github.com/<owner>/<repo> error (ECONNRESET|ETIMEDOUT). Will retry in … retries left.`
  —— pnpm 在解析 `github:` 依赖时探测「仓库是否公开」（`HEAD https://github.com/x/y`）失败。该探测失败被 pnpm 吞掉，
  只会让它改用 git 解析（比 codeload tarball 慢），**不影响结果**；重试退避 500ms→1s 也与 pnpm 默认 `fetch-retries: 2` 一致。
- `[WARN] Issues with peer dependencies found. Run "pnpm peers check" to list them.`
  —— profile 的 peer 缺项是 DSH 的设计（dsh 生成的 `profiles/<p>/pnpm-workspace.yaml` 为 `nodeLinker: hoisted` +
  `autoInstallPeers: false`），这些 peer（`@deepseek-ai/*`、`react` 等）由 Harness 安装层 `$DSH_HOME/profiles/node_modules` 提供，
  不装进 profile 才对（否则会出现重复的 cordis 实例）。看明细：`cd $env:DSH_HOME\profiles\web; pnpm peers check`。

这两个 `[WARN]` 在 TTY 下不会逐行出现——它们被折叠成 `→ 网络重试 N` 之类的一行进度；想逐行看全量：`animation.quiet: false`（或把输出重定向到文件，非 TTY 恒为全量）。命令真失败时，被折叠的行会原样补打，不会丢排查信息。

### `github.com:443` 不可达怎么办

本机实测（2026-09）只有 `github.com:443` 被阻断，`github.com:22`/`codeload.github.com:443`/`registry.npmjs.org:443` 均正常——
这正是上面 HEAD 重试的来源。判别：

```powershell
Test-NetConnection github.com -Port 443        # False = 被阻断
Test-NetConnection codeload.github.com -Port 443
node -e "fetch('https://github.com/omdsh-dev/dsh-at-file',{method:'HEAD'}).then(r=>console.log(r.status)).catch(e=>console.log(e.cause?.code||e.name))"
```

- 不处理也能用：git 依赖会走 SSH 解析（`pnpm-lock.yaml` 里记为 `git+ssh://git@github.com/…#<sha>`），更新照常成功，代价是每次多花十几秒。
- 想消除：给 pnpm 配代理（`$env:HTTPS_PROXY='http://127.0.0.1:端口'` 或 `pnpm config set https-proxy …`），git 需要时再配 `git config --global http.proxy`。
- 放行后首次 `update` 可能把这几个 git 依赖的 lockfile 解析从 `git+ssh` 改写为 codeload tarball（正常），记得 `vdsh sync push` 同步给副机。

## 退出码（launcher）

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 硬失败（更新流程失败、构建/插件校验失败等运行期错误） |
| 2 | 用法/环境错误（参数错误、仓库未找到等） |
| 3 | 构建失败（`pnpm run build` 非 0 退出） |
| 4 | 依赖缺失（requests / pyyaml / node / pnpm） |
| 5 | pwsh（PowerShell 7）缺失 |
| 6 | 端口被占用但未识别为 Harness |

## 环境变量

| 变量 | 作用 |
|---|---|
| `DSH_REPO` | Harness 仓库根目录（默认：`DSH_REPO` → vdsh.yaml → 本机候选探测） |
| `DSH_TAILNET_HOST` | Tailscale 域名（未传 `--tailnet` 时生效） |
| `DSH_HOME` | DSH 数据目录（默认 `~/.dsh`） |

## 文件

| 文件 | 说明 |
|---|---|
| `vdsh_launcher.py` | 入口薄壳 |
| `vdsh/` | 实现包（分层见 design.md） |
| `vdsh.cmd` | 命令行薄壳（CMD / PS 5.1 / PS 7） |
| `dsh.cmd` | 官方 DSH CLI 转发壳（经 `dsh_cli.py` 按 DSH_REPO → vdsh.yaml → 本机候选解析仓库，不再硬编码路径） |
| `vdsh.yaml` | 用户配置（生成物，不入库） |
| `workspace-seed.mjs` / `seed.yml` | 工作区种子（生成物，不入库） |
| `dsh-data-git-sync/` | 同步子工具（sync-dsh.ps1/.cmd + docs） |
| `doc/` | 本目录：设计/开发/经验文档 |
