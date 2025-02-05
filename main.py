from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import sp
from astrbot.api.message_components import At

@register("ban_plugin", "水蜜桃", "用于禁用指定 QQ 用户在群聊或全局范围内使用机器人功能的插件", "1.0.0")
class BanPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        # 从插件配置中获取是否启用禁用功能，默认为启用
        self.enable = config.get('enable', True)
        # 持久化存储，使用 sp 接口加载数据（数据存储为 list，转换为 set 便于处理）
        self.global_ban = set(sp.get('ban_plugin_global_ban', []))
        group_ban_raw = sp.get('ban_plugin_group_ban', {})
        self.group_ban = {gid: set(lst) for gid, lst in group_ban_raw.items()}
        group_allow_raw = sp.get('ban_plugin_group_allow', {})
        self.group_allow = {gid: set(lst) for gid, lst in group_allow_raw.items()}

    def persist(self):
        """将当前禁用数据持久化保存"""
        sp.put('ban_plugin_global_ban', list(self.global_ban))
        sp.put('ban_plugin_group_ban', {gid: list(s) for gid, s in self.group_ban.items()})
        sp.put('ban_plugin_group_allow', {gid: list(s) for gid, s in self.group_allow.items()})
        sp.put('ban_plugin_enable', self.enable)

    def is_banned(self, event: AstrMessageEvent):
        """判断消息发送者是否被禁用。对于群聊场景：
           如果该群存在局部例外，则即使在全局禁用中也允许使用，
           否则全局禁用或群禁用均视为被禁用。"""
        qq = str(event.get_sender_id())
        group_id = event.message_obj.group_id if hasattr(event.message_obj, "group_id") else ""
        if group_id and group_id in self.group_allow and qq in self.group_allow[group_id]:
            return False
        if qq in self.global_ban:
            return True
        if group_id and group_id in self.group_ban and qq in self.group_ban[group_id]:
            return True
        return False

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def filter_banned_users(self, event: AstrMessageEvent):
        """
        全局事件过滤器：
        如果禁用功能启用且发送者被禁用，则停止事件传播，机器人不再响应该用户的消息。
        """
        if not self.enable:
            return
        if self.is_banned(event):
            event.stop_event()
            return

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ban")
    async def ban_user(self, event: AstrMessageEvent):
        """
        在当前群聊中禁用指定 QQ 用户的使用权限。
        格式：/ban @用户...
        支持同时禁用多个用户，且忽略对自己的 @。
        """
        sender_id = str(event.get_sender_id())
        chain = event.message_obj.message
        ats = []
        for comp in chain:
            if isinstance(comp, At):
                qq = str(comp.qq)
                if qq == sender_id:
                    # 忽略管理员对自己的 @
                    continue
                ats.append(qq)
        if not ats:
            yield event.plain_result("请在 /ban 后 @ 一个或多个用户。")
            return

        group_id = event.message_obj.group_id if hasattr(event.message_obj, "group_id") else None
        if not group_id:
            yield event.plain_result("该指令仅限群聊中使用。")
            return

        for qq in ats:
            # 若当前群存在局部例外，则移除局部例外记录
            if group_id in self.group_allow:
                self.group_allow[group_id].discard(qq)
            self.group_ban.setdefault(group_id, set()).add(qq)
        self.persist()
        yield event.plain_result(f"已在本群禁用 QQ {', '.join(ats)} 的使用权限。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ban-all")
    async def ban_user_all(self, event: AstrMessageEvent):
        """
        全局禁用指定 QQ 用户的使用权限。
        格式：/ban-all @用户...
        支持同时禁用多个用户。
        """
        chain = event.message_obj.message
        ats = [str(comp.qq) for comp in chain if isinstance(comp, At)]
        if not ats:
            yield event.plain_result("请在 /ban-all 后 @ 一个或多个用户。")
            return

        for qq in ats:
            self.global_ban.add(qq)
        self.persist()
        yield event.plain_result(f"已全局禁用 QQ {', '.join(ats)} 的使用权限。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("pass")
    async def unban_user(self, event: AstrMessageEvent):
        """
        解除当前群聊中对指定 QQ 用户的禁用。
        格式：/pass @用户...
        解除禁用后，即使该用户处于全局禁用中，在本群也可以使用机器人，
        但在其他场景仍受全局禁用限制。
        """
        chain = event.message_obj.message
        ats = [str(comp.qq) for comp in chain if isinstance(comp, At)]
        if not ats:
            yield event.plain_result("请在 /pass 后 @ 一个或多个用户。")
            return

        group_id = event.message_obj.group_id if hasattr(event.message_obj, "group_id") else None
        if not group_id:
            yield event.plain_result("该指令仅限群聊中使用。")
            return

        for qq in ats:
            if group_id in self.group_ban and qq in self.group_ban[group_id]:
                self.group_ban[group_id].remove(qq)
            self.group_allow.setdefault(group_id, set()).add(qq)
        self.persist()
        yield event.plain_result(f"已解除本群中对 QQ {', '.join(ats)} 的禁用。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("pass-all")
    async def unban_user_all(self, event: AstrMessageEvent):
        """
        解除对指定 QQ 用户的所有禁用（全局及所有群聊）。
        格式：/pass-all @用户...
        支持同时解除多个用户的所有禁用。
        执行后，将彻底移除该用户在全局、所有群聊中因禁用产生的限制。
        """
        chain = event.message_obj.message
        ats = [str(comp.qq) for comp in chain if isinstance(comp, At)]
        if not ats:
            yield event.plain_result("请在 /pass-all 后 @ 一个或多个用户。")
            return

        for qq in ats:
            # 解除全局禁用
            self.global_ban.discard(qq)
            # 遍历所有群聊，解除该用户的群禁用记录
            for gid in list(self.group_ban.keys()):
                self.group_ban[gid].discard(qq)
                if not self.group_ban[gid]:
                    del self.group_ban[gid]
            # 同时移除所有群聊中的局部例外记录（恢复到未设置状态）
            for gid in list(self.group_allow.keys()):
                self.group_allow[gid].discard(qq)
                if not self.group_allow[gid]:
                    del self.group_allow[gid]
        self.persist()
        yield event.plain_result(f"已解除全局及所有群聊中对 QQ {', '.join(ats)} 的禁用。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ban_enable")
    async def ban_enable(self, event: AstrMessageEvent):
        """
        启用禁用功能。
        格式：/ban_enable
        """
        self.enable = True
        self.persist()
        yield event.plain_result("已临时启用禁用功能，重启后失效。永久启用请在插件配置中修改。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ban_disable")
    async def ban_disable(self, event: AstrMessageEvent):
        """
        禁用禁用功能。
        格式：/ban_disable
        """
        self.enable = False
        self.persist()
        yield event.plain_result("已禁用禁用功能，重启后失效。永久禁用请在插件配置中修改。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("banlist")
    async def list_banned_users(self, event: AstrMessageEvent):
        """
        列出当前禁用的用户。
        格式：/banlist
        若在群聊中，会显示本群禁用的用户及全局禁用的用户。
        """
        group_id = event.message_obj.group_id if hasattr(event.message_obj, "group_id") else None
        ret = ""
        if group_id:
            group_banned = self.group_ban.get(group_id, set())
            ret += f"本群禁用的用户: {', '.join(group_banned) if group_banned else '无'}\n"
        ret += f"全局禁用的用户: {', '.join(self.global_ban) if self.global_ban else '无'}"
        yield event.plain_result(ret)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ban-help")
    async def ban_help(self, event: AstrMessageEvent):
        """
        管理员专用命令：显示该插件所有命令列表及功能说明。
        格式：/ban-help
        """
        help_text = (
            "【ban_plugin 插件命令帮助】\n"
            "1. /ban @用户...：在当前群聊中禁用指定 QQ 用户（支持同时禁用多个用户，忽略对自己的 @）。\n"
            "2. /ban-all @用户...：全局禁用指定 QQ 用户（支持同时禁用多个用户）。\n"
            "3. /pass @用户...：解除当前群聊中对指定 QQ 用户的禁用（即使其全局禁用，仍可在本群使用）。\n"
            "4. /pass-all @用户...：解除全局及所有群聊中对指定 QQ 用户的禁用。\n"
            "5. /ban_enable：启用禁用功能。\n"
            "6. /ban_disable：禁用禁用功能。\n"
            "7. /banlist：列出当前禁用的用户（包括本群及全局）。\n"
            "8. /ban-help：显示此帮助信息。"
        )
        yield event.plain_result(help_text)

