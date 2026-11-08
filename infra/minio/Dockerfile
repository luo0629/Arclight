FROM golang:1.25-alpine AS build
# Official upstream release source; independent of discontinued binary images.
# Bound transient upstream retries and verify the exact release before building.
RUN apk add --no-cache ca-certificates curl
RUN curl --fail --location --silent --show-error \
        --retry 3 --retry-delay 2 --retry-max-time 180 \
        --connect-timeout 15 --max-time 90 --proto '=https' --proto-redir '=https' \
        --output /tmp/minio.tar.gz \
        https://github.com/minio/minio/archive/refs/tags/RELEASE.2025-10-15T17-29-55Z.tar.gz \
    && echo 'be6d0bd3696c3a13a35f02d3a0280b64319c67918b4501c5c3d87f96d000085c  /tmp/minio.tar.gz' | sha256sum -c -
WORKDIR /src
RUN tar -xzf /tmp/minio.tar.gz --strip-components=1 -C /src
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/minio .

FROM alpine:3.22
RUN apk add --no-cache ca-certificates curl && addgroup -g 10001 minio && adduser -D -u 10001 -G minio minio && mkdir /data && chown minio:minio /data
COPY --from=build /out/minio /usr/local/bin/minio
COPY --from=build /src/LICENSE /usr/share/licenses/minio/LICENSE
USER 10001:10001
EXPOSE 9000
HEALTHCHECK --interval=3s --timeout=3s --retries=30 CMD curl -fsS http://localhost:9000/minio/health/ready || exit 1
ENTRYPOINT ["minio"]
CMD ["server", "/data", "--console-address", ":9001"]
