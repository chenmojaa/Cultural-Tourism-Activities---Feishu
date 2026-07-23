# -*- coding: utf-8 -*-
"""MySQL 客户端:连 jd_rb_case 查重表,提供 url/summary 维度的查询"""
import logging
from contextlib import contextmanager
from typing import Iterable, List, Optional

import pymysql
from pymysql.cursors import DictCursor

import config

log = logging.getLogger("wenglu")


class MySQLError(Exception):
    pass


def _connect():
    try:
        return pymysql.connect(
            host=config.MYSQL_HOST,
            port=config.MYSQL_PORT,
            user=config.MYSQL_USER,
            password=config.MYSQL_PASSWORD,
            database=config.MYSQL_DATABASE,
            charset="utf8mb4",
            cursorclass=DictCursor,
            connect_timeout=5,
            read_timeout=10,
            write_timeout=10,
        )
    except Exception as e:
        raise MySQLError(f"MySQL 连接失败: {e}") from e


@contextmanager
def mysql_conn():
    """上下文管理器:用完自动关闭"""
    conn = _connect()
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


def find_by_url(url: str) -> Optional[dict]:
    """按 url 字段精确匹配。命中返回一条记录(dict),无则 None。

    注意:既查 url 列也查 url2 列(数据表里 url2 通常是备用链接)。
    """
    if not url:
        return None
    try:
        with mysql_conn() as conn:
            with conn.cursor() as cur:
                days = getattr(config, "DEDUP_LOOKBACK_DAYS", 120)
                sql = (
                    f"SELECT * FROM `{config.MYSQL_TABLE}` "
                    "WHERE (`url` = %s OR `url2` = %s) "
                    "AND `publish_time` IS NOT NULL "
                    "AND `publish_time` >= DATE_SUB(NOW(), INTERVAL %s DAY) "
                    "ORDER BY `publish_time` DESC, `id` DESC LIMIT 1"
                )
                cur.execute(sql, (url, url, days))
                return cur.fetchone()
    except MySQLError as e:
        log.warning(f"MySQL find_by_url 失败: {e}")
        return None


def recent_summaries(days=None) -> List[str]:
    """从 jd_rb_case 拉最近 N 天(publish_time 维度)的 summary,用于语义去重比对。
    days 由 config.DEDUP_LOOKBACK_DAYS 控制,默认 120 天(4 个月)。
    - 只看近 N 天发布过的,避免历史老 summary 误判当前批次为重复
    - 按 publish_time DESC 排(同窗口内最新的优先)
    返回纯文本 list;查不到 / 异常 时返回空 list。
    """
    if days is None:
        days = getattr(config, "DEDUP_LOOKBACK_DAYS", 120)
    if days <= 0:
        return []
    try:
        with mysql_conn() as conn:
            with conn.cursor() as cur:
                sql = (
                    f"SELECT `summary` FROM `{config.MYSQL_TABLE}` "
                    "WHERE `summary` IS NOT NULL AND `summary` <> '' "
                    "AND `publish_time` IS NOT NULL "
                    "AND `publish_time` >= DATE_SUB(NOW(), INTERVAL %s DAY) "
                    "ORDER BY `publish_time` DESC, `id` DESC"
                )
                cur.execute(sql, (days,))
                rows = cur.fetchall()
                return [r["summary"] for r in rows if r.get("summary")]
    except MySQLError as e:
        log.warning(f"MySQL recent_summaries 失败: {e}")
        return []


# MySQL 字段类型映射(用来规范化日期值)
DATE_FIELDS = {
    "publish_time": "datetime",  # datetime DEFAULT NULL
    # use_time 不再入库(用户要求保持 NULL)
}


