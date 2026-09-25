# 开发日志（迭代记录）

按**一次迭代一条记录**追加，供后续迭代开发回溯「当时为什么这么做、测过什么、还剩什么」。
每条记录含：背景 → 改动清单（按需求条目）→ 关键决策与取舍 → 验证情况 → 遗留与后续提示。
前置阅读：`design.md`（架构）→ `dev.md`（开发规程）；踩坑细节见 `experience.md`。

---

## 2026-09-25：DSH 0.1.7-rc.2 更新后构建失败——真机跑的是 `pnpm run run build`（复测全绿、真机全红）

### 1. 背景

用户把 DSH 从 `0.1.7-rc.1` 更新到 `0.1.7-rc.2`（仓库 HEAD `477b4f42`）后，`vdsh update dsh` 的代码合并
与 `pnpm install` 都成功，**构建段全灭**（日志节选）：

```text
→ 版本 0.1.7-rc.1 → 0.1.7-rc.2：清缓存全量重建（避免残留旧产物）
vdsh · 清理构建产物（各包 lib/ 与 tsbuildinfo）
[ERR_PNPM_NO_SCRIPT] Missing script: run

Command "run" not found.
vdsh ⚠ 清缓存未完成（exit code 1）：继续按普通方式重建，失败诊断里会说明这一点
[ERR_PNPM_NO_SCRIPT] Missing script: run

Command "run" not found.
…
vdsh ⚠ 可先清缓存再重建（陈旧产物导致的缺导出/找不到模块，重跑普通构建无效）：
        在 T:\deepseek-harness 执行 `pnpm run clean && pnpm run build`
vdsh ✗ 构建失败（exit code 1）
```

两处都不对：① 构建**根本没跑起来**——报的是「没有 `run` 脚本」，而 vdsh 想跑的是 `clean`/`build`；
② 诊断把用户指回**刚刚失败的那个动作**。用户按诊断手动执行后构建成功
（`lib/.vdsh-build.json` 于 14:43:50 以 `origin=inferred` 记下 HEAD `477b4f42`），
launcher 侧的缺陷仍在——不修则每次跨版本更新都会再来一遍。

### 2. 根因（两处，都在 `features/build.py`）

**(1) 命令拼装：字符串形式多补了一个 `run`。** `_pnpm_command`（2026-09-23 引入）按「路径形式要补
`run`」实现，而调用方按「原样 argv」传 `("run", "build")`：

| 形式 | 谁在用 | 调用方传 | 旧实现拼出 | 真机结果 |
|---|---|---|---|---|
| 路径字符串 | **真机**（`shutil.which("pnpm")` → `pnpm.CMD`） | `"run", "build"` | `pnpm run run build` | ✗ `ERR_PNPM_NO_SCRIPT Missing script: run` |
| argv 列表 | **离线复测**（`[python, _fake_pnpm.py]`） | `"run", "build"` | `python _fake_pnpm.py run build` | ✓ 假 pnpm 正常应答 |

两种形式行为不同，而只有列表形式被复测覆盖 → `_check_build_retry.py` 的 30+ 断言**全绿**、真机**全红**。
真机 `pnpm run run build` 的双行报错（`Missing script: run` + `Command "run" not found.`）已用真 pnpm
复现，与用户日志逐字一致。**与 DSH 0.1.7-rc.2 本身无关**（脚本 `build`/`clean` 都在，
根 `package.json` 已核对），纯粹是 launcher 侧拼错了命令行。

**(2) 诊断：`cleaned` 把「已尝试」记成了「清成功」。** `run_build` 里
`cleaned = clean and clean_build(...)`：`--clean` 路径（**跨版本更新正是这条**）清缓存失败时
`cleaned=False`，诊断于是落到「没清过」分支——把用户指回刚刚失败的动作。该变量在本模块的注释与
重试分支里本来就是「**已尝试**」语义（重试分支显式 `cleaned = True`，不看 `clean_build` 成败），
两处语义不一致。

### 3. 改动清单

1. `_pnpm_command`：两种形式**同构**，`args` 原样拼接，删除隐式补 `run`；docstring 写明事故与
   「不要复活自动补 `run`」。
2. `run_build`：`cleaned` 改为「已尝试」——`if clean: cleaned = True; clean_ok = clean_build(...)`。
3. `missing_script()` + `PNPM_RUN_VERB` + 诊断三分支：从输出抓 pnpm 报缺的脚本名
   （`Missing script: X` / `Command "X" not found.`，pnpm 11 双文案），与「本次**请求**的脚本」
   及「vdsh 拼进去的子命令」对照：
   - 报缺的**正是 vdsh 拼的子命令**（`run`）→ 判**命令拼装错误**（启动器缺陷、与仓库无关），
     并**撤掉**清缓存建议与「dsh web 文件锁」提醒（与本次无关的噪声）；
   - 报缺的**正是请求的脚本** → 提示核对仓库 `package.json` 的 scripts（下次 DSH 改脚本名时这条自己说话）；
   - 其余（例如构建脚本内部对子包的调用报 `Missing script: bundle`）→ 同样指向 `package.json`，
     但明确**不**归咎启动器——误判会把用户的排查方向带偏。
