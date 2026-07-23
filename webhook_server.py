
import json
import threading
import time
import uuid

import requests
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

import config
import dedup
import main as main_mod
import mysql_client
from utils import setup_logger

log = setup_logger()

WEBHOOK_PATH = config.WEBHOOK_PATH
WEBHOOK_PATH_FEISHU = "/timingTasks/wenlv_feishu"  # 触发原 main.py(写飞书)的主流程路径


# 简易内存任务状态(单机进程内,够用)
_jobs: dict = {}
_jobs_lock = threading.Lock()


def _notify_feishu_workflow(rec_id: str, response_data: str, job_id: str, source: str):
    """主动 POST 飞书 webhook URL,触发"接收 Webhook 时"工作流更新记录。
    rec_id: 飞书记录 ID(对应 data_id 字段),用于"修改记录"节点筛选
    response_data: 写入记录的"注意!"字段内容
    source: 标识来源(started / done / failed),便于日志追踪
    失败不抛异常,只记 warning。
    """
    if not config.FEISHU_WORKFLOW_WEBHOOK_URL:
        log.warning("[webhook feishu] FEISHU_WORKFLOW_WEBHOOK_URL 未配置,跳过通知 source={}".format(source))
        return
    try:
        fr = requests.post(
            config.FEISHU_WORKFLOW_WEBHOOK_URL,
            headers={
                "Authorization": "Bearer " + config.FEISHU_WORKFLOW_WEBHOOK_TOKEN,
                "Content-Type": "application/json",
            },
            # 字段名对齐"别人"成功案例(data_id / response_data)
            json={"data_id": rec_id, "response_data": response_data, "job_id": job_id, "source": source},
            timeout=5,
        )
        main_mod.log.info(
            "[webhook feishu] notify source={} status={} body={}".format(
                source, fr.status_code, fr.text[:200]
            )
        )
    except Exception as e:
        main_mod.log.warning("[webhook feishu] notify {} 失败: {}".format(source, e))


def _set_job(job_id: str, status: str, **extra):
    with _jobs_lock:
        _jobs[job_id] = {"status": status, "ts": time.time(), **extra}


def _get_job(job_id: str) -> dict:
    with _jobs_lock:
        return dict(_jobs.get(job_id, {}))


def _run_feishu_job_async(job_id: str, rec_id: str, trigger_time_str: str):
    """后台线程跑 run_to_feishu,记录状态。
    rec_id: 飞书记录 ID(用于事后推送完成/失败状态)
    trigger_time_str: 触发时间字符串(保持回显时间一致)
    """
    started_at = time.time()
    try:
        _set_job(job_id, "running", started_at=started_at)
        result = main_mod.run_to_feishu()
        elapsed = int(time.time() - started_at)
        mins, secs = divmod(elapsed, 60)
        data_done = "触发时间:{}, 更新完成!用时:{}分{}秒".format(
            trigger_time_str, mins, secs
        )
        # 事后推送完成状态给飞书"接收 Webhook 时"工作流
        _notify_feishu_workflow(rec_id, data_done, job_id, "wenglu_webhook_done")
        _set_job(job_id, "done", finished_at=time.time(), result=result, elapsed=elapsed)
        log.info(f"[webhook feishu] job {job_id} done in {mins}m{secs}s: {result}")
    except Exception as e:
        main_mod.log.exception(f"[webhook feishu] job {job_id} 失败")
        elapsed = int(time.time() - started_at)
        mins, secs = divmod(elapsed, 60)
        err_msg = str(e)[:50].replace(chr(10), " ")
        data_fail = "触发时间:{}, 更新失败!用时:{}分{}秒,原因:{}".format(
            trigger_time_str, mins, secs, err_msg
        )
        # 失败也要推送给飞书,让记录上的"注意!"能反映失败
        _notify_feishu_workflow(rec_id, data_fail, job_id, "wenglu_webhook_failed")
        _set_job(job_id, "failed", finished_at=time.time(), error=str(e), elapsed=elapsed)


def _run_job_async(job_id: str):
    """后台线程跑 run_to_mysql,记录状态。"""
    try:
        _set_job(job_id, "running", started_at=time.time())
        result = main_mod.run_to_mysql()
        _set_job(job_id, "done", finished_at=time.time(), result=result)
        log.info(f"[webhook] job {job_id} done: {result}")
    except Exception as e:
        main_mod.log.exception(f"[webhook] job {job_id} 失败")
        _set_job(job_id, "failed", finished_at=time.time(), error=str(e))


