# 原生 Git 实现 DSH 数据同步（替代已废弃的 dsh-data-sync 插件）

> 本方案**不用任何插件**：直接在 DSH 进程之外，用原生 `git` 命令把 `$DSH_HOME` 数据在
> 两台设备之间同步（裸仓库 + 网络共享目录/内网穿透）。配套脚本：`dsh-launcher` 内的
> [`sync-dsh.ps1`](../sync-dsh.ps1)（把下列指令集成成 `init / push / pull / status / help` 五个子命令）。
> 安装 dsh-launcher 后，全部子命令可简写为 **`vdsh sync <命令>`**；`vdsh --sync` 还会在
> 启动服务前自动先执行一次 `pull`。

**为什么放弃插件**：插件通过 `/sync` 斜杠命令触发同步，而 DSH 会把每次斜杠命令的
`command/run` + `command/done` 生命周期事件写进**当前会话的日志文件**（`sessions/…/session.jsonl.zstd`）。
同步范围又包含 `sessions/`，于是产生自指：

- `/sync push` 提交时包含了 `command/run`，但 `command/done` 在命令返回后才落盘 → 紧随其后的
  本地会话文件总是比刚推送的版本多一条记录，`status` 永远显示「待推送」，每次 push 后又要再 push；
- 副机 pull 后会得到一个「 **仍在执行中** 」的悬空 `/sync` 命令卡片（有 run 无 done）。

原生 Git 方案在终端运行，**不经过 DSH 会话，不写任何会话记录**，从根上消除该问题。
（另一个插件的体验问题——全新会话中执行 `/sync` 无任何反馈，因为 DSH 只在「非空白会话」
里渲染命令结果节点——也随之不复存在：命令的输出直接打在终端上。）

---

## 1. 前置条件

- 两台设备均为 Windows（本方案当前针对 Windows；命令本身是 git，bash 下等价）。
- 已安装 git 并加入 PATH；已在两台设备安装 DSH（`$DSH_HOME`，默认为 `C:\Users\<用户>\.dsh`）。
- 一个可共享的位置存放**裸仓库**，两台设备都能访问：
  - 主力机本地磁盘裸仓库：`T:/DataBase/dsh-sync-repo.git`（本方案示例路径）；
  - 副机通过网络驱动器访问：把主力机的 `T:\DataBase` 映射为 `Z:`（或 UNC 路径）。
- 单向使用约定：两台设备**不同时操作**（同步前保证另一台已提交并推送）。

---

## 2. 同步范围与排除

### 2.1 同步内容（allowlist，相对 `$DSH_HOME`）

| 路径 | 内容 |
|---|---|
| `.gitignore` | 排除规则本身（随仓库同步，两端一致） |
| `sessions/` | 全部对话记录（JSONL + zstd 帧） |
| `profiles/web/` | Web Profile 完整配置（含 `node_modules/`） |
| `storages/` | 插件持久化数据 |
| `attachments/` | 对话内图片/附件的字节 |
| `memories/` | 长期记忆（如存在） |
| `settings.yaml` | 全局用户设置 |

**实现方式**：`git add -A -- <allowlist>`——只暂存这些路径，其余（`llm-*/`、`.agent-presets/`、
`.anonymous-user-id`、`profiles/node_modules/` 等）不入库。**不存在的路径会自动跳过**
（脚本已内置过滤；`git add` 对不存在的 pathspec 会直接报错）。

### 2.2 永不入库（`$DSH_HOME/.gitignore`）

```gitignore
# DSH 数据同步：永不提交的内容（原生 Git 方案）
.credentials.yaml
*.log
logs/
*.lock
.dsh-data-sync/
llm-*/
profiles/node_modules/
```

说明：

- `.credentials.yaml`：API 密钥，**严禁入库**（当前仓库历史已验证从未跟踪过它；allowlist 也不含它，双保险）。
- `*.lock`：进程锁文件。注意 `pnpm-lock.yaml` 以 `.yaml` 结尾，**不受影响**，仍会同步。
- `.dsh-data-sync/`：已废弃插件的状态目录（防御性保留）。
- `llm-*/`：模型提供商缓存（如 `llm-deepseek/`），无需同步。

