# vdsh Launcher

**在 DSH 进程之外，补齐 dsh 插件做不到的事。** vdsh 是一个便捷多功能启动器：在任意目录输入 `vdsh` 即可启动 DeepSeek Harness Web、管理仓库构建、在双机之间同步 DSH 数据、配置与自检——这些操作全部发生在 DSH 进程之外，因此不受「插件只能挂在 Web UI 会话上」的限制。

## 为什么需要它（插件做不到什么）

| 功能 | 为什么插件做不到 |
|---|---|
| 一键启动 dsh web | 进程/窗口管理、端口探测与就绪轮询、工作区注册（RPC）、Tailscale 信任围栏都由 launcher 在进程外完成 |
| 构建检测与执行 | 仓库侧文件与 `pnpm` 操作在 DSH 进程之外，launcher 先于启动完成时效检测 |
| DSH 数据双机同步 | 必须位于 DSH 进程之外（原生 Git）——插件方案会让同步命令自身写入 `sessions/`，产生无法收敛的自指残差（旧 `dsh-data-sync` 插件因此作废） |
| 终端体验 | 等待动画、交互向导、环境自检、配置统一管理 |

> 命令原名 `dsh`，与官方 Harness CLI 的 `dsh` 命令冲突（PATH 中会互相抢），已改名 `vdsh`。

## 功能总览

```text
vdsh [工作目录]                    启动 dsh web 并打开浏览器（默认功能）
vdsh build                         直接执行仓库构建（pnpm run build，带动画）
vdsh sync <子命令>                 DSH 数据同步（直通 sync-dsh.ps1；无参 = 交互菜单）
vdsh config                        查看生效配置
vdsh setup                         重新运行首次配置向导
vdsh doctor                        环境自检（只读）
```

## 安装

依赖：Python 3（`pip install requests pyyaml`）、PowerShell 7（`pwsh`）；数据同步（`vdsh sync`）另需 Git（`git` 在 PATH 中，Windows PowerShell 5.1 为系统自带）。

`vdsh.cmd` 已内置本目录，**CMD / Windows PowerShell 5.1 / PowerShell 7 三端通用**。只需把本目录加入用户 PATH（一次性）：

```powershell
[Environment]::SetEnvironmentVariable(
  'Path',
  [Environment]::GetEnvironmentVariable('Path', 'User') + ';T:\Open-Source\dsh-launcher',
  'User')
```

新开控制台生效。若新窗口仍提示找不到 `vdsh`（Windows 的 PATH 缓存行为），重启终端程序或重新登录一次；当前会话可手动刷新：

```powershell
$env:Path = [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + [Environment]::GetEnvironmentVariable('Path', 'Machine')
```

## 首次运行（配置向导）

首次执行 `vdsh`（默认功能）时，若 `vdsh.yaml` 尚未生成，会运行**交互式配置向导**：

```
vdsh · 首次配置向导（Ctrl+C 可中止；随时可 vdsh setup 重跑）
[1/3] DSH 安装目录（Harness 仓库根目录，须包含 package.json）——必需，校验后写入 launcher.repo
[2/3] DSH 数据目录（数据同步根；缺省 DSH_HOME → ~/.dsh）        ——可选
[3/3] Tailscale 域名（手机访问）                                ——可选
```

- 输入无效的安装目录会重新询问（最多 3 次）；回车使用默认值；`s` 跳过。
- 非交互环境（重定向/管道）自动降级为生成默认模板并提示，不阻塞任何命令。
- 随时可用 `vdsh setup` 重跑向导；`vdsh config` 查看生效值。

## 配置（vdsh.yaml）

配置文件位于启动器目录（`T:\Open-Source\dsh-launcher\vdsh.yaml`），首次运行自动生成带注释模板。**删除某行 = 该键回退内置默认值**。

