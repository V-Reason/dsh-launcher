# 设计与架构

## 1. 定位

**vdsh 是一个「多功能启动器」，专门实现 dsh 插件做不到的事。** 判据：

> 任何需要发生在 DSH 进程之外的操作，原则上都应放在 vdsh，而不是插件里。

对照表：

| 需求 | 归属 | 理由 |
|---|---|---|
| 一键启动 dsh web（窗口/进程/就绪/工作区/信任围栏） | vdsh launch | 插件只能挂载在已运行的 Web UI 会话上，无法管理进程与启动期 |
| 仓库构建检测与执行 | vdsh build | 仓库侧文件与 pnpm 生命周期在 DSH 进程之外 |
| Harness 本体 / profile 插件更新 | vdsh update（dsh / plugin 分开） | git pull、依赖安装与构建都在 DSH 进程之外；插件更新走官方 dsh CLI 通路 |
| DSH 数据双机同步 | vdsh sync（直通原生 Git） | 插件方案会让同步命令自身写入 `sessions/`，产生无法收敛的自指残差（旧 `dsh-data-sync` 插件因此作废） |
| 终端体验（动画/向导/自检/统一配置） | vdsh 共享层 | 纯终端 UX，无需 Web UI |

**原则**：零修改 Harness 仓库源码（`DSH_REPO` 只读引用）；一切用户侧状态（config/seed/生成物）放启动器目录；`dsh-data-git-sync/` 是独立子工具，即使脱离 vdsh 也能单独工作（直接脚本/双击菜单）。

## 2. 分层

```
vdsh_launcher.py          入口薄壳（sys.path 引导，转发 vdsh.app.main）
vdsh/
  ├─ app.py               分发器：控制台设置 → 首次运行引导 → 配置加载 → 功能注册表分发
  ├─ 共享层
  │   config.py           常量、路径、退出码（唯一常量来源）
  │   settings.py         vdsh.yaml：模板/加载/校验/合并/文本级补丁/报告/生效值计算
  │   console.py          UTF-8 流设置、say/step/warn/die/ask（文案与退出码统一）
  │   spinner.py          转轮动画 + run_child_progress（流式子进程 + 超时）
  │   bootstrap.py        首次运行引导（交互向导；本包与功能 setup 共用）
  └─ 功能层 features/
      launch.py           启动（默认功能）
      build.py            构建
      sync.py             数据同步（直通 sync-dsh.ps1）
      update.py           更新（dsh 本体 / profile 插件）
      config.py           配置查看
      setup.py            配置向导
      doctor.py           环境自检
```

依赖方向：功能层 → 共享层；功能与功能之间**不互相 import**（例外：launch 长驱动按需调用 build/sync 的纯函数、update 延迟 import build/launch 的 `run_build`/`probe_harness`、doctor 延迟 import launch 的 `probe_harness`——均属组合调用而非耦合）。

### 功能协议

每个功能模块暴露三个符号：

```python
NAME = "sync"                          # 注册名
SUMMARY = "一句话说明"                 # 用于 --help / 注册表
def run(argv, settings) -> int: ...    # 返回进程退出码
```

在 `vdsh/features/__init__.py` 的 `FEATURES` 注册一行即对 CLI 可见（`vdsh <NAME> [args...]`）。未命中注册表 → 默认 launch。

## 3. 关键机制

### 3.1 配置系统（vdsh.yaml）

- **模板 = 默认值的唯一展示**：`settings.TEMPLATE` 与 `settings.DEFAULTS` 手工同步（均有注释），首次运行自动生成；`vdsh config` 可整体查看。
- **校验**：`VALIDATORS` 表定义每键的 (校验器, 空串是否视为未设置)；未知段/键/非法值 → 警告 + 回退该键默认；YAML 解析失败 → 全部回退默认。PyYAML **惰性导入**：仅配置文件存在时才需要（缺失时给出安装提示并退出）。
- **文本级补丁 `patch_values()`**：只替换对应 `键: 值` 行（保留注释与其余内容），值经 `json.dumps` 转义为合法 YAML 双引号标量。设计理由：`remote set` 等命令要持久化配置，全量重写会毁灭用户的注释；行级替换 + 段级追加足够健壮。
- **优先级**：CLI > 环境变量（`DSH_REPO`/`DSH_TAILNET_HOST`/`DSH_HOME`）> vdsh.yaml > 内置默认；生效值计算集中在 `settings.effective_*`。

