# -*- coding: utf-8 -*-
"""端到端 API 测试：异步重算、分组揭示、世代过期、并发幂等、重启恢复。"""
from __future__ import annotations

import threading

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """每个用例独立数据库文件；模块与 worker 全程单例，保证锁/批次一致。

    默认批次窗口 2 秒：并发/重复点击去重，人工顺序点击（测试中用 sleep
    跨过窗口）则正常推进到下一组。
    """
    from app import database, main

    main.get_worker().stop()  # 先停掉上一个用例可能残留的处理循环
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("RECOMMENDER_DB", str(db_path))
    monkeypatch.setenv("REVEAL_CLAIM_TTL", "2")

    database._DB_PATH = str(db_path)
    database._CLAIM_TTL_SECONDS = 2.0
    database._reset_reveal_batch()
    database.init_db()
    main.get_worker().start()
    from fastapi.testclient import TestClient

    with TestClient(main.app) as c:
        yield c, database, main.get_worker()
    main.get_worker().stop()


@pytest.fixture()
def batch_window_client(tmp_path, monkeypatch):
    """宽限窗口拉大的客户端，专测并发/重复点击只推进一次。"""
    from app import database, main

    main.get_worker().stop()
    db_path = tmp_path / "batch.db"
    monkeypatch.setenv("RECOMMENDER_DB", str(db_path))
    monkeypatch.setenv("REVEAL_CLAIM_TTL", "300")

    database._DB_PATH = str(db_path)
    database._CLAIM_TTL_SECONDS = 300.0
    database._reset_reveal_batch()
    database.init_db()
    main.get_worker().start()
    from fastapi.testclient import TestClient

    with TestClient(main.app) as c:
        yield c, database, main.get_worker()
    main.get_worker().stop()


@pytest.fixture()
def short_claim_client(tmp_path, monkeypatch):
    """极短窗口（10ms）的客户端，用于顺序三轮揭示而无需 sleep。"""
    from app import database, main

    main.get_worker().stop()
    db_path = tmp_path / "short.db"
    monkeypatch.setenv("RECOMMENDER_DB", str(db_path))
    monkeypatch.setenv("REVEAL_CLAIM_TTL", "0.01")

    database._DB_PATH = str(db_path)
    database._CLAIM_TTL_SECONDS = 0.01
    database._reset_reveal_batch()
    database.init_db()
    main.get_worker().start()
    from fastapi.testclient import TestClient

    with TestClient(main.app) as c:
        yield c, database, main.get_worker()
    main.get_worker().stop()


def _wait_done(c, timeout_s=10.0):
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        data = c.get("/api/status").json()
        job = data["job"]
        if job and job["status"] in ("done", "failed", "stale"):
            return data
        time.sleep(0.05)
    raise AssertionError("任务未在预期时间内结束")


def test_full_reveal_flow_in_groups_of_two(short_claim_client):
    import time as _time

    c, _, _ = short_claim_client
    data = _wait_done(c)
    assert data["plan"] is not None and data["plan"]["feasible"]

    revealed_total = 0
    for expected_len in (2, 4, 6):
        _time.sleep(0.05)  # 跨过极短批次窗口，模拟分三轮点击
        r = c.post("/api/reveal").json()
        assert r["reveal_outcome"]["advanced"] is True
        assert len(r["state"]["revealed"]) == expected_len
        revealed_total = expected_len
    assert revealed_total == 6

    # 再揭示不推进
    r = c.post("/api/reveal").json()
    assert r["reveal_outcome"]["advanced"] is False
    assert len(r["state"]["revealed"]) == 6

    # 队列满足主题配额与作者不重复
    queue = data["plan"]["queue"]
    assert len({q["author"] for q in queue}) == 6
    from app.articles_data import TOPICS
    assert sorted(q["topic"] for q in queue) == sorted(TOPICS)


