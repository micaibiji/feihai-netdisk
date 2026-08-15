from __future__ import annotations

import base64
import asyncio
import hashlib
import json
import os
import random
import re
import string
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

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
    extraction_code_from_url,
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
    share_api = "https://share-kd-njs.yun.139.com/yun-share"
    share_key = b"PVGDwmcvfs1uV3d1"

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

    @staticmethod
    def parse_share(share_url: str, extraction_code: str = "") -> tuple[str, str]:
        parsed = urlparse(share_url)
        query = parse_qs(parsed.query)
        code = extraction_code_from_url(share_url, extraction_code)
        link_id = str(
            (query.get("linkID") or query.get("linkId") or query.get("id") or [""])[0]
        ).strip()
        if not link_id and parsed.query and "=" not in parsed.query:
            link_id = parsed.query.split("&", 1)[0].strip()
        if not link_id:
            fragment = unquote(parsed.fragment or "")
            match = re.search(r"/(?:m|w)/(?:i|s)/([A-Za-z0-9_-]+)", fragment, re.I)
            if match:
                link_id = match.group(1)
        if not link_id:
            match = re.search(
                r"(?:yun|caiyun)\.139\.com/(?:share/|m/i\??|w/i/)([A-Za-z0-9_-]+)",
                share_url,
                re.I,
            )
            if match:
                link_id = match.group(1)
        link_id = link_id.split("#", 1)[0].split("?", 1)[0].strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,}", link_id):
            raise CloudError("移动云盘分享链接格式不正确")
        return link_id, code

    def _share_headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
            "X-Deviceinfo": "||9|12.27.0|firefox|140.0|||windows 10|1920X1080|zh-CN|||",
            "hcy-cool-flag": "1",
            "CMS-DEVICE": "default",
            "x-m4c-caller": "PC",
            "X-Yun-Api-Version": "v1",
            "Origin": "https://yun.139.com",
            "Referer": "https://yun.139.com/",
        }
        if self.token:
            headers["Authorization"] = f"Basic {self.token}"
        return headers

    @classmethod
    def _share_encrypt(cls, body: dict[str, Any]) -> str:
        plaintext = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        iv = os.urandom(16)
        padder = padding.PKCS7(128).padder()
        padded = padder.update(plaintext) + padder.finalize()
        encryptor = Cipher(algorithms.AES(cls.share_key), modes.CBC(iv)).encryptor()
        encrypted = encryptor.update(padded) + encryptor.finalize()
        return base64.b64encode(iv + encrypted).decode()

    @classmethod
    def _share_decrypt(cls, content: bytes) -> dict[str, Any]:
        stripped = content.strip()
        if stripped.startswith(b"{"):
            value = json.loads(stripped)
            return value if isinstance(value, dict) else {}
        decoded = base64.b64decode(stripped)
        if len(decoded) < 32:
            raise CloudError("移动云盘分享接口返回了无法识别的数据")
        decryptor = Cipher(algorithms.AES(cls.share_key), modes.CBC(decoded[:16])).decryptor()
        padded = decryptor.update(decoded[16:]) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        plaintext = unpadder.update(padded) + unpadder.finalize()
        value = json.loads(plaintext)
        return value if isinstance(value, dict) else {}

    async def _share_post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.post(
                f"{self.share_api}{path}",
                content=self._share_encrypt(body).encode(),
                headers=self._share_headers(),
            )
        response.raise_for_status()
        try:
            value = self._share_decrypt(response.content)
        except (ValueError, TypeError) as error:
            raise CloudError("移动云盘分享接口响应无法解析") from error
        code = str(value.get("resultCode") or value.get("code") or value.get("status") or "0")
        success = value.get("success")
        if success is False or code not in {"", "0", "0000", "SUC0000", "200"}:
            message = value.get("resultDesc") or value.get("message") or value.get("msg") or code
            if code in {"401", "403", "9103", "A001"} or "登录" in str(message):
                raise AuthenticationError(f"移动云盘授权已失效：{message}")
            raise CloudError(f"移动云盘分享：{message}")
        return value

    @staticmethod
    def _share_data(body: dict[str, Any]) -> dict[str, Any]:
        value = body.get("data") or body.get("result") or body
        return value if isinstance(value, dict) else {}

    async def _share_items(
        self, link_id: str, password: str, parent_id: str = "root"
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        body = await self._share_post(
            "/richlifeApp/devapp/IOutLink/getOutLinkInfoV6",
            {
                "getOutLinkInfoReq": {
                    "account": self.account,
                    "linkID": link_id,
                    "passwd": password,
                    "pCaID": parent_id or "root",
                }
            },
        )
        data = self._share_data(body)
        catalogs = data.get("caLst") or []
        contents = data.get("coLst") or []
        return data, catalogs if isinstance(catalogs, list) else [], contents if isinstance(contents, list) else []

    async def inspect_share(self, share_url: str, extraction_code: str = "") -> ShareInspection:
        link_id, password = self.parse_share(share_url, extraction_code)
        files: list[ShareFile] = []
        root_data: dict[str, Any] = {}

        async def walk(parent_id: str, prefix: str, depth: int) -> None:
            nonlocal root_data
            if depth > 10 or len(files) >= 1000:
                return
            data, catalogs, contents = await self._share_items(link_id, password, parent_id)
            if depth == 0:
                root_data = data
            for item in catalogs:
                file_id = str(
                    item.get("caID")
                    or item.get("caId")
                    or item.get("catalogID")
                    or item.get("catalogId")
                    or item.get("id")
                    or ""
                )
                name = str(item.get("caName") or item.get("catalogName") or item.get("name") or "未命名")
                display_path = join_path(prefix or "/", name)
                source_path = str(
                    item.get("path")
                    or item.get("caPath")
                    or item.get("catalogPath")
                    or display_path
                )
                files.append(
                    ShareFile(
                        id=file_id,
                        name=name,
                        is_dir=True,
                        parent_id=parent_id,
                        token=source_path,
                        path=display_path,
                        browser=browser_support(name),
                    )
                )
                if file_id:
                    await walk(file_id, display_path, depth + 1)
            for item in contents:
                file_id = str(
                    item.get("coID")
                    or item.get("coId")
                    or item.get("contentID")
                    or item.get("contentId")
                    or item.get("id")
                    or ""
                )
                name = str(item.get("coName") or item.get("contentName") or item.get("name") or "未命名")
                suffix = str(item.get("coSuffix") or item.get("suffix") or "").lstrip(".")
                if suffix and not PurePosixPath(name).suffix:
                    name = f"{name}.{suffix}"
                mime_type = str(item.get("mimeType") or item.get("contentTypeName") or "")
                display_path = join_path(prefix or "/", name)
                source_path = str(
                    item.get("path")
                    or item.get("coPath")
                    or item.get("contentPath")
                    or display_path
                )
                files.append(
                    ShareFile(
                        id=file_id,
                        name=name,
                        size=int(item.get("coSize") or item.get("contentSize") or item.get("size") or 0),
                        parent_id=parent_id,
                        token=source_path,
                        mime_type=mime_type,
                        path=display_path,
                        browser=browser_support(name, mime_type),
                    )
                )

        await walk("root", "/", 0)
        if not files:
            raise CloudError("移动云盘分享中没有可读取的文件")
        title = str(root_data.get("lkName") or root_data.get("linkName") or "").strip()
        if not title:
            title = next((item.name for item in files if item.parent_id == "root"), files[0].name)
        return ShareInspection(
            self.name,
            link_id,
            title,
            password,
            files,
            {"owner_account": str(root_data.get("ownerAccount") or root_data.get("account") or "")},
        )

    async def save_share(
        self,
        inspection: ShareInspection,
        target_id: str,
        target_path: str,
        selected_file_ids: list[str],
        duplicate_policy: str,
    ) -> SaveResult:
        selected_ids = set(selected_file_ids)
        selected = [item for item in inspection.files if item.id in selected_ids]
        if not selected:
            selected = [item for item in inspection.files if item.parent_id == "root"]
        if not selected:
            raise CloudError("没有选择需要保存的移动云盘文件")
        content_paths = [item.token or item.path or item.id for item in selected if not item.is_dir]
        catalog_paths = [item.token or item.path or item.id for item in selected if item.is_dir]
        folder_name = PurePosixPath(target_path or "/").name or "全部"
        request_body = {
            "createOuterLinkBatchOprTaskReq": {
                "msisdn": self.account,
                "ownerAccount": inspection.secret.get("owner_account", ""),
                "taskType": 1,
                "taskInfo": {
                    "linkID": inspection.share_id,
                    "contentInfoList": content_paths,
                    "catalogInfoList": catalog_paths,
                    "newCatalogID": target_id or self.root_id,
                    "newCatalogName": folder_name,
                    "needPassword": bool(inspection.extraction_code),
                },
                "linkID": inspection.share_id,
                "needPassword": bool(inspection.extraction_code),
            }
        }
        response: dict[str, Any] | None = None
        last_error: Exception | None = None
        for endpoint in (
            "/richlifeApp/devapp/IBatchOprTask/createOuterLinkBatchOprTaskV2",
            "/richlifeApp/devapp/IBatchOprTask/createOuterLinkBatchOprTask",
        ):
            try:
                response = await self._share_post(endpoint, request_body)
                break
            except (CloudError, httpx.HTTPError) as error:
                last_error = error
        if response is None:
            raise CloudError(f"移动云盘同盘保存失败：{last_error}")
        data = self._share_data(response)
        create_result = data.get("createBatchOprTaskRes") or data.get("createOuterLinkBatchOprTaskRes") or {}
        task_id = str(
            data.get("taskID")
            or data.get("taskId")
            or (create_result.get("taskID") if isinstance(create_result, dict) else "")
            or (create_result.get("taskId") if isinstance(create_result, dict) else "")
            or ""
        )
        if task_id:
            for _ in range(10):
                await asyncio.sleep(0.8)
                try:
                    task = await self._share_post(
                        "/richlifeApp/devapp/IBatchOprTask/queryBatchOprTaskDetailV3",
                        {"queryBatchOprTaskDetailReq": {"taskID": task_id, "msisdn": self.account}},
                    )
                except (CloudError, httpx.HTTPError):
                    continue
                task_data = self._share_data(task)
                detail = task_data.get("queryBatchOprTaskDetailRes") or task_data
                batch = detail.get("batchOprTask") if isinstance(detail, dict) else {}
                status = str((batch or {}).get("taskStatus") or (batch or {}).get("status") or "")
                if status.lower() in {"2", "success", "succeed", "finished", "failed"}:
                    break
        saved_files = await self.locate_saved_files(
            target_id, target_path, [item.name for item in selected if not item.is_dir]
        )
        return SaveResult(
            task_id,
            [item.id for item in saved_files],
            saved_files,
            False,
            f"已保存到 {target_path}",
        )

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
