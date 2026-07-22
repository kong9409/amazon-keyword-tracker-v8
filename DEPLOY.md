# Zeabur 部署步骤 · V7.1

1. 解压项目，把内部文件直接覆盖到 GitHub 仓库根目录。
2. 确认存在 `Dockerfile`、`app.py`、`provider_adapter.py`、`sorftime_adapter.py`、`lark_writer.py` 和 `static/`。
3. Zeabur Variable 中不要手工创建 `PORT`、`HOST`、`APP_MODE`、`python app.py` 或任何公共数据源 Key。
4. 在 Volumes 中挂载持久化卷到 `/app/data`，用于每日 09:00 任务、加密凭证及定时 Excel。
5. 点击 Redeploy。
6. 打开 `/api/health`，确认 `ok: true`、`supports_daily: true`。
7. 回到首页，STEP 1 查看 18 个需求字段与接口匹配；STEP 2 选择 Sorftime、卖家精灵、SIF、西柚洞察或其他软件并输入自己的凭证。

## 连接要求

- Sorftime CLI：容器已安装 `sorftime-cli`，用户只输入 Account-SK。
- Sorftime、SIF、西柚和其他 MCP：必须填写 Zeabur 可访问的公网 HTTPS MCP URL。
- 卖家精灵：官方 MCP URL 已内置，只填写 MCP Key。
- 数据源凭证不应配置成全站公共 Zeabur 环境变量。

## 部署后检查

- STEP 1 显示 18 个字段，并且“小类排名”位于“大类排名”后。
- 切换西柚 MCP 后，广告位显示 `get_asin_keyword_rank_trends`，月销量显示 `get_asin_order_trends · 当月销量`。
- 完成一次抓取后，Excel 的“流量占比”列显示百分比两位小数。
