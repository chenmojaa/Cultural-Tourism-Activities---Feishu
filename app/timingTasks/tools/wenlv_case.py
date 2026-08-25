# -*- coding: utf-8 -*-
"""主流程:LLM 推荐 -> summary (LLM 联网搜索带 URL) -> 写入飞书多维表格

用法:
    python main.py            # 正常运行
    python main.py --check    # 只检查飞书凭证和现有记录
"""
import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta
from typing import List
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
from app.utils.prompts import SYSTEM_PROMPT, RECOMMEND_PROMPT, SUMMARY_PROMPT
from app.utils.llmapi import LLMClient
from app.utils.data_store import DataStore
from app.utils import dedup
from app.utils import mysqlapi as mysql_client
from app.utils.tools import (
    setup_logger,
    extract_all_reference_urls,
    parse_json_array,
    strip_citation_marks,
    clean_search_title,
    validate_url,
)

log = setup_logger()



# 占位/示例域名(LLM 凭印象编的假 URL,绝对过滤掉)
_PLACEHOLDER_DOMAINS = (
    "example.com", "example.org", "example.net",
    "test.com", "localhost", "127.0.0.1", "0.0.0.0",
    "abc.com", "placeholder.com", "foo.com", "bar.com",
    "yourdomain.com", "yoursite.com", "domain.com",
    "sample.com", "demo.com", "dummy.com",
)



def _is_placeholder_url(u):
    """检测 URL 是否指向占位/示例域名。"""
    if not u:
        return False
    u_low = u.lower()
    for dom in _PLACEHOLDER_DOMAINS:
        if (dom + "/" in u_low) or u_low.endswith(dom) or ("/" + dom + "/") in u_low:
            return True
    return False



# 黑名单:百科类 / 攻略聚合 / 纯介绍页(用户要求不入飞书)
# 逻辑同 _is_placeholder_url:URL host 或路径命中关键词就丢弃
_BLOCKED_NEWS_DOMAINS = (
    # 百科类
    "baike.baidu.com",
    "baike.sogou.com",
    "baike.so.com",
    "baike.baike.com",
    "baike.toutiao.com",
    "baike.com",
    "wikipedia.org",
    "zh.wikipedia.org",
    "en.wikipedia.org",
    "baike.moegirl.org",
    "baike.kddlife.com",
    # 攻略聚合 / 景点介绍(非新闻报道)
    "www.xiaohongshu.com",
    "www.douyin.com",
    "www.zhihu.com",
    "uke.zhihu.com",
    "www.mafengwo.cn",
    "www.qyer.com",
    "www.16fan.com",
    "www.dujiyou.com",
    "www.dianping.com",
    "www.ctrip.com",
    "www.meituan.com",
)

_BLOCKED_URL_PATH_KEYWORDS = (
    "/item/",        # 百科条目路径特征
    "/qiekoujian/",  # 抖音百科条目路径
    "/baike/",       # 通用百科路径
)

def _is_blocked_news_url(u):
    """检测 URL 是否指向百科/攻略聚合/纯介绍页(非新闻报道)。命中就丢弃。

    返回 True 即丢弃。
    """
    if not u:
        return False
    u_low = u.lower()
    for dom in _BLOCKED_NEWS_DOMAINS:
        if (dom + "/" in u_low) or u_low.endswith(dom) or ("/" + dom + "/") in u_low:
            return True
    for kw in _BLOCKED_URL_PATH_KEYWORDS:
        if kw in u_low:
            return True
    return False

# ============================================================
# 业务规则:案例后过滤
# ============================================================
def _extract_number(s: str):
    """从 '120万' / '5.6w' / '80000' / '1,200' 等字符串中提取数值"""
    if not s:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*(万|w|W)?", str(s).replace(",", ""))
    if not m:
        return None
    n = float(m.group(1))
    if m.group(2):
        n *= 10000
    return n


# 文旅关键词(主体是酒店/纯招商/纯楼盘推广就视为非文旅)
_NON_TOURISM_KEYWORDS = (
    "酒店集群", "酒店开业", "酒店开业仪式", "度假酒店", "酒店项目",
    "酒店落成", "酒店签约", "酒店管理", "酒店集团",
)

