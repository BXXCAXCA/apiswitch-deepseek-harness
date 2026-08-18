"""All public gateway endpoints dispatch through CanonicalRequest and one executor."""
from __future__ import annotations

import json
import hashlib
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from apiswitch.api.deps import authenticate_gateway_token, get_db, require_gateway_token
from apiswitch.db.models import ApiToken, ApiTokenUnifiedModel, BatchJob, MediaJob, ProviderInstance, StoredFile, SystemSetting, UnifiedModel, UnifiedModelCandidate, UpstreamModel
from apiswitch.db.session import SessionLocal
from apiswitch.config import settings
from apiswitch.harness_runtime import is_harness_token
from apiswitch.protocols.canonical import CanonicalEvent, CanonicalRequest, CanonicalResponse, ProtocolError, from_anthropic, from_gemini, from_openai_chat, from_openai_responses, from_terminal, reasoning_signature, response_events, to_anthropic_response, to_gemini_response, to_openai_response, to_openai_responses_usage
from apiswitch.routing.engine import structured_error
from apiswitch.routing.executor import execute_request
from apiswitch.routing.model_catalog import model_catalog_metadata

router = APIRouter(tags=["Gateway"])


def _token_model_ids(db:Session,token:ApiToken)->set[int]:
    if settings.plugin_mode and is_harness_token(token):
        return set(db.scalars(select(UnifiedModel.id)).all())
    return set(db.scalars(select(ApiTokenUnifiedModel.unified_model_id).where(ApiTokenUnifiedModel.api_token_id==token.id)).all())


def _model_authorized(db:Session,token:ApiToken,model_name:str)->bool:
    if settings.plugin_mode and is_harness_token(token):
        return db.scalar(select(UnifiedModel.id).where(UnifiedModel.name==model_name)) is not None
    return db.scalar(select(UnifiedModel.id).join(ApiTokenUnifiedModel,ApiTokenUnifiedModel.unified_model_id==UnifiedModel.id).where(ApiTokenUnifiedModel.api_token_id==token.id,UnifiedModel.name==model_name)) is not None


def _model_access_error(db:Session,token:ApiToken,model_name:str)->tuple[int,dict[str,Any]]:
    model_exists=db.scalar(select(UnifiedModel.id).where(UnifiedModel.name==model_name)) is not None
    if not model_exists:
        if settings.plugin_mode and is_harness_token(token):
            allowed=list(db.scalars(select(UnifiedModel.name).order_by(UnifiedModel.name)).all())
        else:
            allowed=list(db.scalars(select(UnifiedModel.name).join(ApiTokenUnifiedModel,ApiTokenUnifiedModel.unified_model_id==UnifiedModel.id).where(ApiTokenUnifiedModel.api_token_id==token.id).order_by(UnifiedModel.name)).all())
        return 404,structured_error("model_not_found","统一模型 ID 不存在；请使用模型列表返回的精确 ID","model_resolution",details={"model":model_name,"allowed_models":allowed})
    return 403,structured_error("model_not_allowed","该 API Token 未获授权使用此统一模型","token_authorization",details={"model":model_name})


def _require_model_authorized(db:Session,token:ApiToken,model_name:str)->None:
    if not _model_authorized(db,token,model_name):
        raise ProtocolError("model_not_allowed","该 API Token 未获授权使用此统一模型","token_authorization",{"model":model_name})


def _callable_unified_models(db:Session,token:ApiToken)->list[UnifiedModel]:
    query=(
        select(UnifiedModel)
        .join(UnifiedModelCandidate,UnifiedModelCandidate.unified_model_id==UnifiedModel.id)
        .join(UpstreamModel,UpstreamModel.id==UnifiedModelCandidate.upstream_model_id)
        .join(ProviderInstance,ProviderInstance.id==UpstreamModel.provider_instance_id)
        .where(
            UnifiedModel.enabled.is_(True),
            UnifiedModelCandidate.enabled.is_(True),
            UpstreamModel.enabled.is_(True),
            UpstreamModel.remote_status!="missing",
            ProviderInstance.enabled.is_(True),
        )
        .distinct()
        .order_by(UnifiedModel.name)
    )
    if not (settings.plugin_mode and is_harness_token(token)):
        query=query.join(ApiTokenUnifiedModel,ApiTokenUnifiedModel.unified_model_id==UnifiedModel.id).where(ApiTokenUnifiedModel.api_token_id==token.id)
    return list(db.scalars(query).all())


