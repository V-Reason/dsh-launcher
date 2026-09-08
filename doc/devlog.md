# 开发日志（迭代记录）

按**一次迭代一条记录**追加，供后续迭代开发回溯「当时为什么这么做、测过什么、还剩什么」。
每条记录含：背景 → 改动清单（按需求条目）→ 关键决策与取舍 → 验证情况 → 遗留与后续提示。
前置阅读：`design.md`（架构）→ `dev.md`（开发规程）；踩坑细节见 `experience.md`。

---

## 2026-09-08（同日第三波 · 收尾）：聊天记录跨机同步不成立 —— 功能搁置

### 1. 背景与结论

第二波修复后副机可正常启动；用户验证核心诉求「副机看聊天记录」时发现：**数据文件确实
同步了（`sessions/`、`storages/` 都在仓库里），但副机 DSH 界面不显示任何聊天记录**。
调查确认这是 **DSH 数据模型的路径键限制**，不是同步脚本/launcher 能修的问题；用户评估
维护成本后决定**搁置该功能**，本波为收尾：把根因、证据、搁置语义与恢复路径写进文档，
不做新的代码改动。

### 2. 根因（有源码与两侧磁盘证据）

1. **会话按「工作区绝对路径」组织**：
   - `sessions/` 下的目录键 = `projectKey(cwd)`（实现：`packages/session/session-persistence-jsonl/src/format.ts`）：
     分隔符/盘符折成 `-`，非安全字符用 `~XXXX` 转义，整体 `--slug--`。
     实测主力机：`T:\Open-Source\dsh-launcher` → `sessions/--T-Open-Source-dsh-launcher--/`，
     中文路径 → `--T-~6742~4E03~6742~516B-…--` 等。副机 cwd（如 `C:\_TMP`）会生成
     `--C-_TMP--` 类键——**与主力机同步来的键集合不重合**。
   - `storages/workspace.json` 的 `tables.workspaces[].path` 同样是**绝对路径**（实测：
     `T:\deepseek-harness`、`T:\杂七杂八\2.笔记\碎语札`、`C:\Users\V.Reason\…` 等），
     workspace 的会话归属也绑定在这些路径键上。跨机后这些路径在副机要么不存在、要么
     另有所指，workspace 注册与 session 归属自然对不上。
2. **同步只能搬文件，不能改键**：`git` 按路径名搬运，`--T-…--` 目录原样落在副机
   `sessions/` 下；副机 DSH 启动后查询会话时用**副机自己 cwd** 的 `projectKey` 找目录，
   找到的是副机新会话的键区间，主力机键区间不参与 → UI 空。
3. 因此「看聊天记录」失败与路径解析（第一波 dsh_cli，已解决）、模块回退（第二波，已解决）
   完全无关，属**第三类问题**：DSH 内部数据模型假定「一份会话数据只属于本机工作区
   绝对路径」，与「跨机镜像」语义冲突。

### 3. 本波改动（仅文档，无代码）

| 文件 | 内容 |
|---|---|
| `doc/design.md` | 决策记录加一行「聊天记录跨机同步搁置」；§5 已知边界加「已搁置」条目（含 3 条恢复候选路径：DSH 按 workspace id 检索 / 副机路径重映射（junction/虚拟盘）使绝对路径一致 / launcher 按 session id 聚合——均未承诺） |
| `doc/demo.md` | §1 加「⚠ 已知限制」框（现象、根因、搁置状态、其它同步内容不受影响）；§6 验收清单第一项改为「✗ 已知不可用」并指向 §1；§8 FAQ 加一行「白板机看不到聊天记录」 |
| `doc/devlog.md` | 本记录 |
| `doc/experience.md` | §8.6 追加：`projectKey`/`workspace.json` 绝对路径键的跨机失效教训（供后续检索） |
| `doc/usage.md` | 「同步范围」一句标注 `sessions/` 跨机可见性已搁置 |
| `dsh-data-git-sync/README.md`、`docs/native-git-sync.md` | 同步矩阵/说明标注「聊天记录文件同步、UI 可见性已搁置」 |

