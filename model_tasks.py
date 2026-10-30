"""Bounded model tasks with persistent checkpoints and exact citation validation."""

import json
import logging
import re
import time
import httpx
from ai_worker import API_KEY, BASE_URL, MODEL
from report_store import encode

PROMPT_BYTES = 24000
PAYLOAD_BYTES = 15500
SOURCE_BYTES = 3000
STATUSES = {"报道", "待核实", "观点"}

class WorkflowError(Exception):
    """Safe, user-facing failure text without upstream response bodies or credentials."""


class OutputLimitError(WorkflowError):
    pass


class CitationError(WorkflowError):
    pass


def short_references(payload):
    """Only the wire payload uses short IDs; checkpoints retain original IDs."""
    ids = []
    for field in ("posts", "sources"):
        ids.extend(item["id"] for item in payload.get(field, []))
    events = list(payload.get("events", []))
    if payload.get("candidate"):
        events.append(payload["candidate"])
    ids.extend(source for event in events for source in event["source_ids"])
    forward = {source: f"S{index + 1}" for index, source in enumerate(dict.fromkeys(ids))}

    def event_ids(event):
        result = {**event, "source_ids": [forward[source] for source in event["source_ids"]]}
        if "timeline" in event:
            result["timeline"] = [{**entry, "source_ids": [forward[s] for s in entry["source_ids"]]}
                                  for entry in event["timeline"]]
        return result

    wire = dict(payload)
    for field in ("posts", "sources"):
        if field in wire:
            wire[field] = [{**item, "id": forward[item["id"]]} for item in wire[field]]
    if "events" in wire:
        wire["events"] = [event_ids(event) for event in wire["events"]]
    if wire.get("candidate"):
        wire["candidate"] = event_ids(wire["candidate"])
    return wire, {short: original for original, short in forward.items()}


