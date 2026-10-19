"""兼容历史活动代码；新活动名称不受词表限制。"""

import unicodedata

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
    value = unicodedata.normalize("NFKC", value).strip()
    return ALIASES.get(value, value)


def match_activity_name(value: str, catalog: list[dict] | None = None) -> str:
    """Normalize a model name and reuse an exact name from the current catalog."""
    normalized = activity_name(value)
    if not catalog:
        return normalized
    names = {activity_name(item["name"]): item["name"] for item in catalog if item.get("name")}
    return names.get(normalized, normalized)
