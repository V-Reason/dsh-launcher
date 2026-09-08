# 功能演示：白板机接入（主力机 → 副机静态同步）

> 目标：在**主力机**上把 DSH 配置好（插件、插件配置、聊天记录等），在一台**全新白板机**上
> 只装 DSH 与该启动器，按下面步骤一次性获得与主力机**一致的体验**（静态镜像）。
> 原理、同步范围与冲突处理见 [dsh-data-git-sync/docs/native-git-sync.md](../dsh-data-git-sync/docs/native-git-sync.md)；
> 完全不懂 Git 的版本见 [dsh-data-git-sync/docs/beginner-guide.md](../dsh-data-git-sync/docs/beginner-guide.md)。

---

## 1. 场景说明

| 概念 | 含义 |
|---|---|
| 主力机 | 唯一**写入方**：平时在这里用 DSH，收工 `vdsh sync push` |
| 白板机 | 只读镜像：装好 DSH + 本启动器 → 接入 → **只 `pull` 不 `push`**，体验与主力机一致 |
| 静态同步 | 单向：主力机 push → 白板机 pull；两端不同时干活，白板机不产生新数据 |

同步涵盖（默认 allowlist）：聊天记录 `sessions/`、插件与插件配置（`profiles/web/` 清单文件与
`cordis.patch.yml`、用户预设 `.agent-presets/`、全局配置层 `cordis.patch.yml`、插件运行数据
`storages/`、设置 `settings.yaml`）、附件 `attachments/`、记忆 `memories/`。

**不同步**：API 密钥 `.credentials.yaml`（永不入库，白板机需本地配置一次）、DSH 安装本体
（白板机自行安装同版本）、`profiles/web/node_modules/`（白板机 `pnpm install`，版本由
`pnpm-lock.yaml` 锁定）、模型缓存与匿名 ID（自动生成）。

> **DSH 版本 ↔ 插件版本必须配对**：插件清单（`package.json`/`pnpm-lock.yaml`）随仓库同步，
> 插件包体两端各自 `pnpm install`。若主力机升级过 DSH（如 2026-08-30 起 0.1.3-alpha.1
> 移除了 `settingsNamespace` 导出），先在**主力机**按插件迁移指南适配插件并重新 `push`；
> 白板机 `pull` + `pnpm install` 后得到的即适配版本。白板机安装的 DSH 必须与主力机同版本，
> 否则插件 API 与平台不匹配照样启动失败（报错见 §8）。

---

## 2. 前提清单

主力机：

- [ ] DSH 已配置好并正常使用（插件、聊天记录都在 `$DSH_HOME`，默认 `C:\Users\<用户>\.dsh`）。
- [ ] 插件已适配当前 DSH 版本（DSH 更新换代后需迁移，见第 1 节配对说明；`vdsh doctor` 可体检）。
- [ ] 已安装本启动器，`vdsh sync status` 可运行。
- [ ] 已建好裸仓库（如 `T:\DataBase\dsh-sync-repo.git`）并完成过一次 `vdsh sync push`。
- [ ] 若你的仓库在旧版本下已跟踪 `profiles/web/node_modules/`，先做一次迁移（见 §5）。

白板机：

- [ ] Windows + Git 已安装。
- [ ] DSH **同版本**安装（bundle 来自 DSH 安装本体；版本不同会导致插件 API 不匹配，配置无法按预期加载）。
- [ ] 本启动器已就位（拷贝 `dsh-launcher` 目录即可，`vdsh sync` 功能随目录走）。
- [ ] 能访问主力机的裸仓库（网络驱动器 / UNC / 内网穿透，映射成盘符如 `Z:`）。

---

## 3. 主力机一次性准备（只做一次）

```powershell
# 1) 创建裸仓库（已存在可跳过）
git init --bare T:/DataBase/dsh-sync-repo.git
# 2) 让启动器认识它并生成排除规则
vdsh sync init file:///T:/DataBase/dsh-sync-repo.git
# 3) 首次推送（自动建立远端 main 分支）
vdsh sync push
```

