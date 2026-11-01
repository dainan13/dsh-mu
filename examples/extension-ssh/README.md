# 示例扩展：SSH 资源代理（service P）

这是**扩展包机制**的参考实现 ✓ —— 用来验证"扩展 = 自带 rootfs 的受管 jail + 清单"这套，
以及给新会话写正式插件时做起点 ✓。规范见 `../../docs/18-跨端插件与资源代理.md` ✓。

## 目录

```
manifest.json      # 唯一契约：端口 / 入口 / 出网 / admin 页 / 依赖版本
p/broker.py        # 服务 P（本示例）：catalog + mounts + ssh:exec + 审计 + /health + /admin
admin/             # （占位）真实实现可把配置页放这里，由 P 直接返回
bundles/           # 放 dsh 侧插件的 tarball；平台会导入为"已批准资产"
build-rootfs.sh    # 构建本扩展自己的 rootfs（宿主不需要装任何运行时）
```

## 装到平台上（开发机）

```sh
# 1) 放到平台扫描的目录
install -d /usr/share/dsh-mu/extensions/ssh
cp -a manifest.json p admin bundles /usr/share/dsh-mu/extensions/ssh/

# 2) 构建 rootfs（自带 python3；测试时可指向平台已有的 jail 模板）
sh build-rootfs.sh

# 3) 注册
dsh-muctl ext reload
dsh-muctl ext list
dsh-muctl ext status ssh
curl -s http://127.0.0.1:8790/health
```

## 平台给了什么、扩展负责什么

| 平台 ✓ | 扩展 ✓ |
|---|---|
| 跑 jail（mount ns + chroot + cgroup + 无 capability ✓） | rootfs（自带运行时 ✓） |
| 生成 unit / 起停 / 日志 ✓ | P 的进程与端口 ✓ |
| 实例出网自动放行扩展端口 ✓ | 自己的出网需求（写 `net.egress` ✓） |
| admin 页 iframe 承载 ✓ | 页面内容（配置表单 ✓） |
| 导入 `bundles/*.tgz` 为已批准资产 ✓ | 插件的 npm 包 ✓ |
| `dsh-muctl ext cli ssh …` 透传 ✓ | 可选 `cli` 脚本 ✓ |