### 4. 关键决策与取舍

- **搁置而非继续修**：launcher 在 DSH 路径键模型上打补丁（扫描 `--T-…--` 目录重命名/重映射
  到副机键）会在每次 sync 后与副机 session 写入竞争，且 workspace.json 的 `path`
  归属层无法由 Git 同步解决；成本 > 收益，用户拍板搁置。
- **同步机制本身保留**：`sessions/` 仍随仓库同步（无害，且将来若恢复「同机迁移/路径一致
  的镜像」可直接用）；只是不再宣传「副机看聊天记录」为支持项。`demo.md` 验收清单与
  主 README 的「白板机体验一致」表述已修正，避免误导后续使用者。
- **恢复时机**：DSH 上游把会话检索改为（或增加）按 workspace id 而非绝对路径后，或副机
  把主力机路径重映射为相同绝对路径（如 `subst`/junction 挂 `T:` 盘），该功能可无代码恢复。

### 5. 验证情况

| 项 | 方法 | 结果 |
|---|---|---|
| 根因（键规则） | 读 `session-persistence-jsonl/src/format.ts` `projectKey`/`encodeSegment`，与 `sessions/` 实测目录名对照 | 一致：`--T-Open-Source-dsh-launcher--` 等均由盘符+路径折算 |
| 根因（workspace 注册） | 读 `storages/workspace.json` `tables.workspaces[].path` | 全部为绝对路径（`T:\…`、`C:\…`），无相对/别名键 |
| 副机 UI 空数据 | 用户实机复核（「聊天记录根本没同步」） | 复现；与上述键模型推论一致 |
| 代码 | 无代码改动（本波纯文档）；前置两波代码仍编译/解析通过 | — |

### 6. 遗留与后续迭代提示

- **恢复此功能的第一前提**是 DSH 侧改动或副机路径重映射（见 design.md §5）；届时 `sessions/`
  同步链路无需改动（一直可用），只需副机侧让 `projectKey(cwd)` 与 workspace 注册路径
  命中同步来的键。
- 已知「同步内容」宣传口径已在 demo.md 统一：**设置/插件/预设/附件/记忆 = 支持；
  聊天记录文件随仓库、UI 跨机可见性 = 搁置**。
- 若未来再次出现「文件在、UI 不显示」类问题，先查「是否路径相关键（目录名/注册表 path）」，
  再考虑数据损坏——经验见 `experience.md` §8.6。

### 7. 复测清单（回归用）

无代码改动；回归 = 前置两波代码照旧（见第二波 §7）。功能验收口径以 `demo.md` §1/§6 为准。

---

## 2026-09-08（同日第二波）：副机启动失败 —— 模块回退目录被 git 展开入库

### 1. 背景

上一轮修复推送后，副机 `vdsh`（dsh web 启动）报：

```
Error: dsh: C:\Users\V.Reason\.dsh\profiles\web\.dsh-module-fallback\node_modules\@codemirror\commands
exists and is not a symlink or dsh-managed module proxy; remove it so dsh can manage the installation fallback
```

并非路径解析问题（那轮已解决），而是同步数据本身被污染。

### 2. 根因（真实数据验证）

| 证据 | 说明 |
|---|---|
| 主力机 `profiles/web/.dsh-module-fallback/node_modules` 全为 **junction**（LinkType=Junction，指向 `profiles/web/node_modules` 的 pnpm 安装） | 该目录是 dsh 管理的数据：本机链接/proxy，纯可再生缓存 |
| `.dsh` 仓库 `git ls-files` 计数：**12777 个跟踪文件中 12618 个是 `profiles/web/.dsh-module-fallback/node_modules/**`** | Windows git 默认（core.symlinks 关闭）把 junction 当**普通目录递归**，展开成真实文件入库 |
| `.gitignore` 只有 `profiles/web/node_modules/`，**没有 `.dsh-module-fallback/`** | 漏规则 → 该目录随 `git add -A` 全量入库 |
| 副机 checkout 后 `@codemirror/commands` 是真实目录（无链接） | dsh 启动 `ensureSymlink` 校验「symlink 或 dsh-managed proxy」失败 → 抛错，即用户所见 |