_NON_TOURISM_PATH_KEYWORDS = (
    "本条为预进行", "本条为非文旅", "本条非文旅",
)


def _is_invalid_summary(s):
    """判断 summary 是否属于应当丢弃的预进行/非文旅/酒店广告类内容。

    返回 (is_invalid, reason)。
    """
    s = (s or "").strip()
    if not s:
        return False, ""
    head = s[:400]
    # ① LLM 在 summary 开头主动标记为预进行/非文旅
    for kw in _NON_TOURISM_PATH_KEYWORDS:
        if head.startswith(kw) or ("[" + kw) in head[:120]:
            return True, "预进行/非文旅(" + kw + ")"
    # ② 主体是酒店广告(关键词出现在 summary 开头 400 字内)
    for kw in _NON_TOURISM_KEYWORDS:
        if kw in head:
            return True, "酒店广告(" + kw + ")"
    return False, ""


def _is_future_event_date(d, today=None):
    """判断 event_date 是否在未来(开张日 > 今天)。返回 (is_future, parsed_date or None)。"""
    d = (d or "").strip()
    if not d:
        return False, None
    parsed = None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日"):
        try:
            parsed = datetime.strptime(d[:10], fmt)
            break
        except Exception:
            continue
    if parsed is None:
        return False, None
    today = today or datetime.now()
    if parsed.date() > today.date():
        return True, parsed
    return False, parsed


def post_filter(case: dict) -> tuple:
    """极简过滤:只校验 name 和 url。其他字段一律放行。"""
    name = (case.get("name") or "").strip()
    url = (case.get("article_url") or case.get("url") or "").strip()
    if not name:
        return False, "缺名称"
    if url and not url.startswith(("http://", "https://")):
        return False, f"URL 无效: {url}"
    open_date_str = str(case.get("open_date") or "").strip()
    if open_date_str:
        parsed = None
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日"):
            try:
                parsed = datetime.strptime(open_date_str[:10], fmt)
                break
            except Exception:
                continue
        if parsed and parsed > datetime.now() - timedelta(days=config.NEW_OPEN_DAYS):
            return False, f"开张时间过新 ({open_date_str} < {config.NEW_OPEN_DAYS} 天)"
    reception_num = _extract_number(case.get("reception"))
    if reception_num is not None and reception_num < config.MIN_RECEPTION:
        return False, f"接待人数过低: {case.get('reception')}"
    rn = case.get("recent_news")
    if isinstance(rn, dict) and rn.get("date"):
        rn_date_str = str(rn["date"]).strip()
        rn_parsed = None
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日"):
            try:
                rn_parsed = datetime.strptime(rn_date_str[:10], fmt)
                break
            except Exception:
                continue
        if rn_parsed is None:
            return False, f"recent_news.date 解析失败: {rn_date_str}"
        days_ago = (datetime.now() - rn_parsed).days
        if days_ago > config.RECENT_DAYS:
            return False, f"recent_news.date {rn_date_str} 已超出近 {config.RECENT_DAYS} 天({days_ago} 天前)"
        if days_ago < 0:
            return False, f"recent_news.date {rn_date_str} 在未来"
    return True, ""


def recommend_cases(llm: LLMClient, existing: List[dict], batch: int) -> List[dict]:
    existing_text = "\n".join(
        f"- {e['name']} | {e['url']}" for e in existing
    ) or "(无)"
    today_dt = datetime.now()
    window_start_dt = today_dt - timedelta(days=config.RECENT_DAYS)
    near_open_cutoff = (today_dt - timedelta(days=config.NEW_OPEN_DAYS)).strftime("%Y-%m-%d")
    prompt = RECOMMEND_PROMPT.format(
        batch=batch,
        existing_cases=existing_text,
        today=today_dt.strftime("%Y-%m-%d"),
        window_start=window_start_dt.strftime("%Y-%m-%d"),
        window_end=today_dt.strftime("%Y-%m-%d"),
        near_open_cutoff=near_open_cutoff,
        example_date=(today_dt - timedelta(days=7)).strftime("%Y-%m-%d"),
    )
    raw = llm.chat(
        SYSTEM_PROMPT, prompt,
        temperature=0.7,
        max_tokens=config.RECOMMEND_MAX_TOKENS,
        enable_search=True,
    )
    log.info(f"=== LLM 步骤1 原始返回(前 1500 字) ===\n{raw[:1500]}\n=== END ===")
    data = parse_json_array(raw)
    if not data:
        log.warning("LLM 返回无法解析为 JSON 数组(原始前 500 字):")
        log.warning(raw[:500])
    log.info(f"JSON 解析成功,共 {len(data)} 个候选")
    return data


