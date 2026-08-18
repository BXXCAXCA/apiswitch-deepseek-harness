"""Administrative API for the v2 product structure."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apiswitch.api.deps import get_db
from apiswitch.catalog.templates import get_template, list_templates
from apiswitch.config import settings
from apiswitch.db.base import utc_now
from apiswitch.db.models import AgentConfig, ApiToken, ApiTokenUnifiedModel, AuxiliaryModel, AuxiliarySettings, AuxiliaryWorkflow, Budget, CircuitBreaker, ProviderHealth, ProviderInstance, QuotaSnapshot, RequestLog, SchemaMetadata, SystemSetting, UnifiedModel, UnifiedModelCandidate, UpstreamModel, UsageHistory
from apiswitch.routing.engine import plan_auxiliary, protocol_matrix, route_candidates, structured_error
from apiswitch.routing.capabilities import ALL_CAPABILITIES, infer_model_characteristics, normalize_capabilities, normalize_capability_map, normalize_workflow_steps
from apiswitch.security.crypto import SecretCryptoError, secret_crypto
from apiswitch.security.tokens import generate_api_token, hash_api_token, token_prefix
from apiswitch.harness_runtime import harness_token_path
from apiswitch.security.outbound import OutboundURLRejected,validate_outbound_url
from apiswitch.services.budget_enforcement import budget_limit_value, budget_usage_value, refresh_budget_period
from apiswitch.db.models import WebDAVProfile, WebDAVSyncLog

router = APIRouter(prefix="/api/admin", tags=["Admin"])


def _runtime_data_dir():
    if settings.plugin_mode:
        return harness_token_path().parent
    from apiswitch.desktop import _runtime_dir
    return _runtime_dir()


def _error(code: str, message: str, stage: str = "validation", details: dict[str, Any] | None = None) -> HTTPException:
    return HTTPException(status_code=409 if code == "resource_in_use" else 422, detail=structured_error(code, message, stage, details=details)["error"])


_BUDGET_SCOPES={"global","token","api_token","unified_model","provider","provider_instance","upstream_model"}
_BUDGET_MODES={"token_cost","request_count"}
_BUDGET_PERIODS={"rolling_5_hours","calendar_day","calendar_week","calendar_month"}
_BUDGET_ACTIONS={"warn","warn_only","reject","fallback_to_free","fallback_to_cheapest","degrade"}


def _validate_budget_target(db:Session,scope:str,scope_id:Any)->str|None:
    if scope=="global":return None
    if scope_id in {None,""}:raise _error("validation_error","请选择预算作用对象")
    value=str(scope_id)
    try:numeric_id=int(value)
    except (TypeError,ValueError):numeric_id=None
    if scope in {"token","api_token"} and (numeric_id is None or db.get(ApiToken,numeric_id) is None):raise _error("validation_error","预算引用的 API Token 不存在")
    if scope in {"provider","provider_instance"} and (numeric_id is None or db.get(ProviderInstance,numeric_id) is None):raise _error("validation_error","预算引用的供应商不存在")
    if scope=="upstream_model" and (numeric_id is None or db.get(UpstreamModel,numeric_id) is None):raise _error("validation_error","预算引用的上游模型不存在")
    if scope=="unified_model":
        exists=db.get(UnifiedModel,numeric_id) if numeric_id is not None else db.scalar(select(UnifiedModel).where(UnifiedModel.name==value))
        if exists is None:raise _error("validation_error","预算引用的统一模型不存在")
    return value


def _budget_out(row:Budget)->dict[str,Any]:
    period_end=refresh_budget_period(row);limit=budget_limit_value(row);usage=budget_usage_value(row)
    percent=round(float(usage)/float(limit)*100,2) if limit is not None and float(limit)>0 else None
    return {"id":row.id,"name":row.name,"scope":row.scope,"scope_id":row.scope_id,"billing_mode":row.billing_mode,"period_type":row.period_type,"limit_value":limit,"usage_value":usage,"monthly_limit":row.monthly_limit,"request_limit":row.request_limit,"spent_amount":row.spent_amount,"request_count":row.request_count,"currency":row.currency,"period_started_at":row.period_started_at,"period_ends_at":period_end,"usage_percent":percent,"alert_triggered":percent is not None and percent>=row.alert_threshold_percent,"alert_threshold_percent":row.alert_threshold_percent,"enforcement_action":row.enforcement_action,"enabled":row.enabled}


def _provider_base_url(template: dict[str, Any], value: str) -> str:
    """Carry a template's required API path when only its host is replaced."""
    if str(template.get("key") or "").startswith("manual"):
        return value
    try:
        target = urlsplit(value)
        default = urlsplit(str(template.get("base_url") or ""))
    except ValueError:
        return value
    if target.path in {"", "/"} and default.path not in {"", "/"}:
        target = target._replace(path=default.path)
        return urlunsplit(target)
    return value


