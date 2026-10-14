from __future__ import annotations

import base64
import json
import mimetypes
import time
import uuid
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import requests


class BailianError(RuntimeError):
    def __init__(self, code: str, message: str, status: int | None = None, request_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.request_id = request_id


class BailianSubmissionUnknown(BailianError):
    """A paid request may have been accepted; submitting it again is unsafe."""

    def __init__(self, message: str, status: int | None = None, request_id: str | None = None):
        super().__init__("submission_unknown", message, status, request_id)


ANALYSIS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title_en", "style_zh", "seasons", "palette", "garments"],
    "properties": {
        "title_en": {"type": "string"},
        "style_zh": {"type": "string"},
        "seasons": {
            "type": "array",
            "items": {"type": "string", "enum": ["spring", "summer", "autumn", "winter"]},
        },
        "palette": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name_zh", "hex"],
                "properties": {"name_zh": {"type": "string"}, "hex": {"type": "string"}},
            },
        },
        "garments": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type", "label_zh", "description_zh"],
                "properties": {
                    "type": {"type": "string", "enum": ["upper", "lower", "dress"]},
                    "label_zh": {"type": "string"},
                    "description_zh": {"type": "string"},
                },
            },
        },
    },
}

GARMENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["type", "label_zh", "description_zh", "seasons"],
    "properties": {
        "type": {"type": "string", "enum": ["upper", "lower", "dress"]},
        "label_zh": {"type": "string"},
        "description_zh": {"type": "string"},
        "seasons": {
            "type": "array",
            "items": {"type": "string", "enum": ["spring", "summer", "autumn", "winter"]},
        },
    },
}


def validate_analysis(value: object) -> dict:
    if not isinstance(value, dict):
        raise BailianError("invalid_analysis", "穿搭分析没有返回 JSON 对象")
    required = ("title_en", "style_zh", "seasons", "palette", "garments")
    if any(key not in value for key in required):
        raise BailianError("invalid_analysis", "穿搭分析缺少必要字段")
    if not isinstance(value["seasons"], list) or not isinstance(value["palette"], list) or not isinstance(value["garments"], list):
        raise BailianError("invalid_analysis", "穿搭分析字段类型不正确")

    seasons = [item for item in value["seasons"] if item in {"spring", "summer", "autumn", "winter"}]
    palette = []
    for item in value["palette"][:6]:
        if not isinstance(item, dict):
            continue
        color = str(item.get("hex", "")).upper()
        if len(color) != 7 or color[0] != "#" or any(ch not in "0123456789ABCDEF" for ch in color[1:]):
            continue
        palette.append({"name_zh": str(item.get("name_zh", "颜色"))[:20], "hex": color})

    garments = []
    seen = set()
    for item in value["garments"]:
        if not isinstance(item, dict) or item.get("type") not in {"upper", "lower", "dress"}:
            continue
        garment_type = item["type"]
        if garment_type in seen:
            continue
        seen.add(garment_type)
        garments.append(
            {
                "type": garment_type,
                "label_zh": str(item.get("label_zh", "单品"))[:40],
                "description_zh": str(item.get("description_zh", ""))[:240],
            }
        )

    if not garments:
        raise BailianError("no_garment", "画面中没有识别到可试穿的上衣、下装或套装/连衣裙")
    return {
        "title_en": str(value["title_en"])[:100],
        "style_zh": str(value["style_zh"])[:80],
        "seasons": seasons,
        "palette": palette,
        "garments": garments,
    }


def validate_garment(value: object) -> dict:
    if not isinstance(value, dict) or value.get("type") not in {"upper", "lower", "dress"}:
        raise BailianError("invalid_analysis", "单品分析没有返回有效的服装类型")
    seasons = value.get("seasons")
    if not isinstance(seasons, list):
        raise BailianError("invalid_analysis", "单品分析没有返回有效的季节")
    return {
        "type": value["type"],
        "label_zh": str(value.get("label_zh") or "单品")[:40],
        "description_zh": str(value.get("description_zh") or "")[:240],
        "seasons": [item for item in seasons if item in {"spring", "summer", "autumn", "winter"}],
    }


