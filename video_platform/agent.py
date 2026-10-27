"""A bounded local LLM assistant with one video-task tool and explicit review."""

from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field
from .inference import write_json
from .store import ROOT, TaskStore

MODEL_ID = "Qwen/Qwen2.5-1.5B-Instruct"
MODEL_DIR = ROOT / "models/Qwen2.5-1.5B-Instruct"
AGENT_DIR = ROOT / "agent_runs"
_lock = threading.Lock()
_engine = None

SYSTEM = """You are a video request assistant, not a video generator. Return exactly one JSON object, no markdown or extra prose.
You have only two tools:
1. create_video_task: arguments contain prompt (concise English visual description preserving the user's subject, action, scene, camera), preset (fast or preview), seed (integer, default 42). Include a short Chinese summary outside arguments.
2. ask_user: arguments contain question (short Chinese clarification). Include a short Chinese summary outside arguments.
Supported: text-to-video only; fast=320x192,17 frames,about 1 second; preview=480x272,33 frames,about 2 seconds. Both 16fps, one GPU. Default fast. Choose preview for a clearer preview or 2-second request. No 4K, long videos, audio, image upload, system commands, or other tools.
If a required duration/resolution is unsupported, or no visual subject/action is specified, use ask_user. Do not silently promise unsupported requirements. Never claim a task was submitted or a video was generated.
Preserve physical actions faithfully; floating or bobbing must not become walking or running. Do not introduce a different time of day. Questions may only clarify a video scene or a supported preset. Never request server addresses, passwords, system commands or access.
Examples:
User: 快速测试，小猫在草地上走，种子7
{"tool":"create_video_task","arguments":{"prompt":"A small cat walks slowly across green grass in soft daylight. A steady camera follows its movement.","preset":"fast","seed":7},"summary":"将小猫在草地行走的需求整理为快速测试任务，种子7。"}
User: 我要4K一分钟视频
{"tool":"ask_user","arguments":{"question":"目前支持约1秒快速验证或约2秒清晰预览。是否改用其中一档，并描述画面主体？"},"summary":"所需规格超出当前平台范围，需要确认。"}
"""


class Busy(RuntimeError):
    pass


class InvalidModelOutput(ValueError):
    pass


class ModelCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: Literal["create_video_task", "ask_user"]
    arguments: dict
    summary: str = Field(min_length=1, max_length=300)


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=500)


def validate_call(text):
    # Never execute model text as code or take arbitrary tool names/paths.
    call = ModelCall.model_validate_json(text.strip())
    if call.tool == "create_video_task":
        from .api import TaskRequest

        args = TaskRequest.model_validate(call.arguments).model_dump()
        # The public API can grow capabilities without widening this tool's authority.
        if args["model"] != "local-wan" or args["preset"] not in {"fast", "preview"}:
            raise ValueError(
                "需求助手仅能提交Wan快速验证或清晰预览任务，请在工作台使用其他能力。"
            )
    else:
        args = Question.model_validate(call.arguments).model_dump()
        if re.search(
            r"密码|服务器地址|系统命令|ssh|password|server address|shell",
            args["question"],
            re.I,
        ):
            raise ValueError("The assistant may only clarify video requirements.")
    return {"tool": call.tool, "arguments": args, "summary": call.summary}


def preflight(request_text):
    """Explicit product rules, labelled separately from LLM inference."""
    if re.search(
        r"(?:调用|执行|运行|操作).{0,16}(?:shell|终端|系统命令|服务器命令|ssh)|(?:execute|run|call).{0,16}(?:shell|system command|ssh)",
        request_text,
        re.I,
    ):
        return {
            "tool": "ask_user",
            "arguments": {
                "question": "本助手只能整理视频画面并提交生成任务。请描述希望生成的主体、动作与场景。"
            },
            "summary": "请求超出视频任务工具的范围，未调用模型或系统工具。",
        }
    unsupported = bool(
        re.search(
            r"(?<![A-Za-z0-9])(?:4k|8k|720p|1080p|2k)(?![A-Za-z0-9])|3840\s*[x×*]|1920\s*[x×*]|分钟|分鍾|\bminutes?\b",
            request_text,
            re.I,
        )
    )
    durations = re.findall(r"(\d+(?:\.\d+)?)\s*(?:秒|seconds?\b)", request_text, re.I)
    unsupported |= any(float(d) not in {1.0, 2.0} for d in durations)
    unsupported |= bool(re.search(r"(?:三|四|五|六|七|八|九|十|百)秒", request_text))
    if unsupported:
        return {
            "tool": "ask_user",
            "arguments": {
                "question": "当前仅支持约1秒的320×192快速验证，或约2秒的480×272清晰预览。是否改用其中一档？请调整上方需求后重新整理。"
            },
            "summary": "平台规格检查发现当前不支持的分辨率或时长，未擅自降低要求。",
        }
    return None


