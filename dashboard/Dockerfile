# Debug dashboard: python + requests + the docker CLI with its compose and
# buildx plugins. The code is not copied in: compose mounts the repository
# (see the dashboard service in docker-compose.yml), so code edits only need
# a `docker compose restart dashboard`.
FROM docker:29-cli AS docker-cli

FROM python:3.12-slim
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins/ /usr/local/libexec/docker/cli-plugins/
RUN pip install --no-cache-dir "requests>=2.31,<3"
ENTRYPOINT ["python3", "-u", "dashboard/dashboard.py"]
