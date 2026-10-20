# VS Code中文实时任务监控

在VS Code Remote SSH的Linux终端运行：

```bash
python hls_system_monitor.py --config /absolute/path/monitor.local.json
```

配置参见`examples/monitor.example.json`。使用已有Linux Python，无需新装第三方库；交互界面使用Linux标准库curses。不启动、停止或重启任何任务，不改下载、QA、日图、代理、用户权限，不访问EE或读取认证/节点密钥。Ctrl+C或q退出仅关闭视图。

界面按键：

|按键|视图/操作|
|---|---|
|1|总览：三台主机负载，下载、QA、日图、代理状态，NAS空间|
|2|三台主机全部可见用户的进程与当前命令|
|3|master系统级及当前用户级服务（其他用户任务在进程页查看）|
|4|按主机和用户汇总进程数、CPU和RSS|
|5|后处理最近事件，包括HTTP数字状态码和重试|
|↑↓、PgUp/PgDn|滚动/翻页，n/p也可翻页|
|/|搜索用户名、PID、主机或命令；Enter应用，Esc清除|
|c、m|按CPU、内存排序|
|Enter（进程页）|查看当前页首进程的完整命令，Esc返回|
|空格|暂停画面，后台采集和任务继续|
|q、Ctrl+C|退出面板，后台任务继续|

本机约5秒刷新；node1/node2约15秒通过已有免交互SSH探测，连接失败只标记该主机暂不可用。代码和Python绝对路径须在这些节点上都存在，保持已有SSH主机密钥校验；不会修改SSH、防火墙或监听端口。命令来自当前账号可读取的/proc，不读取其他用户的shell历史、不提权；常见密码、令牌、签名URL和URL用户凭据参数会脱敏，但不应将进程视图随意公开分享。

CPU为采样区间值，进程100%表示一个核心，用户汇总可超过100%；RSS相加可能包含共享页。没有权限读取的PID/命令标为不可见。QA一组表示一个日期的一种产品。新正式日图仅从新自动日图状态中已完成月份的收据计数，当前月写入但尚未完成核验的影像会稍后计入；旧试算图不混计。全档案下载统计首次后台载入可能较慢，不阻塞其余界面；速度与ETA至少观察两分钟有效进展后显示，仅QA的ETA不包括水体掩膜和正式日图阶段。

非交互输出：

```bash
python hls_system_monitor.py --config /absolute/path/monitor.local.json --once
python hls_system_monitor.py --config /absolute/path/monitor.local.json --json
```

这些命令等待约3秒取得第一批状态。较慢的全档案统计可能仍显示载入中；交互界面会在后台读取完后自动更新。
