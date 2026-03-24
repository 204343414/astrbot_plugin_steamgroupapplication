"""
AstrBot Plugin: Steam 加群审核
=================================
流程:
1. NapCat(OneBot11) 推送 group_request 事件 → 插件拦截
2. 从验证消息中提取 SteamID（64位 / 自定义URL / STEAM_X:X:X 格式）
3. 调用 Steam Web API 查询玩家资料、封禁、等级、游戏数
4. 用 Pillow 绘制一张审核卡片图片发到群里
5. 管理员引用该卡片消息回复"同意"或"拒绝"→ 自动调用 OneBot API 处理请求
"""

import re
import time
import json
import os
import asyncio
from io import BytesIO
from typing import Optional
from pathlib import Path

import aiohttp
from PIL import Image, ImageDraw, ImageFont

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

# ============================================================
#  常量 & Steam API 端点
# ============================================================

STEAM_API_BASE = "https://api.steampowered.com"

# GetPlayerSummaries: 头像、昵称、主页、在线状态、注册时间等
URL_PLAYER_SUMMARY = f"{STEAM_API_BASE}/ISteamUser/GetPlayerSummaries/v2/"
# GetPlayerBans: VAC封禁、游戏封禁信息
URL_PLAYER_BANS = f"{STEAM_API_BASE}/ISteamUser/GetPlayerBans/v1/"
# GetSteamLevel: Steam等级
URL_STEAM_LEVEL = f"{STEAM_API_BASE}/IPlayerService/GetSteamLevel/v1/"
# GetOwnedGames: 拥有的游戏列表
URL_OWNED_GAMES = f"{STEAM_API_BASE}/IPlayerService/GetOwnedGames/v1/"
# GetRecentlyPlayedGames: 最近游玩
URL_RECENT_GAMES = f"{STEAM_API_BASE}/IPlayerService/GetRecentlyPlayedGames/v1/"
# ResolveVanityURL: 自定义URL → SteamID64
URL_RESOLVE_VANITY = f"{STEAM_API_BASE}/ISteamUser/ResolveVanityURL/v1/"

# SteamID 格式正则
RE_STEAM64 = re.compile(r'(?<!\d)(7656119\d{10})(?!\d)')
RE_STEAMID = re.compile(r'STEAM_([0-5]):([01]):(\d+)', re.IGNORECASE)
RE_STEAM3 = re.compile(r'\[U:1:(\d+)\]')
RE_CUSTOM_URL = re.compile(
    r'(?:https?://)?steamcommunity\.com/id/([a-zA-Z0-9_-]+)', re.IGNORECASE
)
RE_PROFILE_URL = re.compile(
    r'(?:https?://)?steamcommunity\.com/profiles/(7656119\d{10})', re.IGNORECASE
)

# 在线状态映射
PERSONA_STATE = {
    0: "离线", 1: "在线", 2: "忙碌",
    3: "离开", 4: "打盹", 5: "想交易", 6: "想玩游戏"
}

# ============================================================
#  SteamID 转换工具
# ============================================================

def steamid_to_steam64(steam_id_str: str) -> Optional[str]:
    """STEAM_X:Y:Z → Steam64"""
    m = RE_STEAMID.match(steam_id_str)
    if m:
        y, z = int(m.group(2)), int(m.group(3))
        return str(76561197960265728 + z * 2 + y)
    return None

def steam3_to_steam64(steam3_str: str) -> Optional[str]:
    """[U:1:XXXXX] → Steam64"""
    m = RE_STEAM3.match(steam3_str)
    if m:
        account_id = int(m.group(1))
        return str(76561197960265728 + account_id)
    return None

# ============================================================
#  Steam API 请求封装
# ============================================================

