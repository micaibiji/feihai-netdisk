# 飞海网盘：飞牛 NAS 可视化安装教程

本教程面向飞牛 fnOS 用户，不要求使用命令行。完成后会得到三个服务：飞海网盘管理页、Telegram/网盘聚合搜索服务、本机 OpenList 网盘网关。

## 先看完整流程

```mermaid
flowchart LR
    A[下载私人仓库压缩包] --> B[文件管理中解压]
    B --> C[准备 .env 和三个数据目录]
    C --> D[Docker · Compose · 新增项目]
    D --> E[等待三个容器运行]
    E --> F[打开飞海网盘并完成网盘授权]
    F --> G[飞牛影视添加 STRM 目录]
```

预计首次安装用时 10～20 分钟，镜像下载速度取决于 NAS 的网络环境。

## 一、安装前准备

确认飞牛 NAS 已安装并开启 Docker。准备以下内容：

- 一个只在局域网使用的管理账号与至少 16 位强密码。
- NAS 的局域网 IP，例如 `192.168.1.20`。
- 可用端口 `12366` 和 `5244`。
- 如需海报与最新影视榜单，准备 TMDB API Key。
- 如需 Telegram 通知，准备机器人 Token 和 Chat ID。

目录关系如下：

```text
飞海网盘/
└─ program/       ← Compose 项目目录
   ├─ app/        ← 程序文件
   ├─ data/       ← 数据库、加密密钥、任务记录
   ├─ strm/       ← 交给飞牛影视扫描
   ├─ openlist/   ← 115、夸克、移动等扫码授权数据
   ├─ .env
   └─ docker-compose.yml
```

`program/data`、`program/strm` 和 `program/openlist` 都是 NAS 上的真实目录。重新构建容器不会删除这些目录。

## 二、下载并解压程序

1. 在 GitHub 私人仓库打开飞海网盘项目。
2. 点击 **Code → Download ZIP**。
3. 打开飞牛桌面的 **文件管理**。
4. 在 Docker 共享目录中新建 `飞海网盘` 文件夹。
5. 上传 ZIP，右键选择 **解压到当前目录**。
6. 把解压得到的项目文件夹改名为 `program`。

完成后，`program` 内应能看到：

```text
app/
Dockerfile
docker-compose.yml
.env.example
requirements.txt
```

## 三、填写配置文件

1. 在 `program` 中复制 `.env.example`。
2. 把副本重命名为 `.env`；如果文件管理隐藏点文件，可先在电脑上改名再上传。
3. 用文本编辑器打开 `.env`，至少修改下面四项：

```dotenv
ADMIN_USERNAME=admin
ADMIN_PASSWORD=换成你自己的至少16位强密码
OPENLIST_URL=http://你的NAS局域网IP:5244
PUBLIC_BASE_URL=http://你的NAS局域网IP:12366
```

目录使用相对路径即可：

```dotenv
FEIHAI_DATA_PATH=./data
FNTV_STRM_PATH=./strm
OPENLIST_DATA_PATH=./openlist
```

可选功能按需填写：

```dotenv
TMDB_API_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
BAIDU_CLIENT_ID=
BAIDU_REDIRECT_URI=
```

不使用的项目留空，不要删除等号。不要把填写后的 `.env` 上传到 GitHub。

## 四、用飞牛界面创建 Compose 项目

操作路径：

```text
飞牛桌面 → Docker → Compose → 新增项目
```

在创建窗口中依次设置：

| 项目 | 应填写内容 |
|---|---|
| 项目名称 | `feihai-netdisk` |
| 存储位置 | 选择上一步的 `飞海网盘/program` |
| Compose 文件 | 选择目录中的 `docker-compose.yml` |
| 文件夹权限 | 允许项目目录读写 |

点击 **创建/构建** 后保持窗口打开。正常日志顺序大致如下：

```text
openlist Pulling
feihai-drive Built
feihai-pansou Started
feihai-openlist Started
feihai-drive Started
Exited: 0
```

看到 `Exited: 0` 表示构建流程成功结束，不代表服务退出。回到 Compose 卡片应显示 **正在运行**，容器数量为 **3**。