### 3.2 环境桥（Python → PowerShell 5.1）

PS 5.1 无法解析 YAML，也不引入新依赖。方案：**Python 解析配置，经子进程环境变量传给 sync-dsh.ps1**：

| 环境变量 | 含义 |
|---|---|
| `DSH_HOME` | 仅当外部未设且配置了 `sync.data_dir` 时注入 |
| `VDG_SYNC_ALLOWLIST` | JSON 数组（脚本 `ConvertFrom-Json` + 展平一层，见 experience.md） |
| `VDG_SYNC_GITIGNORE_EXTRA` / `VDG_SYNC_COMMIT_*` | 文本 |
| `VDG_SYNC_TIMEOUT` | 秒（0/缺 = 不限） |
| `VDG_ANIMATION_FPS` / `VDG_ANIMATION_FRAMES` | PS runspace 动画参数 |

**关键性质**：直接调用 `sync-dsh.ps1`（无 vdsh 桥）→ 无这些变量 → 完全回退脚本内置默认。这保证了子工具剥离 vdsh 后行为不变，且 PS 动画层在 stdout 被 Python 捕获时自动静默（`[Console]::IsOutputRedirected`）。

### 3.3 动画（双层互斥设计）

- **Python 层**（launch 就绪 / build / 经 vdsh 的同步）：`Spinner` 线程化重绘 `\r帧 消息 秒数`；`say()` 以「锁 + 暂停事件」把动画行与子进程输出行串行化，动画让位到下一行继续。`run_child_progress` 捕获子进程 stdout+stderr（UTF-8）逐行流式输出，动画与数据行互不覆盖；可选超时（守护线程 `proc.wait(timeout)` → `taskkill /T /F` 失败回退 `kill()`）。
- **PS 层**（直接调用 / 双击菜单）：`Invoke-GitSpinner` 把 git 放进独立 runspace，主线程 `BeginInvoke` + `IsCompleted` 循环重绘同款式动画，结束时一次性打印 git 输出；**仅当 stdout 未重定向时启用**（经 vdsh 调用时上层已动画，内层退让）。非动画分支（stdout 被捕获，即经 vdsh 调用）对 git 输出**逐行流式透传**，`fetch`/`push` 带 `--progress`——长操作的实时进度（对象传输/计数）在两种调用方式下都可见，不再缓冲到命令结束；Python 层 `Spinner.say()` 收到 `→ 步骤` 行时刷新转轮消息，长等待期间显示当前动作。
- 非 TTY/重定向：两层都自动静默，只留纯文本流。

### 3.4 启动状态机

```
probe: idle ──→ 全新启动（--sync 拉取 → 按需构建 → spawn 最小化 pwsh 窗口 + --patch/--trusted-host/--no-open）
       │                        └→ 就绪轮询（window.__DSH_BOOT__ 标记）→ 打开浏览器
       └─→ ready: 已运行 → RPC 注册工作区 → 打开浏览器（跳过同步/构建）
       └─→ starting: 端口被占但未就绪 → 等待就绪（30s）→ 同上；超时 → 退出码 6
```

设计要点：就绪标记用 `__DSH_BOOT__`（品牌无关，0.1.1 标题改版后仍稳定）；服务端 `--no-open` 保证只有 launcher 打开一次浏览器；`--patch` 必须位于任何 app 参数之前（位置参数透传规则）。

### 3.5 首次运行引导

- **触发**：仅默认功能（launch）首次运行时（vdsh.yaml 不存在）且流为交互（stdin TTY）；`vdsh sync/config/setup/doctor` 首次运行只生成模板。
- **降级**：非交互 / EOF / Ctrl+C → 生成默认模板 + 提示，不阻塞任何命令。
- **内容**：DSH 安装目录（必填，校验 `package.json`，无效重试 3 次后警告接受）、数据目录、Tailscale 域名。
- **可重入**：`vdsh setup` 复用同一实现（bootstrap.run_setup），模板 + patch_values 写入，注释不丢。

### 3.6 同步子工具