app-boot 判定（`packages/boot/app-boot/lib/index.js:416`）：非 symlink 时，目录内 `package.json` 有 `dsh.moduleFallback.targets`（`!== void 0`）才是合法 proxy，否则抛错。

### 3. 改动清单

**A. 同步卫生（防再次入库）——`dsh-data-git-sync/sync-dsh.ps1`**
- `.gitignore` 生成模板新增 `profiles/*/.dsh-module-fallback/`（带注释说明）；
- 新增 `Ensure-BuiltinIgnoreRules`：`.gitignore` 已存在时也幂等补写内置规则（不依赖 `gitignore_extra`，`init` 与 push/pull 路径共用）；
- 新增 `Repair-ModuleFallbackTracking`：`git ls-files` 检测历史误跟踪 → `git rm -r --cached`（**仅索引，工作区文件保留**）→ 本次 push 的提交携带删除记录，副机拉取后自动清理；置于 `Sync-Push` 的 `git add` 之前（先补 `.gitignore` 再移出索引，避免被 `add -A` 重新加回）；
- `Sync-Pull` 脏区提示：变更集中在 `.dsh-module-fallback` 时给出「整体删除缓存目录（dsh 自动重建）再 pull」的命令。

**B. 启动自愈（vdsh 双通道）——新增 `vdsh/module_fallback.py`**
- `heal_module_fallback(data_dir)`：纯标准库，按 app-boot 规则清理：symlink/junction（`os.path.islink` + `os.path.isjunction`(3.12+) + `st_reparse_tag`/`st_file_attributes & 0x400` 兜底）保留；dsh proxy（`package.json` 含 `dsh.moduleFallback.targets` 键）保留；真实目录/文件 → 删除；空 @scope 壳 → 删除；
- `vdsh/features/launch.py`：启动前（CLI 预检后）调用，`data_dir` 取 `effective_data_dir(settings)` 或 `~/.dsh`，清理数 > 0 时 `warn` 说明；
- `dsh_cli.py`：转发前调用（`DSH_HOME` env → vdsh.yaml `sync.data_dir` → `~/.dsh`），`import` 失败/异常仅提示不阻断（dsh 自身报错兜底）。

**C. 本次已执行的数据仓库修复（主力机 `.dsh` 仓库）**
- 追加 `.gitignore` 内置规则 → `git rm -r --cached profiles/web/.dsh-module-fallback`（12618 文件移出索引，工作区 junction 完好）→ 提交并推送：`886f23e`（untrack）+ `fcbe48c`（gitignore 规则）。副机下次 `pull` 后工作区真实目录被 git 移除，dsh 启动重建。

### 4. 关键决策与取舍

- **自愈判定与 app-boot 逐位对齐**（不是「凡是目录就删」）：junction/proxy 都是 dsh 合法数据，误删会让主力机启动行为改变；只删「非链接且非 proxy」，与 `ensureSymlink` 的 throw 条件严格互补。
- **删除动作只针对同步污染**，且优先「存索引→同步→重建」而非直接动工作区：`git rm --cached` 让主机 junction 无感，副机靠 git 删除记录自动清理，两侧行为一致。
- **自愈放 Python 层（launch + dsh_cli）而非 PS 层**：启动路径已经都在 Python；PS 脚本只管同步卫生；复用无新依赖（纯标准库）。
- **豁免失败**：清理失败不阻断启动 —— 理论上 dsh 自身报错仍会给出指引；自愈是「大概率自动救回」，不是强保证。
- **`dsh_cli.py` 的 `_config_value` 通用化**：原 `_repo_from_config` 是针对 `repo` 的专用正则，抽成按 key 读取（仍文本级 + 剥行尾注释 + JSON 反解），`repo` 与 `data_dir` 同源复用，避免第三份实现。