class BailianClient:
    API_ROOT = "https://dashscope.aliyuncs.com/api/v1"

    def __init__(self, api_key: str, workspace_id: str, session: requests.Session | None = None):
        self.api_key = api_key.strip()
        self.workspace_id = workspace_id.strip()
        self.session = session or requests.Session()
        self.last_usage: dict = {"availability": "not_reported"}

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.workspace_id)

    def _headers(self, **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", **extra}

    def _request(self, method: str, url: str, *, retries: int = 3, **kwargs) -> requests.Response:
        if not self.configured:
            raise BailianError("not_configured", "尚未配置百炼凭证，请先完成 API 设置")
        method = method.upper()
        # Only reads can be retried. A lost response does not prove that a paid
        # POST was rejected, even if callers request more than one attempt.
        attempts = max(1, retries) if method == "GET" else 1
        if method == "POST":
            kwargs["allow_redirects"] = False
        timeout = kwargs.pop("timeout", 60)
        for attempt in range(attempts):
            try:
                response = self.session.request(method, url, timeout=timeout, **kwargs)
            except requests.RequestException as exc:
                if method == "POST":
                    raise BailianSubmissionUnknown("百炼提交结果待核实，可能已产生费用；不会自动重新提交") from exc
                if attempt + 1 < attempts:
                    time.sleep(2**attempt)
                    continue
                raise BailianError("network_error", f"连接百炼失败：{exc}") from exc
            if response.status_code not in {429} and response.status_code < 500:
                break
            if attempt + 1 < attempts:
                time.sleep(2**attempt)
        request_id = response.headers.get("x-request-id") or response.headers.get("x-dashscope-request-id")
        if method == "POST" and (response.status_code >= 500 or 300 <= response.status_code < 400):
            raise BailianSubmissionUnknown(
                f"百炼提交结果待核实（{response.status_code}），不会自动重新提交",
                response.status_code,
                request_id,
            )
        if response.status_code in {401, 403}:
            raise BailianError("auth_failed", "百炼凭证、地域或业务空间不匹配，请检查 API 设置", response.status_code, request_id)
        if response.status_code == 429:
            raise BailianError("rate_limited", "百炼当前限流，稍后可重试", 429, request_id)
        if response.status_code >= 400:
            try:
                detail = response.json().get("message") or response.json().get("code")
            except (ValueError, AttributeError):
                detail = response.text[:160]
            raise BailianError("remote_error", f"百炼请求失败（{response.status_code}）：{detail or '未知错误'}", response.status_code, request_id)
        return response

    @staticmethod
    def _payload(response: requests.Response, *, submitted: bool = False) -> dict:
        request_id = response.headers.get("x-request-id") or response.headers.get("x-dashscope-request-id")
        try:
            payload = response.json()
        except ValueError as exc:
            if submitted:
                raise BailianSubmissionUnknown("百炼已收到请求但响应无法解析，可能已产生费用；不会自动重新提交", request_id=request_id) from exc
            raise BailianError("invalid_response", "百炼查询响应无法解析", request_id=request_id) from exc
        if not isinstance(payload, dict):
            if submitted:
                raise BailianSubmissionUnknown("百炼返回了无效提交响应，可能已产生费用；不会自动重新提交", request_id=request_id)
            raise BailianError("invalid_response", "百炼查询没有返回有效对象", request_id=request_id)
        return payload

    def _record_usage(self, payload: dict) -> None:
        usage = payload.get("usage")
        self.last_usage = dict(usage) if isinstance(usage, dict) and usage else {"availability": "not_reported"}

    def _analyze_image(
        self,
        image_path: Path,
        prompt: str,
        schema_name: str,
        schema: dict,
        *,
        on_submitting: Callable[[], None] | None = None,
    ) -> object:
        self.last_usage = {"availability": "not_reported"}
        mime = mimetypes.guess_type(image_path.name)[0] or "image/jpeg"
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        url = f"https://{self.workspace_id}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions"
        body = {
            "model": "qwen-vl-plus",
            "messages": [
                {"role": "system", "content": "你是服饰视觉分析器，只输出符合 JSON Schema 的对象。"},
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
                        {"type": "text", "text": prompt},
                    ],
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            },
            "temperature": 0.1,
        }
        if not self.configured:
            raise BailianError("not_configured", "尚未配置百炼凭证，请先完成 API 设置")
        if on_submitting:
            on_submitting()
        response = self._request(
            "POST",
            url,
            headers=self._headers(**{"Content-Type": "application/json"}),
            json=body,
            timeout=120,
        )
        payload = self._payload(response, submitted=True)
        self._record_usage(payload)
        try:
            content = payload["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
            value = json.loads(content)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise BailianError("invalid_analysis", "Qwen 返回了无法解析的服饰信息") from exc
        return value

    def analyze_look(
        self,
        image_path: Path,
        source_kind: str = "photo",
        *,
        on_submitting: Callable[[], None] | None = None,
    ) -> dict:
        subject = {
            "composition": "完整穿搭图，可以是真人穿搭、服装平铺、衣架或商品组合",
            "wardrobe": "衣服图片，可能是单件平铺、衣架或商品图，也可能是人物穿搭照",
        }.get(source_kind, "单人穿搭照片")
        prompt = (
            f"分析这张{subject}。只描述画面中可见的服装，不猜品牌、尺码或无法确定的材质。"
            "识别 upper（上衣）、lower（下装）或 dress（连衣裙或连体式套装）；上下分体时不要输出 dress。"
            "英文标题简短自然，中文风格具体，季节使用 spring/summer/autumn/winter，颜色使用有效 #RRGGBB。"
        )
        return validate_analysis(self._analyze_image(image_path, prompt, "look_analysis", ANALYSIS_SCHEMA, on_submitting=on_submitting))

    def analyze_garment(self, image_path: Path, *, on_submitting: Callable[[], None] | None = None) -> dict:
        prompt = (
            "分析图片中的一件主要服装单品，可为平铺图、衣架图或商品图。只描述可见特征，不猜品牌、"
            "尺码或无法确定的材质。判断为 upper（上装）、lower（下装）或 dress（连衣裙或连体式套装），"
            "并给出适合季节 spring/summer/autumn/winter。若有背景或模特，仍只描述主要服装。"
        )
        return validate_garment(self._analyze_image(image_path, prompt, "garment_analysis", GARMENT_SCHEMA, on_submitting=on_submitting))

    def upload_temp(self, image_path: Path, model: str) -> str:
        response = self._request(
            "GET",
            f"{self.API_ROOT}/uploads",
            params={"action": "getPolicy", "model": model},
            headers=self._headers(),
        )
        policy_response = self._payload(response)
        payload = policy_response.get("data") or policy_response.get("output") or {}
        needed = ("upload_dir", "oss_access_key_id", "signature", "policy", "upload_host")
        if not isinstance(payload, dict) or any(not payload.get(key) for key in needed):
            raise BailianError("upload_policy_invalid", "百炼没有返回完整的临时上传凭证")
        key = f"{payload['upload_dir'].rstrip('/')}/{uuid.uuid4().hex}{image_path.suffix.lower()}"
        fields = {
            "OSSAccessKeyId": payload["oss_access_key_id"],
            "Signature": payload["signature"],
            "policy": payload["policy"],
            "x-oss-object-acl": payload.get("x_oss_object_acl", "private"),
            "x-oss-forbid-overwrite": payload.get("x_oss_forbid_overwrite", "true"),
            "key": key,
            "success_action_status": "200",
        }
        try:
            with image_path.open("rb") as image_file:
                upload = self.session.post(
                    payload["upload_host"],
                    data=fields,
                    files={"file": (image_path.name, image_file, mimetypes.guess_type(image_path.name)[0] or "application/octet-stream")},
                    timeout=120,
                )
        except requests.RequestException as exc:
            raise BailianError("upload_failed", "图片临时上传失败，尚未提交收费模型请求") from exc
        if upload.status_code >= 400:
            raise BailianError("upload_failed", f"图片临时上传失败（{upload.status_code}）", upload.status_code)
        return f"oss://{key}"

    def parse_garments(
        self,
        image_path: Path,
        garment_types: list[str],
        *,
        on_submitting: Callable[[], None] | None = None,
    ) -> list[dict]:
        self.last_usage = {"availability": "not_reported"}
        model = "aitryon-parsing-v1"
        oss_url = self.upload_temp(image_path, model)
        url = f"https://{self.workspace_id}.cn-beijing.maas.aliyuncs.com/api/v1/services/vision/image-process/process"
        if on_submitting:
            on_submitting()
        response = self._request(
            "POST",
            url,
            headers=self._headers(
                **{
                    "Content-Type": "application/json",
                    "X-DashScope-OssResourceResolve": "enable",
                }
            ),
            json={"model": model, "input": {"image_url": oss_url}, "parameters": {"clothes_type": garment_types}},
            timeout=120,
        )
        payload = self._payload(response, submitted=True)
        self._record_usage(payload)
        output = payload.get("output") or {}
        if not isinstance(output, dict):
            raise BailianError("empty_parsing", "百炼没有返回有效的服装分割结果，收费请求已提交")
        parsing_urls = output.get("parsing_img_url") or output.get("parsing_image_url") or []
        crop_urls = output.get("crop_img_url") or output.get("crop_image_url") or []
        bboxes = output.get("bbox") or output.get("bboxes") or []
        if isinstance(parsing_urls, str):
            parsing_urls = [parsing_urls]
        if isinstance(crop_urls, str):
            crop_urls = [crop_urls]
        if isinstance(bboxes, dict):
            bboxes = [bboxes]

        results = []
        if parsing_urls or crop_urls:
            for index, garment_type in enumerate(garment_types):
                results.append(
                    {
                        "type": garment_type,
                        "parsing_url": parsing_urls[index] if index < len(parsing_urls) else None,
                        "crop_url": crop_urls[index] if index < len(crop_urls) else None,
                        "bbox": bboxes[index] if index < len(bboxes) else None,
                    }
                )
        else:
            raw_items = output.get("results") or output.get("result") or []
            if isinstance(raw_items, dict):
                raw_items = [raw_items]
            for index, item in enumerate(raw_items):
                if not isinstance(item, dict):
                    continue
                results.append(
                    {
                        "type": item.get("type") or garment_types[min(index, len(garment_types) - 1)],
                        "parsing_url": item.get("parsing_img_url") or item.get("parsing_image_url"),
                        "crop_url": item.get("crop_img_url") or item.get("crop_image_url"),
                        "bbox": item.get("bbox"),
                    }
                )
        if not results or any(not item.get("crop_url") for item in results):
            raise BailianError("empty_parsing", "没有分割出服装，请手动选择服装类型后重试")
        return results

    def submit_tryon(
        self,
        person_path: Path,
        top_path: Path | None,
        bottom_path: Path | None,
        quality: str,
        *,
        on_submitting: Callable[[], None] | None = None,
    ) -> tuple[str, str | None]:
        self.last_usage = {"availability": "not_reported"}
        model = "aitryon-plus" if quality == "plus" else "aitryon"
        input_data = {"person_image_url": self.upload_temp(person_path, model)}
        if top_path:
            input_data["top_garment_url"] = self.upload_temp(top_path, model)
        if bottom_path:
            input_data["bottom_garment_url"] = self.upload_temp(bottom_path, model)
        if on_submitting:
            on_submitting()
        response = self._request(
            "POST",
            f"{self.API_ROOT}/services/aigc/image2image/image-synthesis",
            headers=self._headers(
                **{
                    "Content-Type": "application/json",
                    "X-DashScope-Async": "enable",
                    "X-DashScope-OssResourceResolve": "enable",
                }
            ),
            json={
                "model": model,
                "input": input_data,
                "parameters": {"resolution": -1, "restore_face": True},
            },
            timeout=120,
        )
        payload = self._payload(response, submitted=True)
        self._record_usage(payload)
        output = payload.get("output")
        task_id = output.get("task_id") if isinstance(output, dict) else None
        request_id = payload.get("request_id") or response.headers.get("x-request-id") or response.headers.get("x-dashscope-request-id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise BailianSubmissionUnknown("百炼没有返回有效试穿任务 ID，提交结果待核实；不会自动重新生成", request_id=request_id)
        return task_id, request_id

    def query_tryon(self, task_id: str) -> dict:
        """Read an existing task without submitting another paid generation."""
        self.last_usage = {"availability": "not_reported"}
        if not isinstance(task_id, str) or not task_id.strip():
            raise BailianError("invalid_task_id", "查询原任务需要有效的任务 ID")
        response = self._request("GET", f"{self.API_ROOT}/tasks/{quote(task_id, safe='')}", headers=self._headers(), retries=3)
        payload = self._payload(response)
        self._record_usage(payload)
        output = payload.get("output") or {}
        request_id = payload.get("request_id") or response.headers.get("x-request-id") or response.headers.get("x-dashscope-request-id")
        if not isinstance(output, dict):
            raise BailianError("invalid_response", "百炼没有返回有效任务状态", request_id=request_id)
        status = str(output.get("task_status") or output.get("status") or "").upper()
        if status not in {"PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELED", "UNKNOWN"}:
            raise BailianError("invalid_response", "百炼没有返回有效任务状态", request_id=request_id)
        return {
            "task_id": task_id,
            "status": status,
            "result_url": self._find_result_url(output) if status == "SUCCEEDED" else None,
            "request_id": request_id,
            "message": str(output.get("message") or payload.get("message") or "")[:240],
        }

    def wait_tryon(self, task_id: str, timeout_seconds: int = 180) -> str:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            task = self.query_tryon(task_id)
            if task["status"] in {"FAILED", "CANCELED"}:
                raise BailianError("tryon_failed", task["message"] or "试穿任务未完成", request_id=task["request_id"])
            if task["status"] == "UNKNOWN":
                raise BailianError("task_unknown", "百炼原任务状态待核实，不会重新生成", request_id=task["request_id"])
            if task["status"] == "SUCCEEDED":
                image_url = task["result_url"]
                if not image_url:
                    raise BailianError("result_missing", "试穿任务完成但没有返回图片")
                return image_url
            time.sleep(3)
        raise BailianError("tryon_timeout", f"试穿等待超过 {timeout_seconds} 秒，原任务仍可继续查询，不会重新生成")

    @staticmethod
    def _find_result_url(value: object) -> str | None:
        if isinstance(value, dict):
            for key in ("image_url", "result_url", "url"):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.startswith("http"):
                    return candidate
            for nested in value.values():
                found = BailianClient._find_result_url(nested)
                if found:
                    return found
        if isinstance(value, list):
            for item in value:
                found = BailianClient._find_result_url(item)
                if found:
                    return found
        return None

    def download(self, url: str, destination: Path) -> None:
        response = self._request("GET", url, headers={}, timeout=120)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(response.content)
