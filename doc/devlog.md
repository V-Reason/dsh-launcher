# 开发日志（迭代记录）

按**一次迭代一条记录**追加，供后续迭代开发回溯「当时为什么这么做、测过什么、还剩什么」。
每条记录含：背景 → 改动清单（按需求条目）→ 关键决策与取舍 → 验证情况 → 遗留与后续提示。
前置阅读：`design.md`（架构）→ `dev.md`（开发规程）；踩坑细节见 `experience.md`。

---

## 2026-09-10（第三波）：`vdsh update dsh` 构建失败「无证据」修复

### 1. 背景

用户跑 `vdsh update dsh`：**能正确拉取远端数据**，但**构建时报错**；随后在 dsh 根目录手动
`pnpm install && pnpm run build` 正常，于是怀疑是误报或执行逻辑有问题。

### 2. 排查结论（先定性，再改码）

| 取证 | 方法 | 结果 |
|---|---|---|
| 调用链是否可用 | 用 vdsh 自己的 `run_child_progress` 跑**真实** `pnpm run build`（cwd = 真实仓库，`quiet=Noise()`） | **exit=0**、25.7s、4084 行（`build: recorded 234 client artifact(s)`）→ 调用链与折叠机制无问题 |
| 同上，`pnpm install` | 同执行器 | exit=0、506ms、`Done in …` 正确折叠、`noise.observed=True` |
| 失败发生在哪一步 | `git reflog` + `pnpm-lock.yaml`/`.modules.yaml`/构建产物 mtime | merge @14:34:47 → lockfile 写入 @14:35:54（**install 成功**）→ 产物 @14:39（用户手动构建）→ **只有 build 那一次没过** |
| 环境是否同构 | `pnpm config get engine-strict`、`.npmrc`、Node/pnpm 版本 | 未设置、无、Node 24.15 + pnpm 11.7.0（`packageManager` 钉住）→ 手动与 launcher 无可见差异 |

**结论**：真实失败原因**不可复原**——改前构建的 4000+ 行输出只喂给折叠器，失败时只打
`构建失败（exit code N），请手动检查`：没有 tail、没有日志、没有阶段说明，**真失败与误报在终端上不可区分**。
这类「无证据失败」就是缺陷本身（详见 `experience.md` §9.1）。

顺带查出同一路径上会把「环境问题」伪装成「构建失败」的三处：命令起不来抛 traceback、
pnpm 缺失错报 `EXIT_BUILD(3)`、快进失败只会说「请检查冲突」；
以及两个子进程坑：`proc.stdout.close()` 会在读线程持锁时把函数挂死、
`for line in proc.stdout` 只在 EOF 结束（后代抱住写端即永久挂住）。

### 3. 改动清单

1. **`vdsh/spinner.py`**：`run_child_progress` 新增 `tail_out`（含**被折叠行**的完整行序，供失败复述；传了它就由调用方负责呈现，本函数不再补打折叠行——避免同一批行出现两次）；
   读循环改为「读线程 + 队列」，**收尾判据是「进程已退出（`proc.poll()`）就取走队列已有行」，不是等 EOF**（本机实测管道 EOF 与进程退出无时序保证，读线程会一直卡在 `readline()`）；
   另有 `reader_done + 未完成任务数` 快路径与 `STALL_SECONDS=600`（进程仍在跑但零输出）兜底；
   `Popen` 失败返回新增的 `EXIT_SPAWN_FAILED=127` 并打 `vdsh ⚠ 无法启动命令（…）`（不再抛 traceback）；
   收尾新增 `_release_stream()`——读线程 `join(2s)` 后才 close，未退出就不 close（**这正是「有卡死检测却仍然挂住」的真因**）；
   折叠行补打改走 stderr（stdout 被重定向也可见）。
