"""
DocuFlow - Load Testing with Locust
Tests: auth, single document processing, batch, templates.

Run:
  pip install locust
  locust -f locustfile.py --host=http://localhost:8000 --users=50 --spawn-rate=5
  
  # Headless mode
  locust -f locustfile.py --host=http://localhost:8000 \
    --users=100 --spawn-rate=10 --run-time=5m --headless \
    --csv=results/load_test
"""
import base64
import io
import random
import struct
import zlib
from locust import HttpUser, task, between, events
from locust.exception import RescheduleTask


def make_test_png(width=100, height=100) -> bytes:
    """Generate a minimal valid PNG for upload tests."""
    def chunk(t, d):
        import zlib as z, struct as s
        c = t + d
        return s.pack(">I", len(d)) + c + s.pack(">I", z.crc32(c) & 0xFFFFFFFF)
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    raw = b"\x00" + bytes([random.randint(0, 255) for _ in range(width * 3)]) * height
    idat = chunk(b"IDAT", zlib.compress(raw))
    iend = chunk(b"IEND", b"")
    return sig + ihdr + idat + iend


TEST_PNG = make_test_png()


class DocuFlowUser(HttpUser):
    """Simulates a real API client: authenticate, process documents, check results."""

    wait_time = between(0.5, 2.0)
    token: str = None
    last_document_id: str = None

    def on_start(self):
        """Authenticate before starting tasks."""
        self._authenticate()

    def _authenticate(self):
        with self.client.post(
            "/v2/auth/token",
            json={"client_id": "demo_client", "client_secret": "demo_secret", "grant_type": "client_credentials"},
            catch_response=True,
            name="POST /v2/auth/token",
        ) as resp:
            if resp.status_code == 200:
                self.token = resp.json().get("access_token")
            else:
                resp.failure(f"Auth failed: {resp.status_code}")
                raise RescheduleTask()

    def _headers(self) -> dict:
        if not self.token:
            self._authenticate()
        return {"Authorization": f"Bearer {self.token}"}

    @task(40)  # Most common operation
    def process_single_document(self):
        """Process a single document — core workload."""
        with self.client.post(
            "/v2/process",
            headers=self._headers(),
            data={
                "template_id": random.choice(["CNI_FR_v2", "PASSPORT_INT_v1"]),
                "modules": random.choice(["extraction", "extraction,validation", "extraction,validation,fuzzy"]),
                "priority": random.choice(["high", "normal", "normal", "normal", "low"]),
            },
            files={"file": ("test.png", io.BytesIO(TEST_PNG), "image/png")},
            catch_response=True,
            name="POST /v2/process",
        ) as resp:
            if resp.status_code == 200:
                data = resp.json()
                self.last_document_id = data.get("document_id")
                confidence = data.get("overall_confidence", 0)
                if confidence is not None and confidence < 0.5:
                    resp.failure(f"Low confidence: {confidence}")
            elif resp.status_code == 429:
                resp.failure("Rate limited")
            else:
                resp.failure(f"Process failed: {resp.status_code}")

    @task(20)
    def get_result(self):
        """Retrieve a previously processed document result."""
        if not self.last_document_id:
            return
        with self.client.get(
            f"/v2/results/{self.last_document_id}",
            headers=self._headers(),
            catch_response=True,
            name="GET /v2/results/{id}",
        ) as resp:
            if resp.status_code not in (200, 404):
                resp.failure(f"Get result failed: {resp.status_code}")

    @task(10)
    def list_templates(self):
        """List available templates."""
        with self.client.get(
            "/v2/templates",
            headers=self._headers(),
            catch_response=True,
            name="GET /v2/templates",
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"List templates failed: {resp.status_code}")
            elif len(resp.json()) == 0:
                resp.failure("No templates returned")

    @task(10)
    def get_stats(self):
        """Monitoring stats poll."""
        with self.client.get(
            "/v2/stats",
            headers=self._headers(),
            catch_response=True,
            name="GET /v2/stats",
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"Stats failed: {resp.status_code}")

    @task(5)
    def health_check(self):
        """Health probe (simulates load balancer)."""
        with self.client.get(
            "/health",
            catch_response=True,
            name="GET /health",
        ) as resp:
            if resp.status_code != 200 or resp.json().get("status") != "healthy":
                resp.failure("Health check failed")

    @task(5)
    def submit_batch(self):
        """Submit a small batch job."""
        with self.client.post(
            "/v2/process/batch",
            headers=self._headers(),
            json={
                "documents": [
                    {"url": f"https://example.com/doc{i}.jpg", "template_id": "CNI_FR_v2"}
                    for i in range(random.randint(2, 10))
                ],
                "priority": "normal",
                "modules": ["extraction", "validation"],
            },
            catch_response=True,
            name="POST /v2/process/batch",
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"Batch submit failed: {resp.status_code}")

    @task(5)
    def reauthenticate(self):
        """Periodic token refresh."""
        self._authenticate()

    @task(5)
    def get_specific_template(self):
        with self.client.get(
            "/v2/templates/CNI_FR_v2",
            headers=self._headers(),
            catch_response=True,
            name="GET /v2/templates/{id}",
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"Get template failed: {resp.status_code}")


class HeavyBatchUser(HttpUser):
    """Simulates bulk processing client sending large batches."""
    wait_time = between(5, 15)

    def on_start(self):
        resp = self.client.post(
            "/v2/auth/token",
            json={"client_id": "demo_client", "client_secret": "demo_secret", "grant_type": "client_credentials"}
        )
        self.token = resp.json().get("access_token", "")

    @task
    def large_batch(self):
        """Submit 50-document batch."""
        self.client.post(
            "/v2/process/batch",
            headers={"Authorization": f"Bearer {self.token}"},
            json={
                "documents": [{"url": f"https://storage.example.com/doc{i}.pdf"} for i in range(50)],
                "priority": "low",
                "modules": ["extraction", "validation", "fuzzy"],
            },
            name="POST /v2/process/batch (large)",
        )


# ── Custom metrics ──────────────────────────────────────────────────────
@events.request.add_listener
def on_request(request_type, name, response_time, response_length, response, context, exception, **kwargs):
    """Custom event handler for detailed metrics logging."""
    if exception:
        print(f"FAIL [{request_type}] {name}: {exception}")
    elif response and response.status_code >= 500:
        print(f"ERROR [{request_type}] {name}: HTTP {response.status_code}")


@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    print("\n" + "="*50)
    print("DocuFlow Load Test Starting")
    print(f"Target: {environment.host}")
    print("="*50 + "\n")


@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    stats = environment.stats
    print("\n" + "="*50)
    print("DocuFlow Load Test Complete")
    print(f"Total requests: {stats.total.num_requests}")
    print(f"Failed: {stats.total.num_failures}")
    print(f"RPS: {stats.total.current_rps:.1f}")
    print(f"P95 latency: {stats.total.get_response_time_percentile(0.95):.0f}ms")
    print("="*50 + "\n")