---

## 3. 一次性初始化

### 3.1 主力机（持有裸仓库）

```sh
# 1) 创建/确认裸仓库（已存在可跳过）
git init --bare T:/DataBase/dsh-sync-repo.git

# 2) 把 $DSH_HOME 变成 git 仓库并接入远端
cd ~/.dsh
git init -b main
git remote add origin file:///T:/DataBase/dsh-sync-repo.git

# 3) 用脚本生成 .gitignore 并完成首次推送（脚本会自动建外排除清单）
powershell -ExecutionPolicy Bypass -File sync-dsh.ps1 init file:///T:/DataBase/dsh-sync-repo.git
powershell -ExecutionPolicy Bypass -File sync-dsh.ps1 push     # 首次 push 自动建立上游 main
```

等价的原生命令：

```sh
cd ~/.dsh
git init -b main
git remote add origin file:///T:/DataBase/dsh-sync-repo.git
# 手工创建 §2.2 的 .gitignore 后：
git add -A -- .gitignore sessions profiles/web storages attachments memories settings.yaml
git -c user.name="DSH Sync" -c user.email="dsh-sync@local" commit -m "sync: 首次提交"
git push -u origin main
```

> 提交者身份用 `-c user.name/user.email` 在**每条命令**上注入（不改仓库配置），
> 两端写死为一致的值（`DSH Sync` / `dsh-sync@local`）。

### 3.2 副机（映射网络驱动器 `Z:`）

**全新副机（`$DSH_HOME` 为空/新装 DSH）**：

```sh
cd ~/.dsh
git init -b main
git remote add origin file:///Z:/DataBase/dsh-sync-repo.git
git fetch origin
git checkout -b main origin/main        # 把远端数据完整落下
```

**已有数据的副机（`$DSH_HOME` 已在使用）**：

```sh
cd ~/.dsh
git init -b main
git remote add origin file:///Z:/DataBase/dsh-sync-repo.git
git fetch origin
# 二选一：
git merge origin/main                        # 远端历史并入本地数据（可能产生冲突，见 §6）
# 或
git reset --soft origin/main                 # 以远端为基线，本地数据变成未提交变更，随后 push 即可上传
```

### 3.3 关键环境一致性

- **`core.autocrlf` 两端必须一致**（默认均为 `true` 即可；若改，两台一起改）。
  查询：`git config core.autocrlf`。出现 `LF will be replaced by CRLF` 警告是 Git for Windows
  的正常提示，不影响同步。
- 两台设备 `$DSH_HOME` 的 git 仓库**共用同一个裸仓库的 main 分支**；提交者身份按 §3.1 固定。

---

## 4. 日常同步（原生命令）

```sh
# 查看状态：待推送文件、与远端的前后差异、最近提交
git -C ~/.dsh status --short
git -C ~/.dsh rev-list --left-right --count HEAD...origin/main
git -C ~/.dsh log --oneline -5

# 推送：暂存 → 提交 → 推送
git -C ~/.dsh add -A -- .gitignore sessions profiles/web storages attachments memories settings.yaml
git -C ~/.dsh diff --cached --name-only            # 无输出 = 无变更，无需提交
git -C ~/.dsh -c user.name="DSH Sync" -c user.email="dsh-sync@local" commit -m "sync: $(Get-Date -Format s)"
git -C ~/.dsh push

# 拉取：先试快进，分叉时常规合并
git -C ~/.dsh fetch origin
git -C ~/.dsh merge --ff-only origin/main          # 常态（两端交替使用）：纯快进
git -C ~/.dsh merge origin/main                    # 仅在本地与远端都有新提交时执行
```

> 拉取前工作区必须干净：若 `git status --short` 有输出，**先 push 拉再**（DSH 运行中会话文件
> 持续变化属正常；同步应在 DSH 空闲/停止时进行，见 §7）。