@router.get("/v1/models")
async def list_models(db:Session=Depends(get_db),token:ApiToken=Depends(require_gateway_token))->dict[str,Any]:
    """Return callable unified models using the OpenAI model-list shape."""
    rows=[row for row in _callable_unified_models(db,token) if {"openai_chat","openai_responses"}&set(row.enabled_protocols_json or [])]
    data=[]
    for row in rows:
        data.append({
            "id":row.name,
            "name":row.name,
            "object":"model",
            "created":int(row.created_at.timestamp()),
            "owned_by":"apiswitch",
            **model_catalog_metadata(db,row),
        })
    return {"object":"list","data":data}


def _gemini_model_resource(db:Session,row:UnifiedModel)->dict[str,Any]:
    metadata=model_catalog_metadata(db,row)
    return {
        "name":"models/"+row.name,
        "baseModelId":row.name,
        "version":"1",
        "displayName":row.name,
        "description":row.description or "APISwitch unified model",
        "supportedGenerationMethods":["generateContent"],
        "capabilities":metadata["capabilities"],
        "inputModalities":metadata["inputModalities"],
        "outputModalities":metadata["outputModalities"],
        "supportedFeatures":metadata["supported_features"],
    }


def _callable_gemini_models(db:Session,token:ApiToken)->list[UnifiedModel]:
    return [row for row in _callable_unified_models(db,token) if "gemini_v1beta" in (row.enabled_protocols_json or [])]


@router.get("/v1beta/models")
async def list_gemini_models(
    page_size:int=Query(50,alias="pageSize",ge=1,le=1000),
    page_token:str|None=Query(None,alias="pageToken"),
    db:Session=Depends(get_db),
    token:ApiToken=Depends(require_gateway_token),
)->dict[str,Any]:
    """Return callable unified models using Gemini's native Model resource shape."""
    try:offset=int(page_token or "0")
    except ValueError as exc:raise HTTPException(400,detail=structured_error("validation_error","pageToken 无效","model_list")) from exc
    if offset<0:raise HTTPException(400,detail=structured_error("validation_error","pageToken 无效","model_list"))
    rows=_callable_gemini_models(db,token);page=rows[offset:offset+page_size]
    result={"models":[_gemini_model_resource(db,row) for row in page]}
    if offset+len(page)<len(rows):result["nextPageToken"]=str(offset+len(page))
    return result


@router.get("/v1beta/models/{model}")
async def get_gemini_model(model:str,db:Session=Depends(get_db),token:ApiToken=Depends(require_gateway_token))->dict[str,Any]:
    row=next((item for item in _callable_gemini_models(db,token) if item.name==model),None)
    if row is None:raise HTTPException(404,detail=structured_error("model_not_found","统一模型不存在或未启用 Gemini v1beta","model_list",details={"model":model}))
    return _gemini_model_resource(db,row)


def parse_ingress(protocol: str, payload: dict[str, Any], model_override: str | None = None, *, stream_override: bool | None = None) -> CanonicalRequest:
    if protocol == "openai_chat": return from_openai_chat({**payload,"model":model_override or payload.get("model","")})
    if protocol == "openai_responses": return from_openai_responses({**payload,"model":model_override or payload.get("model","")})
    if protocol == "anthropic_messages": return from_anthropic({**payload,"model":model_override or payload.get("model","")})
    if protocol == "gemini_v1beta":
        request=from_gemini(model_override or payload.get("model", ""),payload)
        if stream_override is not None: request.stream=stream_override
        return request
    request=from_terminal(protocol, protocol, {**payload,"model":model_override or payload.get("model","")})
    if stream_override is not None: request.stream=stream_override
    return request


