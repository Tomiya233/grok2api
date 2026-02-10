# Grok2API

基于 **FastAPI** 的 Grok2API，适配最新 Web 调用格式，支持流式对话、图像生成、图像编辑、联网搜索、深度思考，集成号池并发与自动负载均衡。

## ✨ 核心功能

当前版本提供以下能力：

- **多 Key 管理与持久化**：支持管理员批量创建、备注、删除 API Key，支持多选批量操作。所有密钥均实现持久化存储，重启不丢失。
- **日志审计支持**：实时记录请求细节，且日志数据支持文件持久化存储。
- **并发性能优化 (Critical)**：重构了底层的 Grok 请求和响应处理逻辑。采用全异步流式迭代 (`aiter_lines`)，彻底解决了在消息生成过程中后台管理面板“卡死”或响应缓慢的问题。
- **Token 智能冷却**：请求失败后自动冷却，避免连续使用故障 Token
  - 普通错误：冷却 5 次请求
  - 429 限流 + 有额度：冷却 1 小时
  - 429 限流 + 无额度：冷却 10 小时
- **一键刷新所有 Token**：后台按钮批量刷新剩余次数，带实时进度显示
- **并发保护**：刷新任务进行中自动拒绝重复请求
- **请求统计与持久化**：按小时/天统计请求趋势，包含成功率和模型分布图表，统计数据支持持久化。
- **缓存预览**：后台添加缓存预览板块，可查看缓存的图片/视频
<br>

## 使用说明

### 调用次数与配额

- **普通账号（Basic）**：免费使用 **80 次 / 20 小时**
- **Super 账号**：配额待定（作者未测）
- 系统自动负载均衡各账号调用次数，可在**管理页面**实时查看用量与状态

### 图像生成功能

- 在对话内容中输入如“给我画一个月亮”自动触发图片生成
- 每次以 **Markdown 格式返回两张图片**，共消耗 4 次额度
- **注意：Grok 的图片直链受 403 限制，系统自动缓存图片到本地。必须正确设置 `Base Url` 以确保图片能正常显示！**

### 视频生成功能
- 选择 `grok-imagine-0.9` 模型，传入图片和提示词即可（方式和 OpenAI 的图片分析调用格式一致）
- 返回格式为 `<video src="{full_video_url}" controls="controls"></video>`
- **注意：Grok 的视频直链受 403 限制，系统自动缓存图片到本地。必须正确设置 `Base Url` 以确保视频能正常显示！**

```
curl https://你的服务器地址/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $GROK2API_API_KEY" \
  -d '{
    "model": "grok-imagine-0.9",
    "messages": [
      {
        "role": "user",
        "content": [
          {
            "type": "text",
            "text": "让太阳升起来"
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "https://your-image.jpg"
            }
          }
        ]
      }
    ]
  }'
```

### 关于 `x_statsig_id`

- `x_statsig_id` 是 Grok 用于反机器人的 Token，有逆向资料可参考
- **建议新手勿修改配置，保留默认值即可**
- 尝试用 Camoufox 绕过 403 自动获 id，但 grok 现已限制非登陆的`x_statsig_id`，故弃用，采用固定值以兼容所有请求

<br>

## 如何部署

### 方式一：Docker Compose（推荐）

建议直接本地构建运行：

1. 克隆本仓库
```bash
git clone https://github.com/Tomiya233/grok2api.git
cd grok2api
```

2. 启动服务
```bash
docker-compose up -d --build
```

**docker-compose.yml 参考：**
```yaml
services:
  grok2api:
    build: .
    image: grok2api:latest
    container_name: grok2api
    restart: always
    ports:
      - "8000:8000"
    volumes:
      - grok_data:/app/data
      - ./logs:/app/logs
    environment:
      - LOG_LEVEL=INFO
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"

volumes:
  grok_data:
```

### 方式二：Python 直接运行

**前置要求**：Python 3.11+，建议使用 `uv` 包管理器

1. 安装 uv
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

2. 运行服务
```bash
# 安装依赖并运行
uv sync
uv run python main.py
```

服务默认运行在 `http://127.0.0.1:8000`

### 环境变量说明

| 环境变量      | 必填 | 说明                                    | 示例 |
|---------------|------|-----------------------------------------|------|
| STORAGE_MODE  | 否   | 存储模式：file/mysql/redis               | file |
| DATABASE_URL  | 否   | 数据库连接URL（MySQL/Redis模式时必需）   | mysql://user:pass@host:3306/db |

**存储模式：**
- `file`: 本地文件存储（默认）
- `mysql`: MySQL数据库存储，需设置DATABASE_URL
- `redis`: Redis缓存存储，需设置DATABASE_URL

<br>

## 接口说明

> 与 OpenAI 官方接口完全兼容，API 请求需通过 **Authorization header** 认证

| 方法  | 端点                         | 描述                               | 是否需要认证 |
|-------|------------------------------|------------------------------------|------|
| POST  | `/v1/chat/completions`       | 创建聊天对话（流式/非流式）         | ✅   |
| GET   | `/v1/models`                 | 获取全部支持模型                   | ✅   |
| GET   | `/images/{img_path}`         | 获取生成图片文件                   | ❌   |

<br>

<details>
<summary>管理与统计接口（展开查看更多）</summary>