def _normalize_date(value, col_type: str):
    """把日期值规范化为 MySQL 可接受的格式。支持:
    - YYYY-MM-DD / YYYY-MM-DD HH:MM:SS / YYYY-MM-DDTHH:MM:SS
    - YYYY/MM/DD / YYYY/MM/DD HH:MM:SS / YYYY/MM/DDTHH:MM:SS
    - YYYY\u5e74MM\u6708DD\u65e5(中文格式)
    - \u6beb\u79d2\u65f6\u95f4\u6233(int \u6216\u5168\u6570\u5b57\u5b57\u7b26\u4e32,\u98de\u4e66 Bitable Date \u5b57\u6bb5\u8fd4\u56de\u8fd9\u4e2a)
    - \u5165\u53c2\u662f int \u6216 float: \u89c6\u4e3a ms \u65f6\u95f4\u6233

    datetime: YYYY-MM-DD HH:MM:SS(没时间就补 00:00:00)
    date:     YYYY-MM-DD
    解析失败返回 None(交由上层跳过)
    """
    import re as _re
    import datetime as _dt
    if value is None:
        return None
    # 1) \u6570\u5b57\u7c7b\u578b\u6216\u5168\u6570\u5b57\u5b57\u7b26\u4e32 \u2192 \u89c6\u4e3a\u6beb\u79d2\u65f6\u95f4\u6233
    if isinstance(value, (int, float)):
        try:
            ts = float(value)
            dt = _dt.datetime.fromtimestamp(ts / 1000.0)
            date_part = dt.strftime("%Y-%m-%d")
            time_part = dt.strftime("%H:%M:%S")
            if col_type == "datetime":
                return f"{date_part} {time_part}"
            return date_part
        except Exception:
            return None
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit():
        try:
            ts = int(s)
            dt = _dt.datetime.fromtimestamp(ts / 1000.0)
            date_part = dt.strftime("%Y-%m-%d")
            time_part = dt.strftime("%H:%M:%S")
            if col_type == "datetime":
                return f"{date_part} {time_part}"
            return date_part
        except Exception:
            return None
    # 2) \u5b57\u7b26\u4e32 \u2192 \u5404\u79cd\u5e38\u89c1\u683c\u5f0f
    m = _re.match(
        r"(\d{4})[-/\u5e74](\d{1,2})[-/\u6708](\d{1,2})(?:[\u65e5T\s](\d{1,2}:\d{2}(?::\d{2})?))?",
        s
    )
    if not m:
        return None
    y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    date_part = f"{y}-{mo:02d}-{d:02d}"
    time_part = m.group(4) or ""
    if col_type == "datetime":
        if not time_part:
            time_part = "00:00:00"
        elif time_part.count(":") == 1:
            time_part = time_part + ":00"
        return f"{date_part} {time_part}"
    return date_part


def insert_row(row: dict) -> int:
    """把一条 case dict 写入 jd_rb_case,返回新行的 id。

    - 只写白名单字段(防止 row 里多塞脏数据进表)
    - 日期字段(publish_time)按 MySQL 类型规范化后再写
    - **use_time 字段永不入库**(用户要求,即使 row 传了也忽略,保持 NULL)
    - 缺值/解析失败 自动跳过该字段(不写 NULL 入表,符合表 DEFAULT NULL 设计)
    """
    if not row:
        raise MySQLError("row 为空")
    # use_time 已剔除(用户要求保持 NULL,不入库)
    allowed = [
        "publish_time", "author", "contentAddressNew", "title",
        "source", "url", "url2", "summary", "info_type",
    ]
    cols = []
    values = []
    for c in allowed:
        v = row.get(c)
        # use_time 永远不入库:用户要求保持 NULL,即使 row 里传了值也跳过
        if c == "use_time":
            continue
        if not v:
            continue
        if c in DATE_FIELDS:
            v = _normalize_date(v, DATE_FIELDS[c])
            if v is None:
                continue
        cols.append(c)
        values.append(v)
    if not cols:
        raise MySQLError(f"row 没有可写字段: keys={list(row.keys())}")
    placeholders = ", ".join(f"`{c}` = %s" for c in cols)
    try:
        with mysql_conn() as conn:
            with conn.cursor() as cur:
                sql = f"INSERT INTO `{config.MYSQL_TABLE}` SET {placeholders}"
                cur.execute(sql, values)
                conn.commit()
                return cur.lastrowid
    except MySQLError:
        raise
    except Exception as e:
        raise MySQLError(f"insert_row 失败: {e}") from e


def health_check() -> bool:
    """连通性探活,失败不抛异常"""
    try:
        with mysql_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                return cur.fetchone() is not None
    except Exception as e:
        log.warning(f"MySQL 健康检查失败: {e}")
        return False