预期：步骤行依次为 `→ 提交 N 个文件（sync: …）`、`→ 推送 origin/main…`，最后 `✓ 完成（0.Xs）`；`vdsh sync status` 显示「领先 0 / 落后 0」。

> 之后收工前只需 `vdsh sync push`；`vdsh --sync` 启动服务前会自动先 pull（本机有改动时不要用）。

---

## 4. 白板机接入步骤（顺序重要）

### 4.1 安装 DSH（本机路径）

安装与主力机**相同版本**的 DSH 并构建可用（版本不一致时，同步来的插件/预设可能无法加载）。
启动器首跑向导会问 DSH 安装目录（或设置 `DSH_REPO` / `vdsh setup`）。

### 4.2 先别启动 DSH

> 启动会写 `sessions/`，导致稍后 init 把本机判定为「已有数据」而走合并分支。
> 白板机要求：**接入完成后才第一次启动**。

### 4.3 接入数据仓库

```powershell
vdsh sync init
```

无参即进入**副机配置向导**：依次确认 DSH 安装目录、DSH 数据目录、远端裸仓库地址（每项回车用默认；
残留主力机路径会有提示，填映射盘对应的 `file:///Z:/…` 或 UNC；详细规则见 [usage.md](usage.md) 数据同步节）。
等价带参写法：`vdsh sync init file:///Z:/DataBase/dsh-sync-repo.git`。

预期输出（关键句）：

```
→ 初始化数据仓库（~/.dsh）
→ 数据目录不存在，正在创建…
→ 生成 .gitignore（排除规则，会随仓库同步）
→ 数据目录为全新，自动从远端填充数据…
✓ 完成：远端数据已填入本机，可执行 status 查看。
```

若提示「本机尚无提交但目录里已有 DSH 数据」→ 说明本机启动过 DSH，按
[docs/native-git-sync.md §3.2](../dsh-data-git-sync/docs/native-git-sync.md) 的 merge/reset 二选一收尾。

### 4.4 安装 profile 依赖（插件包本体）

```powershell
cd "$HOME\.dsh\profiles\web"
pnpm install
```

> `profiles/web/node_modules/` 不入库（体积大、跨机符号链接易坏）；版本由随同步过来的
> `pnpm-lock.yaml` 锁定，装出来的依赖与主力机完全一致——包括主力机为适配 DSH 更新
> 后调整的插件版本（清单随同步走，包体只需 install 一次）。

### 4.5 首次启动 DSH

```powershell
vdsh
```

预期：服务就绪、浏览器打开。DSH 启动时会自动重建本机专属的
`profiles/node_modules/`（指向安装依赖的符号链接农场）等运行目录。

### 4.6 配置 API 密钥（唯一需要手填的一项）

在 DSH 设置页填入 API 密钥（或写 `$DSH_HOME\.credentials.yaml`；环境变量亦可）。
**该文件永不入库**——这是刻意的安全边界，也是白板机与主力机唯一不同的本地数据。

### 4.7 核对

```powershell
vdsh sync status
```

预期：`→ 同步状态（~/.dsh）`、`分支: main`、`上游: origin/main — 本地领先 0 提交 / 落后 0 提交`、`待推送: 无`。

---

## 5. 旧仓库迁移（仅主力机，一次）

若你的同步仓库在旧版本下**跟踪过** `profiles/web/node_modules/`（默认 allowlist 曾含整目录），
加一条忽略规则不会自动取消跟踪，需一次性处理（DSH 空闲时）：

```powershell
git -C "$HOME\.dsh" rm -r --cached profiles/web/node_modules
vdsh sync init        # 幂等补写 .gitignore 的 node_modules 排除行（TTY 下为配置向导，
                      #   第 3 项远端回车接受默认即可；等价于旧版「无参取 sync.remote」）
vdsh sync push        # 提交「不再跟踪 node_modules」并推送
```