## 五、第一次打开与验收

在同一局域网的浏览器打开：

- 飞海网盘：`http://你的NAS局域网IP:12366`
- OpenList 授权网关：`http://你的NAS局域网IP:5244`

打开地址后会显示飞海网盘中文登录页，账号密码就是 `.env` 中的 `ADMIN_USERNAME` 和 `ADMIN_PASSWORD`。

进入首页后按以下顺序检查：

1. 首页能看到影视榜单；没填 TMDB Key 时会显示“连接 TMDB”，不会显示任何虚构排名或演示海报。
2. **资源搜索** 输入片名后能显示 Telegram/网盘结果。
3. **网盘账号** 页面按顺序显示 115、百度、夸克、中国移动云盘。
4. **风控中心** 点击检测后会为四个网盘生成检测结果。
5. **系统设置** 可以保存通知与整理选项。

健康检查地址为 `http://你的NAS局域网IP:12366/api/health`。正常结果中应包含：

```json
{"status":"ok","name":"飞海网盘","version":"0.4.0","database":true,"strm_writable":true}
```

## 六、网盘授权

在飞海网盘左侧进入 **网盘账号**：

1. 115 点击 **立即登录** 后，二维码会直接显示在飞海网盘当前页面；使用 115 手机客户端扫码并确认。
2. 页面会依次显示等待扫码、已扫码、成功、过期或取消；二维码过期后点击 **刷新二维码**。
3. 百度、夸克和中国移动云盘只有在对应的真实授权适配完成后才会启用按钮；当前版本不会用占位按钮假装登录成功。
4. 授权完成后回到账号页，再运行一次风控检测。
5. 页面只显示脱敏账号信息；凭证由 NAS 本机密钥加密保存，不要求普通用户粘贴 Token 或 Cookie。

四个网盘相互独立，飞海网盘不会进行跨盘秒传。

## 七、接入飞牛影视

1. 打开 **飞牛影视**。
2. 新建或编辑影视库。
3. 把 `飞海网盘/strm` 添加为媒体目录。
4. 电影库选择电影类型，电视剧库选择电视剧类型；也可以分别添加 `strm/电影` 和 `strm/电视剧`。
5. 执行一次全量扫描。

飞海网盘采用统一命名：

```text
电影/片名 (年份)/片名 (年份).strm
电视剧/剧名 (年份)/Season 01/剧名 (年份) - S01E01.strm
```

## 八、更新版本

1. 先备份 `data`、`strm`、`openlist` 和 `.env`。
2. 下载新版 ZIP，只覆盖 `program` 中的程序文件；保留原 `.env`。
3. 在 Docker → Compose 的项目菜单中选择 **清理**，然后重新 **启动/构建**。
4. 清理只用于重建容器；确认三个数据目录是外部映射后再操作。
5. 打开 `/api/health` 检查版本号、数据库和 STRM 写入状态。

## 九、常见问题

### 页面打不开

- 确认 Compose 显示三个容器正在运行。
- 确认 12366、5244 未被其他程序占用。
- 使用 NAS 局域网 IP，不要填写 `localhost`。

### 登录一直失败

- 账号密码取自 `.env`，修改后必须重新构建容器。
- 密码包含特殊符号时不要在等号两侧加空格。

### 搜索有结果但无法保存

- 搜索与网盘授权是两套状态；先完成对应网盘授权。
- 风控页面出现异常时，系统会保留已有来源并暂停高频操作，不会绕过验证码或限制。

### 飞牛影视看不到新内容

- 确认影视库指向宿主机的 `strm` 目录。
- 在飞海网盘先查看统一命名预览，再让飞牛影视重新扫描。
- STRM 只是播放指向文件，真正播放仍依赖对应网盘账号和有效链接。

## 十、卸载与备份

停止或删除 Compose 项目前，先备份：

```text
.env
data/
strm/
openlist/
```

只删除容器不会影响外部目录；手动删除这些目录会丢失数据库、授权配置或已生成的 STRM。建议先通过飞牛文件管理移动到回收站，确认备份可用后再彻底删除。
