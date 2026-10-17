FROM alpine:3.22 AS schemas

ARG KUBERNETES_VERSION=1.31.0
RUN apk add --no-cache curl=8.14.1-r1 \
    && mkdir -p /schemas \
    && curl --fail --silent --show-error --location \
      "https://raw.githubusercontent.com/yannh/kubernetes-json-schema/master/v${KUBERNETES_VERSION}-standalone-strict/deployment-apps-v1.json" \
      --output /schemas/deployment-apps-v1.json

FROM ghcr.io/yannh/kubeconform:v0.8.0-alpine

COPY --from=schemas /schemas /schemas

ENTRYPOINT ["/kubeconform", "-strict", "-summary", "-output", "json", "-kubernetes-version", "1.31.0", "-schema-location", "file:///schemas/{{.ResourceKind}}{{.KindSuffix}}.json"]
CMD ["-"]
