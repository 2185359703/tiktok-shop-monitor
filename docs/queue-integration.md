# 真实 RabbitMQ 联调

2026-10-14，使用用户已启动的 Docker 引擎和独立 `rabbitmq:4.1-management` 容器，完成六项真实队列检查，并通过同一 Broker 执行业务库服装报告任务。联调没有访问 TikTok、没有批量启动剩余商品任务。

## 六项实际验证

| 检查 | 实际动作 | 结果 |
| --- | --- | --- |
| 报告完整链路 | Scheduler 扫描 SQL 任务、confirm 发布、Worker 执行、提交后 ACK | 报告成功，检查点与文件可读取 |
| 重复投递 | 对同一任务发布两次真实消息 | 只有一次执行尝试 |
| 断线重投 | 收到消息后在 ACK 前关闭 AMQP 连接 | 重连后收到 redelivered 消息 |
| Broker 重启 | 已确认发布持久消息后实际重启验证容器 | 消息保留并可消费 |
| 租约故障恢复 | 子进程领取真实 SQL 租约并持有未确认消息，随后被终止 | 消息重投；真实租约过期后恢复，第二次尝试成功 |
| 失败死信 | 提交无效报告类型，Worker 先记录 dead 再拒绝消息 | 实际死信队列收到对应任务 UUID |

租约检查使用 3 秒租约和 1 秒重试退避，以真实时间等待过期，不手工改数据库时间制造恢复结果。子进程是持有消息和租约的故障探针；恢复任务由实际 Scheduler/Worker 处理。该检查不等同于完整业务 Worker 容器的停机部署演练。

脱敏现场证据在 `data/samples/broker-check/50e41a2e331c/verification.json`，六个 checks 均为 true。使用了一份此前成功公开 PDP 投影，在隔离库中验证 1 个商品、90 个 SKU 的正向报告；这份隔离样本没有写入业务库。

最后一次专项检查于 `2026-10-14T10:20:13.492575+00:00` 附近完成，最终业务报告于北京时间 18:23 成功。196 项完整回归、9 项报告 HTTP 核查和 10 项既有只读接口核查均通过。`scripts.check_runtime` 是只读 HTTP/数据库检查，其 Broker 标识仅表示该脚本没有检查 Broker；真实 Broker 结果以本节 verification.json 为准。

## 隔离与清理

- MySQL 使用独立 `127.0.0.1:33487` 实例，新库名限定 `tiktok_shop_monitor_test_queue_` 加唯一后缀；拒绝已有库，结束只删除本次创建的库。
- Redis 使用独立 `127.0.0.1:36387` 实例，清理仅删除本次唯一前缀，不执行 FLUSHDB。
- Broker 使用唯一容器名、项目标签、随机密码与独立 vhost，AMQP 只映射 `127.0.0.1:5679`。临时配置位于忽略目录并限制本地文件权限，不输出连接串或密码。
- 重启和清理前同时校验保存的完整 Docker ID、项目标签及目录范围，不操作用户其他容器或卷。
- 联调结束移除临时容器、匿名数据卷和本次临时密码文件，保留验证 JSON、导出报告和镜像。用户 Docker 引擎与业务 MySQL 保持运行。

## 再次运行

先准备上述端口的专用隔离 MySQL 和 Redis，并保证它们不承载业务数据。Docker 必须可用，临时端口 5679 不能占用。下面的命令创建自己的 Broker；不会复制业务库凭据到容器。

```powershell
$brokerCheck = .\.venv\Scripts\python.exe -X utf8 -m scripts.prepare_broker_check | ConvertFrom-Json
$brokerMetadata = $brokerCheck.metadata
.\.venv\Scripts\python.exe -X utf8 -m scripts.verify_queue_runtime --metadata $brokerMetadata --detail-evidence 'data/samples/runtime/2fd2a545-0139-4323-85dc-c1313aa2a4cd/3d3c223af89c47c1fcafdd951205b8dd9c81b074836cfaf343d7027d620bafcd.json'
```

detail-evidence 是本地忽略的历史实跑证据，当前机器已有；新机器没有该文件时可省略此参数，六项队列机制仍可检查，但不会有商品正向样本。Broker 初次启动需要就绪等待；验证脚本有界重连，不要求手工修改任务状态。

结束后清理自己的验证容器：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m scripts.cleanup_broker_check --metadata $brokerMetadata
```

清理脚本不关闭 Docker 引擎，不清理业务数据库或其他容器。隔离 MySQL/Redis 是独立进程，需要启动者根据实际数据目录和端口核对后停止；不要关闭 `127.0.0.1:3306` 的业务 MySQL。

## 真实业务报告

队列故障验证使用隔离库；实际服装报告另通过 `scripts.run_report_job` 读取现有业务库并生成报告。完成任务的 checkpoint 包含三个文件绝对路径、report_hash、report_as_of、report_products 和 report_events。相同窗口再次调用复用原任务和文件，不增加执行尝试。

实际报告为 120 个商品、268 个最新 SKU、1 条真实事件，见[报告说明](monitoring-reports.md)。下载接口可依据任务 UUID 取回三种文件；现场核查在 `data/samples/report-check/api-check.json`，原有商品、SKU、历史及事件数据保留。

任务状态以 MySQL 为准，Broker 只发送版本、任务 UUID 和类型。发布 confirm 与消费 ACK 分开处理；SQL 提交后才 ACK，终止失败落为 dead 后再进入死信。断线和重启可能重复通知，因此使用租约领取及业务幂等，不能宣称 exactly-once。

## 尚未验收的范围

本次运行的是一个实际 Broker 与有限故障场景，不是 Broker 集群、高可用或吞吐基准。没有启动完整 Compose 栈，没有进行多 Worker 压测、长期周期监测或全店采集。正常运行仍需持久配置项目 RabbitMQ/Redis 连接，临时联调密码已在清理时移除。

Evidence → Finding → Path：六项真实 Broker 故障检查 + 真实业务报告任务 + 幂等重放 → 已验证范围内消息重投不会重复执行业务，租约故障可恢复且失败可追溯 → 下一步用有限监测名单验证持续运行、刷新时延、积压与服务重启后的配置恢复。
