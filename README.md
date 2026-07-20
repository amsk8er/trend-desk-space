# AI Builder Space 部署说明

本目录记录 Trend Desk 在 `space.ai-builders.com` 上的容器部署约定。

## 运行方式

- 平台从公开 GitHub 仓库构建根目录的 `Dockerfile`。
- FastAPI 与构建后的 React 共用一个容器、一个端口。
- 容器必须监听平台注入的 `PORT`。
- 平台自动注入 `AI_BUILDER_TOKEN`。
- 在线截图识别自动使用平台 OpenAI 兼容接口的 `kimi-k2.5` 视觉模型。

## 私有访问

服务本身拥有公开网址，但交易数据接口默认受登录门保护：

- 用户在网页输入自己的 AI Builder Space Access Key。
- 后端只校验密钥，不把密钥保存到浏览器存储。
- 校验成功后签发带签名的 `HttpOnly` Cookie。
- 除健康检查和登录接口外，所有 `/api/*` 请求均需已登录会话。

## 数据与密钥

- 发布仓库不得包含 `data/`、`secrets.env`、券商截图、账户数据或日志。
- `AI_BUILDER_TOKEN` 只由平台在服务端注入。
- Trend Animals、Tushare 等额外数据源仍需在托管服务端单独配置密钥。

## 当前持久化限制

应用仍使用 SQLite。AI Builder Space 的容器文件系统不应被视为持久磁盘：

- 重新部署或容器重建后，在线录入的数据可能丢失。
- 初次部署适合验证页面、登录保护和 OCR。
- 长期使用前应将业务数据迁移到外部持久数据库，并保留定期备份。

## 发布验收

1. `/api/health` 返回 `{"ok": true}`。
2. 未登录访问业务 API 返回 401。
3. 用 Space Access Key 登录后，交易台、复盘和数据与规则页面可以打开。
4. 使用无敏感信息的合成截图验证 OCR，并确认账户金额与持仓解析。
5. 不把真实券商截图或账户数据写入公开仓库或部署日志。
