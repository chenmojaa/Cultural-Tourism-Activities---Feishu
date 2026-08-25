# -*- coding: utf-8 -*-
"""写入飞书前的查重去重:
  1) URL 精确匹配(查 jd_rb_case.url 和 url2)
  2) summary 语义相似度(调本地 bge-m3 的 /v1/score 接口,逐对打分)

任一命中即视为重复,跳过。
"""
import logging
from typing import List, Optional, Tuple

import requests

import config
from app.utils import mysqlapi as mysql_client
# modeltest.py 是本地 bge-m3 的手工测试桩,保留备用;这里直接打 /v1/score 接口


log = logging.getLogger("wenglu")


def _score_summaries(new_summary: str, existing: List[str], timeout: float = 8.0) -> List[float]:
    """调本地 bge-m3 的 /v1/score,一次返回所有 (new vs existing[i]) 的相似度分数。

    返回 list[float],长度 == len(existing),顺序与 existing 一致。
    出错返回空 list。
    """
    if not new_summary or not existing:
        return []
    payload = {
        "model": config.LOCAL_MODEL_NAME,
        "text_1": [new_summary] * len(existing),
        "text_2": list(existing),
    }
    try:
        r = requests.post(
            config.LOCAL_SCORE_URL,
            headers={
                "Authorization": f"Bearer {config.LOCAL_MODEL_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
        scores = [item.get("score", 0.0) for item in (data.get("data") or [])]
        return scores
    except Exception as e:
        log.warning(f"本地 bge-m3 打分失败: {e}")
        return []


def is_duplicate_by_url(url: str) -> bool:
    """URL 精确匹配。命中返回 True。"""
    if not url:
        return False
    rec = mysql_client.find_by_url(url)
    return rec is not None


def is_duplicate_by_summary(
    new_summary: str,
    existing_summaries: Optional[List[str]] = None,
) -> Tuple[bool, float, int]:
    """summary 语义去重。

    返回 (is_dup, max_score, hit_index)。
      - is_dup=True: 命中相似度阈值
      - max_score: 与所有已有 summary 的最大分
      - hit_index: 触发命中的索引(无命中为 -1)
    """
    if not new_summary:
        return False, 0.0, -1
    if existing_summaries is None:
        existing_summaries = mysql_client.recent_summaries(config.DEDUP_LOOKBACK_DAYS)
    if not existing_summaries:
        return False, 0.0, -1
    scores = _score_summaries(new_summary, existing_summaries)
    if not scores:
        return False, 0.0, -1
    max_score = max(scores)
    if max_score >= config.DEDUP_SUMMARY_THRESHOLD:
        hit_index = scores.index(max_score)
        return True, max_score, hit_index
    return False, max_score, -1


def check_duplicate(
    url: str,
    summary: str,
) -> Tuple[bool, str]:
    """对外唯一入口。命中返回 (True, reason),未命中返回 (False, "")。

    优先级:URL 命中 > summary 语义命中。
    MySQL 不可达 / 本地模型不可达 时按"未命中"处理(优雅降级,不阻塞主流程)。
    """
    # 1) URL 精确匹配
    if url and is_duplicate_by_url(url):
        return True, f"URL 命中 jd_rb_case: {url[:80]}"
    # 2) summary 语义相似度
    is_dup, score, idx = is_duplicate_by_summary(summary)
    if is_dup:
        return True, f"summary 相似度过高 score={score:.3f} (idx={idx}, threshold={config.DEDUP_SUMMARY_THRESHOLD})"
    return False, ""
