# 按完整研究区自动交付日图

`hls_auto_daily.py`独立监测每个研究区，完成一个就处理一个，其他区继续下载。与`--require-network-complete`的整网等待模式不同：本程序只在目标研究区全部月份和源块提交记录核验完整后工作，不因其他研究区未完成而阻塞。单个研究区失败也不阻塞其他已完成区。

```bash
cp examples/auto-daily.example.json private/auto-daily.local.json
# 修改绝对路径及NAS来源，复用已有EE项目认证
.venv/bin/python hls_auto_daily.py --config private/auto-daily.local.json
.venv/bin/python hls_auto_daily.py --config private/auto-daily.local.json --run
```

默认仅离线计划，`--run`启用监测，`--once`只扫描一轮。不要同时启动使用相同正式目录的独立日图服务。部署配置中的`auto_daily_config`可指向该私有配置，由安装器生成`hls-auto-daily.service`；安装后按需`systemctl --user enable --now hls-auto-daily.service`。原下载/代理/优化服务不需重启。

处理阶段：

1. 每区核对冻结库存、月配方、场景完成记录、块及侧车存在。数量提示只能推迟，不能直接放行；正式日图读取每块时仍核验SHA及科学规则。源月数量不匹配则记录等待/阻塞，不改写原库存、不删除多出的场景。
2. 对该区完整来源目录中的全部原始HLS资产，按UTC日期和产品补取Fmask，包括明确水体、陆地、云、未覆盖；不是从已过滤的反射率块恢复QA。同日同产品优先最早有效原始QA，若有效水陆冲突则记255跳过。L30/S30间的同日冲突由历史构建器处理。
3. 使用固定30米投影格网，单请求最多768×768个uint8像元，最近邻，串行下载；下载中断保留已核验的块，异常等待下一轮重试，不切换账户或增加并发。原始QA块、完整日图及SHA收据保留。
4. 所有预期日期/产品收齐并逐个核验后，构建“两个连续有效UTC日期标水→全日期剔除”的静态掩膜。云/缺测跳过、陆地打断，同日只计一次。
5. 调用日图生成器仅处理当前完整研究区，分别输出L30、S30及L30优先融合图，来源/时刻图同步过滤。原始数据和旧试算成果保持。完成收据绑定冻结库存、配置、全套源清单和完整QA历史，已完成区不会每轮重新生成。

这里从Earth Engine仅取小块原始QA，完整时序判别和日图计算在本地进行。小块传输采用[官方getDownloadURL接口](https://developers.google.com/earth-engine/apidocs/ee-image-getdownloadurl)，不提交大范围交互计算；不使用bestEffort或隐式尺度。源资产ID、ROI/源清单SHA、精确CRS/transform、尺寸、客户端版本、采集时间记录在本地收据中，签名下载URL不落盘、不打印。资产不可用、配额或认证失败会阻塞该区，保留进度重试。

状态保存在`state_root/status.json`，完整区域收据在`state_root/REGION/completed.json`。当所有区域交付完毕程序正常退出。通过systemd运行时使用`Restart=on-failure`，不会在全部成功后反复重启。

## 明确排除不需要的月份

用户决定舍弃某个整月/末月分片时，在私有自动配置加入例如：

```json
"excluded_months": [
  {"month": "RIS/2026-10-01__2026-10-10", "reason": "User explicitly omitted this final partial month"}
]
```

边界必须精确匹配原冻结库存。排除项同步用于区域完成判断、完整QA历史及正式日图，其他区域保持原范围；范围和理由写入QA历史与完成收据。原始库存、源文件及原有下载进度保留，排除不代表这些影像已下载完成，也不伪造场景完成记录。该示例使RIS研究时段截止2026-09-30，水体历史判定也仅使用保留时段。独立日图CLI对应参数为`--exclude-month RIS/2026-10-01__2026-10-10`。