4. `_check_build_cmd.py`（新增复测，gitignore 不入库）：见 §5。

### 4. 关键决策与取舍

- **修成「argv 原样」，而不是让 helper 拥有 `run`**（即调用方只传脚本名、由 helper 补 `run`）：
  事故的成因是「隐式变换 + 两种形式不一致」。原样拼接可被肉眼核对（写下的就是执行的），
  且只剩一条拼接路径，「两种形式行为不同」这一类不可能再出现。
- **诊断加 missing-script 指纹，而不是加一句「也可能是拼装问题」**：两条互斥建议并列等于没有建议。
  指纹的判据要**窄**：只有「报缺的名字正是 vdsh 自己拼进去的子命令」（`run`）才判启动器缺陷——
  否则构建脚本内部对子包的调用（`Missing script: bundle`）会被诬告成启动器的问题，把排查方向带偏。
- **不做「脚本名硬编码校验」**（例如启动时断言仓库 `package.json` 必须有 `build`）：那会把 DSH 的
  正常改名变成启动器硬失败；用输出指纹区分，同时覆盖「仓库改名」这一真实可能。
- **反向验证作为验收的一部分**：把 `_pnpm_command` 还原成旧实现，新用例 D 必红（实测 exit 3），
  且诊断当场打出「命令拼装错误」——证明守卫拦得住**原缺陷**，而不是只拦得住我改过的那一行。

### 5. 验证情况

| 用例 | 内容 | 结果 |
|---|---|---|
| `python _check_build_cmd.py`（新增，43 断言） | A 两种形式同构 + `run` 恰好一个；B **AST 扫 `build.py` 每个调用点**逐个核对拼装结果、并核对子命令与 `PNPM_RUN_VERB` 一致；C **真机 pnpm** 复现 `run run build` → `Missing script: run`、`run build` → 正常执行；D **字符串 pnpm 端到端**（真 `pnpm.CMD` + 真脚本，走 `run_build` 的增量与 `--clean` 两条路）；E `--clean` 清缓存失败时的诊断；F missing-script 诊断三分支（含「子包脚本缺失不诬告启动器」）；末两条为工作区卫生（无 `.pnpm-store`、无本次 scratch 残留） | `✓ 全部通过（含真机 pnpm 端到端）` |
| 反向验证（旧实现 + 真 pnpm） | 字符串形式拼出 `run run build` → exit 3、`Missing script: run`、诊断点出「命令拼装错误」 | 守卫有效 |
| `python _check_build_retry.py`（30+ 断言） | 陈旧产物自愈、构建时效判定、启动侧、文案 | `✓ 全部通过` |
| `python _check_ready_parse.py`（26 断言） | 就绪信号解析（上一轮修复） | `ALL PASS` |

**未做**：在 `T:\deepseek-harness` 上真跑一次全量重建——仓库目录在沙箱工作区之外（写入 EPERM），
且用户的 dsh web 正占用 3080（构建需先停服务）。终验放到**下一次 `vdsh update dsh`**，
或用户空闲时 `vdsh build --clean`。

### 6. 遗留与后续提示

1. 复测入口现在是**两条**，都要跑：`python _check_build_cmd.py`（含真机 pnpm，需 PATH 里有 pnpm）
   与 `python _check_build_retry.py`（纯离线）——前者管「真机形态」，后者管「自愈/时效判定」。
2. `_fake_pnpm.py` 的注释写着「不入库」，但它已随 `98ada4f` 入库——保留：它是离线驱动，
   且现在与真 pnpm 收同样的 argv。
3. 通用教训写进 `experience.md` §7.7：**注入替身必须与真件同构**；只覆盖一种调用形式的复测等于
   没覆盖，而「真机端到端跑一个 3 行真脚本」比十条替身断言更能拦住这类缺陷。
4. 顺手修掉复测脚手架的两处卫生问题（本次跑真 pnpm 才暴露）：① 真 pnpm 会在**工作区根**建
   `.pnpm-store/`（200+ 文件）→ 用例内用 `pnpm_config_store_dir` 指进 scratch；② `rmtree(..., ignore_errors=True)`
   在只读文件（git 的 `.git/objects/*`）上**静默半途而废** → 换成清只读位再重试的 `rmtree()` 并把
   「残留清单」纳入用例断言（实测工作区里积了 10 个 09-23 起就删不掉的旧目录）。
   仍有 2 个**早期沙箱 ACL 实验**留下的目录（`probe-l9mdzx06`、`vdsh-build-check-24t4kg3g`）连枚举都
   `WinError 5`，本机删不掉——已 gitignore，不影响功能；用例只核对「无本次残留」。