```yaml
launcher:
  repo: T:\deepseek-harness        # DSH 安装目录（向导/DSH_REPO 优先）
  tailnet: ""                      # 默认 Tailscale 域名
  startup_timeout_seconds: 180     # 服务就绪等待上限（秒）
  starting_budget_seconds: 30      # 端口被占但未就绪时的等待上限（秒）
  poll_gap_seconds: 0.5            # 就绪轮询间隔（秒）
  open_browser: true               # false = 就绪后不自动打开浏览器（仍打印地址）
  workspace_seed: true             # false = 不注入工作区种子插件
animation:
  fps: 8                           # TTY 动画帧率（1-60）
  frames: "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"   # 动画帧序列
sync:
  data_dir: ""                     # DSH 数据目录；空 = DSH_HOME → ~/.dsh
  remote: ""                       # 默认远端；vdsh sync init 无参时使用
  allowlist: [.gitignore, sessions, profiles/web, storages, attachments, memories, settings.yaml]
  gitignore_extra: ''              # 追加进自动生成的 .gitignore（按缺失行幂等补写）
  commit_name: "DSH Sync"          # 提交者身份（两端一致）
  commit_email: "dsh-sync@local"
  timeout_seconds: 0               # 0 = 不限时；>0 时 fetch/push 等超过即终止（退出码 1）
```

**优先级**：命令行参数 > 环境变量（`DSH_REPO` / `DSH_TAILNET_HOST` / `DSH_HOME`）> vdsh.yaml > 内置默认。

未知键/非法值会打印警告并回退默认；YAML 整体解析失败则全部回退默认。

## 使用

### 启动

| 命令 | 行为 |
|---|---|
| `vdsh` | 工作目录 = 当前目录，启动服务并自动打开浏览器 |
| `vdsh T:\项目` | 指定工作目录（不存在则报错） |
| `vdsh --tailnet xxx.ts.net` | 手机经 Tailscale 访问（自动加 `--trusted-host`） |
| `vdsh --sync` | 启动服务前先自动执行一次 `sync pull`（仅实例未运行时；失败/未初始化只告警，不阻塞启动） |
| 服务已在运行 | 打开浏览器，并把当前目录注册进该实例 |

- 服务窗口**最小化启动、不抢焦点**；点任务栏图标可呼出查看日志 / Ctrl+C 停止。
- 所有需要等待的命令统一使用**同款加载动画**（转轮 + 秒数）：服务就绪、`build`、`sync push/pull/init`、`--sync`；直接调用 `sync-dsh.ps1` 在交互终端同样显示动画（脚本内 runspace 实现）。非交互/重定向自动静默。
- 浏览器**只由启动器在就绪后打开一次**（服务端 `--no-open` 关闭其自带自动打开）。
- 就绪检测以页面中的 `window.__DSH_BOOT__` 标记为准（0.1.1 起标题品牌化，旧标题仅兼容回退）。
- 构建产物缺失或源码更新时，询问是否执行 `pnpm run build`。

### 手机访问（Tailscale Serve）

1. 在 Tailscale 控制台启用 Serve（按 `tailscale serve` 提示链接）；
2. PC 执行一次（配置持久化）：`tailscale serve --bg http://127.0.0.1:3080`；
3. 启动 vdsh 带域名：`vdsh --tailnet v-reason-tx.tail1145de.ts.net`（自动加 `--trusted-host`，否则手机端 API 一律 403）；
4. 手机：Tailscale App 登录同账号 → 打开 `https://v-reason-tx.tail1145de.ts.net`。

> 安全：`--trusted-host` 意味着同 tailnet 内可访问该域名的设备拥有完整控制权；不要开 Funnel。

### 工作区

启动时经 `--patch` 注入 `workspace-seed.mjs`，把启动目录自动注册为 Web UI 工作区（幂等）。首次启动后在侧边栏点一次该工作区，之后每次启动自动选中。

### 数据同步（`vdsh sync`）

用**原生 Git** 在 DSH 进程之外同步两台电脑的 DSH 数据（聊天记录、设置、插件数据……），零依赖、不装插件——因此不存在插件方案的「同步命令自己产生新记录」自指问题。完整方法见 `dsh-data-git-sync/docs/native-git-sync.md`，小白上手教程见 `dsh-data-git-sync/docs/beginner-guide.md`。