- 进程外原生 Git：`sync-dsh.ps1`（PS 5.1 兼容，UTF-8 BOM 文件）持有全部数据操作；vdsh 只做配置注入、动画承载与退出码透传。
- 退出码语义 `0-4` 是 vdsh 与脚本的契约（`vdsh --sync` 依赖 3/4 分支只告警不阻断）。
- 配置面（allowlist/身份/.gitignore/远端/超时）全部可经 vdsh.yaml 控制，脚本缺省值即 vdsh.py 内建造的默认。
- 交互面：`vdsh sync init` 无参（TTY）→ 配置向导（repo/data_dir/remote，见 features/sync.py）；`dsh.cmd` 转发壳经 `dsh_cli.py` 按 DSH_REPO → vdsh.yaml → 本机候选解析仓库路径，不再硬编码（跨机复制 launcher 自愈）。
- 启动失败面：launch 启动前预检 CLI 产物与 node；`wait_until_ready` 持有进程句柄，就绪前子进程退出 → 立即失败并给日志尾部（不再空转到超时）。

## 4. 演进决策记录

| 决策 | 原因 |
|---|---|
| 改名 `dsh` → `vdsh` | 与官方 Harness CLI 的 `dsh` 命令在 PATH 冲突 |
| 单文件 → 包 + features 注册表 | 功能增多后按「一件事一个模块」解耦，注册一条命令即可上线 |
| sync 用原生 Git 而非插件 | 自指残差问题（旧 dsh-data-sync 插件已废弃） |
| PS 侧持久化 remote 用文本补丁 | 保留用户注释；两份实现（Python patch_values / PS Set-VdgConfigRemote）逻辑等价 |
| ps1 用 `powershell`（5.1）作宿主 | 脚本声明 5.1 兼容；无需 PS 7；经 vdsh 时编码/动画由 Python 层控制 |
| 进度用 git `--progress` + PS 流式透传（2026-09） | git 本身即权威进度；PS 管道天然逐行，无需字节级统计/网络探测；PS 层动画分支（直接终端）保持 runspace 缓冲不变 |
| sync init 向导放 Python 层（2026-09） | vdsh.yaml 的读取/校验/`patch_values` 属 launcher 域；脚本侧只管 init 本体，退出码 0-4 契约不变 |
| dsh.cmd 动态解析仓库（2026-09） | 硬编码绝对路径在跨机复制时必挂；解析链 DSH_REPO → vdsh.yaml → `REPO_CANDIDATES`，显式 env 永不覆盖 |
| 启动失败即时反馈（2026-09） | `-NoExit` 使子进程崩溃后句柄仍存活 → 空转到超时；去掉后靠 `proc.poll()` 检测 + 日志尾部，失败不再无反馈 |
| 模块回退目录按 app-boot 规则自愈（2026-09） | git 把 junction 展开入库 → 副机检出真实目录 → dsh 报「not a symlink or dsh-managed module proxy」。`heal_module_fallback` 严格复制 ensureSymlink 判定（链接/proxy 保留，其余删除），launch 与 dsh_cli 双通道调用；比报错驱动的手工删除可预期 |
| `.dsh-module-fallback` 列入内置 ignore 规则（2026-09） | 该目录是 dsh 可再生缓存（纯本机链接/proxy），同步必有污染；不作为 `gitignore_extra` 交给用户配置，避免「忘配」复发；`Sync-Push` 自动 `git rm -r --cached` 历史误跟踪（只动索引） |

## 5. 已知边界

- `launcher.port` 不可配置：dsh web 端口未被验证可改，避免虚假开关。
- 直连 `sync-dsh.ps1` 不读 vdsh.yaml（环境桥只在 vdsh 调用时注入）——文档已注明（usage.md「配置生效范围」）。
- 构建行为不区分「需要询问」与「强制」：launch 内按需询问、`vdsh build` 直接构建，属有意差异。
- `REPO_CANDIDATES`（config.py）是**硬编码候选列表**：换安装位置/机型需追加候选（现含 `C:\deepseek-harness`、`T:\deepseek-harness`）；显式 `DSH_REPO` 优先级始终最高。
- `vdsh sync init` 无参仅 **TTY** 进交互向导；非 TTY 维持「回退 sync.remote / 用法错误」。向导校验是提示式（不截停），用户可带 URL 直跑绕过。
- 启动失败检测只覆盖 **launcher 自己拉起的进程**（spawn_server 的句柄）；「starting」分支无句柄，仍是 30s 预算 + 超时提示。
- `dsh_cli.py` 与 `sync-dsh.ps1 Get-VdgConfigRemote` 是**两份文本级读取实现**（剥注释 → JSON 反解），须保持同步（同 `patch_values`/`Set-VdgConfigRemote` 约定）。
- init 的 fetch 失败仍返回 0（「origin 暂不可达，可稍后再试」）；严格失败语义在 pull/push（退出码 1）。