### 5. 验证情况

| 项 | 方法 | 结果 |
|---|---|---|
| 自愈逻辑 | 临时目录模拟：junction + @scope/junction + proxy + 顶层真实目录 + @scope 真实目录 + 游离文件 + 空 @scope 壳（`vdsh_mf_test.py`） | 7 项断言全 PASS；清除 4 项（真实目录×2、文件、空壳），junction/proxy 保留；初版「空壳未计数」已修复 |
| Python 全量 | `python -m py_compile`（module_fallback/launch/dsh_cli） | 0 |
| PS 脚本 | `[Parser]::ParseFile` + BOM | 0 / `EF BB BF`（编辑后已恢复） |
| 数据仓库卫生 | 实测 `.dsh`：`git rm -r --cached` 移出 12618 文件 → commit → push `79bd2ec..fcbe48c` | ✓；工作区 junction 完好（`Test-Path node_modules` = True）；status 无残留 |
| 误跟踪检测 | `git ls-files -- profiles/web/.dsh-module-fallback` | 移出后为空 ✓ |

**未做实机验证**：副机真实 `pull` → dsh 重建全链路（副机不在本机可操作范围）；残留风险极低（删除记录 + 启动自愈双保险）。

### 6. 遗留与后续迭代提示

- **任何含 junction/reparse point 的目录都可能被 git 展开**：同步数据里发现新「真实目录」异常时，先 `git ls-files | 查路径` 确认是否被跟踪，规则进 `BuiltinIgnoreRules` 而非 `gitignore_extra`。
- `profiles/node_modules/`（顶层共享层）已在既有 `.gitignore` 覆盖；若未来 dsh 增加其它回退/缓存目录，同样追加内置规则。
- `heal_module_fallback` 的 reparse 判定在 Python < 3.12 走 `st_file_attributes` 兜底（本机 3.13 用 `os.path.isjunction` + `st_reparse_tag`）；低版本 Python 如报 `AttributeError` 请报错反馈。
- `git rm -r --cached` 的删除记录如果与其它机器本地提交分叉，pull 时可能产生 delete/modify 冲突——`Sync-Pull` 已给「删目录重试」指引。
- 理论上 `profiles/web/node_modules/` 同样会被展开（旧库已处理）；`vdsh sync status` 现两目录都应有规则且无跟踪。

### 7. 复测清单（回归用）

```powershell
python -X utf8 -m py_compile vdsh_launcher.py dsh_cli.py vdsh\app.py vdsh\config.py vdsh\settings.py vdsh\spinner.py vdsh\bootstrap.py vdsh\features\sync.py vdsh\features\launch.py vdsh\module_fallback.py
# PS 解析 + BOM
$p = (Resolve-Path .\dsh-data-git-sync\sync-dsh.ps1); $e=$null;$t=$null
[System.Management.Automation.Language.Parser]::ParseFile($p,[ref]$t,[ref]$e)|Out-Null; $e
[IO.File]::ReadAllBytes($p)[0..2]
# 自愈冒烟：临时目录模拟后确认只清污染（见 §5 表）
python -X utf8 $env:TEMP\vdsh_mf_test.py
# 只读冒烟
python -X utf8 vdsh_launcher.py sync status
git -C C:\Users\V.Reason\.dsh ls-files -- profiles/web/.dsh-module-fallback   # 应无输出
```

---

## 2026-09-08：同步体验优化 + 副机路径自愈 + 启动失败即时反馈

### 1. 背景

用户提出 5 项体验问题（附副机 `dsh web` 报错 annex）：

1. `vdsh sync init` 的「获取远端数据」无进度反馈（有转轮动画但无内容级进度），长时间空转无法察觉；
2. `vdsh sync status` 待推送文件单行拼接，一长串难读；
3. 副机 `vdsh sync init` 需要带 URL 参数，应改为交互式填写 `repo / data_dir / remote` 并做路径校验；
4. 副机首次拉取后 `dsh web` 报 `Cannot find module 'T:\deepseek-harness\apps\cli\lib\bin.js'`；
5. `vdsh` 启动时长时间空转无反馈，而 `dsh web` 直启能立刻看到报错。

