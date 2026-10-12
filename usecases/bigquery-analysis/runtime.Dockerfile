# BigQuery analysis use case runtime: the shared satin-runtime plus the BigQuery client.
# Build with usecases/bigquery-analysis/scripts/build-images.sh (builds satin-runtime first).
ARG BASE=satin-runtime:dev
FROM ${BASE}

USER root
RUN pip install --no-cache-dir "google-cloud-bigquery==3.46.1"
USER satin
