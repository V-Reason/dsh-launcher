# vdsh Launcher

**在 DSH 进程之外，补齐 dsh 插件做不到的事**：一键启动 dsh web、仓库构建、DSH 数据双机同步、配置与自检。

## 快速开始

**1. 依赖**：Python 3（`pip install requests pyyaml`）、PowerShell 7（`pwsh`）；`vdsh sync` 另需 Git。

**2. 安装**：把本目录加入用户 PATH（一次性），新开控制台生效：

```powershell
[Environment]::SetEnvironmentVariable(
  'Path',
  [Environment]::GetEnvironmentVariable('Path', 'User') + ';T:\Open-Source\dsh-launcher',
  'User')
```

**3. 首次运行**：输入 `vdsh` → 向导提示输入 **DSH 安装目录**（Harness 仓库根目录，须含 package.json）→ 自动启动服务并打开浏览器。中途跳过/非交互也可用，之后随时 `vdsh setup` 重配。

**4. 日常命令**：

```powershell
vdsh                              # 启动服务并打开浏览器（当前目录）
vdsh --sync                       # 启动前自动 pull 数据
vdsh --tailnet xxx.ts.net         # 手机经 Tailscale 访问
vdsh sync push / pull / status    # 收工前推送 / 开工前拉取 / 查看状态
vdsh sync init <URL>              # 一次性初始化同步仓库
vdsh sync remote [set <URL>]      # 查看/设置远端仓库位置
vdsh update dsh                   # 更新 Harness 本体（git pull + install + build）
vdsh update plugin [web]          # 更新 profile 插件依赖（update --latest）
vdsh config                       # 查看生效配置
vdsh doctor                       # 环境自检
```

**5. 配置**：自动生成的 `vdsh.yaml`（启动器目录）——启动超时、动画帧率、同步范围/远端/身份等，删行即回退默认；详细说明见 [doc/usage.md](doc/usage.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [doc/usage.md](doc/usage.md) | 完整使用参考：命令、配置全键、手机访问、工作区、数据同步、退出码 |
| [doc/design.md](doc/design.md) | 定位与架构设计：分层、功能协议、关键机制与理由 |
| [doc/dev.md](doc/dev.md) | 开发指南：新增功能 5 步、编码约定、测试与发布 |
| [doc/experience.md](doc/experience.md) | 经验与踩坑：PS 5.1 差异、编码、YAML、沙箱限制等 |

## 功能一览

| 命令 | 说明 |
|---|---|
| `vdsh`（默认） | 启动 dsh web：最小化窗口、就绪检测、工作区注册、打开浏览器 |
| `vdsh build` | 直接执行仓库构建（问题自动检测亦可），带动画 |
| `vdsh sync …` | 进程外原生 Git 同步 DSH 数据（避免插件自指）；动画 + 超时 |
| `vdsh update dsh / plugin` | 更新 Harness 本体 / profile 插件依赖（分开执行） |
| `vdsh config` / `setup` / `doctor` | 配置查看 / 重跑向导 / 环境自检 |

> 原名 `dsh`，与官方 Harness CLI 冲突已改名；数据同步的完整方法见 `dsh-data-git-sync/docs/`。

[MIT](LICENSE)
