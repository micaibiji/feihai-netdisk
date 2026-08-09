from __future__ import annotations

import base64
import hashlib
import json
import random
import string
import time
from typing import Any
from urllib.parse import quote

import httpx

from .base import (
    AuthenticationError,
    CapabilityError,
    CloudAdapter,
    CloudError,
    DirectLink,
    FolderEntry,
    SaveResult,
    ShareFile,
    ShareInspection,
    browser_support,
    credential_payload,
    join_path,
)


class MobileAdapter(CloudAdapter):
    """中国移动云盘个人云适配器。

    当前凭证格式与移动云盘网页端的 Basic token 一致。目录、建目录、直链和
    删除均走个人云接口；分享链接的同盘转存只有在服务端开放相应能力时才启用，
    绝不通过下载再上传来伪装成“同盘转存”。
    """

    name = "china_mobile"
    label = "中国移动云盘"
    root_id = "root"
    api = "https://personal-kd-njs.yun.139.com"
    user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125 Safari/537.36"

    def __init__(self, credential: str):
        super().__init__(credential)
        payload = credential_payload(credential)
        raw = payload.get("token") or payload.get("credential", "")
        self.token = raw.removeprefix("Basic ").strip()
        if len(self.token) < 12:
            raise AuthenticationError("移动云盘需要填写网页端 Basic Token")
        try:
            decoded = base64.b64decode(self.token + "===").decode("utf-8", "ignore")
        except Exception:  # pragma: no cover - 服务端最终会再次验证
            decoded = ""
        parts = decoded.split(":")
        self.account = payload.get("account") or (parts[1] if len(parts) > 1 else "")

    @staticmethod
    def _md5(value: str) -> str:
        return hashlib.md5(value.encode("utf-8")).hexdigest()

    def _headers(self, body: dict[str, Any] | None = None) -> dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        rand = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(16))
        compact = json.dumps(body or {}, ensure_ascii=False, separators=(",", ":"))
        # 移动云盘网页客户端使用 URL 编码后的请求体字符排序生成摘要。
        sorted_body = "".join(sorted(quote(compact, safe="")))
        body_digest = self._md5(base64.b64encode(sorted_body.encode()).decode())
        time_digest = self._md5(timestamp + rand)
        signature = self._md5(body_digest + time_digest).upper()
        return {
            "Authorization": f"Basic {self.token}",
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": self.user_agent,
            "x-requested-with": "XMLHttpRequest",
            "x-yun-timestamp": timestamp,
            "x-yun-random": rand,
            "x-yun-signature": signature,
        }

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.post(
                f"{self.api}{path}", json=body, headers=self._headers(body)
            )
        response.raise_for_status()
        data = response.json()
        success = data.get("success")
        code = str(data.get("code") or data.get("resultCode") or "")
        if success is False or (code and code not in {"0", "200", "S_OK"}):
            message = data.get("message") or data.get("msg") or data.get("resultDesc") or code
            if code in {"401", "403", "9103", "A001"} or "登录" in str(message):
                raise AuthenticationError(f"移动云盘授权已失效：{message}")
            raise CloudError(f"移动云盘：{message}")
        return data

    @staticmethod
    def _data(body: dict[str, Any]) -> dict[str, Any]:
        value = body.get("data") or body.get("result") or body
        return value if isinstance(value, dict) else {}

    async def probe(self) -> dict[str, Any]:
        await self._items(self.root_id)
        label = self.account[-4:] if self.account else "已授权"
        return {"account": f"移动云盘 {label}"}

    async def _items(self, parent_id: str) -> list[dict[str, Any]]:
        page, output = 1, []
        while page <= 100:
            body = await self._post(
                "/hcy/file/list",
                {
                    "catalogID": parent_id or self.root_id,
                    "pageInfo": {"pageNum": page, "pageSize": 200},
                    "sortDirection": 1,
                    "sortType": 0,
                },
            )
            data = self._data(body)
            items = data.get("list") or data.get("content") or data.get("fileList") or []
            if not isinstance(items, list):
                items = []
            output.extend(items)
            total = int(data.get("total") or data.get("totalCount") or len(output))
            if len(output) >= total or len(items) < 200:
                break
            page += 1
        return output

    @staticmethod
    def _id(item: dict[str, Any]) -> str:
        return str(item.get("fileId") or item.get("fileID") or item.get("catalogID") or item.get("id") or "")

    @staticmethod
    def _name(item: dict[str, Any]) -> str:
        return str(item.get("fileName") or item.get("name") or item.get("catalogName") or "未命名")

    @staticmethod
    def _is_dir(item: dict[str, Any]) -> bool:
        value = item.get("fileType", item.get("type", item.get("isFolder")))
        if isinstance(value, bool):
            return value
        return str(value).lower() in {"0", "folder", "catalog", "dir"}

    async def list_directories(self, parent_id: str, parent_path: str) -> list[FolderEntry]:
        return [
            FolderEntry(self._id(item), self._name(item), join_path(parent_path, self._name(item)))
            for item in await self._items(parent_id or self.root_id)
            if self._is_dir(item)
        ]

    async def create_folder(self, parent_id: str, parent_path: str, name: str) -> FolderEntry:
        body = await self._post(
            "/hcy/file/create",
            {"parentCatalogID": parent_id or self.root_id, "catalogName": name},
        )
        data = self._data(body)
        folder_id = str(data.get("catalogID") or data.get("fileId") or data.get("id") or "")
        if not folder_id:
            folder = next(
                (item for item in await self.list_directories(parent_id, parent_path) if item.name == name),
                None,
            )
            if folder:
                return folder
            raise CloudError("移动云盘已请求创建目录，但没有返回目录编号")
        return FolderEntry(folder_id, name, join_path(parent_path, name))

    async def inspect_share(self, share_url: str, extraction_code: str = "") -> ShareInspection:
        raise CapabilityError("移动云盘分享解析接口尚未通过当前账号实测；链接仍可复制")

    async def save_share(
        self,
        inspection: ShareInspection,
        target_id: str,
        target_path: str,
        selected_file_ids: list[str],
        duplicate_policy: str,
    ) -> SaveResult:
        raise CapabilityError("当前移动云盘账号未提供可靠的同盘分享转存接口")

    async def locate_saved_files(
        self, target_id: str, target_path: str, expected_names: list[str]
    ) -> list[ShareFile]:
        wanted = set(expected_names)
        result: list[ShareFile] = []
        for item in await self._items(target_id or self.root_id):
            name = self._name(item)
            if name not in wanted or self._is_dir(item):
                continue
            result.append(
                ShareFile(
                    id=self._id(item),
                    name=name,
                    size=int(item.get("fileSize") or item.get("size") or 0),
                    parent_id=target_id or self.root_id,
                    mime_type=str(item.get("mimeType") or ""),
                    path=join_path(target_path, name),
                    browser=browser_support(name, str(item.get("mimeType") or "")),
                )
            )
        return result

    async def direct_link(self, file: ShareFile) -> DirectLink:
        body = await self._post("/hcy/file/getDownloadUrl", {"fileId": file.id})
        data = self._data(body)
        url = data.get("downloadUrl") or data.get("url") or data.get("contentURL")
        if not url:
            raise CloudError("移动云盘没有返回播放地址")
        return DirectLink(str(url), {"User-Agent": self.user_agent}, file.mime_type or "video/mp4")

    async def delete(self, file_ids: list[str], file_paths: list[str] | None = None) -> None:
        if not file_ids:
            return
        await self._post("/hcy/recyclebin/batchTrash", {"fileIds": file_ids})
