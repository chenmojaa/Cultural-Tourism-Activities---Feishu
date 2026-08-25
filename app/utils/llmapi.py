# -*- coding: utf-8 -*-
import logging
from typing import Any, List, Optional

import requests

import config
from app.utils.tools import retry

log = logging.getLogger("wenglu")


# 百炼"仅支持 Responses API"的模型(传统 enable_search/search_options 对它们不生效)
# 参考:百炼模型列表 - 千问Plus/Max 部分的"仅 Responses API 支持"标注
_RESPONSES_API_MODELS = (
    "qwen3.6-plus",
    "qwen3.7-plus",
    "qwen3.7-max",
)

# 百炼"传统原生 API 可用 + 想要 enable_source/enable_citation"的模型
# (原生 API 才支持 enable_source + enable_citation,Chat Completions 拿不到角标 URL)
_DASHSCOPE_NATIVE_MODELS = (
    "qwen3.6-max-preview",
    "qwen-plus-latest",
    "qwen-plus",
    # qwen3.6-plus / qwen3.7-plus / qwen3.7-max 不在此列表(仅 Responses API,原生 API 调用会报 "url error")
)


def _model_needs_responses_api(model: str) -> bool:
    m = (model or "").lower()
    return any(m.startswith(p) for p in _RESPONSES_API_MODELS)


def _model_needs_dashscope_native(model: str) -> bool:
    m = (model or "").lower()
    return any(m.startswith(p) for p in _DASHSCOPE_NATIVE_MODELS)


