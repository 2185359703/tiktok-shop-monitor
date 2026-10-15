# 商品介绍采集与独立补全

商品介绍独立保存商品参数、有序文字、列表、详情图和测量表，供选品查看、关键词检索与人工同款核对使用。颜色/尺码 SKU 与商品测量表属于不同资料，不将测量表中的尺寸拼成平台 SKU。

本设计使用现有公开详情请求、队列、租约和限速机制，不增加第二套访问入口。数据库只增量增加 `product_contents`；图片与介绍补全不改商品价格、SKU、稳定业务哈希、已有快照或变化基线。

## 当前能确认的来源

来源为已校验美国站商品页面中的 `product_info.specifications` 和 `product_info.desc_blocks`。商品与店铺身份仍由正常详情解析器校验。采集商家原文，不从标题推断材质，不把 AI 摘要或买家评论混入正文。

| 内容 | 原先实现 | 本次设计 |
| --- | --- | --- |
| Details 参数 | 保存原始参数并归一少数服装属性 | 独立接口返回全部原始键值，保留重复及冲突 |
| 商家段落/列表 | 摊平成纯文本；不支持块会阻止更新旧描述 | 同时保存可搜索纯文本与有序安全节点，明确部分覆盖 |
| 图文顺序 | 不保存 | 按描述来源顺序保留文字、列表、表格与图片 |
| 测量表 | 可能丢失行列关系 | 标准 HTML 表格保留标题、单元格、表头和行列跨度；单位不猜测转换 |
| 详情长图 | image 块被跳过 | 标准 HTML 中可验证的 img 生成 description 图片节点；未知平台图片对象记录 partial |
| 商品相册 | 从 product_info.images 解析 | 仍独立保存在 product_images，不把长图塞入封面轮播 |
| Introduction / Q&A | 未核实为商家资料 | 本次不猜独立组件；若页面标为 AI 内容，不作为商家事实 |
| 买家评论、买家图片与个人资料 | 不采正文 | 保持排除，仅现有商品评分和评价数保留在业务资料 |

`complete` 仅表示上述两个已知来源字段的内容已按当前解析合同完整投影，不能解释成整张网页的所有组件均已采集。现有真实样本没有证实平台详情图片对象、原生尺码表或 Introduction 的完整字段结构。上线后应从同一次正常请求内保留白名单结构诊断，再针对实际形状增加支持；不为凑字段而填造数据。

## 有序内容合同

`app/crawlers/product_content.py` 提供：

```python
from app.crawlers.product_content import parse_product_content

projection = parse_product_content({
    "specifications": [{"name": "Fabric weight", "value": "Heavyweight"}],
    "desc_blocks": [{"type": "text", "text": "<p>Merchant coat.</p>"}],
})
assert projection.sections["description"] == [
    {"type": "paragraph", "text": "Merchant coat."}
]
```

顶层投影包含 `source_version`、`sections`、`description_text`、`completeness`、`unsupported_blocks`、`source_shape`。`sections` 分为 `specifications` 和 `description`，不会把规格键值混成介绍段落。

| 节点 | 字段 |
| --- | --- |
| paragraph | type、text |
| heading | type、text、level（1–6） |
| list | type、ordered 布尔值、items 字符串数组 |
| table | type、rows；单元格包含 text、header、rowspan、colspan |
| image | type、url、alt、role=description、width、height |

参数项包含 `name`、`value`、`source=specifications`。尺寸表的 inch/cm、标题与单元格原文保留；没有明确单位便保持未知。图片不能仅凭视觉被标成尺码图，当前通用角色为 `description`。此步骤不运行 OCR、不下载图片文件，不修改平台原始尺寸或标签。

解析器优先读取已证实的 text/content 文字字段。列表 content 为字符串数组。未知字段结构和未知块只记录位置、类型、原因与计数，不递归保存任意原站对象。只采能确认的内容；缺失值、空内容和不支持结构不会补成推测文案。

HTML 转为受限数据节点后才交给前端固定组件渲染，原站 HTML 不直接注入页面。脚本、样式、事件、表单、iframe 等可执行结构被剔除；图片拒绝凭据、私网地址和危险协议。标准 HTML 图片、表格与 caption 支持由合成合同用例验证，不能据此声称真实平台形状已经验证。

正文投影限制为 1,000,000 UTF-8 字节、最多 500 个原始描述块、每段文字最多 200,000 字符；HTML 最大 64 层嵌套、每段最多 5,000 个标签。超限只使可选介绍标为 partial，不将有效价格/SKU 详情误判为采集失败。

## 数据库与事务

`product_contents` 每个商品一行，唯一键为 `product_id`，外键指向 `products.id`。保存以下信息：

| 字段组 | 用途 |
| --- | --- |
| description_text、sections | 纯文本检索投影与可展示的安全结构 |
| completeness、unsupported_blocks、source_shape | 区分完整、部分、明确空与缺失；记录来源形状 |
| source_version、content_hash、content_version | 独立内容解析及变化版本，与 Product.data_version 分离 |
| source_url、response_hash、observed_at | 实际公开来源、响应证据摘要与观察时间 |

`persist_product_content(session, product_id, projection, *, source_url, response_hash, observed_at)` 验证受控投影、来源商品 ID、平台市场、时区与响应摘要，先锁 Product，再锁内容行，只写扩展表。调用方仍须校验响应与任务的店铺归属以及租约有效性。重复内容不新增重复行或递增内容版本；更旧观察不回退内容，同一时刻的冲突观察不覆盖已有结果。