---

## 2026-09-24：DSH 0.1.7-rc.1 后 `dsh web:` 就绪信号不再独占一行 → vdsh 空等到超时

### 1. 背景

用户升级 DSH 到 `0.1.7-rc.1`（仓库 HEAD `46a7f68b09`）后，`vdsh` 启动表现为「dsh 实际已经起来，
但 launcher 一直在等」，Ctrl+C 才退出：

```text
PS T:\deepseek-harness> vdsh
vdsh · 启动 dsh web（工作区 T:\deepseek-harness）
  File "…\vdsh\features\launch.py", line 235, in wait_until_ready
    time.sleep(gap)
KeyboardInterrupt
```

用户按 `dsh-plugin-migration-guide.md` 提示排查（该指南记录的 0.1.7-rc.1 破坏性变更是
① 删 `ctx.settings.register` ② 删「目录式预设」）。**两条都不是本次根因**，但指南的价值在于
解释了「为什么这一版才开始黏连」——见 §2。

### 2. 根因（字节级取证）

launcher 的就绪判定唯一依赖日志里的「认证 URL 行」，用的是**行首锚定**正则：

```python
re.search(r"(?m)^\s*dsh web:\s*(\S+)", text)   # 旧：launch.py:120
```

而那次失败的日志（`%TEMP%\vdsh-web.log`，910 字节，15:39:49）里，就绪信号**不在行首**：

```text
… 服务\xe9\x94\x9b?dsh web: http://127.0.0.1:3080/?token=PhI5iHh9uja1QRnQKBJn-lE03Ag1e3FqOZl0SQcLxYk\r\n
                  ^ 紧邻 URL 的字节是 `?`(0x3f)，前面没有 CR/LF
```

| 判据 | 结果 |
|---|---|
| 旧锚定正则打该日志 | **None**（抓不到 → 空等 180s） |
| 非锚定 `dsh web:\s*(\S+)` | 完整 URL（token 43 字符，一字不差） |
| 该 token 换 cookie | 当时 200 + `__DSH_BOOT__`（服务其实早已就绪）；事后 401（token 是 per-process，旧日志的 token 随进程重启作废——这本身也说明「日志旧于进程」是个需要区分的状态） |
| `probe_harness()` | `auth`（401 正文含 `dsh web authentication required`，认证闸未变） |

黏连来源：0.1.7-rc.1 的启动审计在**有插件激活失败时**多打一段诊断
（`packages/boot/app-boot/src/index.ts:873` → `auditStartupEntries`），日志前 8 行正是
`dsh: warning: 1 entry did not activate` + `dsh-at-file … ctx.settings.register is not a function`
（profile 里第三方 `dsh-at-file` 仍按 ≤0.1.6 的形状调用已删 API → 整行 failed → 触发审计）。
该段输出与相邻输出在 pwsh `*>` 全流重定向下并进同一行（复现：`process.stdout.write('B: 无换行')`
+ `console.log('C: …')` → 落盘 `B: 无换行C: …\r\n`）；同日志还可见 PowerShell 文本解码造成的
私用区乱码（`U+E187` 等）。

**结论**：launcher 的就绪解析假设「信号独占一行」，这个假设在 0.1.3-alpha.1 引入该通道时就存在，
只是 0.1.7 起才第一次出现黏连源。修复**不依赖** profile 侧插件是否被修好——任何插件失败/任何
审计输出都可能再次黏连。

### 3. 改动（`vdsh/features/launch.py`）

1. **`_read_log(log_path)`**（新）：读日志的唯一入口，`errors="replace"` 解码（半个多字节字符不再让读取抛错），
   `_url_line_from_log`/`_lan_url_from_log` 共用（改前两份重复的 try/read）。
2. **`_url_line_from_log`**：正则去掉 `(?m)^\s*` → `r"dsh web:\s*(\S+)"`。每个进程只打印一次该信号，
   全文匹配无歧义；URL 内无空白，黏连前缀不入捕获（实测取到完整 43 字符 token）。
3. **`_lan_url_from_log`**：`r"\(LAN:\s*(\S+?)\)"` —— 空格可选（黏连时写成 `…?token=…(LAN: …)`），
   非贪婪以在 `)` 处截断。三种形态（紧贴/带空格/无 LAN）实跑一致。
4. **`_ready_from_log(log_path)`**（新）：返回 `(authed_url, session, lan_url, signal_seen)`，
   把「信号一直没出现（启动还早）」与「信号出现过但取不出可用 URL（输出被污染）」分开。
