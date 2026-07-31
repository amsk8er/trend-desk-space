# backend/notify/feishu.py
"""节点⑩：把日报 markdown 导入为飞书云文档，返回可点开的文档链接。

走飞书开放平台「素材上传 + 导入任务」四步（应用凭证，后端直调）：
  1. POST /auth/v3/tenant_access_token/internal      取 tenant_access_token
  2. POST /drive/v1/medias/upload_all                上传 .md 素材，拿 file_token
  3. POST /drive/v1/import_tasks                      建导入任务（挂到目标文件夹），拿 ticket
  4. GET  /drive/v1/import_tasks/{ticket}            轮询直到成功，取 result.url

凭证从环境变量读（FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_FOLDER_TOKEN），
由 lifespan 的 load_secrets_env() 注入；故在调用时读，不在 import 期固化。
"""
import asyncio
import json
import os

import httpx

DEFAULT_BASE = "https://open.feishu.cn/open-apis"


class FeishuError(RuntimeError):
    pass


def _data(payload: dict) -> dict:
    """校验飞书统一返回的 code，非 0 抛错；返回 data 段。"""
    if payload.get("code", 0) != 0:
        raise FeishuError(f"feishu code={payload.get('code')} msg={payload.get('msg')!r}")
    return payload.get("data", {})


class FeishuClient:
    def __init__(self, *, app_id: str, app_secret: str, folder_token: str = "",
                 base_url: str = DEFAULT_BASE, transport=None,
                 poll_delay: float = 1.0, poll_tries: int = 60):
        # folder_token 可空：留空 = 挂到应用自己的云空间根目录（mount_key=""），
        # 个人版无法把应用加成文件夹协作者时走这条，不依赖文件夹共享。
        if not (app_id and app_secret):
            raise FeishuError(
                "缺少飞书凭证：请在 secrets.env 设置 "
                "FEISHU_APP_ID / FEISHU_APP_SECRET")
        self.app_id = app_id
        self.app_secret = app_secret
        self.folder_token = folder_token
        self._base = base_url.rstrip("/")
        self._transport = transport
        self._poll_delay = poll_delay
        self._poll_tries = poll_tries

    def _http(self, token: str | None = None) -> httpx.AsyncClient:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return httpx.AsyncClient(base_url=self._base, transport=self._transport,
                                 headers=headers, timeout=30)

    async def _tenant_token(self) -> str:
        async with self._http() as c:
            r = await c.post("/auth/v3/tenant_access_token/internal",
                             json={"app_id": self.app_id, "app_secret": self.app_secret})
            payload = r.json()
            if payload.get("code", 0) != 0:
                raise FeishuError(
                    f"取 tenant_access_token 失败 code={payload.get('code')} "
                    f"msg={payload.get('msg')!r}")
            return payload["tenant_access_token"]

    async def _upload_md(self, token: str, *, filename: str, content: bytes) -> str:
        extra = json.dumps({"obj_type": "docx", "file_extension": "md"})
        async with self._http(token) as c:
            r = await c.post("/drive/v1/medias/upload_all",
                             data={"file_name": filename,
                                   "parent_type": "ccm_import_open",
                                   "size": str(len(content)),
                                   "extra": extra},
                             files={"file": (filename, content, "text/markdown")})
            return _data(r.json())["file_token"]

    async def _create_import_task(self, token: str, *, file_token: str, filename: str) -> str:
        async with self._http(token) as c:
            r = await c.post("/drive/v1/import_tasks",
                             json={"file_extension": "md",
                                   "file_token": file_token,
                                   "type": "docx",
                                   "file_name": filename,
                                   "point": {"mount_type": 1, "mount_key": self.folder_token}})
            return _data(r.json())["ticket"]

    async def _poll(self, token: str, *, ticket: str) -> dict:
        async with self._http(token) as c:
            for _ in range(self._poll_tries):
                r = await c.get(f"/drive/v1/import_tasks/{ticket}")
                result = _data(r.json())["result"]
                status = result.get("job_status")
                if status == 0:
                    return result
                if status in (1, 2):          # 1=初始化 2=处理中，继续轮询
                    await asyncio.sleep(self._poll_delay)
                    continue
                raise FeishuError(
                    f"导入任务失败 job_status={status} "
                    f"msg={result.get('job_error_msg')!r}")
            raise FeishuError(f"导入任务超时（轮询 {self._poll_tries} 次仍未完成）ticket={ticket}")

    async def push(self, *, title: str, markdown: str) -> str:
        content = markdown.encode("utf-8")
        filename = f"{title}.md"
        token = await self._tenant_token()
        file_token = await self._upload_md(token, filename=filename, content=content)
        ticket = await self._create_import_task(token, file_token=file_token, filename=filename)
        result = await self._poll(token, ticket=ticket)
        return result["url"]


async def push_report(*, title: str, markdown: str) -> str:
    """节点⑩调用入口：用环境变量里的应用凭证把日报推成飞书云文档，返回文档 URL。"""
    client = FeishuClient(
        app_id=os.getenv("FEISHU_APP_ID", ""),
        app_secret=os.getenv("FEISHU_APP_SECRET", ""),
        folder_token=os.getenv("FEISHU_FOLDER_TOKEN", ""),
        base_url=os.getenv("FEISHU_BASE", DEFAULT_BASE),
    )
    return await client.push(title=title, markdown=markdown)
