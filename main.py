"""
AstrBot Plugin: Steam 加群审核
=================================
流程:
1. on_astrbot_loaded 启动后台轮询任务
2. 定期通过 NapCat HTTP API 调用 get_group_system_msg 获取待处理加群请求
3. 从验证消息中提取 SteamID → 调 Steam Web API 查资料
4. 用 Pillow 绘卡片 → 通过 NapCat HTTP API send_group_msg 发到群里
5. 群消息监听器捕获管理员引用回复 → 调 set_group_add_request 同意/拒绝
"""

import re
import os
import time
import base64
import asyncio
from io import BytesIO
from typing import Optional, Dict
from pathlib import Path

import aiohttp
from PIL import Image, ImageDraw, ImageFont

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig

# ============================================================
#  常量
# ============================================================

STEAM_API_BASE = "https://api.steampowered.com"
URL_PLAYER_SUMMARY = f"{STEAM_API_BASE}/ISteamUser/GetPlayerSummaries/v2/"
URL_PLAYER_BANS    = f"{STEAM_API_BASE}/ISteamUser/GetPlayerBans/v1/"
URL_STEAM_LEVEL    = f"{STEAM_API_BASE}/IPlayerService/GetSteamLevel/v1/"
URL_OWNED_GAMES    = f"{STEAM_API_BASE}/IPlayerService/GetOwnedGames/v1/"
URL_RECENT_GAMES   = f"{STEAM_API_BASE}/IPlayerService/GetRecentlyPlayedGames/v1/"
URL_RESOLVE_VANITY = f"{STEAM_API_BASE}/ISteamUser/ResolveVanityURL/v1/"

RE_STEAM64     = re.compile(r'(?<!\d)(7656119\d{10})(?!\d)')
RE_STEAMID     = re.compile(r'STEAM_([0-5]):([01]):(\d+)', re.IGNORECASE)
RE_STEAM3      = re.compile(r'\[U:1:(\d+)\]')
RE_CUSTOM_URL  = re.compile(r'(?:https?://)?steamcommunity\.com/id/([a-zA-Z0-9_-]+)', re.IGNORECASE)
RE_PROFILE_URL = re.compile(r'(?:https?://)?steamcommunity\.com/profiles/(7656119\d{10})', re.IGNORECASE)

PERSONA_STATE = {0:"离线",1:"在线",2:"忙碌",3:"离开",4:"打盹",5:"想交易",6:"想玩游戏"}


# ============================================================
#  SteamID 转换
# ============================================================

def steamid_to_steam64(s: str) -> Optional[str]:
    m = RE_STEAMID.match(s)
    if m:
        y, z = int(m.group(2)), int(m.group(3))
        return str(76561197960265728 + z * 2 + y)
    return None

def steam3_to_steam64(s: str) -> Optional[str]:
    m = RE_STEAM3.match(s)
    if m:
        return str(76561197960265728 + int(m.group(1)))
    return None


# ============================================================
#  NapCat HTTP API 封装
# ============================================================