5. **`wait_until_ready(..., timeout_message=)`**（改）：内部改用 `_ready_from_log`；超时时回调
   `timeout_message(signal_seen)` 给出**不同**指引（信号在 → 直接给日志里的 URL 让用户开；不在 →
   指向日志末尾排障）。改前超时只有一句泛泛的「服务可能启动失败」，与真正的证据脱节。
6. **`run()`**：`auth` 已在运行分支同样改用 `_ready_from_log`（拿不到时保持原有告警）；
   全新启动分支删掉本地超时 `warn`（交给回调），`starting` 分支传同一回调，且超时文案补上日志路径。

### 4. 关键决策与取舍

- **修解析而不是修启动方式**：`spawn_server` 的 pwsh `*>` 重定向**不会**吞掉子进程写出的换行
  （已实测复现），黏连由上游输出/重定向文本解码造成；改成 Python 直接 spawn node（字节精确但丢独立窗口）
  属行为变更，收益与风险都不划算，不做。
- **放宽正则会不会误命中**：理论上日志别处出现 `dsh web:` 字样会被匹配，但①每进程只打印一次；
  ②捕获值还要过 `_auth_session_bootstrap` 的 200 + `__DSH_BOOT__` 校验，误命中不会造成假就绪。
- **不做「黏连恢复」的 sticky session**：原始方案里想缓存上一轮成功换到的 cookie 作为「之后解析失败」的
  兜底，但该分支实际不可达（只要 URL 能解析出来，下一轮必然也能解析出来；URL 解析成功即 break），
  按「无证据不加机制」删掉，代码保持单路径。
- **不锚定 ≠ 放弃行概念**：`(LAN: …)` 仍按括号边界解析；token 仍按 `\S+` 取到空白为止。
- **profile 侧不动**：`dsh-at-file` 报上游（迁移指南 §3 已列为第三方待修）；`dsh-study-buddy` 按 §3B 已修。
  本次只保证「上游有杂音时 launcher 仍能判就绪」。

### 5. 验证情况

| 项 | 方法 | 结果 |
|---|---|---|
| 解析回归 | `python _check_ready_parse.py`（A–K 共 26 断言） | ALL PASS |
| 夹具真实性 | `FIXTURE_COLLIDED` 与实机失败日志逐字节比对 | 910 == 910 ✓（K 断言） |
| 旧写法必坏 | 旧正则 × 黏连夹具 | `None`（A 断言固化证据） |
| 新写法必好 | 新解析 × 黏连夹具 | 完整 URL，token 43 字符 ✓ |
| 真实日志 | `VDSH_CHECK_LOG=<实机日志>` 跑 H 节 | 取到 URL ✓；token 401 = 日志旧于进程（符合预期） |
| 端到端联通 | 同一 URL 用 requests 走 `?token=` | 当时 200 + `__DSH_BOOT__`（服务确实就绪） |
| 语法 | `ast.parse` / py_compile | OK |

### 6. 遗留与后续迭代提示

- **就绪信号不得假设独占一行**（写进 `experience.md`）：这是本次的硬约束，下次再遇到新 DSH 版
  新增启动输出时直接命中。
- 「日志旧于进程」无法从日志本身判断（token 是 per-process）：`probe_harness() == "auth"` +
  token 401 只能判定「不能自动开浏览器」，不能判定「服务没起来」。当前处理是超时文案指向日志
  让用户自取，未做进一步自动化（要自动化得让 DSH 把 URL 写进固定文件，属平台侧需求）。
- 若将来 `dsh-at-file` 修好（不再有审计告警），黏连源会消失，但**不要**把锚定加回来——
  回归证据在 `_check_ready_parse.py` 的 A 节。
- 本机 `%TEMP%\vdsh-web.log` 每次启动被覆盖：复现/复测要留档就先复制一份（本次夹具即这样取得）。

---



### 1. 背景

上午的「陈旧产物自愈 + 构建基线」刚落地，用户第一次真机启动就撞上两个问题（原话：「有些问题，我没有动DSH，
应该是能启动的，而非出错」）：

```text
PS C:\Users\V.Reason> vdsh
检测构建产物缺失/源码更新，执行build? [Y/n] n
vdsh · 已取消
PS C:\Users\V.Reason> pnpm dsh web          # 手动启动，一切正常
dsh web: http://127.0.0.1:3080/?token=…
```

用户**当天刚删库重下并自己构建过**（产物 mtime 20:14–20:15，HEAD 提交时间 09-22 23:25），
没改过任何源码，却被问「构建产物缺失/源码更新」；答 `n` 之后连启动一起被取消，只能手动 `pnpm dsh web`。

### 2. 根因（两条，都在我上午的改动里）

