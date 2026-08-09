from __future__ import annotations

import json
import re
from http.cookies import SimpleCookie
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote

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


class BaiduAdapter(CloudAdapter):
    name = "baidu"
    label = "百度网盘"
    root_id = "/"
    user_agent = "pan.baidu.com"

    def __init__(self, credential: str):
        super().__init__(credential)
        payload = credential_payload(credential)
        raw = payload.get("credential", "")
        self.cookie = payload.get("cookie", "") or (raw if "=" in raw else "")
        self.access_token = payload.get("access_token", "") or (raw if raw and "=" not in raw else "")
        if not self.cookie and not self.access_token:
            raise AuthenticationError("百度凭证应为 Access Token 或含 BDUSS 的 Cookie")

    def headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://pan.baidu.com/disk/main",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125 Safari/537.36",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    async def _json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        headers = self.headers()
        headers.update(kwargs.pop("headers", {}))
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.request(method, url, headers=headers, **kwargs)
        response.raise_for_status()
        body = response.json()
        errno = body.get("errno", body.get("error_code", 0))
        if errno not in (None, 0):
            message = body.get("errmsg") or body.get("error_msg") or body.get("error_description") or f"错误码 {errno}"
            if int(errno) in (-6, 111, 401, 403):
                raise AuthenticationError(f"百度授权已失效：{message}")
            raise CloudError(f"百度网盘：{message}")
        return body

    async def probe(self) -> dict[str, Any]:
        if self.access_token:
            body = await self._json(
                "GET", "https://pan.baidu.com/rest/2.0/xpan/nas",
                params={"method": "uinfo", "access_token": self.access_token},
            )
            return {"account": body.get("baidu_name") or body.get("netdisk_name") or "百度账号"}
        body = await self._json(
            "GET", "https://pan.baidu.com/api/gettemplatevariable",
            params={"clienttype": 0, "web": 1, "fields": '["username","uk"]'},
        )
        result = body.get("result") or {}
        if not result.get("uk"):
            raise AuthenticationError("百度 Cookie 已失效，请重新填写")
        return {"account": result.get("username") or f"百度用户 {str(result['uk'])[-4:]}"}

    async def _items(self, path: str) -> list[dict[str, Any]]:
        output, start = [], 0
        while start < 10000:
            if self.access_token:
                body = await self._json(
                    "GET", "https://pan.baidu.com/rest/2.0/xpan/file",
                    params={
                        "method": "list", "dir": path, "start": start, "limit": 1000,
                        "order": "name", "desc": 0, "web": "web", "access_token": self.access_token,
                    },
                )
            else:
                body = await self._json(
                    "GET", "https://pan.baidu.com/api/list",
                    params={"dir": path, "start": start, "num": 1000, "order": "name", "desc": 0, "web": 1},
                )
            items = body.get("list") or []
            output.extend(items)
            if len(items) < 1000:
                break
            start += len(items)
        return output

    async def list_directories(self, parent_id: str, parent_path: str) -> list[FolderEntry]:
        path = parent_path or "/"
        return [
            FolderEntry(str(item.get("path")), str(item.get("server_filename")), str(item.get("path")))
            for item in await self._items(path)
            if int(item.get("isdir") or 0) == 1
        ]

    async def create_folder(self, parent_id: str, parent_path: str, name: str) -> FolderEntry:
        path = join_path(parent_path, name)
        form = {"path": path, "size": "0", "isdir": "1", "rtype": "3", "block_list": "[]"}
        if self.access_token:
            await self._json(
                "POST", "https://pan.baidu.com/rest/2.0/xpan/file",
                params={"method": "create", "access_token": self.access_token}, data=form,
            )
        else:
            await self._json(
                "POST", "https://pan.baidu.com/api/create",
                params={"a": "commit", "channel": "chunlei", "web": 1, "clienttype": 0}, data=form,
            )
        return FolderEntry(path, name, path)

    @staticmethod
    def parse_share(share_url: str, extraction_code: str) -> tuple[str, str, str]:
        match = re.search(r"pan\.baidu\.com/(?:s/|share/init\?surl=)([A-Za-z0-9_-]+)", share_url, re.I)
        if not match:
            raise CloudError("百度分享链接格式不正确")
        feature = match.group(1)
        surl = feature[1:] if feature.startswith("1") else feature
        return feature, surl, extraction_code_from_url(share_url, extraction_code)

    @staticmethod
    def _balanced_json(text: str, marker: str) -> dict[str, Any] | None:
        index = text.find(marker)
        while index >= 0:
            start = text.rfind("{", 0, index)
            while start >= 0:
                depth, quoted, escaped = 0, False, False
                for pos in range(start, len(text)):
                    char = text[pos]
                    if quoted:
                        if escaped:
                            escaped = False
                        elif char == "\\":
                            escaped = True
                        elif char == '"':
                            quoted = False
                        continue
                    if char == '"':
                        quoted = True
                    elif char == "{":
                        depth += 1
                    elif char == "}":
                        depth -= 1
                        if depth == 0:
                            try:
                                candidate = json.loads(text[start:pos + 1])
                            except json.JSONDecodeError:
                                break
                            if isinstance(candidate, dict) and marker.strip('"') in json.dumps(candidate, ensure_ascii=False):
                                return candidate
                            break
                start = text.rfind("{", 0, start)
            index = text.find(marker, index + len(marker))
        return None

    async def _open_share(self, share_url: str, extraction_code: str) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
        feature, surl, code = self.parse_share(share_url, extraction_code)
        cookies = SimpleCookie()
        cookies.load(self.cookie)
        cookie_dict = {key: morsel.value for key, morsel in cookies.items()}
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, cookies=cookie_dict, headers=self.headers()) as client:
            response = await client.get(f"https://pan.baidu.com/s/{feature}")
            response.raise_for_status()
            if code:
                verify = await client.post(
                    "https://pan.baidu.com/share/verify",
                    params={"surl": surl, "channel": "chunlei", "web": 1, "clienttype": 0},
                    data={"pwd": code, "vcode": "", "vcode_str": ""},
                    headers={"Referer": share_url, "X-Requested-With": "XMLHttpRequest"},
                )
                result = verify.json()
                if result.get("errno") not in (0, None):
                    raise CloudError("百度分享提取码不正确")
                response = await client.get(f"https://pan.baidu.com/s/{feature}")
                response.raise_for_status()
            html = response.text
            if "platform-non-found" in html or "error-404" in html:
                raise CloudError("百度分享链接已失效")
            meta = self._balanced_json(html, '"shareid"')
            if not meta:
                raise CloudError("百度分享页暂时无法解析；请确认 Cookie 中包含 BDUSS 和 STOKEN")
            file_list = meta.get("file_list") or meta.get("fileList") or []
            if isinstance(file_list, dict):
                file_list = file_list.get("list") or file_list.get("data") or []
            if not isinstance(file_list, list):
                file_list = []
            return meta, file_list, code

    async def inspect_share(self, share_url: str, extraction_code: str = "") -> ShareInspection:
        meta, items, code = await self._open_share(share_url, extraction_code)
        share_id = str(meta.get("shareid") or meta.get("share_id") or "")
        share_uk = str(meta.get("share_uk") or meta.get("uk") or "")
        if not share_id or not share_uk:
            raise CloudError("百度分享页缺少转存信息")
        files = []
        for item in items:
            name = str(item.get("server_filename") or item.get("name") or "未命名")
            is_dir = int(item.get("isdir") or 0) == 1
            path = str(item.get("path") or f"/{name}")
            files.append(ShareFile(
                id=str(item.get("fs_id") or item.get("fsid") or ""), name=name,
                size=int(item.get("size") or 0), is_dir=is_dir,
                mime_type=str(item.get("mime_type") or ""), path=path,
                browser=browser_support(name, str(item.get("mime_type") or "")),
            ))
        if not files:
            raise CloudError("百度分享中没有可读取的文件")
        return ShareInspection(
            self.name, share_id, files[0].name, code, files,
            {"share_uk": share_uk, "bdstoken": str(meta.get("bdstoken") or "")},
        )

    async def save_share(
        self, inspection: ShareInspection, target_id: str, target_path: str,
        selected_file_ids: list[str], duplicate_policy: str,
    ) -> SaveResult:
        if not self.cookie:
            raise CloudError("百度官方 Access Token 可以浏览目录，但分享转存还需要网页登录 Cookie")
        fresh = await self.inspect_share(
            f"https://pan.baidu.com/share/init?surl={inspection.share_id}", inspection.extraction_code
        ) if not inspection.secret.get("share_uk") else inspection
        selected = [item for item in fresh.files if item.id in set(selected_file_ids)]
        if not selected:
            selected = fresh.files
        bdstoken = fresh.secret.get("bdstoken", "")
        params = {
            "shareid": fresh.share_id, "from": fresh.secret["share_uk"], "bdstoken": bdstoken,
            "channel": "chunlei", "web": 1, "clienttype": 0,
        }
        body = await self._json(
            "POST", "https://pan.baidu.com/share/transfer", params=params,
            data={"fsidlist": json.dumps([int(item.id) for item in selected]), "path": target_path or "/"},
            headers={"Referer": "https://pan.baidu.com/"},
        )
        saved = await self.locate_saved_files(target_id, target_path, [item.name for item in selected])
        duplicate = int(body.get("errno") or 0) in (4, 12)
        return SaveResult("", [item.id for item in saved], saved, duplicate, f"已保存到 {target_path}")

    async def locate_saved_files(self, target_id: str, target_path: str, expected_names: list[str]) -> list[ShareFile]:
        wanted = set(expected_names)
        output = []
        for item in await self._items(target_path or "/"):
            name = str(item.get("server_filename") or "")
            if name not in wanted or int(item.get("isdir") or 0) == 1:
                continue
            output.append(ShareFile(
                id=str(item.get("fs_id") or ""), name=name, size=int(item.get("size") or 0),
                parent_id=target_path, path=str(item.get("path") or join_path(target_path, name)),
                browser=browser_support(name, str(item.get("mime_type") or "")),
            ))
        return output

    async def direct_link(self, file: ShareFile) -> DirectLink:
        if self.access_token:
            body = await self._json(
                "GET", "https://pan.baidu.com/rest/2.0/xpan/multimedia",
                params={
                    "method": "filemetas", "fsids": f"[{file.id}]", "dlink": 1,
                    "access_token": self.access_token,
                },
            )
            items = body.get("list") or []
            if not items or not items[0].get("dlink"):
                raise CloudError("百度没有返回播放地址")
            url = f"{items[0]['dlink']}&access_token={quote(self.access_token)}"
            return DirectLink(url, {"User-Agent": self.user_agent}, file.mime_type or "video/mp4")
        body = await self._json(
            "GET", "https://pan.baidu.com/api/filemetas",
            params={"target": json.dumps([file.path]), "dlink": 1, "web": 5, "origin": "dlna"},
        )
        items = body.get("info") or []
        if not items or not items[0].get("dlink"):
            raise CloudError("百度没有返回播放地址")
        return DirectLink(str(items[0]["dlink"]), {"Cookie": self.cookie, "User-Agent": self.user_agent}, file.mime_type or "video/mp4")

    async def delete(self, file_ids: list[str], file_paths: list[str] | None = None) -> None:
        paths = [path for path in (file_paths or []) if path]
        if not paths:
            return
        filelist = json.dumps(paths, ensure_ascii=False)
        if self.access_token:
            await self._json(
                "POST", "https://pan.baidu.com/rest/2.0/xpan/file",
                params={"method": "filemanager", "opera": "delete", "access_token": self.access_token},
                data={"async": 0, "filelist": filelist},
            )
        else:
            await self._json(
                "POST", "https://pan.baidu.com/api/filemanager",
                params={"opera": "delete", "channel": "chunlei", "web": 1, "clienttype": 0},
                data={"async": 0, "filelist": filelist},
            )
