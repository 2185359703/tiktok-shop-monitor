# 前端、商品图片与介绍部署

本次发布将 ShopScope 前端、分析接口、商品相册和结构化介绍放入同一个版本镜像。美国站服装全量采集继续使用原批次、两名 Worker、共享限速及 `g20261015` 队列代次。

## 访问与配置

- 目标主机：`google-vps`（34.20.217.151），项目目录 `/opt/tiktok-shop-monitor`。
- API 仅监听服务器 `127.0.0.1:8000`，本机通过 `deployment/open_tunnel.ps1 -ProxyPort 7897` 转发至 <http://127.0.0.1:18080/>。
- 服务器连接信息保存在受保护的 `deployment.env`，不进入代码、镜像或 Git。
- 应用使用 `deployment/compose.server.yml` 与 `deployment/compose.frontend.yml`；四个应用服务使用同一版本镜像。
- MySQL 在原 4 GiB 主机内分配 512 MiB 缓冲池与 1 GiB 容器上限。`deployment/tune_mysql_cache.py --execute` 可在线调整，保留数据库进程和数据卷。

## 发布顺序

1. `deployment/create_frontend_release.py --release-id frontend-...` 冻结白名单源码，生成逐文件摘要、压缩包与清单。冻结过程核对源文件没有被并发修改。
2. 在服务器从该目录构建版本镜像；`deployment/verify_runtime.py --image ... --source-root ... --log-name frontend-content-tests.log` 使用临时 MySQL 库与临时 Redis 执行回归。
3. 增量执行 `scripts.migrate_workbench --execute` 与 `scripts.migrate_product_content --execute`。只创建缺失扩展表，不 ALTER 原十张业务表。
4. `deployment/rollout_frontend.py --release-id ...` 更新 API、依次替换两名 Worker，再更新 Scheduler 和 Collection；不重建基础设施服务。
5. 核对运行镜像与 API 支持 `media_content_only` 后，先记录历史商品业务摘要，再执行下述补采。
6. 通过服务器页面检查工作台、列表、详情相册、资料页、字体及接口；记录最终发布清单与实际任务结果。

## 历史图片与介绍补采

```sh
python -m scripts.enqueue_image_backfill
python -m scripts.enqueue_image_backfill --execute --workers-ready
python -m scripts.enqueue_image_backfill --status
```

默认使用显式 `media_content_only`，一次公开详情请求同时补相册和商品介绍。`--workers-ready` 是部署者对实际运行镜像的能力检查；必须先更新所有 Worker。原 `images_only` 保持只写图库的含义。

任务只覆盖既有 100 家美国服装店内已成功采过详情、缺少相册或介绍投影的商品。稳定幂等键避免重复创建，不自动重开同版本的失败或无图任务。未完成的普通详情任务在一次正常采集中同步提取相册与介绍。

补采沿用已有重试、资源冷却、租约与共享访问频率，不增加并发 Worker。相册写入 `product_images`；介绍、完整参数和有序节点写入 `product_contents`。这两种补全不刷新价格、库存、SKU、商品业务版本、原成功时间或历史基线。介绍图片仅保留公开 URL，不保存二进制图片。

`GET /api/v1/images/backfill/status` 分别返回实际图库覆盖、内容记录数、完整度和持久任务状态。任务成功不等于有图，也不等于介绍完整。未知结构保留覆盖缺口；独立 Introduction、AI 问答与评论正文没有被混入商家介绍。详见 [商品介绍设计](product-content.md)。

## 回退与证据

发布清单、隔离测试结果、容器镜像检查和真实补采前后摘要保存在 `data/samples/deployment/`，该目录不进入 Git。旧版本镜像与数据卷保留。

补采入队后不能直接把 Worker 回退到不识别采集模式的旧版本，否则旧代码可能把补采当成完整详情。可以单独回退 API/前端；Worker 必须保留模式校验，或先按现有租约流程停止补采调度并处理待执行消息。不要清空原队列、重置尝试次数或重新导入数据库。