def restore_references(value, references):
    """Resolve exact local aliases, rejecting invented, numeric or shortened IDs."""
    if isinstance(value, list):
        return [restore_references(item, references) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for field, item in value.items():
        if field in {"source_ids", "ignored_ids"}:
            if not isinstance(item, list) or any(not isinstance(source, str) or source not in references for source in item):
                raise CitationError("模型返回了无法匹配的来源编号")
            result[field] = [references[source] for source in item]
        else:
            result[field] = restore_references(item, references)
    return result


def byte_len(value):
    return len(encode(value).encode("utf-8"))


def clip(text, limit):
    return str(text).encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def source_fragment(text):
    """Bound the serialized fragment, including escapes, without dropping text."""
    fragment = clip(text, SOURCE_BYTES)
    while byte_len(fragment) > SOURCE_BYTES:
        fragment = fragment[:max(1, len(fragment) * 3 // 4)]
    return fragment


def batches(items, byte_limit=PAYLOAD_BYTES, count_limit=18):
    current = []
    for item in items:
        if byte_len([item]) > byte_limit:
            raise WorkflowError("单条处理材料超过上下文限制")
        if current and (len(current) >= count_limit or byte_len(current + [item]) > byte_limit):
            yield current
            current = []
        current.append(item)
    if current:
        yield current


def text_field(value, name, limit, required=True):
    text = value.get(name)
    if not isinstance(text, str) or (required and not text.strip()) or len(text) > limit:
        raise WorkflowError("模型返回的日报字段不完整，重试可继续已完成的步骤")
    return text.strip()


def citations(value, allowed):
    if not isinstance(value, list) or not value or any(not isinstance(i, str) or i not in allowed for i in value):
        raise CitationError("模型返回了无法匹配的来源编号")
    return list(dict.fromkeys(value))


def event_schema(item, allowed, timeline=False):
    if not isinstance(item, dict):
        raise WorkflowError("模型未返回有效的事件")
    result = {key: text_field(item, key, limit) for key, limit in
              (("title", 100), ("summary", 600), ("category", 24), ("reason", 180))}
    if any(re.search(r"@(?![A-Za-z0-9_])", text) for text in result.values()):
        raise WorkflowError("模型返回的账号署名不完整，正在重试")
    score = item.get("importance")
    if not isinstance(score, int) or isinstance(score, bool) or not 1 <= score <= 5:
        raise WorkflowError("模型返回的事件评分无效")
    result["importance"] = score
    result["status"] = item.get("status") if item.get("status") in STATUSES else "报道"
    result["source_ids"] = citations(item.get("source_ids"), allowed)
    if timeline:
        entries = item.get("timeline")
        if not isinstance(entries, list) or not 1 <= len(entries) <= 4:
            raise WorkflowError("模型未返回有效的事件进展")
        result["timeline"] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise WorkflowError("模型返回的事件进展无效")
            result["timeline"].append({"text": text_field(entry, "text", 240),
                                      "source_ids": citations(entry.get("source_ids"), set(result["source_ids"]))})
    return result


def extract_schema(value, allowed, partial=False):
    if not isinstance(value, dict) or not isinstance(value.get("events"), list):
        raise WorkflowError("模型未返回有效的事件列表")
    events = [event_schema(item, allowed) for item in value["events"]]
    ignored = value.get("ignored_ids", [])
    if not isinstance(ignored, list) or any(not isinstance(i, str) or i not in allowed for i in ignored):
        raise CitationError("模型返回了无法匹配的来源编号")
    covered = set(ignored) | {i for e in events for i in e["source_ids"]}
    if covered != allowed and not partial:
        raise WorkflowError("本批仍有推文未处理，重试可继续已完成的步骤")
    result = {"events": events, "ignored_ids": ignored}
    if partial:
        result["missing_ids"] = sorted(allowed - covered)
    return result


def select_schema(value, allowed, limit):
    if not isinstance(value, dict) or not isinstance(value.get("events"), list):
        raise WorkflowError("模型未返回有效的重点事件列表")
    if not 1 <= len(value["events"]) <= limit:
        raise WorkflowError(f"重点事件数量为 {len(value['events'])}，须保留 1 至 {limit} 个")
    events = [event_schema(item, allowed) for item in value["events"]]
    excess = [f"第 {index + 1} 个事件有 {len(e['source_ids'])} 条" for index, e in enumerate(events) if len(e["source_ids"]) > 5]
    if excess:
        raise WorkflowError("事件来源超限：" + "、".join(excess) + "；每个事件最多引用 5 条来源，请保留最能支持主要事实的来源，并据此调整摘要")
    return {"events": events}


class ModelTasks:
    def __init__(self, store, run_id, client):
        self.store, self.run_id, self.client = store, run_id, client

    def call(self, key, instruction, payload, validate, *, model=MODEL, base_url=BASE_URL, api_key=API_KEY):
        cached = self.store.step(self.run_id, key)
        if cached is not None:
            return validate(cached)
        wire, references = short_references(payload)
        content = encode(wire)
        if references:
            instruction += "\n来源使用本批短编号（如 S1、S2）。source_ids 和 ignored_ids 必须逐字使用输入编号字符串；时间线引用还必须包含在该事件的 source_ids 中。"
        if len((instruction + content).encode("utf-8")) > PROMPT_BYTES:
            raise WorkflowError("处理材料超过上下文限制，请检查分批设置")
        last_error = "模型请求失败"
        messages = [
            {"role": "system", "content": instruction},
            {"role": "user", "content": content}]
        body = {"model": model, "messages": messages,
            "temperature": 0.1, "max_tokens": 7000}
        # DeepSeek defaults to extended reasoning, which consumes the same output
        # budget as the JSON and caused extraction to truncate and repeat batches.
        # Keep reasoning for the final source check; earlier passes are mechanical.
        if "deepseek" in model.lower() and key.startswith(("extract:", "select:", "facts:", "route:", "update:")):
            body["thinking"] = {"type": "disabled"}
        for attempt in range(3):
            started = time.monotonic()
            try:
                response = self.client.post(f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=body, timeout=150)
                if response.is_error:
                    last_error = f"模型接口返回 HTTP {response.status_code}"
                    if response.status_code in (400, 401, 403, 404):
                        raise WorkflowError(last_error)
                else:
                    data = response.json()
                    self.store.usage(self.run_id, data.get("usage") or {})
                    choice = data["choices"][0]
                    if choice.get("finish_reason") == "length":
                        if "deepseek" in model.lower() and "thinking" not in body and attempt < 2:
                            body["thinking"] = {"type": "disabled"}
                            continue
                        raise OutputLimitError("模型输出达到长度限制，请重试")
                    raw = choice["message"]["content"].strip()
                    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I)
                    checked = validate(restore_references(json.loads(raw), references))
                    self.store.save_step(self.run_id, key, checked)
                    logging.info("Model task %s step %s completed in %.1fs", self.run_id, key, time.monotonic() - started)
                    return checked
            except OutputLimitError:
                raise
            except WorkflowError as error:
                last_error = str(error)
                if "HTTP" in last_error:
                    raise
                # A deterministic model needs correction feedback, not the same
                # unchanged prompt three times. Never echo untrusted output.
                feedback = "上次输出未通过校验：" + last_error + "。请修正并重新输出完整 JSON。"
                if references:
                    feedback += "本批允许的来源编号：" + ", ".join(references) + "。时间线引用必须同时列入该事件的 source_ids。"
                if len((instruction + content + feedback).encode("utf-8")) <= PROMPT_BYTES:
                    body["messages"] = messages + [{"role": "user", "content": feedback}]
                if attempt == 2:
                    raise
            except httpx.HTTPError:
                last_error = "连接模型接口超时或网络异常"
            except (ValueError, KeyError, TypeError, IndexError, AttributeError):
                last_error = "模型返回的数据不完整，重试可继续已完成的步骤"
            logging.warning("Model task %s step %s attempt %s: %s", self.run_id, key, attempt + 1, last_error)
            if attempt < 2:
                time.sleep(2 ** (attempt + 1))
        raise WorkflowError(last_error)
