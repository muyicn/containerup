# 真实引擎 E2E 测试镜像（健康版）：MARK 参数变更即产生不同摘要；VERSION 写入 OCI 版本标签
FROM alpine:3.20
ARG MARK=base
ARG VERSION=0.0.0
LABEL org.opencontainers.image.version=$VERSION
RUN mkdir -p /opt && echo "$MARK" > /opt/mark
HEALTHCHECK --interval=2s --timeout=1s --start-period=1s --retries=2 CMD /bin/sh -c "exit 0"
CMD ["sleep", "3600"]
