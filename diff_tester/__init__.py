"""diff_tester — 安卓→鸿蒙翻译差分模糊测试工具包（离线评估版）。

实现依据：《AI实现参考1_完整差分测试链路.md》与《差分测试设计文档.md》。
可独立运行的 CLI 工具包，不依赖 A2H 翻译流水线代码。

三个子命令：
  python -m diff_tester record   # 安卓端探索 + 录制抽象事件轨迹
  python -m diff_tester replay   # 鸿蒙端语义回放 + 分叉检测
  python -m diff_tester evaluate # 归因 + 指标汇总 + 报告
"""

__version__ = "0.1.0"