def render_egress(request: CanonicalRequest, upstream: CanonicalResponse, request_id: str) -> dict[str, Any] | Response:
    if upstream.binary is not None:
        return Response(
            content=upstream.binary,
            media_type=upstream.media_type or "application/octet-stream",
            headers={"x-apiswitch-request-id": request_id, "x-apiswitch-model": request.unified_model},
        )
    if request.inbound_protocol == "anthropic_messages": return to_anthropic_response(request,upstream,request_id)
    if request.inbound_protocol == "gemini_v1beta": return to_gemini_response(request,upstream,request_id)
    if request.request_type in {"embeddings","moderations","rerank","search","images","audio","video","music"}:
        if upstream.get("raw"):
            result=dict(upstream["raw"]);result["model"]=request.unified_model;result["apiswitch"]={"request_id":request_id};return result
        return {"id":request_id,"object":request.request_type,"model":request.unified_model,"data":[{"mock":True,"result":upstream["text"]}]}
    if request.inbound_protocol == "openai_responses":
        output=[]
        if upstream.reasoning_content:output.append({"id":"rs_"+request_id,"type":"reasoning","status":"completed","summary":[{"type":"summary_text","text":upstream.reasoning_content}]})
        if upstream.text:output.append({"id":"msg_"+request_id,"type":"message","role":"assistant","status":"completed","content":[{"type":"output_text","text":upstream.text,"annotations":[]}]})
        output.extend({"id":item.get("id") or f"call_{index}","type":"function_call","status":"completed","call_id":item.get("id") or f"call_{index}","name":item.get("name",""),"arguments":json.dumps(item.get("arguments") or {},ensure_ascii=False,separators=(",",":"))} for index,item in enumerate(upstream.tool_calls))
        return {"id":"resp_"+request_id,"object":"response","created_at":int(time.time()),"status":"completed","error":None,"incomplete_details":None,"instructions":request.instructions,"model":request.unified_model,"output":output,"output_text":upstream.text,"parallel_tool_calls":True,"tool_choice":request.tool_choice or "auto","tools":request.tools,"temperature":request.parameters.get("temperature"),"top_p":request.parameters.get("top_p"),"usage":to_openai_responses_usage(upstream.usage),"metadata":{}}
    return to_openai_response(request,upstream,request_id)


def _sse(data: dict[str, Any], event: str | None = None) -> str:
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