def step1_recommend(
    llm: LLMClient,
    store: DataStore,
    target_count: int = None,
    existing: List[dict] = None,
) -> List[dict]:
    """让 LLM 推荐文旅案例直到拿到 target_count 条(过 post_filter 后)。

    target_count: 本次要多少条;不传就用 config.TARGET_COUNT
    existing:     已在本次 run 里采纳过的案例[{name, url}, ...],会喂给 prompt 让 LLM 避开重复
    """
    target = target_count if target_count is not None else config.TARGET_COUNT
    log.info("=" * 60)
    log.info("步骤 1:开始请求大模型推荐案例")
    collected: List[dict] = []
    existing = existing or []

    for round_i in range(1, config.MAX_BATCH_ROUNDS + 1):
        need = target - len(collected)
        if need <= 0:
            break
        log.info(f"--- 第 {round_i}/{config.MAX_BATCH_ROUNDS} 轮,还需 {need} 条 ---")
        # 智能 retry:LLM 偷懒不调联网时(快速返出且 name 全空),强制重调一次
        candidates = []
        for attempt in range(1, 3):  # 最多 2 次
            try:
                candidates = recommend_cases(llm, existing, config.REQ_BATCH)
            except Exception as e:
                log.exception(f"第 {round_i} 轮请求失败: {e}")
                break
            # 检测:有效 case 太少时重调(同时覆盖 LLM 偷懒不调 web_search 的情况)
            valid_count = sum(1 for c in candidates if (c.get("name") or "").strip())
            if valid_count < config.REQ_BATCH // 2 and attempt < 2:
                log.warning(
                    f"第 {round_i} 轮第 {attempt} 次只有 {valid_count}/{config.REQ_BATCH} 条有效 case,重调"
                )
                continue
            break
        log.info(f"LLM 本轮返回 {len(candidates)} 条候选")

        for case in candidates:
            # 家有字段兼容:LLM 偷子用 title/case_name/活动名称 时,也能被识别
            if not case.get("name"):
                case["name"] = (case.get("title") or case.get("case_name") or case.get("活动名称") or "").strip()
            if not case.get("article_url"):
                case["article_url"] = (case.get("url") or case.get("link") or "").strip()
            ok, reason = post_filter(case)
            if not ok:
                log.info(f"过滤(规则):{case.get('name')!r} -- {reason}")
                continue
            collected.append(case)
            # 把刚采纳的也并入 existing,后面轮次让 LLM 别再推荐同一条
            existing.append({"name": case.get("name", ""), "url": case.get("article_url", "")})
            log.info(f"采纳 ({len(collected)}/{target}): {case.get('name')}")
            if len(collected) >= target:
                break
        time.sleep(1)

    log.info(f"步骤 1 完成,共采纳 {len(collected)} 个案例(目标 {target})")
    return collected


