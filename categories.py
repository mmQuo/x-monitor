"""Shared post categories and the macro/geopolitics filter."""

MACRO_GROUP = "宏观与地缘"
MACRO_CATEGORIES = ("通胀与利率", "地缘冲突", "能源与石油")
CATEGORIES = [
    "AI资讯", "财经新闻", "公司与产品", "上币与产品", "链上资金", "安全预警", "项目动态", "市场与宏观",
    *MACRO_CATEGORIES, "人物观点", "活动与社区", "其他",
]
CATEGORY_FILTERS = [
    MACRO_GROUP, *MACRO_CATEGORIES,
    *(category for category in CATEGORIES if category not in MACRO_CATEGORIES),
]

MACRO_CATEGORY_RULES = """宏观与地缘按主要事件分类，category 使用以下具体名称：
通胀与利率：CPI、核心通胀、PCE、PPI、非农、失业率、GDP 等经济数据，以及美联储等央行的加息、降息、利率决议、会议纪要、政策讲话、量化宽松/紧缩（QE/QT）、央行资产负债表与缩表/扩表、国债收益率。
地缘冲突：战争、军事行动、停火谈判、国际制裁、外交冲突与贸易摩擦，以及这些事件的进展。
能源与石油：原油、天然气、能源供需，WTI/布伦特油价，EIA 库存与产量、OPEC/OPEC+ 产量政策、油气运输与能源设施事件；区块链 gas 费和代币销毁不属于能源新闻。
交叉主题以主要事件为准：冲突或制裁的进展归地缘冲突；以油价、库存、产量或能源供给变化为主的报道归能源与石油。
上述主题的预测或评论仍按主题分类，但摘要须明确标注是谁预测或表达观点，不得将预期、传闻写成已公布的事实。不要只凭账号名称推断主题。
经济数据与利率需区分实际值、预期值、前值，区分同比/环比、总体/核心指标、百分比/基点，以及决议/预测；原文没有提供的数字不要补充。
其他一般市场走势或资产行情可用市场与宏观，企业财报可用财经新闻；不要将宏观政策、能源或国际新闻归入加密项目动态。
"""


def matches_category(selected: str, category: str | None) -> bool:
    if not selected:
        return True
    if selected == MACRO_GROUP:
        return category in MACRO_CATEGORIES
    return category == selected