def render_sse(request: CanonicalRequest, events: list[CanonicalEvent]) -> list[str]:
    """Map canonical events to the exact event vocabulary of the caller."""
    output: list[str] = []
    if request.inbound_protocol == "openai_chat":
        stream_id = "chatcmpl_" + events[0].request_id
        for event in events:
            delta: dict[str, Any] = {}
            finish_reason = None
            if event.event_type == "start": delta = {"role": "assistant"}
            elif event.event_type=="reasoning_delta":delta={"reasoning_content":event.delta or ""}
            elif event.event_type=="content_delta": delta = {"content": event.delta or ""}
            elif event.event_type=="tool_delta":
                item=event.data or {};delta={"tool_calls":[{"index":event.index,"id":item.get("id") or f"call_{event.index}","type":"function","function":{"name":item.get("name",""),"arguments":json.dumps(item.get("arguments") or {},ensure_ascii=False,separators=(",",":"))}}]}
            elif event.event_type == "completed": finish_reason = "stop"
            output.append(_sse({"id":stream_id,"object":"chat.completion.chunk","model":request.unified_model,"choices":[{"index":0,"delta":delta,"finish_reason":finish_reason}]}))
        output.append("data: [DONE]\n\n")
        return output
    if request.inbound_protocol == "openai_responses":
        response_id = "resp_" + events[0].request_id
        created_at = int(time.time())
        sequence = 0

        def emit(event_type: str, **payload: Any) -> None:
            nonlocal sequence
            output.append(_sse({"type": event_type, "sequence_number": sequence, **payload}, event_type))
            sequence += 1

        emit(
            "response.created",
            response={
                "id": response_id,
                "object": "response",
                "created_at": created_at,
                "status": "in_progress",
                "model": request.unified_model,
                "output": [],
            },
        )
        emit(
            "response.in_progress",
            response={"id": response_id, "object": "response", "created_at": created_at, "status": "in_progress", "model": request.unified_model, "output": []},
        )
        output_index = 0
        for event in events:
            if event.event_type == "start":
                continue
            if event.event_type == "reasoning_delta":
                item_id = "rs_" + event.request_id
                summary = event.delta or ""
                item = {"id": item_id, "type": "reasoning", "status": "in_progress", "summary": []}
                part = {"type": "summary_text", "text": ""}
                emit("response.output_item.added", output_index=output_index, item=item)
                emit("response.reasoning_summary_part.added", item_id=item_id, output_index=output_index, summary_index=0, part=part)
                emit("response.reasoning_summary_text.delta", item_id=item_id, output_index=output_index, summary_index=0, delta=summary)
                emit("response.reasoning_summary_text.done", item_id=item_id, output_index=output_index, summary_index=0, text=summary)
                emit("response.reasoning_summary_part.done", item_id=item_id, output_index=output_index, summary_index=0, part={"type": "summary_text", "text": summary})
                emit("response.output_item.done", output_index=output_index, item={"id": item_id, "type": "reasoning", "status": "completed", "summary": [{"type": "summary_text", "text": summary}]})
                output_index += 1
            elif event.event_type == "content_delta":
                item_id = "msg_" + event.request_id
                text = event.delta or ""
                emit("response.output_item.added", output_index=output_index, item={"id": item_id, "type": "message", "status": "in_progress", "role": "assistant", "content": []})
                emit("response.content_part.added", item_id=item_id, output_index=output_index, content_index=0, part={"type": "output_text", "text": "", "annotations": []})
                emit("response.output_text.delta", item_id=item_id, output_index=output_index, content_index=0, delta=text)
                emit("response.output_text.done", item_id=item_id, output_index=output_index, content_index=0, text=text)
                emit("response.content_part.done", item_id=item_id, output_index=output_index, content_index=0, part={"type": "output_text", "text": text, "annotations": []})
                emit("response.output_item.done", output_index=output_index, item={"id": item_id, "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": text, "annotations": []}]})
                output_index += 1
            elif event.event_type=="tool_delta":
                item=event.data or {};item_id=item.get("id") or f"call_{event.index}";arguments=json.dumps(item.get("arguments") or {},ensure_ascii=False,separators=(",",":"))
                emit("response.output_item.added", output_index=output_index, item={"id": item_id, "type": "function_call", "status": "in_progress", "call_id": item_id, "name": item.get("name", ""), "arguments": ""})
                emit("response.function_call_arguments.delta", item_id=item_id, output_index=output_index, delta=arguments)
                emit("response.function_call_arguments.done", item_id=item_id, output_index=output_index, arguments=arguments)
                emit("response.output_item.done", output_index=output_index, item={"id": item_id, "type": "function_call", "status": "completed", "call_id": item_id, "name": item.get("name", ""), "arguments": arguments})
                output_index += 1
            elif event.event_type == "completed":
                emit("response.completed", response=event.response)
        return output
    if request.inbound_protocol == "anthropic_messages":
        request_id=events[0].request_id
        output.append(_sse({"type":"message_start","message":{"id":"msg_"+request_id,"type":"message","role":"assistant","model":request.unified_model,"content":[],"stop_reason":None,"usage":{"input_tokens":0,"output_tokens":0}}},"message_start"))
        block_index=0
        for event in events:
            if event.event_type=="reasoning_delta":
                reasoning=event.delta or "";signature=reasoning_signature(request_id,reasoning)
                output.append(_sse({"type":"content_block_start","index":block_index,"content_block":{"type":"thinking","thinking":"","signature":""}},"content_block_start"))
                output.append(_sse({"type":"content_block_delta","index":block_index,"delta":{"type":"thinking_delta","thinking":reasoning}},"content_block_delta"))
                output.append(_sse({"type":"content_block_delta","index":block_index,"delta":{"type":"signature_delta","signature":signature}},"content_block_delta"))
                output.append(_sse({"type":"content_block_stop","index":block_index},"content_block_stop"));block_index+=1
            elif event.event_type=="content_delta":
                output.append(_sse({"type":"content_block_start","index":block_index,"content_block":{"type":"text","text":""}},"content_block_start"))
                output.append(_sse({"type":"content_block_delta","index":block_index,"delta":{"type":"text_delta","text":event.delta or ""}},"content_block_delta"))
                output.append(_sse({"type":"content_block_stop","index":block_index},"content_block_stop"));block_index+=1
            elif event.event_type=="tool_delta":
                item=event.data or {};index=block_index
                output.append(_sse({"type":"content_block_start","index":index,"content_block":{"type":"tool_use","id":item.get("id") or f"toolu_{index}","name":item.get("name",""),"input":{}}},"content_block_start"))
                output.append(_sse({"type":"content_block_delta","index":index,"delta":{"type":"input_json_delta","partial_json":json.dumps(item.get("arguments") or {},ensure_ascii=False,separators=(",",":"))}},"content_block_delta"))
                output.append(_sse({"type":"content_block_stop","index":index},"content_block_stop"));block_index+=1
        output.append(_sse({"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":None},"usage":{"output_tokens":0}},"message_delta"))
        output.append(_sse({"type":"message_stop"},"message_stop"))
        return output
    if request.inbound_protocol == "gemini_v1beta":
        for event in events:
            if event.event_type=="reasoning_delta":
                output.append(_sse({"candidates":[{"content":{"role":"model","parts":[{"text":event.delta or "","thought":True}]}}],"responseId":event.request_id}))
            elif event.event_type=="content_delta":
                output.append(_sse({"candidates":[{"content":{"role":"model","parts":[{"text":event.delta}]}}],"responseId":event.request_id}))
            elif event.event_type=="tool_delta":
                item=event.data or {};output.append(_sse({"candidates":[{"content":{"role":"model","parts":[{"functionCall":{"name":item.get("name",""),"args":item.get("arguments") or {}}}]}}],"responseId":event.request_id}))
            elif event.event_type == "completed":
                output.append(_sse({"candidates":[{"content":{"role":"model","parts":[]},"finishReason":"STOP"}],"responseId":event.request_id,"modelVersion":request.unified_model}))
        return output
    raise ProtocolError("protocol_conversion_unsupported",f"{request.inbound_protocol} 不支持事件流转换","protocol_conversion")


