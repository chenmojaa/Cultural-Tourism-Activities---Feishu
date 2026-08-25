# 文旅案例自动化

> 让 LLM 联网搜索，自动收集近 3 个月内的真实文旅活动/项目案例，写入飞书多维表格和 MySQL。

## 流程概览

```
┌─────────────────┐  ┌──────────────────┐  ┌──────────────────────┐
│ 飞书工作流按钮   │→│ webhook_server   │→│ timingTasks/tools     │
│ (发送HTTP请求)   │  │ (Flask 13001)    │  │ wenlv_case.py 主流程 │
└─────────────────┘  └──────────────────┘  └──────────┬───────────┘
                                                     │
                          ┌──────────────────────────┼──────────────────────────┐
                          ▼                          ▼                          ▼
                  ┌──────────────┐          ┌──────────────────┐      ┌──────────────────┐
                  │ 步骤 1       │          │ 步骤 2           │      │ 写入飞书 / MySQL  │
                  │ 千问 推荐案例 │ →        │ 千问 联网搜索     │ →    │ Bitable / 查重    │
                  │ (NAMES only) │          │ + 300字总结+URL  │      │ add_rows/insert   │
                  └──────────────┘          └──────────────────┘      └──────────────────┘
```

## 项目结构

```
.
├── main.py                  # 入口:python main.py 跑主流程;--serve 启动 webhook
├── webhook_server.py        # 兼容入口:Flask webhook 服务(13001 + 80)
├── config.py                # 本地凭证(已 .gitignore)
├── config.example.py        # 占位模板(可入仓)
├── requirements.txt
├── app/
│   ├── __init__.py          # create_app(),注册 timingTasks 蓝图
│   ├── utils/               # 接口层,对应 back-end-flask/app/utils
│   │   ├── llmapi.py        # 千问客户端:Chat Completions / Responses / 原生 API
│   │   ├── feishuapi.py     # 飞书 Bitable 客户端
│   │   ├── data_store.py    # 飞书写入行转换与 DataStore
│   │   ├── mysqlapi.py      # jd_rb_case 查重表 + 写入
│   │   ├── dedup.py         # URL 精准 + bge-m3 摘要语义去重
│   │   ├── prompts.py       # LLM 提示词
│   │   └── tools.py         # 日志 / 重试 / URL 验证 / JSON 容错解析
│   └── timingTasks/         # 对应 back-end-flask/app/timingTasks
│       ├── routes.py        # webhook 路由(Flask blueprint)
│       └── tools/           # 业务任务层
│           └── wenlv_case.py # 主流程:推荐 -> 联网搜索总结 -> 查重 -> 写入
└── modeltest.py             # 本地模型手工测试桩
```

## 快速开始

```bash
cd 文旅案例自动化
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

cp config.example.py config.py
# 填入真实 API_KEY / MySQL 密码 / 飞书凭证,或用环境变量注入
```

## 启动

```bash
# 跑主流程(命令行)
python main.py

# 只检查飞书凭证和现有记录
python main.py --check

# 启动 webhook 服务(默认 13001,兼容端口 80)
python webhook_server.py
# 或
python main.py --serve
```

## API 端点

| 路径 | 方法 | token 鉴权 | 说明 |
|---|---|---|---|
| `/health`, `/ping` | GET/HEAD | 免 | 健康检查 |
| `/` | GET/HEAD | 免 | 服务描述 |
| `/timingTasks/wenlv_feishu` | POST | 需要 | 触发主流程(LLM 采集 + 写飞书) |
| `/timingTasks/wenlv_db` | POST | 免 | 接收多维表数据写 MySQL |
| `/timingTasks/wenlv_feishu?status=<id>` | GET/HEAD | 免 | 查询任务状态(只读) |
| `/timingTasks/wenlv_db?status=<id>` | GET/HEAD | 免 | 查询任务状态(只读) |

## 关键特性

- LLM 联网搜索真实 URL，百炼原生 API 返回 `search_info.search_results`
- 占位域名黑名单，HTTP 实时 URL 验证
- MySQL URL 精确查重 + 本地 bge-m3 摘要语义相似度去重
- 飞书多维表 8 字段 schema + 省份归一化
- Webhook 双路径:写 MySQL 或跑主流程写飞书

## 数据库表结构(jd_rb_case 关键字段)

```sql
CREATE TABLE jd_rb_case (
    id           INT PRIMARY KEY AUTO_INCREMENT,
    title        VARCHAR(255),
    url          VARCHAR(500),
    url2         VARCHAR(500),
    summary      TEXT,
    publish_time DATETIME,
    author       VARCHAR(100),
    source       VARCHAR(20),
    info_type    VARCHAR(50),
    contentAddressNew VARCHAR(50),
    use_time     DATE          -- 永不入库,保持 NULL
);
```

## 许可

仅供内部使用。