2. **`vdsh/config.py`**：新增 `BUILD_LOG_PATH`（`%TEMP%\vdsh-build.log`）、`BUILD_TAIL_LINES=15`；退出码注释与 usage 对齐（3=构建失败、4=依赖缺失含 node/pnpm）。
3. **`vdsh/features/build.py`**：`run_build(repo, pnpm=None, code_is_new=False)`——`collect` 收全量（**不用 `tail_out`**：让 `run_child_progress` 补打折叠行，自己只复述末尾）→ 失败时补打**末尾 15 行** + 落盘完整日志 + 点明「仓库代码已更新、仅构建未完成」（`code_is_new` 由 update 段传入，`vdsh build` 不会误用该句）；成功即删日志（只留最近一次失败现场）；缺 pnpm / 起不来归 `EXIT_DEPS(4)`。
4. **`vdsh/features/update.py`**：新增 `_git_stdout`（只取 stdout）用于**脏工作区判定**与分支名解析；上游名加形状校验 `_valid_upstream`（两段、无空白/控制字符，**刻意不限制非 ASCII**——git 允许中文分支名，ASCII 白名单会把合法仓库判成「未配置上游」）；快进失败且本地无领先提交时改指向「上游被 force push」+ `git log HEAD..<upstream>` / `git reset --hard <upstream>` 两条路；未提交改动的确认文案补「合并可能失败或覆盖本地改动」。
5. **文档**：`usage.md`（`update dsh` 失败时看什么 + 退出码表拆清）、`dev.md`（职责表两行 + §3 新增「失败诊断最低要求」+ §5 新增第 7 项测试）、`experience.md` §9（新增「子进程与失败诊断」主题：无证据失败、两个子进程坑、git 判定）、本条目。

### 4. 关键决策与取舍

- **不假装知道根因**：真实失败已不可复原，本轮只做「让下一次失败可诊断」，并在文档里写明取证边界——比编一个解释更有价值。
- **失败诊断不受输出简约约束**（沿用既有例外条款）：末尾 15 行 + 日志路径 + 阶段说明进输出；成功路径形态一字不改。
- **不做自动重试/自动回滚**：对 25s+ 的构建自动重试会掩盖真问题；把证据交给人，恢复动作写进文案。
- **日志落 `%TEMP%` 固定路径、成功即删**：失败现场可复制粘贴去查，成功路径不给 `%TEMP%` 堆垃圾。
- **`STALL_SECONDS=600` 只在「完全无输出」时触发**：正常构建每几秒必有输出，不会误杀。
- **`tail_out` 不做长度限制**：调用方决定截断；构建日志是权威来源，内存成本可忽略。

### 5. 验证情况

| 项 | 命令/方法 | 结果 |
|---|---|---|
| 编译 | `python -m py_compile`（涉及 4 个模块）+ `python vdsh_launcher.py --help` | 通过 |
| 分流/动画回归（扩展为 10 组） | `%TEMP%\vdsh_spinner_check.py` | 全部通过（原 5 组不变；新增 `tail_out` 完整性、**后代抱写端时按「进程已退出」立即收尾**（<15s，修复前 60.1s）、**进程仍在跑且零输出时 STALL 有界终止**、**瞬退子进程 5 次往返不丢行**、命令不存在返回 127 且无 traceback） |
| `update dsh` 判定与诊断（新增） | `%TEMP%\vdsh_update_dsh_check.py`（离线：stub pnpm + 临时 git 仓库） | 全部通过：失败诊断（末尾输出/日志落盘/`code_is_new` 文案/退出码 3）、`vdsh build` 不误宣称、成功清理日志、命令缺失退 4、`_git_stdout` 只取 stdout、上游名形状校验（接受 `origin/master` 与 `origin/中文分支`，拒绝警告文本/空/控制字符）、上游名不合法退 2、三条判定（已是最新/有更新/快进失败） |
| profile 校验回归 | `%TEMP%\vdsh_profile_state_check.py` | 全部通过（未受影响） |
| 插件输出预览回归 | `%TEMP%\vdsh_output_preview.py` | 退出码 0、输出形态不变（未受影响） |
| 三条命令输出预览 | `%TEMP%\vdsh_standard_preview.py`（新增 build 失败段） | 成功/失败/update/launch 形态符合约定：失败段为「真输出 → 折叠行补打 → 末尾 15 行 → 日志路径 → 阶段说明 → `vdsh ✗`」 |
| 真机构建（新读循环 + 日志清理） | `python vdsh_launcher.py build`（真实仓库，29.0s） | 退出码 0、`vdsh ✓ 构建完成`、`%TEMP%\vdsh-build.log` 已清理 |
| 真机只读复测 | `%TEMP%\vdsh_update_live_check.py`（跳过运行中询问，其余真跑） | `vdsh ✓ dsh 已是最新（0.1.5-rc.1）`，退出码 0 |
| 真机 `vdsh update dsh` | 本会话 GUI 正跑在该实例上（`auth`），非交互下按设计中止 | 行为符合预期（停止服务后可作为下一次真机验证） |
| 未执行 | 失败构建的**真机**复现（需真有远端更新 + 构建真的失败） | 见 §6 |