# ============================================================
# 步骤 2: LLM 联网搜索 -> 300 字总结 + 真实 URL
# ============================================================
def step2_summary(llm: LLMClient, case: dict) -> dict:
    """让 LLM 联网搜索后写 300 字 summary。URL 优先用 LLM 调用返回的 search_info.search_results[0] (来自搜索引擎,真实可靠)。"""
    name = case["name"]
    log.info(f"步骤 2:为 [{name}] 联网搜索 + 写 300 字总结")
    prompt = SUMMARY_PROMPT.format(
        case_name=name,
        today=datetime.now().strftime('%Y-%m-%d'),
    )
    try:
        raw = llm.chat(
            SYSTEM_PROMPT, prompt,
            temperature=0.6,
            max_tokens=config.SUMMARY_MAX_TOKENS,
        )
    except Exception as e:
        log.exception(f"summary 失败: {name}, {e}")
        return {}

    # 1) 搜索引擎直接给的真实结果(含 url 和 site_name)
    search_results = llm.get_last_search_results()
    search_url = (search_results[0].get("url") or "").strip() if search_results else ""
    # site_name 是搜索引擎标注的发布方(例: "上观新闻"),比 URL 域名准得多
    search_site = (search_results[0].get("site_name") or "").strip() if search_results else ""


    def _extract_url_from_field(v):
        """从 LLM 填的 article_url 字段里拆出纯 URL:
        - 纯 URL: 直接返回
        - markdown [title](URL) / 【title】(URL): 拆出 URL
        - 混合文本含 URL: 提取第一个 http(s):// 字符串
        - 命中占位域名(example.com / localhost 等)→ 返回空,让上层丢弃
        """
        v = (v or "").strip()
        if not v:
            return ""
        m_url = ""
        if v.startswith(("http://", "https://")):
            m_url = v
        else:
            m = re.search(r"\]\((https?://[^\)]+)\)|【[^】]+】\((https?://[^\)]+)\)", v)
            if m:
                m_url = m.group(1) or m.group(2) or ""
            if not m_url:
                m2 = re.search(r"https?://[^\s\)【】]+", v)
                m_url = (m2.group(0) if m2 else "").rstrip("。,;.")
        if not m_url:
            return ""
        if _is_placeholder_url(m_url):
            return ""
        return m_url
    data = parse_json_array(raw)
    if data and isinstance(data[0], dict):
        obj = data[0]
        llm_publisher = (obj.get("publisher") or "").strip()
        llm_url = _extract_url_from_field(obj.get("article_url"))
        return {
            "summary": strip_citation_marks((obj.get("summary") or "").strip()),
            # URL 优先级:搜索引擎真实结果 > LLM 给的 article_url(拆出 URL) > 文本兑底
            "article_url": search_url or llm_url,
            # publisher 优先级:LLM 给的(最准) > 搜索引擎 site_name (备选) > 空
            "publisher": llm_publisher or search_site,
            "article_date": (obj.get("article_date") or "").strip(),
            "event_date": (obj.get("event_date") or "").strip(),
        }
    log.warning(f"[{name}] LLM 未返回 JSON,兜底从纯文本提 URL")
    urls = extract_all_reference_urls(raw)
    return {
        "summary": strip_citation_marks(raw.strip()),
        "article_url": search_url or (urls[0][1] if urls else "").strip(),
        "publisher": search_site,
        "article_date": "",
        "event_date": "",
    }


# ============================================================
# 行构造工具
# ============================================================
# 省级行政区短名(直接对应输出,例如 "山东省" / "内蒙古自治区" -> "山东" / "内蒙古")
_PROVINCE_SHORT = (
    "河北", "山西", "辽宁", "吉林", "黑龙江", "江苏", "浙江", "安徽", "福建", "江西",
    "山东", "河南", "湖北", "湖南", "广东", "海南", "四川", "贵州", "云南", "陕西",
    "甘肃", "青海", "内蒙古", "广西", "西藏", "宁夏", "新疆",
)


def _extract_province(location: str) -> str:
    """从 LLM 给的 location 中提取省份名(不带省/市/自治区等后缀,直辖市也不带市)。

    支持的输入示例:
      - "北京" / "北京市" / "北京市朝阳区"                 -> "北京"
      - "上海" / "上海市" / "上海市浦东新区"               -> "上海"
      - "天津" / "天津市"                                 -> "天津"
      - "重庆" / "重庆市" / "重庆"                         -> "重庆"
      - "山东省" / "山东省济南市" / "济南"                 -> "山东"
      - "浙江省杭州市"                                    -> "浙江"
      - "内蒙古" / "内蒙古自治区" / "内蒙古呼和浩特市"      -> "内蒙古"
      - "新疆" / "新疆维吾尔自治区" / "新疆乌鲁木齐市"      -> "新疆"
      - "西藏" / "西藏自治区" / "西藏拉萨市"               -> "西藏"
      - "广西" / "广西壮族自治区" / "广西桂林市"           -> "广西"
      - "宁夏" / "宁夏回族自治区"                          -> "宁夏"
      - "" / None                                         -> ""
    """
    if not location:
        return ""
    s = str(location).strip()
    if not s:
        return ""
    # 直辖市:北京/上海/天津/重庆(整名就是省份,不挂市)
    for m_prov in ("北京", "上海", "天津", "重庆"):
        if s.startswith(m_prov):
            return m_prov
    # 1) 精确短省名匹配
    for prov in _PROVINCE_SHORT:
        if s.startswith(prov):
            return prov
    # 2) LLM 没给省份(只给了城市),无法映射,原样返回
    return s


