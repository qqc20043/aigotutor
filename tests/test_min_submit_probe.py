"""最小化提交覆盖探针（诊断用，跑完即删）。"""

import uuid


async def test_min_submit(client):
    r = await client.post(
        "/api/auth/register",
        json={"username": f"m{uuid.uuid4().hex[:8]}", "password": "test123456"},
    )
    token = r.json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    p = await client.post(
        "/api/problems", json={"title": "探针题", "statement": "s", "tags": ["模拟"]}
    )
    pid = p.json()["id"]
    r = await client.post(
        "/api/submissions",
        json={
            "problem_id": pid,
            "language": "python",
            "code": "print(1)",
            "verdict": "AC",
            "judge_mode": "caller",
        },
    )
    assert r.status_code == 201, r.text
