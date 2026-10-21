# HTTPS（网关 TLS）

网关默认只监听 **80（纯 HTTP）**。开启后同时监听 **443**，证书放在 `/etc/dsh-mu/tls/`，
并且证书只以**只读**方式进入网关 jail（实例看不到它）。

## 1. 最快路径（自签，适合内网）

```sh
dsh-muctl tls self-sign                 # 用主机名做 CN，SAN 里自动带上本机 IP
dsh-muctl tls enable
dsh-muctl gateway restart
dsh-muctl tls status                    # 看 subject / 有效期 / 剩余天数
```

浏览器会提示"证书不受信任"——自签证书的正常现象，选择继续即可。
要避免提示，用第 2 种方式换正式证书。

选项：

```sh
dsh-muctl tls self-sign --cn dsh.example.com --hosts dsh.example.com chat.example.com --days 825
```

`--hosts` 里的名字会写进 **SAN**（现代浏览器只认 SAN，不看 CN）。

## 2. 用已有证书（正式 CA / Let's Encrypt / 公司 CA）

```sh
dsh-muctl tls import --cert /path/fullchain.pem --key /path/privkey.pem
dsh-muctl tls enable
dsh-muctl gateway restart
```

- `--cert` 必须是**包含中间证书的 chain**（Let's Encrypt 的 `fullchain.pem`）；
- `--key` 只在本机存放（`0600`），不会进入任何实例 jail；
- 过期后续期：重复执行 `tls import` + `gateway restart`。

## 3. 80 与 443 的关系

| 配置 | 行为 |
|---|---|
| `tls_enabled=true`, `tls_keep_http=true`（默认） | 80 与 443 都提供服务；会话 cookie **不带 Secure**（http 也能登录） |
| `tls_enabled=true`, `tls_redirect=true` | 80 的请求 **301 到 https**；会话 cookie 带 `Secure` |
| `tls_enabled=false` | 只有 80（默认） |

```sh
dsh-muctl tls redirect on      # 需要 http→https 跳转时
dsh-muctl tls redirect off
dsh-muctl gateway restart
```

> `Secure` 只在"80 已跳转 443"时加：否则浏览器在 http:// 下会拒发 cookie，导致登录死循环。

## 4. 自检

```sh
dsh-muctl doctor
```

新增三项：

- `HTTPS certificate` —— 证书与私钥可读、内容像 PEM
- `HTTPS certificate expiry` —— 剩余天数（<14 天为 WARN，过期/缺失为 FAIL）
- `HTTPS listening on 443` —— 端口是否真的在监听

命令行验证：

```sh
openssl s_client -connect 127.0.0.1:443 -servername <你的主机名> </dev/null 2>/dev/null | openssl x509 -noout -subject -dates
```

## 5. 实现位置（维护者向）

| 文件 | 作用 |
|---|---|
| `cli/dshmuctl.py` → `cmd_tls` | `status / self-sign / import / enable / disable / redirect / keep-http` |
| `cli/dshmuctl.py` → `tls_nginx_block` | 生成 443 的 `listen`/`ssl_*` 指令，以及可选的 80→443 跳转 server |
| `gateway/dsh-mu-server.conf` → `@TLS_LISTEN@` | 模板占位符，TLS 关闭时渲染成空 |
| `jail/dsh-mu-jail` / 网关 conf | `/etc/dsh-mu/tls` 以 **ro** 挂到 `/etc/dsh-mu-gateway-tls` |
| `gateway/lua/dshmu.lua` → `_M.cookie` | TLS + 跳转同时开启时给会话 cookie 加 `Secure` |

## 6. 注意

- 443 需要 `CAP_NET_BIND_SERVICE`：网关 jail 本来就保留了这一个能力（`KEEP_CAPS=net_bind_service`）。
- 换证书后必须 `gateway restart`：`nginx` 只在启动时读取证书文件。
- 自签证书的 `-days` 最大不要超过 825 天（Apple 平台对有效期有上限，超了会直接拒绝）。

## 7. 踩过的坑（实现时必读）

| 现象 | 原因 | 现在的处理 |
|---|---|---|
| 开启后网关 **起不来**，报 `duplicate listen 0.0.0.0:80` | 模板自带 `listen 80`，TLS 分支又加了一条 | 模板只留 `@LISTEN_DIRECTIVES@` 一个占位符，由 `listen_directives()` **统一**生成 |
| 报 `duplicate default server for 0.0.0.0:80` | 80→443 跳转的 server 也写了 `default_server` | 跳转 server 用普通 `listen 80;`，default 永远只属于主 server |
| 报 `cannot load certificate …` | 网关 jail 没挂证书目录 | `write_gateway_jail_conf()` **无条件**把 `/etc/dsh-mu/tls` 以 ro 挂进 jail |
| `TLS_ENABLED="false"` 却仍然启用 | `_conf_coerce()` **缺 bool 分支**，`bool("False") == True` | 补上 bool 解析（`true/false/1/0/on/off/yes/no`） |
| 报 `chown(/var/tmp/nginx/…) failed` | nginx 启动时会 chown 临时目录，而网关 jail 只有 `CAP_NET_BIND_SERVICE` | 每次渲染时删除并重建 `/var/tmp/nginx`（root 属主） |
| `dsh-muctl config` 改了但渲染没变 | `sync_all()` 用的是调用方手里的旧 `state["config"]` | `sync_all()` 开头用 `load_config()` 覆盖，conf 文件是唯一事实来源 |

### 安全网

`dsh-muctl tls enable` 会**先校验再生效**：在 jail 内跑
`nginx -t -c /etc/dsh-mu-gateway/nginx.conf`，失败就把开关回滚并打印 nginx 的原文，
所以一次错误的证书配置**不会**把在跑的网关搞挂。
