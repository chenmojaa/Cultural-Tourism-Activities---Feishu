# -*- coding: utf-8 -*-
"工具函数:日志、重试、URL 解析、容错 JSON 解析"
import re
import time
import logging
import functools
import json

import requests

# LOG_PATH \u5df2\u4e0d\u518d\u4f7f\u7528(\u65e5\u5fd7\u53ea\u8f93\u51fa\u63a7\u5236\u53f0)


def setup_logger(name="wenglu"):
    """\u53ea\u8f93\u51fa\u5230\u63a7\u5236\u53f0(\u4e0d\u518d\u5199\u65e5\u5fd7\u6587\u4ef6)。"""
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO); ch.setFormatter(fmt)
    logger.addHandler(ch)
    return logger


def retry(max_attempts=3, delay=2.0, backoff=1.5):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last = None
            for i in range(max_attempts):
                try:
                    return fn(*args, **kwargs)
                except Exception as e:
                    last = e
                    wait = delay * (backoff ** i)
                    logging.warning("retry" + ": " + str(e) + ", wait " + str(int(wait)) + "s")
                    time.sleep(wait)
            raise last
        return wrapper
    return deco


def extract_all_reference_urls(text):
    """从 LLM 给的总结文本里尽量多抓 URL,按 (来源描述, URL) 顺序返回。

    抓取顺序(命中即停止覆盖):
      1) markdown 形 [标题](URL)
      2) ‘参考资料:’ 段落,每一行抓 URL
      3) 编号 1/2/3. 标题 - URL 行
      4) 任何 https?:// 字符串(去重保序)
    返回 list[(label, url)],label 用来定位它是从哪段摘的(便于排查)。
    """
    if not text: return []
    out = []
    seen = set()

    def add(label, url):
        u = (url or "").strip().rstrip("。,;.")
        if not u.startswith(("http://", "https://")): return
        if u in seen: return
        # 公众号短链 '/s/<id>' 太短,常被误吞后面的标点,处理
        if "mm.weixin.qq.com/s/" in u:
            pass  # 保留
        seen.add(u)
        out.append((label, u))

    # 1) markdown links
    for m in re.finditer(r"\[([^\]]+)\]\((https?://[^)]+)\)", text):
        add("md_link:" + m.group(1).strip()[:30], m.group(2))

    # 2) 参考资料 section
    sec = re.search(r"参考资料\s*[:\u3010]?\s*\n(.+?)(?:\Z)", text, re.DOTALL)
    if sec:
        block = sec.group(1)
        for m in re.finditer(r"https?://[^\s)\]<>]+", block):
            add("ref_section", m.group(0))

    # 3) numbered lines like 1. 标题 - URL
    for m in re.finditer(r"\d+[.\u3001]\s*[^\n\-]{2,80}?\s*[-—\u2014]\s*(https?://\S+)", text):
        add("numbered_line", m.group(1))

    # 4) bare URLs anywhere
    for m in re.finditer(r"https?://[^\s)\]<>]+", text):
        add("bare_url", m.group(0))

    return out


def _find_matching_bracket(s, start):
    Q = chr(34)
    B = chr(92)
    depth = 0
    in_str = False
    escape = False
    i = start
    n = len(s)
    while i < n:
        ch = s[i]
        if in_str:
            if escape: escape = False
            elif ch == B: escape = True
            elif ch == Q: in_str = False
        else:
            if ch == Q: in_str = True
            elif ch == "[": depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0: return i
        i += 1
    return -1


def _extract_complete_objects(s):
    Q = chr(34)
    B = chr(92)
    n = len(s); i = 0; results = []
    while i < n:
        j = s.find("{", i)
        if j < 0: break
        depth = 0; in_str = False; escape = False; end = -1; k = j
        while k < n:
            ch = s[k]
            if in_str:
                if escape: escape = False
                elif ch == B: escape = True
                elif ch == Q: in_str = False
            else:
                if ch == Q: in_str = True
                elif ch == "{": depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0: end = k; break
            k += 1
        if end < 0: break
        snippet = s[j:end + 1]
        try:
            obj = json.loads(snippet)
            if isinstance(obj, dict): results.append(obj)
        except: pass
        i = end + 1
    return results


def _unwrap_list(data):
    if isinstance(data, list): return data
    if isinstance(data, dict):
        for k in ("cases", "data", "list"):
            if k in data and isinstance(data[k], list): return data[k]
        # 单个对象也包成 1 条 list (step2 SUMMARY_PROMPT 返回的就是单个 dict)
        return [data]
    return []


def _strip_markdown_fence(raw):
    """剥掉 ```json ... ``` 围栏(包括可能的语言标记)。"""
    if not raw: return raw
    m = re.search(r"^\s*```(?:json|JSON)?\s*\n(.*?)\n```\s*$", raw, re.DOTALL)
    if m:
        return m.group(1).strip()
    return raw.strip()


