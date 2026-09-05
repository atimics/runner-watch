import pytest
from fastapi.testclient import TestClient

from runner_web import main, operations


@pytest.mark.parametrize("path", ["/health/data", "/health/data/details", "/api/capabilities"])
@pytest.mark.parametrize(
    ("direct_host", "forwarded_host", "secret", "product"),
    [
        ("runner-watch-ratimics.fly.dev", "sports.rati.chat", "trusted-edge", "sports"),
        ("runner-watch-ratimics.fly.dev", "runners.rati.chat", "trusted-edge", "runners"),
        ("runner-watch-ratimics.fly.dev", "sports.rati.chat", "wrong-edge", "runners"),
        ("runner-watch-ratimics.fly.dev", "unknown.example", "trusted-edge", "runners"),
        ("sports.rati.chat", "", "", "sports"),
    ],
)
def test_operations_use_the_authenticated_public_product(
    monkeypatch, path, direct_host, forwarded_host, secret, product
) -> None:
    monkeypatch.setattr(main, "EDGE_PROXY_SECRET_VALUE", "trusted-edge")
    monkeypatch.setattr(main, "REQUIRE_EDGE_PROXY_SECRET", False)
    monkeypatch.setattr(operations, "OPERATIONS_TOKEN", "operator-token")
    seen = []

    def health(selected):
        seen.append(selected)
        return {"status": "degraded" if selected == "sports" else "ok"}

    def capabilities(_workers, *, product):
        seen.append(product)
        return {"product": product}

    monkeypatch.setattr(operations, "data_health", health)
    monkeypatch.setattr(operations, "runtime_capabilities", capabilities)
    client = TestClient(main.app, base_url=f"https://{direct_host}")
    response = client.get(
        path,
        headers={
            "x-forwarded-host": forwarded_host,
            "x-rati-edge-secret": secret,
            "authorization": "Bearer operator-token",
        },
    )
    assert seen == [product]
    if path == "/api/capabilities":
        assert response.status_code == 200
        assert response.json() == {"product": product}
    else:
        assert response.status_code == (503 if product == "sports" else 200)