### 6. 遗留与后续迭代提示

- **下一次真机失败即验收**：等真有远端更新时跑一次 `vdsh update dsh`；若构建又失败，终端应给出
  末尾 15 行 + `%TEMP%\vdsh-build.log` 路径——把那段输出贴回来即可定位真实根因（这是本轮无法完成的一步）。
- `STALL_SECONDS`（600s）尚无真机样本；若遇到原生编译等长静默阶段被误杀，调大该常量即可。
  （注意它现在只兜「进程仍在跑但零输出」；「直系子进程已退出、后代抱句柄」已由 `proc.poll()` 判据立即收尾。）
- `spinner.say()` 非 TTY 分支仍是裸 `print`（非 TTY 下即「原样透传」，不套前缀）；若要彻底统一可后续走 `console.say`。
- `_release_stream` 在极端情况下会放弃 close（留一个 daemon 线程）；这是「宁可有界返回，不要挂死」的取舍，未再优化。
- **本轮最大的返工点**：最初按「等 EOF + 哨兵行」实现收尾，先撞上「哨兵排在未取走的行前面 → 瞬退进程丢输出」，
  改成「`reader_done` + 未完成任务数」后又撞上「读线程等不到 EOF → 主循环永久 `get()`」，
  最终才落到「进程已退出即取走队列已有行」。**结论：进程退出是权威信号，流关闭不是**（见 experience.md §9.2）。

---

## 2026-09-10（第一波）：输出标准统一（全体命令对齐同一套元素）

### 1. 背景

上一波把 `vdsh update plugin` 的输出按「转轮=任务性质+秒数、`→`=进度、`vdsh ·`=节点、`vdsh ✓/✗`=结论」重做后，
用户要求**其它命令也往这个标准靠**，同时明确「特殊项特殊处理」。

### 2. 标准与例外（先定死，再改码）

- **四种元素**：转轮 `⠋ 任务性质 47s`（消息不带 `…`、不写 pnpm 内部计数）、`→ 事实`（进度）、`vdsh · …`（节点/收尾数据）、
  `vdsh ✓/✗/⚠`（结论）；成功任务的耗时为**独立一行** `vdsh · 用时 1m46s`。解释性长文不进输出（归文档与 `vdsh doctor`）。
- **例外（特殊项）**：报告类（`config`/`doctor`/`sync status`）用各自报告格式；向导类（首次向导、`setup`、`sync init` 无参、
  构建/更新前确认）保留人机对话形态；`sync-dsh.ps1` 可脱离 vdsh 独立运行，保留自有文案；`dsh.cmd` 转发壳前缀为 `dsh`；
  失败诊断（exit code、下一步命令、被折叠行补打）不受简约约束。

### 3. 改动清单

1. **新增 `vdsh/pnpm_log.py`**：把 `update.py` 里的 pnpm 分流逻辑（`ANSI_RE`/`QUIET_RULES`/`Noise`/`has_network_failure`）抽成共享模块，
   供 `update`（两条 pnpm 路径）与 `build` 复用（原先只有 update 接入了折叠，`pnpm run build` 的 `Done in …`、peer 提示会漏出来）。