class LocalEngine:
    def __init__(self):
        os.environ["CUDA_VISIBLE_DEVICES"] = ""
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TOKENIZERS_PARALLELISM"] = "false"
        if not (ROOT / "logs/qwen-download-complete.json").exists():
            raise RuntimeError("需求助手模型尚未下载并校验完成。")
        import psutil

        if psutil.virtual_memory().available < 16 * 1024**3:
            raise RuntimeError("当前系统可用内存不足16GiB，暂不加载需求助手。")
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self.torch = torch
        torch.set_num_threads(4)
        torch.set_num_interop_threads(1)
        self.tokenizer = AutoTokenizer.from_pretrained(
            MODEL_DIR, local_files_only=True, trust_remote_code=False
        )
        self.model = (
            AutoModelForCausalLM.from_pretrained(
                MODEL_DIR,
                torch_dtype=torch.float32,
                local_files_only=True,
                trust_remote_code=False,
                attn_implementation="eager",
            )
            .to("cpu")
            .eval()
        )

    def generate(self, messages):
        text = self.tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        inputs = self.tokenizer(text, return_tensors="pt")
        if inputs.input_ids.shape[1] > 1800:
            raise ValueError("需求过长，请缩短描述。")
        with self.torch.inference_mode():
            output = self.model.generate(
                **inputs,
                max_new_tokens=320,
                do_sample=False,
                max_time=60,
                pad_token_id=self.tokenizer.eos_token_id,
            )
        tokens = output[0, inputs.input_ids.shape[1] :]
        return self.tokenizer.decode(tokens, skip_special_tokens=True), len(tokens)


def plan_request(request_text):
    global _engine
    if not _lock.acquire(blocking=False):
        raise Busy("需求助手正在整理另一个请求，请稍后重试。")
    plan_id = uuid.uuid4().hex
    directory = AGENT_DIR / plan_id
    started = time.perf_counter()
    trace = {
        "id": plan_id,
        "status": "planning",
        "request": request_text,
        "model_id": MODEL_ID,
        "model_device": "cpu",
        "model_precision": "float32",
        "cpu_threads": 4,
        "engine_loaded_this_request": _engine is None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "attempts": [],
    }
    try:
        directory.mkdir(parents=True, exist_ok=False)
        guarded = preflight(request_text)
        if guarded is not None:
            trace.update(
                status="needs_clarification",
                decision_source="platform_precheck",
                call=guarded,
                planning_seconds=round(time.perf_counter() - started, 3),
                engine_loaded_this_request=False,
            )
            write_json(directory / "plan.json", trace)
            return public_plan(trace)
        trace["decision_source"] = "local_llm"
        if _engine is None:
            loading_started = time.perf_counter()
            _engine = LocalEngine()
            trace["engine_load_seconds"] = round(
                time.perf_counter() - loading_started, 3
            )
        messages = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": request_text},
        ]
        for attempt in range(2):
            raw, count = _engine.generate(messages)
            trace["attempts"].append({"raw_output": raw, "generated_tokens": count})
            try:
                call = validate_call(raw)
                break
            except (ValueError, TypeError) as error:
                trace["attempts"][-1]["validation_error"] = str(error)
                if attempt:
                    raise InvalidModelOutput(
                        "模型两次输出都未通过结构校验，请缩短需求或手动填写。"
                    ) from error
                messages.extend(
                    [
                        {"role": "assistant", "content": raw},
                        {
                            "role": "user",
                            "content": "Your previous output was invalid. Return ONLY the exact JSON schema from the system message, with a valid tool and arguments. Do not add any other fields or prose.",
                        },
                    ]
                )
        trace.update(
            status=(
                "needs_review"
                if call["tool"] == "create_video_task"
                else "needs_clarification"
            ),
            call=call,
            planning_seconds=round(time.perf_counter() - started, 3),
        )
        write_json(directory / "plan.json", trace)
        return public_plan(trace)
    except Exception as error:
        trace.update(
            status="failed",
            error=f"{type(error).__name__}: {error}",
            planning_seconds=round(time.perf_counter() - started, 3),
        )
        if directory.exists():
            write_json(directory / "plan.json", trace)
        raise
    finally:
        _lock.release()


def public_plan(trace):
    return {
        k: trace[k]
        for k in [
            "id",
            "status",
            "model_id",
            "model_device",
            "decision_source",
            "call",
            "planning_seconds",
        ]
    }


def read_plan(plan_id):
    if not re.fullmatch(r"[0-9a-f]{32}", plan_id):
        raise FileNotFoundError("方案不存在。")
    return json.loads((AGENT_DIR / plan_id / "plan.json").read_text())


def submit_plan(plan_id, store=None):
    # Validate before using the identifier in a path, and serialize retries.
    read_plan(plan_id)
    with (AGENT_DIR / plan_id / "submit.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        return _submit_plan(plan_id, store)


def _submit_plan(plan_id, store=None):
    trace = read_plan(plan_id)
    if preflight(trace.get("request", "")) is not None:
        raise ValueError("原需求超出当前规格或工具范围，请重新整理，未创建任务。")
    if trace["status"] not in {"needs_review", "submitted"}:
        raise ValueError("这个方案尚不能提交，请先补充需求。")
    call = validate_call(json.dumps(trace["call"]))
    if call["tool"] != "create_video_task":
        raise ValueError("方案没有可执行的视频任务工具。")
    from .api import TaskRequest

    parameters = TaskRequest.model_validate(call["arguments"]).parameters()
    task = (store or TaskStore()).create(parameters, key="agent-" + plan_id)
    trace.update(
        status="submitted",
        tool_observation={
            "tool": "create_video_task",
            "task_id": task["id"],
            "status_at_submission": task["status"],
        },
    )
    write_json(AGENT_DIR / plan_id / "plan.json", trace)
    return task