def _extract_url_date(url: str) -> str:
    """从 URL 路径中抽取 /YYYY/MM/DD/ 或 /YYYY-MM-DD/ 日期,失败返回空字符串。"""
    if not url:
        return ""
    m = re.search(r"/(20\d{2})[-/](\d{1,2})[-/](\d{1,2})/", url)
    if not m:
        return ""
    y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return ""
    return f"{y}-{mo:02d}-{d:02d}"


def _detect_url_type(url: str) -> str:
    """根据 URL 判断媒体类型:微信公众号 -> 微信,其他 -> 网页。

    source 字段只表示渠道(网页/微信/微博 等),不表示发布方名。发布方放 author。
    """
    if not url:
        return "网页"
    u = url.lower()
    if "weixin.qq.com" in u or "mp.weixin.qq.com" in u:
        return "微信"
    return "网页"


def _build_row(case: dict, summary: str, ref_url: str, meta: dict) -> dict:
    """按 Bitable 9 字段 schema 构造一行。
    source = 渠道(微信/网页),author = 发布方名(LLM 给或搜索引擎 site_name)。
    """
    article_url = (
        meta.get("article_url")
        or (case.get("article_url") or "").strip()
        or ref_url
        or case.get("url", "")
    )
    publisher = (case.get("publisher") or "").strip()
    return {
        "title": (case.get("name") or "").strip(),
        "url": article_url,
        "summary": (summary or "").strip(),
        "contentAddressNew": _extract_province(case.get("location", "")),
        "source": _detect_url_type(article_url),
        "author": publisher,
        "publish_time": (
            (case.get("recent_news") or {}).get("date", "")
            or (case.get("article_date") or "").strip()
            or _extract_url_date(article_url)
        ),
        "info_type": "典型案例",
    }


# ============================================================
# CLI 模式
# ============================================================
def cmd_check(store: DataStore):
    log.info("[check] 验证飞书凭证与现有数据")
    records = store.load_existing_records()
    log.info(f"[check] OK,飞书侧共 {len(records)} 条历史记录")
    for rec in records[:3]:
        fields = rec.get("fields", {})
        name = fields.get(config.FEISHU_FIELD_MAPPING["title"], "?")
        log.info(f"  - {name}")