class SteamAPI:
    """封装 Steam Web API 的异步请求"""

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def _get(self, url: str, params: dict) -> dict:
        params["key"] = self.api_key
        params["format"] = "json"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    logger.error(f"Steam API 请求失败: {resp.status} {url}")
                    return {}
                return await resp.json()

    async def resolve_vanity_url(self, vanity: str) -> Optional[str]:
        """自定义 URL → Steam64 ID"""
        data = await self._get(URL_RESOLVE_VANITY, {"vanityurl": vanity})
        resp = data.get("response", {})
        if resp.get("success") == 1:
            return resp.get("steamid")
        return None

    async def get_player_summary(self, steam64: str) -> dict:
        """获取玩家基本资料（头像、昵称、主页、状态、注册时间等）"""
        data = await self._get(URL_PLAYER_SUMMARY, {"steamids": steam64})
        players = data.get("response", {}).get("players", [])
        return players[0] if players else {}

    async def get_player_bans(self, steam64: str) -> dict:
        """获取封禁信息"""
        data = await self._get(URL_PLAYER_BANS, {"steamids": steam64})
        players = data.get("players", [])
        return players[0] if players else {}

    async def get_steam_level(self, steam64: str) -> int:
        """获取 Steam 等级"""
        data = await self._get(URL_STEAM_LEVEL, {"steamid": steam64})
        return data.get("response", {}).get("player_level", 0)

    async def get_owned_games(self, steam64: str, include_info: bool = True) -> dict:
        """获取游戏列表"""
        params = {
            "steamid": steam64,
            "include_appinfo": "1" if include_info else "0",
            "include_played_free_games": "1",
        }
        data = await self._get(URL_OWNED_GAMES, params)
        return data.get("response", {})

    async def get_recent_games(self, steam64: str, count: int = 3) -> list:
        """获取最近游玩的游戏"""
        data = await self._get(URL_RECENT_GAMES, {"steamid": steam64, "count": count})
        return data.get("response", {}).get("games", [])

    async def download_image(self, url: str) -> Optional[Image.Image]:
        """下载图片并返回 PIL Image"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        raw = await resp.read()
                        return Image.open(BytesIO(raw))
        except Exception as e:
            logger.warning(f"下载图片失败: {e}")
        return None

    async def fetch_full_profile(self, steam64: str) -> dict:
        """并发拉取所有资料，合并成一个字典"""
        summary, bans, level, games_data, recent = await asyncio.gather(
            self.get_player_summary(steam64),
            self.get_player_bans(steam64),
            self.get_steam_level(steam64),
            self.get_owned_games(steam64),
            self.get_recent_games(steam64),
        )
        return {
            "steam64": steam64,
            "summary": summary,
            "bans": bans,
            "level": level,
            "game_count": games_data.get("game_count", 0),
            "games": games_data.get("games", []),
            "recent_games": recent,
        }


# ============================================================
#  卡片绘制器
# ============================================================

class CardRenderer:
    """使用 Pillow 绘制 Steam 资料审核卡片"""

    # 颜色
    BG_COLOR = (30, 30, 30)
    HEADER_COLOR = (23, 26, 33)
    TEXT_WHITE = (255, 255, 255)
    TEXT_GRAY = (180, 180, 180)
    TEXT_GREEN = (87, 203, 222)
    TEXT_RED = (255, 77, 77)
    TEXT_GOLD = (255, 215, 0)
    ACCENT_BLUE = (66, 133, 244)
    DIVIDER_COLOR = (60, 60, 60)

    CARD_W = 600
    PADDING = 20

    def __init__(self):
        # 尝试加载中文字体，找不到就用默认
        self.font_path = self._find_font()
        self.font_lg = self._load_font(22)
        self.font_md = self._load_font(16)
        self.font_sm = self._load_font(13)
        self.font_title = self._load_font(26)

    def _find_font(self) -> Optional[str]:
        """尝试寻找可用的中文字体"""
        candidates = [
            # Linux 常见
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            # macOS
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/STHeiti Light.ttc",
            # Windows
            "C:\\Windows\\Fonts\\msyh.ttc",
            "C:\\Windows\\Fonts\\simhei.ttf",
        ]
        for path in candidates:
            if os.path.exists(path):
                return path
        return None

    def _load_font(self, size: int):
        try:
            if self.font_path:
                return ImageFont.truetype(self.font_path, size)
        except Exception:
            pass
        try:
            return ImageFont.truetype("arial.ttf", size)
        except Exception:
            return ImageFont.load_default()

    def render(self, profile: dict, qq_id: str = "", avatar_img: Image.Image = None) -> bytes:
        """绘制卡片，返回 PNG bytes"""
        summary = profile.get("summary", {})
        bans = profile.get("bans", {})
        level = profile.get("level", 0)
        game_count = profile.get("game_count", 0)
        recent_games = profile.get("recent_games", [])
        steam64 = profile.get("steam64", "")

        persona_name = summary.get("personaname", "未知")
        real_name = summary.get("realname", "")
        profile_url = summary.get("profileurl", "")
        persona_state = PERSONA_STATE.get(summary.get("personastate", 0), "未知")
        time_created = summary.get("timecreated", 0)
        country_code = summary.get("loccountrycode", "")

        vac_banned = bans.get("VACBanned", False)
        vac_count = bans.get("NumberOfVACBans", 0)
        game_bans = bans.get("NumberOfGameBans", 0)
        community_banned = bans.get("CommunityBanned", False)
        economy_ban = bans.get("EconomyBan", "none")

        # ---- 动态计算高度 ----
        h = 20  # top padding
        h += 90  # header (avatar + name)
        h += 10
        h += 2   # divider
        h += 15
        h += 30 * 7  # info rows (7行基础信息)
        h += 15
        h += 2   # divider
        h += 15
        h += 30 * 3  # ban info (3行)
        if recent_games:
            h += 15
            h += 2  # divider
            h += 15
            h += 25  # "最近游玩" 标题
            h += 28 * min(len(recent_games), 5)
        h += 30  # bottom

        img = Image.new("RGB", (self.CARD_W, h), self.BG_COLOR)
        draw = ImageDraw.Draw(img)
        p = self.PADDING
        y = 15

        # ---- Header: 头像 + 名字 ----
        # 画 header 背景
        draw.rectangle([(0, 0), (self.CARD_W, y + 95)], fill=self.HEADER_COLOR)

        # 粘贴头像
        avatar_size = 72
        if avatar_img:
            avatar_img = avatar_img.resize((avatar_size, avatar_size))
            # 圆角蒙版
            mask = Image.new("L", (avatar_size, avatar_size), 0)
            mask_draw = ImageDraw.Draw(mask)
            mask_draw.rounded_rectangle([(0, 0), (avatar_size - 1, avatar_size - 1)], radius=12, fill=255)
            img.paste(avatar_img, (p, y + 10), mask)
        else:
            draw.rounded_rectangle(
                [(p, y + 10), (p + avatar_size, y + 10 + avatar_size)],
                radius=12, fill=(80, 80, 80)
            )
            draw.text((p + 20, y + 30), "?", fill=self.TEXT_GRAY, font=self.font_lg)

        name_x = p + avatar_size + 15

        # 名字
        draw.text((name_x, y + 12), persona_name, fill=self.TEXT_WHITE, font=self.font_title)
        # Steam64 ID
        draw.text((name_x, y + 42), f"Steam64: {steam64}", fill=self.TEXT_GRAY, font=self.font_sm)
        # 在线状态小标签
        state_color = self.TEXT_GREEN if summary.get("personastate", 0) > 0 else self.TEXT_GRAY
        draw.text((name_x, y + 60), f"● {persona_state}", fill=state_color, font=self.font_sm)

        if qq_id:
            draw.text((self.CARD_W - p - 140, y + 60), f"QQ: {qq_id}", fill=self.TEXT_GRAY, font=self.font_sm)

        y += 95 + 10

        # ---- 分割线 ----
        draw.rectangle([(p, y), (self.CARD_W - p, y + 1)], fill=self.DIVIDER_COLOR)
        y += 17

        # ---- 基础信息 ----
        def draw_row(label, value, color=self.TEXT_WHITE):
            nonlocal y
            draw.text((p, y), label, fill=self.TEXT_GRAY, font=self.font_md)
            draw.text((p + 140, y), str(value), fill=color, font=self.font_md)
            y += 30

        draw_row("🎮 Steam 等级", f"Lv.{level}", self.TEXT_GOLD)
        draw_row("📦 游戏数量", f"{game_count} 个")
        if real_name:
            draw_row("👤 真实姓名", real_name)
        draw_row("🌐 国家/地区", country_code if country_code else "未公开")
        draw_row("📅 注册时间",
                 time.strftime("%Y-%m-%d", time.localtime(time_created)) if time_created else "未公开")
        draw_row("🔗 主页", profile_url[:45] + "..." if len(profile_url) > 45 else profile_url,
                 self.ACCENT_BLUE)
        # 资料可见性
        visibility = summary.get("communityvisibilitystate", 1)
        vis_text = "公开" if visibility == 3 else "私密/仅好友"
        vis_color = self.TEXT_GREEN if visibility == 3 else self.TEXT_RED
        draw_row("🔒 资料可见性", vis_text, vis_color)

        y += 5
        # ---- 分割线 ----
        draw.rectangle([(p, y), (self.CARD_W - p, y + 1)], fill=self.DIVIDER_COLOR)
        y += 17

        # ---- 封禁信息 ----
        draw.text((p, y), "⚠ 封禁信息", fill=self.TEXT_WHITE, font=self.font_lg)
        y += 30

        vac_text = f"有 ({vac_count}次)" if vac_banned else "无"
        vac_color = self.TEXT_RED if vac_banned else self.TEXT_GREEN
        draw_row("VAC 封禁", vac_text, vac_color)

        gb_text = f"有 ({game_bans}次)" if game_bans > 0 else "无"
        gb_color = self.TEXT_RED if game_bans > 0 else self.TEXT_GREEN
        draw_row("游戏封禁", gb_text, gb_color)

        cb_text = "是" if community_banned else "否"
        draw_row("社区封禁", cb_text, self.TEXT_RED if community_banned else self.TEXT_GREEN)

        # ---- 最近游玩 ----
        if recent_games:
            y += 5
            draw.rectangle([(p, y), (self.CARD_W - p, y + 1)], fill=self.DIVIDER_COLOR)
            y += 15
            draw.text((p, y), "🕹 最近游玩", fill=self.TEXT_WHITE, font=self.font_lg)
            y += 28
            for g in recent_games[:5]:
                name = g.get("name", "Unknown")
                pt2w = g.get("playtime_2weeks", 0)
                hours = round(pt2w / 60, 1)
                draw.text((p + 10, y), f"• {name}", fill=self.TEXT_WHITE, font=self.font_sm)
                draw.text((self.CARD_W - p - 100, y), f"{hours}h / 2周",
                          fill=self.TEXT_GRAY, font=self.font_sm)
                y += 28

        # ---- 底部提示 ----
        y += 10
        draw.text(
            (p, y),
            "💡 管理员请引用本消息回复「同意」或「拒绝」",
            fill=(120, 120, 120), font=self.font_sm
        )

        # 输出
        buf = BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()


# ============================================================
#  从文本中提取 SteamID
# ============================================================

async def extract_steam64(text: str, steam_api: SteamAPI) -> Optional[str]:
    """
    从一段文本中尝试提取 Steam64 ID
    支持格式:
      - 纯 Steam64 数字: 76561198xxxxxxxxx
      - STEAM_X:Y:Z
      - [U:1:XXXXX]
      - https://steamcommunity.com/profiles/76561198xxx
      - https://steamcommunity.com/id/custom_name
      - 纯自定义 URL 名（兜底匹配纯字母数字）
    """
    text = text.strip()

    # 1. steamcommunity.com/profiles/XXXXX
    m = RE_PROFILE_URL.search(text)
    if m:
        return m.group(1)

    # 2. steamcommunity.com/id/custom
    m = RE_CUSTOM_URL.search(text)
    if m:
        vanity = m.group(1)
        resolved = await steam_api.resolve_vanity_url(vanity)
        if resolved:
            return resolved

    # 3. 纯 Steam64
    m = RE_STEAM64.search(text)
    if m:
        return m.group(1)

    # 4. STEAM_X:Y:Z
    m = RE_STEAMID.search(text)
    if m:
        return steamid_to_steam64(m.group(0))

    # 5. [U:1:XXXXX]
    m = RE_STEAM3.search(text)
    if m:
        return steam3_to_steam64(m.group(0))

    # 6. 兜底: 如果整段文本像是一个自定义 URL 名（纯字母数字下划线，3-32位）
    clean = text.strip().split()[0] if text.strip() else ""
    if clean and re.match(r'^[a-zA-Z0-9_-]{3,32}$', clean):
        resolved = await steam_api.resolve_vanity_url(clean)
        if resolved:
            return resolved

    return None


# ============================================================
#  插件主类
# ============================================================

@register(
    "astrbot_plugin_steam_verify",
    "YourName",
    "Steam 加群审核插件 - 自动爬取 Steam 资料，管理员引用回复审批",
    "1.0.0",
    "https://github.com/yourname/astrbot_plugin_steam_verify"
)
class SteamVerifyPlugin(Star):
    """
    AstrBot 插件：Steam 加群审核

    工作流程:
    ┌──────────────────────────────────────────────────────┐
    │  NapCat 推送 group_request 事件                       │
    │  → 提取验证消息中的 SteamID                           │
    │  → 调用 Steam API 获取完整资料                        │
    │  → Pillow 绘制卡片 → 发送到群                         │
    │  → 管理员引用卡片回复「同意/拒绝」                     │
    │  → 调用 OneBot set_group_add_request 处理             │
    └──────────────────────────────────────────────────────┘
    """

    def __init__(self, context: Context):
        super().__init__(context)
        self.steam_api: Optional[SteamAPI] = None
        self.renderer = CardRenderer()

        # 待审核请求缓存:  bot_msg_id -> request_info
        # request_info = {
        #     "flag": str,         # OneBot 的 flag，用于 approve/reject
        #     "sub_type": str,     # "add" 或 "invite"
        #     "group_id": str,
        #     "user_id": str,
        #     "steam64": str,
        #     "profile": dict,
        #     "timestamp": float,
        # }
        self.pending_requests: dict = {}

        # 配置
        self.config: dict = {}

    def _load_config(self):
        """从 AstrBot 配置系统加载插件配置"""
        try:
            cfg = self.context.get_config()
            self.config = cfg if cfg else {}
        except Exception:
            self.config = {}

        api_key = self.config.get("steam_api_key", "")
        if api_key:
            self.steam_api = SteamAPI(api_key)
        else:
            logger.error("[SteamVerify] 未配置 steam_api_key！插件无法正常工作。")
            self.steam_api = None

    # --------------------------------------------------------
    #  NapCat / OneBot11 原始事件监听
    #  AstrBot 的 aiocqhttp 适配器会将 OneBot11 事件透传
    #  我们需要监听 request.group 类型的原始事件
    # --------------------------------------------------------

    @filter.on_decorating_result()
    async def on_startup(self):
        """插件加载时初始化"""
        self._load_config()
        logger.info("[SteamVerify] 插件已加载！")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """
        监听所有群消息 —— 用于捕获管理员引用回复「同意/拒绝」

        AstrBot 目前主要通过消息事件来驱动插件，
        加群请求事件(request)我们通过注册 NapCat raw event handler 来处理
        """
        # 先检查是否是群消息
        if not event.message_obj:
            return

        raw = getattr(event.message_obj, "raw_message", None) or {}
        message_chain = getattr(event.message_obj, "message", [])

        # 检查是否是引用回复 (reply 组件)
        reply_msg_id = None
        for seg in (message_chain if isinstance(message_chain, list) else []):
            if isinstance(seg, dict):
                if seg.get("type") == "reply":
                    reply_msg_id = str(seg.get("data", {}).get("id", ""))
                    break
            else:
                # AstrBot 消息组件对象
                seg_type = getattr(seg, "type", "")
                if seg_type == "reply":
                    reply_msg_id = str(getattr(seg, "data", {}).get("id", ""))
                    break

        if not reply_msg_id:
            return

        # 检查是否引用的是我们的待审核卡片
        if reply_msg_id not in self.pending_requests:
            return

        # 检查发送者是否为管理员
        role = getattr(event, "role", "member")
        if role != "admin":
            # 也检查 raw 中的 sender.role
            sender = {}
            if hasattr(event.message_obj, "raw") and isinstance(event.message_obj.raw, dict):
                sender = event.message_obj.raw.get("sender", {})
            sender_role = sender.get("role", "member")
            if sender_role not in ("admin", "owner"):
                yield event.plain_result("⚠ 只有管理员或群主才能审批加群请求。")
                return

        # 解析操作
        text = event.message_str.strip().lower()
        req_info = self.pending_requests[reply_msg_id]

        # 检查是否过期
        expire_min = self.config.get("card_expire_minutes", 1440)
        if time.time() - req_info["timestamp"] > expire_min * 60:
            del self.pending_requests[reply_msg_id]
            yield event.plain_result("⏰ 该审核请求已过期，请在 QQ 中手动处理。")
            return

        approve = None
        reason = ""
        if text in ("同意", "通过", "批准", "approve", "yes", "y", "ok"):
            approve = True
        elif text.startswith(("拒绝", "驳回", "reject", "deny", "no", "n")):
            approve = False
            # 支持 "拒绝 理由xxx"
            parts = event.message_str.strip().split(None, 1)
            if len(parts) > 1:
                reason = parts[1]

        if approve is None:
            return  # 不是审批操作，忽略

        # 调用 OneBot API 处理加群请求
        try:
            await self._handle_group_request(
                event, req_info["flag"], req_info["sub_type"], approve, reason
            )
            action_text = "✅ 已同意" if approve else f"❌ 已拒绝"
            if reason:
                action_text += f"（理由：{reason}）"
            action_text += f"\n👤 QQ: {req_info['user_id']}\n🎮 Steam: {req_info['steam64']}"
            yield event.plain_result(action_text)
        except Exception as e:
            logger.error(f"[SteamVerify] 处理加群请求失败: {e}")
            yield event.plain_result(f"❌ 处理失败: {e}")
        finally:
            # 清理
            self.pending_requests.pop(reply_msg_id, None)

    async def _handle_group_request(self, event, flag, sub_type, approve, reason=""):
        """
        调用 NapCat/OneBot11 的 set_group_add_request 接口

        不同的 AstrBot 版本和适配器，调用底层 API 的方式可能略有差异。
        这里展示几种兼容写法。
        """
        # 方式1: 通过 event 的 platform_meta 获取 bot 实例
        bot = None
        if hasattr(event, "platform_meta"):
            pm = event.platform_meta
            if hasattr(pm, "bot"):
                bot = pm.bot
            elif hasattr(pm, "client"):
                bot = pm.client

        if bot and hasattr(bot, "api"):
            # aiocqhttp 的 bot.api 对象
            await bot.api.set_group_add_request(
                flag=flag,
                sub_type=sub_type,
                approve=approve,
                reason=reason if not approve else ""
            )
            return

        if bot and hasattr(bot, "call_action"):
            await bot.call_action(
                "set_group_add_request",
                flag=flag,
                sub_type=sub_type,
                approve=approve,
                reason=reason if not approve else ""
            )
            return

        # 方式2: 直接 HTTP 调用 NapCat/go-cqhttp
        # 需要你在配置中指定 onebot_http_url，例如 http://127.0.0.1:3000
        ob_url = self.config.get("onebot_http_url", "")
        if ob_url:
            async with aiohttp.ClientSession() as session:
                await session.post(
                    f"{ob_url.rstrip('/')}/set_group_add_request",
                    json={
                        "flag": flag,
                        "sub_type": sub_type,
                        "approve": approve,
                        "reason": reason if not approve else ""
                    }
                )
            return

        raise RuntimeError("无法找到可用的 OneBot API 调用方式，请检查适配器配置")

    # --------------------------------------------------------
    #  加群请求事件处理（核心）
    #  这个方法需要被注册为 NapCat raw event handler
    #  在 AstrBot 的 aiocqhttp 适配器中，通过事件钩子注册
    # --------------------------------------------------------

    @filter.command("steam_verify_test")
    async def test_lookup(self, event: AstrMessageEvent, steam_input: str):
        """手动测试查询 Steam 资料，用法: /steam_verify_test <SteamID或链接>"""
        self._load_config()
        if not self.steam_api:
            yield event.plain_result("❌ 未配置 Steam API Key")
            return

        steam64 = await extract_steam64(steam_input, self.steam_api)
        if not steam64:
            yield event.plain_result(f"❌ 无法从 '{steam_input}' 中解析出有效的 Steam ID")
            return

        yield event.plain_result(f"🔍 正在查询 Steam 资料: {steam64} ...")

        profile = await self.steam_api.fetch_full_profile(steam64)
        if not profile.get("summary"):
            yield event.plain_result("❌ 未找到该 Steam 用户，请检查 ID 是否正确")
            return

        # 下载头像
        avatar_url = profile["summary"].get("avatarfull") or profile["summary"].get("avatarmedium", "")
        avatar_img = await self.steam_api.download_image(avatar_url) if avatar_url else None

        # 绘制卡片
        card_bytes = self.renderer.render(profile, avatar_img=avatar_img)

        # 保存临时文件
        tmp_dir = Path("data/temp")
        tmp_dir.mkdir(parents=True, exist_ok=True)
        card_path = tmp_dir / f"steam_card_{steam64}.png"
        with open(card_path, "wb") as f:
            f.write(card_bytes)

        # 通过 AstrBot 发图
        from astrbot.api.message_components import Image as AstrImage
        chain = [AstrImage.fromFileSystem(str(card_path))]
        yield event.chain_result(chain)

    @filter.command("steam_verify_reload")
    async def reload_config(self, event: AstrMessageEvent):
        """重新加载插件配置"""
        if event.role != "admin":
            yield event.plain_result("⚠ 仅管理员可执行此操作")
            return
        self._load_config()
        yield event.plain_result("✅ 配置已重新加载")

    @filter.command("steam_verify_pending")
    async def show_pending(self, event: AstrMessageEvent):
        """查看当前待审核的加群请求"""
        if event.role != "admin":
            yield event.plain_result("⚠ 仅管理员可执行此操作")
            return
        if not self.pending_requests:
            yield event.plain_result("📋 当前没有待审核的请求")
            return

        lines = ["📋 待审核列表:"]
        for msg_id, info in self.pending_requests.items():
            elapsed = int((time.time() - info["timestamp"]) / 60)
            lines.append(
                f"  • QQ {info['user_id']} | Steam {info['steam64']} | "
                f"群 {info['group_id']} | {elapsed}分钟前"
            )
        yield event.plain_result("\n".join(lines))

    # ========================================================
    #  注册 NapCat/OneBot 原始事件钩子
    #
    #  ★★★ 这是关键部分 ★★★
    #  AstrBot 的 aiocqhttp 适配器在启动时，会创建底层 bot 实例。
    #  我们需要在 bot 实例上注册 request.group 事件的处理函数。
    #
    #  由于 AstrBot 的事件系统主要面向消息，request 类型事件
    #  需要我们通过「定时轮询」或「注入底层 hook」来实现。
    #
    #  以下提供「HTTP 轮询」和「Websocket 事件注入」两种方案。
    # ========================================================

    @filter.command("steam_verify_bind")
    async def bind_group_request_handler(self, event: AstrMessageEvent):
        """
        [管理员] 启动加群请求监听服务
        使用方式: 在需要监控的群里发 /steam_verify_bind
        然后插件会开始轮询 NapCat 的未处理请求列表
        """
        if event.role != "admin":
            yield event.plain_result("⚠ 仅管理员可执行此操作")
            return

        self._load_config()
        if not self.steam_api:
            yield event.plain_result("❌ 请先配置 steam_api_key")
            return

        group_id = event.session_id
        yield event.plain_result(
            f"✅ 已启动加群申请监听\n"
            f"📌 群号: {group_id}\n"
            f"🔄 将持续监控加群请求并自动生成 Steam 资料卡片\n"
            f"💡 管理员引用卡片回复「同意」或「拒绝」即可操作"
        )

        # 启动后台轮询任务
        asyncio.create_task(self._poll_group_requests(event))

    async def _poll_group_requests(self, event: AstrMessageEvent):
        """
        后台轮询 NapCat 的未处理加群请求

        OneBot11 没有标准的 "获取未处理请求列表" 接口，
        但 NapCat 扩展了 get_group_system_msg 接口。

        另一种方案：注册 WebSocket 事件处理（见下方 _setup_ws_handler）
        """
        processed_flags = set()

        while True:
            try:
                ob_url = self.config.get("onebot_http_url", "")
                if not ob_url:
                    await asyncio.sleep(30)
                    continue

                async with aiohttp.ClientSession() as session:
                    # NapCat 支持 get_group_system_msg
                    async with session.post(
                        f"{ob_url.rstrip('/')}/get_group_system_msg",
                        json={},
                        timeout=aiohttp.ClientTimeout(total=10)
                    ) as resp:
                        if resp.status != 200:
                            await asyncio.sleep(15)
                            continue
                        result = await resp.json()

                data = result.get("data", {})
                join_requests = data.get("join_requests", []) or []

                # 合并 filtered_join_requests (NapCat 特有)
                join_requests += data.get("filteredJoinRequests", []) or []

                for req in join_requests:
                    flag = str(req.get("request_id", "") or req.get("msg_seq", ""))
                    if not flag or flag in processed_flags:
                        continue

                    processed_flags.add(flag)

                    group_id = str(req.get("group_id", ""))
                    user_id = str(req.get("requester_uin", "") or req.get("user_id", ""))
                    comment = req.get("message", "") or req.get("additional", "") or ""

                    # 检查是否在监控群列表
                    monitored = self.config.get("monitored_groups", [])
                    if monitored and group_id not in [str(g) for g in monitored]:
                        continue

                    logger.info(
                        f"[SteamVerify] 收到加群请求: 群{group_id} QQ{user_id} "
                        f"验证消息: {comment}"
                    )

                    # 提取 SteamID
                    steam64 = await extract_steam64(comment, self.steam_api)
                    if not steam64:
                        logger.info(f"[SteamVerify] 验证消息中未找到 SteamID: {comment}")
                        # 也发个提示到群里
                        try:
                            await event.send(event.plain_result(
                                f"📨 新的加群申请\n"
                                f"👤 QQ: {user_id}\n"
                                f"💬 验证消息: {comment}\n"
                                f"⚠ 未检测到有效的 Steam ID，请手动审核"
                            ))
                        except Exception:
                            pass
                        continue

                    # 查询 Steam 资料
                    await self._process_join_request(
                        event, flag, "add", group_id, user_id, steam64
                    )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[SteamVerify] 轮询加群请求出错: {e}")

            await asyncio.sleep(10)  # 每10秒检查一次

    async def _process_join_request(
        self, event: AstrMessageEvent,
        flag: str, sub_type: str,
        group_id: str, user_id: str, steam64: str
    ):
        """处理单个加群请求：查资料 → 画卡片 → 发群 → 存缓存"""

        profile = await self.steam_api.fetch_full_profile(steam64)

        if not profile.get("summary"):
            await event.send(event.plain_result(
                f"📨 加群申请 | QQ: {user_id}\n"
                f"🔍 Steam64: {steam64}\n"
                f"❌ 无法获取 Steam 资料（可能是无效 ID 或私密资料）"
            ))
            return

        # ---- 自动审核逻辑 ----
        bans = profile.get("bans", {})
        level = profile.get("level", 0)
        game_count = profile.get("game_count", 0)

        auto_reject_reason = None

        # VAC 自动拒绝
        if self.config.get("auto_reject_vac") and bans.get("VACBanned"):
            auto_reject_reason = "存在 VAC 封禁记录"

        # 等级检查
        min_level = self.config.get("min_steam_level", 0)
        if min_level > 0 and level < min_level:
            auto_reject_reason = f"Steam 等级 {level} < 要求 {min_level}"

        # 游戏数检查
        min_games = self.config.get("min_games_count", 0)
        if min_games > 0 and game_count < min_games:
            auto_reject_reason = f"游戏数量 {game_count} < 要求 {min_games}"

        # 必需游戏检查
        required_appids = self.config.get("required_game_appids", [])
        if required_appids:
            owned_appids = {g.get("appid") for g in profile.get("games", [])}
            missing = [str(a) for a in required_appids if a not in owned_appids]
            if missing:
                auto_reject_reason = f"缺少必需游戏 AppID: {', '.join(missing)}"

        # 下载头像
        avatar_url = (
            profile["summary"].get("avatarfull")
            or profile["summary"].get("avatarmedium", "")
        )
        avatar_img = await self.steam_api.download_image(avatar_url) if avatar_url else None

        # 绘制卡片
        card_bytes = self.renderer.render(profile, qq_id=user_id, avatar_img=avatar_img)

        # 保存临时图片
        tmp_dir = Path("data/temp")
        tmp_dir.mkdir(parents=True, exist_ok=True)
        card_path = tmp_dir / f"steam_card_{steam64}_{int(time.time())}.png"
        with open(card_path, "wb") as f:
            f.write(card_bytes)

        # 自动拒绝
        if auto_reject_reason:
            await event.send(event.plain_result(
                f"📨 加群申请 | QQ: {user_id}\n"
                f"❌ 已自动拒绝: {auto_reject_reason}"
            ))
            try:
                from astrbot.api.message_components import Image as AstrImage
                await event.send(event.chain_result([AstrImage.fromFileSystem(str(card_path))]))
            except Exception:
                pass

            # 执行拒绝
            try:
                await self._handle_group_request(
                    event, flag, sub_type, False, auto_reject_reason
                )
            except Exception as e:
                logger.error(f"[SteamVerify] 自动拒绝失败: {e}")
            return

        # ---- 发送卡片并等待管理员审批 ----
        try:
            from astrbot.api.message_components import Image as AstrImage

            # 先发提示文字
            await event.send(event.plain_result(
                f"📨 新的加群申请\n"
                f"👤 QQ: {user_id} | 群: {group_id}\n"
                f"🎮 Steam64: {steam64}\n"
                f"💡 请引用下方卡片回复「同意」或「拒绝」"
            ))

            # 发图片卡片 —— 需要捕获消息 ID
            # AstrBot 的 event.send() 返回的对象里可能包含 message_id
            send_result = await event.send(
                event.chain_result([AstrImage.fromFileSystem(str(card_path))])
            )

            # 尝试获取消息 ID
            bot_msg_id = None
            if isinstance(send_result, dict):
                bot_msg_id = str(send_result.get("message_id", ""))
            elif hasattr(send_result, "message_id"):
                bot_msg_id = str(send_result.message_id)
            elif hasattr(send_result, "data"):
                d = send_result.data if isinstance(send_result.data, dict) else {}
                bot_msg_id = str(d.get("message_id", ""))

            if bot_msg_id:
                self.pending_requests[bot_msg_id] = {
                    "flag": flag,
                    "sub_type": sub_type,
                    "group_id": group_id,
                    "user_id": user_id,
                    "steam64": steam64,
                    "profile": profile,
                    "timestamp": time.time(),
                }
                logger.info(
                    f"[SteamVerify] 卡片已发送，msg_id={bot_msg_id}，"
                    f"等待管理员审批"
                )
            else:
                logger.warning(
                    "[SteamVerify] 无法获取卡片消息 ID，"
                    "管理员将无法通过引用回复审批"
                )

        except Exception as e:
            logger.error(f"[SteamVerify] 发送卡片失败: {e}")
            await event.send(event.plain_result(f"❌ 发送资料卡片失败: {e}"))

    async def terminate(self):
        """插件卸载时清理"""
        self.pending_requests.clear()
        logger.info("[SteamVerify] 插件已卸载")