2. **`vdsh/console.py`**：新增 `progress()`（`→` 行）与 `ok()`（`✓` 结论行），与既有 `step/warn/die/fail` 组成完整词表。
3. **`features/update.py`**：`update dsh` 段对齐——`→ 远端更新 3 个提交`、`→ 本地另有 N 个提交（常规合并…）`、`→ 版本 A → B`、
   `vdsh ✓ dsh 已更新（版本）` + `vdsh · 用时 …`；转轮消息去掉省略号；确认告警与重启提醒压短。插件段维持上一波形态。
4. **`features/build.py`**：接入 `Noise`（构建工具输出照常透传，只折叠 pnpm 自身行），转轮消息 `构建`，
   结束 `vdsh ✓ 构建完成` + `vdsh · 用时 …`（原为 `构建完成。`）。
5. **`features/launch.py`**：`say("vdsh · …")` 的裸前缀改走 console——就绪/已在运行改为 `vdsh ✓`，手机访问与用时改 `vdsh ·`；
   转轮消息 `启动 dsh web` / `等待实例就绪`；三处啰嗦告警压短（仓库自愈、认证提示、回退目录清理）。
6. **`features/sync.py` / `bootstrap.py`**：清掉裸 `print("vdsh …")`（dev.md §3 早就要求走 console），
   改用 `step/warn/fail`；`sync` 转轮消息去省略号；`auto_sync_pull` 结论改 `step/warn`（同步本身的 `✓/耗时` 由脚本打印，不重复）。
7. **`features/doctor.py`/`cli.py`/`config.py`**：**不改**——报告类与用法输出属特殊项（doctor 已是 `✓/✗/⚠/—` 体系）。
8. **文档**：`usage.md` 新增「输出约定与常见告警」一节（四元素表 + 各命令形态 + 例外清单）；`dev.md` §3 把标准写成**编码约定**（约束后续代码）并补 `console.py`/`pnpm_log.py` 目录职责行；本条目。

### 4. 关键决策与取舍

- **标准写进 dev.md §3，而不只是 usage.md**：输出形态是「后续代码要遵守的约束」，放在开发约定里才有约束力；
  usage.md 只讲用户看到什么。
- **pnpm 分流抽成独立模块**：build 也需要同一套折叠（`pnpm run build` 同样会打 peer 提示与 `Done in …`）；
  放 `spinner` 会把 pnpm 语义塞进动画机制层，放 `update` 则要 feature 互相 import。
- **`sync-dsh.ps1` 不动**（BOM 风险 + 它可独立运行）：脚本保留 `→ 步骤` / `✓ 结论` / `  耗时 2.3s` 自有文案，
  vdsh 不重复打印结论（否则一次同步出现两个 ✓）。
- **`vdsh build` 的第三方输出照旧透传**：`vite …`、`> dsh@… build` 是构建工具的真话，折叠它等于隐瞒；只折叠 pnpm 自己的样板行。
- **报告与向导不强行套格式**：`config`/`doctor`/`sync status` 是读报告，向导是人机对话，套「转轮+结论」反而更难读。

### 5. 验证情况

| 项 | 命令/方法 | 结果 |
|---|---|---|
| 编译 | `python -m py_compile`（全量） | 通过 |
| 输出预览（build） | 假 TTY + 样本 pnpm/构建日志 | `→ 解析 174 · 复用 7` → 构建工具输出原样 → `vdsh ✓ 构建完成` → `vdsh · 用时 2.5s`；`Done in …`/peer 提示被折叠 |
| 输出预览（update dsh） | stub git/版本/pnpm + 假 TTY | `→ 远端更新 3 个提交` / `→ 版本 0.1.2 → 0.1.3` / `vdsh ✓ dsh 已更新（0.1.3）` / `vdsh · 用时 0.9s` / `vdsh ⚠ 重启 dsh web 后生效` |
| 输出预览（launch 两条路径） | stub 探测/就绪/RPC | 全新启动：`vdsh · 启动 dsh web（工作区 …）` → `vdsh ✓ dsh web 就绪 → URL` → `vdsh · 用时 …` → `vdsh · 手机访问 → …`；已在运行：`vdsh ✓ dsh web 已在运行 → URL` |
| 分流用例（5 组） | `%TEMP%\vdsh_spinner_check.py` | 全部通过（改用 `pnpm_log.Noise`；stdout/stderr 同流断言） |
| profile 校验用例（11 组） | `%TEMP%\vdsh_profile_state_check.py` | 全部通过（未受影响） |
| 真机只读命令 | `vdsh sync status` / `vdsh config` / `vdsh doctor` | 退出码 0；`sync status` 保持脚本自有格式，doctor 报告格式不变 |
| 未执行 | `vdsh`（真启动）、`vdsh build`、`vdsh update dsh` 真机 | 需要停服/耗时构建，见 §6 |

