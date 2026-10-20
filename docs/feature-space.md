# 样本质控与NDVI–STR对比

`hls-feature-space`在原始日图之外生成独立的分析样本。安装绘图依赖：`pip install -e '.[analysis]'`。输入为同研究区、同UTC日期、同格网的L30/S30六波段日图，命令中的日期和研究区由使用者指定，应与输入收据一致。工具不访问Earth Engine，不修改下载档案、服务或原始日图。

```bash
hls-feature-space \
  --l30 /nasdata/HLS/daily/L30/REGION/2020/REGION_L30_20200623.tif \
  --s30 /nasdata/HLS/daily/S30/REGION/2020/REGION_S30_20200623.tif \
  --output-root /nasdata/HLS/analysis/REGION_20200623_v1 \
  --region REGION --date 20200623 --plot --run
```

省略`--run`仅展示计划。输出根下分别生成`L30/REGION/2020`、`S30/REGION/2020`、`FUSED/REGION/2020`的`*_QC.tif`以及融合来源图，主图六波段仍为red/nir/swir2/NDVI/STR/Fmask。主图先分别质控，再做同日L30优先、S30补空，整组六波段来自同一观测。不能从产品日图推断所有像元的精确时刻，本工具不伪造新的时刻图；保留生产日图的原收据和时刻追溯文件。

## 自动检查与保留

- 检查完整六波段、共同格网、NoData一致、严格晴空陆地低气溶胶QA，以及存储指数与反射率公式的一致性。
- 本次模型输入域为`0≤red,nir≤1`、`0<swir2≤1`、`red+nir>1e-6`、`−1≤NDVI≤1`、`STR≥0`。这是保守分析域，不代表所有域外NBAR值都是损坏数据。
- 不按STR>15、SWIR2<0.01或分位数自动删点。STR在SWIR2接近0时自然增大，具体地物和质量需要额外证据。
- 不重采样、钳制负值、平滑、插值、跨日期补值或对L30/S30平均。
- 绘图上排显示STR 0–15主体，下排显示全部保留点（零点用symlog容纳）；每排内共用坐标/分箱/色标。下排计数必须覆盖所有保留点。图是探索性像元密度图，没有拟合干湿边，不将显示尺度变化当作物理边界变化。

## 有证据的额外剔除

用`--review /private/review.json`传入明确的产品像元观测清单。行列从0开始，清单必须绑定两个原始输入文件的SHA256；文件变化、重复记录、越界、缺理由/证据或已经无效的像元都会拒绝。下面的校验和为占位，必须替换成实际文件值：

```json
{
  "input_sha256": {"L30": "REPLACE_WITH_SHA256", "S30": "REPLACE_WITH_SHA256"},
  "reject": [
    {"product": "L30", "row": 10, "col": 20,
     "reason": "reviewed spectral-domain violation",
     "evidence": "Reference to an independently checked source-band and spatial/temporal audit"}
  ]
}
```

清单只表示审查结论，工具不能验证文字证据真实性。推荐保留每点邻域、其他日期有效观测数、原始QA和辅助光谱。其他日期缺测不等于异常；过去是水体也不等于当前像元必须删除。证据不足者保留并注明不确定性。

结果`analysis.json`记录输入校验和、规则、完整清单、输出校验和和像元统计；全部栅格写后读回比对。只有相同指纹且文件校验和一致时复用；不同分析或残缺输出拒绝覆盖，使用新目录重新运行。工具面向单日样本，读取整幅影像，内存需求随区域增长；大型生产整图继续使用窗口化日图服务。
