"""兼容历史活动代码；新活动名称不受词表限制。"""

ALIASES = {
    "阅读": "reading",
    "读书": "reading",
    "书写": "writing",
    "使用电脑": "computer",
    "使用手机": "phone",
    "离开画面": "away",
    "无人": "away",
    "休息": "resting",
    "其他活动": "other",
    "无法判断": "unknown",
    "睡眠": "睡觉",
    "sleeping": "睡觉",
}


def activity_name(value: str) -> str:
    value = value.strip()
    return ALIASES.get(value, value)
