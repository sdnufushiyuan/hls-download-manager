# 按日期与轨道/瓦片输出单个 TIFF

分块用于可靠下载与断点；特征空间分析使用独立的按日整图输出。程序读取已完成月份的原有块，在本地按窗口拼接成**实际六波段 GeoTIFF**，不是VRT。原始块、完成记录、库存和下载任务均不改动。仅为缺失的来源标识向EE查询小批元数据，结果缓存；不重新下载栅格。

## 分组口径

| 产品 | 输出一张TIFF的分组键 | 文件名示例 |
|---|---|---|
| HLS L30 | UTC观测日期 + 来源Landsat的WRS path/row | `RIS_L30_20130428_P032_R025.tif` |
| HLS S30 | UTC观测日期 + MGRS瓦片号 | `RIS_S30_20170310_T14UNA.tif` |

两种产品分开。同日不同path/row或不同MGRS目标瓦片分别输出。HLS的`T14UNA`等编号是MGRS网格，不能解释成WRS path/row；L30从官方`LANDSAT_PRODUCT_ID`提取WRS和观测日期，再核对时间戳。缺失来源、日期冲突或同一HLS资产包含多个不同WRS行列时，该月份暂不交付并写入阻塞原因，不猜测或将整块重复归给两个来源。[L30目录](https://developers.google.com/earth-engine/datasets/catalog/NASA_HLS_HLSL30_v002)、[S30目录](https://developers.google.com/earth-engine/datasets/catalog/NASA_HLS_HLSS30_v002)。

输出范围是**该研究区与目标来源覆盖的交集**，不是重新获取ROI外的整幅原始卫星影像。现有数据已经经过晴空陆地筛选；NoData不会被插值或补成有效像元。全云/无有效像元的日图仍可保存，但JSON中的`valid_pixels=0`，不能用来拟合特征空间。

## 像元与去重

- 保留原30米投影、像元对齐、float32、NoData=-9999及red/nir/swir2/NDVI/STR/Fmask六波段。
- 按原窗口写入，不重采样、不重新缩放，不做日期均值。当天同组有多个时刻时，按UTC时间、资产ID依次取首个有效的**完整六波段像元**，避免各波段来自不同观测。
- L30的贡献来源始终限定在同一WRS/date组。S30原下载策略会把同一精确观测时刻的相邻MGRS重叠像元分配给其中一个瓦片；日图可从该同产品、同精确时刻的已存块取回目标瓦片覆盖范围内的去重像元。逐时刻限制目标覆盖，不跨时刻或跨产品回填；贡献资产完整记录在JSON中。因此这是已有去重晴空数据的空间重组，并非承诺还原每个原始颗粒去重前的独立像元值。
- 输入块先按原规则核验SHA256、网格、波段、掩膜及质量；输出关闭后逐窗口重新读取，核对六波段完整字节，再计算文件SHA256。无损DEFLATE，必要时BigTIFF。内存按小窗口使用，单进程串行；GeoTIFF内部tiling不妨碍它作为一个文件使用。

TIFF旁的JSON记录分组、原始资产、所有贡献块SHA256、来源元数据、原配方哈希、网格、重叠优先级、有效像元数、软件版本和输出SHA256。复用已有日图需匹配处理指纹与校验和；冲突或缺失配套JSON时拒绝覆盖。

## 运行

先计划，不写文件、不连接EE：

```bash
.venv/bin/python hls_daily_mosaic.py \
  --inventory runs/archive/inventory.json \
  --source-root /nasdata/HLS/archive \
  --output-root /nasdata/HLS/daily-by-track \
  --state-root /opt/hls-download-manager/runs/daily-mosaic \
  --nas-source //nas.example.org/data
```

首次可加`--regions RIS --month 2020-01-01__2020-02-01 --run`试处理完整月份。来源目录和输出目录必须独立且不能相互包含；state/catalog也放到来源档案之外。

持续处理加`--watch --run`。程序先确认NAS挂载来源与至少256GiB余量，再检查月内所有景完成记录和冻结库存数量。月份尚未完整就等待；已有完成月份可先生成日图，无需等整个研究区结束。后台处理也集中在面积较小的未交付区域，再进入下一区。每5分钟检查一次，新的完整月份陆续进入队列；一个元数据批次网络失败后等待下轮，不连续冲击EE配额。

部署JSON有`daily_mosaic`配置时安装器生成第四个unit，仍不自动启动；明确启用：

```bash
systemctl --user enable --now hls-daily-mosaic.service
journalctl --user -u hls-daily-mosaic.service -f --no-pager
```

它独立于下载归档服务，默认CPU上限50%、内存上限512MiB、低CPU/IO优先级。暂停拼接不会停止下载。`state_root/status.json`提供月份、TIFF数量和阻塞原因；`state_root/months/区域/月.json`是已交付记录。watch对已交付月份核对冻结manifest后复用收据，不反复扫描所有原始栅格；校验时间表示交付时点，不是持续的存储完整性审计。手工改动任何原始档案或日图后，应停止后台并独立审计，不能手工修改收据来绕过核验。

下载进度面板的“研究区完成”描述原始分块档案。特征空间分析应取日图目录，并确认拼接status对应月份已交付、无阻塞；原分块完成不等于全部日图已经生成。分析成果另存，勿改输入档案和日图校验记录。