def test_reveal_is_idempotent_under_concurrency(batch_window_client):
    c, database, _ = batch_window_client
    _wait_done(c)
    assert database._CLAIM_TTL_SECONDS == 300  # 显式并发去重窗口
    # 层 1：数据库写事务真并发 —— 5 个线程同时请求“下一组”，只允许推进一次
    outcomes = []
    errors = []

    def hit():
        try:
            outcomes.append(database.reveal_next_two())
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=hit) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    advanced = [o for o in outcomes if o["advanced"]]
    idempotent = [o for o in outcomes if o.get("idempotent")]
    assert len(advanced) == 1       # 只有一个请求真正推进
    assert len(idempotent) == 4     # 其余命中同一并发批次，不重复推进
    status = c.get("/api/status").json()
    assert len(status["state"]["revealed"]) == 2

    # 层 2：API 上同一进度点在批次窗口内重复请求同样不推进
    again = c.post("/api/reveal").json()
    assert again["reveal_outcome"]["advanced"] is False
    assert again["reveal_outcome"].get("idempotent") is True
    assert len(again["state"]["revealed"]) == 2


def test_concurrent_reveal_with_default_window_dedups(client):
    # 默认 2 秒窗口：并发线程同批只推进一次；跨过窗口后的顺序请求继续推进
    import time as _time

    c, database, _ = client
    _wait_done(c)
    outcomes = []

    def hit():
        outcomes.append(database.reveal_next_two())

    threads = [threading.Thread(target=hit) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for o in outcomes if o["advanced"]) == 1
    assert len(c.get("/api/status").json()["state"]["revealed"]) == 2

    _time.sleep(2.2)  # 跨过批次窗口，模拟用户稍后再次点击
    next_round = c.post("/api/reveal").json()
    assert next_round["reveal_outcome"]["advanced"] is True
    assert len(next_round["state"]["revealed"]) == 4


def test_sequential_reveals_progress_after_claim_expires(short_claim_client):
    import time as _time

    c, _, _ = short_claim_client
    _wait_done(c)
    for expected_len in (2, 4, 6):
        _time.sleep(0.05)  # 票据极短，稍候即过期，模拟用户分三轮顺序点击
        r = c.post("/api/reveal").json()
        assert r["reveal_outcome"]["advanced"] is True
        assert len(r["state"]["revealed"]) == expected_len

    tail = c.post("/api/reveal").json()
    assert tail["reveal_outcome"]["advanced"] is False
    assert len(tail["state"]["revealed"]) == 6
    ids = [x["article_id"] for x in tail["state"]["revealed"]]
    assert len(set(ids)) == 6


def test_condition_change_resets_reveal_progress_point(client):
    c, _, _ = client
    _wait_done(c)
    first = c.post("/api/reveal").json()
    old_ids = [x["article_id"] for x in first["state"]["revealed"]]
    assert len(old_ids) == 2

    # 条件变更产生新世代（新进度点），可继续揭示且已揭示前缀保留
    r = c.put("/api/condition", json={
        "interest": "新的兴趣：关注康复训练、运动损伤与高原体能",
    })
    assert r.status_code == 200
    data = _wait_done(c)
    assert [x["article_id"] for x in data["state"]["revealed"]] == old_ids

    moved = c.post("/api/reveal").json()
    assert moved["reveal_outcome"]["advanced"] is True
    assert len(moved["state"]["revealed"]) == 4
    assert [x["article_id"] for x in moved["state"]["revealed"][:2]] == old_ids


def test_condition_change_keeps_prefix_and_recomputes(client):
    c, _, _ = client
    _wait_done(c)
    c.post("/api/reveal")
    before = c.get("/api/status").json()
    prefix_ids = [x["article_id"] for x in before["state"]["revealed"]]
    assert len(prefix_ids) == 2

    payload = {
        "interest": "我关注足球比赛的战术数据分析和马拉松耐力训练",
        "quotas": before["state"]["quotas"],
        "compute_delay_ms": 0,
    }
    r = c.put("/api/condition", json=payload)
    assert r.status_code == 200
    data = _wait_done(c)
    # 世代推进，前缀不变
    assert data["state"]["generation"] == before["state"]["generation"] + 1
    assert [x["article_id"] for x in data["state"]["revealed"]] == prefix_ids
    # 新计划的前两个位置仍是原文章
    queue = data["plan"]["queue"]
    assert [queue[0]["article_id"], queue[1]["article_id"]] == prefix_ids