def _protocol_error_status(exc:ProtocolError)->int:
    """Expose transient upstream failures without client/model special cases."""
    details=exc.details if isinstance(exc.details,dict) else {};cause=str(details.get("cause") or exc.error_type)
    try:upstream_status=int(details.get("status_code") or 0)
    except (TypeError,ValueError):upstream_status=0
    if upstream_status==429:return 429
    if exc.error_type=="all_upstream_candidates_failed":
        statuses={int(value) for value in details.get("upstream_statuses",[]) if str(value).isdigit()}
        if 429 in statuses:return 429
        if 408 in statuses:return 504
        return 502
    if cause=="provider_timeout":return 504
    if cause=="provider_unavailable":return 503
    if cause=="upstream_http_error" and upstream_status>=500:return 502
    return 400


async def _execute(
    protocol: str,
    payload: dict[str, Any],
    db: Session,
    token: ApiToken,
    model: str | None = None,
    *,
    stream_override: bool | None = None,
):
    try:
        canonical=parse_ingress(protocol,payload,model,stream_override=stream_override)
        if not _model_authorized(db,token,canonical.unified_model):
            status_code,error=_model_access_error(db,token,canonical.unified_model)
            return JSONResponse(status_code=status_code,content=error)
        metadata,upstream=await execute_request(db,canonical,token.id); result=render_egress(canonical,upstream,metadata["request_id"])
        if canonical.stream:
            events=response_events(upstream,metadata["request_id"],result)
            return StreamingResponse(iter(render_sse(canonical,events)),media_type="text/event-stream")
        return result
    except ProtocolError as exc:
        return JSONResponse(status_code=_protocol_error_status(exc),content=structured_error(exc.error_type,str(exc),exc.stage,details=exc.details))


@router.post("/v1/chat/completions")
async def chat(payload:dict[str,Any],db:Session=Depends(get_db),token:ApiToken=Depends(require_gateway_token)): return await _execute("openai_chat",payload,db,token)
@router.post("/v1/responses")
async def responses(payload:dict[str,Any],db:Session=Depends(get_db),token:ApiToken=Depends(require_gateway_token)): return await _execute("openai_responses",payload,db,token)
@router.post("/v1/messages")
async def messages(payload:dict[str,Any],db:Session=Depends(get_db),token:ApiToken=Depends(require_gateway_token)): return await _execute("anthropic_messages",payload,db,token)
@router.post("/v1beta/models/{model}:generateContent")
async def gemini(model:str,payload:dict[str,Any],db:Session=Depends(get_db),token:ApiToken=Depends(require_gateway_token)): return await _execute("gemini_v1beta",payload,db,token,model)


@router.post("/v1beta/models/{model}:streamGenerateContent")
async def gemini_stream(model:str,payload:dict[str,Any],db:Session=Depends(get_db),token:ApiToken=Depends(require_gateway_token)):
    return await _execute("gemini_v1beta",payload,db,token,model,stream_override=True)

