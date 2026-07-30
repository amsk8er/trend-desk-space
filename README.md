# Cindy's Space

`cindyzhang.ai-builders.space` 的公开组合部署仓。

- `/`：Cindy 项目主页与各静态项目入口。
- `/trend-desk/`：从私有 `trend-desk/main` 脱敏同步的生产运行时。
- `/deployment-manifest.json`：本次 Trend Desk 对应的源 commit 与运行时树摘要。

Trend Desk 不是在本仓库中独立开发。每次发布必须从私有主仓的已提交
`origin/main` 运行 `scripts/sync_cindy_portal.py`，审核路径级差异、通过完整测试
后再合并到 `master`。不要手工复制数据库、截图、日志、密钥或私有部署配置。

AI Builder 服务名保持 `cindyzhang`，容器监听平台注入的 `PORT`；门户根路径公开，
交易台继续由独立访问密钥保护。