def test_slow_recompute_stale_result_never_revealed(client):
    c, database, worker = client
    _wait_done(c)
    # 开启一个 2 秒的慢重算
    r = c.put("/api/condition", json={
        "interest": "慢重算条件 A：关注帆船与海岛旅行",
        "compute_delay_ms": 2000,
    }).json()
    gen_a = r["state"]["generation"]
    assert r["job"]["status"] in ("pending", "running")

    # 重算窗口内立刻改条件（无延迟）
    r2 = c.put("/api/condition", json={
        "interest": "慢重算条件 B：关注家庭烘焙与低油低盐饮食",
        "compute_delay_ms": 0,
    }).json()
    gen_b = r2["state"]["generation"]
    assert gen_b == gen_a + 1

    # 等待所有任务结算
    import time
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        job_a = database.get_job(gen_a)
        job_b = database.get_job(gen_b)
        if job_b["status"] == "done" and job_a["status"] in ("stale", "done", "failed"):
            break
        time.sleep(0.05)

    job_a = database.get_job(gen_a)
    job_b = database.get_job(gen_b)
    # 旧世代结果必须被标记 stale，且没有可用 result
    assert job_a["status"] == "stale"
    assert job_b["status"] == "done"

    status = c.get("/api/status").json()
    # 当前计划来自新世代；揭示只会采用新条件的推荐
    assert status["job"]["generation"] == gen_b
    out = c.post("/api/reveal").json()
    revealed = out["state"]["revealed"]
    plan_b_ids = [q["article_id"] for q in job_b["result"]["queue"][:2]]
    assert [x["article_id"] for x in revealed] == plan_b_ids


def test_failed_job_does_not_advance(client, monkeypatch):
    c, database, worker = client
    _wait_done(c)
    c.post("/api/reveal")  # 已有 2 篇揭示

    # 让求解器抛错，模拟重算失败
    import app.worker as wmod
    def boom(*args, **kwargs):
        raise RuntimeError("simulated compute failure")
    monkeypatch.setattr(wmod, "solve_queue", boom)

    c.put("/api/condition", json={"interest": "触发失败的兴趣", "compute_delay_ms": 0})
    import time
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = database.get_latest_job()
        if job["status"] == "failed":
            break
        time.sleep(0.05)
    assert database.get_latest_job()["status"] == "failed"

    # 揭示被拒绝，进度不变
    r = c.post("/api/reveal")
    assert r.status_code == 409
    status = c.get("/api/status").json()
    assert len(status["state"]["revealed"]) == 2


def test_exclusion_flow_and_revealed_protection(client):
    c, _, _ = client
    _wait_done(c)
    first = c.post("/api/reveal").json()
    revealed_id = first["state"]["revealed"][0]["article_id"]

    # 已揭示文章不能排除
    r = c.post("/api/exclusions", json={"article_id": revealed_id})
    assert r.status_code == 409

    # 排除一篇未揭示文章可以成功并触发新世代
    candidates = [a["id"] for a in first["articles"]
                  if a["id"] not in {x["article_id"] for x in first["state"]["revealed"]}]
    r2 = c.post("/api/exclusions", json={"article_id": candidates[0]}).json()
    assert candidates[0] in r2["state"]["excluded_ids"]
    data = _wait_done(c)
    # 新计划中不得出现被排除文章
    ids = {q["article_id"] for q in data["plan"]["queue"]}
    assert candidates[0] not in ids

    # 取消排除
    r3 = c.delete(f"/api/exclusions/{candidates[0]}").json()
    assert candidates[0] not in r3["state"]["excluded_ids"]