| 现象 | 根因 | 证据 |
|---|---|---|
| 没动过 DSH 却被问「源码更新」 | `build_needed()` 把**基线缺失**直接判为需构建；而用户手动 `pnpm run build` 的检出本来就没有基线（基线只有 vdsh 构建成功才写） | 该检出 `lib/.vdsh-build.json` 不存在；`apps/cli/lib/bin.js` 20:15:38、`apps/web/dist/index.html` 20:15:47 都新于源码（20:12）与 HEAD 提交（09-22 23:25） |
| 答 `n` 连启动一起取消 | launch 侧写死「拒绝 = `已取消` + `return 0`」，与「产物是否真的不可用」无关 | `vdsh · 已取消`，而同一时刻手动 `pnpm dsh web` 启动完全正常 |

顺带修掉同一函数里的老漏检：源码 mtime 只比对 `apps/cli/src`、`apps/web/src` 两个目录，
`packages/*/src` 的改动（正是 devlog 上一条里 `SettingsProvider` 那类跨包重命名）判不出来。

### 3. 改动

1. **`vdsh/config.py`**：`SRC_DIRS`（两个 src 目录）→ `SRC_ROOTS`（`apps/`、`packages/`、`native/`、`vendor/`、`scripts/`）
   + `SRC_SKIP_DIRS`（`node_modules`/`lib`/`dist`/… 必须排除：产物若算进源码，「产物比源码新」永远不成立）
   + `SRC_SKIP_SUFFIXES`（`*.tsbuildinfo`：`tsc -b` 的增量状态，写在仓库根与各包 `lib/` 下，同样是输出）
   + `SRC_ROOT_FILES`/`SRC_ROOT_PREFIXES`（根级构建输入：`package.json`/`pnpm-lock.yaml`/`pnpm-workspace.yaml`/`tsconfig*`/`tsdown.config.*`）；
   `BUILD_PROMPT` 拆成按原因取文案的 `BUILD_PROMPTS`（`missing`/`stale`），保留通用文案作回退。
2. **`vdsh/features/build.py`**：
   - 判定改 `build_reason(repo)`（返回 `REASON_MISSING`/`REASON_STALE`/None），按**证据递进**：
     产物缺失 → 基线 HEAD ≠ 当前 HEAD → 源码 mtime 新于产物 → **都没有就不提示**；
   - **「有产物、无基线」不再判为需构建**：证据显示产物不旧时顺手写一份 `origin="inferred"` 的基线
     （与真实构建写的 `origin="build"` 区分），下次启动只比 HEAD；
   - `build_needed()` 保留为 `build_reason() is not None` 的布尔形式（语义与副作用都在 docstring 里写明）；
   - `confirm_build(reason)` 按原因选文案；`newest_mtime(root, skip=, skip_suffixes=)` 增加跳过规则，
     新增 `source_newest_mtime(repo)`（`SRC_ROOTS` + 根级构建输入）。
3. **`vdsh/features/launch.py`**：拒绝构建**只告警、不取消启动**——产物缺失 → 「已跳过构建（缺少构建产物）：
   启动很可能失败，需要时运行 vdsh build」，产物陈旧 → 「已跳过构建（产物可能陈旧）：若启动报缺导出/找不到模块，
   运行 vdsh build --clean」；随后照常走预检/启动（真缺 CLI 产物时预检给「请先执行 vdsh build」）。
   `confirm_build()` 同时把三种「拒绝」统一成 False：答 n、非交互 EOF、**提示符处 Ctrl+C**（改前 Ctrl+C 会
   抛 KeyboardInterrupt，`app.main` 无兜底 → traceback）。
4. **文档**：usage.md（启动一节改「证据递进」，构建基线一条同步）、design.md（状态机注 + 2 条演进决策 + 1 条已知边界）、
   dev.md（config.py / build.py 行、§5.8 复测清单扩到 H/I/J）。
5. **`_check_build_retry.py`**（不入库）：H 重写为新语义 + 新增 I（启动侧，替身 `spawn_server`/`wait_until_ready`/`probe_harness`）
   与 J（`confirm_build` 文案与解析）。

### 4. 关键决策与取舍

- **「未知」不等于「需构建」**：基线缺失只是「vdsh 没构建过」，不是「产物旧了」。改前用「保守起见问一次」换安全，
  代价是每个手动构建过的检出（含刚重下重建）每次启动都被问——用户视角这就是误报。现在把「保守」换成三条可验证证据。
- **认账写基线（`origin=inferred`）是有意副作用**：不写的话「无基线」状态永远存在，判定永远是 mtime 级；
  写了之后判定退化为一次 `git rev-parse` 比较。字段留痕，人工排查时能分辨这行基线不是构建写下的。