# ============================================================
# 步骤 3 (简化):直接信任 LLM 联网搜索返回的 URL
# ============================================================
def _process_one_case(llm: LLMClient, case: dict, idx: int, total: int):
    """单个案例:LLM 联网搜索 + 返回 {summary, article_url, publisher, article_date} → 写入飞书。"""
    name = case.get("name", "?")
    log.info(f"--- 处理第 {idx}/{total} 个案例:[{name}] ---")

    res = step2_summary(llm, case)
    summary = res.get("summary", "").strip()
    if not summary:
        log.warning(f"案例 [{name}] summary 为空,丢弃")
        return None

    # 新规则 ①+②:summary 开头标记预进行/非文旅,或主体是酒店广告,直接丢弃
    invalid, reason = _is_invalid_summary(summary)
    if invalid:
        log.warning(f"案例 [{name}] {reason},丢弃")
        return None

    # 新规则 ③:活动实际开张日期在未来,丢弃(LLM 步骤 2 输出的 event_date 优先,否则用 case.open_date)
    event_date_str = res.get("event_date", "") or case.get("open_date", "") or case.get("event_date", "")
    is_future, _ = _is_future_event_date(event_date_str)
    if is_future:
        log.warning(f"案例 [{name}] 活动开张日 {event_date_str} 在未来,丢弃")
        return None

    # URL 兜底顺序(前面任一拿到就停):
    # URL 只取 LLM 联网搜索的真结果(对应响应中 [1][2] 角标),不采纳 step1 推荐时
    # LLM 自己编造的 article_url — 那种是 LLM 凭训练数据编的,典型 404。
    ref_url = res.get("article_url", "")
    src_tag = "search_results"
    if not ref_url:
        cands = extract_all_reference_urls(summary)
        if cands:
            ref_url = cands[0][1]
            src_tag = "summary_text"
    if not ref_url:
        # LLM 没调联网,summary 也没 URL → 严格丢弃,不让假 URL 落库
        log.debug(
            f"案例 [{name}] 无可用 URL: "
            f"search_results={len(llm.get_last_search_results())}, "
            f"summary_含 URL={bool(extract_all_reference_urls(summary))}"
        )
        log.warning(f"案例 [{name}] 无可用 URL(LLM 未触发联网/无角标),丢弃")
        return None
    # 黑名单兜底:百科/攻略聚合/纯介绍页(用户要求不入飞书)
    if _is_blocked_news_url(ref_url):
        log.warning(f'案例 [{name}] URL 命中百科/攻略黑名单,丢弃: {ref_url[:80]}')
        return None
    # URL 实测:过滤 LLM 编造的假 URL(典型: gz.gov.cn/.../2026/.../*.html 404)
    if config.VALIDATE_URLS:
        ok, info = validate_url(ref_url, timeout=config.URL_VALIDATE_TIMEOUT)
        if not ok:
            log.warning(f"案例 [{name}] URL 不可达({info}),丢弃: {ref_url[:80]}")
            return None
        log.debug(f"案例 [{name}] URL 验证通过({info}): {ref_url[:80]}")
    log.info(f"案例 [{name}] 采纳 URL (src={src_tag}): {ref_url[:100]}")

    case = dict(case)
    if res.get("publisher") and not case.get("publisher"):
        case["publisher"] = res["publisher"]
    # publish_time 优先级:_build_row 里 recent_news.date > article_date > URL 日期
    # step2 的 article_date 必须无条件覆盖 step1 的 article_date(LLM 经常在 step1 返旧日期),
    # 否则飞书里会写错的活动时间。同时把 recent_news.date 同步,保证 _build_row 两种字段都能读到。
    if res.get("article_date"):
        case["article_date"] = res["article_date"]
        case.setdefault("recent_news", {})["date"] = res["article_date"]
    # 用搜索引擎返回的真实标题覆盖 LLM 生成的"案例名称",确保 title 跟活动发布的标题一致
    search_results = llm.get_last_search_results()
    if search_results:
        real_title = clean_search_title(search_results[0].get("title", ""))
        if real_title:
            case["name"] = real_title

    meta = {
        "article_title": "",
        "article_url": ref_url,
        "article_publisher": res.get("publisher", ""),
        "article_author": res.get("publisher", ""),
        "valid": True,
        "failure_reason": "",
        "_url_source": "llm_search",
    }
    row = _build_row(case, summary, ref_url, meta)
    if not row.get("url"):
        log.warning(f"案例 [{name}] _build_row 未填入 url,放弃写入")
        return None
    log.info(f"案例 [{name}] 通过,准备写入飞书 (publisher={res.get('publisher', '')!r})")
    return row


def _process_cases_batch(llm: LLMClient, cases: list, batch_idx: int) -> list:
    """并发跑一批 case(每个 _process_one_case),返回非 None 的 row 列表。

    batch_idx: 用于日志,标记是哪一轮重试的批。
    """
    if not cases:
        return []
    rows = [None] * len(cases)
    max_workers = min(getattr(config, 'PARALLEL_WORKERS', 5), len(cases))
    log.info(f'第 {batch_idx} 批:并发处理 {len(cases)} 个案例,max_workers={max_workers}')
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_idx = {
            pool.submit(_process_one_case, llm, case, i + 1, len(cases)): i
            for i, case in enumerate(cases)
        }
        for fut in as_completed(future_to_idx):
            i = future_to_idx[fut]
            try:
                rows[i] = fut.result()
            except Exception as e:
                log.exception(f'第 {batch_idx} 批 案例 [#{i+1}] 处理失败: {e}')
                rows[i] = None
    rows = [r for r in rows if r]
    dropped = len(cases) - len(rows)
    log.info(f'第 {batch_idx} 批:产出 {len(rows)} 条有效行(丢弃 {dropped} 条)')
    return rows