def test_infeasible_condition_stops_reveal_but_keeps_prefix(client):
    c, database, _ = client
    _wait_done(c)
    c.post("/api/reveal")  # 2 篇揭示
    status = c.get("/api/status").json()
    revealed_topics = [x["topic"] for x in status["state"]["revealed"]]

    # 把已揭示主题的配额改为 0，制造与前缀不兼容的条件，API 应直接 409
    bad_quotas = {t: 1 for t in status["topics"]}
    bad_quotas[revealed_topics[0]] = 0
    # 总和仍要保持 6 才能通过求和校验，把另一个未揭示主题调成 2
    other = next(t for t in status["topics"] if t not in revealed_topics)
    bad_quotas[other] = 2
    r = c.put("/api/condition", json={"quotas": bad_quotas})
    assert r.status_code == 409

    # 直接在求解层构造不可行：排除某主题全部 5 篇
    from app.articles_data import ARTICLES
    target_topic = next(t for t in status["topics"] if t not in revealed_topics)
    ids = [a["id"] for a in ARTICLES if a["topic"] == target_topic]
    for aid in ids:
        c.post("/api/exclusions", json={"article_id": aid})
    data = _wait_done(c)
    assert data["plan"]["feasible"] is False
    # 揭示不推进、前缀保留
    out = c.post("/api/reveal").json()
    assert out["reveal_outcome"]["advanced"] is False
    assert len(out["state"]["revealed"]) == 2


def test_quota_sum_validation(client):
    c, _, _ = client
    _wait_done(c)
    status = c.get("/api/status").json()
    bad = dict(status["state"]["quotas"])
    bad["人工智能"] = 2  # 总和变 7
    r = c.put("/api/condition", json={"quotas": bad})
    assert r.status_code == 422


def test_restart_retains_revealed_prefix(tmp_path, monkeypatch):
    db_file = tmp_path / "restart.db"
    monkeypatch.setenv("RECOMMENDER_DB", str(db_file))

    from app import database, main
    from fastapi.testclient import TestClient

    main.get_worker().stop()
    database._DB_PATH = str(db_file)
    database._CLAIM_TTL_SECONDS = 0.01
    database._reset_reveal_batch()
    database.init_db()
    main.get_worker().start()
    with TestClient(main.app) as c1:
        _wait_done(c1)
        c1.post("/api/reveal")
        import time as _time
        _time.sleep(0.05)
        c1.post("/api/reveal")
        before = c1.get("/api/status").json()
    main.get_worker().stop()

    # 模拟进程重启：worker 停止后重新启动，数据库文件保持不变
    database.init_db()
    main.get_worker().start()
    try:
        with TestClient(main.app) as c2:
            after = c2.get("/api/status").json()
            assert [x["article_id"] for x in after["state"]["revealed"]] == \
                   [x["article_id"] for x in before["state"]["revealed"]]
            assert after["state"]["generation"] == before["state"]["generation"]
            assert after["state"]["interest"] == before["state"]["interest"]
    finally:
        main.get_worker().stop()


# ---------------- 条件版本核对：双窗口草稿保护 ----------------


