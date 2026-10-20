# 连续有效观测水体：跨日期统一剔除

规则固定为**同一位置连续两个不同UTC日期的有效观测均标水，则在全部日期的L30、S30、FUSED中剔除该位置**。不是只删除那两天，也不是把当天水体改成陆地。

- 原始Fmask的位1–4（云、邻云/影、云影、雪）均为0、气溶胶为低才算有效；位5为水体。
- 云、缺测和未覆盖跳过，不当作陆地，不打断连续序列。有效陆地会把连续标水计数清零。
- 同一天两传感器只算一个日期；若两者的有效水/陆标记冲突，该日跳过，不凑成两次，也不强制判水。
- 一个日期、多个MGRS瓦片必须先在原固定格网上形成一张原始QA日图，不能把同日瓦片当成多次观测。
- 这是用户指定的历史排除规则；后来转为陆地的位置也会被永久剔除。用同一静态掩膜处理全部产品和日期，避免S30把被剔除位置填回。

## 下载完成后再处理

除下述整网等待模式，也可使用[按完整研究区自动处理](auto-daily.md)：每个研究区全时段完整核验后，单独补齐其原始QA历史并处理，不等待其他研究区。不能只凭几个月下载完成就构建覆盖全时段的永久水体掩膜。

`hls-daily-mosaic --require-network-complete --watch --run ...`会先等待冻结库存中**全部研究区、全部非空月份**的下载完成，再开始任何日图。数量提示只作等待预筛；提示齐全后还必须核对场景完成记录、配方、月份数量和块文件/侧车存在，后续写日图时逐块核验SHA。清单数量不一致时保持等待，不截断列表强行通过。

生产配置可设`daily_mosaic.require_network_complete=true`。如果还配置`daily_mosaic.water_mask_root`，完成下载后继续等待每个研究区的已核验水体掩膜；没有掩膜或原始QA时不会输出未过滤的新日图。已经形成的旧日图保留为旧成果，新的掩膜规则使用独立输出和状态目录，不能复用未过滤月收据。

## 原始QA不可省略

原严格晴空陆地下载已把水体像元掩成NoData，不能据此恢复历史水体标记。需要在整网下载完成后补取/整理未经晴空陆地过滤的原始HLS Fmask，并使用已有账号、投影、完整时间范围与冻结资产清单。工具不自动把NoData、低SWIR、NDVI或过去的猜测转换为水体，也不会在当前下载期间补跑全历史QA。

构建器需要一份指向原始QA日图的清单：

```json
{
  "raw_unfiltered_fmask": true,
  "observations": [
    {"date":"20200601","product":"L30","path":"/nasdata/HLS/raw-qa/L30_20200601.tif","sha256":"REPLACE_WITH_SHA256"},
    {"date":"20200608","product":"S30","path":"/nasdata/HLS/raw-qa/S30_20200608.tif","sha256":"REPLACE_WITH_SHA256"}
  ]
}
```

必须为完整原始QA历史，包含明确陆地和水体日，不能只列水体日期或已筛选的局部样本；代码不能判断人为遗漏的日期。清单应与冻结来源目录逐日核对后使用。所有QA为同研究区固定网格的单波段uint8 TIFF，255/NoData跳过，原始QA不进行分类均值重采样。

```bash
hls-water-history build --manifest /private/REGION_raw_qa.json \
  --output /nasdata/HLS/water-history/REGION/water_history.tif --run
```

得到0/1静态掩膜和输入/规则/输出SHA收据。每个区域的文件必须为`water_mask_root/REGION/water_history.tif`。随后在区域日图服务中增加：

```text
--require-network-complete --water-mask-root /nasdata/HLS/water-history
--layout product-region --grouping region-day --fuse-products
```

服务将同一掩膜应用到各日L30/S30及融合结果，六波段整组设NoData；融合来源置0、时间置4294967295。掩膜/收据校验和进入日图指纹，规则变动或旧未过滤成果会拒绝复用；处理期间掩膜变化也会停止。不能对同一区选择不同产品掩膜。

另外可对单个旧主图或辅助图另存过滤版本：

```bash
hls-water-history apply --input /nasdata/HLS/old/REGION_L30_20200623.tif \
  --mask /nasdata/HLS/water-history/REGION/water_history.tif \
  --output /nasdata/HLS/filtered/REGION_L30_20200623.tif --run
```

原文件保持，写后读回逐窗口比对；主图、来源和时间图都应使用同一掩膜。单文件工具不替代完整日图服务的融合追溯收据。所有命令省略`--run`为计划，不写文件、不访问网络。
