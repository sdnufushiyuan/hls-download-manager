# Linux部署

## 1. 环境和路径

三台节点的角色/主机名为master、node1、node2，SSH别名与角色匹配。master可免交互SSH到两个计算节点，计算节点可免交互SSH回master，用于自有代理隧道。保持主机密钥校验，先人工核实各节点指纹。

在共享存储克隆项目，三台可用同一绝对路径及Linux虚拟环境；每台需要Linux、Python3.11+、OpenSSH客户端、`ss`和`findmnt`。NAS采用CIFS挂载到`/nasdata`，运行用户可读写，默认保留至少256GiB空间。挂载和系统依赖由管理员配置，安装脚本不修改SSH服务器、防火墙或NAS挂载。

复制示例后逐项修改：

```bash
mkdir -m 700 private
cp examples/deployment.example.json private/deployment.local.json
cp examples/regions.example.json private/regions.local.json
cp examples/probe.example.json private/probe.local.json
cp examples/nodes.example.yaml private/nodes.local.yaml
chmod 600 private/*
```

示例中的`/opt/hls-download-manager`须替换为实际项目路径；`nas_source`须与`findmnt --target /nasdata`显示的CIFS来源完全一致。`regions.local.json`填写六个ROI与对应米制投影；ROI和NAS实际数据不上传GitHub。

## 2. 认证和代理

在master认证Earth Engine并设置自己的项目：

```bash
.venv/bin/earthengine authenticate
export EE_PROJECT=your-earth-engine-project
```

不要提交Earth Engine用户认证目录。各节点共享同一用户HOME时可复用合法认证；不共享时需分别按Earth Engine要求认证。此项目不会申请新的账号、自动开通计费或替用户修改Google配额。

从[Mihomo官方发布页](https://github.com/MetaCubeX/mihomo/releases)安装适合Linux架构的内核，填写`mihomo_binary`。把已有、合法可用且无外部dialer依赖的节点填入私有nodes配置。示例节点不能直接使用。

```bash
.venv/bin/python tools/build_proxy_config.py \
  --nodes private/nodes.local.yaml --probe private/probe.local.json \
  --initial 'YOUR_NODE_NAME' --out private

# 用实际内核路径校验，不打印原始配置
/path/to/mihomo -t -d private/mihomo-runtime -f private/mihomo.json

.venv/bin/python tools/install_services.py --config private/deployment.local.json
```

审阅`private/rendered-systemd/`后安装：

```bash
.venv/bin/python tools/install_services.py --config private/deployment.local.json --install
systemctl --user start hls-download-proxy.service
```

安装不启动归档或性能优化。代理只监听loopback；控制端口19091有随机密钥，正式HTTP入口8118，逐节点测速入口18400起。勿把控制API或测速入口暴露到公网。

## 3. 准备真实小块参考

先按README在一个ROI试取一景。选择已通过校验、含有晴空有效像元的块，记录其对应的完整HLS资产ID和传感器。修改`private/probe.local.json`中的ROI、scene_id、sensor、reference，保持scale/aerosol与生产规则一致。

```bash
.venv/bin/python tools/prepare_probe_reference.py \
  --probe private/probe.local.json \
  --grid-tile /nasdata/HLS/bootstrap/L30/YOUR_SCENE/r000000_c000000.tif
```

工具以该块的网格、选定ROI和原始科学掩膜取128×128参考到private目录，不改已有生产块。参考必须来自所选资产覆盖的有效位置；全NoData或极小压缩样本不适合判断带宽。换ROI/资产/规则时应使用新的参考和配置目录。

## 4. 冻结六区库存

```bash
.venv/bin/python tools/create_inventory.py \
  --regions private/regions.local.json \
  --start 2020-01-01 --end 2020-02-01 \
  --out runs/archive/inventory.json
```

先用短日期验证，再为正式档案选择真实范围。此步骤向EE查询有界月度元数据，会使用自己的项目配额；并非离线操作。已经冻结的库存不覆盖，日期/ROI改变时使用新的run目录和输出根目录，并对应修改部署配置。

```bash
.venv/bin/python run_hls_archive.py \
  --inventory runs/archive/inventory.json --out /nasdata/HLS/archive --plan
```

单ROI的其他研究可直接使用`download_hls_optram.py`或`run_hls_cluster.py`，无需凑齐六个ROI；v0.1的六区面板/库存暂不用于该模式。

## 5. 启动后台任务

确认inventory、NAS来源、所有共享路径、用户SSH、参考样本及私有代理配置后：

```bash
systemctl --user enable --now hls-archive.service hls-node-optimizer.service
loginctl enable-linger "$USER"
loginctl show-user "$USER" -p Linger

.venv/bin/python tools/dashboard.py --config private/deployment.local.json
```

linger可能需要管理员授权。服务默认每节点4路，可在明确测试后选1–8；不保证并发越高越快。更改JSON后先停止对应任务，重新渲染和审阅配置；安装器拒绝覆盖不同的已安装unit，修改既有部署应由用户备份并审阅差异后处理。

## 6. 停止与继续

```bash
# 暂停归档，不删除数据
systemctl --user stop hls-archive.service
# 恢复：核验已有文件后继续
systemctl --user start hls-archive.service
# 仅停止性能择优，原生代理故障回退仍在
systemctl --user stop hls-node-optimizer.service
```

普通自动节点选择不重启代理、不清除已有连接；初次加入这些配置需要启动/重启内核，正在使用旧代理的请求可能重试。任何文件校验、NAS或认证错误应先查明，不要删除已有文件来强行继续。

## 可选邮件

```bash
.venv/bin/python hls_email_alert.py --configure \
  --sender sender@YOUR_DOMAIN --recipient recipient@YOUR_DOMAIN
```

此命令明确提交一封测试邮件；需QQ邮箱SMTP授权码，输入不回显，不使用QQ登录密码。SMTP配置在当前用户私有目录；不提交GitHub。完成配置后可由自己的任务调度器运行`hls_email_alert.py --check`，同一故障去重。公开版不自动安装邮件timer。
