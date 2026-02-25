FROM python:3.14.3-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends cron \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir requests pyyaml

COPY huntarr.py /app/huntarr.py
COPY docker-entrypoint.sh /app/docker-entrypoint.sh

# Run huntarr hourly and send output to container logs.
RUN printf 'SHELL=/bin/sh\nPATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n0 * * * * root /usr/local/bin/python3 /app/huntarr.py -c /config/config.yaml >> /proc/1/fd/1 2>> /proc/1/fd/2\n' > /etc/cron.d/huntarr \
    && chmod 0644 /etc/cron.d/huntarr \
    && chmod +x /app/docker-entrypoint.sh

WORKDIR /config

# Mount /config with config.yaml and huntarr.db for persistence.
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["cron", "-f"]