已有非空内容时，新的 missing、empty 或 partial 响应保留旧记录，返回 `retained_existing_content`，失败和覆盖原因写入外层任务结果。第一次获取到部分资料可以保存并标为 partial。此策略优先保留此前证据，无法仅凭一个空白页面确认商家删除了介绍。

返回值包含 `content_saved`、`complete`、`completeness`、`source_version`、`content_nodes`、`content_parameters`、`unsupported_blocks`，供正常详情和显式补采共同写入检查点。`complete` 是本次响应的已知来源覆盖状态；与 `content_saved`、实际数据库覆盖数量应分别统计。

## 增量建表

离线预览不会读取连接凭据或连接数据库：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m scripts.migrate_product_content
```

在已配置的项目数据库检查或执行增量建表：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m scripts.migrate_product_content --check
.\.venv\Scripts\python.exe -X utf8 -m scripts.migrate_product_content --execute
```

该命令只创建缺失的 `product_contents`。如现有基础表缺失或模型定义漂移，命令停止，不执行 ALTER，不重新初始化采集库。所有新字段与表均有中文注释。`migrations/005_product_contents.sql` 与当前模型一致，历史 001/002 SQL 保持原定义。

## 读取与展示

`GET /api/v1/products/{product_id}/content` 使用已有市场作用域。不存在或不属于当前市场的商品返回 404。数据库内容扩展未启用时仍返回有效状态，不影响原商品详情：

```json
{
  "product_id": "7",
  "readiness": false,
  "available": false,
  "status": "pending_schema",
  "content": null
}
```

建表但尚未采集为 `readiness=true`、`status=not_captured`。存在记录时 `content` 返回 `description_text`、`sections`、`completeness`、`source_version`、`observed_at`、`unsupported_blocks`、`source_shape`、`source_url`、`content_hash`、`content_version`。`available` 表示存在可显示参数或描述节点，不等于完整。

前端进入商品资料区再读取此接口，商品参数、图文介绍和测量表按实际资料展示。不要将全量长正文加入工作台统计接口。照片采用远程公开链接与懒加载；链接失效时显示不可用，不使用演示图片替换真实资料。

## 补采接入

正常 PRODUCT_DETAIL 从同一次响应产生相册和内容投影。历史补采采用显式 `media_content_only`，由主处理器在同一次请求中调用相册与内容持久化，保留既有 `images_only` 语义。所有 Worker 升级并确认能力后再派发新模式，使用现有队列与请求闸门，不启动额外不受限抓取器。

新任务按商品、市场、模式和内容解析版本建立稳定去重键。已有相册不能作为介绍已经齐全的证据，跳过请求前检查需要补全的各项投影。统计分别列出参数、正文、长图、表格覆盖以及 partial/empty/missing，任务成功数量不能替代内容覆盖数量。

## 证据、结论与实施路径

范围为本项目已有公开业务投影的本地检查和实现，未额外访问 TikTok、VPS 或买家资料。文档采用普通技术说明结构（flavor=null）。

| Evidence | 本地来源与观察 |
| --- | --- |
| E-001 | `app/crawlers/product_detail.py` 旧 parse_description：image 跳过、文字摊平；`app/services/product_detail.py` 对 unsupported 块保留旧描述 |
| E-002 | `data/samples/product-detail/anrabess-source.json`：7 参数，原文本1250字符；SHA256 215d95f0d50d19b63f53e094a7f0b41503ee1f3466bc0f3dade99045fe349bce |
| E-003 | `data/samples/product-detail/dokotoo-source.json`：17 参数，原文本1069字符；SHA256 c28d3427be9035b66b77916d430de57ea756b8bfa983650de77c042175929cb0 |
| E-004 | `data/samples/product-detail/chouyatou-source.json`：27 参数、13描述块；旧解析有 unsupported=text，不能称完整；SHA256 8187706fd0203a672e1a0a8158d76ff4dfec1ecdc0a534bb2003a32ebc59ce7e |
| E-005 | `tests/fixtures/product_contents.json` 为上述样本只含 specifications、desc_blocks 的切片；SHA256 d380d28aef1414d08ac5b83fe0b2eb62cb09bd21ddb8dd4da30d7c5beee44142 |

Finding F-001（E-001 至 E-004）：旧实现已有部分介绍与参数，但不具备截图完整图文资料的覆盖证据。Finding F-002（E-005 与解析/API测试）：新的安全节点合同能保留已确认文字和参数、识别未知结构；标准 HTML 图表支持可离线验证，真实平台长图对象仍需实证。

Path P-001：现有一次详情请求 → 商品/店铺身份校验 → 已知字段的独立内容投影 → 租约内持久化 product_contents → 按需内容 API → 安全组件展示。后续真实结构不受支持时，保留 shape/partial 证据再扩展解析版本，不把未知节点解释为完整采集。

本地回归命令：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest tests/test_product_content.py tests/test_product_content_api.py tests/test_schema.py -q
```

MySQL 存储案例在项目既有隔离测试库机制下运行：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest tests/test_product_content_storage.py -q
```

本地未配置 MYSQL_TEST_URL 时，存储案例不会触碰业务数据库；真实 MySQL 的事务、旧观察保护、并发唯一键及回滚行为须由部署端隔离回归验证后再上线。
