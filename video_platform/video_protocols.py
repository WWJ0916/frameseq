"""Official video APIs only. No generic endpoint, redirects, or POST retries."""

import ipaddress, json, re, socket
from urllib.parse import urlsplit, urljoin
import httpx

PROVIDERS = {
    "minimax": {
        "name": "MiniMax",
        "models": [
            "MiniMax-H3",
            "MiniMax-H3-Highspeed",
            "MiniMax-H3-Lite",
            "MiniMax-H3-Max",
        ],
        "regions": ["cn", "intl"],
        "resolutions": ["768P", "2K", "480P"],
        "min_seconds": 4,
        "max_seconds": 15,
        "docs": "https://platform.minimax.cn/docs/api-reference/video-generation-v2-create",
    },
    "seedance": {
        "name": "火山方舟 Seedance",
        "models": ["doubao-seedance-2-5-260628"],
        "regions": ["cn-beijing"],
        "resolutions": ["480p", "720p", "1080p"],
        "min_seconds": 4,
        "max_seconds": 15,
        "docs": "https://console.volcengine.com/ark/region:cn-beijing/docs/create-video-generation-task-api",
    },
    "wan": {
        "name": "阿里百炼 Wan",
        "models": ["wan3.0-video", "wan3.0-video-prime"],
        "regions": [
            "cn-beijing",
            "ap-southeast-1",
            "ap-northeast-1",
            "eu-central-1",
            "us-east-1",
            "cn-hongkong",
        ],
        "resolutions": ["480P", "720P", "1080P"],
        "min_seconds": 2,
        "max_seconds": 15,
        "docs": "https://help.aliyun.com/en/model-studio/wan3-video-generation-api-reference",
    },
}
RATIOS = ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"]


class ProviderError(RuntimeError):
    def __init__(
        self, message="供应商请求未完成，请核对官方控制台。", *, definite=False
    ):
        super().__init__(message)
        self.definite = definite


def validate_connection(c):
    p = PROVIDERS.get(c["provider"])
    if not p or c["model"] not in p["models"] or c["region"] not in p["regions"]:
        raise ValueError("供应商、模型或区域不匹配。")
    if c["provider"] == "wan" and not re.fullmatch(
        r"[a-zA-Z0-9][a-zA-Z0-9-]{0,63}", c.get("workspace", "")
    ):
        raise ValueError("Wan 3.0需要百炼工作空间ID。")


def parameters(c, prompt, duration, resolution, ratio):
    validate_connection(c)
    p = PROVIDERS[c["provider"]]
    if (
        type(duration) is not int
        or not p["min_seconds"] <= duration <= p["max_seconds"]
    ):
        raise ValueError("时长不在当前平台支持范围内（最多15秒）。")
    if resolution not in p["resolutions"] or ratio not in RATIOS:
        raise ValueError("分辨率或画幅不受支持。")
    if c["provider"] == "minimax":
        allowed = {
            "MiniMax-H3": ["768P", "2K"],
            "MiniMax-H3-Highspeed": ["768P"],
            "MiniMax-H3-Lite": ["480P", "768P"],
            "MiniMax-H3-Max": ["480P", "768P"],
        }[c["model"]]
        if resolution not in allowed or (
            c["model"] == "MiniMax-H3-Max" and duration < 5
        ):
            raise ValueError("所选H3版本不支持该分辨率或时长。")
    return {
        "model_id": c["model"],
        "prompt": prompt,
        "duration": duration,
        "resolution": resolution,
        "ratio": ratio,
        "provider_id": c["provider"],
        "execution": "customer_video_api",
    }


def base_url(c):
    validate_connection(c)
    if c["provider"] == "minimax":
        return (
            "https://api.minimax.cn"
            if c["region"] == "cn"
            else "https://api.minimax.io"
        )
    if c["provider"] == "seedance":
        return "https://ark.cn-beijing.volces.com"
    return f"https://{c['workspace']}.{c['region']}.maas.aliyuncs.com"


