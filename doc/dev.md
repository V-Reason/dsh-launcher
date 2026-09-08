# 开发指南

> 历次迭代的背景 / 改动清单 / 决策取舍 / 验证与遗留见 [devlog.md](devlog.md)（按日期追加）；
> 修改任何文件前先读 §1 目录职责表与 §3 编码约定。

## 0. 开发环境

- Python 3.9+（当前 3.13）：`pip install requests pyyaml`
- PowerShell 7（spawn 宿主）+ Windows PowerShell 5.1（同步脚本宿主，系统自带）
- Git（同步与发布用）

代码检查：改完运行 `python -m py_compile`（全量），入口冒烟 `python vdsh_launcher.py --help`。

## 1. 目录职责速查

| 路径 | 职责 | 修改时的注意 |
|---|---|---|
| `vdsh_launcher.py` | 入口薄壳 | 几乎不动；只做 sys.path 与 `from vdsh.app import main` |
| `vdsh/app.py` | 分发器、首启接线 | 新功能在注册表加行，不在此堆逻辑 |
| `vdsh/settings.py` | vdsh.yaml 全部语义 | **TEMPLATE 与 DEFAULTS/VALIDATORS 需同步改**；模板是 raw 字符串，首行不能以 `\` 开头 |
| `vdsh/config.py` | 常量 | 退出码新增在此 |
| `vdsh/bootstrap.py` | 向导 | 与 `features/setup.py` 共用；提问接受逻辑在此 |
| `vdsh/spinner.py` | 动画/子进程 | `configure()` 在 app 启动时设置全局；新增超时/参数导出注意线程安全（见 experience.md） |
| `vdsh/module_fallback.py` | 模块回退目录自愈 | 纯标准库；判定必须与 app-boot `ensureSymlink` 对齐（链接/proxy 保留，其余删除）；launch 与 dsh_cli 双通道调用；data_dir 缺省 `~/.dsh` |
| `vdsh/features/sync.py` | 同步桥 | 退出码透传语义 0-4 不能变；`init` 无参（TTY）= 配置向导（repo/data_dir/remote 校验并写 vdsh.yaml）；同步脚本是 `dsh-data-git-sync/sync-dsh.ps1` |
| `dsh_cli.py` | 官方 CLI 转发壳（dsh.cmd 调用） | 纯标准库；按 DSH_REPO → vdsh.yaml → `REPO_CANDIDATES` 解析并转发 node；不写配置 |
| `vdsh/features/update.py` | 更新（dsh/plugin） | git fetch/merge、pnpm 经 `run_child_progress`；插件更新走官方 `dsh plugin` 通路（bundle 重调解） |
| `dsh-data-git-sync/sync-dsh.ps1` | 同步本体 | **UTF-8 BOM 文件**；任何编辑器保存可能去 BOM（PS 5.1 会按 GBK 误读 → 全文件报错）；`BuiltinIgnoreRules` 里的路径是同步卫生红线（junction 展开入库的坑，见 experience.md §8.5），新排除项进这里而非 `gitignore_extra` |

## 2. 新增一个功能（5 步）

1. 建 `vdsh/features/<name>.py`：

```python
# vdsh/features/agent.py
from ..console import say

NAME = "agent"
SUMMARY = "示例功能：打印一句话"

def run(argv, settings):
    if argv:
        from ..console import die
        die("agent 不接受参数", 2)
    say("hello")
    return 0
