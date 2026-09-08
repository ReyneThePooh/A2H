"""全局配置。

阈值（0.75/0.55/0.9/0.8）、权重、掩码正则、白名单路径全部外置可配（验收要求 §9），
支持 `--config config.yaml` 覆盖默认值。
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# 默认掩码正则（§6.1）：命中 → 替换为 "<VOLATILE>"
# ---------------------------------------------------------------------------
DEFAULT_MASK_PATTERNS: list[str] = [
    r"\d{1,2}:\d{2}(:\d{2})?",                      # 时间 12:34 / 12:34:56
    r"\d{4}[-/年]\d{1,2}[-/月](\d{1,2}日?)?",        # 日期 2026-09-04 / 2026年9月4日
    r"\d{1,2}[月/-]\d{1,2}日?",                      # 短日期 9/4、9月4日
    r"\d{6,}",                                       # 纯数字长串（验证码、订单号等）
    r"\d{1,3}(\.\d+)?\s*%",                          # 百分比进度 / 电量样式
]

# TYPE 事件模糊语料库（§6.4）：合法值 + 边界值
DEFAULT_TYPE_CORPUS: list[str] = [
    "test@example.com",                              # 合法邮箱
    "13800138000",                                   # 合法手机号
    "hello 世界 Mixed123",                            # 中英混排
    "x" * 200,                                       # 超长 200 字符
    "\U0001F600\U0001F389",                          # emoji
    "",                                              # 空串
    "Robert'); DROP TABLE--",                        # 含 '-- 的串
]


@dataclass
class MatcherConfig:
    """控件对齐器权重与阈值（§6.2）。"""

    w_id: float = 0.35
    w_text: float = 0.25
    w_role: float = 0.10
    w_pos: float = 0.15
    w_img: float = 0.15
    th_hi: float = 0.75      # >= th_hi 且与第二名差距 >= min_gap → MATCHED
    th_lo: float = 0.55      # [th_lo, th_hi) 或差距不足 → AMBIGUOUS；< th_lo → UNMAPPED
    min_gap: float = 0.10


@dataclass
class OracleConfig:
    """分层预言阈值（§6.3）。"""

    jaccard_min: float = 0.90        # 掩码文本 multiset Jaccard 下限
    widget_align_min: float = 0.80   # 控件对齐率下限
    widget_text_sim_min: float = 0.80  # L2 控件文本模糊匹配阈值（0~1）
    page_stem_sim_min: float = 0.75  # 页面名启发式匹配阈值（无映射表条目时）
    enable_l3: bool = True           # L3 视觉参考（仅记录，不判失败）


@dataclass
class DeviceConfig:
    """设备与外部命令行工具。"""

    adb_path: str = "adb"
    hdc_path: str = "hdc"
    android_serial: Optional[str] = None
    harmony_serial: Optional[str] = None
    cmd_timeout_s: float = 30.0
    cmd_retries: int = 1             # 失败重试次数（验收要求：1 次）
    stable_timeout_s: float = 10.0   # 界面稳定判据超时（§4.6）
    stable_interval_s: float = 0.5   # 两次 dump 最小间隔
    launch_wait_s: float = 3.0       # 冷启动后固定等待
    harmony_ability: str = "EntryAbility"  # aa start 的 ability 名


@dataclass
class ExplorerConfig:
    """源端控件感知探索器（§6.4）。"""

    back_prob: float = 0.10          # 每步改选系统事件 BACK 的概率
    long_click_prob: float = 0.05
    swipe_prob: float = 0.05         # 对 list 控件改做 SWIPE 的概率
    novelty_weight: float = 2.0      # 从未操作过的控件权重
    visited_weight: float = 1.0
    random_seed: Optional[int] = None
    type_corpus: list[str] = field(default_factory=lambda: list(DEFAULT_TYPE_CORPUS))


@dataclass
class Config:
    matcher: MatcherConfig = field(default_factory=MatcherConfig)
    oracle: OracleConfig = field(default_factory=OracleConfig)
    device: DeviceConfig = field(default_factory=DeviceConfig)
    explorer: ExplorerConfig = field(default_factory=ExplorerConfig)
    mask_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_MASK_PATTERNS))
    whitelist_path: Optional[str] = None   # 平台差异白名单 yaml（归因用）

    @classmethod
    def load(cls, yaml_path: Optional[str] = None) -> "Config":
        cfg = cls()
        if yaml_path:
            import yaml  # 延迟导入，避免离线单测硬依赖
            with open(yaml_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            _update_dataclass(cfg, data)
        return cfg


def _update_dataclass(obj, data: dict) -> None:
    """用嵌套 dict 覆盖 dataclass 字段，未知键报错以防拼写错误静默失效。"""
    for k, v in data.items():
        if not hasattr(obj, k):
            raise KeyError(f"未知配置项: {k}（对象 {type(obj).__name__}）")
        cur = getattr(obj, k)
        if dataclasses.is_dataclass(cur) and isinstance(v, dict):
            _update_dataclass(cur, v)
        else:
            setattr(obj, k, v)
