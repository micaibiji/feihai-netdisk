from __future__ import annotations

import re
from typing import Any

import httpx

from .base import (
    AuthenticationError,
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


class Pan115Adapter(CloudAdapter):
    name = "115"
    label = "115网盘"
    root_id = "0"
    api = "https://webapi.115.com"
    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125 Safari/537.36"
    )

    def __init__(self, credential: str):
        super().__init__(credential)
        payload = credential_payload(credential)
        self.cookie = payload.get("cookie") or payload.get("credential", "")
        if "=" not in self.cookie:
            raise AuthenticationError("115凭证应为扫码结果或网页登录 Cookie")

    def headers(self) -> dict[str, str]:
        return {
            "Cookie": self.cookie,
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://115.com/",
        }

    async def request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        headers = self.headers()
        headers.update(kwargs.pop("headers", {}))
        async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
            response = await client.request(method, url, headers=headers, **kwargs)
        response.raise_for_status()
        body = response.json()
        if body.get("state") is False:
            message = body.get("error") or body.get("error_msg") or body.get("message") or "115请求失败"
            if body.get("errno") in (99, 401, 403) or "登录" in str(message):
                raise AuthenticationError(f"115授权已失效：{message}")
            raise CloudError(str(message))
        return body

    async def probe(self) -> dict[str, Any]:
        body = await self.request("GET", "https://my.115.com/?ct=ajax&ac=get_user_aq")
        data = body.get("data") or {}
        uid = str(data.get("uid") or "")
        if not uid:
            raise AuthenticationError("115授权已失效，请重新扫码")
        return {"account": data.get("user_name") or f"115用户 {uid[-4:]}", "user_id": uid}

    async def _items(self, parent_id: str) -> list[dict[str, Any]]:
        offset, output = 0, []
        while offset < 10000:
            body = await self.request(
                "GET", f"{self.api}/files",
                params={
                    "aid": 1, "cid": parent_id or self.root_id, "offset": offset,
                    "limit": 200, "show_dir": 1, "o": "file_name", "asc": 1,
                    "format": "json", "natsort": 1,
                },
            )
            data = body.get("data") or []
            output.extend(data)
            count = int(body.get("count") or len(output))
            if len(output) >= count or len(data) < 200:
                break
            offset += len(data)
        return output

    @staticmethod
    def _is_dir(item: dict[str, Any]) -> bool:
        return "cid" in item and not item.get("fid")

    @staticmethod
    def _id(item: dict[str, Any]) -> str:
        return str(item.get("fid") or item.get("cid") or "")

    @staticmethod
    def _name(item: dict[str, Any]) -> str:
        return str(item.get("n") or item.get("file_name") or item.get("name") or "未命名")

    async def list_directories(self, parent_id: str, parent_path: str) -> list[FolderEntry]:
        return [
            FolderEntry(self._id(item), self._name(item), join_path(parent_path, self._name(item)))
            for item in await self._items(parent_id or self.root_id)
            if self._is_dir(item)
        ]

    async def create_folder(self, parent_id: str, parent_path: str, name: str) -> FolderEntry:
        body = await self.request(
            "POST", f"{self.api}/files/add",
            data={"pid": parent_id or self.root_id, "cname": name},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        folder_id = str(body.get("cid") or (body.get("data") or {}).get("cid") or "")
        if not folder_id:
            matches = await self.list_directories(parent_id, parent_path)
            found = next((item for item in matches if item.name == name), None)
            if found:
                return found
            raise CloudError("115文件夹创建成功但没有返回目录编号")
        return FolderEntry(folder_id, name, join_path(parent_path, name))

    @staticmethod
    def parse_share(share_url: str, extraction_code: str) -> tuple[str, str]:
        match = re.search(r"(?:115|115cdn|anxia)\.com/s/([A-Za-z0-9]+)", share_url, re.I)
        if not match:
            raise CloudError("115分享链接格式不正确")
        code = extraction_code_from_url(share_url, extraction_code)
        return match.group(1), code

    async def _share_items(self, share_code: str, receive_code: str, parent_id: str = "") -> list[dict[str, Any]]:
        offset, output = 0, []
        while offset < 10000:
            body = await self.request(
                "GET", f"{self.api}/share/snap",
                params={
                    "share_code": share_code, "receive_code": receive_code,
                    "cid": parent_id, "offset": offset, "limit": 200,
                },
            )
            data = body.get("data") or {}
            items = data.get("list") or []
            output.extend(items)
            count = int(data.get("count") or len(output))
            if len(output) >= count or len(items) < 200:
                break
            offset += len(items)
        return output

    async def inspect_share(self, share_url: str, extraction_code: str = "") -> ShareInspection:
        share_code, receive_code = self.parse_share(share_url, extraction_code)
        files: list[ShareFile] = []

        async def walk(parent_id: str, prefix: str, depth: int) -> None:
            if depth > 8 or len(files) >= 500:
                return
            for item in await self._share_items(share_code, receive_code, parent_id):
                name = self._name(item)
                is_dir = self._is_dir(item)
                item_id = self._id(item)
                file = ShareFile(
                    id=item_id, name=name, size=int(item.get("s") or item.get("size") or 0),
                    is_dir=is_dir, parent_id=parent_id, pick_code=str(item.get("pc") or ""),
                    mime_type=str(item.get("m") or ""), path=f"{prefix}/{name}".replace("//", "/"),
                    browser=browser_support(name, str(item.get("m") or "")),
                )
                files.append(file)
                if is_dir:
                    await walk(item_id, file.path, depth + 1)

        await walk("", "", 0)
        if not files:
            raise CloudError("115分享中没有可读取的文件，或提取码不正确")
        title = next((item.name for item in files if not item.parent_id), files[0].name)
        return ShareInspection(self.name, share_code, title, receive_code, files)

    async def save_share(
        self, inspection: ShareInspection, target_id: str, target_path: str,
        selected_file_ids: list[str], duplicate_policy: str,
    ) -> SaveResult:
        selected = [item for item in inspection.files if item.id in set(selected_file_ids)]
        if not selected:
            selected = [item for item in inspection.files if not item.parent_id]
        if not selected:
            raise CloudError("没有选择需要保存的文件")
        account = await self.probe()
        try:
            await self.request(
                "POST", f"{self.api}/share/receive",
                data={
                    "user_id": account["user_id"], "share_code": inspection.share_id,
                    "receive_code": inspection.extraction_code,
                    "file_id": ",".join(item.id for item in selected),
                    "cid": target_id or self.root_id,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            duplicate = False
        except CloudError as error:
            if "无需重复" not in str(error) and "重复" not in str(error):
                raise
            duplicate = True
        saved = await self.locate_saved_files(target_id, target_path, [item.name for item in selected])
        return SaveResult("", [item.id for item in saved], saved, duplicate, f"已保存到 {target_path}")

    async def locate_saved_files(self, target_id: str, target_path: str, expected_names: list[str]) -> list[ShareFile]:
        wanted = set(expected_names)
        output = []
        for item in await self._items(target_id or self.root_id):
            name = self._name(item)
            if name not in wanted or self._is_dir(item):
                continue
            output.append(ShareFile(
                id=self._id(item), name=name, size=int(item.get("s") or 0),
                parent_id=target_id or self.root_id, pick_code=str(item.get("pc") or ""),
                path=join_path(target_path, name), browser=browser_support(name),
            ))
        return output

    async def direct_link(self, file: ShareFile) -> DirectLink:
        if not file.pick_code:
            for item in await self._items(file.parent_id or self.root_id):
                if self._id(item) == file.id:
                    file.pick_code = str(item.get("pc") or "")
                    break
        if not file.pick_code:
            raise CloudError("115没有返回视频播放标识")
        body = await self.request(
            "GET", f"{self.api}/files/download", params={"pickcode": file.pick_code}
        )
        data = body.get("data") or body
        url = data.get("file_url") or data.get("url") or data.get("download_url")
        if isinstance(url, dict):
            url = url.get("url")
        if not url:
            raise CloudError("115暂时没有返回可播放直链")
        return DirectLink(str(url), {"Cookie": self.cookie, "Referer": "https://115.com/", "User-Agent": self.user_agent}, file.mime_type or "video/mp4")

    async def delete(self, file_ids: list[str], file_paths: list[str] | None = None) -> None:
        if not file_ids:
            return
        data = [("fid[]", value) for value in file_ids]
        await self.request(
            "POST", f"{self.api}/rb/delete", data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
