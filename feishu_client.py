# -*- coding: utf-8 -*-
"""飞书多维表格(Bitable)API 客户端"""
import time
import logging
from typing import List, Optional

import requests

import config

log = logging.getLogger("wenglu")


class FeishuError(Exception):
    """飞书 API 业务错误"""
    pass


class FeishuClient:
    def __init__(
        self,
        app_id: str = None,
        app_secret: str = None,
        app_token: str = None,
        table_id: str = None,
    ):
        self.app_id = app_id or config.FEISHU_APP_ID
        self.app_secret = app_secret or config.FEISHU_APP_SECRET
        self.app_token = app_token or config.FEISHU_APP_TOKEN
        self.table_id = table_id or config.FEISHU_TABLE_ID
        self.base_url = config.FEISHU_BASE_URL
        self._token: Optional[str] = None
        self._token_expire: float = 0.0
        self._check_creds()

    def _check_creds(self):
        missing = []
        if not self.app_id:     missing.append("FEISHU_APP_ID")
        if not self.app_secret: missing.append("FEISHU_APP_SECRET")
        if not self.app_token:  missing.append("FEISHU_APP_TOKEN")
        if not self.table_id:   missing.append("FEISHU_TABLE_ID")
        if missing:
            raise RuntimeError(
                "飞书凭证未配置,缺少: " + ", ".join(missing) + "\n"
                "请在 config.py 中填写,或通过环境变量设置。\n"
                "获取方式:打开多维表格 URL https://xxx.feishu.cn/base/APP_TOKEN?table=TABLE_ID"
            )

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expire - 60:
            return self._token
        url = f"{self.base_url}/auth/v3/tenant_access_token/internal"
        r = requests.post(
            url,
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            raise FeishuError(f"获取 tenant_access_token 失败: {data}")
        self._token = data["tenant_access_token"]
        self._token_expire = time.time() + int(data.get("expire", 7200))
        log.info("飞书 tenant_access_token 已刷新")
        return self._token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def list_records(self, page_size: int = 500) -> List[dict]:
        """分页拉取所有记录(原始 records 列表,每条含 record_id 和 fields)"""
        records = []
        page_token = None
        url = (
            f"{self.base_url}/bitable/v1/apps/{self.app_token}"
            f"/tables/{self.table_id}/records"
        )
        while True:
            params = {"page_size": page_size}
            if page_token:
                params["page_token"] = page_token
            r = requests.get(url, headers=self._headers(), params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
            if data.get("code") != 0:
                raise FeishuError(
                    f"拉取飞书记录失败: code={data.get('code')} msg={data.get('msg')}"
                )
            items = (data.get("data") or {}).get("items") or []
            records.extend(items)
            if not (data.get("data") or {}).get("has_more"):
                break
            page_token = data["data"].get("page_token")
        log.info(f"飞书侧共拉取到 {len(records)} 条历史记录")
        return records

    def add_records(self, records: List[dict], max_per_batch: int = 500) -> List[dict]:
        """批量新增 records 到 Bitable。
        records: List[dict],每个 dict 的 key 已是飞书字段名(已通过 mapping 转换)"""
        if not records:
            return []
        url = (
            f"{self.base_url}/bitable/v1/apps/{self.app_token}"
            f"/tables/{self.table_id}/records/batch_create?user_id_type=open_id"
        )
        all_added = []
        for i in range(0, len(records), max_per_batch):
            batch = records[i:i + max_per_batch]
            body = {"records": [{"fields": r} for r in batch]}
            r = requests.post(url, headers=self._headers(), json=body, timeout=60)
            r.raise_for_status()
            data = r.json()
            if data.get("code") != 0:
                raise FeishuError(
                    f"写入飞书记录失败: code={data.get('code')} msg={data.get('msg')}\n"
                    f"前 2 条样例: {batch[:2]}"
                )
            all_added.extend((data.get("data") or {}).get("records") or [])
        log.info(f"飞书侧已写入 {len(all_added)} 条新记录")
        return all_added