- **源码范围扩到构建读到的所有根**：`apps/`+`packages/`+`native/`+`vendor/`+`scripts/` 全量遍历 + 根级构建输入，
  实测 7720 个文件约 0.5s（NTFS 热缓存），相对启动的秒级开销可忽略；只收「会被构建读取的目录」需要维护一张
  与 `pnpm-workspace.yaml`/tsconfig 项目引用同步的表，不如整根遍历 + 排除产物（`lib`/`dist`/`*.tsbuildinfo`）稳。
  `website/`（文档站）有意排除：不参与 `pnpm run build`。
- **拒绝构建仍启动**：启动是这条通路的目的，构建只是前置优化；用「能不能跑」（CLI 产物预检、就绪轮询、
  子进程退出即时反馈）把关，而不是用「有没有构建过」。
- **不改「空输入 = 同意构建」**：默认值保持 `[Y/n]`（回车即构建），只有明确答 `n` 才跳过。

### 5. 验证情况

| 验证 | 手段 | 结果 |
|---|---|---|
| 离线用例（不入库） | `python _check_build_retry.py` + `_fake_pnpm.py`：A–G 原有 30 项 + H 重写 + I 启动侧 + J 文案 | `✓ 全部通过`（61 项） |
| 关键回归（本次报告的场景） | I1：产物齐全、无基线、源码不旧 → **不提问**且照常 `spawn` | 通过（`answers == []`） |
| 拒绝不再取消 | I2：产物比源码旧 → 提问；答 `n` → 告警「产物可能陈旧」+ 仍 `spawn` | 通过 |
| 认账基线 | H：`build_reason()` 返回 None 且写出 `origin=inferred` + 当前 HEAD；判为陈旧时**不写** | 通过 |
| 扩范围生效 | H：仅 `packages/demo/src` 比产物新 → `REASON_STALE`（老实现漏检）；根级 `tsdown.config.ts` 更新 → `REASON_STALE` | 通过 |
| 输出不算源码 | H：根级 `README.md`、根/包内 `*.tsbuildinfo`、`apps/cli/lib/**` → 仍为「不提示」（否则刚构建完就说陈旧） | 通过 |
| 真机只读核对 | `T:\deepseek-harness`：产物 20:15 > 全部构建输入（0 个比产物新）、无基线 → `build_reason` 为 None（**下次 `vdsh` 不再误报**，并就地认账写基线） | 通过 |
| 编译/冒烟 | `python -m py_compile`（22 文件）、`vdsh --help` | 退出码 0 |

### 6. 遗留与后续

- **认账基线的正确性依赖「产物不比源码旧」**：若用户手动构建后又改了源码却没重建，mtime 判定会提示（符合预期）；
  但若某工具把源码 mtime 改**旧**（少见），误判方向是「少提示一次」而不是「反复误报」——取舍偏静默。
- **启动判定多了一次遍历**：`SRC_ROOTS` + 根级输入约 0.5s；若日后仓库膨胀到秒级，可先比 HEAD（命中即跳过遍历）
  或改用 `git status --porcelain` 的脏文件列表。
- **`pnpm run clean` 清掉基线后仍会按证据重判**：清过缓存但没重建时产物仍在（clean 删的就是产物，故此时通常判 `missing`），
  两者一致，无需额外状态。
- 上一轮遗留（未复现「增量构建为何漏刷 `lib/`」、指纹表随打包器措辞维护、`--clean` 无耗时预估）仍然有效。

### 7. 需要一并复测的既有行为（改的是启动主通路的判定分支）

`vdsh`（端口空闲，产物齐全、无基线）→ 不再提问、直接启动；`vdsh`（真的改过 `apps/` 或 `packages/` 源码）→ 提问，
回车构建；`vdsh build` / `--clean` / `--no-retry` 行为不变；`vdsh update dsh`（版本变化清缓存、失败诊断）不变；
`vdsh doctor`/`config`/`sync` 不经 build 判定，不受影响。

---

## 2026-09-23：构建失败自动清缓存重建 + 构建基线（第三波的「不可复原」根因找到了）

### 1. 背景

用户跑 `vdsh update dsh` 失败，随后**手动 `pnpm install && pnpm run build` 同样失败**，
最终只能**删库重下载**才恢复；`vdsh build` 重跑也是同样的报错。

报错两次都指向 `lib/` 产物与源码不同步（均为 tsdown 的 `MISSING_EXPORT`）：

```
[MISSING_EXPORT] "removeLinkProjections" is not exported by "../../packages/boot/app-boot/lib/index.js".
[MISSING_EXPORT] "sanitizeProfile" is not exported by "../../packages/boot/app-boot/lib/index.js".
[MISSING_EXPORT] "SettingsProvider" is not exported by "../settings/src/index.ts".
```

### 2. 根因（这次有证据，补上第三波的遗留）