```powershell
vdsh sync status                 # 状态：领先/落后、待推送文件、最近提交
vdsh sync push                   # 收工前：暂存变更 → 提交（可配置身份）→ 推送
vdsh sync pull                   # 开工前：快进优先，分叉时合并；冲突给出指引
vdsh sync init <URL>             # 一次性初始化（URL 缺省取 vdsh.yaml 的 sync.remote）
vdsh sync remote [set <URL>]     # 查看/设置远端仓库位置（同步写入 vdsh.yaml）
vdsh sync                        # 交互菜单（同双击 sync-dsh.cmd）
vdsh --sync                      # 启动服务前自动 pull 一次
```

- 数据目录自动识别：`DSH_HOME` → vdsh.yaml `sync.data_dir` → `~/.dsh`。
- 同步范围（allowlist）、`.gitignore` 追加规则、提交身份、单次超时均可经 vdsh.yaml 配置（通过 `vdsh` 调用时生效；直接调用脚本使用内置默认）。
- 规则：**两台电脑不要同时干活**——A 收工 `push` → B 开工 `pull`；DSH 空闲时再同步。
- 脚本退出码语义：`0` 成功 / `1` 硬失败（含超时）/ `2` 用法错误 / `3` 被阻塞（脏工作区、冲突）/ `4` 未初始化（可跳过）。

### 配置与自检

```powershell
vdsh config      # 查看生效配置 + 来源标注
vdsh setup       # 重跑首次配置向导
vdsh doctor      # 环境自检：仓库/工具链/端口/数据目录（任何 ✗ → 退出码 1）
```

## 架构

```
vdsh_launcher.py        薄入口（PATH 上的 vdsh.cmd 调用）
vdsh/                   实现包（功能尽量解耦）
  ├─ 共享层
  │   config.py         常量、路径、退出码
  │   settings.py       vdsh.yaml：加载/校验/合并/模板/文本级补丁/报告
  │   console.py        控制台流编码、文案前缀、退出码辅助
  │   spinner.py        加载动画 + 子进程流式执行器（含超时）
  │   bootstrap.py      首次运行引导（交互向导）
  ├─ 功能层 features/    每个功能一个模块，注册即对 CLI 可见
  │   launch.py         启动（默认功能）
  │   build.py          构建
  │   sync.py           数据同步（直通 sync-dsh.ps1）
  │   config.py         配置查看
  │   setup.py          配置向导
  │   doctor.py         环境自检
  └─ app.py             分发器：引导 → 配置 → 功能注册表
dsh-data-git-sync/      独立子工具（sync-dsh.ps1 + .cmd + docs；跨进程原生 Git）
workspace-seed.mjs      工作区种子插件（启动时生成）
```

**新增一个功能**（示例：`vdsh agent`）：

```python
# vdsh/features/agent.py
from ..console import say

NAME = "agent"
SUMMARY = "示例功能：打印一句话"

def run(argv, settings):
    say("hello")
    return 0
```

然后在 `vdsh/features/__init__.py` 的 `FEATURES` 中加一行 `"agent": ("agent", agent.SUMMARY, agent.run)` 即可：`vdsh agent`。

## 退出码（launcher）

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 2 | 用法/环境错误（参数错误、仓库未找到等） |
| 3 | 构建失败或 pnpm 缺失 |
| 4 | 依赖缺失（requests / pyyaml） |
| 5 | pwsh（PowerShell 7）缺失 |
| 6 | 端口被占用但未识别为 Harness |

`vdsh sync` 原样透传同步脚本退出码（0-4）。

## 环境变量

- `DSH_REPO`：Harness 仓库根目录（默认 `T:\deepseek-harness`）
- `DSH_TAILNET_HOST`：Tailscale 域名（未传 `--tailnet` 时生效）
- `DSH_HOME`：DSH 数据目录（默认 `~/.dsh`）

## 许可证

[MIT](LICENSE)