class VideoProvider:
    def __init__(self, config, key, transport=None):
        self.config = config
        self.key = key
        self.transport = transport

    def request(self, method, path, body=None):
        headers = {"Authorization": "Bearer " + self.key}
        if self.config["provider"] == "wan":
            headers["X-DashScope-Async"] = "enable"
        try:
            with httpx.Client(
                transport=self.transport,
                timeout=40,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                with client.stream(
                    method, base_url(self.config) + path, headers=headers, json=body
                ) as response:
                    if response.status_code >= 300:
                        raise ProviderError(
                            f"供应商HTTP {response.status_code}，未自动重新提交。",
                            definite=response.status_code
                            in (400, 401, 402, 403, 404, 422, 429),
                        )
                    raw = b""
                    for b in response.iter_bytes():
                        raw += b
                        if len(raw) > 1024**2:
                            raise ProviderError("供应商响应过大，未自动重新提交。")
                    data = json.loads(raw)
                    if not isinstance(data, dict):
                        raise ProviderError()
                    if data.get("base_resp", {}).get("status_code", 0) != 0:
                        raise ProviderError(
                            "供应商拒绝请求，请核对密钥、余额和模型权限。",
                            definite=True,
                        )
                    return data
        except ProviderError:
            raise
        except Exception:
            raise ProviderError(
                "连接或响应异常；提交结果可能不明，不自动重试生成。"
            ) from None

    def submit(self, p):
        provider = self.config["provider"]
        common = {k: p[k] for k in ("duration", "resolution", "ratio")}
        if provider == "wan":
            data = self.request(
                "POST",
                "/api/v1/services/aigc/video-generation/video-synthesis",
                {
                    "model": p["model_id"],
                    "input": {"prompt": p["prompt"]},
                    "parameters": common,
                },
            )
            tid = data.get("output", {}).get("task_id")
        else:
            body = {
                "model": p["model_id"],
                "content": [{"type": "text", "text": p["prompt"]}],
                **common,
            }
            data = self.request(
                "POST",
                (
                    "/v2/video_generation"
                    if provider == "minimax"
                    else "/api/v3/contents/generations/tasks"
                ),
                body,
            )
            tid = data.get("task_id") if provider == "minimax" else data.get("id")
        if not isinstance(tid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", tid):
            raise ProviderError("未取得有效任务ID，禁止自动重新提交。")
        return tid

    def query(self, tid):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", tid):
            raise ProviderError("任务ID格式无效。")
        provider = self.config["provider"]
        path = (
            "/v2/query/video_generation/"
            if provider == "minimax"
            else (
                "/api/v3/contents/generations/tasks/"
                if provider == "seedance"
                else "/api/v1/tasks/"
            )
        ) + tid
        d = self.request("GET", path)
        if provider == "minimax":
            d = d.get("task", {})
            state = d.get("status")
            url = d.get("content", {}).get("url")
        elif provider == "seedance":
            state = d.get("status")
            url = d.get("content", {}).get("video_url")
        else:
            d = d.get("output", {})
            state = d.get("task_status")
            url = d.get("video_url")
        state = str(state).lower()
        state = {"pending": "queued", "canceled": "cancelled"}.get(state, state)
        if state not in (
            "queued",
            "running",
            "succeeded",
            "failed",
            "cancelled",
            "expired",
            "unknown",
        ):
            raise ProviderError("供应商状态未知，保留任务ID。")
        return {"status": state, "url": url if isinstance(url, str) else None}


def safe_result_target(url, provider, resolver=socket.getaddrinfo):
    u = urlsplit(url)
    host = u.hostname or ""
    domains = {
        "minimax": (
            "hailuoai.com",
            "minimax.cn",
            "minimax.io",
            "minimaxi.com",
            "minimax.chat",
        ),
        "seedance": ("volces.com", "volcengine.com", "byteimg.com"),
        "wan": ("aliyuncs.com",),
    }[provider]
    exact = (
        {"algeng-video-infer.oss-cn-shanghai.aliyuncs.com"}
        if provider == "minimax"
        else set()
    )
    if (
        u.scheme != "https"
        or u.username
        or u.password
        or u.port not in (None, 443)
        or not (
            host in exact or any(host == d or host.endswith("." + d) for d in domains)
        )
    ):
        raise ProviderError("结果下载域名未获准，保留任务ID，不重新生成。")
    addresses = {r[4][0] for r in resolver(host, 443, type=socket.SOCK_STREAM)}
    if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
        raise ProviderError("结果地址不是公网地址，已拒绝下载。")
    return host, sorted(addresses)[0]


def download_result(url, path, provider, transport=None, resolver=socket.getaddrinfo):
    """Pin validated public IP, preserve certificate/SNI and Host; no bearer header."""
    part = path.with_suffix(".part")
    size = 0
    try:
        with httpx.Client(
            transport=transport, timeout=90, trust_env=False, follow_redirects=False
        ) as client:
            for _ in range(4):
                host, ip = safe_result_target(url, provider, resolver)
                req = client.build_request(
                    "GET", httpx.URL(url).copy_with(host=ip), headers={"Host": host}
                )
                req.extensions["sni_hostname"] = host
                response = client.send(req, stream=True)
                try:
                    if response.status_code in (301, 302, 303, 307, 308):
                        url = urljoin(url, response.headers.get("location", ""))
                        continue
                    if response.status_code != 200:
                        raise ProviderError("结果下载暂未完成，保留官方任务ID。")
                    with part.open("wb") as output:
                        for block in response.iter_bytes():
                            size += len(block)
                            if size > 128 * 1024**2:
                                raise ProviderError("结果超过128MiB上限。")
                            output.write(block)
                    part.replace(path)
                    return
                finally:
                    response.close()
            raise ProviderError("结果下载跳转过多。")
    except ProviderError:
        raise
    except Exception:
        raise ProviderError("结果下载失败，保留任务ID，不重新生成。") from None
    finally:
        if part.exists():
            part.unlink()