| 方法  | 端点                    | 描述               | 认证 |
|-------|-------------------------|--------------------|------|
| GET   | /login                  | 管理员登录页面     | ❌   |
| GET   | /manage                 | 管理控制台页面     | ❌   |
| POST  | /api/login              | 管理员登录认证     | ❌   |
| POST  | /api/logout             | 管理员登出         | ✅   |
| GET   | /api/tokens             | 获取 Token 列表    | ✅   |
| POST  | /api/tokens/add         | 批量添加 Token     | ✅   |
| POST  | /api/tokens/delete      | 批量删除 Token     | ✅   |
| GET   | /api/settings           | 获取系统配置       | ✅   |
| POST  | /api/settings           | 更新系统配置       | ✅   |
| GET   | /api/cache/size         | 获取缓存大小       | ✅   |
| POST  | /api/cache/clear        | 清理所有缓存       | ✅   |
| POST  | /api/cache/clear/images | 清理图片缓存       | ✅   |
| POST  | /api/cache/clear/videos | 清理视频缓存       | ✅   |
| GET   | /api/stats              | 获取统计信息       | ✅   |
| POST  | /api/tokens/tags        | 更新 Token 标签     | ✅   |
| POST  | /api/tokens/note        | 更新 Token 备注     | ✅   |
| POST  | /api/tokens/test        | 测试 Token 可用性   | ✅   |
| GET   | /api/tokens/tags/all    | 获取所有标签列表    | ✅   |
| GET   | /api/storage/mode       | 获取存储模式信息    | ✅   |
| POST  | /api/tokens/refresh-all | 一键刷新所有Token   | ✅   |
| GET   | /api/tokens/refresh-progress | 获取刷新进度       | ✅   |
| GET   | /api/keys               | 获取 API Key 列表  | ✅   |
| POST  | /api/keys/add           | 创建新 API Key     | ✅   |
| POST  | /api/keys/delete        | 删除 API Key       | ✅   |
| POST  | /api/keys/status        | 切换 Key 启用状态  | ✅   |
| POST  | /api/keys/name          | 修改 Key 备注名称  | ✅   |
| GET   | /api/logs               | 获取请求日志(1000条)| ✅   |
| POST  | /api/logs/clear         | 清空所有审计日志   | ✅   |

</details>

<br>

## 可用模型一览

| 模型名称               | 计次   | 账户类型      | 图像生成/编辑 | 深度思考 | 联网搜索 | 视频生成 |
|------------------------|--------|--------------|--------------|----------|----------|----------|
| `grok-4`               | 1      | Basic/Super  | ✅           | ✅       | ✅       | ❌       |
| `grok-4.1-thinking`    | 1      | Basic/Super  | ✅           | ✅       | ✅       | ❌       |
| `grok-imagine-0.9`     | -      | Basic/Super  | ✅           | ❌       | ❌       | ✅       |
| `grok-4-fast`          | 1      | Basic/Super  | ✅           | ✅       | ✅       | ❌       |
| `grok-4-fast-expert`   | 4      | Basic/Super  | ✅           | ✅       | ✅       | ❌       |
| `grok-4-expert`        | 4      | Basic/Super  | ✅           | ✅       | ✅       | ❌       |
| `grok-4-heavy`         | 1      | Super        | ✅           | ✅       | ✅       | ❌       |
| `grok-3-fast`          | 1      | Basic/Super  | ✅           | ❌       | ✅       | ❌       |

<br>

## 配置参数说明

> 服务启动后，登录 `/login` 管理后台进行参数配置

| 参数名                     | 作用域  | 必填 | 说明                                    | 默认值 |
|----------------------------|---------|------|-----------------------------------------|--------|
| admin_username             | global  | 否   | 管理后台登录用户名                      | "admin"|
| admin_password             | global  | 否   | 管理后台登录密码                        | "admin"|
| log_level                  | global  | 否   | 日志级别：DEBUG/INFO/...                | "INFO" |
| image_mode                 | global  | 否   | 图片返回模式：url/base64                | "url"  |
| image_cache_max_size_mb    | global  | 否   | 图片缓存最大容量(MB)                     | 512    |
| video_cache_max_size_mb    | global  | 否   | 视频缓存最大容量(MB)                     | 1024   |
| base_url                   | global  | 否   | 服务基础URL/图片访问基准                 | ""     |
| api_key                    | grok    | 否   | API 密钥（可选加强安全）                | ""     |
| proxy_url                  | grok    | 否   | HTTP代理服务器地址                      | ""     |
| stream_chunk_timeout       | grok    | 否   | 流式分块超时时间(秒)                     | 120    |
| stream_first_response_timeout | grok | 否   | 流式首次响应超时时间(秒)                 | 30     |
| stream_total_timeout       | grok    | 否   | 流式总超时时间(秒)                       | 600    |
| cf_clearance               | grok    | 否   | Cloudflare安全令牌                      | ""     |
| x_statsig_id               | grok    | 是   | 反机器人唯一标识符                      | "ZTpUeXBlRXJyb3I6IENhbm5vdCByZWFkIHByb3BlcnRpZXMgb2YgdW5kZWZpbmVkIChyZWFkaW5nICdjaGlsZE5vZGVzJyk=" |
| filtered_tags              | grok    | 否   | 过滤响应标签（逗号分隔）                | "xaiartifact,xai:tool_usage_card,grok:render" |
| show_thinking              | grok    | 否   | 显示思考过程 true(显示)/false(隐藏)     | true   |
| temporary                  | grok    | 否   | 会话模式 true(临时)/false               | true   |

<br>

## ⚠️ 注意事项

本项目仅供学习与研究，请遵守相关使用条款！