def _token_expiry(value: Any) -> datetime | None:
    """Parse API expiry timestamps and persist them as naive UTC for SQLite."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise _error("validation_error", "expires_at 必须是 ISO 8601 时间", "token_validation") from exc
    else:
        raise _error("validation_error", "expires_at 必须是 ISO 8601 时间", "token_validation")
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _token_scopes(value: Any) -> list[str]:
    if not isinstance(value, list) or not value or any(not isinstance(scope, str) or not scope.strip() for scope in value):
        raise _error("validation_error", "scopes 必须是非空字符串数组", "token_validation")
    return list(dict.fromkeys(scope.strip() for scope in value))


def _valid_capabilities(value: Any, *, field: str, allowed: tuple[str, ...] = ALL_CAPABILITIES) -> list[str]:
    try:
        return normalize_capabilities(value, allowed=allowed, field=field)
    except ValueError as exc:
        raise _error("validation_error", str(exc), "capability_validation") from exc


def _valid_capability_map(value: Any, *, field: str) -> dict[str, list[str]]:
    try:
        return normalize_capability_map(value, field=field)
    except ValueError as exc:
        raise _error("validation_error", str(exc), "capability_validation") from exc


def _provider_out(row: ProviderInstance) -> dict[str, Any]:
    return {"id": row.id, "name": row.name, "template_key": row.template_key, "protocol_type": row.protocol_type, "base_url": row.base_url, "credential_configured": bool(row.api_key_encrypted), "oauth_configured": bool(row.oauth_encrypted_json), "custom_headers_configured": bool(row.custom_headers_encrypted_json), "proxy_type":row.proxy_type,"proxy_configured":bool(row.proxy_url_encrypted),"timeout_seconds": row.timeout_seconds, "enabled": row.enabled, "verification_status": row.verification_status, "last_tested_at": row.last_tested_at, "last_test_error": row.last_test_error}


def _model_out(row: UpstreamModel,db:Session|None=None) -> dict[str, Any]:
    result={key: getattr(row, key) for key in ("id", "provider_instance_id", "model_id", "display_name", "input_capabilities_json", "output_capabilities_json", "context_window", "max_output_tokens", "input_price", "output_price", "cached_input_price", "currency", "pricing_source", "pricing_effective_at", "tags_json", "enabled", "remote_status", "last_synced_at")}
    if db is not None:
        result["reference_count"]=(db.scalar(select(func.count()).select_from(UnifiedModelCandidate).where(UnifiedModelCandidate.upstream_model_id==row.id)) or 0)+(db.scalar(select(func.count()).select_from(AuxiliaryModel).where(AuxiliaryModel.upstream_model_id==row.id)) or 0)
    return result


@router.get("/runtime")
def runtime(db: Session = Depends(get_db)) -> dict[str, Any]:
    from apiswitch import __version__
    import platform
    from pathlib import Path
    if settings.plugin_mode:
        data=harness_token_path().parent
        info={
            "base_url":f"http://{settings.listen_host}:{settings.port}",
            "port":settings.port,
            "data_directory":str(data),
            "desktop":False,
            "plugin_mode":True,
        }
    else:
        from apiswitch.desktop import runtime_info
        info=runtime_info();data=Path(str(info.get("data_directory")))
    generation = db.scalar(select(SchemaMetadata.generation).order_by(SchemaMetadata.generation.desc()))
    database_path=Path(settings.database_url.removeprefix("sqlite:///")) if settings.database_url.startswith("sqlite:///") else None
    return {**info, "version":__version__,"schema_generation": generation, "single_instance": True,"listen_host":settings.listen_host,"master_key_status":"configured" if settings.master_key or (data/"master.key").is_file() else "environment_or_not_initialized","database_status":"ready" if database_path and database_path.is_file() else "development","platform":platform.platform(),"python_runtime":"bundled" if getattr(__import__("sys"),"frozen",False) else "development"}


@router.get("/diagnostics")
def diagnostics(db:Session=Depends(get_db))->dict[str,Any]:
    return {"runtime":runtime(db),"counts":{"provider_instances":db.scalar(select(func.count(ProviderInstance.id))) or 0,"upstream_models":db.scalar(select(func.count(UpstreamModel.id))) or 0,"unified_models":db.scalar(select(func.count(UnifiedModel.id))) or 0,"request_logs":db.scalar(select(func.count(RequestLog.id))) or 0},"settings_keys":sorted(db.scalars(select(SystemSetting.key)).all()),"recent_error_types":list(db.scalars(select(RequestLog.error_type).where(RequestLog.success.is_(False),RequestLog.error_type.is_not(None)).order_by(RequestLog.id.desc()).limit(20)).all()),"excluded":["master.key","database","credentials","request_content"]}


@router.get("/dashboard/summary")
def dashboard_summary(db: Session = Depends(get_db)) -> dict[str, Any]:
    requests_total = db.scalar(select(func.count(RequestLog.id))) or 0
    success_total = db.scalar(
        select(func.count(RequestLog.id)).where(RequestLog.success.is_(True))
    ) or 0
    average_latency_ms = db.scalar(select(func.avg(RequestLog.latency_ms))) or 0
    average_first_token_latency_ms = db.scalar(select(func.avg(RequestLog.first_token_latency_ms))) or 0
    open_circuit_breakers = db.scalar(
        select(func.count(CircuitBreaker.id)).where(CircuitBreaker.state == "open")
    ) or 0
    active_budgets=list(db.scalars(select(Budget).where(Budget.enabled.is_(True))).all())
    for budget in active_budgets:refresh_budget_period(budget)
    monthly_cost_budgets=[budget for budget in active_budgets if budget.billing_mode=="token_cost" and budget.period_type=="calendar_month"]
    monthly_budget_used=sum(float(budget.spent_amount or 0) for budget in monthly_cost_budgets)
    monthly_budget_limit=sum(float(budget.monthly_limit or 0) for budget in monthly_cost_budgets)
    recent_errors = db.scalars(
        select(RequestLog.error_message)
        .where(RequestLog.success.is_(False), RequestLog.error_message.is_not(None))
        .order_by(RequestLog.started_at.desc())
        .limit(5)
    ).all()
    since=utc_now()-timedelta(hours=24)
    provider_instances=db.scalar(select(func.count(ProviderInstance.id))) or 0
    available_upstream_models=db.scalar(select(func.count(UpstreamModel.id)).join(ProviderInstance,UpstreamModel.provider_instance_id==ProviderInstance.id).where(UpstreamModel.enabled.is_(True),ProviderInstance.enabled.is_(True),UpstreamModel.remote_status!="missing")) or 0
    unified_models=db.scalar(select(func.count(UnifiedModel.id)).where(UnifiedModel.enabled.is_(True))) or 0
    auxiliary_models=db.scalar(select(func.count(AuxiliaryModel.id)).where(AuxiliaryModel.enabled.is_(True))) or 0
    requests_24h=db.scalar(select(func.count(RequestLog.id)).where(RequestLog.started_at>=since)) or 0
    cost_24h=db.scalar(select(func.coalesce(func.sum(RequestLog.estimated_cost),0)).where(RequestLog.started_at>=since)) or 0
    budget_alerts=sum(1 for row in active_budgets if budget_limit_value(row) and float(budget_usage_value(row))/float(budget_limit_value(row) or 1)*100>=row.alert_threshold_percent)
    db.commit()
    failure_total = max(requests_total - success_total, 0)
    return {
        "requests_total": requests_total,
        "success_rate": success_total / requests_total if requests_total else 1.0,
        "failure_rate": failure_total / requests_total if requests_total else 0.0,
        "average_latency_ms": round(float(average_latency_ms), 2),
        "first_token_latency_ms": round(float(average_first_token_latency_ms), 2),
        "open_circuit_breakers": open_circuit_breakers,
        "monthly_budget_used": float(monthly_budget_used),
        "monthly_budget_limit": float(monthly_budget_limit),
        "recent_errors": recent_errors,
        "provider_instances":provider_instances,
        "available_upstream_models":available_upstream_models,
        "unified_models":unified_models,
        "auxiliary_models":auxiliary_models,
        "requests_24h":requests_24h,
        "cost_24h":float(cost_24h),
        "budget_alerts":budget_alerts,
    }


@router.get("/provider-templates")
def provider_templates() -> list[dict[str, Any]]: return list_templates()


@router.get("/provider-instances")
def provider_instances(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return [_provider_out(row) for row in db.scalars(select(ProviderInstance).order_by(ProviderInstance.id)).all()]


@router.post("/provider-instances", status_code=201)
def create_provider_instance(payload: dict[str, Any] = Body(...), db: Session = Depends(get_db)) -> dict[str, Any]:
    template_key = str(payload.get("template_key", "manual")); template = get_template(template_key)
    if template is None: raise _error("validation_error", "未知供应商模板")
    protocol = payload.get("protocol_type", template["protocol_type"])
    if template_key.startswith("manual") and protocol not in {"openai_compatible", "anthropic_messages", "gemini", "custom"}: raise _error("validation_error", "手动供应商必须选择受支持协议")
    if not template_key.startswith("manual") and protocol != template["protocol_type"]:
        raise _error("validation_error", "目录供应商必须使用模板定义的协议", "provider_validation")
    custom_headers = payload.get("custom_headers") or {}
    if not isinstance(custom_headers, dict):
        raise _error("validation_error", "自定义请求头必须是对象")
    headers = {**(template.get("default_headers") or {}), **custom_headers}
    forbidden_headers={"host","content-length","transfer-encoding","connection"}
    if not isinstance(headers, dict) or any("\r" in str(k)+str(v) or "\n" in str(k)+str(v) for k, v in headers.items()): raise _error("validation_error", "自定义请求头包含非法换行")
    if any(str(key).lower() in forbidden_headers for key in headers):raise _error("validation_error","自定义请求头包含受保护字段")
    name = str(payload.get("name", "")).strip()
    base_url=_provider_base_url(template,str(payload.get("base_url",template["base_url"])))
    if not name or not base_url: raise _error("validation_error", "实例名称和 Base URL 必填")
    if not base_url.startswith("mock://"):
        try:base_url=validate_outbound_url(base_url,"供应商 Base URL")
        except OutboundURLRejected as exc:raise _error("validation_error",str(exc),"provider_validation") from exc
    try:
        oauth=payload.get("oauth") or {};proxy_url=payload.get("proxy_url")
        if not isinstance(oauth,dict):raise _error("validation_error","OAuth 附加凭据必须是对象")
        row = ProviderInstance(name=name, template_key=template_key, protocol_type=protocol, base_url=base_url.rstrip("/"), api_key_encrypted=secret_crypto.encrypt(str(payload["api_key"])) if payload.get("api_key") else None, oauth_encrypted_json={k:secret_crypto.encrypt(str(v)) for k,v in oauth.items()} or None,custom_headers_encrypted_json={k: secret_crypto.encrypt(str(v)) for k, v in headers.items()} or None, timeout_seconds=int(payload.get("timeout_seconds", 120)),proxy_type=payload.get("proxy_type"),proxy_url_encrypted=secret_crypto.encrypt(str(proxy_url)) if proxy_url else None, enabled=bool(payload.get("enabled", True)), verification_status=template["verification_status"])
    except SecretCryptoError as exc: raise _error("validation_error", str(exc), "secret_storage") from exc
    db.add(row); db.commit(); db.refresh(row); return _provider_out(row)


@router.get("/provider-instances/{instance_id}")
def get_provider_instance(instance_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    row = db.get(ProviderInstance, instance_id)
    if not row: raise HTTPException(404, "供应商实例不存在")
    return _provider_out(row)


@router.patch("/provider-instances/{instance_id}")
def update_provider_instance(instance_id: int, payload: dict[str, Any] = Body(...), db: Session = Depends(get_db)) -> dict[str, Any]:
    row = db.get(ProviderInstance, instance_id)
    if not row: raise HTTPException(404, "供应商实例不存在")
    if "template_key" in payload and payload["template_key"] != row.template_key:
        raise _error("validation_error", "供应商实例不能直接更换模板；请从模板目录新建独立实例", "provider_validation")
    if "base_url" in payload and not str(payload["base_url"]).startswith("mock://"):
        template=get_template(row.template_key) or {"key":"manual","base_url":""}
        try:payload["base_url"]=validate_outbound_url(_provider_base_url(template,str(payload["base_url"])),"供应商 Base URL")
        except OutboundURLRejected as exc:raise _error("validation_error",str(exc),"provider_validation") from exc
    if "protocol_type" in payload and payload["protocol_type"] not in {"openai","openai_compatible","anthropic_messages","gemini","custom"}:raise _error("validation_error","不支持的供应商协议")
    if "protocol_type" in payload and not row.template_key.startswith("manual") and payload["protocol_type"] != (get_template(row.template_key) or {}).get("protocol_type"):
        raise _error("validation_error", "目录供应商必须使用模板定义的协议", "provider_validation")
    for field in ("name", "base_url", "protocol_type", "timeout_seconds", "proxy_type", "enabled"):
        if field in payload: setattr(row, field, payload[field])
    if "api_key" in payload: row.api_key_encrypted = secret_crypto.encrypt(str(payload["api_key"])) if payload["api_key"] else None
    if "custom_headers" in payload:
        headers = payload["custom_headers"] or {}
        if any("\r" in str(k)+str(v) or "\n" in str(k)+str(v) for k,v in headers.items()): raise _error("validation_error", "自定义请求头包含非法换行")
        if any(str(key).lower() in {"host","content-length","transfer-encoding","connection"} for key in headers):raise _error("validation_error","自定义请求头包含受保护字段")
        row.custom_headers_encrypted_json = {k: secret_crypto.encrypt(str(v)) for k,v in headers.items()} or None
    if "oauth" in payload:
        oauth=payload["oauth"] or {}
        if not isinstance(oauth,dict):raise _error("validation_error","OAuth 附加凭据必须是对象")
        row.oauth_encrypted_json={k:secret_crypto.encrypt(str(v)) for k,v in oauth.items()} or None
    if "proxy_url" in payload:row.proxy_url_encrypted=secret_crypto.encrypt(str(payload["proxy_url"])) if payload["proxy_url"] else None
    db.commit(); return _provider_out(row)


@router.delete("/provider-instances/{instance_id}")
def delete_provider_instance(instance_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    row = db.get(ProviderInstance, instance_id)
    if not row: raise HTTPException(404, "供应商实例不存在")
    model_ids=list(db.scalars(select(UpstreamModel.id).where(UpstreamModel.provider_instance_id==instance_id)).all())
    unified_refs=db.scalar(select(func.count()).select_from(UnifiedModelCandidate).where(UnifiedModelCandidate.upstream_model_id.in_(model_ids))) if model_ids else 0
    auxiliary_refs=db.scalar(select(func.count()).select_from(AuxiliaryModel).where(AuxiliaryModel.upstream_model_id.in_(model_ids))) if model_ids else 0
    if model_ids: raise _error("resource_in_use", "供应商实例仍包含上游模型，请先删除上游模型", details={"references":{"upstream_models":len(model_ids),"unified_candidates":unified_refs or 0,"auxiliary_models":auxiliary_refs or 0}})
    db.delete(row); db.commit(); return {"deleted": True}


@router.post("/provider-instances/{instance_id}/duplicate", status_code=201)
def duplicate_provider_instance(instance_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    source = db.get(ProviderInstance, instance_id)
    if not source: raise HTTPException(404, "供应商实例不存在")
    base_name=f"{source.name} 副本";name=base_name;suffix=2
    while db.scalar(select(ProviderInstance.id).where(ProviderInstance.name==name)) is not None:name=f"{base_name} {suffix}";suffix+=1
    copy = ProviderInstance(name=name, template_key=source.template_key, protocol_type=source.protocol_type, base_url=source.base_url, api_key_encrypted=source.api_key_encrypted,oauth_encrypted_json=source.oauth_encrypted_json, custom_headers_encrypted_json=source.custom_headers_encrypted_json, timeout_seconds=source.timeout_seconds,proxy_type=source.proxy_type,proxy_url_encrypted=source.proxy_url_encrypted, enabled=source.enabled, verification_status=source.verification_status)
    db.add(copy); db.commit(); db.refresh(copy); return _provider_out(copy)


@router.post("/provider-instances/{instance_id}/test")
async def test_provider_instance(instance_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    row = db.get(ProviderInstance, instance_id)
    if not row: raise HTTPException(404, "供应商实例不存在")
    from apiswitch.protocols.canonical import ProtocolError
    from apiswitch.routing.executor import discover_models
    try:models=await discover_models(row)
    except ProtocolError as exc:
        row.last_tested_at=utc_now();row.last_test_error=str(exc);db.commit();raise _error(exc.error_type,str(exc),exc.stage,exc.details) from exc
    row.last_tested_at=utc_now();row.last_test_error=None
    if not row.base_url.startswith("mock://"):row.verification_status="connection_verified"
    db.commit();return {"ok":True,"mode":"mock" if row.base_url.startswith("mock://") else "remote","model_count":len(models),"message":"供应商连接成功"}


@router.post("/provider-instances/{instance_id}/upstream-models/discover")
async def discover_provider_models(instance_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Preview a remote model catalog without persisting every returned model."""
    row = db.get(ProviderInstance, instance_id)
    if not row: raise HTTPException(404, "供应商实例不存在")
    from apiswitch.protocols.canonical import ProtocolError
    from apiswitch.routing.executor import discover_models
    try:models=await discover_models(row)
    except ProtocolError as exc:raise _error(exc.error_type,str(exc),exc.stage,exc.details) from exc
    return {"models":models,"count":len(models)}


