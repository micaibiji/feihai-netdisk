from __future__ import annotations

import base64
import hashlib
import json
import random
import re
import string
from datetime import datetime
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
    root_id = "/"
    fallback_api = "https://personal-kd-njs.yun.139.com/hcy"
    route_api = "https://user-njs.yun.139.com/user/route/qryRoutePolicy"
    user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125 Safari/537.36"

    def __init__(self, credential: str):
        super().__init__(credential)
        payload = credential_payload(credential)
        raw = payload.get("token") or payload.get("credential", "")
        raw = re.sub(r"^\s*authorization\s*:\s*", "", raw, flags=re.I)
        self.token = re.sub(r"^\s*basic\s+", "", raw, flags=re.I).strip()
        if len(self.token) < 12:
            raise AuthenticationError("移动云盘需要填写 Authorization 中 Basic 后面的 Token")
        try:
            decoded = base64.b64decode(self.token + "===").decode("utf-8", "ignore")
        except Exception:  # pragma: no cover - 服务端最终会再次验证
            decoded = ""
        parts = decoded.split(":")
        self.account = payload.get("account") or (parts[1] if len(parts) > 1 else "")
        self.api = self.fallback_api
        self._route_resolved = False

    @staticmethod
    def _md5(value: str) -> str:
        return hashlib.md5(value.encode("utf-8")).hexdigest()

    def _headers(self, body: dict[str, Any] | None = None) -> dict[str, str]:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rand = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(16))
        compact = json.dumps(body or {}, ensure_ascii=False, separators=(",", ":"))
        # 移动云盘网页客户端使用 URL 编码后的请求体字符排序生成摘要。
        sorted_body = "".join(sorted(quote(compact, safe="-_.!~*'()")))
        body_digest = self._md5(base64.b64encode(sorted_body.encode()).decode())
        time_digest = self._md5(timestamp + ":" + rand)
        signature = self._md5(body_digest + time_digest).upper()
        return {
            "Authorization": f"Basic {self.token}",
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": self.user_agent,
            "Origin": "https://yun.139.com",
            "Referer": "https://yun.139.com/w/",
            "Caller": "web",
            "Cms-Device": "default",
            "Mcloud-Channel": "1000101",
            "Mcloud-Client": "10701",
            "Mcloud-Route": "001",
            "Mcloud-Sign": f"{timestamp},{rand},{signature}",
            "Mcloud-Version": "7.14.0",
            "x-DeviceInfo": "||9|7.14.0|chrome|120.0.0.0|||windows 10||zh-CN|||",
            "x-huawei-channelSrc": "10000034",
            "x-inner-ntwk": "2",
            "x-m4c-caller": "PC",
            "x-m4c-src": "10002",
            "x-SvcType": "1",
            "Inner-Hcy-Router-Https": "1",
            "X-Yun-Api-Version": "v1",
            "X-Yun-App-Channel": "10000034",
            "X-Yun-Channel-Source": "10000034",
            "X-Yun-Client-Info": "||9|7.14.0|chrome|120.0.0.0|||windows 10||zh-CN|||dW5kZWZpbmVk||",
            "X-Yun-Module-Type": "100",
            "X-Yun-Svc-Type": "1",
        }

    async def _post(self, path: str, body: dict[str, Any], *, absolute: bool = False) -> dict[str, Any]:
        if not absolute and not self._route_resolved:
            await self._resolve_route()
        compact = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        url = path if absolute else f"{self.api}{path}"
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.post(
                url, content=compact.encode("utf-8"), headers=self._headers(body)
            )
        response.raise_for_status()
        data = response.json()
        success = data.get("success")
        code = str(data.get("code") or data.get("resultCode") or "")
        message = data.get("message") or data.get("msg") or data.get("resultDesc") or code
        # 移动云盘不同入口返回的成功码并不统一；路由接口还会返回
        # “请求成功”一类文字结果。不能仅凭一个非 0 code 把它判为失败。
        known_success_codes = {"0", "200", "0000", "0A000000", "S_OK"}
        success_message = any(marker in str(message) for marker in ("请求成功", "操作成功"))
        failed = success is False or (
            bool(code)
            and code not in known_success_codes
            and success is not True
            and not success_message
        )
        if failed:
            if code in {"401", "403", "9103", "A001"} or "登录" in str(message):
                raise AuthenticationError(f"移动云盘授权已失效：{message}")
            raise CloudError(f"移动云盘：{message}")
        return data

    async def _resolve_route(self) -> None:
        if not self.account:
            raise AuthenticationError("移动云盘 Token 中没有账号信息，请重新复制完整 Authorization")
        body = await self._post(
            self.route_api,
            {
                "userInfo": {"userType": 1, "accountType": 1, "accountName": self.account},
                "modAddrType": 1,
            },
            absolute=True,
        )
        policies = (body.get("data") or {}).get("routePolicyList") or []
        host = next(
            (str(item.get("httpsUrl") or "") for item in policies if item.get("modName") == "personal"),
            "",
        )
        if not host:
            raise CloudError("移动云盘没有返回个人云服务地址，请重新获取 Authorization")
        self.api = host.rstrip("/")
        self._route_resolved = True

    @staticmethod
    def _data(body: dict[str, Any]) -> dict[str, Any]:
        value = body.get("data") or body.get("result") or body
        return value if isinstance(value, dict) else {}

    async def probe(self) -> dict[str, Any]:
        await self._items(self.root_id)
        label = self.account[-4:] if self.account else "已授权"
        return {"account": f"移动云盘 {label}"}

    async def _items(self, parent_id: str) -> list[dict[str, Any]]:
        cursor, output = "", []
        for _ in range(100):
            body = await self._post(
                "/file/list",
                {
                    "imageThumbnailStyleList": ["Small", "Large"],
                    "orderBy": "updated_at",
                    "orderDirection": "DESC",
                    "pageInfo": {"pageCursor": cursor, "pageSize": 100},
                    "parentFileId": parent_id or self.root_id,
                },
            )
            data = self._data(body)
            items = data.get("items") or []
            if not isinstance(items, list):
                items = []
            output.extend(items)
            cursor = str(data.get("nextPageCursor") or "")
            if not cursor:
                break
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
            "/file/create",
            {
                "parentFileId": parent_id or self.root_id,
                "name": name,
                "description": "",
                "type": "folder",
                "fileRenameMode": "force_rename",
            },
        )
        data = self._data(body)
        folder_id = str(data.get("fileId") or data.get("id") or "")
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
        body = await self._post("/file/getDownloadUrl", {"fileId": file.id})
        data = self._data(body)
        url = data.get("cdnUrl") if data.get("cdnSwitch") else None
        url = url or data.get("url") or data.get("downloadUrl") or data.get("contentURL")
        if not url:
            raise CloudError("移动云盘没有返回播放地址")
        return DirectLink(str(url), {"User-Agent": self.user_agent}, file.mime_type or "video/mp4")

    async def delete(self, file_ids: list[str], file_paths: list[str] | None = None) -> None:
        if not file_ids:
            return
        await self._post("/recyclebin/batchTrash", {"fileIds": file_ids})