async def _terminal_payload(request: Request, operation: str) -> dict[str, Any]:
    """Accept the native JSON or multipart shape used by the public endpoint."""
    content_type = request.headers.get("content-type", "").lower()
    if not content_type.startswith("multipart/form-data"):
        try:
            payload = await request.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(
                400,
                detail=structured_error("validation_error", "请求正文必须是有效 JSON 或 multipart/form-data", "protocol_ingress"),
            ) from exc
        if not isinstance(payload, dict):
            raise HTTPException(400, detail=structured_error("validation_error", "请求正文必须是对象", "protocol_ingress"))
        return {**payload, "_apiswitch_operation": operation}

    form = await request.form()
    payload: dict[str, Any] = {"_apiswitch_operation": operation}
    multipart: list[dict[str, Any]] = []
    for key, value in form.multi_items():
        if hasattr(value, "read") and hasattr(value, "filename"):
            content = await value.read()
            multipart.append({
                "field": key,
                "filename": value.filename or key,
                "content_type": value.content_type or "application/octet-stream",
                "content": content,
            })
        elif key in payload:
            existing = payload[key]
            payload[key] = [*existing, str(value)] if isinstance(existing, list) else [existing, str(value)]
        else:
            payload[key] = str(value)
    payload["_apiswitch_multipart"] = multipart
    return payload


for _path, _protocol, _operation in [
    ("/v1/embeddings", "embeddings", "embeddings"),
    ("/v1/images/generations", "images", "images_generations"),
    ("/v1/images/edits", "images", "images_edits"),
    ("/v1/images/variations", "images", "images_variations"),
    ("/v1/audio/speech", "audio", "audio_speech"),
    ("/v1/audio/transcriptions", "audio", "audio_transcriptions"),
    ("/v1/moderations", "moderations", "moderations"),
    ("/v1/rerank", "rerank", "rerank"),
    ("/v1/search", "search", "search"),
]:
    async def terminal(
        request: Request,
        db: Session = Depends(get_db),
        token: ApiToken = Depends(require_gateway_token),
        _protocol: str = _protocol,
        _operation: str = _operation,
    ):
        return await _execute(_protocol, await _terminal_payload(request, _operation), db, token)

    router.add_api_route(_path, terminal, methods=["POST"])

@router.post("/v1/files")
async def create_file(file: UploadFile = File(...), purpose: str = "assistants", db: Session = Depends(get_db), token: ApiToken = Depends(require_gateway_token)):
    content = await file.read()
    configured=db.get(SystemSetting,"upload_limit_bytes")
    upload_limit=int(configured.value_json) if configured and configured.value_json else settings.file_max_upload_bytes
    if len(content) > upload_limit:
        raise HTTPException(413, detail=structured_error("validation_error", "文件超过上传限制", "file_validation"))
    from pathlib import Path
    root = Path(settings.file_storage_dir); root.mkdir(parents=True, exist_ok=True)
    file_id = f"file_{uuid.uuid4().hex}"; target = root / file_id
    target.write_bytes(content)
    row = StoredFile(id=file_id, api_token_id=token.id, filename=file.filename or file_id, purpose=purpose, mime_type=file.content_type, byte_size=len(content), sha256=hashlib.sha256(content).hexdigest(), storage_path=str(target))
    db.add(row); db.commit()
    return {"id": file_id, "object": "file", "filename": row.filename, "purpose": purpose, "bytes": len(content), "status": row.status}

@router.get("/v1/files/{file_id}")
async def get_file(file_id: str, db: Session = Depends(get_db), token: ApiToken = Depends(require_gateway_token)):
    row = db.get(StoredFile, file_id)
    if not row or row.api_token_id != token.id: raise HTTPException(404, detail=structured_error("validation_error", "文件不存在", "file_lookup"))
    return {"id": row.id, "object": "file", "filename": row.filename, "purpose": row.purpose, "bytes": row.byte_size, "status": row.status}


@router.get("/v1/files")
async def list_files(db: Session = Depends(get_db), token: ApiToken = Depends(require_gateway_token)):
    rows = list(db.scalars(select(StoredFile).where(StoredFile.api_token_id == token.id).order_by(StoredFile.created_at.desc())).all())
    return {"object": "list", "data": [{"id": row.id, "object": "file", "filename": row.filename, "purpose": row.purpose, "bytes": row.byte_size, "status": row.status} for row in rows]}


@router.get("/v1/files/{file_id}/content")
async def get_file_content(file_id: str, db: Session = Depends(get_db), token: ApiToken = Depends(require_gateway_token)):
    row = db.get(StoredFile, file_id)
    if not row or row.api_token_id != token.id: raise HTTPException(404, detail=structured_error("validation_error", "文件不存在", "file_lookup"))
    from pathlib import Path
    path = Path(row.storage_path)
    if not path.is_file(): raise HTTPException(404, detail=structured_error("validation_error", "文件内容不存在", "file_lookup"))
    return FileResponse(path, media_type=row.mime_type or "application/octet-stream", filename=row.filename)