def test_stale_form_submit_rejected_and_state_untouched(client):
    """甲保存新兴趣后，乙用旧表单整体提交：必须 409 拒绝，
    且甲的条件、已揭示前缀、世代、版本、任务全部不变，也不登记新任务。"""
    c, database, _ = client
    data = _wait_done(c)
    v0 = data["state"]["condition_version"]
    gen0 = data["state"]["generation"]
    old_interest = data["state"]["interest"]

    # 先揭示两篇，验证拒绝时已揭示前缀不变
    c.post("/api/reveal")
    revealed_before = c.get("/api/status").json()["state"]["revealed"]
    assert len(revealed_before) == 2

    # 窗口甲保存新兴趣（基于最新版本）
    r = c.put("/api/condition", json={
        "interest": "甲的新兴趣：关注高原训练与运动营养",
        "base_version": v0,
    })
    assert r.status_code == 200
    after_a = r.json()
    v1 = after_a["state"]["condition_version"]
    gen1 = after_a["state"]["generation"]
    assert v1 == v0 + 1 and gen1 == gen0 + 1
    _wait_done(c)

    # 窗口乙仍持有基于 v0 的旧表单（内含旧兴趣），只改了配额便整体提交
    stale_quotas = {t: 1 for t in data["topics"]}
    stale_quotas["人工智能"] = 2
    stale_quotas["医疗健康"] = 0
    r2 = c.put("/api/condition", json={
        "interest": old_interest,
        "quotas": stale_quotas,
        "base_version": v0,
    })
    assert r2.status_code == 409
    detail = r2.json()["detail"]
    assert detail["code"] == "condition_conflict"
    assert detail["base_version"] == v0
    assert detail["current_version"] == v1
    assert detail["current_interest"] == "甲的新兴趣：关注高原训练与运动营养"
    assert detail["current_quotas"] == after_a["state"]["quotas"]

    # 拒绝是原子的：甲的兴趣没有被旧表单改回，其余状态原样
    st = c.get("/api/status").json()
    assert st["state"]["interest"] == "甲的新兴趣：关注高原训练与运动营养"
    assert st["state"]["condition_version"] == v1
    assert st["state"]["generation"] == gen1
    assert st["state"]["revealed"] == revealed_before
    # 仅成功提交才启动重算：最新任务仍是甲的世代，没有新任务登记
    assert database.get_latest_job()["generation"] == gen1
    assert database.get_job(gen1)["status"] == "done"


