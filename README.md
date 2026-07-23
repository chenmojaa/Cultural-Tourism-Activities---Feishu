# 文旅案例自动化

> 让 LLM 联网搜索,自动收集近 3 个月内的真实文旅活动/项目案例,写入飞书多维表格和 MySQL。

## 流程概览

```
┌─────────────────┐  ┌──────────────────┐  ┌──────────────────┐
│ 飞书工作流按钮   │→│ webhook_server   │→│ main.py 主流程   │
│ (发送HTTP请求)   │  │ (13001 端口)     │  │                  │
└─────────────────┘  └──────────────────┘  └────────┬─────────┘
                                                   │
                            ┌──────────────────────┼──────────────────────┐
                            ▼                      ▼                      ▼
                    ┌──────────────┐    ┌──────────────────┐   ┌──────────────────┐
                    │ 步骤 1       │    │ 步骤 2           │   │ 写入飞书         │
                    │ 千问 推荐案例│ →  │ 千问 联网搜索     │ → │ Bitable          │
                    │ (NAMES only) │    │ + 300字总结+URL  │   │ add_rows()       │
                    └──────────────┘    └──────────────────┘   └──────────────────┘
```

## 关键特性

- **LLM 联网搜索真实 URL** — 步骤 2 强制开 web_search,百炼原生 API 返回 `search_info.search_results`,直接拿真实 URL 写库
- **占位域名黑名单** — `example.com` / `localhost` / `127.0.0.1` 等假 URL 一律丢弃
- **HTTP 实时 URL 验证** — 采纳前 HEAD/GET 验证可访问性,过滤 404/SSL 失败
- **MySQL 查重** — URL 精确 + 本地 bge-m3 摘要语义相似度,默认 0.85 阈值
- **飞书多维表写入** — 9 字段 schema(title/url/summary/source/author/...)+ 省份归一化
- **Webhook 双路径** — `POST /wenlv_db`(写 MySQL)+ `POST /wenlv_feishu`(跑主流程写飞书)

## 项目结构

```
.
├── main.py              # 主流程:LLM 推荐 → 联网搜索 + 总结 → 写飞书
├── llm_client.py        # 千问客户端:Chat Completions / Responses / 原生 API 三路由
├── prompts.py           # 极简提示词:联网搜索后直接给数据
├── utils.py             # 重试 / URL 验证 / JSON 容错解析
├── webhook_server.py    # 13001 端口 HTTP 服务,接飞书工作流
├── mysql_client.py      # jd_rb_case 查重表 + 写入(use_time 字段永不入库)
├── feishu_client.py     # 飞书 Bitable OpenAPI
├── dedup.py             # URL 精准 + bge-m3 摘要语义去重
├── data_store.py        # 飞书写入(date→毫秒时间戳)
├── modeltest.py         # 本地 bge-m3 测试桩(调用 /v1/score)
├── config.py            # 本地凭证(已 .gitignore)
├── config.example.py    # 占位模板(可入仓)
├── requirements.txt     # 依赖
└── .gitignore           # 排除 config.py / .venv / 日志 等
```

## 快速开始

### 1. 克隆并安装依赖

```bash
git clone https://github.com/chenmojaa/文旅案例自动化.git
cd 文旅案例自动化
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 2. 配置凭证

```bash
cp config.example.py config.py
# 编辑 config.py,填入真实 API_KEY / MySQL 密码 / 飞书凭证
```

或者用环境变量注入(推荐,免改 config.py):

```bash
# Windows PowerShell
$env:QWEN_API_KEY="sk-xxx"
$env:MYSQL_PASSWORD="your-password"
$env:FEISHU_APP_SECRET="your-secret"

# Linux / macOS
export QWEN_API_KEY="sk-xxx"
export MYSQL_PASSWORD="your-password"
export FEISHU_APP_SECRET="your-secret"
```

### 3. 启动 Webhook 服务

```bash
python webhook_server.py
# 默认监听 http://0.0.0.0:13001
```

### 4. 飞书工作流配置

- 创建"发送 HTTP 请求"节点,目标 URL = `http://10.0.0.110:13001/timingTasks/wenlv_feishu?token=你的token`
- 请求方法 = POST
- 请求头 = `Content-Type: application/json`
- Body 里带 `data_id`(飞书记录 ID)用于回显

### 5. 触发主流程

```bash
# 手动触发(测试用)
curl -X POST "http://127.0.0.1:13001/timingTasks/wenlv_feishu?token=你的token" \
     -H "Content-Type: application/json" \
     -d '{"data_id":"001","name":"测试"}'
```

## API 端点

| 路径 | 方法 | token 鉴权 | 说明 |
|---|---|---|---|
| `/health`, `/ping` | GET/HEAD | 免 | 健康检查 |
| `/` | GET/HEAD | 免 | 服务描述 |
| `/timingTasks/wenlv_feishu` | POST | 需要 | 触发主流程(LLM 采集 + 写飞书) |
| `/timingTasks/wenlv_db` | POST | 免 | 接收多维表数据写 MySQL |
| `/timingTasks/wenlv_feishu?status=<id>` | GET/HEAD | 免 | 查询任务状态(只读) |

## 配置说明

`config.py` 关键参数:

| 参数 | 默认 | 说明 |
|---|---|---|
| `QWEN_MODEL` | `qwen3.6-max-preview` | 千问模型名 |
| `QWEN_ENABLE_SEARCH` | `1` | 是否开联网搜索 |
| `TARGET_COUNT` | `40` | 单次跑要采集多少条 |
| `MAX_RETRY_ROUNDS` | `3` | 查重后条数不够时的最大补生成轮数 |
| `PARALLEL_WORKERS` | `8` | 步骤 2 并发线程数 |
| `DEDUP_LOOKBACK_DAYS` | `120` | MySQL 查重时间窗(最近 N 天) |
| `VALIDATE_URLS` | `1` | 是否 HTTP 验证 URL 可达性 |

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
