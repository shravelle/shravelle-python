# Python

```bash
pip install ./sdks/shravelle-sdk-python            # or "shravelle[auto]" from your index
opentelemetry-bootstrap -a install  # installs instrumentations for the libraries you use
```
Zero code: `shravelle-run gunicorn myproject.wsgi` (also uvicorn, celery, python app.py).

In code (before importing the framework):
```python
import shravelle
shravelle.configure(service_name="checkout")    # SHRAVELLE_API_KEY / SHRAVELLE_ENDPOINT from env
```
`configure(service_name, api_key, endpoint, environment, service_version, sample_rate, capture_logs=True,
log_level=logging.INFO, auto_instrument=True, metric_interval_ms=30000, resource_attributes)`.
The standard `logging` module is forwarded as logs with trace ids. Uncaught exceptions are reported.

Frameworks: Django, Flask, FastAPI/Starlette, Celery, aiohttp, Tornado, Pyramid; clients: requests, httpx,
urllib3, SQLAlchemy, psycopg/psycopg2, asyncpg, PyMySQL, pymongo, redis, kafka-python, boto3, grpc.

## API
```python
shravelle.notice_error(exc, {"order_id": 123})
shravelle.set_user(user.id, plan="pro")
shravelle.add_attributes(cart_value=1450)
shravelle.record_event("coupon_applied", code="DIWALI")
with shravelle.segment("pricing.calculate"):
    ...
@shravelle.segment("load_user")
def load_user(uid): ...
shravelle.counter("orders_total").add(1, {"method": "upi"})
shravelle.histogram("cart_value", unit="INR").record(1450)
shravelle.record_deployment("1.4.2", commit=os.environ.get("GIT_SHA", ""))
app = shravelle.wsgi_middleware(app)      # plain WSGI apps without a framework instrumentation
```
CLI: `shravelle deploy --version 1.4.2 --commit $GIT_SHA --description "..."` records a deployment marker from CI.


---
Part of [Shravelle](https://shravelle.com). Setup guide for every platform: [shravelle.com/docs](https://shravelle.com/docs.html) · Get your keys: [shravelle.com/get-started](https://shravelle.com/get-started.html)