### 6. 遗留与后续迭代提示

- `vdsh`（真启动）、`vdsh build`、`vdsh update dsh` 的**真机观感未验**：预览用的是 stub 子进程，实际观感请在真终端各跑一次。
- `sync-dsh.ps1` 的 `  耗时 2.3s` 与 vdsh 的 `vdsh · 用时 …` 仍是两种写法（脚本独立运行所需）；若日后要求完全统一，需按 dev.md §5.4 的 BOM 流程改脚本。
- `app.py` / `settings.py` 仍有少量导入期/早期 `print("vdsh …")`（console 尚未可用或刻意不引入依赖），属特殊项。

---

## 2026-09-10：插件更新可信化 + 输出按「进度/节点/结论」重做 + pnpm `[WARN]` 排查

### 1. 背景

用户跑 `vdsh update plugin` 看到成片 `[WARN]`（`HEAD https://github.com/… error (ECONNRESET/ETIMEDOUT). Will retry in …` 与
`Issues with peer dependencies found`），怀疑是错误或被吞掉的错误；随后提出三个需求：**（a）要能保证插件「确确实实」被正确更新**；
**（b）更新时的 TTY 动画要去掉多余无关的说明**；**（c）只在乎「当前更新进度」和「是否成功更新」**——第一版实现（折叠噪声 + 长结论行尾注）
仍不达标：转轮只显示 pnpm 内部计数、结论行塞满解释。最终形态按用户给的视觉约定重做：
`⠋ 任务名 47s`（任务性质 + 秒数）、`→ …`（进度）、`vdsh · …`（节点）、`vdsh ✓/✗`（结论）。

### 2. 排查结论（先定性，再改码）

- `[WARN]` 不来自 vdsh（vdsh 只用 `vdsh ·/⚠/✗`）。HEAD 只有一处来源：pnpm 的 `isRepoPublic()` 探针（`method:'HEAD'` +
  `retry:{retries:2,factor:2,minTimeout:500}`），失败被 `catch { return false }` 吞掉，只影响「tarball vs git 解析」，**非错误**。
- peer 提示是 DSH 的 profile 设计（`app-boot/src/profile.ts` 写 `nodeLinker: hoisted` + `autoInstallPeers: false`，
  peer 由 `$DSH_HOME/profiles/node_modules` 安装层提供）；`pnpm peers check` 的 16 项全部可解析。
- `Packages: -2` 是 pnpm 统计行（清 2 个过期 node_modules 条目），manifest/lockfile 未变，5/5 依赖在位。
- **底层条件是真的**：实测 `github.com:443` TCP FAIL，而 `github.com:22`/`codeload.github.com:443`/`registry.npmjs.org:443` 均 OPEN
  → 只阻断该端点；pnpm 因此回退 git/SSH 解析（lockfile 记为 `git+ssh://…#<sha>`），代价仅耗时。
- 排查中发现两个**真问题**：旧实现只比已装包 `version`，git 依赖「commit 变了、版本号没变」会被误判为「无变化」；
  且无法区分「装了 ≠ 生效」。

### 3. 改动清单

1. **新增 `vdsh/profile_state.py`**：三重证据校验（`package.json` ↔ `pnpm-lock.yaml` ↔ `node_modules/.modules.yaml` 的
   `hoistedLocations` 解析身份），git 比 commit；生效方式四态（`profile 层`/`预设挂载`/`普通依赖`/`未激活`）；
   硬失败（依赖缺失、声明 bundle 却未激活、lockfile 与磁盘不一致、未记入 lockfile）与告警分级；缺 PyYAML/无 `hoistedLocations` 时降级为告警而非假失败。
