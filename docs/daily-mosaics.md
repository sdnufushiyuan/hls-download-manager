# 研究区按日整图与L30优先融合

默认按**研究区 + UTC观测日期 + 产品**输出一个真实六波段GeoTIFF，跨HLS瓦片拼接。各日期/产品采用原下载档案的同一30米投影和固定ROI包围网格；区域外、未覆盖、云或不合格位置保留NoData。没有观测的日期不凭空生成数据。

HLS L30/S30使用共同的MGRS切片体系，整区日图不再按原始Landsat WRS path/row或单个MGRS瓦片拆开。现有下载已经导出到各研究区固定网格；拼接直接复用，**不再次重投影或重采样**。研究区导出网格的原点与原生MGRS瓦片原点不是同一个概念。[NASA网格说明](https://hls.gsfc.nasa.gov/algorithms/)。

## 输出

| 文件示例 | 内容 |
|---|---|
| `RIS/L30/2016/RIS_L30_20160623.tif` | 当天L30覆盖研究区的全部瓦片拼接 |
| `RIS/S30/2016/RIS_S30_20160623.tif` | 当天S30覆盖研究区的全部瓦片拼接 |
| `RIS/FUSED/2016/RIS_FUSED_20160623.tif` | 可选的额外融合图：合格L30优先、同日合格S30补缺 |
| `RIS/FUSED/2016/RIS_FUSED_20160623_source.tif` | 像元来源：0=NoData，1=L30，2=S30 |
| `RIS/FUSED/2016/RIS_FUSED_20160623_time_utc.tif` | 观测时刻：距UTC午夜的秒数，4294967295=NoData |

主图始终为red、nir、swir2、NDVI、STR、Fmask六波段float32，NoData=-9999。NDVI为第4、STR为第5波段；来源/时刻另存，不改变主图结构。无损DEFLATE压缩，内部TIFF tiling仍是一个真实文件，较大影像使用BigTIFF。

融合用`--fuse-products`显式启用，原L30/S30日图继续保留。只有一种产品的日期，融合图使用该产品的有效像元。全云日期可能生成NoData图，JSON中`valid_pixels=0`，不能用来拟合特征空间。

## 取值与质量

1. 普通产品日图按UTC时刻、资产ID依次取首个有效的完整六波段像元。
2. 融合图中**所有合格L30优先于所有S30**，不因S30观测较早而覆盖L30。L30无有效值的位置才取同UTC日期的合格S30；各产品内仍按时刻、资产ID排序。
3. 六波段整组来自同一个来源，不做波段分别填补、均值、插值或跨日期填补。
4. 沿用原严格质量要求：Fmask位1–5为0、既定气溶胶阈值、六波段NoData一致，核验数值、网格、配方与SHA；不为填满区域降低标准。
5. 原档案的同产品同精确时刻去重规则保持，日图重组其已有贡献块，不另造缺失观测。

物理波段对应：L30的SWIR2为B7，S30的SWIR2为B12；NIR分别为B5和B8A。HLS已将S30的光谱响应调整到Landsat OLI参考，不能按相同数字的波段号直接拼，也不再次应用比例或bandpass校正。[NASA对应与校正表](https://hls.gsfc.nasa.gov/algorithms/)、[L30目录](https://developers.google.com/earth-engine/datasets/catalog/NASA_HLS_HLSL30_v002)、[S30目录](https://developers.google.com/earth-engine/datasets/catalog/NASA_HLS_HLSS30_v002)。

同日不等于同一时刻，也不代表传感器差异完全消失。融合图是派生的同日组合；原产品图、来源图和时刻图支持后续检查与分别建模。

## 本地处理和核验

默认region-day仅用冻结的景编号/时间戳分组，**无需WRS元数据查询、EE初始化或重新下载栅格**。月内所有景完成且库存数量相符后，逐窗口核验输入原块并写出；关闭后逐窗口重读，主图与来源/时刻图的完整字节都需一致，再计算SHA并提交收据。

JSON记录所有贡献资产/块SHA、原配方、固定网格、规则、波段对应、有效像元数、L30/S30贡献数、版本及所有输出SHA。复用须匹配指纹、校验和及两张融合追溯图；冲突或缺失时拒绝覆盖。原始下载块、原断点及旧版按轨道结果不删除、不重写。

## 使用

先查看计划，不写数据、不联网：

```bash
.venv/bin/python hls_daily_mosaic.py \
  --inventory runs/archive/inventory.json \
  --source-root /nasdata/HLS/archive \
  --output-root /nasdata/HLS/daily-by-region \
  --state-root /opt/hls-download-manager/runs/daily-region-mosaic \
  --nas-source //nas.example.org/data --grouping region-day --fuse-products
```

首次可加`--regions RIS --month 2020-01-01__2020-02-01 --run`处理完整月份；持续运行加`--watch --run`。原始档案和输出目录必须独立且互不包含，state位于原始档案外。**旧track-day切换时使用新的output/state目录**，避免不同口径与收据混用。旧模式可显式`--grouping track-day`，仍需要GEE来源元数据，不能与fusion同时使用。

部署JSON设置`daily_mosaic.grouping=region-day`、`daily_mosaic.fuse_products=true`。安装器只生成/安装服务，不自动启动：

```bash
systemctl --user enable --now hls-daily-mosaic.service
journalctl --user -u hls-daily-mosaic.service -n 20 -f --no-pager
```

每300秒检查新完成月份，集中处理较小研究区。沿用NAS来源/挂载核验、256GiB空间保留、单进程窗口处理；CPU上限50%、内存512MiB、低CPU/IO优先级。暂停拼接不停止下载。

`state_root/status.json`与`state_root/months/区域/月.json`记录交付、待完成和阻塞。TIFF计数仅包含主日图，两张融合追溯图另计。watch对已交付月核对冻结manifest后复用，不反复扫描全部原块；校验时间是交付时点，不是持续存储审计。

原下载面板的研究区完成描述分块档案；特征空间使用的整图交付需查看本服务收据。分析结果另存，不修改输入或手动更改收据。
