# 完整使用参考

日常用法见根目录 README「快速开始」；本文是详尽参考。

## 命令行

```text
vdsh [工作目录]                    启动 dsh web 并打开浏览器（默认功能）
    --tailnet xxx.ts.net          手机经 Tailscale 访问（自动加 --trusted-host）
    --sync                        启动服务前自动拉取 DSH 数据（sync pull）
vdsh build                         直接执行仓库构建（pnpm run build，带动画）
vdsh sync <子命令>                 数据同步（init/push/pull/status/remote；无参 = 交互菜单）
vdsh config                        查看生效配置
vdsh setup                         重跑首次配置向导
vdsh doctor                        环境自检（只读）
vdsh --help                        用法
```

### 启动（launch，默认功能）

| 情况 | 行为 |
|---|---|
| 全新启动（端口空闲） | 可选 `--sync` 自动 pull → 按需构建 → 弹最小化 pwsh 窗口启动 → 就绪轮询 → 打开浏览器 |
| 实例已在运行 | 注册当前目录到该实例（RPC），打开浏览器，跳过同步与构建 |
| 端口被占但未就绪 | 等待其完成（默认 30s 预算），而非拉起第二个实例 |

要点：

- 服务窗口**最小化启动、不抢焦点**（CREATE_NEW_CONSOLE + SW_SHOWMINNOACTIVE）；点任务栏图标呼出看日志 / Ctrl+C 停止。
- 就绪判定以页面中的 `window.__DSH_BOOT__` 引导清单为主标记（0.1.1 起标题品牌化，旧标题 `DeepSeek Harness` 仅作兼容回退）。
- 浏览器只由启动器在就绪后打开**一次**：服务端以 `--no-open` 关闭其自带自动打开（0.1.1 起 web app 默认自开，会重复）。
- 工作区种子：经 `--patch <seed.yml>` 注入 `workspace-seed.mjs`（启动器目录下生成），把启动目录幂等注册为 Web UI 工作区；`launcher.workspace_seed: false` 可关闭。
- 构建：产物缺失或 `apps/cli/src`、`apps/web/src` 的 mtime 新于产物时，询问 `pnpm run build`；非交互输入（EOF）默认不构建。
- 动画：启动就绪、构建、`sync push/pull/init`、`--sync` 共用同一款转轮动画（帧 + 秒数，8fps 默认）；非 TTY/重定向自动静默。

### 配置（vdsh setup / config）

首次运行执行默认功能时若 `vdsh.yaml` 不存在，运行交互向导（提问 DSH 安装目录——必填校验、数据目录、Tailscale 域名）；非交互环境自动生成默认模板并提示。`vdsh setup` 可随时重跑；`vdsh config` 打印生效值与来源标注。

## 配置（vdsh.yaml）

位于启动器目录（`T:\Open-Source\dsh-launcher\vdsh.yaml`），首次运行自动生成带注释模板，**删除某行 = 该键回退内置默认**。优先级：命令行参数 > 环境变量（`DSH_REPO`/`DSH_TAILNET_HOST`/`DSH_HOME`）> vdsh.yaml > 内置默认。

| 键 | 默认 | 说明 |
|---|---|---|
| `launcher.repo` | `T:\deepseek-harness` | DSH 安装目录（Harness 仓库根，须含 package.json）；向导/`DSH_REPO` 优先 |
| `launcher.tailnet` | （空） | 默认 Tailscale 域名（`--tailnet`/`DSH_TAILNET_HOST` 优先） |
| `launcher.startup_timeout_seconds` | 180 | 服务就绪等待上限 |
| `launcher.starting_budget_seconds` | 30 | 端口被占但未就绪的等待上限 |
| `launcher.poll_gap_seconds` | 0.5 | 就绪轮询间隔 |
| `launcher.open_browser` | true | false = 就绪后不自动开浏览器（仍打印地址） |
| `launcher.workspace_seed` | true | false = 不注入工作区种子插件 |
| `animation.fps` | 8 | TTY 动画帧率（1-60） |
| `animation.frames` | `⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏` | 动画帧序列 |
| `sync.data_dir` | （空） | DSH 数据目录；空 = `DSH_HOME` → `~/.dsh` |
| `sync.remote` | （空） | 默认远端；`vdsh sync init` 无参时使用；`remote set` 写入此处 |
| `sync.allowlist` | 7 项列表 | 同步范围（相对数据目录） |
| `sync.gitignore_extra` | （空） | 追加进自动生成的 `.gitignore`；已有文件按缺失行幂等补写 |
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
vdsh sync status                 # 状态：领先/落后、待推送文件、最近提交
vdsh sync push                   # 收工前：暂存变更 → 提交 → 推送
vdsh sync pull                   # 开工前：快进优先，分叉时合并；冲突给出指引
vdsh sync init <URL>             # 一次性初始化（URL 缺省取 sync.remote）
vdsh sync remote [set <URL>]     # 查看/设置远端（更新 git origin 并写入 vdsh.yaml）
vdsh sync                        # 交互菜单（[1-5] 状态/推送/拉取/初始化/远端）
vdsh --sync                      # 启动服务前自动 pull（仅实例未运行时；失败只告警）
```

- 设计背景（为什么插件做不到）、同步范围、冲突处理见 `dsh-data-git-sync/docs/native-git-sync.md`；小白教程见 `dsh-data-git-sync/docs/beginner-guide.md`。
- 数据目录：`DSH_HOME` → `sync.data_dir` → `~/.dsh`。
- 退出码语义：`0` 成功 / `1` 硬失败（含 timeout 超时）/ `2` 用法错误 / `3` 被阻塞（脏工作区、冲突、远端 main 未建立）/ `4` 未初始化（可跳过）；`vdsh --sync` 依此只告警、不阻塞启动。
- 规则：**两台电脑不要同时干活**：A 收工 `push` → B 开工 `pull`；DSH 空闲时再同步。

## 退出码（launcher）

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 2 | 用法/环境错误（参数错误、仓库未找到等） |
| 3 | 构建失败或 pnpm 缺失 |
| 4 | 依赖缺失（requests / pyyaml） |
| 5 | pwsh（PowerShell 7）缺失 |
| 6 | 端口被占用但未识别为 Harness |

## 环境变量

| 变量 | 作用 |
|---|---|
| `DSH_REPO` | Harness 仓库根目录（默认 `T:\deepseek-harness`） |
| `DSH_TAILNET_HOST` | Tailscale 域名（未传 `--tailnet` 时生效） |
| `DSH_HOME` | DSH 数据目录（默认 `~/.dsh`） |

## 文件

| 文件 | 说明 |
|---|---|
| `vdsh_launcher.py` | 入口薄壳 |
| `vdsh/` | 实现包（分层见 design.md） |
| `vdsh.cmd` | 命令行薄壳（CMD / PS 5.1 / PS 7） |
| `dsh.cmd` | 官方 DSH CLI 转发壳 |
| `vdsh.yaml` | 用户配置（生成物，不入库） |
| `workspace-seed.mjs` / `seed.yml` | 工作区种子（生成物，不入库） |
| `dsh-data-git-sync/` | 同步子工具（sync-dsh.ps1/.cmd + docs） |
| `doc/` | 本目录：设计/开发/经验文档 |