@router.delete("/v1/files/{file_id}")
async def delete_file(file_id: str, db: Session = Depends(get_db), token: ApiToken = Depends(require_gateway_token)):
    row = db.get(StoredFile, file_id)
    if not row or row.api_token_id != token.id: raise HTTPException(404, detail=structured_error("validation_error", "文件不存在", "file_lookup"))
    referenced = db.scalar(select(BatchJob.id).where(
        (BatchJob.input_file_id == file_id)
        | (BatchJob.output_file_id == file_id)
        | (BatchJob.error_file_id == file_id)
    ))
    if referenced:
        raise HTTPException(409, detail=structured_error("resource_in_use", "文件正被批处理任务引用，不能删除", "file_delete", details={"batch_id": referenced}))
    from pathlib import Path
    path = Path(row.storage_path)
    db.delete(row)
    db.commit()
    path.unlink(missing_ok=True)
    return {"id": file_id, "object": "file", "deleted": True}

@router.post("/v1/batches")
async def create_batch(payload: dict[str, Any], db: Session = Depends(get_db), token: ApiToken = Depends(require_gateway_token)):
    from pathlib import Path
    input_file=db.get(StoredFile,str(payload.get("input_file_id","")))
    if not input_file or input_file.api_token_id!=token.id:raise HTTPException(400,detail=structured_error("validation_error","input_file_id 必须引用当前 Token 上传的 JSONL 文件","batch_validation"))
    endpoint=str(payload.get("endpoint") or "/v1/chat/completions")
    protocols={"/v1/chat/completions":"openai_chat","/v1/responses":"openai_responses","/v1/messages":"anthropic_messages"}
    if endpoint not in protocols:raise HTTPException(400,detail=structured_error("protocol_conversion_unsupported","批处理 endpoint 不受支持","batch_validation",details={"endpoint":endpoint}))
    try:
        lines=[json.loads(line) for line in Path(input_file.storage_path).read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError,UnicodeError,json.JSONDecodeError) as exc:raise HTTPException(400,detail=structured_error("validation_error","批处理输入不是有效 UTF-8 JSONL","batch_validation")) from exc
    if not lines:raise HTTPException(400,detail=structured_error("validation_error","批处理输入不能为空","batch_validation"))
    outputs=[];completed=failed=0
    for index,item in enumerate(lines):
        custom_id=str(item.get("custom_id") or f"request-{index}") if isinstance(item,dict) else f"request-{index}"
        body=item.get("body",item) if isinstance(item,dict) else {}
        item_endpoint=str(item.get("url") or endpoint) if isinstance(item,dict) else endpoint
        try:
            protocol=protocols[item_endpoint];canonical=parse_ingress(protocol,body);_require_model_authorized(db,token,canonical.unified_model);metadata,upstream=await execute_request(db,canonical,token.id)
            outputs.append({"id":f"batch_req_{uuid.uuid4().hex}","custom_id":custom_id,"response":{"status_code":200,"request_id":metadata["request_id"],"body":render_egress(canonical,upstream,metadata["request_id"])},"error":None});completed+=1
        except (KeyError,ProtocolError) as exc:
            error=structured_error(getattr(exc,"error_type","protocol_conversion_unsupported"),str(exc),getattr(exc,"stage","batch_item"))
            outputs.append({"id":f"batch_req_{uuid.uuid4().hex}","custom_id":custom_id,"response":None,"error":error["error"]});failed+=1
    encoded=("\n".join(json.dumps(item,ensure_ascii=False,separators=(",",":")) for item in outputs)+"\n").encode("utf-8")
    root=Path(settings.file_storage_dir);root.mkdir(parents=True,exist_ok=True);output_id=f"file_{uuid.uuid4().hex}";target=root/output_id;target.write_bytes(encoded)
    output_file=StoredFile(id=output_id,api_token_id=token.id,filename=f"{output_id}.jsonl",purpose="batch_output",mime_type="application/jsonl",byte_size=len(encoded),sha256=hashlib.sha256(encoded).hexdigest(),storage_path=str(target));db.add(output_file)
    job_id=f"batch_{uuid.uuid4().hex}";counts={"total":len(lines),"completed":completed,"failed":failed}
    db.add(BatchJob(id=job_id,api_token_id=token.id,input_file_id=input_file.id,endpoint=endpoint,status="completed" if failed==0 else "completed_with_errors",request_counts_json=counts,output_file_id=output_id));db.commit()
    return {"id":job_id,"object":"batch","status":"completed" if failed==0 else "completed_with_errors","endpoint":endpoint,"request_counts":counts,"output_file_id":output_id}