| 取证 | 方法 | 结果 |
|---|---|---|
| 缺导出的符号在源码里是否存在 | 读 `packages/boot/app-boot/src/index.ts` | **都在**（`sanitizeProfile` 21 行、`removeLinkProjections` 57 行，分别自 2026-09-16 / 09-19 提交）→ 报错的不是源码，而是 `lib/index.js` 是旧产物 |
| `SettingsProvider` 为什么没了 | 全仓 grep + `git log -S` | 2026-09-21 提交 `601d6761e4` 把 `SettingsProvider` 改成 `SettingsForms`（现在只有 `packages/settings/settings/src/index.ts:223`）；旧 `apps/desktop/lib/types/*` 仍导入前身 |
| 失败入口属于谁 | 报错里的 `lib/types/project-manager.js` | `apps/desktop`（其 `tsdown.config.ts` 以 `lib/types/main.js` 为入口、`clean: false` **从不删产物**），即增量构建没重刷 |
| 为什么手动 `pnpm run build` 也救不回 | 读根 `package.json` + `scripts/build.ts` | `build` = `build:native-system → build:lib → build:web`，**不含清缓存**；`pnpm run clean`（`tsx scripts/clean.ts`）才是删 `lib/`+`*.tsbuildinfo` 的那条路 |
| 为什么删库重下就好了 | 现有检出复测（用户重下后） | `apps/desktop/lib/types/project-manager.js` 已是新内容、不再引用 settings；全量重建即恢复到一致状态 |
| 清缓存会删掉什么 | 静态核对 `scripts/clean.ts` 的删除集合（跟着根 `tsconfig.json` → `tsconfig.host/client.json` → `apps/desktop/tsconfig.host.json` 的项目引用图走） | 只删各包 `lib/`（`outDir` 以 `/types` 结尾时取其父目录）与 `*.tsbuildinfo`，外加 `apps/desktop/lib`；**不碰** `apps/web/dist`、`node_modules`、工作区数据 |

**结论**：`git pull` 跨版本更新后，仓库的**增量**构建可能不重刷 `lib/`，于是打包器读到与 `src/`
不一致的旧产物；`pnpm run build` 又不清缓存，所以重跑、手跑都没用——**「删库重下」是当时唯一的出路**。
这正好解释了第三波「真实失败原因不可复原」：当时的现场日志里只有结尾 15 行，看不到「产物旧于源码」这一层。

附带查出启动侧的同类隐患：`build_needed()` 只比对 `apps/cli/src`、`apps/web/src` 的 mtime，
**任何其他包**（如 `packages/boot/app-boot`）的源码更新都不会触发构建提示，会带着陈旧产物启动。

### 3. 改动清单

1. **`vdsh/features/build.py`（核心）**
   - `looks_like_stale_output(lines, start=0)` + `STALE_OUTPUT_RE`：陈旧产物指纹
     （`MISSING_EXPORT` / `is not exported by` / `MISSING_IMPORT` / `Cannot find module '<…>/lib|types/…>` /
     `ERR_MODULE_NOT_FOUND` / `Could not resolve "<…>/lib|types/…>`）。只针对**第一次尝试**的输出判定
     （`start`），避免把重建阶段的真实报错误判成陈旧产物；普通 TS 类型错误不匹配。
   - `clean_build()`：跑 `pnpm run clean`；清不掉只 `vdsh ⚠` 告警并继续重建（不 `die`）。
   - `run_build(..., clean=False, retry_on_stale=True)`：失败且命中指纹 → 告警 + **清缓存重建一次**；
     `clean=True`（`--clean`、`update dsh` 的版本变更路径）直接全量且不再二次重试；
     两次尝试的输出累积进同一份 `%TEMP%\vdsh-build.log`。
   - `_report_build_failure(..., cleaned, clean_ok)`：清过缓存就不再让用户清一次；清失败时如实说
     「清缓存未能执行」，两条路径都不再给改前那条**已证明无效**的 `pnpm install && pnpm run build`。
   - **构建基线**：成功后写 `lib/.vdsh-build.json`（HEAD sha + 时间 + 版本）。`_read_stamp` 区分
     **缺失**（从未成功构建 → 需构建）与**损坏**（构建过但基线坏了 → 回退 mtime 判定）；`build_needed()`
     增加「基线 HEAD ≠ 当前 HEAD（`git pull`/切分支/reset 后）→ 需构建」，非 git 检出仍回退 mtime，不比改前更容易漏检。
   - `run()`：新增 `vdsh build --clean`（强制全量）与 `--no-retry`（只跑一次），未知参数仍 `EXIT_USAGE`。
2. **`vdsh/features/update.py`**：merge 后**版本号变化**即提示并走 `clean=True`（跨版本就是陈旧产物的高发场景，
   省掉「先失败再清」的一轮浪费）；版本未变仍走增量 + 自动重试兜底。