### 2. 根因盘点（调查结论）

| 条目 | 根因 | 落点 |
|---|---|---|
| 1 | 经 vdsh 调用时 stdout 被捕获 → PS 层 `Invoke-GitSpinner` 按设计退让；git 的 stderr 非 TTY 时不输出进度（无 `--progress`），且 PS 侧缓冲到命令结束才打印 | `sync-dsh.ps1` `Invoke-DshGit` |
| 2 | `Sync-Status` 单行 `（a, b, …）` + 截 5 条 | `sync-dsh.ps1` `Sync-Status` |
| 3 | `run_sync` 无 URL 时仅回退 `sync.remote` 或报错，无交互/校验 | `vdsh/features/sync.py` |
| 4 | **不是 `.dsh` 数据问题**（已核查 settings.yaml/.gitignore/.agent-presets/profiles 配置均无 T: 路径；`storages/workspace.json` 只是普通工作区数据）——是 **`dsh.cmd` 硬编码** `node "T:\deepseek-harness\apps\cli\lib\bin.js"`；另 `config.py DEFAULT_REPO`、`settings.py TEMPLATE` 也带 T: | `dsh.cmd` → 新增 `dsh_cli.py` |
| 5 | `spawn_server` 用 `pwsh -NoExit … *> 日志`：node 崩溃后 pwsh 因 `-NoExit` 不退出 → launcher 无法用句柄感知进程死亡，只能空等 `startup_timeout_seconds`(180s)；且启动前无 CLI/node 预检 | `vdsh/features/launch.py` |

### 3. 改动清单

**条目 1 —— init/push/pull 实时进度**
- `dsh-data-git-sync/sync-dsh.ps1`：
  - `Invoke-DshGit` 非动画分支（stdout 被捕获）改为**逐行流式透传**（`… | ForEach-Object { Write-Host ("$_") }`），不再 `$captured` 收集后统一打印；
  - init/pull 的 `fetch`、push 的两次 `push` 均加 `--progress`（非 TTY 下 git 用换行分隔输出进度更新，可被 PowerShell 管道实时消费）；
- `vdsh/spinner.py`：`Spinner.say()` 收到 `→ ` 开头的步骤行时在锁内刷新 `self.message`，长等待期间转轮显示当前动作（非 TTY 分支同步处理）。

**条目 2 —— status 待推送分行**
- `sync-dsh.ps1` `Sync-Status`：`待推送: N 个变更:` + 每行 `  - 路径`（最多 10 条，超出显示「… 以及另外 N 个（完整明细: git -C … status --short）」）；`Sync-Pull` 脏区提示同款分行。

**条目 3 —— 副机交互式 init**
- `vdsh/features/sync.py` 新增交互向导（`_interactive_init` 及校验函数）：
  - 无参 + `sys.stdin.isatty()` → 三步提问 `launcher.repo` / `sync.data_dir` / `sync.remote`，逐项校验、写回 vdsh.yaml（`patch_values`）后执行 init；`s`/`skip`/`q` = 跳过该项，EOF/Ctrl+C = 整体取消（返回 0，不执行 init）；
  - 非交互（非 TTY）保持旧行为：回退 `sync.remote`，缺失报用法错误（退出码 2）；
  - 校验规则：repo 须含 `package.json`（当前配置无效时默认改用 `probe_repo()` 探测值）；data_dir 须为绝对路径（支持 `~`）、存在时须为目录；remote 支持 `file:///X:/…`、`file://C:/…`（漏第三个斜杠）、`X:\…`、`/X:/…`、UNC `\\server\share\…`、`http(s)://…`，file/本地路径检查存在性与裸仓库形态（`HEAD`+`objects`），http(s) 仅语法校验；残留的 `…/T:/…` 主力机路径会明确提示「映射共享盘后用 Z:/ 或 UNC」。