预期：推送后仓库文件数大幅下降；白板机按 §4.4 各自 `pnpm install`。

---

## 6. 体验一致性验收清单

接入完成后逐项核对（对应 §1「同步涵盖」）：

- [ ] 白板机能看到主力机的聊天记录（`sessions/`）。
- [ ] 设置一致：默认模型、主题、任务通知等（`settings.yaml`）。
- [ ] 主力机的用户 agent 预设出现在 roster（`.agent-presets/`，如 `study` 及其 `skills/`）。
- [ ] 插件配置生效：profile 补丁层 / 全局补丁层（`cordis.patch.yml`）与插件运行数据
      （`storages/`）与主力机一致。
- [ ] 附件与记忆可浏览（`attachments/`、`memories/`）。

> 动态 Cordis 插件（Web UI `cordis_define` 创建）仅存在于进程内：主力机重启也会消失。
> 要让白板机获得同样能力，把它的组合写进 `.agent-presets/` 下的用户预设即可被同步。

---

## 7. 日常流程

| 时机 | 主力机 | 白板机 |
|---|---|---|
| 收工前 | `vdsh sync push` | — |
| 开工前 | — | `vdsh sync pull`（或 `vdsh --sync` 自动拉取） |
| 想确认 | `vdsh sync status` | `vdsh sync status` |

> 静态同步约定：白板机**只 pull 不 push**。若某天白板机也要产生数据，按
> 「不同时干活」规则双向使用（见 native-git-sync.md §1）。

---

## 8. 常见问题（白板机视角）

| 现象 | 原因 | 处理 |
|---|---|---|
| init 提示「本地已有 DSH 数据」 | 接入前启动过 DSH | 按 §4.3 的 merge/reset 收尾；或删掉 `$DSH_HOME` 下数据后重新 init |
| 看不到主力机的用户预设 | 两端 `sync.allowlist` 不一致（缺 `.agent-presets`） | 两端同步同一份 vdsh.yaml 配置后，主力机 `push`，白板机 `pull` |
| 启动报依赖/插件缺失 | 白板机没执行 §4.4 的 `pnpm install`，或 DSH 版本不同 | 执行 `pnpm install`；确认两端 DSH 同版本 |
| 启动报 `does not provide an export named 'settingsNamespace'` | 插件未适配 DSH 0.1.3-alpha.1（2026-08-30 平台 API 重构）| 在**主力机**按插件迁移指南适配并重新 push；白板机 pull + `pnpm install`（见 §2/§4.4） |
| `vdsh config` 的 remote 为空但同步正常 | 旧版 `vdsh sync init` 不持久化远端 | 无参重跑 `vdsh sync init`（TTY 下向导回车确认默认值；新版本 init 与 `remote set` 都会写入） |
| `dsh web` 报 `Cannot find module 'T:\…\apps\cli\lib\bin.js'` | 旧版 `dsh.cmd` 硬编码主力机路径；跨机复制后残留 | 升级 launcher：`dsh.cmd` 现在按 DSH_REPO → vdsh.yaml → 本机候选动态解析；仍报错时运行 `vdsh setup` 或设置 `DSH_REPO` |
| 提示密钥缺失 | `.credentials.yaml` 永不入库 | 按 §4.6 在本地配置一次 |
| node_modules 相关文件出现在待推送 | 旧仓库未迁移 | 按 §5 迁移 |

---

## 9. 相关文档

| 文档 | 内容 |
|---|---|
| [dsh-data-git-sync/docs/native-git-sync.md](../dsh-data-git-sync/docs/native-git-sync.md) | 原理、同步矩阵、冲突处理、故障排查（完整参考） |
| [dsh-data-git-sync/docs/beginner-guide.md](../dsh-data-git-sync/docs/beginner-guide.md) | 不要求懂 Git 的手把手教程 |
| [usage.md](usage.md) | `vdsh sync` 全部子命令与 `vdsh.yaml` 键说明 |
