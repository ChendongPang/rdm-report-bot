#!/usr/bin/env bash
# 重新构建 Docker 镜像。用 --network host + 代理 build-arg 是为了绕开这台机器上
# Docker Hub / Debian 官方源直连很慢/不通的问题（本地起了个 http://127.0.0.1:10808
# 的代理）。这几个参数必须每次都传一致的值，否则 Docker 会认为构建上下文变了，
# 导致缓存失效、重新触发网络问题（详见调试记录）。
# 换了台机器 / 代理端口不一样，改这里的 PROXY 变量即可；如果那台机器网络本身通畅，
# 把 PROXY 相关几行删掉、直接普通 docker build 就行。

set -euo pipefail
cd "$(dirname "$0")"

PROXY="http://127.0.0.1:10808"

sudo docker build --network host \
  --build-arg HTTP_PROXY="$PROXY" \
  --build-arg HTTPS_PROXY="$PROXY" \
  --build-arg http_proxy="$PROXY" \
  --build-arg https_proxy="$PROXY" \
  -t task-flow-agent .