3. **`vdsh/features/build.py` 模块 docstring** 补机制说明（该文件 docstring 是既定约定）。
4. `_pnpm_command(pnpm, *args)`：pnpm 允许是路径（补 `run`）或完整命令列表（测试注入解释器+脚本）。

### 4. 关键决策与取舍

- **用仓库自带的 `pnpm run clean` 而不是自己删目录**：它由仓库维护、按项目引用图精确删除，
  并会拒绝越界/含未知文件的目录；先静态核对删除集合才敢把它写进失败恢复路径。
- **指纹触发而非无条件全量**：全量重建在本机是数分钟级，日常增量只要秒级；普通源码错误
  （TS 类型错误）不该白等一次全量。`update dsh` 仅在**版本号变化**时提前全量。
- **只重试一次**：陈旧产物清一遍就该一致；第二次仍失败必是真实错误，再清只是浪费时间。
- **基线放在 `lib/` 里**（`lib/.vdsh-build.json`）：与产物同生共死——`pnpm run clean` 清掉它等于
  「基线未知」，下次启动照旧提示构建，天然自洽；不用额外状态目录。
- **基线损坏 ≠ 需构建**：否则写坏一次就会每次启动都提示。缺失才判需构建。
- **不猜「为什么增量构建漏刷」**：本次只保证「不管什么原因，vdsh 能自愈」；
  仓库侧 `tsc -b`/tsdown 的增量行为不归 launcher 改（见 §6 遗留）。

### 5. 验证情况

- **离线复测 30 项全过**（`python _check_build_retry.py`，临时脚本不入库，假 pnpm 驱动 + 临时 git 仓库）：
  A 陈旧产物 → `build/clean/build` 后成功、写基线、成功即删日志；B 普通报错不触发清缓存且诊断给
  `pnpm run clean && pnpm run build`；C 清缓存失败仍重建、且不谎称「已清缓存」；D 清后将仍失败 →
  退出码 3 + 完整日志 + 「实为真实构建失败」；E 命令起不来 → 退出码 4；F `--clean` 单次全量（clean 在 build 前）；
  G `--no-retry` 只跑一次、未知参数退出码 2；H 基线四态（缺失/匹配/HEAD 变化/损坏回退 mtime）。
- `python -m py_compile` 全量 21 文件通过；`python vdsh_launcher.py --help` 冒烟通过；
  `vdsh build --bogus` 退出码 2。
- **真机只读核对**：当前检出产物齐全、无基线 → `build_needed()` 为 True（首次启动会提示一次构建，
  这正是期望行为：无基线 = 构建状态未经验证）；真实报错样本命中指纹、普通 TS 错误不命中。
- 开发过程本身也踩到并修掉三处测试与实现的真缺陷：清缓存失败把「已清缓存」事实抹掉、
  损坏基线仍拿 `None` 比 HEAD 导致每次启动提示、`_pnpm_command` 列表形式漏掉 `run` 子命令。

### 6. 遗留与后续迭代提示

- **没有复现「增量构建为什么漏刷 `lib/`」**：本机没能稳定重现（同一检出重跑是正常的）。
  常见嫌疑是 `tsc -b` 的 `*.tsbuildinfo` 与产物不同步（如构建被中断、或跨版本 merge 后
  增量状态未失效）。用户侧已不再需要追究——vdsh 会自愈——但若将来有人能稳定复现，
  值得回报给 Harness 仓库（`scripts/build.ts`、各包 `tsdown.config.ts` 的 `clean: false`）。
- **全量重建的耗时还不透明**：`--clean` 目前只播报「耗时更长」，没有预估；若要更准，
  可在 `vdsh doctor` 里加一项「上次构建方式（增量/全量）+ 基线 HEAD 是否等于当前 HEAD」。
- **测试脚本仍留在 `%TEMP%`/工作区**（`_check_build_retry.py`、`_fake_pnpm.py`，均已 gitignore）：
  若要长期保留，按 dev.md §5 的约定收进复测清单（当前是「不入库」）。
- **`vdsh build` 的指纹表需要随工具升级维护**：若 rolldown/tsdown 改了 `MISSING_EXPORT` 的措辞，
  重试就不会触发（退化为改前的行为，不会更糟）；新措辞出现时按 `STALE_OUTPUT_RE` 补一条。

### 7. 复测清单（回归用）

1. `python _check_build_retry.py`（假 pnpm，秒级）——A–H 八组必须全绿。
2. `python -m py_compile` 全量 + `python vdsh_launcher.py --help`。
3. 真机只读：`build_needed(仓库)` 在无基线时为 True；`vdsh build`（幂等，增量）成功且写出 `lib/.vdsh-build.json`，
   再次调用 `build_needed` 变 False。
4. 故障演练（可选、需真机）：故意把某包 `lib/index.js` 回退成缺导出的旧内容 → `vdsh build` 应
   自动 `clean` + 重建一次并成功。

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