**条目 4 —— 路径不再硬编码（dsh + vdsh 双通道）**
- 新增 `dsh_cli.py` + 改写 `dsh.cmd`：解析链 `DSH_REPO`（显式优先）→ `vdsh.yaml launcher.repo`（文本级正则读取，先剥行尾注释再 JSON 反解）→ `REPO_CANDIDATES` 探测（`C:\deepseek-harness`、`T:\deepseek-harness`）；产物缺失/全部无效给出明确指引并退出 1；参数原样转发 node，退出码透传。纯标准库（不引入 pyyaml/requests）。
- `vdsh/config.py`：新增 `REPO_CANDIDATES`；`vdsh/settings.py`：新增 `probe_repo()`/`normalize_repo()`；`vdsh/bootstrap.py`：向导默认值先探测；`vdsh/features/launch.py`：`launcher.repo` 无效（且非 `DSH_REPO` 显式覆盖）时探测并 `patch_values` 自愈后继续。
- 顺带修复 `sync-dsh.ps1 Get-VdgConfigRemote`：值后行尾注释未剥离导致「git origin 与 vdsh.yaml 不一致」误报（当前真实配置即触发）。

**条目 5 —— 启动失败即时反馈**
- `vdsh/features/launch.py`：
  - 预检：node（`spawn_server` 内）、CLI 产物 `repo/apps/cli/lib/bin.js`（构建步骤后，缺失 `die` 并给指引）；
  - `spawn_server` 去掉 `-NoExit`（node 退出/崩溃 → 窗口自动关闭、进程树完结）并返回 Popen 句柄；
  - `wait_until_ready(..., proc=...)`：轮询中「就绪前子进程已退出」→ 停止动画、取 `WEB_URL_LOG` 日志尾（`_log_tail`，20 行），抛 `ServerExitedError`，`run()` 捕获后 `die`（非 0 退出码）。「starting」分支（端口被占、无子进程句柄）不引入该检测。

**文档**：README / doc/usage.md / doc/design.md / doc/dev.md / dsh-data-git-sync/README.md 与 docs/native-git-sync.md（副机接入流程、进度说明、init 无参向导、status 分列、文件表）。

### 4. 关键决策与取舍

- **进度反馈用 git 原生 `--progress` + 流式透传**，不做字节级统计/网络探测：git 自身输出即权威；PS 管道天然逐行，无需 runspace 二次转发。PS 层动画分支（直接终端）保持「runspace 缓冲、结束统一打印」不变。
- **向导放 Python 层而非 PS 层**：vdsh.yaml 的读取/校验/`patch_values` 属 launcher 职责域（PS 侧只有 Set-VdgConfigRemote 一条文本补丁通路）；脚本侧 `Sync-Init` 只管 init 本体，退出码 0-4 契约不变。
- **校验是「失败重试 + 可跳过 + 可取消」而不是拦截**：向导只保证「答错会重新问」，不阻塞跑到一半的用户（`s` 跳过、Ctrl+C 取消）；远程校验对「非裸仓库/路径不存在」给明确原因，允许用户带参直跑 `vdsh sync init <URL>` 绕过向导校验。
- **路径自愈不覆盖显式配置**：`DSH_REPO` 环境变量始终尊重（launch 与 dsh_cli 一致）；自愈写回 vdsh.yaml 只发生在「配置值无效且非 env 覆盖」时。
- **启动失败检测只认自己拉起的进程**：proc 句柄来自 `spawn_server` 的 Popen；不做日志关键词启发式（避免插件正常告警误判），「starting」分支（第三方进程）维持原有 30s 预算 + 超时提示。
- **待推送分列上限 10 条**：兼顾直观与不刷屏；超出显示截断行与 `git status --short` 路径。
- **`dsh.cmd` 保持 shim 语义**：解析是读操作（不写配置），命中候选时提示「建议 vdsh setup 固化」。

### 5. 验证情况