---

## 5. shell 脚本（sync-dsh.ps1）

把 §4 的指令集并为五个子命令。脚本位于 `dsh-launcher\dsh-data-git-sync\`，位置任意
（内置 `$DSH_HOME` 识别：优先 `$env:DSH_HOME`，缺省 `~/.dsh`）。

```powershell
# 状态
powershell -ExecutionPolicy Bypass -File sync-dsh.ps1 status
# 提交并推送（首次会自动建立上游 main）
powershell -ExecutionPolicy Bypass -File sync-dsh.ps1 push
# 拉取（脏工作区先提示 push；快进优先，必要时常规合并；冲突给出处理指引）
powershell -ExecutionPolicy Bypass -File sync-dsh.ps1 pull
# 一次性初始化（init 后按 §3.2 视副机情况收尾）
powershell -ExecutionPolicy Bypass -File sync-dsh.ps1 init file:///Z:/DataBase/dsh-sync-repo.git
```

安装 dsh-launcher 后，上面四条分别等价于
`vdsh sync status` / `vdsh sync push` / `vdsh sync pull` / `vdsh sync init file:///Z:/...`；
`vdsh --sync` 启动服务前自动先 `pull` 一次（实例未运行时；失败仅告警不阻塞）。

脚本行为要点：

| 子命令 | 行为 |
|---|---|
| `init` | `git init -b main`（如未初始化）、添加 `origin`（缺 `-RemoteUrl` 报错）、缺失时生成 `.gitignore`（UTF-8 无 BOM）、`fetch origin` 并打印下一步（全新副机 checkout / 已有数据 merge 或 reset --soft） |
| `push` | 过滤**实际存在**的 allowlist 路径 → `git add -A -- <路径>` → 有暂存才提交（`DSH Sync` 身份）→ `push`（失败自动重试 `push -u origin main`；非快进时提示先 pull） |
| `pull` | 工作区脏 → 提示先 push（退出码 3）；否则 `fetch` → `merge --ff-only origin/main`，失败分叉走 `merge origin/main`；冲突/无本地提交给出指引（退出码 3） |
| `status` | 仓库/分支/远端 URL、`HEAD...origin/main` 前后差异、待推送文件预览、最近提交 |
| `help` | 打印用法 |

> 退出码语义（供 `vdsh --sync` 等调用方区分）：`0` 成功（含「无变更可推送」）；`1` 硬失败
> （git 命令失败/远端不可达）；`2` 用法错误；`3` 被阻塞（脏工作区、冲突中间态、远端 main 未建立）；
> `4` 未初始化（无仓库/无 origin/数据目录不存在，可跳过）。
> 脚本以 **PowerShell 5.1 兼容** 编写（UTF-8 BOM），`powershell` 与 `pwsh` 均可运行；
> bash 环境按 §4 的命令逐条等价改写即可。

---

## 6. 冲突处理（`git pull` / `merge` 报冲突时）

git 冲突时仓库处于「未合并」中间态，`git status` 显示 `UU`（每侧各有 stage 2/3）。

**先决定回滚还是解决**：

```sh
git -C ~/.dsh merge --abort      # 回滚：仓库回到合并前状态（推荐，随后换时机重试）
```

**文本文件**（JSON/YAML/配置文件等，工作区已写入冲突标记）：

```sh
git -C ~/.dsh checkout --ours -- <文件>    # 保留本地版本
git -C ~/.dsh checkout --theirs -- <文件>  # 使用远端版本
# 或手工编辑保留冲突标记两侧内容后：
git -C ~/.dsh add -- <文件>
git -C ~/.dsh commit                        # 完成合并提交
```

**二进制/zstd 会话**（工作区不留标记，默认保留本地版本文件）：三种走法——