def _extract_token(headers, params: dict) -> str:
    """从以下三个来源按优先级提取 token:
    1) query 参数: ?token=xxx
    2) Authorization: Bearer xxx(标准 Bearer 鉴权)
    3) X-Webhook-Token: xxx(自定义 header 兜底)
    返回第一个非空值,都没有则返回 ""。
    """
    # 1) query
    t = (params.get("token", [""])[0] or "").strip()
    if t:
        return t
    # 2) Authorization: Bearer xxx
    auth = (headers.get("Authorization", "") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    # 3) X-Webhook-Token
    t = (headers.get("X-Webhook-Token", "") or "").strip()
    if t:
        return t
    return ""


def _check_token(headers, params: dict) -> bool:
    """可选 token 鉴权:从 query / Bearer header / X-Webhook-Token 提取,匹配 WEBHOOK_TOKEN 才放行。
    WEBHOOK_TOKEN 空配置时不校验(开发模式)。
    """
    if not config.WEBHOOK_TOKEN:
        return True
    return _extract_token(headers, params) == config.WEBHOOK_TOKEN


class WebhookHandler(BaseHTTPRequestHandler):
    def _handle_path(self):
        """GET/HEAD 共用路径处理:返回 (status_code, body_dict)。

        路由策略(免鉴权优先,触发才走 token):
          - /health  或 /ping                         → 200 健康(免鉴权)
          - /                                       → 200 服务描述(免鉴权)
          - /timingTasks/wenlv_feishu?status=xxx     → 200 任务状态(免鉴权,只读)
          - /timingTasks/wenlv_db?status=xxx         → 200 任务状态(免鉴权,只读)
          - /timingTasks/wenlv_feishu (GET/HEAD)     → 405 请用 POST
          - /timingTasks/wenlv_db (GET/HEAD 触发)    → 需要 token,异步触发
          - 其它                                     → 404
        """
        try:
            url = urlparse(self.path)
            params = parse_qs(url.query)

            # 健康检查(免鉴权)
            if url.path in ("/health", "/ping"):
                return 200, {"status": "ok", "ts": time.time(), "service": "wenlv-webhook"}

            # 首页(免鉴权,简洁描述便于排查)
            if url.path == "/":
                return 200, {
                    "service": "wenlv-webhook",
                    "endpoints": {
                        "health": "/health (免鉴权)",
                        "post_trigger_feishu": WEBHOOK_PATH_FEISHU + " (POST,需 token)",
                        "post_write_mysql": WEBHOOK_PATH + " (POST,免鉴权)",
                        "get_status_feishu": WEBHOOK_PATH_FEISHU + "?status=<job_id> (免鉴权,只读)",
                        "get_status_db": WEBHOOK_PATH + "?status=<job_id> (免鉴权,只读)",
                    },
                }

            # 主路径
            if url.path in (WEBHOOK_PATH, WEBHOOK_PATH_FEISHU):
                # ?status=xxx 是只读查询,免鉴权(任务 id 是 uuid 撞不到)
                if "status" in params:
                    return 200, _get_job(params["status"][0]) or {"status": "not_found"}

                # 触发类操作走 token 鉴权
                if not _check_token(self.headers, params):
                    return 403, {"status": "forbidden", "msg": "token 不正确"}

                if url.path == WEBHOOK_PATH_FEISHU:
                    return 405, {
                        "status": "method_not_allowed",
                        "msg": "请用 POST 触发 main 主流程",
                    }

                # GET /wenlv_db 触发:异步跑
                job_id = str(uuid.uuid4())[:8]
                t = threading.Thread(target=_run_job_async, args=(job_id,), daemon=True)
                t.start()
                return 200, {
                    "status": "started",
                    "job_id": job_id,
                    "query": "GET " + WEBHOOK_PATH + "?status=" + job_id,
                }

            return 404, {"status": "not_found", "path": url.path}
        except Exception as e:
            main_mod.log.exception(f"[webhook] 处理 GET/HEAD 请求失败: {e}")
            return 500, {"status": "error", "msg": str(e)}

    def _send(self, code: int, body: dict):
        """统一发送 JSON 响应;HEAD 仅返回 headers(BaseHTTPRequestHandler 自动不发 body)。"""
        body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body_bytes)

    def do_GET(self):  # noqa: N802
        code, body = self._handle_path()
        self._send(code, body)

    def do_HEAD(self):  # noqa: N802
        # 监控/探活常用 HEAD,返回跟 GET 一致的 headers 不发 body
        code, body = self._handle_path()
        self._send(code, body)

    def do_POST(self):  # noqa: N802
        """POST 接收多维表 8 字段 JSON 写 MySQL, 或触发 main 主流程写飞书。"""
        try:
            url = urlparse(self.path)

            # 路径 1: 触发原 main.py 主流程(写飞书),异步
            if url.path == WEBHOOK_PATH_FEISHU:
                # token 鉴权(query / Authorization Bearer / X-Webhook-Token 任一)
                params0 = parse_qs(url.query)
                if not _check_token(self.headers, params0):
                    received = _extract_token(self.headers, params0)
                    self._json(403, {
                        "status": "forbidden",
                        "msg": "token 不正确",
                        "hint": "通过 query (?token=xxx) 或 Authorization: Bearer 头传 token",
                        "received_token_prefix": (received[:6] + "***") if received else "(none)",
                    })
                    return
                # 解析 body / query:飞书工作流"发送 HTTP 请求"节点配的字段名是 data_id(对齐"别人"成功案例),
                # 保留 id / record_id 兼容旧配置。
                rec_id = ""
                name = ""
                length = int(self.headers.get("Content-Length", "0") or "0")
                if length > 0:
                    raw = self.rfile.read(length)
                    try:
                        obj = json.loads(raw.decode("utf-8")) if raw else {}
                        if isinstance(obj, dict):
                            rec_id = str(
                                obj.get("data_id") or obj.get("id") or obj.get("record_id") or ""
                            ).strip()
                            name = str(
                                obj.get("name") or obj.get("note") or obj.get("data") or ""
                            ).strip()
                    except Exception:
                        pass
                # query 兜底(用户实际工作流把 data_id 放在 query 参数里)
                qp = parse_qs(url.query)
                if not rec_id:
                    rec_id = (
                        qp.get("data_id", [""])[0]
                        or qp.get("id", [""])[0]
                        or qp.get("record_id", [""])[0]
                    ).strip()
                if not name:
                    name = (
                        qp.get("name", [""])[0]
                        or qp.get("note", [""])[0]
                        or qp.get("data", [""])[0]
                    ).strip()
                if not name:
                    name = "文旅案例"
                main_mod.log.info(
                    "[webhook feishu] 收到触发 rec_id={!r} name={!r} query_keys={}".format(
                        rec_id, name, list(qp.keys())
                    )
                )

                # 提前生成 job_id / 触发时间 / 初始回显
                # 主流程跑完后会再次调用 _notify_feishu_workflow 将"注意!"更新为完成/失败状态
                job_id = str(uuid.uuid4())[:8]
                now_str = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
                data_initial = "触发时间:{}, 正在更新案例，预计20分钟......".format(now_str)

                # 主动通知飞书"接收 Webhook 时"工作流,使其节点 2 修改记录的"注意!"字段写入初始状态
                _notify_feishu_workflow(rec_id, data_initial, job_id, "wenglu_webhook")

                # 异步跑主流程(传递 rec_id 和 trigger_time_str,以便跑完后再推送完成状态)
                t = threading.Thread(
                    target=_run_feishu_job_async,
                    args=(job_id, rec_id, now_str),
                    daemon=True,
                )
                t.start()

                # 同步回显:飞书工作流"发送 HTTP 请求"节点的响应配置为 {status, job_id},
                # 字段名必须对齐才不会报"出参返回格式不正确"。
                # id/data 只是调试参考,不给飞书用。
                self._json(200, {
                    "status": "started",
                    "job_id": job_id,
                    "id": rec_id or job_id,
                    "data": data_initial,
                })
                return

            if url.path != WEBHOOK_PATH:
                self._json(404, {"status": "not_found", "path": url.path})
                return

            # 注:wenlv_db 路径不需要 token 鉴权,仅供飞书多维表工作流在公网调用
            # 如果未来要限制谁能调用,可重新加上 _check_token 鉴权块

            # 读取 body
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length > 0 else b""
            if not raw:
                self._json(400, {"code": -1, "status": "bad_request", "msg": "body 为空,需提供 JSON", "data": {"reason": "body 为空"}})
                return
            try:
                row = json.loads(raw.decode("utf-8"))
            except Exception as e:
                self._json(400, {
                                "code": -1,
                                "status": "bad_request",
                                "msg": f"JSON 解析失败: {e}",
                                "data": {"raw_prefix": raw[:200].decode("utf-8", errors="replace")},
                            })
                return
            if not isinstance(row, dict):
                self._json(400, {"code": -1, "status": "bad_request", "msg": "body 必须是 JSON object", "data": {"reason": "type error"}})
                return

            # 白名单预检:至少要有 1 个允许写入的字段,否则返回 400(避免 insert_row 抛 500)
            allowed = ["title", "url", "summary", "publish_time", "author",
                       "contentAddressNew", "source", "info_type"]
            # 显式剥离 use_time(无论飞书是否传,这里都扔掉)— 用户要求不入库
            row.pop("use_time", None)
            if not any(row.get(k) for k in allowed):
                self._json(400, {
                                "code": -1,
                                "status": "bad_request",
                                "msg": f"body 缺少有效字段,至少需要 1 个: {allowed}",
                                "data": {"allowed": allowed},
                            })
                return

            # 查重:URL 精准匹配 + summary 语义相似度(跟主流程 _dedup_against_mysql 一致)
            # 命中则跳过写入,返回 status=duplicate,让飞书工作流可读这个状态
            if config.MYSQL_DEDUP_ENABLED:
                try:
                    url_v = (row.get("url") or "").strip()
                    summary_v = (row.get("summary") or "").strip()
                    is_dup, reason = dedup.check_duplicate(url_v, summary_v)
                    if is_dup:
                        main_mod.log.info(
                            "[webhook POST] 重复入库,跳过: {} (title={!r})".format(
                                reason, (row.get("title") or "")[:30]
                            )
                        )
                        self._json(200, {
                            # 多字段双向兼容: code/status/msg 适配飞书常见 schema, id/data 调试参考
                            "code": 1,
                            "status": "duplicate",
                            "msg": "已存在相似记录,跳过写入",
                            "data": {"reason": reason},
                            "reason": reason,
                        })
                        return
                except Exception as e:
                    # 查重失败降级为"未命中",不阻塞入库(与主流程 _dedup_against_mysql 一致)
                    main_mod.log.warning("[webhook POST] 查重失败,降级为未命中: {}".format(e))

            # 写入 MySQL
            try:
                new_id = mysql_client.insert_row(row)
            except Exception as e:
                main_mod.log.exception(f"[webhook POST] 写入 MySQL 失败: {e}")
                self._json(500, {"code": -1, "status": "error", "msg": str(e), "data": {"error": str(e)}})
                return

            main_mod.log.info(
                f"[webhook POST] 写入 MySQL id={new_id} title={row.get('title', '')[:30]!r}"
            )
            # 多字段双向兼容: code/status/msg 适配飞书常见响应配置,id/title 供后续修改记录使用
            self._json(200, {
                "code": 0,
                "status": "ok",
                "msg": "ok",
                "id": new_id,
                "title": row.get("title", ""),
                "data": {"id": new_id, "title": row.get("title", "")},
            })
        except Exception as e:
            main_mod.log.exception(f"[webhook POST] 处理请求失败: {e}")
            self._json(500, {"status": "error", "msg": str(e)})

    def _json(self, code: int, body: dict):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(body, ensure_ascii=False).encode("utf-8"))

    # 用项目 logger 替代 stderr
    def log_message(self, fmt, *args):
        log.info(f"[webhook] {self.address_string()} - {fmt % args}")


def serve():
    host = config.WEBHOOK_HOST
    port = config.WEBHOOK_PORT
    server = HTTPServer((host, port), WebhookHandler)
    log.info("=" * 60)
    log.info(f"Webhook 服务已启动: http://{host}:{port}")
    log.info(f"POST {WEBHOOK_PATH}  (接收多维表数据 -> 写 MySQL)")
    log.info(f"POST {WEBHOOK_PATH_FEISHU}  (触发 main 主流程 -> 写飞书 Bitable, 不写 MySQL)")
    log.info(f"GET  {WEBHOOK_PATH}  (触发 LLM 采集,异步)")
    log.info(f"GET  /health")
    log.info("按 Ctrl+C 停止")
    log.info("=" * 60)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("收到 Ctrl+C,正在停止...")
        server.shutdown()


if __name__ == "__main__":
    serve()