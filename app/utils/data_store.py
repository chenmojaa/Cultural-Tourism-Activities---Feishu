# -*- coding: utf-8 -*-
"""数据存储层:飞书多维表格唯一写入"""
import logging
from typing import List, Optional

import config
from app.utils.feishuapi import FeishuClient

log = logging.getLogger("wenglu")


# ============================================================
# 字段值转换
# ============================================================
def _date_str_to_ms(value) -> Optional[int]:
    """把日期字符串(YYYY-MM-DD 或 YYYY-MM-DD HH:MM[:SS])转毫秒时间戳。
    失败返回 None。"""
    import datetime as _dt
    s = str(value).strip()
    if not s:
        return None
    # 优先尝试带时间的格式
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = _dt.datetime.strptime(s[:19], fmt)
            return int(dt.timestamp() * 1000)
        except Exception:
            continue
    return None


def to_feishu_value(value, field_type: str = "text"):
    """把内部值转成飞书接受的格式。空值返回 None(飞书跳过该字段)。

    - text:返回 str
    - date:返回 int 毫秒时间戳(Feishu Bitable Date 字段要求)
    """
    if value is None:
        return None
    if field_type == "date":
        return _date_str_to_ms(value)
    s = str(value).strip()
    return s or None


# ============================================================
# DataStore
# ============================================================
class DataStore:
    def __init__(self, client: FeishuClient = None):
        self.client = client or FeishuClient()

    def load_existing_records(self) -> List[dict]:
        """从飞书拉取所有原始记录"""
        return self.client.list_records()

    def add_rows(self, rows: List[dict]) -> List[dict]:
        """内部行 → 飞书格式 → 写入。日期字段(发布/使用)按 Feishu Date 规范转毫秒时间戳。"""
        feishu_records = []
        for row in rows:
            fields = {}
            for col in config.COLUMNS:
                if col not in row:
                    continue
                v = row[col]
                mapped = config.FEISHU_FIELD_MAPPING.get(col, col)
                ftype = config.FEISHU_FIELD_TYPES.get(col, "text")
                conv = to_feishu_value(v, field_type=ftype)
                if conv is not None:
                    fields[mapped] = conv
            feishu_records.append(fields)
        return self.client.add_records(feishu_records)