class LLMClient:
    def __init__(self, provider: str = None):
        self.provider = (provider or config.PROVIDER).lower()
        if self.provider != "qwen":
            raise ValueError(f"不支持的 provider: {self.provider} (仅支持 qwen)")

        key = config.QWEN_API_KEY
        if not key:
            raise RuntimeError(
                "未设置 QWEN_API_KEY。请在 config.py 中填写,"
                '或执行: ="sk-xxx"'
            )
        self.model = config.QWEN_MODEL
        self.use_responses_api = _model_needs_responses_api(self.model)
        # 两个路径独立判断(不互斥):原生 API 优先走,失败后跳 Responses API
        # qwen3.6-plus 同时两者都在列表里,让 chat() 路由决定调用哪个
        self.use_dashscope_native = _model_needs_dashscope_native(self.model)

        # 走原生 API 时,显式把 base_http_api_url 指到跟 QWEN_BASE_URL 同一地域,
        # 避免 SDK 默认国际站导致某些模型报 "url error"(InvalidParameter)
        if self.use_dashscope_native:
            native_base = (config.QWEN_DASHSCOPE_BASE_URL or "").strip()
            if not native_base:
                obu = (config.QWEN_BASE_URL or "").lower()
                if "dashscope-intl" in obu:
                    native_base = "https://dashscope-intl.aliyuncs.com/api/v1"
                else:
                    native_base = "https://dashscope.aliyuncs.com/api/v1"
            self._native_base = native_base
            log.info(f"[native API] base = {native_base}")

        # 仅百炼官方 API 支持搜索;本地服务不支持
        self.is_official = (
            "dashscope.aliyuncs.com" in (config.QWEN_BASE_URL or "")
            or "aliyuncs.com" in (config.QWEN_BASE_URL or "")
        )
        self.supports_search = (
            getattr(config, "QWEN_ENABLE_SEARCH", False) and self.is_official
        )
        # 最近一次调用的完整响应(含 search_info / output),调用方 step2_summary 可读
        self.last_response = None

        # 与 chat() 路由伀起
        if self.use_responses_api:
            api_tag = "[Chat Completions + forced_search]"
        elif self.use_dashscope_native:
            api_tag = "[DashScope 原生 API]"
        else:
            api_tag = "[Responses API]"
        search_tag = " [联网搜索已开]" if self.supports_search else ""
        log.info(
            "LLM 客户端初始化: provider=" + self.provider
            + " model=" + self.model
            + " " + api_tag + search_tag
        )

    @retry(max_attempts=3, delay=5, backoff=2)
    def chat(
        self,
        system: str,
        user: str,
        temperature: float = 0.7,
        max_tokens: int = 4000,
        enable_search: bool = None,
    ) -> str:
        """单轮对话。三条路径自动选:
        - Responses API(tools=[web_search]):qwen3.6-plus / qwen3.7-plus / qwen3.7-max
        - DashScope 原生 API(enable_search+search_options):qwen3.6-max-preview / qwen-plus*
        - Chat Completions 兜底:其它模型(可能拿不到角标 URL)

        enable_search:
        - None: 按 config.QWEN_ENABLE_SEARCH 走
        - True/False: 显式覆盖
        """
        do_search = (
            self.supports_search
            and (enable_search if enable_search is not None else True)
        )
        # qwen3.6-plus 优先 Chat Completions(唯一能强制联网,实测 58s):
        # - 原生 API 报 url error
        # - Responses API 不能 forced_search(拒 tool_choice) 且 LLM 偷懒不调 web_search
        # - Chat Completions + forced_search=True 是唯一能强制联网的路径
        # URL 来源:step2 SUMMARY_PROMPT 强制 LLM 在 article_url 字段填纯 URL,代码中 _extract_url_from_field 拆 markdown
        if self.use_responses_api:
            return self._chat_chat_completions(system, user, temperature, max_tokens, do_search)
        if self.use_dashscope_native:
            return self._chat_dashscope_native(system, user, temperature, max_tokens, do_search)
        return self._chat_responses_api(system, user, temperature, max_tokens, do_search)

    # ----------------------------------------------------------------
    # 路径 1:Responses API(OpenAI 兼容 /v1/responses + tools=[web_search])
    # 适用:qwen3.6-plus / qwen3.7-plus / qwen3.7-max 等"仅 Responses API"模型
    # ----------------------------------------------------------------
    def _chat_responses_api(
        self, system: str, user: str, temperature: float,
        max_tokens: int, do_search: bool,
    ) -> str:
        # 纯 HTTP,无 SDK:POST {QWEN_BASE_URL}/responses
        # Responses API 用 input 而不是 messages;role 仍为 system/user
        input_msgs = []
        if system:
            input_msgs.append({"role": "system", "content": system})
        input_msgs.append({"role": "user", "content": user})

        body = {
            "model": self.model,
            "input": input_msgs,
            "temperature": temperature,
            "max_output_tokens": max_tokens,
        }
        if do_search:
            # 关键:内置 web_search 工具;百炼会在 output 里返回 web_search_call
            # (含 action.sources)和 message.content[].annotations(角标)
            # 注:tool_choice="required" 会被百炼 thinking mode 拒绝(仅提示 LLM 调用,不能强制)
            body["tools"] = [{"type": "web_search"}]
            log.debug(f"[{self.model}] Responses API 启用 web_search 工具")

        url = config.QWEN_BASE_URL.rstrip("/") + "/responses"
        headers = {
            "Authorization": f"Bearer {config.QWEN_API_KEY}",
            "Content-Type": "application/json",
        }
        try:
            # 给 web_search + reasoning 充足时间(5min),避免 60s 默认超时被砍
            r = requests.post(url, headers=headers, json=body, timeout=300.0)
            r.raise_for_status()
            data = r.json()
        except requests.exceptions.RequestException as e:
            raise RuntimeError(
                f"Responses API 调用失败 [{self.model}]: {type(e).__name__}: {e}"
            ) from e
        except ValueError as e:
            raise RuntimeError(
                f"Responses API 响应解析失败 [{self.model}]: {r.text[:300]}"
            ) from e

        # 缓存完整响应(dict),get_last_search_results 读 annotations/sources
        self.last_response = data
        text = _extract_text_from_responses(data)
        if not text:
            # 调试用:打一段输出结构
            try:
                preview = str(data)[:600]
            except Exception:
                preview = "<unrepr>"
            raise RuntimeError(
                f"Responses API 返回空文本 [model={self.model}],原始前 600 字:{preview}"
            )
        return text


    # ----------------------------------------------------------------
    # 路径 1.5: OpenAI 兼容 Chat Completions + forced_search
    # 适用:百炼 OpenAI 兼宽口(/compatible-mode/v1/chat/completions)
    # 优势:extra_body 传 forced_search=True 强制联网(避免 LLM 凭卷)
    # 限制:只适用百炼兼宽模型(qwen-plus / qwen3.6-plus 都可试)
    # 失败调用者应 fallback 到 _chat_responses_api
    # ----------------------------------------------------------------
    def _chat_chat_completions(
        self, system: str, user: str, temperature: float,
        max_tokens: int, do_search: bool,
    ) -> str:
        # 纯 HTTP,无 SDK:POST {QWEN_BASE_URL}/chat/completions
        # 百炼兼容模式把 enable_search / search_options 放在顶层
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})

        body = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if do_search:
            # 严格按百炼官方示例:只传 forced_search=True
            # 多传 enable_source/enable_citation 会让 LLM 模仿百炼内部搜索格式 返 [{"query":...}],而不是真推荐 case
            body["enable_search"] = True
            body["search_options"] = {"forced_search": True}
            log.debug(f"[{self.model}] Chat Completions 启用 forced_search=True(精简)")

        url = config.QWEN_BASE_URL.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {config.QWEN_API_KEY}",
            "Content-Type": "application/json",
        }
        try:
            r = requests.post(url, headers=headers, json=body, timeout=300.0)
            r.raise_for_status()
            data = r.json()
        except requests.exceptions.RequestException as e:
            raise RuntimeError(
                f"Chat Completions 调用失败 [{self.model}]: {type(e).__name__}: {e}"
            ) from e
        except ValueError as e:
            raise RuntimeError(
                f"Chat Completions 响应解析失败 [{self.model}]: {r.text[:300]}"
            ) from e

        self.last_response = data
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"Chat Completions 返回空 choices [model={self.model}]")
        content = (choices[0].get("message") or {}).get("content") or ""
        if not content:
            raise RuntimeError(f"Chat Completions 返回空文本 [model={self.model}]")
        return content

    # ----------------------------------------------------------------
    # 路径 2:DashScope 原生 API
    # ----------------------------------------------------------------
    # 路径 2:DashScope 原生 API(enable_search + search_options)
    # 适用:qwen3.6-max-preview / qwen-plus* 等
    # ----------------------------------------------------------------
    def _chat_dashscope_native(
        self, system: str, user: str, temperature: float,
        max_tokens: int, do_search: bool,
    ) -> str:
        # Python 3.7 兼容:不依赖 dashscope SDK,直接调 DashScope HTTP 接口
        # 端点: POST {base}/services/aigc/text-generation/generation
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})

        parameters = {
            "temperature": temperature,
            "max_tokens": max_tokens,
            "result_format": "message",
        }
        if do_search:
            # 关键:开启联网 + 真实搜索结果 + 角标标注
            parameters["enable_search"] = True
            # forced_search=True 强制模型每次都调联网搜索(否则模型可能凭印象编)
            parameters["search_options"] = {
                "enable_source": True,            # 返回 search_info.search_results
                "enable_citation": True,          # 回复里带 [ref_N] 角标
                "citation_format": "[ref_<number>]",  # 角标样式更易读
                "forced_search": True,            # 强制联网,避免 LLM 编造
            }
            log.debug(f"[{self.model}] DashScope 原生 API 启用联网搜索 (forced_search + enable_source + enable_citation)")

        url = f"{self._native_base.rstrip('/')}/services/aigc/text-generation/generation"
        headers = {
            "Authorization": f"Bearer {config.QWEN_API_KEY}",
            "Content-Type": "application/json",
        }
        body = {"model": self.model, "input": {"messages": messages}, "parameters": parameters}
        try:
            r = requests.post(url, headers=headers, json=body, timeout=300.0)
        except requests.exceptions.RequestException as e:
            raise RuntimeError(
                f"DashScope HTTP 调用失败 [{self.model}]: {type(e).__name__}: {e}"
            ) from e

        try:
            data = r.json()
        except ValueError as e:
            raise RuntimeError(
                f"DashScope 响应解析失败 [{self.model}]: status={r.status_code} body={r.text[:300]}"
            ) from e

        # DashScope 业务错误用顶层 code 字段(HTTP 仍为 200)
        if data.get("code"):
            raise RuntimeError(
                f"DashScope API error [{self.model}]: code={data.get('code')} "
                f"msg={data.get('message')}"
            )

        # 缓存完整响应(dict),供 step2_summary 读 search_info
        self.last_response = data

        output = data.get("output") or {}
        choices = output.get("choices") or []
        if not choices:
            raise RuntimeError(
                f"DashScope 返回空 choices [model={self.model}], body={str(data)[:300]}"
            )
        content = (choices[0].get("message") or {}).get("content") or ""
        return content

    def web_search_called(self) -> bool:
        """检测上一次 chat() 调用 LLM 是否真调了 web_search 工具。

        Returns:
            True: LLM 调了 web_search(可拿到搜索源 URL 列表)
            False: LLM 偷懒没调(凭训练数据编)
        """
        if not self.last_response:
            return False
        out = (
            self.last_response.get("output")
            if isinstance(self.last_response, dict)
            else getattr(self.last_response, "output", None)
        )
        if not out:
            return False
        for item in out:
            item_t = _get_attr(item, "type")
            if item_t == "web_search_call":
                return True
        return False

    def get_last_search_results(self) -> list:
        """读上一次 chat() 调用的真实搜索结果列表(供 step2_summary 拿 URL/publisher 用)。

        兼容三种返回结构:
        - Responses API:从 output[].web_search_call.action.sources(若百炼给)
                       或 output[].message.content[].annotations.url_citation(角标 URL)
        - DashScope 原生 API:从 response.output.search_info.search_results
        """
        if not self.last_response:
            return []
        results: List[dict] = []

        # 1) 尝试 Responses API 结构
        out = None
        if isinstance(self.last_response, dict):
            out = self.last_response.get("output")
        else:
            out = getattr(self.last_response, "output", None)
        if out:
            for item in out:
                item_t = _get_attr(item, "type")
                if item_t == "web_search_call":
                    # action.sources(若存在)
                    action = _get_attr(item, "action") or {}
                    sources = _get_attr(action, "sources") or []
                    for i, s in enumerate(sources, 1):
                        results.append({
                            "index": i,
                            "title": _get_attr(s, "title") or "",
                            "url": _get_attr(s, "url") or "",
                        })
                elif item_t == "message":
                    # message.content[].annotations(角标引用)
                    content = _get_attr(item, "content") or []
                    for c in content:
                        annos = _get_attr(c, "annotations") or []
                        for a in annos:
                            a_t = _get_attr(a, "type")
                            if a_t in ("url_citation", "citation", "web_search_citation"):
                                results.append({
                                    "index": _get_attr(a, "index") or len(results) + 1,
                                    "title": _get_attr(a, "title") or "",
                                    "url": _get_attr(a, "url") or "",
                                })
            if results:
                return results

        # 2) 回退:原生 API(DashScope HTTP 直调)的 search_info
        try:
            resp_obj = self.last_response
            output = (
                resp_obj.get("output")
                if isinstance(resp_obj, dict)
                else getattr(resp_obj, "output", None)
            )
            if output is not None:
                info = (
                    output.get("search_info")
                    if isinstance(output, dict)
                    else getattr(output, "search_info", None)
                ) or {}
                if isinstance(info, dict):
                    for r in info.get("search_results") or []:
                        results.append(r)
        except Exception:
            pass
        return results


def _get_attr(obj: Any, key: str, default=None):
    """统一从 dict / 对象取字段。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _extract_text_from_responses(resp) -> str:
    """从 OpenAI Responses API 返回中提取 assistant 文本。"""
    out = _get_attr(resp, "output") or []
    parts: List[str] = []
    for item in out:
        item_t = _get_attr(item, "type")
        if item_t == "message":
            content = _get_attr(item, "content") or []
            for c in content:
                c_t = _get_attr(c, "type")
                if c_t == "output_text":
                    txt = _get_attr(c, "text") or ""
                    if txt:
                        parts.append(txt)
    return "\n".join(parts).strip()
        # 2) 回退:原生 API(DashScope HTTP 直调)的 search_info
