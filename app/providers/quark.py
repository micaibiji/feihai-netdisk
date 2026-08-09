from __future__ import annotations

import asyncio
import random
import re
import time
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


class QuarkAdapter(CloudAdapter):
    name = "quark"
    label = "夸克网盘"
    root_id = "0"
    api = "https://drive-pc.quark.cn/1/clouddrive"
    share_api = "https://drive-h.quark.cn/1/clouddrive"
    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) quark-cloud-drive/2.5.20 Chrome/100 Safari/537.36"
    )

    def __init__(self, credential: str):
        super().__init__(credential)
        payload = credential_payload(credential)
        self.cookie = payload.get("cookie") or payload.get("credential", "")
        if "=" not in self.cookie:
            raise AuthenticationError("夸克凭证应为浏览器 Cookie")

    def headers(self) -> dict[str, str]:
        return {
            "Cookie": self.cookie,
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://pan.quark.cn",
            "Referer": "https://pan.quark.cn/",
            "User-Agent": self.user_agent,
        }

    @staticmethod
    def params(**extra: Any) -> dict[str, Any]:
        return {"pr": "ucpro", "fr": "pc", "uc_param_str": "", **extra}

    async def request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        headers = self.headers()
        headers.update(kwargs.pop("headers", {}))
        params = self.params(**kwargs.pop("params", {}))
        async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
            response = await client.request(method, url, headers=headers, params=params, **kwargs)
        response.raise_for_status()
        body = response.json()
        if body.get("code") not in (None, 0) or body.get("status") not in (None, 200):
            message = body.get("message") or body.get("msg") or f"夸克接口返回 {body.get('code')}"
            if body.get("code") in (401, 403, 32003, 32004):
                raise AuthenticationError(f"夸克授权已失效：{message}")
            raise CloudError(str(message))
        return body

    async def probe(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            response = await client.get(
                "https://pan.quark.cn/account/info",
                headers=self.headers(),
                params={"fr": "pc", "platform": "pc"},
            )
        response.raise_for_status()
        body = response.json()
        if body.get("success") is not True:
            raise AuthenticationError("夸克 Cookie 已失效，请重新填写")
        data = body.get("data") or {}
        return {"account": data.get("nickname") or data.get("mobile") or "夸克账号"}

    async def _items(self, parent_id: str) -> list[dict[str, Any]]:
        page, output = 1, []
        while page <= 20:
            body = await self.request(
                "GET", f"{self.api}/file/sort",
                params={
                    "pdir_fid": parent_id or self.root_id, "_page": page, "_size": 100,
                    "_fetch_total": 1, "_fetch_sub_dirs": 0,
                    "_sort": "file_type:asc,file_name:asc",
                },
            )
            data = body.get("data") or {}
            items = data.get("list") or []
            output.extend(items)
            total = int((body.get("metadata") or {}).get("_total") or data.get("total") or len(output))
            if len(output) >= total or len(items) < 100:
                break
            page += 1
        return output

    async def list_directories(self, parent_id: str, parent_path: str) -> list[FolderEntry]:
        current = parent_id or self.root_id
        return [
            FolderEntry(str(item.get("fid")), str(item.get("file_name")), join_path(parent_path, str(item.get("file_name"))))
            for item in await self._items(current)
            if item.get("dir") is True or int(item.get("file_type", 1)) == 0
        ]

    async def create_folder(self, parent_id: str, parent_path: str, name: str) -> FolderEntry:
        body = await self.request(
            "POST", f"{self.api}/file",
            json={"pdir_fid": parent_id or self.root_id, "file_name": name, "dir_path": "", "dir_init_lock": False},
        )
        data = body.get("data") or {}
        return FolderEntry(str(data.get("fid")), name, join_path(parent_path, name))

    @staticmethod
    def parse_share(share_url: str, extraction_code: str) -> tuple[str, str]:
        match = re.search(r"/s/([A-Za-z0-9_-]+)", share_url)
        if not match:
            raise CloudError("夸克分享链接格式不正确")
        return match.group(1), extraction_code_from_url(share_url, extraction_code)

    async def _share_token(self, share_id: str, code: str) -> str:
        body = await self.request(
            "POST", f"{self.share_api}/share/sharepage/token",
            params={"__dt": random.randint(100, 999), "__t": int(time.time() * 1000)},
            json={"pwd_id": share_id, "passcode": code, "support_visit_limit_private_share": True},
        )
        token = str((body.get("data") or {}).get("stoken") or "")
        if not token:
            raise CloudError("夸克分享链接无法打开或提取码不正确")
        return token

    async def _share_items(self, share_id: str, stoken: str, parent_id: str = "0") -> list[dict[str, Any]]:
        page, output = 1, []
        while page <= 10:
            body = await self.request(
                "GET", f"{self.share_api}/share/sharepage/detail",
                params={
                    "pwd_id": share_id, "stoken": stoken, "pdir_fid": parent_id,
                    "force": 0, "_page": page, "_size": 100, "_fetch_banner": 0,
                    "_fetch_share": 1, "_fetch_total": 1,
                    "_sort": "file_type:asc,file_name:asc", "__t": int(time.time() * 1000),
                },
            )
            data = body.get("data") or {}
            items = data.get("list") or []
            output.extend(items)
            total = int((body.get("metadata") or {}).get("_total") or data.get("total") or len(output))
            if len(output) >= total or len(items) < 100:
                break
            page += 1
        return output

    async def inspect_share(self, share_url: str, extraction_code: str = "") -> ShareInspection:
        share_id, code = self.parse_share(share_url, extraction_code)
        stoken = await self._share_token(share_id, code)
        files: list[ShareFile] = []

        async def walk(parent_id: str, prefix: str, depth: int) -> None:
            if depth > 8 or len(files) >= 500:
                return
            for item in await self._share_items(share_id, stoken, parent_id):
                name = str(item.get("file_name") or "未命名")
                is_dir = bool(item.get("dir")) or int(item.get("file_type", 1)) == 0
                file = ShareFile(
                    id=str(item.get("fid")), name=name, size=int(item.get("size") or 0),
                    is_dir=is_dir, parent_id=parent_id,
                    token=str(item.get("share_fid_token") or ""),
                    mime_type=str(item.get("mime_type") or ""), path=f"{prefix}/{name}".replace("//", "/"),
                    browser=browser_support(name, str(item.get("mime_type") or "")),
                )
                files.append(file)
                if is_dir:
                    await walk(file.id, file.path, depth + 1)

        await walk("0", "", 0)
        if not files:
            raise CloudError("夸克分享中没有可读取的文件")
        title = next((item.name for item in files if item.parent_id == "0"), files[0].name)
        return ShareInspection(self.name, share_id, title, code, files, {"stoken": stoken})

    async def save_share(
        self, inspection: ShareInspection, target_id: str, target_path: str,
        selected_file_ids: list[str], duplicate_policy: str,
    ) -> SaveResult:
        selected = [item for item in inspection.files if item.id in set(selected_file_ids)]
        if not selected:
            selected = [item for item in inspection.files if item.parent_id == "0"]
        if not selected:
            raise CloudError("没有选择需要保存的文件")
        body = await self.request(
            "POST", f"{self.api}/share/sharepage/save",
            params={"__dt": random.randint(100, 999), "__t": int(time.time() * 1000)},
            json={
                "fid_list": [item.id for item in selected],
                "fid_token_list": [item.token for item in selected],
                "to_pdir_fid": target_id or self.root_id,
                "pwd_id": inspection.share_id, "stoken": inspection.secret["stoken"],
                "pdir_fid": "0", "pdir_save_all": False, "exclude_fids": [], "scene": "link",
            },
        )
        data = body.get("data") or {}
        task_id = str(data.get("task_id") or "")
        saved_ids: list[str] = []
        if task_id:
            for retry in range(12):
                task = await self.request(
                    "GET", f"{self.api}/task", params={"task_id": task_id, "retry_index": retry}
                )
                task_data = task.get("data") or {}
                saved_ids = [str(value) for value in ((task_data.get("save_as") or {}).get("save_as_top_fids") or [])]
                if task_data.get("status") or saved_ids:
                    break
                await asyncio.sleep(min(2, 0.3 + retry * 0.15))
        saved_files = await self.locate_saved_files(target_id, target_path, [item.name for item in selected])
        return SaveResult(task_id, saved_ids, saved_files, False, f"已保存到 {target_path}")

    async def locate_saved_files(self, target_id: str, target_path: str, expected_names: list[str]) -> list[ShareFile]:
        wanted = set(expected_names)
        result = []
        for item in await self._items(target_id or self.root_id):
            name = str(item.get("file_name") or "")
            if name not in wanted or item.get("dir"):
                continue
            result.append(ShareFile(
                id=str(item.get("fid")), name=name, size=int(item.get("size") or 0),
                parent_id=target_id or self.root_id, mime_type=str(item.get("mime_type") or ""),
                path=join_path(target_path, name), browser=browser_support(name, str(item.get("mime_type") or "")),
            ))
        return result

    async def direct_link(self, file: ShareFile) -> DirectLink:
        body = await self.request("POST", f"{self.api}/file/download", json={"fids": [file.id]})
        data = body.get("data") or []
        if not data or not data[0].get("download_url"):
            raise CloudError("夸克暂时没有返回可播放直链")
        return DirectLink(str(data[0]["download_url"]), {"Referer": "https://pan.quark.cn/", "User-Agent": self.user_agent}, file.mime_type or "video/mp4")

    async def delete(self, file_ids: list[str], file_paths: list[str] | None = None) -> None:
        if not file_ids:
            return
        await self.request(
            "POST", f"{self.api}/file/delete",
            json={"action_type": 2, "filelist": file_ids, "exclude_fids": []},
        )