@router.get("/provider-instances/{instance_id}/upstream-models")
def list_upstream_models(instance_id: int, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return [_model_out(row,db) for row in db.scalars(select(UpstreamModel).where(UpstreamModel.provider_instance_id == instance_id).order_by(UpstreamModel.id)).all()]


@router.get("/upstream-models")
def list_all_upstream_models(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows=db.execute(
        select(UpstreamModel,ProviderInstance.name)
        .join(ProviderInstance,UpstreamModel.provider_instance_id==ProviderInstance.id)
        .order_by(ProviderInstance.name,UpstreamModel.model_id,UpstreamModel.id)
    ).all()
    return [{**_model_out(model,db),"provider_name":provider_name} for model,provider_name in rows]


def _write_model(db: Session, instance_id: int, item: dict[str, Any], existing: UpstreamModel | None = None) -> UpstreamModel:
    model_id = str(item.get("model_id") or item.get("id") or "").strip()
    if not model_id: raise _error("validation_error", "上游模型 ID 必填")
    row = existing or UpstreamModel(provider_instance_id=instance_id, model_id=model_id, display_name=model_id)
    inferred = infer_model_characteristics(model_id, item.get("remote_metadata") or item)
    for capability_field in ("input_capabilities_json", "output_capabilities_json", "context_window", "max_output_tokens"):
        value = inferred.get(capability_field)
        if capability_field not in item and (existing is None or getattr(row, capability_field) is None):
            setattr(row, capability_field, value)
    for key in ("display_name", "input_capabilities_json", "output_capabilities_json", "context_window", "max_output_tokens", "input_price", "output_price", "cached_input_price", "currency", "pricing_source", "pricing_effective_at", "tags_json", "enabled"):
        if key in item:
            value=item[key]
            if key in {"input_capabilities_json","output_capabilities_json"}:value=_valid_capabilities(value,field=key)
            setattr(row, key, value)
    row.display_name = str(item.get("display_name") or row.display_name or model_id); row.remote_status = item.get("remote_status", "available"); row.remote_metadata_json = item.get("remote_metadata", row.remote_metadata_json); row.last_synced_at = utc_now()
    if existing is None: db.add(row)
    return row


@router.post("/upstream-models/infer-capabilities")
def infer_upstream_model_capabilities(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    model_id = str(payload.get("model_id") or "").strip()
    if not model_id: raise _error("validation_error", "上游模型 ID 必填")
    return infer_model_characteristics(model_id, payload.get("metadata"))


@router.post("/upstream-models/{model_id}/probe")
async def probe_upstream_model(model_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    row = db.get(UpstreamModel, model_id)
    if not row:
        raise HTTPException(404, "上游模型不存在")
    provider = db.get(ProviderInstance, row.provider_instance_id)
    if not provider:
        raise _error("provider_unavailable", "上游模型所属供应商不存在", "model_probe")
    from apiswitch.protocols.canonical import ProtocolError
    from apiswitch.routing.executor import probe_model
    try:
        result = await probe_model(provider, row)
    except ProtocolError as exc:
        row.remote_status = "probe_failed"
        row.last_synced_at = utc_now()
        db.commit()
        raise _error(exc.error_type, str(exc), "model_probe", exc.details) from exc
    row.remote_status = "available"
    row.last_synced_at = utc_now()
    db.commit()
    return {**result, "model": _model_out(row, db)}


@router.post("/provider-instances/{instance_id}/upstream-models", status_code=201)
def add_upstream_model(instance_id: int, payload: dict[str, Any] = Body(...), db: Session = Depends(get_db)) -> dict[str, Any]:
    if not db.get(ProviderInstance, instance_id): raise HTTPException(404, "供应商实例不存在")
    existing = db.scalar(select(UpstreamModel).where(UpstreamModel.provider_instance_id == instance_id, UpstreamModel.model_id == payload.get("model_id")))
    if existing: raise _error("validation_error", "同一供应商实例内模型 ID 已存在")
    row = _write_model(db, instance_id, payload); db.commit(); db.refresh(row); return _model_out(row)


@router.post("/provider-instances/{instance_id}/upstream-models/sync")
async def sync_upstream_models(instance_id: int, payload: dict[str, Any] = Body(default={}), db: Session = Depends(get_db)) -> dict[str, Any]:
    provider=db.get(ProviderInstance, instance_id)
    if not provider: raise HTTPException(404, "供应商实例不存在")
    if "models" in payload:remote=payload["models"]
    else:
        from apiswitch.protocols.canonical import ProtocolError
        from apiswitch.routing.executor import discover_models
        try:remote=await discover_models(provider)
        except ProtocolError as exc:raise _error(exc.error_type,str(exc),exc.stage,exc.details) from exc
    if not isinstance(remote, list): raise _error("validation_error", "models 必须为数组")
    current = {m.model_id: m for m in db.scalars(select(UpstreamModel).where(UpstreamModel.provider_instance_id == instance_id)).all()}
    remote_ids = [str(item.get("model_id") or item.get("id") or "").strip() for item in remote if isinstance(item, dict)]
    basename_counts: dict[str, int] = {}
    for remote_id in remote_ids:
        basename = remote_id.rsplit("/", 1)[-1]
        basename_counts[basename] = basename_counts.get(basename, 0) + 1
    repaired_legacy_ids: set[str] = set()
    seen=set(); added=updated=unchanged=missing=0
    for item in remote:
        if not isinstance(item, dict): raise _error("validation_error", "models 数组成员必须是对象")
        model_id = str(item.get("model_id") or item.get("id") or "").strip()
        if not model_id: raise _error("validation_error", "远端模型 ID 不能为空")
        seen.add(model_id)
        row=current.get(model_id)
        repaired_legacy_id=False
        if row is None and "/" in model_id:
            # Generation-two builds before this fix kept only the basename of
            # namespaced OpenAI-compatible IDs.  Repair an unambiguous legacy
            # row in place so unified/auxiliary model references remain valid.
            legacy_id=model_id.rsplit("/",1)[-1]
            legacy=current.get(legacy_id)
            if legacy is not None and basename_counts.get(legacy_id)==1:
                row=legacy
                repaired_legacy_ids.add(legacy_id)
                row.model_id=model_id
                current[model_id]=row
                repaired_legacy_id=True
        if row is not None:
            before={key:getattr(row,key) for key in ("model_id","display_name","input_capabilities_json","output_capabilities_json","context_window","max_output_tokens","input_price","output_price","cached_input_price","currency","pricing_source","pricing_effective_at","tags_json","enabled","remote_status","remote_metadata_json")}
            _write_model(db, instance_id, item, row)
            after={key:getattr(row,key) for key in before}
            if before==after and not repaired_legacy_id:unchanged+=1
            else:updated+=1
        else: _write_model(db, instance_id, item); added += 1
    for model_id, row in current.items():
        if model_id in repaired_legacy_ids: continue
        if model_id not in seen:
            referenced = db.scalar(select(func.count()).select_from(UnifiedModelCandidate).where(UnifiedModelCandidate.upstream_model_id == row.id)) or db.scalar(select(func.count()).select_from(AuxiliaryModel).where(AuxiliaryModel.upstream_model_id == row.id))
            if referenced: row.remote_status="missing"; missing += 1
            else: unchanged += 1
    db.commit(); return {"added": added, "updated": updated, "marked_missing": missing, "unchanged": unchanged, "errors": []}


@router.patch("/upstream-models/{model_id}")
def update_upstream_model(model_id: int, payload: dict[str, Any] = Body(...), db: Session = Depends(get_db)) -> dict[str, Any]:
    row=db.get(UpstreamModel, model_id)
    if not row: raise HTTPException(404, "上游模型不存在")
    for key, value in payload.items():
        if key in {"input_capabilities_json","output_capabilities_json"}:value=_valid_capabilities(value,field=key)
        if hasattr(row,key) and key not in {"id","provider_instance_id"}: setattr(row,key,value)
    db.commit(); return _model_out(row)


@router.delete("/upstream-models/{model_id}")
def delete_upstream_model(model_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    row=db.get(UpstreamModel, model_id)
    if not row: raise HTTPException(404, "上游模型不存在")
    references = {"unified_candidates": db.scalar(select(func.count()).select_from(UnifiedModelCandidate).where(UnifiedModelCandidate.upstream_model_id == model_id)) or 0, "auxiliary_models": db.scalar(select(func.count()).select_from(AuxiliaryModel).where(AuxiliaryModel.upstream_model_id == model_id)) or 0}
    if any(references.values()): raise _error("resource_in_use", "上游模型正被引用", details={"references": references})
    db.delete(row); db.commit(); return {"deleted": True}


@router.post("/upstream-models/bulk")
def bulk_upstream_models(payload: dict[str, Any] = Body(...), db: Session = Depends(get_db)) -> dict[str, Any]:
    ids=payload.get("ids",[]); action=payload.get("action")
    if not isinstance(ids,list) or not ids or any(not isinstance(model_id,int) for model_id in ids) or len(ids)!=len(set(ids)):
        raise _error("validation_error", "ids 必须是非空且不重复的整数数组")
    rows=list(db.scalars(select(UpstreamModel).where(UpstreamModel.id.in_(ids))).all())
    found_ids={row.id for row in rows}
    missing_ids=sorted(set(ids)-found_ids)
    if missing_ids:raise _error("validation_error","部分上游模型不存在",details={"missing_ids":missing_ids})
    if action not in {"enable","disable","delete","configure"}: raise _error("validation_error", "不支持的批量操作")
    if action=="delete":
        blocked=[]
        for row in rows:
            refs=(db.scalar(select(func.count()).select_from(UnifiedModelCandidate).where(UnifiedModelCandidate.upstream_model_id==row.id)) or 0)+(db.scalar(select(func.count()).select_from(AuxiliaryModel).where(AuxiliaryModel.upstream_model_id==row.id)) or 0)
            if refs:blocked.append(row.id)
        if blocked:raise _error("resource_in_use","部分上游模型正被引用",details={"references":blocked})
        for row in rows:db.delete(row)
    elif action in {"enable","disable"}:
        for row in rows: row.enabled=action=="enable"
    else:
        configuration=payload.get("configuration")
        if not isinstance(configuration,dict) or not configuration:
            raise _error("validation_error","configuration 必须是非空对象")
        allowed={"input_capabilities_json","output_capabilities_json","context_window","max_output_tokens","input_price","output_price","cached_input_price","tags_json"}
        unknown=sorted(set(configuration)-allowed)
        if unknown:raise _error("validation_error","批量配置包含不支持的字段",details={"fields":unknown})
        normalized=dict(configuration)
        for key in ("input_capabilities_json","output_capabilities_json"):
            if key in normalized:normalized[key]=_valid_capabilities(normalized[key],field=key)
        for key in ("context_window","max_output_tokens"):
            value=normalized.get(key)
            if key in normalized and value is not None and (isinstance(value,bool) or not isinstance(value,int) or value<1):
                raise _error("validation_error",f"{key} 必须是正整数或 null")
        for key in ("input_price","output_price","cached_input_price"):
            value=normalized.get(key)
            if key in normalized and value is not None and (isinstance(value,bool) or not isinstance(value,(int,float)) or value<0):
                raise _error("validation_error",f"{key} 必须是非负数或 null")
        if "tags_json" in normalized and (not isinstance(normalized["tags_json"],list) or any(not isinstance(tag,str) or not tag.strip() for tag in normalized["tags_json"])):
            raise _error("validation_error","tags_json 必须是字符串数组")
        for row in rows:
            for key,value in normalized.items():setattr(row,key,value)
            if any(key in normalized for key in ("input_price","output_price","cached_input_price")):row.pricing_source="manual"
    db.commit(); return {"updated":len(rows),"action":action}


def _candidate_out(db: Session, row: UnifiedModelCandidate) -> dict[str, Any]:
    upstream=db.get(UpstreamModel,row.upstream_model_id); provider=db.get(ProviderInstance,upstream.provider_instance_id) if upstream else None
    return {"id":row.id,"upstream_model_id":row.upstream_model_id,"priority":row.priority,"weight":row.weight,"capability_overrides":row.capability_overrides_json or {},"enabled":row.enabled,"upstream_model":_model_out(upstream) if upstream else None,"provider_instance":_provider_out(provider) if provider else None}


def _apply_priority_order(rows: list[Any], ordered_ids: Any, resource_name: str) -> None:
    if not isinstance(ordered_ids, list) or any(not isinstance(row_id, int) for row_id in ordered_ids):
        raise _error("validation_error", f"{resource_name}排序必须是 ID 数组")
    rows_by_id = {row.id: row for row in rows}
    if len(ordered_ids) != len(rows_by_id) or set(ordered_ids) != set(rows_by_id):
        raise _error("validation_error", f"{resource_name}排序必须包含当前全部项目且不能重复")
    for priority, row_id in enumerate(ordered_ids, start=1):
        rows_by_id[row_id].priority = priority


def _unified_out(db: Session, row: UnifiedModel) -> dict[str, Any]:
    return {"id":row.id,"name":row.name,"description":row.description,"required_capabilities":row.required_capabilities_json or {},"enabled_protocols":row.enabled_protocols_json or [],"routing_mode":row.routing_mode,"combo_strategy":row.combo_strategy,"preferred_tier":row.preferred_tier,"session_affinity_enabled":row.session_affinity_enabled,"max_cost_per_request":row.max_cost_per_request,"max_latency_ms":row.max_latency_ms,"min_context_window":row.min_context_window,"enabled":row.enabled,"candidates":[_candidate_out(db,c) for c in db.scalars(select(UnifiedModelCandidate).where(UnifiedModelCandidate.unified_model_id==row.id).order_by(UnifiedModelCandidate.priority)).all()]}


@router.get("/unified-models")
def list_unified_models(db: Session = Depends(get_db)) -> list[dict[str, Any]]: return [_unified_out(db,row) for row in db.scalars(select(UnifiedModel).order_by(UnifiedModel.id)).all()]


@router.post("/unified-models",status_code=201)
def create_unified_model(payload: dict[str,Any]=Body(...),db:Session=Depends(get_db))->dict[str,Any]:
    name=str(payload.get("name","")).strip()
    if not name: raise _error("validation_error","统一模型名称必填")
    required_capabilities=_valid_capability_map(payload.get("required_capabilities",{}),field="required_capabilities")
    row=UnifiedModel(name=name,description=payload.get("description"),required_capabilities_json=required_capabilities,enabled_protocols_json=payload.get("enabled_protocols",["openai_chat","openai_responses","anthropic_messages","gemini_v1beta"]),routing_mode=payload.get("routing_mode","static"),combo_strategy=payload.get("combo_strategy","priority"),preferred_tier=payload.get("preferred_tier","balanced"),session_affinity_enabled=payload.get("session_affinity_enabled",True),max_cost_per_request=payload.get("max_cost_per_request"),max_latency_ms=payload.get("max_latency_ms"),min_context_window=payload.get("min_context_window"),enabled=payload.get("enabled",True))
    db.add(row); db.commit(); db.refresh(row); return _unified_out(db,row)


@router.get("/unified-models/{unified_id}")
def get_unified_model(unified_id:int,db:Session=Depends(get_db))->dict[str,Any]:
    row=db.get(UnifiedModel,unified_id)
    if not row: raise HTTPException(404,"统一模型不存在")
    return _unified_out(db,row)


@router.patch("/unified-models/{unified_id}")
def update_unified_model(unified_id:int,payload:dict[str,Any]=Body(...),db:Session=Depends(get_db))->dict[str,Any]:
    row=db.get(UnifiedModel,unified_id)
    if not row: raise HTTPException(404,"统一模型不存在")
    mapping={"required_capabilities":"required_capabilities_json","enabled_protocols":"enabled_protocols_json"}
    for key,value in payload.items():
        if key=="required_capabilities":value=_valid_capability_map(value,field="required_capabilities")
        attr=mapping.get(key,key)
        if hasattr(row,attr) and attr not in {"id","candidates"}: setattr(row,attr,value)
    db.commit();return _unified_out(db,row)


@router.delete("/unified-models/{unified_id}")
def delete_unified_model(unified_id:int,db:Session=Depends(get_db))->dict[str,bool]:
    row=db.get(UnifiedModel,unified_id)
    if not row:raise HTTPException(404,"统一模型不存在")
    references={
        "auxiliary_models":db.scalar(select(func.count()).select_from(AuxiliaryModel).where(AuxiliaryModel.unified_model_id==unified_id)) or 0,
        "auxiliary_workflows":db.scalar(select(func.count()).select_from(AuxiliaryWorkflow).where(AuxiliaryWorkflow.unified_model_id==unified_id)) or 0,
        "agent_configs":db.scalar(select(func.count()).select_from(AgentConfig).where(
            (AgentConfig.main_model_id==unified_id)|(AgentConfig.opus_model_id==unified_id)|
            (AgentConfig.sonnet_model_id==unified_id)|(AgentConfig.haiku_model_id==unified_id)
        )) or 0,
        "api_tokens":db.scalar(select(func.count()).select_from(ApiTokenUnifiedModel).where(ApiTokenUnifiedModel.unified_model_id==unified_id)) or 0,
    }
    if any(references.values()):
        raise _error("resource_in_use","统一模型仍被辅助配置、Agent 配置或 API Token 引用","unified_model_delete",references)
    db.delete(row);db.commit();return {"deleted":True}


@router.post("/unified-models/{unified_id}/candidates",status_code=201)
def add_candidate(unified_id:int,payload:dict[str,Any]=Body(...),db:Session=Depends(get_db))->dict[str,Any]:
    if not db.get(UnifiedModel,unified_id):raise HTTPException(404,"统一模型不存在")
    upstream_id=payload.get("upstream_model_id")
    if not db.get(UpstreamModel,upstream_id):raise _error("validation_error","上游模型不存在")
    overrides=_valid_capability_map(payload.get("capability_overrides",{}),field="capability_overrides")
    priority=payload.get("priority")
    if priority is None:priority=(db.scalar(select(func.max(UnifiedModelCandidate.priority)).where(UnifiedModelCandidate.unified_model_id==unified_id)) or 0)+1
    row=UnifiedModelCandidate(unified_model_id=unified_id,upstream_model_id=upstream_id,priority=priority,weight=payload.get("weight",100),capability_overrides_json=overrides,enabled=payload.get("enabled",True));db.add(row);db.commit();db.refresh(row);return _candidate_out(db,row)


@router.post("/unified-models/{unified_id}/candidates/bulk",status_code=201)
def add_candidates_bulk(unified_id:int,payload:dict[str,Any]=Body(...),db:Session=Depends(get_db))->list[dict[str,Any]]:
    if not db.get(UnifiedModel,unified_id):raise HTTPException(404,"统一模型不存在")
    upstream_ids=payload.get("upstream_model_ids")
    if not isinstance(upstream_ids,list) or not upstream_ids or any(not isinstance(model_id,int) for model_id in upstream_ids) or len(upstream_ids)!=len(set(upstream_ids)):
        raise _error("validation_error","upstream_model_ids 必须是非空且不重复的整数数组")
    found_ids=set(db.scalars(select(UpstreamModel.id).where(UpstreamModel.id.in_(upstream_ids))).all())
    missing_ids=sorted(set(upstream_ids)-found_ids)
    if missing_ids:raise _error("validation_error","部分上游模型不存在",details={"missing_ids":missing_ids})
    existing_ids=set(db.scalars(select(UnifiedModelCandidate.upstream_model_id).where(UnifiedModelCandidate.unified_model_id==unified_id,UnifiedModelCandidate.upstream_model_id.in_(upstream_ids))).all())
    if existing_ids:raise _error("validation_error","部分上游模型已是当前统一模型的候选",details={"existing_upstream_model_ids":sorted(existing_ids)})
    overrides=_valid_capability_map(payload.get("capability_overrides",{}),field="capability_overrides")
    first_priority=(db.scalar(select(func.max(UnifiedModelCandidate.priority)).where(UnifiedModelCandidate.unified_model_id==unified_id)) or 0)+1
    rows=[]
    for offset,upstream_id in enumerate(upstream_ids):
        row=UnifiedModelCandidate(unified_model_id=unified_id,upstream_model_id=upstream_id,priority=first_priority+offset,weight=payload.get("weight",100),capability_overrides_json=overrides,enabled=payload.get("enabled",True))
        db.add(row);rows.append(row)
    db.flush()
    result=[_candidate_out(db,row) for row in rows]
    db.commit()
    return result


@router.patch("/unified-models/{unified_id}/candidates/reorder")
def reorder_candidates(unified_id:int,payload:dict[str,Any]=Body(...),db:Session=Depends(get_db))->list[dict[str,Any]]:
    if not db.get(UnifiedModel,unified_id):raise HTTPException(404,"统一模型不存在")
    rows=list(db.scalars(select(UnifiedModelCandidate).where(UnifiedModelCandidate.unified_model_id==unified_id)).all())
    _apply_priority_order(rows,payload.get("ids"),"候选模型")
    db.commit()
    return [_candidate_out(db,row) for row in sorted(rows,key=lambda item:(item.priority,item.id))]


@router.patch("/unified-models/{unified_id}/candidates/{candidate_id}")
def update_candidate(unified_id:int,candidate_id:int,payload:dict[str,Any]=Body(...),db:Session=Depends(get_db))->dict[str,Any]:
    row=db.get(UnifiedModelCandidate,candidate_id)
    if not row or row.unified_model_id!=unified_id:raise HTTPException(404,"候选不存在")
    for key,value in payload.items():
        if key=="capability_overrides":value=_valid_capability_map(value,field="capability_overrides")
        attr="capability_overrides_json" if key=="capability_overrides" else key
        if hasattr(row,attr):setattr(row,attr,value)
    db.commit();return _candidate_out(db,row)


@router.delete("/unified-models/{unified_id}/candidates/{candidate_id}")
def delete_candidate(unified_id:int,candidate_id:int,db:Session=Depends(get_db))->dict[str,bool]:
    row=db.get(UnifiedModelCandidate,candidate_id)
    if not row or row.unified_model_id!=unified_id:raise HTTPException(404,"候选不存在")
    db.delete(row);db.commit();return {"deleted":True}


@router.get("/unified-models/{unified_id}/protocol-matrix")
def get_protocol_matrix(unified_id:int,db:Session=Depends(get_db))->list[dict[str,Any]]:
    try:return protocol_matrix(db,unified_id)
    except Exception as exc:raise _error("validation_error",str(exc)) from exc


@router.get("/auxiliary/settings")
def auxiliary_settings(db:Session=Depends(get_db))->dict[str,Any]:
    row=db.get(AuxiliarySettings,1) or AuxiliarySettings(id=1,mode="global_pool");return {"mode":row.mode}


@router.patch("/auxiliary/settings")
def update_auxiliary_settings(payload:dict[str,Any]=Body(...),db:Session=Depends(get_db))->dict[str,Any]:
    mode=payload.get("mode")
    if mode not in {"disabled","per_unified_model","global_pool"}:raise _error("validation_error","无效辅助模型模式")
    row=db.get(AuxiliarySettings,1) or AuxiliarySettings(id=1);row.mode=mode;db.add(row);db.commit();return {"mode":row.mode}


@router.get("/auxiliary/models")
def list_auxiliary_models(db:Session=Depends(get_db))->list[dict[str,Any]]:
    return [_auxiliary_model_out(db, m) for m in db.scalars(select(AuxiliaryModel).order_by(AuxiliaryModel.priority)).all()]


def _auxiliary_model_out(db: Session, row: AuxiliaryModel) -> dict[str, Any]:
    upstream = db.get(UpstreamModel, row.upstream_model_id)
    provider = db.get(ProviderInstance, upstream.provider_instance_id) if upstream else None
    unified = db.get(UnifiedModel, row.unified_model_id) if row.unified_model_id else None
    return {
        "id": row.id, "upstream_model_id": row.upstream_model_id,
        "unified_model_id": row.unified_model_id, "capabilities": row.capabilities_json or [],
        "priority": row.priority, "enabled": row.enabled,
        "upstream_model": _model_out(upstream) if upstream else None,
        "provider_instance": _provider_out(provider) if provider else None,
        "unified_model": {"id": unified.id, "name": unified.name} if unified else None,
    }


@router.post("/auxiliary/models",status_code=201)
def add_auxiliary_model(payload:dict[str,Any]=Body(...),db:Session=Depends(get_db))->dict[str,Any]:
    if not db.get(UpstreamModel,payload.get("upstream_model_id")):raise _error("validation_error","上游模型不存在")
    capabilities=_valid_capabilities(payload.get("capabilities",[]),field="capabilities")
    priority=payload.get("priority")
    if priority is None:priority=(db.scalar(select(func.max(AuxiliaryModel.priority))) or 0)+1
    row=AuxiliaryModel(upstream_model_id=payload["upstream_model_id"],unified_model_id=payload.get("unified_model_id"),capabilities_json=capabilities,priority=priority,enabled=payload.get("enabled",True));db.add(row);db.commit();return _auxiliary_model_out(db,row)


@router.patch("/auxiliary/models/reorder")
def reorder_auxiliary_models(payload:dict[str,Any]=Body(...),db:Session=Depends(get_db))->list[dict[str,Any]]:
    rows=list(db.scalars(select(AuxiliaryModel)).all())
    _apply_priority_order(rows,payload.get("ids"),"辅助模型")
    db.commit()
    return [_auxiliary_model_out(db,row) for row in sorted(rows,key=lambda item:(item.priority,item.id))]


@router.patch("/auxiliary/models/{model_id}")
@router.delete("/auxiliary/models/{model_id}")
def mutate_auxiliary_model(model_id:int,payload:dict[str,Any]|None=Body(default=None),db:Session=Depends(get_db))->dict[str,Any]:
    row=db.get(AuxiliaryModel,model_id)
    if not row:raise HTTPException(404,"辅助模型不存在")
    # DELETE requests arrive with no body; FastAPI selects this shared handler.
    if payload is None:db.delete(row);db.commit();return {"deleted":True}
    for key,value in payload.items():
        if key=="capabilities":value=_valid_capabilities(value,field="capabilities")
        setattr(row,"capabilities_json" if key=="capabilities" else key,value)
    db.commit();return _auxiliary_model_out(db,row)


def _workflow_out(row: AuxiliaryWorkflow) -> dict[str, Any]:
    return {"id":row.id,"scope":row.scope,"unified_model_id":row.unified_model_id,"workflow_type":row.workflow_type,"input_capability":row.input_capability,"output_capability":row.output_capability,"ordered_steps":row.ordered_steps_json,"priority":row.priority,"enabled":row.enabled}


@router.get("/auxiliary/workflows")
def list_auxiliary_workflows(db:Session=Depends(get_db))->list[dict[str,Any]]: return [_workflow_out(row) for row in db.scalars(select(AuxiliaryWorkflow).order_by(AuxiliaryWorkflow.priority,AuxiliaryWorkflow.id)).all()]


@router.post("/auxiliary/workflows",status_code=201)
def add_auxiliary_workflow(payload:dict[str,Any]=Body(...),db:Session=Depends(get_db))->dict[str,Any]:
    workflow_type=payload.get("workflow_type")
    if workflow_type not in {"vision_to_text","file_extract","context_compress","tool_plan","audio_transcribe","structured_repair","terminal_capability"}:raise _error("validation_error","未知辅助工作流")
    input_capability=_valid_capabilities([payload.get("input_capability","text")],field="input_capability")[0]
    output_capability=_valid_capabilities([payload.get("output_capability","text")],field="output_capability")[0]
    try:steps=normalize_workflow_steps(payload.get("ordered_steps",[]))
    except ValueError as exc:raise _error("validation_error",str(exc),"capability_validation") from exc
    priority=payload.get("priority")
    if priority is None:priority=(db.scalar(select(func.max(AuxiliaryWorkflow.priority))) or 0)+1
    row=AuxiliaryWorkflow(scope=payload.get("scope","global"),unified_model_id=payload.get("unified_model_id"),workflow_type=workflow_type,input_capability=input_capability,output_capability=output_capability,ordered_steps_json=steps,priority=priority,enabled=payload.get("enabled",True));db.add(row);db.commit();return _workflow_out(row)


@router.patch("/auxiliary/workflows/reorder")
def reorder_auxiliary_workflows(payload:dict[str,Any]=Body(...),db:Session=Depends(get_db))->list[dict[str,Any]]:
    rows=list(db.scalars(select(AuxiliaryWorkflow)).all())
    _apply_priority_order(rows,payload.get("ids"),"辅助工作流")
    db.commit()
    return [_workflow_out(row) for row in sorted(rows,key=lambda item:(item.priority,item.id))]


@router.patch("/auxiliary/workflows/{workflow_id}")
def update_auxiliary_workflow(workflow_id:int,payload:dict[str,Any]=Body(...),db:Session=Depends(get_db))->dict[str,Any]:
    row=db.get(AuxiliaryWorkflow,workflow_id)
    if not row:raise HTTPException(404,"辅助工作流不存在")
    for key,value in payload.items():
        if key in {"input_capability","output_capability"}:value=_valid_capabilities([value],field=key)[0]
        if key=="ordered_steps":
            try:value=normalize_workflow_steps(value)
            except ValueError as exc:raise _error("validation_error",str(exc),"capability_validation") from exc
        setattr(row,"ordered_steps_json" if key=="ordered_steps" else key,value)
    db.commit();return _workflow_out(row)


@router.delete("/auxiliary/workflows/{workflow_id}")
def delete_auxiliary_workflow(workflow_id:int,db:Session=Depends(get_db))->dict[str,bool]:
    row=db.get(AuxiliaryWorkflow,workflow_id)
    if not row:raise HTTPException(404,"辅助工作流不存在")
    db.delete(row);db.commit();return {"deleted":True}


@router.post("/auxiliary/plan")
def auxiliary_plan(payload:dict[str,Any]=Body(...),db:Session=Depends(get_db))->dict[str,Any]:
    from apiswitch.protocols.canonical import CanonicalRequest
    unified=db.get(UnifiedModel,payload.get("unified_model_id")) if payload.get("unified_model_id") else db.scalar(select(UnifiedModel).where(UnifiedModel.name==payload.get("unified_model")))
    if not unified:raise _error("validation_error","统一模型不存在")
    request=CanonicalRequest("chat","openai_chat",unified.name,required_input=payload.get("required_input",["text"]),required_output=payload.get("required_output",["text"]))
    try:return plan_auxiliary(db,request,unified)
    except Exception as exc:return {"error":structured_error(getattr(exc,"error_type","auxiliary_workflow_not_configured"),str(exc),getattr(exc,"stage","auxiliary_plan"))["error"]}


@router.get("/tokens")
def list_tokens(db:Session=Depends(get_db))->list[dict[str,Any]]:
    return [_token_out(db,t) for t in db.scalars(select(ApiToken).order_by(ApiToken.id)).all()]


def _replace_token_models(db:Session,token_id:int,value:Any)->list[int]:
    if not isinstance(value,list) or any(not isinstance(item,int) or isinstance(item,bool) for item in value):
        raise _error("validation_error","unified_model_ids 必须是统一模型 ID 数组","token_validation")
    model_ids=list(dict.fromkeys(value))
    existing=set(db.scalars(select(UnifiedModel.id).where(UnifiedModel.id.in_(model_ids))).all()) if model_ids else set()
    missing=[item for item in model_ids if item not in existing]
    if missing:raise _error("validation_error","选择的统一模型不存在","token_validation",{"unified_model_ids":missing})
    db.query(ApiTokenUnifiedModel).filter(ApiTokenUnifiedModel.api_token_id==token_id).delete(synchronize_session=False)
    for model_id in model_ids:db.add(ApiTokenUnifiedModel(api_token_id=token_id,unified_model_id=model_id))
    return model_ids


def _token_out(db:Session,row:ApiToken)->dict[str,Any]:
    models=list(db.execute(select(UnifiedModel.id,UnifiedModel.name).join(ApiTokenUnifiedModel,ApiTokenUnifiedModel.unified_model_id==UnifiedModel.id).where(ApiTokenUnifiedModel.api_token_id==row.id).order_by(UnifiedModel.name)).all())
    return {"id":row.id,"name":row.name,"prefix":row.token_prefix,"scopes":row.scopes_json or [],"expires_at":row.expires_at,"enabled":row.enabled,"last_used_at":row.last_used_at,"budget_id":row.budget_id,"unified_model_ids":[item.id for item in models],"unified_models":[{"id":item.id,"name":item.name} for item in models]}


@router.post("/tokens",status_code=201)
def create_token(payload:dict[str,Any]=Body(...),db:Session=Depends(get_db))->dict[str,Any]:
    plain=generate_api_token(); prefix=token_prefix(plain); token_hash=hash_api_token(plain)
    row=ApiToken(name=str(payload.get("name") or "API Token").strip(),token_prefix=prefix,token_hash=token_hash,scopes_json=_token_scopes(payload.get("scopes",["gateway:invoke"])),expires_at=_token_expiry(payload.get("expires_at")),enabled=bool(payload.get("enabled",True)),budget_id=payload.get("budget_id"));db.add(row);db.flush()
    model_ids=_replace_token_models(db,row.id,payload.get("unified_model_ids",[]));db.commit()
    return {"id":row.id,"token":plain,"prefix":prefix,"unified_model_ids":model_ids,"message":"请立即复制 Token；明文不会再次显示"}


@router.patch("/tokens/{token_id}")
def update_token(token_id:int,payload:dict[str,Any]=Body(...),db:Session=Depends(get_db))->dict[str,Any]:
    row=db.get(ApiToken,token_id)
    if not row:raise HTTPException(404,"Token 不存在")
    allowed={"name","scopes","expires_at","enabled","budget_id","unified_model_ids"}
    unknown=set(payload)-allowed
    if unknown:raise _error("validation_error",f"不支持修改字段：{', '.join(sorted(unknown))}","token_validation")
    for key,value in payload.items():
        if key=="scopes":row.scopes_json=_token_scopes(value)
        elif key=="expires_at":row.expires_at=_token_expiry(value)
        elif key=="name":row.name=str(value).strip()
        elif key=="unified_model_ids":_replace_token_models(db,row.id,value)
        else:setattr(row,key,value)
    db.commit();return _token_out(db,row)


@router.post("/tokens/{token_id}/rotate")
def rotate_token(token_id:int,db:Session=Depends(get_db))->dict[str,Any]:
    """Replace a lost client secret without exposing the previous hash or plaintext."""
    row=db.get(ApiToken,token_id)
    if not row:raise HTTPException(404,"Token 不存在")
    plain=generate_api_token();prefix=token_prefix(plain)
    row.token_prefix=prefix;row.token_hash=hash_api_token(plain);row.last_used_at=None
    db.commit()
    return {"id":row.id,"token":plain,"prefix":prefix,"message":"旧 Token 已立即失效；请复制新 Token，明文不会再次显示"}


def _delete_token_row(db:Session,row:ApiToken)->None:
    token_id=row.id
    for log in db.scalars(select(RequestLog).where(RequestLog.api_token_id==token_id)).all():
        log.api_token_prefix_snapshot=log.api_token_prefix_snapshot or row.token_prefix;log.api_token_id=None
    for usage_row in db.scalars(select(UsageHistory).where(UsageHistory.api_token_id==token_id)).all():usage_row.api_token_id=None
    for agent in db.scalars(select(AgentConfig).where(AgentConfig.api_token_id==token_id)).all():
        agent.api_token_id=None;agent.api_token_mode="auto"
    db.query(ApiTokenUnifiedModel).filter(ApiTokenUnifiedModel.api_token_id==token_id).delete(synchronize_session=False)
    db.delete(row)


@router.delete("/tokens/{token_id}")
def delete_token(token_id:int,db:Session=Depends(get_db))->dict[str,bool]:
    row=db.get(ApiToken,token_id)
    if not row:raise HTTPException(404,"Token 不存在")
    _delete_token_row(db,row);db.commit();return {"deleted":True}


@router.get("/router/status")
def router_status(db:Session=Depends(get_db))->dict[str,Any]:
    models=[_unified_out(db,row) for row in db.scalars(select(UnifiedModel)).all()]
    health=[{"upstream_model_id":row.upstream_model_id,"success_count":row.success_count,"failure_count":row.failure_count,"avg_latency_ms":row.avg_latency_ms,"last_success_at":row.last_success_at,"last_failure_at":row.last_failure_at,"last_failure_reason":row.last_failure_reason} for row in db.scalars(select(ProviderHealth)).all()]
    breakers=[{"upstream_model_id":row.upstream_model_id,"state":row.state,"opened_at":row.opened_at,"half_open_at":row.half_open_at,"consecutive_failures":row.consecutive_failures,"failure_threshold":row.failure_threshold,"cooldown_seconds":row.cooldown_seconds} for row in db.scalars(select(CircuitBreaker)).all()]
    quotas=[{"upstream_model_id":row.upstream_model_id,"remaining_requests":row.remaining_requests,"remaining_tokens":row.remaining_tokens,"remaining_credit":row.remaining_credit,"captured_at":row.captured_at} for row in db.scalars(select(QuotaSnapshot).order_by(QuotaSnapshot.captured_at.desc())).all()]
    return {"models":models,"matrix":{str(row["id"]):protocol_matrix(db,row["id"]) for row in models},"health":health,"circuit_breakers":breakers,"quotas":quotas,"budgets":budgets(db)}


@router.post("/router/convert/test")
def convert_test(payload:dict[str,Any]=Body(...),db:Session=Depends(get_db))->dict[str,Any]:
    from apiswitch.gateway.v2 import parse_ingress, render_egress
    from apiswitch.protocols.canonical import CanonicalResponse
    try:
        canonical=parse_ingress(payload.get("protocol","openai_chat"),payload.get("request",{}),payload.get("model")); unified,candidates,explanation=route_candidates(db,canonical); auxiliary=plan_auxiliary(db,canonical,unified); selected=candidates[0]; upstream={"protocol":selected.provider.protocol_type,"model":selected.upstream.model_id,"canonical":canonical.dump()}; mock=CanonicalResponse(text="Mock upstream response",mock=True); return {"ingress":payload.get("request",{}),"canonical":canonical.dump(),"auxiliary":auxiliary,"candidates":explanation,"upstream_request":upstream,"upstream_response":mock.dump(),"final_response":render_egress(canonical,mock,"dry_run")}
    except Exception as exc:return {"error":structured_error(getattr(exc,"error_type","protocol_conversion_unsupported"),str(exc),getattr(exc,"stage","protocol_conversion"))["error"]}


@router.get("/logs")
def logs(success:bool|None=Query(default=None),unified_model:str|None=None,upstream_model_id:int|None=None,provider_instance_id:int|None=None,inbound_protocol:str|None=None,api_token_id:int|None=None,request_kind:str|None=None,started_after:str|None=None,started_before:str|None=None,min_cost:float|None=Query(default=None,ge=0),max_cost:float|None=Query(default=None,ge=0),limit:int=Query(default=500,ge=1,le=1000),db:Session=Depends(get_db))->list[dict[str,Any]]:
    if min_cost is not None and max_cost is not None and min_cost > max_cost:raise _error("validation_error","最低成本不能高于最高成本","log_filter")
    if request_kind not in {None,"main","auxiliary"}:raise _error("validation_error","无效调用类型","log_filter")
    statement=select(RequestLog)
    for condition in (RequestLog.success==success if success is not None else None,RequestLog.unified_model==unified_model if unified_model else None,RequestLog.upstream_model_id==upstream_model_id if upstream_model_id is not None else None,RequestLog.provider_instance_id==provider_instance_id if provider_instance_id is not None else None,RequestLog.inbound_protocol==inbound_protocol if inbound_protocol else None,RequestLog.api_token_id==api_token_id if api_token_id is not None else None,RequestLog.request_kind==request_kind if request_kind else None):
        if condition is not None:statement=statement.where(condition)
    if started_after:statement=statement.where(RequestLog.started_at>=_token_expiry(started_after))
    if started_before:statement=statement.where(RequestLog.started_at<=_token_expiry(started_before))
    if min_cost is not None:statement=statement.where(RequestLog.estimated_cost>=min_cost)
    if max_cost is not None:statement=statement.where(RequestLog.estimated_cost<=max_cost)
    rows=db.scalars(statement.order_by(RequestLog.id.desc()).limit(limit)).all()
    provider_ids={r.provider_instance_id for r in rows if r.provider_instance_id is not None};upstream_ids={r.upstream_model_id for r in rows if r.upstream_model_id is not None};token_ids={r.api_token_id for r in rows if r.api_token_id is not None}
    provider_names=dict(db.execute(select(ProviderInstance.id,ProviderInstance.name).where(ProviderInstance.id.in_(provider_ids))).all()) if provider_ids else {}
    upstream_names=dict(db.execute(select(UpstreamModel.id,UpstreamModel.model_id).where(UpstreamModel.id.in_(upstream_ids))).all()) if upstream_ids else {}
    token_names=dict(db.execute(select(ApiToken.id,ApiToken.name).where(ApiToken.id.in_(token_ids))).all()) if token_ids else {}
    return [{"request_id":r.request_id,"request_kind":r.request_kind or "main","parent_request_id":r.parent_request_id,"started_at":r.started_at,"finished_at":r.finished_at,"inbound_protocol":r.inbound_protocol,"unified_model":r.unified_model,"provider_instance_id":r.provider_instance_id,"provider_name":provider_names.get(r.provider_instance_id),"upstream_model_id":r.upstream_model_id,"upstream_model_name":upstream_names.get(r.upstream_model_id),"api_token_id":r.api_token_id,"api_token_name":token_names.get(r.api_token_id),"api_token_prefix":r.api_token_prefix_snapshot,"success":r.success,"error_type":r.error_type,"error_message":r.error_message,"failure_stage":r.failure_stage,"candidate_summary":r.candidate_summary_json,"auxiliary_summary":r.auxiliary_summary_json,"prompt":r.prompt_json,"response":r.response_json,"input_tokens":r.input_tokens,"output_tokens":r.output_tokens,"estimated_cost":r.estimated_cost,"latency_ms":r.latency_ms,"first_token_latency_ms":r.first_token_latency_ms} for r in rows]


@router.get("/accounting/pricing")
def pricing(db:Session=Depends(get_db))->list[dict[str,Any]]: return [_model_out(row) for row in db.scalars(select(UpstreamModel)).all()]


@router.patch("/accounting/pricing/{model_id}")
def update_pricing(model_id:int,payload:dict[str,Any]=Body(...),db:Session=Depends(get_db))->dict[str,Any]:
    row=db.get(UpstreamModel,model_id)
    if not row:raise HTTPException(404,"上游模型不存在")
    allowed={"input_price","output_price","cached_input_price","currency","pricing_source","pricing_effective_at"}
    if set(payload)-allowed:raise _error("validation_error","价格请求包含不支持字段","pricing_validation")
    for key,value in payload.items():setattr(row,key,_token_expiry(value) if key=="pricing_effective_at" else value)
    if any(value is not None and value<0 for value in (row.input_price,row.output_price,row.cached_input_price)):raise _error("validation_error","价格不能为负数","pricing_validation")
    db.commit();return _model_out(row)


@router.get("/accounting/usage")
def usage(db:Session=Depends(get_db))->dict[str,Any]:
    def grouped(column:Any)->list[dict[str,Any]]:
        rows=db.execute(select(column,func.count(UsageHistory.id),func.coalesce(func.sum(UsageHistory.input_tokens),0),func.coalesce(func.sum(UsageHistory.output_tokens),0),func.coalesce(func.sum(UsageHistory.estimated_cost),0)).group_by(column).order_by(func.count(UsageHistory.id).desc())).all()
        return [{"key":key,"requests":count,"input_tokens":int(input_tokens or 0),"output_tokens":int(output_tokens or 0),"cost":float(cost or 0)} for key,count,input_tokens,output_tokens,cost in rows]
    return {"requests":db.scalar(select(func.count()).select_from(RequestLog)) or 0,"successful_requests":db.scalar(select(func.count()).select_from(UsageHistory)) or 0,"input_tokens":db.scalar(select(func.coalesce(func.sum(UsageHistory.input_tokens),0))) or 0,"output_tokens":db.scalar(select(func.coalesce(func.sum(UsageHistory.output_tokens),0))) or 0,"cost":float(db.scalar(select(func.coalesce(func.sum(UsageHistory.estimated_cost),0))) or 0),"by_provider_instance":grouped(UsageHistory.provider_instance_id),"by_upstream_model":grouped(UsageHistory.upstream_model_id),"by_unified_model":grouped(UsageHistory.unified_model),"by_protocol":grouped(UsageHistory.inbound_protocol),"by_api_token":grouped(UsageHistory.api_token_id)}


@router.get("/budgets")
def budgets(db:Session=Depends(get_db))->list[dict[str,Any]]:
    result=[_budget_out(row) for row in db.scalars(select(Budget).order_by(Budget.id.desc())).all()];db.commit();return result


@router.post("/budgets",status_code=201)
def add_budget(payload:dict[str,Any]=Body(...),db:Session=Depends(get_db))->dict[str,Any]:
    scope=str(payload.get("scope") or "global");mode=str(payload.get("billing_mode") or "token_cost");period=str(payload.get("period_type") or "calendar_month");action=str(payload.get("enforcement_action") or "warn")
    if scope not in _BUDGET_SCOPES:raise _error("validation_error","无效预算范围")
    if mode not in _BUDGET_MODES:raise _error("validation_error","无效计费方式")
    if period not in _BUDGET_PERIODS:raise _error("validation_error","无效统计周期")
    if action not in _BUDGET_ACTIONS:raise _error("validation_error","无效预算动作")
    name=str(payload.get("name") or "").strip()
    if not name:raise _error("validation_error","预算名称不能为空")
    threshold=int(payload.get("alert_threshold_percent",80))
    if not 1<=threshold<=100:raise _error("validation_error","告警阈值必须在 1 到 100 之间")
    raw_limit=payload.get("limit_value",payload.get("request_limit") if mode=="request_count" else payload.get("monthly_limit"))
    if raw_limit is not None and float(raw_limit)<0:raise _error("validation_error","预算上限不能小于 0")
    if mode=="request_count" and raw_limit is not None and float(raw_limit)!=int(float(raw_limit)):raise _error("validation_error","调用条数上限必须是整数")
    row=Budget(name=name,scope=scope,scope_id=_validate_budget_target(db,scope,payload.get("scope_id")),billing_mode=mode,period_type=period,monthly_limit=float(raw_limit) if mode=="token_cost" and raw_limit is not None else None,request_limit=int(float(raw_limit)) if mode=="request_count" and raw_limit is not None else None,currency=payload.get("currency","USD"),alert_threshold_percent=threshold,enforcement_action=action,enabled=payload.get("enabled",True));refresh_budget_period(row);db.add(row);db.commit();db.refresh(row);return _budget_out(row)


@router.patch("/budgets/{budget_id}")
def update_budget(budget_id:int,payload:dict[str,Any]=Body(...),db:Session=Depends(get_db))->dict[str,Any]:
    row=db.get(Budget,budget_id)
    if not row:raise HTTPException(404,"预算不存在")
    scope=str(payload.get("scope",row.scope));mode=str(payload.get("billing_mode",row.billing_mode));period=str(payload.get("period_type",row.period_type));action=str(payload.get("enforcement_action",row.enforcement_action))
    if scope not in _BUDGET_SCOPES:raise _error("validation_error","无效预算范围")
    if mode not in _BUDGET_MODES:raise _error("validation_error","无效计费方式")
    if period not in _BUDGET_PERIODS:raise _error("validation_error","无效统计周期")
    if action not in _BUDGET_ACTIONS:raise _error("validation_error","无效预算动作")
    name=str(payload.get("name",row.name)).strip()
    if not name:raise _error("validation_error","预算名称不能为空")
    threshold=int(payload.get("alert_threshold_percent",row.alert_threshold_percent))
    if not 1<=threshold<=100:raise _error("validation_error","告警阈值必须在 1 到 100 之间")
    target=_validate_budget_target(db,scope,payload.get("scope_id",row.scope_id))
    mode_or_period_changed=mode!=row.billing_mode or period!=row.period_type
    row.name=name;row.scope=scope;row.scope_id=target;row.billing_mode=mode;row.period_type=period;row.enforcement_action=action;row.alert_threshold_percent=threshold
    for key in ("currency","enabled"):
        if key in payload:setattr(row,key,payload[key])
    if "spent_amount" in payload:row.spent_amount=max(float(payload["spent_amount"]),0)
    if "request_count" in payload:row.request_count=max(int(payload["request_count"]),0)
    has_limit=any(key in payload for key in ("limit_value","monthly_limit","request_limit")) or mode_or_period_changed
    if has_limit:
        raw_limit=payload.get("limit_value",payload.get("request_limit") if mode=="request_count" else payload.get("monthly_limit"))
        if raw_limit is not None and float(raw_limit)<0:raise _error("validation_error","预算上限不能小于 0")
        if mode=="request_count" and raw_limit is not None and float(raw_limit)!=int(float(raw_limit)):raise _error("validation_error","调用条数上限必须是整数")
        row.monthly_limit=float(raw_limit) if mode=="token_cost" and raw_limit is not None else None;row.request_limit=int(float(raw_limit)) if mode=="request_count" and raw_limit is not None else None
    if mode_or_period_changed:row.spent_amount=0;row.request_count=0;row.period_started_at=None
    refresh_budget_period(row);db.commit();db.refresh(row);return _budget_out(row)


@router.delete("/budgets/{budget_id}")
def delete_budget(budget_id:int,db:Session=Depends(get_db))->dict[str,bool]:
    row=db.get(Budget,budget_id)
    if not row:raise HTTPException(404,"预算不存在")
    db.delete(row);db.commit();return {"deleted":True}


@router.get("/settings")
def get_settings(db:Session=Depends(get_db))->dict[str,Any]:
    values={row.key:row.value_json for row in db.scalars(select(SystemSetting)).all()}
    values.setdefault("gateway_enabled",True)
    values.setdefault("save_full_prompt_response",False)
    return values


@router.patch("/settings")
def update_settings(payload:dict[str,Any]=Body(...),db:Session=Depends(get_db))->dict[str,Any]:
    if "gateway_enabled" in payload and not isinstance(payload["gateway_enabled"],bool):
        raise _error("validation_error","gateway_enabled 必须是布尔值","system_settings")
    if "save_full_prompt_response" in payload and not isinstance(payload["save_full_prompt_response"],bool):
        raise _error("validation_error","save_full_prompt_response 必须是布尔值","system_settings")
    if "preferred_port" in payload:
        value=payload["preferred_port"]
        if isinstance(value,bool) or not isinstance(value,int) or not 1<=value<=65535:
            raise _error("validation_error","preferred_port 必须是 1 到 65535 的整数","system_settings")
    if "upload_limit_bytes" in payload:
        value=payload["upload_limit_bytes"]
        if isinstance(value,bool) or not isinstance(value,int) or not 1<=value<=10*1024*1024*1024:
            raise _error("validation_error","upload_limit_bytes 必须是 1 到 10 GiB 的整数","system_settings")
    for key,value in payload.items():
        row=db.get(SystemSetting,key) or SystemSetting(key=key);row.value_json=value;db.add(row)
    db.commit();return get_settings(db)


@router.get("/settings/startup")
def startup_status()->dict[str,Any]:
    from apiswitch.desktop import _read_startup_command,is_startup_enabled
    return {"enabled":is_startup_enabled(),"command":_read_startup_command()}


@router.patch("/settings/startup")
def update_startup(payload:dict[str,Any]=Body(...))->dict[str,Any]:
    from apiswitch.desktop import _read_startup_command,is_startup_enabled,set_startup_enabled
    try:set_startup_enabled(bool(payload.get("enabled")))
    except OSError as exc:raise _error("startup_update_failed",str(exc),"desktop_settings") from exc
    return {"enabled":is_startup_enabled(),"command":_read_startup_command()}


@router.post("/database/backup")
def database_backup(payload:dict[str,Any]=Body(default={})) -> dict[str,Any]:
    from pathlib import Path
    from apiswitch.backup.archive import BackupError,create_archive
    root=_runtime_data_dir(); destination=Path(payload.get("destination") or root/"backups"/f"backup-{utc_now().strftime('%Y%m%dT%H%M%SZ')}.apsbak")
    try:return create_archive(root,str(payload.get("backup_password", "")),destination)
    except BackupError as exc:raise _error("webdav_backup_invalid",str(exc),"backup") from exc


@router.post("/database/restore")
def database_restore(payload:dict[str,Any]=Body(...))->dict[str,bool]:
    from pathlib import Path
    from apiswitch.backup.archive import BackupError,restore_archive
    from apiswitch.db.session import engine
    if payload.get("confirm") is not True:raise _error("confirmation_required","恢复会替换本地数据，必须显式确认","restore")
    try:
        engine.dispose()
        restore_archive(_runtime_data_dir(),str(payload.get("backup_password","")),Path(payload["archive_path"]))
    except BackupError as exc:
        code="database_generation_mismatch" if str(exc)=="database_generation_mismatch" else "webdav_backup_invalid"
        raise _error(code,"数据库 generation 不兼容" if code=="database_generation_mismatch" else str(exc),"restore") from exc
    except Exception as exc:raise _error("webdav_backup_invalid",str(exc),"restore") from exc
    finally:engine.dispose()
    return {"restored":True}


@router.get("/agents")
def agents(db:Session=Depends(get_db))->list[dict[str,Any]]:
    from apiswitch.services.agent_configs import AGENT_SPECS
    rows=[]
    for agent in db.scalars(select(AgentConfig)).all():
        token=db.get(ApiToken,agent.api_token_id) if agent.api_token_id else None
        rows.append({
            "id":agent.id,"agent_type":agent.agent_type,"label":AGENT_SPECS.get(agent.agent_type,{}).get("label",agent.agent_type),
            "profile_name":agent.profile_name,"config_path":agent.config_path,"enabled":agent.enabled,
            "main_model_id":agent.main_model_id,"model_ids":agent.model_ids_json or ([agent.main_model_id] if agent.main_model_id else []),
            "opus_model_id":agent.opus_model_id,"sonnet_model_id":agent.sonnet_model_id,"haiku_model_id":agent.haiku_model_id,
            "api_token_id":token.id if token else None,"api_token_prefix":token.token_prefix if token else None,
            "api_token_name":token.name if token else None,"api_token_mode":agent.api_token_mode or "auto",
            "last_written_base_url":agent.last_written_base_url,"last_backup_path":agent.last_backup_path,
        })
    return rows


@router.delete("/agents/{agent_type}")
def delete_agent(agent_type:str,db:Session=Depends(get_db))->dict[str,Any]:
    row=_agent_row(db,agent_type)
    if not row:raise HTTPException(404,"Agent 配置不存在")
    token_id=row.api_token_id
    remove_independent_token=(row.api_token_mode or "auto")=="auto" and token_id is not None
    config_path=row.config_path
    db.delete(row);db.flush()
    token_deleted=False
    if remove_independent_token:
        still_referenced=db.scalar(select(AgentConfig.id).where(AgentConfig.api_token_id==token_id).limit(1))
        token=db.get(ApiToken,token_id)
        if still_referenced is None and token is not None:
            _delete_token_row(db,token)
            token_deleted=True
    db.commit()
    return {
        "deleted":True,
        "agent_type":agent_type,
        "api_token_deleted":token_deleted,
        "config_file_preserved":True,
        "config_path":config_path,
    }


_AGENT_TOKEN_PATTERN=re.compile(r"ask_[A-Za-z0-9_-]{8,}")


def _agent_model_ids(payload:dict[str,Any])->list[int]:
    raw=payload.get("model_ids",[])
    if raw is None:raw=[]
    if not isinstance(raw,list) or any(not isinstance(item,int) or isinstance(item,bool) for item in raw):
        raise _error("validation_error","model_ids 必须是统一模型 ID 数组","agent_config")
    values=[payload.get("main_model_id"),*raw]
    values.extend(payload.get(field) for field in ("opus_model_id","sonnet_model_id","haiku_model_id"))
    return list(dict.fromkeys(item for item in values if isinstance(item,int) and not isinstance(item,bool)))


def _agent_row(db:Session,agent_type:str)->AgentConfig|None:
    return db.scalar(select(AgentConfig).where(AgentConfig.agent_type==agent_type))


def _agent_token_mode(payload:dict[str,Any])->str:
    mode=str(payload.get("api_token_mode") or "auto")
    if mode not in {"auto","manual"}:
        raise _error("validation_error","API Key 模式必须是 auto 或 manual","agent_config")
    return mode


def _manual_agent_token(
    db:Session,
    payload:dict[str,Any],
    saved:AgentConfig|None,
    model_ids:list[int],
)->tuple[ApiToken,str|None]:
    token_id=payload.get("api_token_id")
    if not isinstance(token_id,int) or isinstance(token_id,bool):
        raise _error("validation_error","请选择现有 API Key","agent_config")
    token=db.get(ApiToken,token_id)
    if not token:
        raise _error("validation_error","选择的 API Key 不存在","agent_config")
    if not token.enabled:
        raise _error("validation_error","选择的 API Key 已停用","agent_config")
    if "gateway:invoke" not in (token.scopes_json or []):
        raise _error("validation_error","选择的 API Key 缺少 gateway:invoke 权限","agent_config")
    if token.expires_at:
        expires=token.expires_at if token.expires_at.tzinfo else token.expires_at.replace(tzinfo=timezone.utc)
        if expires<=datetime.now(timezone.utc):
            raise _error("validation_error","选择的 API Key 已过期","agent_config")
    allowed=set(db.scalars(select(ApiTokenUnifiedModel.unified_model_id).where(ApiTokenUnifiedModel.api_token_id==token.id)).all())
    missing=[model_id for model_id in model_ids if model_id not in allowed]
    if missing:
        raise _error(
            "validation_error",
            "选择的 API Key 未授权全部 Agent 模型，请先在客户端管理中补充模型权限",
            "agent_config",
            {"unified_model_ids":missing},
        )
    provided=str(payload.get("api_token") or "").strip() or None
    if provided and hash_api_token(provided)!=token.token_hash:
        raise _error("validation_error","API Key 明文与所选 Key 不匹配","agent_config")
    if not provided and (not saved or saved.api_token_id!=token.id):
        raise _error(
            "validation_error",
            "首次选择此 API Key 时请输入明文；APISwitch 数据库只保存哈希，无法反向读取",
            "agent_config",
        )
    return token,provided


def _preview_agent_token(
    db:Session,
    payload:dict[str,Any],
    saved:AgentConfig|None,
    model_ids:list[int],
)->tuple[str,str|None,ApiToken|None]:
    from apiswitch.services.agent_configs import AGENT_TOKEN_PLACEHOLDER
    mode=_agent_token_mode(payload)
    if mode=="manual":
        token,plain=_manual_agent_token(db,payload,saved,model_ids)
        return mode,plain,token
    saved_token=db.get(ApiToken,saved.api_token_id) if saved and saved.api_token_id and (saved.api_token_mode or "auto")=="auto" else None
    preview_token=payload.get("api_token") or (
        AGENT_TOKEN_PLACEHOLDER if payload.get("rotate_api_key") or saved_token is None else None
    )
    return mode,preview_token,saved_token


def _ensure_agent_token(
    db:Session,
    row:AgentConfig,
    model_ids:list[int],
    *,
    requested_token:Any=None,
    rotate:bool=False,
)->tuple[str|None,ApiToken,bool]:
    from apiswitch.services.agent_configs import AGENT_SPECS
    provided=str(requested_token).strip() if isinstance(requested_token,str) and requested_token.strip() else None
    token_row=db.get(ApiToken,row.api_token_id) if row.api_token_id else None
    created=token_row is None
    plain=provided
    if token_row is None:
        plain=plain or generate_api_token()
        token_row=ApiToken(
            name=f"Agent · {AGENT_SPECS.get(row.agent_type,{}).get('label',row.agent_type)}",
            token_prefix=token_prefix(plain),token_hash=hash_api_token(plain),
            scopes_json=["gateway:invoke"],enabled=True,
        )
        db.add(token_row);db.flush();row.api_token_id=token_row.id
    elif rotate or plain:
        plain=plain or generate_api_token()
        token_row.token_prefix=token_prefix(plain);token_row.token_hash=hash_api_token(plain);token_row.last_used_at=None;token_row.enabled=True
    _replace_token_models(db,token_row.id,model_ids)
    return plain,token_row,created


def _content_with_agent_token(content:str,token_row:ApiToken,plain:str|None)->str:
    from apiswitch.services.agent_configs import AGENT_TOKEN_PLACEHOLDER
    rendered=content
    if plain:
        rendered=rendered.replace(AGENT_TOKEN_PLACEHOLDER,plain)
        rendered=_AGENT_TOKEN_PATTERN.sub(
            lambda match: plain if hash_api_token(match.group(0))==token_row.token_hash else match.group(0),
            rendered,
        )
        if plain not in rendered:
            raise ValueError("编辑后的配置缺少选定的 Agent API Key")
        return rendered
    if not any(hash_api_token(value)==token_row.token_hash for value in _AGENT_TOKEN_PATTERN.findall(rendered)):
        raise ValueError("编辑后的配置缺少当前 Agent API Key；自动模式可启用轮换，手动模式请重新输入明文")
    return rendered


def _editable_content(value:Any)->str|None:
    if value is None:return None
    if isinstance(value,str):return value
    if isinstance(value,dict):return json.dumps(value,ensure_ascii=False,indent=2)+"\n"
    raise _error("validation_error","content 必须是可编辑的配置文本","agent_config")


def _claude_preview(payload:dict[str,Any],db:Session)->dict[str,Any]:
    from apiswitch.desktop import runtime_info
    from apiswitch.services.agent_configs import MODEL_FIELDS,_selected_models,claude_content,validate_user_config_path
    base_url=runtime_info()["base_url"]
    from pathlib import Path
    try:target=validate_user_config_path(Path(payload.get("config_path") or Path.home()/".claude"/"settings.json"))
    except ValueError as exc:raise _error("validation_error",str(exc),"agent_config") from exc
    existing=target.read_text(encoding="utf-8") if target.is_file() else ""
    saved=_agent_row(db,"claude-code")
    model_ids=_agent_model_ids(payload)
    mode,preview_token,token=_preview_agent_token(db,payload,saved,model_ids)
    try:
        _selected_models(db,payload.get("main_model_id"),model_ids,"anthropic_messages")
        content=claude_content(db,{key:payload.get(key) for key in MODEL_FIELDS},str(base_url),existing_text=existing,api_token=preview_token)
        rendered=json.dumps(content,ensure_ascii=False,indent=2)+"\n"
        if mode=="manual" and token:rendered=_content_with_agent_token(rendered,token,preview_token)
    except ValueError as exc:raise _error("validation_error",str(exc)) from exc
    return {
        "base_url":base_url,"config_path":str(target),
        "content":rendered,"language":"json",
        "token_hint":"可自动创建独立 API Key，也可选择已有 Key；明文直接写入此配置，不依赖外部环境变量。",
        "api_token_mode":mode,"api_token_id":token.id if token else None,
        "api_token_name":token.name if token else None,"api_token_prefix":token.token_prefix if token else None,
    }


@router.post("/agents/claude-code/preview")
def claude_preview(payload:dict[str,Any]=Body(...),db:Session=Depends(get_db))->dict[str,Any]:return _claude_preview(payload,db)


@router.post("/agents/claude-code/write")
def claude_write(payload:dict[str,Any]=Body(...),db:Session=Depends(get_db))->dict[str,Any]:
    from apiswitch.services.agent_configs import MODEL_FIELDS,write_claude_config
    preview=_claude_preview(payload,db)
    row=_agent_row(db,"claude-code") or AgentConfig(agent_type="claude-code")
    previous_mode=row.api_token_mode or "auto"
    for key in MODEL_FIELDS:setattr(row,key,payload.get(key))
    model_ids=_agent_model_ids(payload);row.model_ids_json=model_ids
    row.config_path=preview["config_path"];row.enabled=True;db.add(row);db.flush()
    try:
        mode=_agent_token_mode(payload)
        if mode=="manual":
            token_row,plain=_manual_agent_token(db,payload,row,model_ids);created=False
            row.api_token_id=token_row.id;row.api_token_mode="manual"
        else:
            if previous_mode!="auto":row.api_token_id=None
            row.api_token_mode="auto"
            plain,token_row,created=_ensure_agent_token(db,row,model_ids,requested_token=payload.get("api_token"),rotate=bool(payload.get("rotate_api_key")))
        content=_content_with_agent_token(_editable_content(payload.get("content")) or preview["content"],token_row,plain)
        backup=write_claude_config(db,row,str(preview["base_url"]),api_token=plain,content_override=content);db.commit()
    except (OSError,ValueError) as exc:db.rollback();raise _error("agent_write_failed",str(exc),"agent_config") from exc
    return {"written":True,"path":row.config_path,"backup_path":str(backup) if backup else None,"api_token_mode":row.api_token_mode,"api_token_id":token_row.id,"api_token_prefix":token_row.token_prefix,"api_token_created":created}


@router.post("/agents/claude-code/restore")
def claude_restore(payload:dict[str,Any]=Body(...))->dict[str,bool]:
    from apiswitch.services.agent_configs import restore_agent_config
    try:restore_agent_config(str(payload["config_path"]),str(payload["backup_path"]))
    except (OSError,ValueError) as exc:raise _error("validation_error",str(exc),"agent_config") from exc
    return {"restored":True}


def _agent_preview(agent_type:str,payload:dict[str,Any],db:Session)->dict[str,Any]:
    from apiswitch.desktop import runtime_info
    from apiswitch.services.agent_configs import preview_agent_config
    saved=_agent_row(db,agent_type)
    model_ids=_agent_model_ids(payload)
    mode,preview_token,token=_preview_agent_token(db,payload,saved,model_ids)
    try:
        result=preview_agent_config(
            db,agent_type,payload.get("main_model_id"),str(runtime_info()["base_url"]),payload.get("config_path"),
            model_ids=model_ids,api_token=preview_token,
        )
        if mode=="manual" and token:result["content"]=_content_with_agent_token(result["content"],token,preview_token)
        result["api_token_mode"]=mode
        result["api_token_id"]=token.id if token else None
        result["api_token_name"]=token.name if token else None
        result["api_token_prefix"]=token.token_prefix if token else None
        return result
    except (OSError,ValueError) as exc:raise _error("validation_error",str(exc),"agent_config") from exc


@router.post("/agents/{agent_type}/preview")
def agent_preview(agent_type:str,payload:dict[str,Any]=Body(...),db:Session=Depends(get_db))->dict[str,Any]:
    return _agent_preview(agent_type,payload,db)


@router.post("/agents/{agent_type}/write")
def agent_write(agent_type:str,payload:dict[str,Any]=Body(...),db:Session=Depends(get_db))->dict[str,Any]:
    from apiswitch.desktop import runtime_info
    from apiswitch.services.agent_configs import write_agent_config
    preview=_agent_preview(agent_type,payload,db)
    row=_agent_row(db,agent_type) or AgentConfig(agent_type=agent_type)
    previous_mode=row.api_token_mode or "auto"
    model_ids=_agent_model_ids(payload)
    row.config_path=preview["config_path"];row.main_model_id=payload.get("main_model_id");row.model_ids_json=model_ids;row.enabled=True;db.add(row);db.flush()
    try:
        mode=_agent_token_mode(payload)
        if mode=="manual":
            token_row,plain=_manual_agent_token(db,payload,row,model_ids);created=False
            row.api_token_id=token_row.id;row.api_token_mode="manual"
        else:
            if previous_mode!="auto":row.api_token_id=None
            row.api_token_mode="auto"
            plain,token_row,created=_ensure_agent_token(db,row,model_ids,requested_token=payload.get("api_token"),rotate=bool(payload.get("rotate_api_key")))
        content=_content_with_agent_token(_editable_content(payload.get("content")) or preview["content"],token_row,plain)
        backup=write_agent_config(db,row,str(runtime_info()["base_url"]),plain,content_override=content);db.commit()
    except (OSError,ValueError) as exc:db.rollback();raise _error("agent_write_failed",str(exc),"agent_config") from exc
    return {"written":True,"path":row.config_path,"backup_path":str(backup) if backup else None,"agent_type":agent_type,"api_token_mode":row.api_token_mode,"api_token_id":token_row.id,"api_token_prefix":token_row.token_prefix,"api_token_created":created}


@router.post("/agents/{agent_type}/restore")
def agent_restore(agent_type:str,payload:dict[str,Any]=Body(...))->dict[str,bool]:
    from apiswitch.services.agent_configs import restore_agent_config
    try:restore_agent_config(str(payload["config_path"]),str(payload["backup_path"]))
    except (OSError,ValueError) as exc:raise _error("validation_error",str(exc),"agent_config") from exc
    return {"restored":True}


@router.post("/agents/refresh-base-url")
def refresh_agent_base_url(db:Session=Depends(get_db))->dict[str,int]:
    from apiswitch.desktop import runtime_info
    from apiswitch.services.agent_configs import refresh_enabled_agent_configs
    try:updated=refresh_enabled_agent_configs(db,str(runtime_info()["base_url"]))
    except (OSError,ValueError) as exc:db.rollback();raise _error("agent_write_failed",str(exc),"agent_config") from exc
    return {"updated":updated}


def _webdav_out(row:WebDAVProfile)->dict[str,Any]:return {"id":row.id,"name":row.name,"url":row.url,"username":row.username,"enabled":row.enabled,"password_configured":bool(row.password_encrypted),"backup_password_configured":bool(row.backup_password_encrypted)}
@router.get("/webdav/profiles")
def list_webdav_profiles(db:Session=Depends(get_db))->list[dict[str,Any]]:return [_webdav_out(row) for row in db.scalars(select(WebDAVProfile)).all()]
@router.post("/webdav/profiles",status_code=201)
def add_webdav_profile(payload:dict[str,Any]=Body(...),db:Session=Depends(get_db))->dict[str,Any]:
    try:url=validate_outbound_url(str(payload.get("url","")),"WebDAV 地址")
    except OutboundURLRejected as exc:raise _error("validation_error",str(exc),"webdav_validation") from exc
    row=WebDAVProfile(name=payload.get("name","WebDAV"),url=url,username=payload.get("username"),password_encrypted=secret_crypto.encrypt(payload["password"]) if payload.get("password") else None,backup_password_encrypted=secret_crypto.encrypt(payload["backup_password"]) if payload.get("backup_password") else None,enabled=payload.get("enabled",True));db.add(row);db.commit();return _webdav_out(row)
@router.patch("/webdav/profiles/{profile_id}")
def update_webdav_profile(profile_id:int,payload:dict[str,Any]=Body(...),db:Session=Depends(get_db))->dict[str,Any]:
    row=db.get(WebDAVProfile,profile_id)
    if not row:raise HTTPException(404,"WebDAV 配置不存在")
    if "url" in payload:
        try:payload["url"]=validate_outbound_url(str(payload["url"]),"WebDAV 地址")
        except OutboundURLRejected as exc:raise _error("validation_error",str(exc),"webdav_validation") from exc
    for key in ("name","url","username","enabled"):
        if key in payload:setattr(row,key,payload[key])
    for key,attr in (("password","password_encrypted"),("backup_password","backup_password_encrypted")):
        if key in payload:setattr(row,attr,secret_crypto.encrypt(payload[key]) if payload[key] else None)
    db.commit();return _webdav_out(row)
@router.delete("/webdav/profiles/{profile_id}")
def delete_webdav_profile(profile_id:int,db:Session=Depends(get_db))->dict[str,bool]:
    row=db.get(WebDAVProfile,profile_id)
    if not row:raise HTTPException(404,"WebDAV 配置不存在")
    db.delete(row);db.commit();return {"deleted":True}
@router.post("/webdav/profiles/{profile_id}/test")
def test_webdav(profile_id:int,db:Session=Depends(get_db))->dict[str,Any]:
    row=db.get(WebDAVProfile,profile_id)
    if not row:raise HTTPException(404,"WebDAV 配置不存在")
    from apiswitch.backup.webdav import test
    try:test(row.url,row.username,secret_crypto.decrypt(row.password_encrypted) if row.password_encrypted else None)
    except Exception as exc:
        db.add(WebDAVSyncLog(profile_id=row.id,direction="test",success=False,error_message=str(exc)));db.commit()
        raise _error("webdav_connection_failed",str(exc),"webdav_test") from exc
    db.add(WebDAVSyncLog(profile_id=row.id,direction="test",success=True));db.commit()
    return {"ok":True,"mode":"remote","message":"WebDAV 连接成功"}
@router.get("/webdav/profiles/{profile_id}/archives")
def webdav_archives(profile_id:int,db:Session=Depends(get_db))->list[dict[str,Any]]:
    row=db.get(WebDAVProfile,profile_id)
    if not row or not row.enabled:raise _error("validation_error","WebDAV 配置不存在或已禁用")
    from apiswitch.backup.webdav import list_archives
    try:items=list_archives(row.url,row.username,secret_crypto.decrypt(row.password_encrypted) if row.password_encrypted else None)
    except Exception as exc:
        db.add(WebDAVSyncLog(profile_id=row.id,direction="list",success=False,error_message=str(exc)));db.commit();raise _error("webdav_connection_failed",str(exc),"webdav_list") from exc
    db.add(WebDAVSyncLog(profile_id=row.id,direction="list",success=True));db.commit();return items
@router.post("/webdav/export")
def webdav_export(payload:dict[str,Any]=Body(...),db:Session=Depends(get_db))->dict[str,Any]:
    return database_backup({"backup_password":payload.get("backup_password"),"destination":payload.get("destination")})
@router.post("/webdav/preview")
def webdav_preview(payload:dict[str,Any]=Body(...))->dict[str,Any]:
    import hashlib
    from pathlib import Path
    from apiswitch.backup.archive import inspect_archive
    path=Path(payload["archive_path"])
    try:metadata=inspect_archive(path,str(payload.get("backup_password","")))
    except Exception as exc:raise _error("webdav_backup_invalid",str(exc),"webdav_preview") from exc
    local=_runtime_data_dir();declared={item["path"]:item["sha256"] for item in metadata["files"]};conflicts=[]
    for relative,expected in declared.items():
        current=local.joinpath(*relative.split("/"))
        if current.is_file() and hashlib.sha256(current.read_bytes()).hexdigest()!=expected:conflicts.append(relative)
    local_paths=[]
    for root_name in ("apiswitch.db","master.key","files","logs"):
        root=local/root_name
        if root.is_file():paths=[root]
        elif root.is_dir():paths=[item for item in root.rglob("*") if item.is_file()]
        else:paths=[]
        for item in paths:
            relative=str(item.relative_to(local)).replace("\\","/")
            if relative not in declared:local_paths.append(relative)
    return {"archive_exists":True,"archive":metadata,"local_changes":sorted(local_paths),"conflicts":sorted(conflicts),"conflict_strategies":["abort","replace_local"],"action":"restore_requires_explicit_confirmation"}
@router.post("/webdav/upload")
def webdav_upload(payload:dict[str,Any]=Body(...),db:Session=Depends(get_db))->dict[str,Any]:
    from pathlib import Path
    from apiswitch.backup.webdav import upload
    row=db.get(WebDAVProfile,int(payload["profile_id"]))
    if not row or not row.enabled:raise _error("validation_error","WebDAV 配置不存在或已禁用")
    backup_password=payload.get("backup_password") or (secret_crypto.decrypt(row.backup_password_encrypted) if row.backup_password_encrypted else None)
    if not backup_password:raise _error("validation_error","WebDAV 配置缺少独立备份密码","webdav_upload")
    exported=webdav_export({"backup_password":backup_password,"destination":payload.get("destination")})
    remote_path=str(payload.get("remote_path") or Path(exported["path"]).name)
    try:upload(row.url,remote_path,row.username,secret_crypto.decrypt(row.password_encrypted) if row.password_encrypted else None,Path(exported["path"]))
    except Exception as exc:
        db.add(WebDAVSyncLog(profile_id=row.id,direction="upload",success=False,error_message=str(exc)));db.commit();raise _error("webdav_upload_failed",str(exc),"webdav_upload") from exc
    db.add(WebDAVSyncLog(profile_id=row.id,direction="upload",remote_version=remote_path,checksum=exported["sha256"],success=True,summary_json={"archive_path":Path(exported["path"]).name}));db.commit();return {"uploaded":True,"remote_path":remote_path,"archive":exported}
@router.post("/webdav/download")
def webdav_download(payload:dict[str,Any]=Body(...),db:Session=Depends(get_db))->dict[str,Any]:
    from pathlib import Path
    from apiswitch.backup.webdav import download
    row=db.get(WebDAVProfile,int(payload["profile_id"]))
    if not row or not row.enabled:raise _error("validation_error","WebDAV 配置不存在或已禁用")
    remote_path=str(payload["remote_path"]); destination=_runtime_data_dir()/"backups"/"downloads"/Path(remote_path).name
    try:download(row.url,remote_path,row.username,secret_crypto.decrypt(row.password_encrypted) if row.password_encrypted else None,destination)
    except Exception as exc:
        db.add(WebDAVSyncLog(profile_id=row.id,direction="download",success=False,error_message=str(exc)));db.commit();raise _error("webdav_download_failed",str(exc),"webdav_download") from exc
    checksum=__import__("hashlib").sha256(destination.read_bytes()).hexdigest()
    db.add(WebDAVSyncLog(profile_id=row.id,direction="download",remote_version=remote_path,checksum=checksum,success=True));db.commit();return {"downloaded":True,"archive_path":str(destination),"sha256":checksum,"requires_explicit_restore":True}
@router.post("/webdav/restore")
def webdav_restore(payload:dict[str,Any]=Body(...),db:Session=Depends(get_db))->dict[str,bool]:
    strategy=payload.get("conflict_strategy","replace_local")
    if strategy not in {"abort","replace_local"}:raise _error("validation_error","无效冲突策略","restore")
    if strategy=="abort":
        preview=webdav_preview(payload)
        if preview["conflicts"] or preview["local_changes"]:raise _error("restore_conflict","检测到本地冲突，已中止恢复","restore",{"conflicts":preview["conflicts"],"local_changes":preview["local_changes"]})
    result=database_restore(payload)
    profile_id=payload.get("profile_id")
    if profile_id and db.get(WebDAVProfile,int(profile_id)):
        db.add(WebDAVSyncLog(profile_id=int(profile_id),direction="restore",conflict_decision=strategy,success=True,summary_json={"archive_path":str(payload.get("archive_path"))}));db.commit()
    return result
@router.get("/webdav/logs")
def webdav_logs(db:Session=Depends(get_db))->list[dict[str,Any]]:return [{"id":x.id,"profile_id":x.profile_id,"direction":x.direction,"remote_version":x.remote_version,"checksum":x.checksum,"conflict_decision":x.conflict_decision,"success":x.success,"summary":x.summary_json,"error_message":x.error_message,"created_at":x.created_at} for x in db.scalars(select(WebDAVSyncLog).order_by(WebDAVSyncLog.id.desc())).all()]