def parse_json_array(raw):
    """容错 JSON 解析:剥围栏 -> 修字面换行 -> 优先对象 -> 数组 -> 提取完整对象。

    注意:当 LLM 返回带角标 [1][2] 的 summary 时,不能直接 find("[") — 会误把角标当数组起点。
    所以优先搜对象 { ... } ,失败再退到数组。
    """
    if not raw: return []
    cleaned = _strip_markdown_fence(raw)
    # 修 LLM 偶尔在字符串里塞字面换行(JSON 不合法)
    cleaned = _fix_literal_newlines_in_json(cleaned)
    # 1. 直接整段 json.loads
    try: return _unwrap_list(json.loads(cleaned))
    except: pass
    # 2. 优先找 { ... } 对象(LLM 在 step2 返回的就是单个对象)
    obj_start = cleaned.find("{")
    if obj_start >= 0:
        obj_end = _find_matching_brace(cleaned, obj_start)
        if obj_end > obj_start:
            try: return _unwrap_list(json.loads(cleaned[obj_start:obj_end + 1]))
            except: pass
    # 3. 找 [ ... ] 数组
    arr_start = cleaned.find("[")
    if arr_start >= 0:
        arr_end = _find_matching_bracket(cleaned, arr_start)
        if arr_end > arr_start:
            try: return _unwrap_list(json.loads(cleaned[arr_start:arr_end + 1]))
            except: return _extract_complete_objects(cleaned[arr_start:arr_end + 1])
    # 4. 兜底:从 { 开始提完整对象
    if obj_start >= 0:
        return _extract_complete_objects(cleaned[obj_start:])
    return []


def _find_matching_brace(s, start):
    """找与 s[start] 的 { 匹配的 },考虑字符串转义。"""
    Q = chr(34)
    B = chr(92)
    depth = 0
    in_str = False
    escape = False
    i = start
    n = len(s)
    while i < n:
        ch = s[i]
        if in_str:
            if escape: escape = False
            elif ch == B: escape = True
            elif ch == Q: in_str = False
        else:
            if ch == Q: in_str = True
            elif ch == "{": depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0: return i
        i += 1
    return -1


def _fix_literal_newlines_in_json(s):
    """把 LLM 偶尔在 JSON 字符串里塞的字面换行/回车转义成 \\n / \\r。简单状态机。"""
    Q = chr(34)
    B = chr(92)
    LF = chr(10)
    CR = chr(13)
    out = []
    in_str = False
    escape = False
    for ch in s:
        if in_str:
            if escape:
                escape = False
            elif ch == B:
                escape = True
            elif ch == Q:
                in_str = False
            elif ch == LF:
                out.append(B + "n"); continue
            elif ch == CR:
                out.append(B + "r"); continue
        else:
            if ch == Q:
                in_str = True
        out.append(ch)
    return "".join(out)

def strip_citation_marks(text):
    """从 LLM summary 里剥掉 [1][2][1,2][1-3] 之类的角标引用。"""
    if not text: return text
    # 匹配 [1] / [1,2] / [1,2,3] / [1-3] / [ref_1]
    return re.sub(r"\[\s*\d+(?:\s*[,\-]\s*\d+)*\s*\]|\[ref_\d+\]", "", text).strip()

def clean_search_title(title):
    """清洗搜索引擎抓的标题:去掉 '|..._源站名' 之类的尾巴。

    例: "朱家角古镇汉风奇妙夜|朱家角古镇|..._新浪新闻" -> "朱家角古镇汉风奇妙夜"
    """
    if not title: return title
    s = str(title).strip()
    # 1. 按 | 切,取第一段(最常是真实标题,后面是 SEO 关键词)
    s = s.split("|", 1)[0].strip()
    # 2. 末尾 _源站名 切掉(例: "_新浪新闻" / " - 央视网")
    s = re.sub(r"\s*[_\-][^_\-]+$", "", s).strip()
    return s

def validate_url(url, timeout: float = 4.0):
    """轻量级 URL 实测:HEAD 优先,微信公众号等拒绝 HEAD 时自动 GET+Range 兜底。

    返回 (ok, info):
      - ok=True  : info 是 HTTP 状态码
      - ok=False : info 是错误描述("404"/"SSL"/"timeout"/"conn_err"/...)

    判定规则:
      - 2xx/3xx                 -> True(URL 可达)
      - 405 Method Not Allowed  -> GET 兜底,2xx/3xx/206 才 True
      - 401/403/429             -> True(可能反爬,URL 本身存在)
      - 4xx(除上述)             -> False(URL 编造,典型 404)
      - 5xx                     -> GET 兜底,2xx/3xx/206 才 True
      - SSL/timeout/conn_err    -> False(网络层失败)
    """
    if not url or not url.startswith(("http://", "https://")):
        return False, "invalid_scheme"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    }
    try:
        r = requests.head(url, allow_redirects=True, timeout=timeout, headers=headers)
        sc = r.status_code
        if 200 <= sc < 400:
            return True, sc
        if sc == 405:
            r2 = requests.get(url, allow_redirects=True, timeout=timeout, stream=True,
                              headers={**headers, "Range": "bytes=0-1024"})
            sc2 = r2.status_code
            if sc2 in (200, 201, 206) or 300 <= sc2 < 400:
                return True, sc2
            return False, sc2
        if sc in (401, 403, 429):
            return True, sc
        # 4xx(404/410)/5xx:先 GET 兜底
        try:
            r2 = requests.get(url, allow_redirects=True, timeout=timeout, stream=True,
                              headers={**headers, "Range": "bytes=0-1024"})
            sc2 = r2.status_code
            if sc2 in (200, 201, 206) or 300 <= sc2 < 400:
                return True, sc2
            return False, sc2
        except Exception:
            return False, sc
    except requests.exceptions.SSLError:
        return False, "SSL"
    except requests.exceptions.Timeout:
        return False, "timeout"
    except requests.exceptions.ConnectionError:
        return False, "conn_err"
    except Exception as e:
        return False, str(e)[:50]