2. **`features/update.py`**：`_update_plugin` 改为前后快照 + 差异（`diff_plugins`），结束打**两行**——结论
   （`vdsh ✓ 插件已是最新（5 个依赖校验通过）`；有更新则列 `名字 旧→新`）与耗时（`vdsh · 用时 1m46s`；用户要求耗时单独成行）；
   校验失败逐条 `vdsh ✗` + 退出码 1；
   `pnpm install`（update dsh）同样接入折叠与网络失败提示；旧 `_installed_versions()` 删除（避免两套语义）。
   另加「pnpm 确实跑了」的旁证：全程没有 pnpm 运行标记时补一句 `vdsh ⚠ 未见 pnpm 运行标记…`，避免把「状态没变」说成「已更新」。
3. **`spinner.py`**：`Spinner.set_message()`（只改转轮文案）；`run_child_progress(..., collect=, quiet=, replay_on_failure=)`——
   quiet 回调把子进程输出分成三个去向：**原样打印 / 静默折叠 / 归一化进度行 `→ …`**（打印进度行后转轮文案恢复为任务名，
   不跟随 pnpm 内部计数漂移）；非 0 退出时把折叠行**原样补打**；非 TTY 恒为全量透传。
4. **`console.py`**：新增 `fail()`（非致命 `vdsh ✗`）与 `ok()`（结论 `vdsh ✓`）。
5. **`features/doctor.py`**：profile 段改用 `profile_state.verify()`，逐插件打 `版本/commit · 生效方式`，失败/告警分级，peer 一行指引
   （结论行不再展开生效方式，明细都收在这里）。
6. **配置**：新增 `animation.quiet`（默认 true；TTY 折叠 pnpm 低价值行并改打 `→` 进度行，false = 全量排障用），同步 `DEFAULTS`/`VALIDATORS`/`TEMPLATE`/`config_report`/`app.py` 与 `vdsh.yaml`。
7. **文档**：`usage.md`（输出前缀对照表 + 更新校验 + `[WARN]` 怎么看 + `github.com:443` 处置 + 配置键）、`experience.md` §7.4/§7.5、本条目、`dev.md` 目录职责表。

### 4. 关键决策与取舍

- **不折叠失败证据**：瘦身只作用于成功路径；任何非 0 退出都把折叠行补打（`replay_on_failure`）。非 TTY 一律全量，保证日志/CI 可回溯。
- **语义留在功能层**：spinner 只提供机制（`quiet` 回调 / `collect` / 环形缓冲），「什么算噪声、翻译成什么进度」由 `update.py` 的 `_PnpmNoise` 决定（与 dev.md §3 分层一致）。
- **输出只有四种东西**（用户给的约定）：`⠋ 任务名 47s` 转轮、`→ …` 进度、`vdsh · …` 节点、`vdsh ✓/✗` 结论。
  解释性文字一律不进输出——第一版把「网络重试意味着什么」「缺 peer 为什么正常」塞进结论行尾注，被判定为「不明所以的说明」，已全部移到文档 +
  `vdsh doctor`，结论行只回答「更新了什么 / 是否已是最新 / 用时」。
- **转轮文案是任务名，不是 pnpm 计数**：进度单独用 `→` 行承载，否则转轮会显示 `已解析 174 · 复用 7` 这类只有作者看得懂的内部计数。
  为此外层在打印进度行后显式把转轮文案复位（`set_message(message)`）。
- **进度节流按种类各自计时**（`THROTTLE_SECONDS = 3.0`）：解析进度与网络重试是两种信号，共用一个窗口会让「停滞期的唯一反馈」被挤掉。
- **不制造新假告警**：`未声明 dsh.bundle` 不判失败也不告警（`dsh-study-buddy` 由预设挂载是合法形态）；「未见引用」仅对插件形状的包做 ⚠；
  降级路径（无 PyYAML / 无 `hoistedLocations`）只 ⚠；doctor 的 peer 指引为信息行（`—`）。
