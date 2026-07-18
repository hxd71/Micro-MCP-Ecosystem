# Internal API Notes

## health_check endpoint

Path: /api/health
Method: GET
Expected: status=ok, uptime_seconds

## deploy endpoint

Path: /api/deploy
Method: POST
Body:
- service_name: string
- image_tag: string

Failure handling:
- Retry up to 2 times for transient network failures.
- Always log deployment id for tracing.

## rollback endpoint

Path: /api/rollback
Method: POST
Body:
- service_name: string
- target_version: string

Use rollback when deploy verification fails or error rate spikes.
