# 小白教程：两台电脑同步 DSH 数据

> 本教程**不要求你懂 Git**。跟着做就行：每步都告诉你「做什么 → 为什么 → 看到什么算成功」。
> 全程大约 15 分钟。如果中途卡住，直接看 [第 8 节：常见问题](#8-常见问题-小白版)。

---

## 0. 先弄懂这三句话（真的只需要三句）

把这个工具想象成：**一个数据储物柜**（git 仓库），两台电脑的 DSH 数据都往这里存、从这取。

| 说法 | 意思 | 什么时候用 |
|---|---|---|
| 推送 push | 把**本机**的 DSH 数据**存进**储物柜 | **收工前**（要关机/要换机器前） |
| 拉取 pull | 把储物柜里的数据**搬到本机** | **开工前**（新的一天/换机器后） |
| 状态 status | 看看本机和储物柜**差多少** | 想确认时 |

**唯一规则**：两台电脑**不要同时干活**。A 收工推送 → B 开工拉取 → B 收工推送 → A 开工拉取……循环。

> 为什么这么麻烦？因为 DSH 的数据（聊天记录、设置）是「哪台电脑哪份」。这个工具让你
> **两台电脑看到同一份数据**，代价就是轮流使用。

---

## 1. 需要准备什么（先检查清单）

两台电脑都完成下面 4 项才能开始：

- [ ] **两台 Windows 电脑**：本教程称 A = 主力机（数据最多那台），B = 副机。
- [ ] **两台都装好了 DSH**，并且都**正常使用过**（打开过、聊过天）。
- [ ] **两台都装了 Git**。检查方法：打开 PowerShell（见第 2 节），输入 `git --version` 回车。
      - 有版本号（如 `git version 2.4x.x`）→ 通过。
      - 报「不是内部或外部命令」→ 在 PowerShell 里执行 `winget install Git.Git` 后重开窗口；
        或去 https://git-scm.com/ 下载，一路点「下一步」安装。
- [ ] **A 电脑上建好一个共享文件夹**，比如 `T:\DataBase`（工具会在里面创建储物柜）。
      B 电脑需要能访问它（网上邻居/共享，见第 4 节）。

✔️ 另外：把整个 **`dsh-launcher`** 文件夹拷到两台电脑上（它同时包含启动器 `vdsh` 和本同步工具）。
放在哪都行，本教程按 **`C:\tools\dsh-launcher\dsh-data-git-sync`** 举例（放别的路径就把教程里的路径替换掉）。
装了 launcher 后，教程里的 `powershell -ExecutionPolicy Bypass -File C:\tools\dsh-launcher\dsh-data-git-sync\sync-dsh.ps1 XXX`
都可直接简写成 **`vdsh sync XXX`**；下面仍以完整写法举例、便于看清每步做的什么。

---

## 2. 怎么打开「命令窗口」（只学这一次）

两种方式，任选：

- **懒人法（推荐小白）**：直接**双击 `sync-dsh.cmd`** → 弹出菜单，按数字键选就行。
- **标准法（能看到完整输出）**：按 `Win 键`，输入 `powershell`，回车。
  之后教程里的命令都在这个**蓝/黑窗口**里打字，每输完一行按**回车**执行。

> 两种方法背后是同一个工具：菜单只是把命令帮你点了。
> 下面教程以命令行为主（能看清每一步），菜单法会顺带说明。

---

## 3. 第一步：在 A（主力机）创建数据储物柜

> 只做**一次**。B 电脑不需要做这步。

打开 PowerShell，逐条输入（每条回车、等它执行完）：

```powershell
# ① 创建储物柜（一个专门存数据的文件夹）
git init --bare T:/DataBase/dsh-sync-repo.git
```
**预期**：显示 `Initialized empty Git repository in T:/DataBase/dsh-sync-repo.git/`。
（已经建过会提示「已存在」之类，也没关系。）

```powershell
# ② 让工具认识储物柜，并生成「黑名单」（防止 API 密钥等被误存）
powershell -ExecutionPolicy Bypass -File C:\tools\dsh-launcher\dsh-data-git-sync\sync-dsh.ps1 init file:///T:/DataBase/dsh-sync-repo.git
```
**预期**：依次看到 `初始化 git 仓库` → `添加 origin` → `生成 .gitignore` → `core.autocrlf = true`。
（还会提示「远端还没有 main 分支……先 push」——正常，下一步就建了。）

```powershell
# ③ 第一次把 A 的数据存进去（自动建立跟踪）
powershell -ExecutionPolicy Bypass -File C:\tools\dsh-launcher\dsh-data-git-sync\sync-dsh.ps1 push
```
**预期**：最后一行 `✅ 推送完成。`
（第一次会看到 `fatal: ... no upstream branch` 的提示——这是正常流程，工具会自动再试 `-u` 建好。）

```powershell
# ④ 看一眼（可选）
powershell -ExecutionPolicy Bypass -File C:\tools\dsh-launcher\dsh-data-git-sync\sync-dsh.ps1 status
```
**预期**：`上游: origin/main — 本地领先 0 提交 / 落后 0 提交`，`待推送: 无`。
（A 这台如果 DSH 正好开着、正在写聊天记录，`待推送` 有几个文件也是正常的，属于新数据。）

💡 菜单法：双击 `sync-dsh.cmd`，按 `2`（推送）、按 `1`（状态）。

---

## 4. 第二步：让 B 电脑「看得见」A 的储物柜

> 原理：把 A 电脑的 `T:\DataBase` 变成 B 电脑的一个盘符（比如 `Z:`）。

**4.1 在 A 电脑上允许共享**（只需一次）：

1. 打开文件资源管理器，右键 `T:\DataBase` → **属性** → **共享** → **共享...**
2. 添加 `Everyone`（或你的账号），权限选**读取/写入**，点共享。
3. 记下 A 电脑的名字：在 A 的 PowerShell 输入 `hostname` 回车（记下来，比如 `A-PC`）。
   （不确定网络环境？用 A 的 IP 也行：A 上输入 `ipconfig`，找到 IPv4 地址。）

**4.2 在 B 电脑上映射盘符**：

方式一（图形界面）：B 上打开文件资源管理器 →「此电脑」右键 →**映射网络驱动器** →
驱动器选 `Z:`，文件夹填 `\\A-PC\DataBase`（或 `\\192.168.1.10\DataBase`），勾选「重新连接时登录」→ 完成。

方式二（命令）：B 的 PowerShell 输入：
```powershell
net use Z: \\A-PC\DataBase
```

**4.3 验证**：B 的 PowerShell 输入 `dir Z:\`，能看到 `dsh-sync-repo.git` 文件夹 → 成功。

> 你自己的环境不同？（比如裸仓库在别的路径、或走了内网穿透）——没关系，原理一样：
> **A 的裸仓库路径** 对应 A 写 `file:///T:/...`，**B 的可访问路径** 对应 B 写 `file:///Z:/...`。
> 把教程里的路径换成你实际的即可。

---

## 5. 第三步：B 电脑接入（拿 A 的数据）

> 只做**一次**。

B 的 PowerShell 输入：

```powershell
powershell -ExecutionPolicy Bypass -File C:\tools\dsh-launcher\dsh-data-git-sync\sync-dsh.ps1 init file:///Z:/DataBase/dsh-sync-repo.git
```

然后**看屏幕提示，二选一**：

- **「数据目录为全新，自动从远端填充数据… → ✅ 已完成」**：说明 B 是台全新电脑，数据已经自动搬进来了，**这条命令到此完成**。
- **「本机尚无提交但目录里已有 DSH 数据」**：说明 B 之前自己用过 DSH（有自己的一堆数据）。此时**选一条**：
  - 命令 `git -C "$HOME\.dsh" merge origin/main` —— 把 A 的数据和 B 的数据**合并**（推荐，两边都要）；
  - 命令 `git -C "$HOME\.dsh" reset --soft origin/main` —— 以 A 的数据为准，B 的数据变成「待推送」的新数据。
- **「远端还没有 main 分支」**：说明 A 还没推送过，回去看第 3 节第 ③ 步。

完成后验证：

```powershell
powershell -ExecutionPolicy Bypass -File C:\tools\dsh-launcher\dsh-data-git-sync\sync-dsh.ps1 status
```
**预期**：`分支: main`、`上游: origin/main`、`待推送: 无`（或列出 B 特有数据，正常）。

---

## 6. 日常使用（记住这一句就够了）

> **开工前拉一次，收工前推一次。**

| 时机 | 怎么做 |
|---|---|
| 早上/换机器后，准备用 DSH | 推送前**先拉取**：`pull` |
| 干完活，要关机/换机器 | **先推送**：`push` |
| 想确认两边一致 | `status` |

**命令行版**（PowerShell）：
```powershell
powershell -ExecutionPolicy Bypass -File C:\tools\dsh-launcher\dsh-data-git-sync\sync-dsh.ps1 pull
powershell -ExecutionPolicy Bypass -File C:\tools\dsh-launcher\dsh-data-git-sync\sync-dsh.ps1 push
powershell -ExecutionPolicy Bypass -File C:\tools\dsh-launcher\dsh-data-git-sync\sync-dsh.ps1 status
```
（装了 dsh-launcher：上面三条就是 `vdsh sync pull` / `vdsh sync push` / `vdsh sync status`；
`vdsh --sync` 启动服务前还会自动先拉取一次。）

**菜单版**：双击 `sync-dsh.cmd` → 按 `3`（拉取）/ `2`（推送）/ `1`（状态）。

**演练一次**（强烈建议，验证真的通了）：
1. A 上发一条聊天消息 → A 收工 `push`；
2. B 上开工 `pull` → B 打开 DSH，能看到那条消息 → 成功！

---

## 7. 别人想不起「规则」时的三条注意事项

1. **DSH 正在干活时不要同步**：如果 DSH 正在生成回复（聊天记录还在动），`push` 会把这些新记录也
   存进去（正常）；`pull` 会提示「先推送」。**等 DSH 空闲/再同步**最省心。
2. **别把 API 密钥弄进去**：`.credentials.yaml`（密钥文件）已被自动排除，永远不会被同步。
   你不需要做任何事。
3. **看到 `LF will be replaced by CRLF` 警告**：正常现象，忽略即可。

---

## 8. 常见问题（小白版）

| 现象 | 原因 | 怎么办 |
|---|---|---|
| 双击 `sync-dsh.cmd` 窗口一闪而过 | 没装 Git 或脚本报错 | 先装 Git（第 1 节）；或改用 PowerShell 手动运行看完整报错 |
| 提示「尚未初始化」 | 这台电脑还没执行过 `init` | 先 `init <地址>`（第 3/5 节） |
| 提示「数据目录不存在」 | 这台电脑没运行过 DSH | 先打开 DSH 用一下；或直接用 `init`（会自动创建） |
| 提示「工作区有未提交变更，先 push」 | `pull` 前本机有未存的新数据 | 先执行 `push`，再 `pull` |
| 提示「冲突」（merge 报错） | 两台都改了同一份数据（没遵守轮流使用） | **先回滚**：`git -C "$HOME\.dsh" merge --abort`；详细处理见 `docs/native-git-sync.md` 第 6 节 |
| 提示「远端 origin/main 不存在」 | A 还没推送过 | 去 A 执行一次 `push` |
| 提示「`fatal: ... no upstream branch`」 | 第一次推送（正常） | 不用管，工具会自动用 `-u` 补建；看到 ✅ 推送完成即成功 |
| `status` 显示「待推送: xxx」 | 本机有新增数据（含刚聊的天） | 推送后就没了；如果一直有且不是你产生的，先 `pull` 看远端 |
| 找不到 `$HOME\.dsh` | 你的 DSH 数据目录可能不是默认位置 | 在 PowerShell 输入 `echo $env:DSH_HOME` 看实际路径；命令里的 `$HOME\.dsh` 就是它 |

---

## 9. 终极备忘（复制到桌面都行）

```
【每天】开工前：  pull（拉取）
【每天】收工前：  push（推送）
【一年一次】换电脑：A 建储物柜 → B init 接入（第 3、4、5 节）

数据目录：  $env:DSH_HOME（默认 C:\Users\你的用户名\.dsh）
同步范围：  sessions/（聊天） profiles/web/（配置+插件数据） storages/（插件数据）
            attachments/（附件图） memories/（记忆） settings.yaml（设置）
                —— 其余自动忽略；.credentials.yaml 永不入库。
详细文档：docs/native-git-sync.md
```