class NapCatAPI:
    """直接通过 HTTP 调用 NapCat 的 OneBot11 API"""

    def __init__(self, base_url: str, token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.token = token

    async def call(self, action: str, params: dict = None) -> dict:
        url = f"{self.base_url}/{action}"
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=params or {},
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status != 200:
                        logger.error(f"[SteamVerify] NapCat API {action} HTTP {resp.status}")
                        return {}
                    data = await resp.json()
                    return data
        except Exception as e:
            logger.error(f"[SteamVerify] NapCat API {action} 异常: {e}")
            return {}

    async def send_group_msg(self, group_id: str, message: list) -> dict:
        """发送群消息，返回含 message_id 的结果"""
        return await self.call("send_group_msg", {
            "group_id": int(group_id),
            "message": message
        })

    async def set_group_add_request(self, flag: str, sub_type: str,
                                     approve: bool, reason: str = "") -> dict:
        return await self.call("set_group_add_request", {
            "flag": flag,
            "sub_type": sub_type,
            "approve": approve,
            "reason": reason
        })

    async def get_group_system_msg(self) -> dict:
        """获取群系统消息（包含加群请求）"""
        return await self.call("get_group_system_msg")


# ============================================================
#  Steam API 封装
# ============================================================

class SteamAPI:
    def __init__(self, api_key: str):
        self.api_key = api_key

    async def _get(self, url: str, params: dict) -> dict:
        params["key"] = self.api_key
        params["format"] = "json"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params,
                                       timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        logger.error(f"[SteamVerify] Steam API {resp.status} {url}")
                        return {}
                    return await resp.json()
        except Exception as e:
            logger.error(f"[SteamVerify] Steam API 异常: {e}")
            return {}

    async def resolve_vanity_url(self, vanity: str) -> Optional[str]:
        data = await self._get(URL_RESOLVE_VANITY, {"vanityurl": vanity})
        resp = data.get("response", {})
        if resp.get("success") == 1:
            return resp.get("steamid")
        return None

    async def get_player_summary(self, steam64: str) -> dict:
        data = await self._get(URL_PLAYER_SUMMARY, {"steamids": steam64})
        players = data.get("response", {}).get("players", [])
        return players[0] if players else {}

    async def get_player_bans(self, steam64: str) -> dict:
        data = await self._get(URL_PLAYER_BANS, {"steamids": steam64})
        players = data.get("players", [])
        return players[0] if players else {}

    async def get_steam_level(self, steam64: str) -> int:
        data = await self._get(URL_STEAM_LEVEL, {"steamid": steam64})
        return data.get("response", {}).get("player_level", 0)

    async def get_owned_games(self, steam64: str) -> dict:
        data = await self._get(URL_OWNED_GAMES, {
            "steamid": steam64,
            "include_appinfo": "1",
            "include_played_free_games": "1",
        })
        return data.get("response", {})

    async def get_recent_games(self, steam64: str, count: int = 3) -> list:
        data = await self._get(URL_RECENT_GAMES, {"steamid": steam64, "count": count})
        return data.get("response", {}).get("games", [])

    async def download_image(self, url: str) -> Optional[Image.Image]:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        return Image.open(BytesIO(await resp.read()))
        except Exception as e:
            logger.warning(f"[SteamVerify] 下载图片失败: {e}")
        return None

    async def fetch_full_profile(self, steam64: str) -> dict:
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
#  从文本提取 SteamID
# ============================================================

async def extract_steam64(text: str, steam_api: SteamAPI) -> Optional[str]:
    text = text.strip()

    m = RE_PROFILE_URL.search(text)
    if m: return m.group(1)

    m = RE_CUSTOM_URL.search(text)
    if m:
        resolved = await steam_api.resolve_vanity_url(m.group(1))
        if resolved: return resolved

    m = RE_STEAM64.search(text)
    if m: return m.group(1)

    m = RE_STEAMID.search(text)
    if m: return steamid_to_steam64(m.group(0))

    m = RE_STEAM3.search(text)
    if m: return steam3_to_steam64(m.group(0))

    # 兜底：纯字母数字当 vanity URL 试
    clean = text.split()[0] if text else ""
    if clean and re.match(r'^[a-zA-Z0-9_-]{3,32}$', clean):
        resolved = await steam_api.resolve_vanity_url(clean)
        if resolved: return resolved

    return None


# ============================================================
#  卡片绘制器
# ============================================================

class CardRenderer:
    BG_COLOR      = (30, 30, 30)
    HEADER_COLOR  = (23, 26, 33)
    TEXT_WHITE    = (255, 255, 255)
    TEXT_GRAY     = (180, 180, 180)
    TEXT_GREEN    = (87, 203, 222)
    TEXT_RED      = (255, 77, 77)
    TEXT_GOLD     = (255, 215, 0)
    ACCENT_BLUE   = (66, 133, 244)
    DIVIDER_COLOR = (60, 60, 60)
    CARD_W = 600
    PADDING = 20

    def __init__(self):
        self.font_path = self._find_font()
        self.font_lg    = self._load_font(22)
        self.font_md    = self._load_font(16)
        self.font_sm    = self._load_font(13)
        self.font_title = self._load_font(26)

    def _find_font(self) -> Optional[str]:
        candidates = [
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/System/Library/Fonts/PingFang.ttc",
            "C:\\Windows\\Fonts\\msyh.ttc",
            "C:\\Windows\\Fonts\\simhei.ttf",
        ]
        for p in candidates:
            if os.path.exists(p): return p
        return None

    def _load_font(self, size: int):
        try:
            if self.font_path:
                return ImageFont.truetype(self.font_path, size)
        except Exception: pass
        try:
            return ImageFont.truetype("arial.ttf", size)
        except Exception:
            return ImageFont.load_default()

    def render(self, profile: dict, qq_id: str = "",
               avatar_img: Image.Image = None) -> bytes:
        summary      = profile.get("summary", {})
        bans         = profile.get("bans", {})
        level        = profile.get("level", 0)
        game_count   = profile.get("game_count", 0)
        recent_games = profile.get("recent_games", [])
        steam64      = profile.get("steam64", "")

        persona_name  = summary.get("personaname", "未知")
        real_name     = summary.get("realname", "")
        profile_url   = summary.get("profileurl", "")
        persona_state = PERSONA_STATE.get(summary.get("personastate", 0), "未知")
        time_created  = summary.get("timecreated", 0)
        country_code  = summary.get("loccountrycode", "")

        vac_banned       = bans.get("VACBanned", False)
        vac_count        = bans.get("NumberOfVACBans", 0)
        game_bans_count  = bans.get("NumberOfGameBans", 0)
        community_banned = bans.get("CommunityBanned", False)

        # 动态高度
        h = 20 + 90 + 10 + 2 + 15 + 30*7 + 15 + 2 + 15 + 30*3
        if real_name: h += 30
        if recent_games:
            h += 15 + 2 + 15 + 25 + 28 * min(len(recent_games), 5)
        h += 40

        img = Image.new("RGB", (self.CARD_W, h), self.BG_COLOR)
        draw = ImageDraw.Draw(img)
        p = self.PADDING
        y = 15

        draw.rectangle([(0, 0), (self.CARD_W, y + 95)], fill=self.HEADER_COLOR)

        avatar_size = 72
        if avatar_img:
            avatar_img = avatar_img.resize((avatar_size, avatar_size))
            mask = Image.new("L", (avatar_size, avatar_size), 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                [(0,0),(avatar_size-1,avatar_size-1)], radius=12, fill=255)
            img.paste(avatar_img, (p, y+10), mask)
        else:
            draw.rounded_rectangle(
                [(p, y+10),(p+avatar_size, y+10+avatar_size)],
                radius=12, fill=(80,80,80))
            draw.text((p+20, y+30), "?", fill=self.TEXT_GRAY, font=self.font_lg)

        nx = p + avatar_size + 15
        draw.text((nx, y+12), persona_name, fill=self.TEXT_WHITE, font=self.font_title)
        draw.text((nx, y+42), f"Steam64: {steam64}", fill=self.TEXT_GRAY, font=self.font_sm)
        state_color = self.TEXT_GREEN if summary.get("personastate",0) > 0 else self.TEXT_GRAY
        draw.text((nx, y+60), f"● {persona_state}", fill=state_color, font=self.font_sm)
        if qq_id:
            draw.text((self.CARD_W-p-140, y+60), f"QQ: {qq_id}",
                       fill=self.TEXT_GRAY, font=self.font_sm)

        y += 105
        draw.rectangle([(p,y),(self.CARD_W-p,y+1)], fill=self.DIVIDER_COLOR)
        y += 17

        def row(label, value, color=self.TEXT_WHITE):
            nonlocal y
            draw.text((p,y), label, fill=self.TEXT_GRAY, font=self.font_md)
            draw.text((p+140,y), str(value), fill=color, font=self.font_md)
            y += 30

        row("🎮 Steam 等级", f"Lv.{level}", self.TEXT_GOLD)
        row("📦 游戏数量", f"{game_count} 个")
        if real_name:
            row("👤 真实姓名", real_name)
        row("🌐 国家/地区", country_code or "未公开")
        row("📅 注册时间",
            time.strftime("%Y-%m-%d", time.localtime(time_created)) if time_created else "未公开")
        row("🔗 主页",
            profile_url[:45]+"..." if len(profile_url)>45 else profile_url,
            self.ACCENT_BLUE)
        vis = summary.get("communityvisibilitystate", 1)
        row("🔒 资料可见性",
            "公开" if vis==3 else "私密/仅好友",
            self.TEXT_GREEN if vis==3 else self.TEXT_RED)

        y += 5
        draw.rectangle([(p,y),(self.CARD_W-p,y+1)], fill=self.DIVIDER_COLOR)
        y += 17

        draw.text((p,y), "⚠ 封禁信息", fill=self.TEXT_WHITE, font=self.font_lg)
        y += 30
        row("VAC 封禁",
            f"有 ({vac_count}次)" if vac_banned else "无",
            self.TEXT_RED if vac_banned else self.TEXT_GREEN)
        row("游戏封禁",
            f"有 ({game_bans_count}次)" if game_bans_count else "无",
            self.TEXT_RED if game_bans_count else self.TEXT_GREEN)
        row("社区封禁",
            "是" if community_banned else "否",
            self.TEXT_RED if community_banned else self.TEXT_GREEN)

        if recent_games:
            y += 5
            draw.rectangle([(p,y),(self.CARD_W-p,y+1)], fill=self.DIVIDER_COLOR)
            y += 15
            draw.text((p,y), "🕹 最近游玩", fill=self.TEXT_WHITE, font=self.font_lg)
            y += 28
            for g in recent_games[:5]:
                name = g.get("name","Unknown")
                hours = round(g.get("playtime_2weeks",0)/60, 1)
                draw.text((p+10,y), f"• {name}", fill=self.TEXT_WHITE, font=self.font_sm)
                draw.text((self.CARD_W-p-100,y), f"{hours}h/2周",
                          fill=self.TEXT_GRAY, font=self.font_sm)
                y += 28

        y += 10
        draw.text((p,y), "💡 管理员请引用本消息回复「同意」或「拒绝」",
                  fill=(120,120,120), font=self.font_sm)

        buf = BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()


# ============================================================
#  插件主类
# ============================================================

@register(
    "astrbot_plugin_steam_verify",
    "YourName",
    "Steam 加群审核插件",
    "1.0.0",
    "https://github.com/yourname/astrbot_plugin_steam_verify"
)
class SteamVerifyPlugin(Star):

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        # ★ 关键：config 由 AstrBot 框架注入，是个 dict-like 对象
        self.config = config
        self.renderer = CardRenderer()

        # 待审核缓存: bot_msg_id(str) -> request_info
        self.pending_requests: Dict[str, dict] = {}
        # 已处理过的 flag 集合，防重复
        self.processed_flags: set = set()

        # 初始化 API 客户端
        self.steam_api: Optional[SteamAPI] = None
        self.napcat: Optional[NapCatAPI] = None
        self._init_clients()

        # 启动后台轮询
        self._poll_task: Optional[asyncio.Task] = None
        self._poll_started: bool = False

    def _init_clients(self):
        """根据配置初始化 API 客户端"""
        steam_key = self.config.get("steam_api_key", "")
        if steam_key:
            self.steam_api = SteamAPI(steam_key)
            logger.info("[SteamVerify] Steam API 已初始化")
        else:
            logger.error("[SteamVerify] ❌ 未配置 steam_api_key！")

        napcat_url = self.config.get("napcat_http_url", "")
        napcat_token = self.config.get("napcat_token", "")
        if napcat_url:
            self.napcat = NapCatAPI(napcat_url, napcat_token)
            logger.info(f"[SteamVerify] NapCat API 已初始化: {napcat_url}")
        else:
            logger.error("[SteamVerify] ❌ 未配置 napcat_http_url！")

    # ----------------------------------------------------------
    #  AstrBot 加载完成后启动轮询
    # ----------------------------------------------------------

    def _ensure_poll_started(self):
        """确保轮询任务在运行（惰性启动，第一次收到群消息时触发）"""
        if self._poll_started and self._poll_task and not self._poll_task.done():
            return
        try:
            self._poll_task = asyncio.create_task(self._poll_loop())
            self._poll_started = True
            logger.info("[SteamVerify] ✅ 后台轮询任务已启动")
        except Exception as e:
            logger.error(f"[SteamVerify] 启动轮询失败: {e}")

    async def _poll_loop(self):
        """后台轮询：定期检查 NapCat 的群系统消息"""
        interval = self.config.get("poll_interval", 10)
        # 等几秒让 NapCat 连接稳定
        await asyncio.sleep(5)

        while True:
            try:
                if self.napcat and self.steam_api:
                    await self._check_group_requests()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[SteamVerify] 轮询出错: {e}")
            await asyncio.sleep(interval)

    async def _check_group_requests(self):
        """调用 get_group_system_msg 获取待处理的加群请求"""
        result = await self.napcat.get_group_system_msg()
        logger.debug(f"[SteamVerify] get_group_system_msg 返回: {list(result.get('data', {}).keys()) if result.get('data') else 'empty'}")
        data = result.get("data", {})
        if not data:
            return

        # NapCat 不同版本字段名不同，全部兼容
        join_list = (
            data.get("join_requests", [])
            or data.get("joinRequests", [])
            or data.get("join_list", [])
            or data.get("filtered_join_requests", [])
            or data.get("filteredJoinRequests", [])
            or []
        )

        monitored = [str(g) for g in self.config.get("monitored_groups", [])]

        for req in join_list:
            # 提取字段（不同 NapCat 版本字段名可能不同）
            flag     = str(req.get("request_id", "") or req.get("flag", "") or req.get("msg_seq", ""))
            group_id = str(req.get("group_id", "") or req.get("groupId", ""))
            user_id  = str(req.get("requester_uin", "") or req.get("user_id", "") or req.get("userId", ""))
            comment  = str(req.get("message", "") or req.get("additional", "") or req.get("comment", ""))
            checked  = req.get("checked", False) or req.get("is_handled", False)

            # 跳过已处理的
            if checked:
                continue
            if not flag or flag in self.processed_flags:
                continue
            # 群号过滤
            if monitored and group_id not in monitored:
                continue

            self.processed_flags.add(flag)
            logger.info(f"[SteamVerify] 新加群请求: 群{group_id} QQ{user_id} 消息: {comment}")

            # 异步处理，不阻塞轮询
            asyncio.create_task(
                self._handle_join_request(flag, group_id, user_id, comment)
            )

    async def _handle_join_request(self, flag: str, group_id: str,
                                    user_id: str, comment: str):
        """处理单个加群请求"""
        notify_group = self.config.get("notify_group_id", "") or group_id

        # 1. 提取 SteamID
        steam64 = await extract_steam64(comment, self.steam_api)
        if not steam64:
            # 没找到 SteamID，发提醒让管理员手动处理
            await self.napcat.send_group_msg(notify_group, [
                {"type": "text", "data": {"text":
                    f"📨 新的加群申请\n"
                    f"👤 QQ: {user_id}\n"
                    f"📝 验证消息: {comment}\n"
                    f"⚠ 未检测到有效的 Steam ID，请手动审核"
                }}
            ])
            return

        # 2. 查 Steam 资料
        logger.info(f"[SteamVerify] 查询 Steam 资料: {steam64}")
        profile = await self.steam_api.fetch_full_profile(steam64)

        if not profile.get("summary"):
            await self.napcat.send_group_msg(notify_group, [
                {"type": "text", "data": {"text":
                    f"📨 加群申请 | QQ: {user_id}\n"
                    f"🔍 Steam64: {steam64}\n"
                    f"❌ 无法获取 Steam 资料（无效ID或私密）"
                }}
            ])
            return

        # 3. 自动审核检查
        bans       = profile.get("bans", {})
        level      = profile.get("level", 0)
        game_count = profile.get("game_count", 0)

        auto_reject_reason = None

        if self.config.get("auto_reject_vac") and bans.get("VACBanned"):
            auto_reject_reason = "存在 VAC 封禁记录"

        min_lvl = self.config.get("min_steam_level", 0)
        if min_lvl > 0 and level < min_lvl:
            auto_reject_reason = f"Steam 等级 {level} < 要求 {min_lvl}"

        min_g = self.config.get("min_games_count", 0)
        if min_g > 0 and game_count < min_g:
            auto_reject_reason = f"游戏数量 {game_count} < 要求 {min_g}"

        req_appids = self.config.get("required_game_appids", [])
        if req_appids:
            owned = {g.get("appid") for g in profile.get("games", [])}
            missing = [str(a) for a in req_appids if a not in owned]
            if missing:
                auto_reject_reason = f"缺少必需游戏: {','.join(missing)}"

        # 4. 下载头像 & 画卡片
        avatar_url = (profile["summary"].get("avatarfull")
                      or profile["summary"].get("avatarmedium", ""))
        avatar_img = await self.steam_api.download_image(avatar_url) if avatar_url else None
        card_bytes = self.renderer.render(profile, qq_id=user_id, avatar_img=avatar_img)

        # 转 base64 给 NapCat 发图（不需要保存文件）
        card_b64 = base64.b64encode(card_bytes).decode()

        # 5. 自动拒绝
        if auto_reject_reason:
            await self.napcat.send_group_msg(notify_group, [
                {"type": "text", "data": {"text":
                    f"📨 加群申请 | QQ: {user_id}\n❌ 已自动拒绝: {auto_reject_reason}"}},
            ])
            await self.napcat.send_group_msg(notify_group, [
                {"type": "image", "data": {"file": f"base64://{card_b64}"}}
            ])
            await self.napcat.set_group_add_request(flag, "add", False, auto_reject_reason)
            logger.info(f"[SteamVerify] 自动拒绝 QQ{user_id}: {auto_reject_reason}")
            return

        # 6. 发卡片，等管理员审批
        # 先发文字提示
        await self.napcat.send_group_msg(notify_group, [
            {"type": "text", "data": {"text":
                f"📨 新的加群申请\n"
                f"👤 QQ: {user_id} | 群: {group_id}\n"
                f"🎮 Steam: {profile['summary'].get('personaname','')} ({steam64})\n"
                f"💡 请引用下方卡片回复「同意」或「拒绝」"
            }}
        ])

        # 发图片卡片
        send_result = await self.napcat.send_group_msg(notify_group, [
            {"type": "image", "data": {"file": f"base64://{card_b64}"}}
        ])

        # 提取 message_id
        bot_msg_id = str(send_result.get("data", {}).get("message_id", ""))
        if bot_msg_id and bot_msg_id != "0":
            self.pending_requests[bot_msg_id] = {
                "flag": flag,
                "sub_type": "add",
                "group_id": group_id,
                "user_id": user_id,
                "steam64": steam64,
                "timestamp": time.time(),
            }
            logger.info(f"[SteamVerify] 卡片已发送 msg_id={bot_msg_id}，等待审批")
        else:
            logger.warning(f"[SteamVerify] 发送卡片未获取到 message_id: {send_result}")

    # ----------------------------------------------------------
    #  监听群消息：捕获管理员引用回复
    # ----------------------------------------------------------

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """监听所有群消息，检查是否是对审核卡片的引用回复"""
        # 惰性启动轮询（第一次收到群消息时自动拉起）
        self._ensure_poll_started()

        if not self.pending_requests:
            return  # 没有待审核的就跳过

        # 获取原始消息数据来找 reply 组件
        raw_msg = getattr(event.message_obj, "raw_message", None)
        message_chain = getattr(event.message_obj, "message", [])

        # 从消息链中提取引用的 message_id
        reply_msg_id = None

        # message_chain 可能是 list[dict] 或 list[组件对象]
        if isinstance(message_chain, list):
            for seg in message_chain:
                if isinstance(seg, dict):
                    if seg.get("type") == "reply":
                        reply_msg_id = str(seg.get("data", {}).get("id", ""))
                        break
                else:
                    # AstrBot 消息组件对象
                    seg_type = getattr(seg, "type", None)
                    if seg_type and str(seg_type).lower() in ("reply", "reference"):
                        seg_data = getattr(seg, "data", {})
                        if isinstance(seg_data, dict):
                            reply_msg_id = str(seg_data.get("id", ""))
                        break

        # 如果 message_chain 里没找到，尝试从 raw 里找
        if not reply_msg_id and isinstance(raw_msg, str) and "[CQ:reply,id=" in raw_msg:
            m = re.search(r'\[CQ:reply,id=(-?\d+)', raw_msg)
            if m:
                reply_msg_id = m.group(1)

        if not reply_msg_id or reply_msg_id not in self.pending_requests:
            return

        # 检查权限：只有管理员/群主可以操作
        if event.role != "admin":
            # role 字段由 AstrBot 根据 sender.role 设置
            # 有时可能没正确识别，这里放宽一下，也检查 raw
            return

        req_info = self.pending_requests[reply_msg_id]

        # 过期检查
        expire_min = self.config.get("card_expire_minutes", 1440)
        if time.time() - req_info["timestamp"] > expire_min * 60:
            del self.pending_requests[reply_msg_id]
            yield event.plain_result("⏰ 该审核请求已过期，请手动处理。")
            event.stop_event()
            return

        # 解析操作
        text = event.message_str.strip().lower()
        approve = None
        reason = ""

        if text in ("同意", "通过", "批准", "approve", "yes", "y", "ok"):
            approve = True
        elif any(text.startswith(k) for k in ("拒绝", "驳回", "reject", "deny", "no", "n")):
            approve = False
            parts = event.message_str.strip().split(None, 1)
            if len(parts) > 1:
                reason = parts[1]

        if approve is None:
            return  # 不是审批指令，忽略

        # 调用 NapCat API 处理请求
        try:
            await self.napcat.set_group_add_request(
                req_info["flag"], req_info["sub_type"], approve, reason
            )
            action = "✅ 已同意" if approve else "❌ 已拒绝"
            if reason:
                action += f"（理由：{reason}）"
            action += f"\n👤 QQ: {req_info['user_id']}\n🎮 Steam: {req_info['steam64']}"
            yield event.plain_result(action)
        except Exception as e:
            logger.error(f"[SteamVerify] 处理失败: {e}")
            yield event.plain_result(f"❌ 处理失败: {e}")
        finally:
            self.pending_requests.pop(reply_msg_id, None)

        event.stop_event()  # 阻止后续 LLM 处理

    # ----------------------------------------------------------
    #  手动测试命令
    # ----------------------------------------------------------

    @filter.command("steam_lookup")
    async def test_lookup(self, event: AstrMessageEvent, steam_input: str):
        """手动查询 Steam 资料: /steam_lookup <SteamID或链接>"""
        if not self.steam_api:
            yield event.plain_result("❌ 未配置 steam_api_key，请在插件设置中填写")
            return

        steam64 = await extract_steam64(steam_input, self.steam_api)
        if not steam64:
            yield event.plain_result(f"❌ 无法解析 Steam ID: {steam_input}")
            return

        yield event.plain_result(f"🔍 正在查询 {steam64} ...")

        profile = await self.steam_api.fetch_full_profile(steam64)
        if not profile.get("summary"):
            yield event.plain_result("❌ 未找到该 Steam 用户")
            return

        avatar_url = (profile["summary"].get("avatarfull")
                      or profile["summary"].get("avatarmedium", ""))
        avatar_img = await self.steam_api.download_image(avatar_url) if avatar_url else None
        card_bytes = self.renderer.render(profile, avatar_img=avatar_img)

        # 保存到 data 目录
        tmp_dir = Path("data/temp")
        tmp_dir.mkdir(parents=True, exist_ok=True)
        card_path = tmp_dir / f"steam_card_{steam64}.png"
        card_path.write_bytes(card_bytes)

        yield event.image_result(str(card_path))
    @filter.command("steam_start")
    async def manual_start(self, event: AstrMessageEvent):
        """手动启动轮询: /steam_start"""
        self._ensure_poll_started()
        running = "✅ 运行中" if (self._poll_task and not self._poll_task.done()) else "❌ 未运行"
        yield event.plain_result(f"🔄 轮询任务状态: {running}")
    @filter.command("steam_pending")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def show_pending(self, event: AstrMessageEvent):
        """查看待审核列表: /steam_pending"""
        if not self.pending_requests:
            yield event.plain_result("📋 当前没有待审核的请求")
            return
        lines = ["📋 待审核列表:"]
        for msg_id, info in self.pending_requests.items():
            elapsed = int((time.time() - info["timestamp"]) / 60)
            lines.append(
                f"  • QQ {info['user_id']} | Steam {info['steam64']} | "
                f"群 {info['group_id']} | {elapsed}分钟前 | msg_id={msg_id}"
            )
        yield event.plain_result("\n".join(lines))

    @filter.command("steam_status")
    async def show_status(self, event: AstrMessageEvent):
        """查看插件状态: /steam_status"""
        # 顺便确保轮询在跑
        self._ensure_poll_started()

        steam_ok = "✅" if self.steam_api else "❌"
        napcat_ok = "✅" if self.napcat else "❌"
        poll_ok = "✅ 运行中" if (self._poll_task and not self._poll_task.done()) else "❌ 未运行"
        monitored = self.config.get("monitored_groups", [])
        mon_text = ", ".join(str(g) for g in monitored) if monitored else "全部群"

        yield event.plain_result(
            f"🔧 Steam 加群审核 插件状态\n"
            f"Steam API: {steam_ok}\n"
            f"NapCat API: {napcat_ok} ({self.config.get('napcat_http_url', '未配置')})\n"
            f"轮询任务: {poll_ok}\n"
            f"监控群: {mon_text}\n"
            f"待审核数: {len(self.pending_requests)}\n"
            f"已处理数: {len(self.processed_flags)}"
        )

    async def terminate(self):
        """插件卸载"""
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
        self.pending_requests.clear()
        self.processed_flags.clear()
        logger.info("[SteamVerify] 插件已卸载")
