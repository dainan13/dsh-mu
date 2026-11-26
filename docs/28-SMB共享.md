# 28 · SMB 共享（由扩展提供）

> 面向：运维 / 扩展开发者。
> 结论：**SMB 归扩展管**。平台只提供站点入口、身份注入与观测，不参与账号与配置。

## 1. 分工

```
扩展 dsh-mu-smb-ext（独立仓库，apk 安装）
  · 依赖拉入 samba / samba-server / nss_wrapper（装在系统层）
  · 自己的 openrc 单元：dsh-mu-smb（smbd, 445）、dsh-mu-smb-ui（界面服务, unix socket）
  · 自己的 CLI：/usr/local/sbin/dsh-mu-smb
  · 自己的数据：/var/lib/dsh-mu/smb/{accounts.json,passdb.tdb,vpasswd,vgroup,…}
  · 自己的界面：/smb/ 站点（由平台网关代理，注入身份头）
  · 自己的客户端插件：随扩展安装推送到实例（设置页 + 引导区卡片）

平台
  · 安装/升级扩展包（apk）与 `dsh-muctl ext reload`
  · 站点入口登记（manifest 的 site 段，kind=socket，upstream=扩展的 unix socket）
  · 观测：扩展列表里的 system 单元 running/stopped
  · 不提供任何 SMB 账号/配置/面板接口
```

## 2. 模型

```
平台用户 1:1 SMB 账号（同名）
密码：只存在 Samba 的 tdbsam（扩展目录下）
文件系统身份：uid 0（root）、gid 0 —— 有意为之：
  DSH 实例本身以 root 运行、工作区也是 root 所有，因此 SMB 与实例之间没有权限错配。
  代价：共享范围内是 root 级权限，没有文件级隔离。

共享（默认两条，path 用 %U）
  \\<host>\home       → /var/lib/dsh-mu/instances/%U/home
  \\<host>\workspace  → /var/lib/dsh-mu/instances/%U/workspace
[home] 里的 workspace 由扩展在启动 smbd 前 bind mount 成真实工作区
```

## 3. 为什么用 nss_wrapper

Samba 4.23 没有 Non-Unix Account 后端：`smbpasswd -a <user>` 必须能通过
`getpwnam` 解析出 uid。扩展用 nss_wrapper 只给 smbd 进程树注入一份**合并**用户表：

```
/var/lib/dsh-mu/smb/vpasswd = /etc/passwd + 每个开通用户一行 "<name>:x:0:0:…"
```

必须是合并文件：只写虚拟条目时 Samba 连 root 都解析不出来，会报
`Failed to open .../secrets.tdb`。系统 `/etc/passwd` 保持干净。

## 4. 扩展命令（在扩展包内）

```sh
dsh-mu-smb status [user]
dsh-mu-smb list
dsh-mu-smb sync
dsh-mu-smb enable <user> --password '…'
dsh-mu-smb passwd <user> --password '…'
dsh-mu-smb disable <user>
dsh-mu-smb share add --share name --path /dir
```

用户侧：DSH「设置 → SMB 共享」与引导区卡片（走 `/smb/` 站点接口）。
管理员侧：`/smb/` 页面（共享清单与账号状态；重置/停用）。

## 5. 风险与审批

```
service.kind = "system"：安装本扩展 = 在系统层安装 root 权限的 smbd 并监听 445。
只应安装可信来源的扩展包（manifest 里 risk=high，安装时需要显式确认）。
```

## 6. 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `Failed to add entry for user X` | vpasswd 缺该用户或不是合并文件 | `dsh-mu-smb sync` |
| `Failed to open .../secrets.tdb` | vpasswd 缺真实用户（root） | 确认 vpasswd 是合并文件 |
| 改密码后 Windows 不弹新密码框 | Windows 缓存旧凭据 | `cmdkey /delete:<host>`、重启 WebClient、注销重登 |
| `[home]` 里 workspace 为空 | bind mount 没建立 | `dsh-mu-smb sync` 或重启扩展单元 |
| 445 不通 | 单元没起 / 端口被占 | `rc-service dsh-mu-smb status` |