@router.get("/v1/batches/{batch_id}")
async def get_batch(batch_id: str, db: Session = Depends(get_db), _: ApiToken = Depends(require_gateway_token)):
    row = db.get(BatchJob, batch_id)
    if not row: raise HTTPException(404, detail=structured_error("validation_error", "批处理不存在", "batch_lookup"))
    if row.api_token_id!=_.id:raise HTTPException(404,detail=structured_error("validation_error","批处理不存在","batch_lookup"))
    return {"id": row.id, "object": "batch", "status": row.status, "endpoint": row.endpoint, "request_counts": row.request_counts_json,"output_file_id":row.output_file_id,"error_file_id":row.error_file_id}


async def _create_media_job(media_type:str,payload:dict[str,Any],db:Session,token:ApiToken)->dict[str,Any]:
    result=await _execute(media_type,payload,db,token)
    if isinstance(result,JSONResponse) and result.status_code>=400:return result
    job_id=f"{media_type}_{uuid.uuid4().hex}"
    row=MediaJob(id=job_id,api_token_id=token.id,media_type=media_type,unified_model=str(payload.get("model","")),status="completed",result_json=result if isinstance(result,dict) else {"streamed":True})
    db.add(row);db.commit()
    return {"id":job_id,"object":media_type,"status":"completed","model":row.unified_model,"result":row.result_json}


@router.post("/v1/videos/generations")
async def create_video(payload:dict[str,Any],db:Session=Depends(get_db),token:ApiToken=Depends(require_gateway_token)):return await _create_media_job("video",payload,db,token)


@router.get("/v1/videos/{job_id}")
async def get_video(job_id:str,db:Session=Depends(get_db),token:ApiToken=Depends(require_gateway_token)):
    row=db.get(MediaJob,job_id)
    if not row or row.media_type!="video" or row.api_token_id!=token.id:raise HTTPException(404,detail=structured_error("validation_error","视频任务不存在","media_lookup"))
    return {"id":row.id,"object":"video","status":row.status,"model":row.unified_model,"result":row.result_json,"error":row.error_json}


@router.post("/v1/music/generations")
async def create_music(payload:dict[str,Any],db:Session=Depends(get_db),token:ApiToken=Depends(require_gateway_token)):return await _create_media_job("music",payload,db,token)


@router.get("/v1/music/{job_id}")
async def get_music(job_id:str,db:Session=Depends(get_db),token:ApiToken=Depends(require_gateway_token)):
    row=db.get(MediaJob,job_id)
    if not row or row.media_type!="music" or row.api_token_id!=token.id:raise HTTPException(404,detail=structured_error("validation_error","音乐任务不存在","media_lookup"))
    return {"id":row.id,"object":"music","status":row.status,"model":row.unified_model,"result":row.result_json,"error":row.error_json}

@router.websocket("/v1/ws/chat/completions")
async def websocket_chat(websocket: WebSocket):
    await websocket.accept()
    close_code=1000
    with SessionLocal() as db:
        try:
            api_token=authenticate_gateway_token(
                db,
                websocket.headers.get("authorization"),
                websocket.headers.get("x-api-key"),
                websocket.headers.get("x-goog-api-key"),
            )
            payload = await websocket.receive_json()
            canonical = parse_ingress("openai_chat", payload)
            _require_model_authorized(db,api_token,canonical.unified_model)
            metadata, upstream = await execute_request(db, canonical, api_token.id)
            response=render_egress(canonical,upstream,metadata["request_id"])
            for event in response_events(upstream,metadata["request_id"],response):
                frame={"event":event.event_type,"request_id":event.request_id}
                if event.delta is not None:frame["delta"]=event.delta
                if event.response is not None:frame["response"]=event.response
                if event.data is not None:frame["data"]=event.data
                await websocket.send_json(frame)
        except HTTPException as exc:
            detail=exc.detail if isinstance(exc.detail,dict) else {"type":"authentication_error","message":str(exc.detail)}
            await websocket.send_json({"error":{**detail,"stage":"authentication"}});close_code=1008
        except ProtocolError as exc:
            await websocket.send_json(structured_error(exc.error_type, str(exc), exc.stage, details=exc.details))
        finally:
            await websocket.close(code=close_code)
