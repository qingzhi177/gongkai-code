# Memory System v1.0

AI 长期记忆系统，基于 Paramecium 架构。

## 功能
- L0 对话原文存储（带版本管理）
- L1 记忆索引（DeepSeek 提取，保留语感）
- L2 人格画像（关于用户/AI/我们）
- recall/feel/update_profile 工具（AI 自主调用）
- BM25 + 向量 RRF 融合检索
- 意图路由（时间/事实/情感/语义）
- Web Dashboard（管理面板）
- 自动备份（GitHub）

## 部署
1. 安装 Docker/Node.js/Python3/nginx
2. docker compose up -d（启动 ChromaDB）
3. 记忆服务：cd services/memory-service && python -m venv venv && source venv/bin/activate && pip install -r requirements.txt
4. 网关：cd services/gateway && npm install
5. 复制 .env.example 为 .env，填入 API Key
6. 配置 systemd 服务 + nginx 反向代理 + certbot SSL
7. 配置 cron 定时 L1 提取（每10分钟）

## 文件结构
- docker-compose.yml：ChromaDB 容器
- backup.sh：自动备份脚本
- services/memory-service/：Python 记忆服务
- services/gateway/：Node.js 网关
- services/dashboard/：前端管理面板
- data/：数据目录（不入 Git）
