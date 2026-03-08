# Production Deployment Guide

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Environment Configuration](#environment-configuration)
3. [Docker Compose (Single Server)](#docker-compose-single-server)
4. [AWS ECS + RDS + ElastiCache](#aws-ecs--rds--elasticache)
5. [Scaling Workers](#scaling-workers)
6. [Monitoring & Alerting](#monitoring--alerting)
7. [Backup & Recovery](#backup--recovery)
8. [Security Hardening](#security-hardening)
9. [Troubleshooting](#troubleshooting)

---

## Prerequisites

| Component        | Minimum Version | Purpose                        |
|------------------|-----------------|--------------------------------|
| Docker           | 24+             | Container runtime              |
| Docker Compose   | 2.20+           | Multi-service orchestration    |
| PostgreSQL       | 16              | Primary data store             |
| Redis            | 7               | Job queue + rate limiting      |
| Tesseract OCR    | 5.x             | Scanned PDF extraction         |
| Poppler          | 22.x            | PDF → image conversion         |

All system deps are bundled in the Docker image; bare-metal installs need them manually.

---

## Environment Configuration

Copy `.env.example` to `.env` and set **at minimum**:

```bash
# ── REQUIRED ──
DATABASE_URL=postgresql+asyncpg://user:strongpassword@db-host:5432/docpipeline
REDIS_URL=redis://redis-host:6379/0
ENVIRONMENT=production
API_KEY=$(openssl rand -hex 32)   # generate a strong key

# ── RECOMMENDED ──
DEBUG=false
LOG_FORMAT=json
LOG_LEVEL=INFO
STORAGE_BACKEND=s3                # or "local" with persistent volume
S3_BUCKET_NAME=my-docpipeline-bucket
S3_REGION=us-east-1
S3_ACCESS_KEY=AKIA...
S3_SECRET_KEY=...

# ── TUNING ──
DB_POOL_SIZE=20
DB_MAX_OVERFLOW=30
RATE_LIMIT_REQUESTS=300
RATE_LIMIT_WINDOW_SECONDS=60
MAX_UPLOAD_SIZE_MB=100
CHUNK_SIZE_PAGES=100
```

---

## Docker Compose (Single Server)

Best for **staging** or a **single-server deployment** (≤ 1000 docs/day).

```bash
# 1. Build and start
docker-compose up -d --build

# 2. Verify health
curl http://localhost:8000/health
# → {"status":"healthy","version":"2.0.0","environment":"production"}

# 3. Scale workers as needed
docker-compose up -d --scale worker=3

# 4. View logs
docker-compose logs -f api worker

# 5. Graceful shutdown
docker-compose down
```

### Persistent Volumes

The compose file defines four named volumes:

| Volume          | Mount Point                   | Contains                 |
|-----------------|-------------------------------|--------------------------|
| `postgres_data` | `/var/lib/postgresql/data`    | Database files           |
| `redis_data`    | `/data`                       | Redis AOF/RDB snapshots  |
| `upload_data`   | `/app/uploads`                | Uploaded PDFs            |
| `export_data`   | `/app/exports`                | Generated Excel files    |

> **Tip:** For production use S3 (`STORAGE_BACKEND=s3`) to decouple storage from the host.

---

## AWS ECS + RDS + ElastiCache

### 1. Infrastructure

| Resource              | Spec                                    |
|-----------------------|-----------------------------------------|
| **ECS Cluster**       | Fargate                                 |
| **API Task**          | CPU 512 / Memory 1024, 2–4 desired count |
| **Worker Task**       | CPU 1024 / Memory 2048, 1–3 desired count |
| **RDS**               | PostgreSQL 16, `db.t3.medium`, Multi-AZ  |
| **ElastiCache**       | Redis 7, `cache.t3.micro`, single-node   |
| **ALB**               | Internet-facing, target group → API:8000 |
| **S3 Bucket**         | Private, versioning enabled              |
| **ECR Repository**    | `docpipeline`                            |

### 2. Build & Push Image

```bash
ACCOUNT=123456789012
REGION=us-east-1

aws ecr get-login-password --region $REGION | \
  docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.$REGION.amazonaws.com

docker build -t docpipeline -f docker/Dockerfile .
docker tag docpipeline:latest $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/docpipeline:latest
docker push $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/docpipeline:latest
```

### 3. Task Definitions

**API Task** (default CMD — `uvicorn … --workers 4`):

```json
{
  "containerDefinitions": [{
    "name": "api",
    "image": "<account>.dkr.ecr.<region>.amazonaws.com/docpipeline:latest",
    "portMappings": [{"containerPort": 8000}],
    "environment": [
      {"name": "DATABASE_URL", "value": "postgresql+asyncpg://..."},
      {"name": "REDIS_URL",    "value": "redis://...elasticache...:6379/0"},
      {"name": "API_KEY",      "value": "..."},
      {"name": "STORAGE_BACKEND", "value": "s3"},
      {"name": "S3_BUCKET_NAME",  "value": "my-bucket"}
    ],
    "healthCheck": {
      "command": ["CMD-SHELL", "curl -f http://localhost:8000/health || exit 1"],
      "interval": 30,
      "timeout": 10,
      "retries": 3
    }
  }]
}
```

**Worker Task** (override CMD):

```json
{
  "containerDefinitions": [{
    "name": "worker",
    "image": "<account>.dkr.ecr.<region>.amazonaws.com/docpipeline:latest",
    "command": ["python", "-m", "app.worker"],
    "environment": [ /* same as API minus port mapping */ ]
  }]
}
```

### 4. Auto-Scaling

- **API:** Target-tracking on ALB `RequestCountPerTarget` (target: 500 req/min)
- **Worker:** Step-scaling on Redis queue length via custom CloudWatch metric

---

## Scaling Workers

Workers are stateless consumers. Scale horizontally:

```bash
# Docker Compose
docker-compose up -d --scale worker=5

# ECS
aws ecs update-service --cluster prod --service worker --desired-count 5
```

Each worker runs a tight loop:
1. `BRPOP docpipeline:jobs` (5 s timeout)
2. Load PDF from storage → extract → write to PostgreSQL
3. Update metrics gauges
4. Loop

No coordination needed — Redis guarantees exactly-once delivery per `BRPOP`.

---

## Monitoring & Alerting

### Prometheus Metrics

Scrape `GET /metrics` (text exposition format):

| Metric                                 | Type      | Description                           |
|----------------------------------------|-----------|---------------------------------------|
| `http_requests_total`                  | Counter   | Requests by method + path + status    |
| `http_request_duration_seconds`        | Histogram | Latency distribution                  |
| `documents_processed_total`            | Counter   | Documents by status (completed/failed)|
| `document_processing_duration_seconds` | Histogram | Extraction wall-clock time            |
| `active_jobs`                          | Gauge     | Jobs currently being processed        |

### Grafana Dashboard (suggested panels)

1. **Request rate** — `rate(http_requests_total[5m])`
2. **P95 latency** — `histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m]))`
3. **Error rate** — `rate(http_requests_total{status=~"5.."}[5m])`
4. **Queue depth** — custom CloudWatch metric or Redis `LLEN docpipeline:jobs`
5. **Active workers** — `active_jobs` gauge

### Alerting Rules

```yaml
- alert: HighErrorRate
  expr: rate(http_requests_total{status=~"5.."}[5m]) > 0.05
  for: 5m

- alert: QueueBacklog
  expr: redis_queue_length > 100
  for: 10m

- alert: SlowProcessing
  expr: histogram_quantile(0.95, rate(document_processing_duration_seconds_bucket[5m])) > 30
  for: 5m
```

---

## Backup & Recovery

### PostgreSQL

```bash
# Automated daily backup (cron)
pg_dump -Fc -h db-host -U postgres docpipeline > backup_$(date +%Y%m%d).dump

# Restore
pg_restore -h db-host -U postgres -d docpipeline backup_20250115.dump
```

On **RDS**: enable automated backups with 7-day retention + point-in-time recovery.

### Redis

Redis data is ephemeral (job queue). Loss of Redis means in-flight jobs are lost, but the API falls back to BackgroundTasks automatically. No backup needed.

### S3 / File Storage

- Enable **versioning** on the S3 bucket
- Enable **cross-region replication** for DR
- For local storage: mount a persistent volume and back it up separately

---

## Security Hardening

| Area              | Implementation                                        |
|-------------------|-------------------------------------------------------|
| **Authentication** | `API_KEY` env var → X-API-Key header (SHA-256 compare) |
| **Rate limiting**  | Per-IP sliding window (Redis-backed)                  |
| **File validation**| PDF-only, size cap, filename sanitisation               |
| **Container**      | Non-root user, read-only filesystem where possible     |
| **Network**        | Internal services (DB, Redis) on private subnets       |
| **TLS**            | Terminate at ALB / reverse proxy, not in the app       |
| **Secrets**        | Use AWS Secrets Manager or Vault, never commit `.env`  |

---

## Troubleshooting

| Symptom                            | Cause                                   | Fix                                            |
|------------------------------------|-----------------------------------------|-------------------------------------------------|
| `502 Bad Gateway`                  | API container not ready                 | Check health endpoint, increase `start_period`  |
| Upload returns `413`               | File exceeds `MAX_UPLOAD_SIZE_MB`       | Increase the env var                            |
| Documents stuck in `pending`       | Worker not running or Redis unreachable | Check worker logs, verify `REDIS_URL`           |
| `429 Too Many Requests`            | Rate limit hit                          | Increase `RATE_LIMIT_REQUESTS` or whitelist IP  |
| OCR extraction produces empty text | Tesseract not installed or wrong lang   | Ensure `tesseract-ocr` + `tesseract-ocr-eng`   |
| Connection pool exhausted          | High concurrency                        | Increase `DB_POOL_SIZE` + `DB_MAX_OVERFLOW`     |
| Worker crashes on large PDFs       | OOM                                     | Increase memory or reduce `CHUNK_SIZE_PAGES`    |
