# 贡献指南

感谢你愿意改进这个项目。为了不把「能收能发」这条命根子弄坏，下面几条请务必先读。

## 开发环境

```powershell
py -3.12 -m venv .venv-wechat
.\.venv-wechat\Scripts\pip install -r requirements.wechat.txt -r requirements.gui.txt
```

- 目标平台：Windows 10 1903+ / Windows 11（64 位），微信 4.1.x 桌面版。
- 发送能力需要管理员权限，开发时请用 `以管理员身份启动控制台.bat` 启动。
- 不要修改 `.venv-wechat/Lib/site-packages/wechatauto`：那是第三方依赖，改它等于让别人的环境和你的不一致。

## 跑测试

```powershell
.\.venv-wechat\Scripts\python -m unittest discover -s tests
```

- 单文件：`python -m unittest discover -s tests -p 'test_xxx.py'`
- 不要用 `-m unittest tests.xxx`（会报 `ModuleNotFoundError: test_wechat_app_flow`）。
- 提交前至少跑一遍全量；改动涉及发送链路时，请补一条能证明「被改掉的校验真的不能删」的用例。

## 代码约定

- 注释与提示语用中文，面向「看不懂代码的用户」也能读懂。
- **发送链路一律 fail-closed**：拿不准是不是当前会话、拿不准有没有草稿，就停下来报错，不许猜。
- 不新增「主动发消息」类功能（群发、加好友、朋友圈等），这个项目刻意不做。
- 新增网络端点必须挂在 `create_app()` 里，并且有 `require_bound()` 之类的绑定校验；
  后端只监听 `127.0.0.1`，不要开公网。
- 日志里不允许出现回复正文、完整会话名、密钥；会话名最多前 8 个字符。

## 版本与文档

改动行为就要提版本：

1. 只改 `src/runtime_identity.py` 的 `APP_VERSION`（唯一来源）；
2. 在 `CHANGES.md` 顶部加一段本版本说明（一段话讲清楚「修了什么、怎么验证的」）；
3. 同步 `instructions/WECHAT-CONTRACT.md`、`微信版使用说明.md`、`功能对照与使用说明.md`、
   `diy-src/00-先读我.md` ~ `05-自测与打包.md` 顶部段落；
4. 文档是纯 CRLF、UTF-8 无 BOM，改完请用字节核对，不要用会偷偷转行尾的编辑器。

## 插件包

新增可选插件包请放在 `plugin-packs/<id>/`，至少包含：

```text
plugin.json          名称/版本/入口/依赖/兼容版本/作者/能力/数据用途
plugin.py            薄封装，真正逻辑仍留在 src/plugins/
README.md            这个插件做什么、怎么用、有什么副作用
settings.example.json 示例配置
```

- 主程序只加载用户手动放入的包，**不会自动安装**；
- 清单缺字段、版本不兼容、加载失败都必须降级成中文提示，不许让控制台起不来；
- 不要提交任何密钥、令牌、个人配置、聊天记录。

## 提交前自检清单

- [ ] `python -m unittest discover -s tests` 全绿
- [ ] 没有把 `data/`、`logs/`、`build/`、`dist/`、`installer/` 加进提交
- [ ] 没有硬编码路径、密钥、模型账号
- [ ] 改动的行为已写进 CHANGES.md，版本号已递增
