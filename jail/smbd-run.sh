#!/bin/sh
# dsh-mu SMB 启动包装器
#
# 只给 smbd 进程树注入 nss_wrapper：系统 /etc/passwd 保持干净，
# 但 smbd 看到的用户表是 /var/lib/dsh-mu/smb/vpasswd（真实用户 + 每个平台用户一行 uid=0）。
# 注意：这里必须用合并文件（真实条目 + 虚拟条目），只放虚拟条目时 Samba 连 root 都解析不出来，
#       会报 "Failed to open .../secrets.tdb"（实测踩过）。
set -e
export LD_PRELOAD=/usr/lib/libnss_wrapper.so
export NSS_WRAPPER_PASSWD=/var/lib/dsh-mu/smb/vpasswd
export NSS_WRAPPER_GROUP=/var/lib/dsh-mu/smb/vgroup
exec /usr/sbin/smbd --foreground --no-process-group -s /etc/dsh-mu/smb/smb.conf
