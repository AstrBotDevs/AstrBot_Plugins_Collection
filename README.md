# AstrBot 禁用插件

这是一个用于 AstrBot 的禁用插件，允许管理员通过指令禁用或解除指定 QQ 用户的机器人使用权限，ban-help查看详情

## 功能

- **ban <QQ号>**：在当前群聊中禁用指定 QQ 用户，禁用后该用户在本群内发送的消息将不被机器人处理。
- **ban-all <QQ号>**：全局禁用指定 QQ 用户，禁用后该用户在所有场景（群聊和私聊）发送的消息均不被机器人处理。
- **pass <QQ号>**：解除当前群聊中对指定 QQ 用户的禁用。
- **pass-all <QQ号>**：解除全局对指定 QQ 用户的禁用。

## 使用方法

1. 将本插件仓库克隆或下载后，放入 AstrBot 项目的 `data/plugins/` 目录下，确保目录名称为 `astrbot_plugin_ban`。
2. 确认插件文件完整（包括 `main.py`、`metadata.yaml`、`README.md` 以及可选的配置文件）。
3. 重启 AstrBot，插件会自动加载，并在管理面板的插件列表中显示为已安装插件。
4. 使用管理员账号在群聊中发送指令，即可控制用户的禁用状态。

## 配置

目前本插件暂无额外配置项。如有需要，可通过添加 `_conf_schema.json` 文件扩展配置。

## 开发

参考 [AstrBot 插件开发文档](https://astrbot.soulter.top/dev/plugin.html) 了解更多插件开发和打包上传的细节。