- **不给 pnpm 加 `--fetch-retries`**：探测失败已被吞掉，多 retry 只增加耗时；也不替用户改网络/代理，只给判别命令与建议。
- **不做额外 pnpm 调用**（不跑 `pnpm list`）：校验全部读文件，离线、快、可复现。

### 5. 验证情况

| 项 | 命令/方法 | 结果 |
|---|---|---|
| 编译 | `python -m py_compile`（全量） | 通过 |
| 入口冒烟 | `python vdsh_launcher.py --help` | 通过 |
| profile 校验 11 组用例 | 临时脚本（临时目录造最小 profile） | 全部通过：registry 版本变化、**git commit 变化但版本号相同**、未激活→✗、磁盘与 lockfile 不一致→✗、预设挂载不误报、缺失→✗、无引用→仅 ⚠、降级只告警、added/removed、scoped/含 `@` 的键解析 |
| 动画/分流 5 组用例 | 假 TTY 流 + `python -u` 子进程 | 全部通过：原始噪声零输出、生成 `→ 解析 …`/`→ 网络重试 N` 进度行、**转轮文案恒为任务名**、重试进度行节流为 1 条、真行保留、失败补打折叠行、非 TTY/`quiet=false` 全量、无 `quiet` 回调时行为不变、无 pnpm 运行标记时 `observed=False` |
| 端到端输出预览 | `_update_plugin` + 假 TTY + 用用户原始 pnpm 日志当子进程输出 | 终端最终内容恰为 3 行节点/进度/结论（见 §2），转轮期间恒定显示 `⠋ 更新插件 Ns` |
| doctor | `python vdsh_launcher.py doctor`（不接管道） | 退出码 0；输出 5 个插件的 版本/commit + 生效方式（4 个 profile 层 · 1 个预设挂载）+ peer 指引 |
| 真机 E2E | `vdsh update plugin`（需先停 dsh web） | **未执行**（本会话的 GUI 就是该实例，见 §6） |

真实 profile 只读校验输出：`@liustack/modlens 3.26.1`、`dsh-at-file 0.7.0@da602d1`、`dsh-better-sidebar 0.18.1`、
`dsh-study-buddy 0.9.1@3836f27（预设挂载）`、`dsh-task-notify 1.6.1@e4b3994`，无硬失败。

### 6. 遗留与后续迭代提示

- **真机 `vdsh update plugin` 复跑未做**：需停 dsh web。建议在真终端跑一次并复跑第二次（幂等），确认「`→` 进度行 + 结论/耗时两行」的实际观感与转轮秒数。
- 断网/代理缺失时 `update plugin` 的耗时仍来自 pnpm 探测重试（约 10–20s）；若长期如此，考虑在 vdsh 侧给 `dsh plugin` 传 pnpm 网络参数（本轮故意未做）。
- `build.py` 的 `pnpm run build` 未接 `quiet`（可能同样打 peer 提示）；如需一致体验，可后续把 `_PnpmNoise` 复用过去。
- 副机是否同样阻断 `github.com:443` 未验证（本机实测为主）；若副机正常，lockfile 可能被改写为 codeload 解析，注意 `vdsh sync push` 的先后。

### 7. 复测清单（回归用）

```powershell
cd T:\Open-Source\dsh-launcher
python -m py_compile (Get-ChildItem -Recurse -File -Include *.py -Path .\vdsh).FullName
python vdsh_launcher.py --help
python vdsh_launcher.py doctor          # 不接管道；echo $LASTEXITCODE 应为 0
python "$env:TEMP\vdsh_profile_state_check.py"   # 11 组校验用例（临时脚本，见 §5）
python "$env:TEMP\vdsh_spinner_check.py"         # 5 组折叠/进度行/补打/透传用例
python "$env:TEMP\vdsh_output_preview.py"        # 端到端输出预览（假 TTY + 样本 pnpm 日志）
# 真机（先停 dsh web）：
vdsh update plugin
```

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
