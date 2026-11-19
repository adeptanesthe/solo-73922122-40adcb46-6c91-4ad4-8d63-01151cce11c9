# -*- coding: utf-8 -*-
"""FastAPI 应用：推荐队列的条件管理、异步重查、分组揭示与演示页面。"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import database
from .articles_data import ARTICLES, TOPICS
from .tfidf_model import get_model
from .worker import get_worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("recommender")

DEFAULT_INTEREST = "我想了解人工智能模型训练与数据驱动的分析方法"
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


class ConditionIn(BaseModel):
    interest: str | None = Field(default=None, description="兴趣短文")
    quotas: dict[str, int] | None = Field(default=None, description="各主题精确篇数")
    compute_delay_ms: int = Field(default=0, ge=0, le=30000)
    base_version: int | None = Field(
        default=None,
        ge=1,
        description="开始编辑时的条件版本；提供时服务器原子核对，"
        "其他窗口已修改条件或重置队列则整体拒绝（409），"
        "不改动任何状态也不启动重算。缺省为旧客户端兼容行为（不核对）。",
    )


class ExclusionIn(BaseModel):
    article_id: int = Field(ge=1)
    compute_delay_ms: int = Field(default=0, ge=0, le=30000)

def _validate_quotas(quotas: dict[str, int]) -> None:
    unknown = set(quotas) - set(TOPICS)
    if unknown:
        raise HTTPException(422, f"未知主题: {sorted(unknown)}")
    missing = set(TOPICS) - set(quotas)
    if missing:
        raise HTTPException(422, f"缺少主题配额: {sorted(missing)}")
    if any(v < 0 for v in quotas.values()):
        raise HTTPException(422, "配额不能为负数")
    if sum(quotas.values()) != 6:
        raise HTTPException(422, f"六个主题配额之和必须为 6，当前为 {sum(quotas.values())}")


def _validate_interest(interest: str) -> None:
    if not interest or not interest.strip():
        raise HTTPException(422, "兴趣短文不能为空")
    if len(interest) > 2000:
        raise HTTPException(422, "兴趣短文过长（上限 2000 字）")


def _article_brief(a: dict) -> dict:
    return {"id": a["id"], "title": a["title"], "topic": a["topic"], "author": a["author"]}


def _status_payload() -> dict:
    state = database.get_state()
    job = database.get_latest_job()
    plan = None
    if job and job["status"] == "done" and job["generation"] == state["generation"]:
        plan = job["result"]
    return {
        "state": state,
        "job": job,
        "plan": plan,
        "topics": TOPICS,
        "articles": [_article_brief(a) for a in ARTICLES],
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    # 预热 TF-IDF（仅文章拟合），失败不影响启动，worker 内会再试
    try:
        get_model()
    except Exception:  # pragma: no cover
        log.exception("TF-IDF 预热失败")
    get_worker().start()
    try:
        yield
    finally:
        get_worker().stop()


app = FastAPI(title="技术编辑选题推荐系统", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/status")
def status() -> dict:
    return _status_payload()


@app.get("/api/articles/{article_id}")
def article_detail(article_id: int) -> dict:
    for a in ARTICLES:
        if a["id"] == article_id:
            return a
    raise HTTPException(404, "文章不存在")


def _conflict_detail(current: dict, base_version: int | None) -> dict:
    """409 结构化的冲突详情：前端据此保留草稿并展示服务器当前条件。"""
    return {
        "code": "condition_conflict",
        "message": (
            "提交被拒绝：你编辑期间，条件已被其他窗口修改或队列已被重置。"
            "你的草稿未生效，服务器条件保持不变；请查看最新条件后重新提交。"
        ),
        "base_version": base_version,
        "current_version": current["condition_version"],
        "current_interest": current["interest"],
        "current_quotas": current["quotas"],
        "generation": current["generation"],
    }


@app.put("/api/condition")
def update_condition(body: ConditionIn) -> dict:
    """修改兴趣和/或配额，开启新世代并触发异步重算。

    乐观并发控制：携带 base_version 的提交会先快速比对、再在写事务内
    原子核对条件版本；版本过期（其他窗口已改条件或已重置）时返回 409，
    原条件、已揭示前缀与当前任务均不变，也不登记新的重算任务。
    """
    current = database.get_state()
    # 快速路径：版本明显过期时直接拒绝，避免白做前置可行性检查；
    # 竞态窗口由 begin_new_generation 写事务内的原子核对兜底。
    if (
        body.base_version is not None
        and current["condition_version"] != body.base_version
    ):
        raise HTTPException(
            409, detail=_conflict_detail(current, body.base_version)
        )
    new_quotas = body.quotas if body.quotas is not None else current["quotas"]
    _validate_quotas(new_quotas)
    new_interest = body.interest if body.interest is not None else current["interest"]
    _validate_interest(new_interest)

    # 与已揭示前缀的兼容性前置检查（详细不可行原因由重算任务给出）
    from .optimizer import solve_queue  # 局部导入避免循环
    model = get_model()
    check = solve_queue(
        relevance=model.relevance_vector(new_interest),
        quotas=new_quotas,
        prefix=current["revealed"],
        excluded_ids=set(current["excluded_ids"]),
    )
    if not check.feasible:
        raise HTTPException(409, f"新条件与已揭示前缀不兼容: {check.reason}")

    try:
        database.begin_new_generation(
            interest=new_interest,
            quotas=new_quotas,
            compute_delay_ms=body.compute_delay_ms,
            expected_version=body.base_version,
        )
    except database.ConditionConflict as exc:
        raise HTTPException(
            409, detail=_conflict_detail(exc.current, body.base_version)
        ) from None
    return _status_payload()


@app.post("/api/exclusions")
def add_exclusion(body: ExclusionIn) -> dict:
    if not any(a["id"] == body.article_id for a in ARTICLES):
        raise HTTPException(404, "文章不存在")
    try:
        database.add_exclusion(body.article_id, body.compute_delay_ms)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return _status_payload()


@app.delete("/api/exclusions/{article_id}")
def remove_exclusion(article_id: int, compute_delay_ms: int = 0) -> dict:
    try:
        database.remove_exclusion(article_id, compute_delay_ms)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return _status_payload()


@app.post("/api/reveal")
def reveal() -> dict:
    """揭示下一组两篇；同条件同进度下并发只推进一次。"""
    try:
        outcome = database.reveal_next_two()
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    payload = _status_payload()
    payload["reveal_outcome"] = outcome
    return payload


@app.post("/api/reset")
def reset_demo() -> dict:
    """重置演示：清空揭示、排除与任务，世代回到 1。"""
    database.reset_all(DEFAULT_INTEREST, {t: 1 for t in TOPICS})
    return _status_payload()