def _dedup_against_mysql(rows: list) -> list:
    """批量查重:一次拉最近 summary 缓存,逐行用 URL + summary 相似度比对。

    MySQL 或本地 bge-m3 不可达时,优雅降级返回原 rows,不阻塞主流程。
    """
    if not rows:
        return rows
    log.info(f'查重:对 {len(rows)} 条候选与 jd_rb_case 校对...')
    # 探测 MySQL,失败直接放过
    if not mysql_client.health_check():
        log.warning('MySQL 不可达,跳过查重,所有候选保留')
        return rows
    # 一次性拉所有候选涉及的 URL 命中情况(并行:对每条 row 查一次;小批量可以串行)
    existing_summaries = mysql_client.recent_summaries(config.DEDUP_LOOKBACK_DAYS)
    kept: list = []
    for row in rows:
        url = (row.get('url') or '').strip()
        summary = (row.get('summary') or '').strip()
        title = (row.get('title') or '').strip()
        try:
            is_dup, reason = dedup.check_duplicate(url, summary)
        except Exception as e:
            log.warning(f'查重调用异常,放过该条 [{title}]: {e}')
            is_dup, reason = False, ''
        if is_dup:
            log.info(f'查重命中,丢弃 [{title}]: {reason}')
        else:
            kept.append(row)
    dropped = len(rows) - len(kept)
    log.info(f'查重完成:保留 {len(kept)} 条,丢弃 {dropped} 条')
    return kept


def run():
    parser = argparse.ArgumentParser(description='文旅案例自动化采集')
    parser.add_argument('--check', action='store_true', help='只检查飞书凭证与现有数据')
    args = parser.parse_args()

    if args.check:
        store = DataStore()
        cmd_check(store)
        return

    llm = LLMClient()
    store = DataStore()

    # ---- 正常流程:外层循环,dedup 后条数不够就补生成 ----
    rows: list = []
    max_rounds = getattr(config, 'MAX_RETRY_ROUNDS', 3)
    # step1 用的 existing(本 run 已采纳的案例名/URL,让 LLM 别重复推荐)
    in_run_existing: list = []

    for round_i in range(1, max_rounds + 1):
        need = config.TARGET_COUNT - len(rows)
        if need <= 0:
            break
        log.info(f"=== 重试轮 {round_i}/{max_rounds}:已有 {len(rows)} 条,目标 {config.TARGET_COUNT},还需 {need} 条 ===")

        # 1) 让 LLM 推荐 need 条(扣除本 run 已采纳的)
        cases = step1_recommend(llm, store, target_count=need, existing=in_run_existing)
        if not cases:
            log.warning(f"第 {round_i} 轮 LLM 没采纳到任何案例,跳出循环")
            break

        # 2) 并发跑 step2(联网搜索 + 写 summary + URL 验证)
        new_rows = _process_cases_batch(llm, cases, round_i)

        # 3) 与 jd_rb_case 查重(URL 精准 + summary 语义)
        if config.MYSQL_DEDUP_ENABLED:
            new_rows = _dedup_against_mysql(new_rows)

        # 把新行加入累计,并把它们的标题/URL 也喂给后续轮次,避免 LLM 重复推荐
        for r in new_rows:
            in_run_existing.append({"name": r.get("title", ""), "url": r.get("url", "")})

        rows.extend(new_rows)
        log.info(f"第 {round_i} 轮:采纳 {len(cases)} 条 -> 落地 {len(new_rows)} 条 -> 累计 {len(rows)}/{config.TARGET_COUNT}")

        # 兜底:这一轮零产出,LLM 大概率在重复推荐,再补也白搭
        if not new_rows:
            log.warning(f"第 {round_i} 轮零产出,停止补生成")
            break

    if not rows:
        log.warning('没有可写入的行,流程结束')
        return

    log.info(f'准备写入飞书 {len(rows)} 条新案例')
    try:
        store.add_rows(rows)
    except Exception as e:
        log.exception(f'飞书写入失败: {e}')
    log.info('流程完成')