```sh
git -C ~/.dsh checkout --ours -- <文件> && git add -- <文件>     # 保留本地
git -C ~/.dsh checkout --theirs -- <文件> && git add -- <文件>   # 使用远端
# 双方都保留（fork：本地不动，远端另存为 <文件>.remote-fork）：
git -C ~/.dsh show :3:<文件> > "<文件>.remote-fork"
git -C ~/.dsh add -- <文件> "<文件>.remote-fork"
# 三条任一完成后：
git -C ~/.dsh commit
```

> `:3:` 是索引中的远端 stage（`ours`=`:2:`、`theirs`=`:3:`）。fork 方式对 zstd 会话数据最安全——
> 两侧内容都保留，不互相覆盖。按时间戳抉择：`git log -1 --format=%ct HEAD -- <文件>` 对比
> `origin/main` 的同一查询，新者胜。

---

## 7. 与 DSH 运行的关系（重要）

- **同步应在 DSH 空闲或已停止时执行**：DSH 运行时会持续写 `sessions/`（当前会话）与 `storages/`
  （投影缓存），工作区会一直「脏」。刚完成一轮对话（模型响应结束、写盘已刷新）后运行 `push`
  即可得到干净结果；`pull` 前若有脏文件，先 `push`。
- 原生 Git 不写会话记录，**不会出现插件的自指问题**；但为拿到确定的快照，仍建议避开 DSH
  正在生成回复的瞬间。
- `profiles/web/` 包含 `node_modules/`（pnpm 在 Windows 上使用 junction）。副机首次检出后建议
  在 `profiles/web` 下执行一次 `pnpm install` 校验；若 junction 异常，可自行调整：
  allowlist 只留 `profiles/web/package.json` 与 `pnpm-lock.yaml`，两端各自 `pnpm install`
  （代价：依赖装取在本地完成，非「零差异」镜像）。

---

## 8. 安全与注意事项

- **密钥**：`.credentials.yaml` 在 `.gitignore` + allowlist 双保险下永不入库。可用
  `git log --all --oneline -- .credentials.yaml` 自查历史（当前仓库为无记录）。
- **日志/锁**：`*.log`、`logs/`、`*.lock` 一律不入库。
- **远端不可达**（共享盘未挂载/内网穿透断开）：`push/pull` 会报 git 错误，本地数据不受影响。
- **并发**：同一时刻只允许在一台设备操作；仓库的分叉（两端都有新提交）用 `merge` 合并而非覆盖。

---

## 9. 故障排查

| 症状 | 处理 |
|---|---|
| `pathspec did not match` | allowlist 路径不存在；脚本已自动过滤，手工命令需确认目录存在 |
| push 报 `origin/main` 不存在 | 远端还没建立分支，先 `git push -u origin main`（脚本会自动重试） |
| pull 报「工作区有未提交变更」 | 先 `push`；或确认 DSH 已空闲/停止后再试 |
| pull 报冲突 | 按 §6：先 `merge --abort` 回滚，或解决后 `commit` |
| 检测到 `.credentials.yaml` 被跟踪 | `git rm --cached .credentials.yaml`，并考虑从历史清除（`git filter-repo`），立即更换密钥 |
| 换行反复翻动（大量 `\r`/`\n` diff） | 两端 `core.autocrlf` 不一致；统一后 `git add --renormalize .` 一次 |
| 副机 node_modules 异常 | 在 `profiles/web` 下 `pnpm install`；或按 §7 改为只同步清单文件 |
| 脚本中文乱码/解析失败 | 确保以 `powershell -File`（或 `pwsh`）运行；文件为 UTF-8 BOM 编码 |

---

## 10. 与旧插件的关系

- 插件已从 `profiles/web` 的依赖与 bundle 层移除（`dsh plugin --profile web remove
  dsh-data-sync`），`pnpm-workspace.yaml` 的 `allowBuilds`、`$DSH_HOME/.dsh-data-sync/`、
  `unified-sync-config.yaml` 已清理。
- `$DSH_HOME` 的数据仓库（`origin = file:///T:/DataBase/dsh-sync-repo.git`）**原样保留**，
  由原生 Git 方案继续使用；`dsh-launcher\dsh-data-git-sync\sync-dsh.ps1` 即替代工具。
