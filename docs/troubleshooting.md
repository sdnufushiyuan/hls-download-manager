# 排错

| 现象 | 检查和处理 |
|---|---|
| `SSLEOFError` / 连接超时 | 查看代理健康；连接意外断开进入有限网络重试，保留已校验块 |
| Google HTTP429 | 请求或计算限制，不据此认定节点被限速；等待、减少并发，按自己的项目配额处理 |
| 授权/HTTP401/403 | 检查认证与项目授权；自动换节点不能修复账号权限 |
| NAS来源不匹配 | 核对CIFS来源、/nasdata挂载及运行用户权限；不得写入失挂载后遗留的本地目录 |
| 某节点显示等待300秒 | 同一月网络重试等待，尚未宣告永久失败；之后先核验缓存 |
| 服务active但暂无新TIF | 可能在核验旧块或跳过无晴空/重复覆盖；结合候选进度和日志判断 |
| 进度读取慢 | 首次扫描NAS和历史记录可能耗时较长；之后复用内存记录，不因慢扫描启动第二个下载任务 |
| ETA采样中 | 尚未积累120秒/3候选，或新服务批次；不要用两分钟样本承诺长期完工日期 |
| optimizer不可用 | 核对私有config、参考、控制API和EE权限；原生代理回退独立运行 |
| 缓存校验失败 | 先保存日志查原因，不删除数据或放宽科学/哈希检查 |

```bash
systemctl --user status hls-archive.service hls-download-proxy.service hls-node-optimizer.service --no-pager
journalctl --user -u hls-archive.service -n 50 --no-pager
```

提交问题前运行离线测试，使用合成数据复现。不要发布原始私有配置、签名下载URL、认证缓存或未脱敏日志。