def collect_to_rows() -> list:
    """跑完 step1+step2+dedup,返回行列表(不写飞书/MySQL)。

    webhook_server 调用此函数拿到 rows,然后自己选写哪里(默认走 MySQL)。
    """
    llm = LLMClient()
    rows: list = []
    max_rounds = getattr(config, 'MAX_RETRY_ROUNDS', 3)
    in_run_existing: list = []

    for round_i in range(1, max_rounds + 1):
        need = config.TARGET_COUNT - len(rows)
        if need <= 0:
            break
        log.info(f"=== 重试轮 {round_i}/{max_rounds}:已有 {len(rows)} 条,目标 {config.TARGET_COUNT},还需 {need} 条 ===")

        cases = step1_recommend(llm, None, target_count=need, existing=in_run_existing)
        if not cases:
            log.warning(f"第 {round_i} 轮 LLM 没采纳到任何案例,跳出循环")
            break

        new_rows = _process_cases_batch(llm, cases, round_i)

        if config.MYSQL_DEDUP_ENABLED:
            new_rows = _dedup_against_mysql(new_rows)

        for r in new_rows:
            in_run_existing.append({"name": r.get("title", ""), "url": r.get("url", "")})
        rows.extend(new_rows)
        log.info(f"第 {round_i} 轮:采纳 {len(cases)} 条 -> 落地 {len(new_rows)} 条 -> 累计 {len(rows)}/{config.TARGET_COUNT}")

        if not new_rows:
            log.warning(f"第 {round_i} 轮零产出,停止补生成")
            break
    return rows


def run_to_mysql():
    """webhook 入口:采集案例并写入 jd_rb_case(不写飞书)。"""
    log.info("[webhook] 收到触发,开始采集 + 写 MySQL")
    rows = collect_to_rows()
    if not rows:
        log.warning("[webhook] 没有可写入的行,结束")
        return {"collected": 0, "inserted": 0}
    inserted = 0
    for r in rows:
        try:
            new_id = mysql_client.insert_row(r)
            inserted += 1
            log.info(f"[webhook] 写入 MySQL id={new_id} title={r.get('title', '')[:30]!r}")
        except Exception as e:
            log.exception(f"[webhook] 写入 MySQL 失败: {r.get('title', '')}: {e}")
    log.info(f"[webhook] 完成:采集 {len(rows)} 条,写入 {inserted} 条")
    return {"collected": len(rows), "inserted": inserted}


def run_to_feishu():
    """webhook 入口:跑主流程,把案例写入飞书 Bitable(不写 MySQL)。"""
    log.info("[webhook feishu] 收到触发,开始采集 + 写飞书")
    rows = collect_to_rows()
    if not rows:
        log.warning("[webhook feishu] 没有可写入的行,结束")
        return {"collected": 0, "added": 0}
    store = DataStore()
    try:
        added = store.add_rows(rows)
    except Exception as e:
        log.exception(f"[webhook feishu] 飞书写入失败: {e}")
        return {"collected": len(rows), "added": 0, "error": str(e)}
    log.info(f"[webhook feishu] 完成:采集 {len(rows)} 条,写入 {len(added)} 条")
    return {"collected": len(rows), "added": len(added)}



def wenlv_to_mysql_main():
    """定时任务入口:采集案例并写入 MySQL。"""
    return run_to_mysql()


def wenlv_to_feishu_main():
    """定时任务入口:采集案例并写入飞书多维表格。"""
    return run_to_feishu()


def _safe_run():
    try:
        run()
    except RuntimeError as e:
        log.error("[FATAL] " + str(e))
        sys.exit(1)
    except KeyboardInterrupt:
        log.warning("[!] 用户中断")
        sys.exit(130)
    except Exception as e:
        log.exception("[FATAL] 未捕获异常: " + str(e))
        sys.exit(1)


if __name__ == "__main__":
    _safe_run()
