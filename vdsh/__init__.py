# -*- coding: utf-8 -*-
"""vdsh — DeepSeek Harness Web 多功能启动器（Windows / PowerShell 7）。

定位：实现「dsh 插件做不到的事」——一切需要发生在 DSH 进程之外的操作
（进程/仓库管理、进程外原生 Git 数据同步、终端体验）。功能尽量解耦：

  共享层  config     常量、路径、退出码（唯一常量来源）
          settings   vdsh.yaml 统一配置：加载/校验/合并/模板/文本级补丁/报告
          spinner    加载动画 + 子进程流式执行器（含超时）
          console    控制台流编码、文案前缀、退出码统一辅助
          bootstrap  首次运行引导（交互向导；vdsh setup 共用）
  功能层  features/  launch 启动 / build 构建 / sync 数据同步 /
                     config 配置查看 / setup 配置向导 / doctor 环境自检
  分发器  app        main()：首次运行引导 → 配置加载 → 功能注册表分发
"""