| 项 | 方法 | 结果 |
|---|---|---|
| Python 全量 | `python -m py_compile`（含 `-W error::SyntaxWarning`） | 0 |
| PS 脚本 | `[Parser]::ParseFile` + BOM 前 3 字节校验 | OK / `EF BB BF`（编辑工具会剥 BOM，已幂等恢复） |
| `vdsh sync status` | 真实数据仓库实跑（只读） | 6 项待推送逐行列出 ✓；`sync remote` 一致（不再误报） |
| fetch 流式 | 临时 bare+mirror，构造增量对象后重定向跑 `fetch --progress` | 每行 `remote: …`/`Counting…` 逐条出现；无 `--progress` 对照 = 无输出（即当初空转原因） |
| 向导 | 临时 vdsh.yaml + 临时 bare 仓库 + 假 input 模拟 | 全部校验分支（file:///、file://C:、盘符、根路径、UNC 形态、缺失、普通目录、http 语法）符合预期；三键写入 vdsh.yaml ✓ |
| Spinner | 假 TTY 断言 `say("→ 获取远端数据…")` 后 `message` 刷新 | ✓ |
| 启动失败 | 假进程对象 + 假日志，`proc.poll()!=None` → `ServerExitedError` 带日志尾 | ✓ |
| dsh_cli | `_resolve()` 三来源 + 无效 DSH_REPO 报错路径 | ✓ |

**未做实机验证**：真·副机两台机器全流程（共享盘/Z: 映射、`vdsh sync init` 向导 → pull → `pnpm install`）；本次为单机模拟。首次真机使用后建议把实测结果补到 §8 demo 或本表。

### 6. 遗留与后续迭代提示

- `REPO_CANDIDATES`（config.py）是**硬编码候选列表**（`C:\deepseek-harness`、`T:\deepseek-harness`）：换机器/装新位置要加候选，或后续改为自动探测（注册表/同级目录/`C:\` 浅层扫描）。
- `dsh_cli.py` 与 `sync-dsh.ps1 Get-VdgConfigRemote` 是**两份文本级读取实现**（剥注释→JSON 反解），规则必须保持一致；改一处要同步另一处（与 `patch_values`/`Set-VdgConfigRemote` 的既有双实现约定相同）。
- `vdsh sync init` 无参语义变化：TTY 下由「直接取 sync.remote」改为「向导（回车确认默认值即等价）」；`doc/demo.md` §5 迁移流程、`dsh-data-git-sync/README.md` 已同步。若某处脚本化调用 `init`（非 TTY）行为不变。
- init 的 fetch 失败仍返回 0（「origin 暂不可达」语义，配合 `→ 注意` 行）；如需严格失败请在 pull/push 场景（退出码 1）。
- 启动失败检测覆盖「就绪前进程退出」；若 node 在就绪后崩溃，进程退出不再报（服务已可用，符合预期）。
- `spinner.say` 的 `→ ` 步骤行刷新会改变任何经 `run_child_progress` 运行的子脚本转轮消息（build/update 同款受益）；若某天步骤行不是 `→ ` 前缀，刷新逻辑不会触发（不误改）。
- 副机向导中「已确认是裸仓库」等提示面向 `file://`/本地路径；`http(s)` 远端无法校验存在性，属刻意取舍。

### 7. 复测清单（回归用）

```powershell
python -X utf8 -m py_compile vdsh_launcher.py dsh_cli.py vdsh\app.py vdsh\config.py vdsh\settings.py vdsh\spinner.py vdsh\bootstrap.py vdsh\features\sync.py vdsh\features\launch.py
# PS 解析 + BOM
$p = (Resolve-Path .\dsh-data-git-sync\sync-dsh.ps1); $e=$null;$t=$null
[System.Management.Automation.Language.Parser]::ParseFile($p,[ref]$t,[ref]$e)|Out-Null; $e
[IO.File]::ReadAllBytes($p)[0..2]
# 只读冒烟
python -X utf8 vdsh_launcher.py --help
python -X utf8 vdsh_launcher.py sync status
python -X utf8 vdsh_launcher.py sync remote
```