```

2. 在 `vdsh/features/__init__.py` 注册：

```python
from . import agent
FEATURES = {..., "agent": (agent.NAME, agent.SUMMARY, agent.run)}
```

3. `vdsh/cli.py` 的 `print_usage()` 无需手工维护（自动遍历 FEATURES）。
4. `doc/usage.md` 命令表与 `doc/design.md` 分层图补一行；README「功能总览」视规模决定。
5. 测试（见 §5）→ `python vdsh_launcher.py agent` 冒烟。

## 3. 编码约定

- **输出**：一律走 `vdsh/console.py`（say/step/warn/die），不要裸 `print` 到 stderr 或混用前缀。退出码只经 `die(code=...)` 或 feature `return`。
- **配置读取**：功能入口的参数是 `settings`（完整 dict，永不为 None）；环境覆盖用 `settings.effective_*`。
- **动画**：等待型子进程用 `spinner.run_child_progress(cmd, message, env=..., timeout=...)`；手工异步进度用 `Spinner.say()`。
- **进程**：需要捕获输出的用 `run_child_progress`（内部已处理 UTF-8/流式/Ctrl+C/超时）；需要继承 stdio 的（交互菜单、Read-Host）用 `subprocess.call`。
- **提示文案保持中文**；英文仅限代码与标识符。
- **不加新运行时依赖**。确有需要（如 YAML 这类）惰性导入 + 清晰报错，见 `settings.load_settings` 的 pyyaml 处理。

## 4. 核心机制调用示例

```python
# 用配置的动画与超时跑一个长命令
from .spinner import run_child_progress
code = run_child_progress(["git", "fetch"], "拉取远端…", env=env, timeout=120)

# 文本级安全写配置（保留注释）
from . import settings as S
S.patch_values({("sync", "remote"): "file:///Z:/repo.git"})

# 查看生效值（含 env 覆盖）
repo = S.effective_repo(settings)
tailnet = S.effective_tailnet(cli_value, settings)
```

## 5. 测试方法（不碰真实数据）

沙箱环境：本会话的 PowerShell 沙箱**禁止 git 的网络与 sh 信号管道**（Win32 error 5），因此网络型 git 操作无法在此端到端复现——用以下降级方法：

1. **配置/向导**：删除 vdsh.yaml → monkeypatch `builtins.input` + 假 TTY（`isatty()` 返回 True）喂答案 → 断言 `load_settings()` 结果。见过往实现（test_wizard 五场景：有效/重试/默认/跳过/3 次无效）。
2. **动画**：假 TTY stdout 类（记录 writes）断言帧序列、`say()` 行不被覆盖、`finish()` 擦除；`run_child_progress` 用 `python -u -c 'sleep'` 做超时击杀测试（taskkill 返回码检查 + kill 回退）。
3. **同步**：临时 DSH_HOME + `git init --bare` 本地远端 + 临时 vdsh.yaml 配置（`sync.remote`/`allowlist`/`gitignore_extra`/`commit_name`）验证 init/push 的暂存范围、.gitignore 内容、提交身份、`remote set` 的 git origin + yaml 行替换（注释保留）。
4. **PS 脚本**：每次编辑后 `powershell -File doc/…` 用 `[System.Management.Automation.Language.Parser]::ParseFile` 校验 + 恢复 BOM（用 ReadAllText(UTF8 无 BOM) + WriteAllText(UTF8 带 BOM)）。
5. **退出码**：不接管道运行并 `echo $LASTEXITCODE`（管道 + Select-Object 会吞掉/污染退出码）。

## 6. 测试与发布流程

1. 全量复测（§5 各项）+ py_compile + `python vdsh_launcher.py --help`。
2. `git add -A && git commit -m "feat: ..."`（提交信息随功能，中文正文亦可）。
3. `git push`（本沙箱需要完整权限批准 git 的 ssh 传输；真实终端直接 push 即可）。
4. 文档类改动与代码同 commit 或紧随其后。

## 7. 常见坑速记（详见 experience.md）

- PS 5.1：`ConvertFrom-Json` 顶层数组不拆管道（需展平一层）；`-File` 读无 BOM UTF-8 会当 GBK；`2>&1 | ForEach {"$_"}` 会把 stderr 变 ErrorRecord 文案。
- YAML：双引号内 `\` 必须转义；值生成用 `json.dumps` 最稳；raw 字符串开头的 `\` 会变成文件首字符。
- 沙箱外代码不要依赖「本会话沙箱行为」（taskkill 权限、管道限制）——那是 DSH 沙箱约束，用户环境无此限制。
