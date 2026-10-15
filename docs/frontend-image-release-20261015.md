# 2026-10-15 前端与历史图片补采发布记录

服务器：`google-vps`（34.20.217.151），目录 `/opt/tiktok-shop-monitor`。

公网入口：<http://34.20.217.151/shopscope/#products>。复用服务器现有 Nginx 的 **80 端口**，浏览器访问不依赖本机 SSH 转发。

原本机入口 <http://127.0.0.1:18080/#products> 仍可用于运维检查。API 继续监听服务器 `127.0.0.1:8000`，由 Nginx 转发 `/shopscope/`。

## 实际部署组成

| 部分 | 发布版本 |
| --- | --- |
| API、两台 Worker、Scheduler、Collection 基础镜像 | `frontend-20261015-images-verified-c7a93e14` |
| 基础镜像摘要 | `sha256:8d03c0f175c854d63b6743622f1bb8f8c6bda2b78dc12db75488838b712d07d0` |
| 前端只读目录 | `releases/frontend-ui-public-20261015-f7c3102a/app/frontend` |
| 状态查询只读修复 | `releases/frontend-status-20261015-f284a6c1/app/services/image_backfill.py` |

运行命令同时使用 `deployment/compose.server.yml` 与 `deployment/compose.frontend.yml`。API 有两个显式只读覆盖：前端目录和补采状态模块。下一次完整镜像更新时需要同步更新或取消这两个覆盖，避免覆盖新镜像内的文件。

旧镜像、任务尝试、检查点、采集卷和 MySQL 数据保留。原 `g20261015` 队列继续工作，停用详情队列仍为 541,025 条通知，没有清空。原 Sub2API、PostgreSQL、Redis 和本项目基础设施检查正常。服务器根分区约 29 GiB，现场剩余约 15 GiB。

数据库增量创建图库、两张匹配表和商品介绍表，当前共十四张业务表；两次扩展迁移均返回结构无漂移，没有改动原十张表。

## 公网入口

前端根据自身模块路径识别部署前缀，静态资源、字体、接口请求、报告预览与下载、接口文档链接兼容根路径及 `/shopscope/`。本次仅调整访问路径，没有改动界面布局和文案。前缀路径验证覆盖八十个断言。

`deployment/nginx-shopscope.conf` 通过独立 include 接入原 Sub2API 虚拟主机：`/shopscope` 跳转到带尾斜杠的入口，`/shopscope/` 转发到本项目，原 `/` 继续服务 Sub2API。无需新增公网端口或开放数据库与消息队列端口。

`deployment/apply_public_entry.py` 保存站点配置备份，先执行 `nginx -t` 再平滑重载，检查失败则恢复原配置。Sub2API 每次响应会生成新的 CSP nonce，首页对照仅排除该属性，仍对页面正文、脚本和链接进行完整摘要比较。

公开 Swagger 文档为 <http://34.20.217.151/shopscope/docs>，其 OpenAPI 声明使用 `/shopscope` 作为服务前缀。接口结构变更后需要重新运行入口脚本以更新 `deployment/public-openapi.json`。

## 补采范围与实际结果

已经创建 **18,840 条** `media_content_only` 持久任务，覆盖既有一百家美国站服装店中需要补全图片或介绍的历史详情商品。同一次公开详情请求保存相册和商品介绍，不刷新价格、库存、SKU、商品版本、原详情成功时间或历史基线。

两台 Worker 复用原请求闸门、重试和资源冷却，真实请求最小间隔仍为两秒。新完成的普通详情任务同时保存相册与介绍。图片仅保留公开 URL，没有批量下载二进制图片。

现场检查已确认实际成功补采和真实图片显示，后续数量继续变化。图库覆盖、成功、无图、重试与终止失败分别保留，不将任务成功数当成图片覆盖数。完整状态见 `/api/v1/images/backfill/status` 及 `data/samples/deployment/frontend-final-check.json`。

## 验证

- 基础镜像在独立 MySQL 测试库和临时 Redis 中 **391 项通过，无跳过**。
- 状态查询修复在同一镜像与隔离数据库中 **12 项通过**。真实执行计划使用商品驱动和任务资源索引的完整四列，现场返回约 17.3 秒，无需新增索引。
- 前端十八个资源进行 HTTP 字节摘要对照；实际页面验证一百家店铺筛选、类目筛选、商品列表、相册切换、报价、SKU、介绍与详细参数。
- 公网 `/shopscope/` 的十八个资源再次全部匹配冻结摘要；从 Windows 禁用系统代理直接请求七个公网路径，确认入口跳转、健康、数据接口、文档和原首页正常，没有使用 SSH 转发。浏览器实测 Halara US 筛选、网格图片及八张相册的商品详情。
- 真实抽查九十四个已完成补采的历史商品，价格、版本、成功时间和原 SKU 记录摘要与采集前一致。

前后摘要使用保存基线时的 v1 投影算法。后来的工具增加了描述、原始属性和更多 SKU 字段，不能直接与旧摘要比较；`scripts/verify_image_backfill_legacy.py` 保留原算法进行实际复核。`image-business-after.json` 是算法不一致的无效对照，正确证据为 `image-business-after-v1.json`，未据此回写业务数据。

证据保存在本地和服务器的 `data/samples/deployment/`：`frontend-image-tests.log`、`frontend-status-tests.log`、`frontend-final-check.json`、`image-backfill-start.json`、`image-status-query-check.json`、`frontend-ui-http-check.json` 及页面截图。发布源码逐文件摘要和独立压缩包也保留在该目录。

公网检查补充 `public-entry.json`、`public-http-check.json`、`public-api-check.json`、`public-shop-products.png` 与 `public-product-detail.png`。入口脚本的原始证据位于服务器 `data/samples/deployment/public-entry/`，站点配置备份仅在该受控目录保留。外网检查工具为 `deployment/verify_public_entry.py`。

## 当前限制

历史补采和原全量采集仍在后台运行，存在真实重试与终止失败，未宣称全部完成。部分商品介绍明确标为部分内容。旧规格标签按已存数据展示，未在补图时重新解释或回写。

当前按种子店铺采集，混合品类店铺也包含非服装商品；服装分析需要使用店铺、类目或搜索筛选，不将未筛选总量当作纯服装市场样本。

全市场工作台首次聚合仍较慢，本轮没有将其作为通过的性能验收；商品列表、筛选、详情、图片和介绍已完成真实页面验收。后续统计性能优化应继续使用隔离副本，避免与店铺档案开发并行覆盖共享文件。