def test_submit_with_matching_version_succeeds_and_starts_recompute(client):
    """携带当前版本的提交正常生效，且只有它才登记新的重算任务。"""
    c, database, _ = client
    data = _wait_done(c)
    v0 = data["state"]["condition_version"]
    gen0 = data["state"]["generation"]

    r = c.put("/api/condition", json={
        "interest": "基于最新版本提交：关注数据可视化与图表叙事",
        "base_version": v0,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["state"]["condition_version"] == v0 + 1
    assert body["state"]["generation"] == gen0 + 1
    assert body["job"]["generation"] == gen0 + 1
    assert body["job"]["status"] in ("pending", "running", "done")
    done = _wait_done(c)
    assert done["state"]["interest"] == "基于最新版本提交：关注数据可视化与图表叙事"


def test_reset_invalidates_stale_form(client):
    """重置演示把世代归 1，但条件版本继续递增：重置前的旧表单提交仍被拒绝。"""
    c, _, _ = client
    data = _wait_done(c)
    v0 = data["state"]["condition_version"]
    gen0 = data["state"]["generation"]
    assert gen0 == 1  # 初始世代；重置后也会回到 1，单靠世代号无法识别重置

    c.post("/api/reset")
    after_reset = _wait_done(c)
    assert after_reset["state"]["generation"] == 1
    assert after_reset["state"]["condition_version"] != v0

    r = c.put("/api/condition", json={
        "interest": "重置前旧表单里的兴趣",
        "base_version": v0,
    })
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "condition_conflict"
    st = c.get("/api/status").json()
    assert st["state"]["generation"] == 1  # 世代未被旧表单推进
    assert st["state"]["interest"] == after_reset["state"]["interest"]


def test_exclusion_change_invalidates_stale_form(client):
    """其他窗口排除/取消排除文章同样推进条件版本，旧表单提交被拒绝。"""
    c, _, _ = client
    data = _wait_done(c)
    v0 = data["state"]["condition_version"]
    candidate = data["articles"][0]["id"]

    r = c.post("/api/exclusions", json={"article_id": candidate})
    assert r.status_code == 200
    assert r.json()["state"]["condition_version"] == v0 + 1

    r2 = c.put("/api/condition", json={
        "interest": "基于旧版本的兴趣",
        "base_version": v0,
    })
    assert r2.status_code == 409
    assert r2.json()["detail"]["code"] == "condition_conflict"

    # 取消排除也推进版本
    v1 = r.json()["state"]["condition_version"]
    r3 = c.delete(f"/api/exclusions/{candidate}")
    assert r3.status_code == 200
    assert r3.json()["state"]["condition_version"] == v1 + 1


def test_reveal_does_not_invalidate_form(client):
    """揭示只推进阅读进度、不改变条件：同版本表单仍可正常提交。"""
    c, _, _ = client
    data = _wait_done(c)
    v0 = data["state"]["condition_version"]

    c.post("/api/reveal")
    st = c.get("/api/status").json()
    assert st["state"]["condition_version"] == v0

    r = c.put("/api/condition", json={
        "interest": "揭示后基于同版本提交：关注赛事数据分析",
        "base_version": v0,
    })
    assert r.status_code == 200


def test_submit_without_base_version_keeps_legacy_behavior(client):
    """旧客户端不带 base_version：保持原有行为（不核对版本）。"""
    c, _, _ = client
    _wait_done(c)
    r = c.put("/api/condition", json={"interest": "不带版本的旧客户端提交"})
    assert r.status_code == 200
    assert r.json()["state"]["interest"] == "不带版本的旧客户端提交"


def test_rejected_submit_does_not_disturb_running_job(client):
    """慢重算期间旧表单提交被拒绝：进行中的任务不受干扰，
    其结果照常落库成为当前推荐（拒绝没有开启新世代、没有把任务标 stale）。"""
    c, database, _ = client
    data = _wait_done(c)
    v0 = data["state"]["condition_version"]

    # 甲开启 1.5 秒慢重算
    r = c.put("/api/condition", json={
        "interest": "甲的慢重算条件：关注海岛旅行与潜水装备",
        "compute_delay_ms": 1500,
    })
    gen_a = r.json()["state"]["generation"]
    v_a = r.json()["state"]["condition_version"]

    # 乙用旧版本表单提交 -> 拒绝
    r2 = c.put("/api/condition", json={
        "interest": "乙的旧表单兴趣",
        "base_version": v0,
    })
    assert r2.status_code == 409

    # 甲的慢任务正常完成并落库，推荐来自甲的条件
    import time
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        if database.get_job(gen_a)["status"] == "done":
            break
        time.sleep(0.05)
    assert database.get_job(gen_a)["status"] == "done"
    st = c.get("/api/status").json()
    assert st["state"]["generation"] == gen_a
    assert st["state"]["condition_version"] == v_a
    assert st["state"]["interest"] == "甲的慢重算条件：关注海岛旅行与潜水装备"
    assert st["plan"] is not None and st["plan"]["feasible"]


def test_concurrent_submits_same_version_exactly_one_wins(client):
    """两个窗口同时基于同一版本提交：写事务内原子核对保证只有一个成功。"""
    c, database, _ = client
    data = _wait_done(c)
    v0 = data["state"]["condition_version"]

    statuses = []

    def submit(i):
        r = c.put("/api/condition", json={
            "interest": f"并发提交 {i}：关注主题方向 {i}",
            "base_version": v0,
        })
        statuses.append(r.status_code)

    threads = [threading.Thread(target=submit, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(statuses) == [200, 409, 409, 409]
    st = c.get("/api/status").json()
    assert st["state"]["condition_version"] == v0 + 1  # 版本只推进一次


def test_version_check_is_atomic_at_database_layer(client):
    """数据库层直接并发：expected_version 核对与写入在同一事务内完成。"""
    c, database, _ = client
    data = _wait_done(c)
    v0 = data["state"]["condition_version"]

    outcomes = []

    def submit(i):
        try:
            database.begin_new_generation(
                f"数据库层并发兴趣 {i}", None, 0, expected_version=v0
            )
            outcomes.append("ok")
        except database.ConditionConflict:
            outcomes.append("conflict")

    threads = [threading.Thread(target=submit, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes.count("ok") == 1
    assert outcomes.count("conflict") == 3
    assert database.get_state()["condition_version"] == v0 + 1
