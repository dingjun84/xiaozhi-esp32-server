# OpenCloudOS 单语音服务部署

适用于 OpenCloudOS 8/9、systemd、现有 appuser 账号。仅安装 Python 语音服务，不包含智控台、数据库、数字人前端。脚本检测 x86_64/aarch64；ARM 是否能安装全部固定版本的 Python 依赖，以 pip 安装结果为准。需要能够访问 GitHub、conda-forge、PyPI 及你使用的模型 API。

安装目录固定为 `/home/appuser/xiaozhi-server`。独立 Python 3.10 环境位于 `/home/appuser/.conda/envs/xiaozhi`。Miniforge 位于 `/home/appuser/miniforge3`；不替换系统 Python、不自动添加 EPEL/RPM Fusion，不关闭 SELinux、不修改 SSH 或防火墙。签名密钥及模型 API 配置随包携带，请勿公开分享此包。

## 1. 管理员上传和解压

使用可 SSH 登录的管理账号上传，不使用 appuser 上传。将下面的 SERVER_IP 和 ADMIN 替换为真实值。在 Mac 终端执行（文件路径改成实际压缩包路径）：

```bash
scp xiaozhi-server-opencloudos-20261113.zip ADMIN@SERVER_IP:~/
```

登录服务器后执行。解压目录必须是新目录；如之前部署过，请先备份旧配置，合并代码时避免覆盖服务器已调整的密钥和配置。

```bash
mkdir -p ~/xiaozhi-opencloudos-upload
unzip -n ~/xiaozhi-server-opencloudos-20261113.zip -d ~/xiaozhi-opencloudos-upload
sudo mkdir -p /home/appuser/xiaozhi-server
sudo cp -a ~/xiaozhi-opencloudos-upload/xiaozhi-server/. /home/appuser/xiaozhi-server/
sudo bash /home/appuser/xiaozhi-server/deploy/opencloudos/01-system.sh
```

已有服务正在运行时，复制文件和更新依赖前先执行 `sudo systemctl stop xiaozhi-server`。本次脚本不会修改已有 appuser 的登录密码及 SSH 限制。

## 2. 切换 appuser，安装运行环境

```bash
sudo -iu appuser
cd /home/appuser/xiaozhi-server
bash deploy/opencloudos/02-install.sh SERVER_PUBLIC_IPV4
```

将 SERVER_PUBLIC_IPV4 替换为服务器公网 IPv4；私网使用则填写客户端可访问的私网 IPv4。脚本会备份原配置，保留 API 密钥，更新监听地址及 OTA 下发地址，开启 Token 校验并清空免认证设备列表。现有有效 auth_key 会保留。固定使用 8000/8003；采用域名、TLS、代理映射或自定义端口时，不要原样执行本脚本的地址更新部分。

逐步检查输出；任何一步失败会停止，不要继续执行下一步。下载或网络失败可以修复网络后重跑；Miniforge 安装中途失败导致目录残留时，先检查该目录，不要直接删除已有环境。若 pip 报依赖冲突，保留完整报错，不要擅自升级项目锁定版本。

前台试运行：

```bash
bash deploy/opencloudos/run-server.sh
```

另开 SSH 窗口执行：

```bash
curl --fail --show-error http://127.0.0.1:8003/xiaozhi/ota/
ss -lnt | grep -E ':(8000|8003)\b'
```

确认监听正常、OTA 返回公网 WebSocket 地址后，使用数字人完成一次语音交互。单纯看到启动地址日志不代表模型和网络都正常。当前配置使用讯飞流式 ASR，不需要 SenseVoiceSmall/model.pt；若改用本地 FunASR，需另行补齐权重。此前讯飞的 `11200 / licc limit` 是服务商授权问题，部署脚本不会修复，需要检查对应服务授权和参数。

在前台窗口按 Ctrl+C，停止前台进程，再返回管理员：

```bash
exit
```

## 3. 管理员注册 systemd 服务

```bash
sudo bash /home/appuser/xiaozhi-server/deploy/opencloudos/03-service.sh
sudo journalctl -u xiaozhi-server -n 100 --no-pager
curl --fail --show-error http://127.0.0.1:8003/xiaozhi/ota/
```

服务以 appuser 运行，开机自启，异常退出后重启；60 秒内连续失败会限流。重复运行本脚本会备份已有服务文件并重启服务。常用命令：

```bash
sudo systemctl status xiaozhi-server --no-pager
sudo journalctl -u xiaozhi-server -f
sudo systemctl restart xiaozhi-server
sudo systemctl stop xiaozhi-server
# 排除连续启动失败的问题后：
sudo systemctl reset-failed xiaozhi-server
sudo systemctl start xiaozhi-server
```

若出现 `Permission denied`，先查看文件权限以及 `sudo journalctl -u xiaozhi-server`。怀疑 SELinux 时检查 `sudo ausearch -m AVC -ts recent`（需要已安装 audit 工具），根据实际拒绝记录处理，不直接关闭 SELinux。

## 4. 网络放行

在云安全组中允许你的客户端公网 IP 访问 TCP 8000、8003，并移除这两个端口已有的全网放行规则。若启用 firewalld，还需在实际生效的 zone 中允许相同来源；不使用 Ubuntu 的 ufw 命令，不自动启动或关闭防火墙。

```bash
sudo systemctl is-active firewalld
# 仅在 firewalld 已运行时查看生效区域和规则：
sudo firewall-cmd --get-active-zones
sudo firewall-cmd --list-all
```

例如网卡使用 public zone，客户端公网 IPv4 为 CLIENT_PUBLIC_IP，则执行（替换占位符；如不是 public，请替换 zone）：

```bash
sudo firewall-cmd --permanent --zone=public --add-rich-rule='rule family="ipv4" source address="CLIENT_PUBLIC_IP/32" port port="8000" protocol="tcp" accept'
sudo firewall-cmd --permanent --zone=public --add-rich-rule='rule family="ipv4" source address="CLIENT_PUBLIC_IP/32" port port="8003" protocol="tcp" accept'
sudo firewall-cmd --reload
sudo firewall-cmd --zone=public --list-all
```

这两条规则不会收窄已有的全网端口放行，需要检查并移除已有宽泛规则。客户端公网 IP 变化时也需更新云安全组及防火墙规则。

数字人在 Mac 继续运行，OTA 地址填 `http://SERVER_PUBLIC_IPV4:8003/xiaozhi/ota/`。数字人页面若使用 HTTPS，则需配置域名、HTTPS/WSS 代理后再连接。

注意：当前 Token 开关只控制 WebSocket 校验；公开的 OTA 接口仍会向提供 Device-ID、Client-ID 的客户端签发 Token。要限制其他人使用，请同时限制 8000/8003 的来源 IP。不要把 allowed_devices 当成准入名单，它表示免 Token 校验的设备。

## 参考

- OpenCloudOS 文档：https://docs.opencloudos.org/faq/
- Miniforge 安装说明：https://github.com/conda-forge/miniforge

本流程已做脚本语法和压缩包完整性检查，尚未在你的 OpenCloudOS 服务器上执行。
