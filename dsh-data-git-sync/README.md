# dsh-data-git-sync

用**原生 Git** 同步两台电脑的 DSH 数据（聊天记录、设置、插件数据……）。
零依赖、不装任何插件、在 DSH 进程之外运行——因此不存在插件方案的「同步命令自己产生新记录」
自指问题，也不需要开 Web 界面才能看到结果。

> 本工具是已废弃插件 `dsh-data-sync` 的替代品。插件问题背景见
> [docs/native-git-sync.md](docs/native-git-sync.md) 开头。
>
> 本目录是 **dsh-launcher** 的一部分（`T:\Open-Source\dsh-launcher\dsh-data-git-sync`）。
> 安装 launcher 后所有同步命令可简写为 **`vdsh sync <命令>`**（见下）。

## 本包内容（相对 dsh-launcher/dsh-data-git-sync/）

| 文件 | 说明 |
|---|---|
| `sync-dsh.ps1` | 主脚本：`init` / `push` / `pull` / `status` / `help` 五个命令 |
| `sync-dsh.cmd` | **双击即可用**的小白启动器（菜单：状态 / 推送 / 拉取 / 初始化） |
| `docs/beginner-guide.md` | **小白教程**：从零开始、手把手、含常见问题 |
| `docs/native-git-sync.md` | 完整方法/参考文档：同步范围、原生命令、冲突处理、故障排查 |

## 三种调用方式（结果一样）

| 方式 | 写法 |
|---|---|
| **vdsh 集成（推荐）** | `vdsh sync status` / `vdsh sync push` / `vdsh sync pull` / `vdsh sync init <URL>` |
| **双击菜单** | 双击本目录 `sync-dsh.cmd`（菜单：状态 / 推送 / 拉取 / 初始化） |
| **直接脚本** | `powershell -ExecutionPolicy Bypass -File <本目录>\sync-dsh.ps1 status` |

此外，`vdsh --sync` 会在**启动服务前**自动先执行一次 `pull`（仅实例未运行时；
失败或未初始化只告警，不阻塞启动），对应「开工前拉取」。

## 快速开始（三件事）

1. **主力机创建数据仓库**（只做一次）：
   ```
   git init --bare T:/DataBase/dsh-sync-repo.git
   vdsh sync init file:///T:/DataBase/dsh-sync-repo.git
   vdsh sync push
   ```
2. **副机接入**（把主力机的 `T:\DataBase` 映射成 `Z:` 后）：
   ```
   vdsh sync init file:///Z:/DataBase/dsh-sync-repo.git
   vdsh sync pull
   ```
   > 全新副机目录为空时执行 `git -C "$HOME\.dsh" checkout -b main origin/main`；
   > 副机已有 DSH 数据时执行 `git -C "$HOME\.dsh" merge origin/main`。
3. **日常同步**：开工前 `vdsh sync pull`，收工前 `vdsh sync push`。

首次使用请先看 **[docs/beginner-guide.md](docs/beginner-guide.md)**。

## 脚本用法

```
sync-dsh.ps1 status                # 查看状态：领先/落后、待推送文件、最近提交
sync-dsh.ps1 push                  # 暂存变更 -> 提交（DSH Sync 身份）-> 推送
sync-dsh.ps1 pull                  # 快进优先，分叉时合并；冲突给出处理指引
sync-dsh.ps1 init <远程URL>        # 一次性初始化：git init + origin + .gitignore + fetch
sync-dsh.ps1 help                  # 用法说明
```

- 数据目录自动识别：`$env:DSH_HOME`，缺省 `~/.dsh`。
- 动画：经 `vdsh sync push/pull/init` 调用时显示进度动画（与 vdsh 启动同款转轮 + 秒数）；
  直接/菜单调用时，`fetch`/`push`/`merge` 阶段在**交互终端**同样显示（脚本内 runspace 实现），
  输出重定向自动静默；`push`/`pull` 成功时附总耗时。
- 同步范围（allowlist）：`.gitignore`、`sessions/`、`profiles/web/`、`storages/`、
  `attachments/`、`memories/`、`settings.yaml`；不存在自动跳过。
- 排除（随仓库同步的 `.gitignore`）：`.credentials.yaml`、`*.log`、`logs/`、`*.lock`、
  `.dsh-data-sync/`、`llm-*/`、`profiles/node_modules/`。
- **退出码**（供 `vdsh --sync` 等调用方区分）：`0` 成功；`1` 硬失败；`2` 用法错误；
  `3` 被阻塞（脏工作区 / 冲突 / 远端 main 未建立）；`4` 未初始化（可跳过）。

## 注意事项（小白必读）

- **两台电脑不要同时干活**：A 收工 `push` 后，B 开工先 `pull`。
- **DSH 空闲时再同步**：DSH 正在生成回复时同步，会话文件还在变化，`pull` 会提示先 `push`。
- **`.credentials.yaml`（API 密钥）永不入库**：已双保险（allowlist 不含 + `.gitignore`）。
- Windows 上 `core.autocrlf` 提示（`LF will be replaced by CRLF`）是正常现象，可忽略。
