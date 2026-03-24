"""
AstrBot Plugin: Steam 加群审核
================================
蓝本: astrbot_plugin_auto_approve_all (已验证能捕获 request 事件)
核心: event.bot (aiocqhttp client) 直接通过 WS 调 OneBot11 API
不需要配置 HTTP 端口/token
"""

import re
import os
import time
import json
import base64
import asyncio
from io import BytesIO
from typing import Optional, Dict
from pathlib import Path

import aiohttp
from PIL import Image, ImageDraw, ImageFont

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

# ============================================================
#  常量
# ============================================================

STEAM_API_BASE = "https://api.steampowered.com"
URL_PLAYER_SUMMARY = f"{STEAM_API_BASE}/ISteamUser/GetPlayerSummaries/v2/"
URL_PLAYER_BANS = f"{STEAM_API_BASE}/ISteamUser/GetPlayerBans/v1/"
URL_STEAM_LEVEL = f"{STEAM_API_BASE}/IPlayerService/GetSteamLevel/v1/"
URL_OWNED_GAMES = f"{STEAM_API_BASE}/IPlayerService/GetOwnedGames/v1/"
URL_RECENT_GAMES = f"{STEAM_API_BASE}/IPlayerService/GetRecentlyPlayedGames/v1/"
URL_RESOLVE_VANITY = f"{STEAM_API_BASE}/ISteamUser/ResolveVanityURL/v1/"

RE_STEAM64 = re.compile(r"(?<!\d)(7656119\d{10})(?!\d)")
RE_STEAMID = re.compile(r"STEAM_([0-5]):([01]):(\d+)", re.IGNORECASE)
RE_STEAM3 = re.compile(r"\[U:1:(\d+)\]")
RE_CUSTOM_URL = re.compile(
    r"(?:https?://)?steamcommunity\.com/id/([a-zA-Z0-9_-]+)", re.IGNORECASE
)
RE_PROFILE_URL = re.compile(
    r"(?:https?://)?steamcommunity\.com/profiles/(7656119\d{10})", re.IGNORECASE
)

PERSONA_STATE = {
    0: "离线", 1: "在线", 2: "忙碌",
    3: "离开", 4: "打盹", 5: "想交易", 6: "想玩游戏",
}
# 绑定数据存储
BINDFILE = Path("data/steam_verify/bindings.json")
# 自动下载的字体保存路径
FONT_DIR = Path("data/fonts")
FONT_FILE = FONT_DIR / "LXGWWenKai-Regular.ttf"
FONT_URL = "https://raw.githubusercontent.com/lxgw/LxgwWenKai/main/fonts/TTF/LXGWWenKai-Regular.ttf"


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
                async with session.get(
                    url, params=params, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
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
        return resp.get("steamid") if resp.get("success") == 1 else None

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
        data = await self._get(
            URL_OWNED_GAMES,
            {"steamid": steam64, "include_appinfo": "1", "include_played_free_games": "1"},
        )
        return data.get("response", {})

    async def get_recent_games(self, steam64: str, count: int = 3) -> list:
        data = await self._get(URL_RECENT_GAMES, {"steamid": steam64, "count": count})
        return data.get("response", {}).get("games", [])

    async def download_image(self, url: str) -> Optional[Image.Image]:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
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
    if m:
        return m.group(1)

    m = RE_CUSTOM_URL.search(text)
    if m:
        resolved = await steam_api.resolve_vanity_url(m.group(1))
        if resolved:
            return resolved

    m = RE_STEAM64.search(text)
    if m:
        return m.group(1)

    m = RE_STEAMID.search(text)
    if m:
        return steamid_to_steam64(m.group(0))

    m = RE_STEAM3.search(text)
    if m:
        return steam3_to_steam64(m.group(0))

    # 兜底：整段像 vanity URL
    clean = text.split()[0] if text else ""
    if clean and re.match(r"^[a-zA-Z0-9_-]{3,32}$", clean):
        resolved = await steam_api.resolve_vanity_url(clean)
        if resolved:
            return resolved

    return None


# ============================================================
#  字体管理（自动下载中文字体，解决方框问题）
# ============================================================

async def ensure_font() -> Optional[str]:
    """确保有可用的中文字体，没有就自动下载"""
    # 1. 先检查系统自带的
    system_fonts = [
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "C:\\Windows\\Fonts\\msyh.ttc",
        "C:\\Windows\\Fonts\\simhei.ttf",
    ]
    for p in system_fonts:
        if os.path.exists(p):
            logger.info(f"[SteamVerify] 找到系统字体: {p}")
            return p

    # 2. 检查已下载的
    if FONT_FILE.exists() and FONT_FILE.stat().st_size > 100000:
        logger.info(f"[SteamVerify] 使用已下载字体: {FONT_FILE}")
        return str(FONT_FILE)

    # 3. 自动下载 LXGW WenKai（开源中文字体，约 8MB）
    logger.info("[SteamVerify] 未找到中文字体，正在自动下载 LXGW WenKai ...")
    FONT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(FONT_URL, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    FONT_FILE.write_bytes(data)
                    logger.info(f"[SteamVerify] 字体下载完成: {FONT_FILE} ({len(data)} bytes)")
                    return str(FONT_FILE)
                else:
                    logger.error(f"[SteamVerify] 字体下载失败: HTTP {resp.status}")
    except Exception as e:
        logger.error(f"[SteamVerify] 字体下载异常: {e}")

    return None


# ============================================================
#  卡片绘制器
# ============================================================

class CardRenderer:
    BG       = (30, 30, 30)
    HEADER   = (23, 26, 33)
    WHITE    = (255, 255, 255)
    GRAY     = (180, 180, 180)
    CYAN     = (87, 203, 222)
    RED      = (255, 77, 77)
    GOLD     = (255, 215, 0)
    BLUE     = (66, 133, 244)
    DIVIDER  = (60, 60, 60)
    W        = 600
    PAD      = 20

    def __init__(self, font_path: Optional[str] = None):
        self.font_path = font_path
        self.font_lg = self._load(22)
        self.font_md = self._load(16)
        self.font_sm = self._load(13)
        self.font_xl = self._load(26)

    def _load(self, size: int):
        if self.font_path:
            try:
                return ImageFont.truetype(self.font_path, size)
            except Exception:
                pass
        return ImageFont.load_default()

    def render(self, profile: dict, qq_id: str = "", avatar_img: Image.Image = None) -> bytes:
        s = profile.get("summary", {})
        b = profile.get("bans", {})
        steam64 = profile.get("steam64", "")
        level = profile.get("level", 0)
        game_count = profile.get("game_count", 0)
        recent = profile.get("recent_games", [])

        name = s.get("personaname", "未知")
        real_name = s.get("realname", "")
        url = s.get("profileurl", "")
        state = PERSONA_STATE.get(s.get("personastate", 0), "未知")
        created = s.get("timecreated", 0)
        country = s.get("loccountrycode", "")
        vis = s.get("communityvisibilitystate", 1)
        vac = b.get("VACBanned", False)
        vac_n = b.get("NumberOfVACBans", 0)
        gb_n = b.get("NumberOfGameBans", 0)
        cb = b.get("CommunityBanned", False)

        # ---- 计算高度 ----
        h = 120  # header
        rows = 6 + (1 if real_name else 0)
        h += 20 + rows * 30 + 20  # 基础信息
        h += 2 + 15 + 30 + 3 * 30  # 封禁区
        if recent:
            h += 2 + 15 + 28 + min(len(recent), 5) * 26
        h += 40  # 底部提示

        img = Image.new("RGB", (self.W, h), self.BG)
        draw = ImageDraw.Draw(img)
        p = self.PAD
        y = 0

        # ---- Header 背景 ----
        draw.rectangle([(0, 0), (self.W, 105)], fill=self.HEADER)

        # ---- 头像 ----
        av_sz = 72
        ax, ay = p, 16
        if avatar_img:
            av = avatar_img.resize((av_sz, av_sz))
            mask = Image.new("L", (av_sz, av_sz), 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                [(0, 0), (av_sz - 1, av_sz - 1)], radius=12, fill=255
            )
            img.paste(av, (ax, ay), mask)
        else:
            draw.rounded_rectangle(
                [(ax, ay), (ax + av_sz, ay + av_sz)], radius=12, fill=(80, 80, 80)
            )
            draw.text((ax + 22, ay + 22), "?", fill=self.GRAY, font=self.font_lg)

        # ---- 名字 / ID / 状态 ----
        nx = ax + av_sz + 15
        draw.text((nx, 18), name, fill=self.WHITE, font=self.font_xl)
        draw.text((nx, 48), f"Steam64: {steam64}", fill=self.GRAY, font=self.font_sm)
        sc = self.CYAN if s.get("personastate", 0) > 0 else self.GRAY
        draw.text((nx, 68), f"● {state}", fill=sc, font=self.font_sm)
        if qq_id:
            draw.text((self.W - p - 130, 68), f"QQ: {qq_id}", fill=self.GRAY, font=self.font_sm)

        y = 110

        # ---- 分割线 ----
        def divider():
            nonlocal y
            draw.rectangle([(p, y), (self.W - p, y + 1)], fill=self.DIVIDER)
            y += 15

        # ---- 信息行 ----
        def row(label, value, color=self.WHITE):
            nonlocal y
            draw.text((p, y), label, fill=self.GRAY, font=self.font_md)
            txt = str(value)
            draw.text((p + 150, y), txt, fill=color, font=self.font_md)
            y += 30

        divider()
        row("🎮  Steam 等级", f"Lv.{level}", self.GOLD)
        row("📦  游戏数量", f"{game_count} 个")
        if real_name:
            row("👤  真实姓名", real_name)
        row("🌐  国家/地区", country or "未公开")
        row("📅  注册时间",
            time.strftime("%Y-%m-%d", time.localtime(created)) if created else "未公开")
        purl = url[:42] + "…" if len(url) > 42 else url
        row("🔗  主页", purl, self.BLUE)
        row("🔒  资料可见性",
            "公开" if vis == 3 else "私密/仅好友",
            self.CYAN if vis == 3 else self.RED)

        y += 5
        divider()

        # ---- 封禁信息 ----
        draw.text((p, y), "⚠  封禁信息", fill=self.WHITE, font=self.font_lg)
        y += 30
        row("VAC 封禁",
            f"有 ({vac_n}次)" if vac else "无",
            self.RED if vac else self.CYAN)
        row("游戏封禁",
            f"有 ({gb_n}次)" if gb_n else "无",
            self.RED if gb_n else self.CYAN)
        row("社区封禁",
            "是" if cb else "否",
            self.RED if cb else self.CYAN)

        # ---- 最近游玩 ----
        if recent:
            y += 5
            divider()
            draw.text((p, y), "🕹  最近游玩", fill=self.WHITE, font=self.font_lg)
            y += 28
            for g in recent[:5]:
                gn = g.get("name", "Unknown")
                hrs = round(g.get("playtime_2weeks", 0) / 60, 1)
                draw.text((p + 10, y), f"• {gn}", fill=self.WHITE, font=self.font_sm)
                draw.text((self.W - p - 100, y), f"{hrs}h/2周", fill=self.GRAY, font=self.font_sm)
                y += 26

        # ---- 底部提示 ----
        y += 12
        draw.text(
            (p, y),
            "💡 管理员请引用本消息回复「同意」或「拒绝」",
            fill=(120, 120, 120),
            font=self.font_sm,
        )

        buf = BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()


# ============================================================
#  插件主类
# ============================================================

@register(
    "astrbot_plugin_steam_verify",
    "YourName",
    "Steam 加群审核：抓取申请中的SteamID → 查资料出卡片 → 引用回复同意/拒绝",
    "1.0.0",
    "https://github.com/yourname/astrbot_plugin_steam_verify",
)
class SteamVerifyPlugin(Star):

    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)

        # 配置
        self.config: dict = config if config else {}

        # Steam API
        steam_key = self.config.get("steam_api_key", "")
        self.steam_api = SteamAPI(steam_key) if steam_key else None

        # 卡片渲染器（字体会在第一次使用时异步初始化）
        self.renderer: Optional[CardRenderer] = None
        self._font_ready = False

        # 待审核: msg_id(str) -> {flag, sub_type, group_id, user_id, steam64, timestamp}
        self.pending: Dict[str, dict] = {}
        # 已处理的 flag，防重复
        self.processed_flags: set = set()

        # 每群绑定: { "群号": { "QQ号": "steam64", ... }, ... }
        self.bindings: Dict[str, Dict[str, str]] = self._load_bindings()

        if self.steam_api:
            logger.info("[SteamVerify] ✅ Steam API 已初始化")
        else:
            logger.error("[SteamVerify] ❌ 未配置 steam_api_key")
    # ==========================================================
    #  绑定数据管理（每群独立）
    # ==========================================================

    def _load_bindings(self) -> Dict[str, Dict[str, str]]:
        """从文件加载绑定数据"""
        try:
            if BINDFILE.exists():
                data = json.loads(BINDFILE.read_text(encoding="utf-8"))
                logger.info(f"[SteamVerify] 已加载绑定数据: {sum(len(v) for v in data.values())} 条")
                return data
        except Exception as e:
            logger.error(f"[SteamVerify] 加载绑定失败: {e}")
        return {}

    def _save_bindings(self):
        """保存绑定数据到文件"""
        try:
            BINDFILE.parent.mkdir(parents=True, exist_ok=True)
            BINDFILE.write_text(json.dumps(self.bindings, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.error(f"[SteamVerify] 保存绑定失败: {e}")

    def _bind(self, group_id: str, qq_id: str, steam64: str):
        """绑定 QQ ↔ Steam64（某个群内）"""
        if group_id not in self.bindings:
            self.bindings[group_id] = {}
        self.bindings[group_id][qq_id] = steam64
        self._save_bindings()
        logger.info(f"[SteamVerify] 绑定: 群{group_id} QQ{qq_id} → {steam64}")

    def _unbind(self, group_id: str, qq_id: str) -> bool:
        """解绑"""
        if group_id in self.bindings and qq_id in self.bindings[group_id]:
            del self.bindings[group_id][qq_id]
            self._save_bindings()
            return True
        return False

    def _check_steam_dup(self, group_id: str, steam64: str) -> Optional[str]:
        """检查该群内是否已有人绑定了这个 steam64，返回已绑定的 QQ 号或 None"""
        grp = self.bindings.get(group_id, {})
        for qq, sid in grp.items():
            if sid == steam64:
                return qq
        return None

    def _get_binding(self, group_id: str, qq_id: str) -> Optional[str]:
        """查询某人在某群的绑定"""
        return self.bindings.get(group_id, {}).get(qq_id)
    async def _ensure_renderer(self):
        """首次使用时初始化渲染器（含字体下载）"""
        if self._font_ready:
            return
        font_path = await ensure_font()
        self.renderer = CardRenderer(font_path)
        self._font_ready = True
        logger.info(f"[SteamVerify] 卡片渲染器已初始化, font={font_path}")

    # ==========================================================
    #  核心事件监听（蓝本 = auto_approve_all 的写法，已验证能收到 request）
    # ==========================================================

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def on_event(self, event: AstrMessageEvent):
        """
        统一入口：拦截 aiocqhttp 的所有事件
        - post_type == "request" + request_type == "group" → 加群申请
        - post_type == "message" + message_type == "group" → 群消息（检查管理员引用回复）
        """
        raw = getattr(event.message_obj, "raw_message", None)
        if not isinstance(raw, dict):
            return

        post_type = raw.get("post_type")

        # ---- 加群申请 ----
        if post_type == "request" and raw.get("request_type") == "group":
            sub_type = raw.get("sub_type", "")
            if sub_type == "add":
                await self._on_join_request(event, raw)
            return  # request 事件不需要继续传播

        # ---- 群消息（管理员引用回复审批） ----
        if post_type == "message" and raw.get("message_type") == "group":
            await self._on_group_msg(event, raw)
            return

    # ==========================================================
    #  处理加群申请
    # ==========================================================

    async def _on_join_request(self, event: AstrMessageEvent, raw: dict):
        """收到 request.group.add 事件"""
        flag = str(raw.get("flag", ""))
        group_id = str(raw.get("group_id", ""))
        user_id = str(raw.get("user_id", ""))
        comment = str(raw.get("comment", ""))  # 验证消息（SteamID 在这里）

        if not flag or flag in self.processed_flags:
            return
        self.processed_flags.add(flag)

        # 群过滤
        monitored = [str(g) for g in self.config.get("monitored_groups", [])]
        if monitored and group_id not in monitored:
            return

        logger.info(
            f"[SteamVerify] 📨 加群申请: group={group_id} user={user_id} "
            f"comment='{comment}' flag={flag}"
        )

        # 拿 client
        if not isinstance(event, AiocqhttpMessageEvent):
            logger.error("[SteamVerify] 事件非 AiocqhttpMessageEvent，跳过")
            return
        client = event.bot

        if not self.steam_api:
            logger.error("[SteamVerify] steam_api_key 未配置，无法查询")
            return

        notify_gid = int(self.config.get("notify_group_id", "") or group_id)

        # 提取 SteamID
        steam64 = await extract_steam64(comment, self.steam_api)
        if not steam64:
            await client.send_group_msg(
                group_id=notify_gid,
                message=[
                    {
                        "type": "text",
                        "data": {
                            "text": (
                                f"📨 新的加群申请\n"
                                f"👤 QQ: {user_id}\n"
                                f"📝 验证消息: {comment}\n"
                                f"⚠ 未检测到有效 Steam ID，请手动审核"
                            )
                        },
                    }
                ],
            )
            return

        # 查 Steam 资料
        logger.info(f"[SteamVerify] 🔍 查询 Steam: {steam64}")
        profile = await self.steam_api.fetch_full_profile(steam64)

        if not profile.get("summary"):
            await client.send_group_msg(
                group_id=notify_gid,
                message=[
                    {
                        "type": "text",
                        "data": {
                            "text": (
                                f"📨 加群申请 | QQ: {user_id}\n"
                                f"Steam64: {steam64}\n"
                                f"❌ 无法获取 Steam 资料（无效ID或私密）"
                            )
                        },
                    }
                ],
            )
            return

        # ---- 去重检查 ----
        dup_qq = self._check_steam_dup(group_id, steam64)
        if dup_qq:
            await client.send_group_msg(
                group_id=notify_gid,
                message=[{"type": "text", "data": {"text":
                    f"⚠ 重复 Steam 账号\n"
                    f"👤 申请人 QQ: {user_id}\n"
                    f"🎮 Steam64: {steam64}\n"
                    f"❗ 该 Steam 已被群内 QQ {dup_qq} 绑定\n"
                    f"已自动拒绝，如需放行请手动处理"
                }}],
            )
            try:
                await client.set_group_add_request(
                    flag=flag, sub_type="add", approve=False,
                    reason="Steam账号已被群内其他成员绑定"
                )
                logger.info(f"[SteamVerify] 🚫 重复Steam拒绝: QQ{user_id} steam={steam64} 已绑定QQ{dup_qq}")
            except Exception as e:
                logger.error(f"[SteamVerify] 拒绝失败: {e}")
            return

        # ---- 画卡片（无论自动还是手动都要发） ----
        await self._ensure_renderer()
        avatar_url = profile["summary"].get("avatarfull") or profile["summary"].get("avatarmedium", "")
        avatar_img = await self.steam_api.download_image(avatar_url) if avatar_url else None
        card_bytes = self.renderer.render(profile, qq_id=user_id, avatar_img=avatar_img)
        card_b64 = base64.b64encode(card_bytes).decode()

        # ---- 判断条件 ----
        bans = profile.get("bans", {})
        owned_appids = {g.get("appid") for g in profile.get("games", [])}
        level = profile.get("level", 0)
        game_count = profile.get("game_count", 0)

        # VAC 检查
        if self.config.get("auto_reject_vac") and bans.get("VACBanned"):
            await client.send_group_msg(
                group_id=notify_gid,
                message=[{"type": "text", "data": {"text": f"📨 QQ {user_id}\n❌ 自动拒绝: VAC 封禁"}}],
            )
            await client.send_group_msg(
                group_id=notify_gid,
                message=[{"type": "image", "data": {"file": f"base64://{card_b64}"}}],
            )
            try:
                await client.set_group_add_request(flag=flag, sub_type="add", approve=False, reason="VAC封禁")
            except Exception as e:
                logger.error(f"[SteamVerify] 拒绝失败: {e}")
            return

        # 等级/游戏数检查
        min_lvl = self.config.get("min_steam_level", 0)
        min_g = self.config.get("min_games_count", 0)
        block_reason = None
        if min_lvl and level < min_lvl:
            block_reason = f"等级 {level} < {min_lvl}"
        if min_g and game_count < min_g:
            block_reason = f"游戏数 {game_count} < {min_g}"
        if block_reason:
            await client.send_group_msg(
                group_id=notify_gid,
                message=[{"type": "text", "data": {"text": f"📨 QQ {user_id}\n❌ 自动拒绝: {block_reason}"}}],
            )
            await client.send_group_msg(
                group_id=notify_gid,
                message=[{"type": "image", "data": {"file": f"base64://{card_b64}"}}],
            )
            try:
                await client.set_group_add_request(flag=flag, sub_type="add", approve=False, reason=block_reason)
            except Exception as e:
                logger.error(f"[SteamVerify] 拒绝失败: {e}")
            return

        # ---- 核心判断：是否拥有必需游戏 ----
        req_apps = self.config.get("required_game_appids", [4000])
        has_required = bool(req_apps and any(a in owned_appids for a in req_apps))
        # 资料私密时 owned_appids 为空，特殊处理
        is_private = profile.get("summary", {}).get("communityvisibilitystate", 1) != 3

        persona = profile["summary"].get("personaname", "")

        if has_required:
            # ===== 有必需游戏 → 自动通过 + 自动绑定 =====
            try:
                await client.set_group_add_request(flag=flag, sub_type="add", approve=True)
                self._bind(group_id, user_id, steam64)
                logger.info(f"[SteamVerify] ✅ 自动通过: QQ{user_id} steam={steam64}")
            except Exception as e:
                logger.error(f"[SteamVerify] 自动通过失败: {e}")

            await client.send_group_msg(
                group_id=notify_gid,
                message=[{"type": "text", "data": {"text":
                    f"📨 加群申请 - 已自动通过 ✅\n"
                    f"👤 QQ: {user_id}\n"
                    f"🎮 Steam: {persona} ({steam64})\n"
                    f"✅ 拥有必需游戏，已自动放行并绑定"
                }}],
            )
            await client.send_group_msg(
                group_id=notify_gid,
                message=[{"type": "image", "data": {"file": f"base64://{card_b64}"}}],
            )
            return

        # ===== 没有必需游戏 / 私密资料 → 手动审核 =====
        no_game_action = self.config.get("no_game_action", "manual")

        if no_game_action == "reject" and not is_private:
            # 配置为自动拒绝
            await client.send_group_msg(
                group_id=notify_gid,
                message=[{"type": "text", "data": {"text":
                    f"📨 QQ {user_id}\n❌ 自动拒绝: 未拥有必需游戏"
                }}],
            )
            await client.send_group_msg(
                group_id=notify_gid,
                message=[{"type": "image", "data": {"file": f"base64://{card_b64}"}}],
            )
            try:
                await client.set_group_add_request(flag=flag, sub_type="add", approve=False, reason="未拥有必需游戏")
            except Exception as e:
                logger.error(f"[SteamVerify] 拒绝失败: {e}")
            return

        # ===== 发卡片等管理员手动审批 =====
        reason_hint = "🔒 资料私密，无法检测游戏" if is_private else "⚠ 未检测到必需游戏"
        await client.send_group_msg(
            group_id=notify_gid,
            message=[{"type": "text", "data": {"text":
                f"📨 新的加群申请（需手动审核）\n"
                f"👤 QQ: {user_id} | 群: {group_id}\n"
                f"🎮 Steam: {persona} ({steam64})\n"
                f"{reason_hint}\n"
                f"💡 请引用下方卡片回复「同意」或「拒绝」"
            }}],
        )

        try:
            result = await client.send_group_msg(
                group_id=notify_gid,
                message=[{"type": "image", "data": {"file": f"base64://{card_b64}"}}],
            )
            msg_id = None
            if isinstance(result, dict):
                msg_id = str(result.get("message_id", ""))
            if msg_id and msg_id != "0" and msg_id != "None":
                self.pending[msg_id] = {
                    "flag": flag,
                    "sub_type": "add",
                    "group_id": group_id,
                    "user_id": user_id,
                    "steam64": steam64,
                    "timestamp": time.time(),
                }
                logger.info(f"[SteamVerify] 📋 卡片已发送 msg_id={msg_id}，等待手动审批")
            else:
                logger.warning(f"[SteamVerify] ⚠ 未获取到 msg_id: {result}")
        except Exception as e:
            logger.error(f"[SteamVerify] 发送卡片失败: {e}")

    # ==========================================================
    #  处理管理员引用回复（同意/拒绝）
    # ==========================================================

    async def _on_group_msg(self, event: AstrMessageEvent, raw: dict):
        """群消息：检测管理员引用卡片回复"""
        if not self.pending:
            return

        # 从 message 数组中找 reply 段和 text 段
        msg_chain = raw.get("message", [])
        if isinstance(msg_chain, str):
            return  # CQ 码字符串格式，先不处理

        reply_id = None
        text_content = ""

        for seg in msg_chain:
            if not isinstance(seg, dict):
                continue
            seg_type = seg.get("type", "")
            seg_data = seg.get("data", {})
            if seg_type == "reply":
                reply_id = str(seg_data.get("id", ""))
            elif seg_type == "text":
                text_content += seg_data.get("text", "")

        if not reply_id or reply_id not in self.pending:
            return

        # 检查发送者权限
        sender = raw.get("sender", {})
        role = sender.get("role", "member")
        if role not in ("admin", "owner"):
            return

        req = self.pending[reply_id]

        # 过期检查
        expire = self.config.get("card_expire_minutes", 1440)
        if time.time() - req["timestamp"] > expire * 60:
            del self.pending[reply_id]
            return

        # 解析指令
        text = text_content.strip().lower()
        approve = None
        reason = ""

        if text in ("同意", "通过", "批准", "approve", "yes", "y", "ok"):
            approve = True
        elif any(text.startswith(k) for k in ("拒绝", "驳回", "reject", "deny", "no", "n")):
            approve = False
            parts = text_content.strip().split(None, 1)
            if len(parts) > 1:
                reason = parts[1]
        else:
            return  # 不是审批指令

        # 拿 client
        if not isinstance(event, AiocqhttpMessageEvent):
            return
        client = event.bot

        # 执行审批
        try:
            await client.set_group_add_request(
                flag=req["flag"],
                sub_type=req["sub_type"],
                approve=approve,
                reason=reason,
            )
            result_text = "✅ 已同意" if approve else "❌ 已拒绝"
            if reason:
                result_text += f"（{reason}）"
            result_text += f"\n👤 QQ: {req['user_id']} | 🎮 Steam: {req['steam64']}"

            # 同意时自动绑定
            if approve:
                self._bind(req["group_id"], req["user_id"], req["steam64"])
                result_text += "\n🔗 已自动绑定 QQ ↔ Steam"

            gid = int(raw.get("group_id", req["group_id"]))
            await client.send_group_msg(
                group_id=gid,
                message=[{"type": "text", "data": {"text": result_text}}],
            )
            logger.info(f"[SteamVerify] {'同意' if approve else '拒绝'} QQ {req['user_id']}")
        except Exception as e:
            logger.error(f"[SteamVerify] 审批失败: {e}")
        finally:
            self.pending.pop(reply_id, None)

        event.stop_event()

    # ==========================================================
    #  工具命令
    # ==========================================================

    @filter.command("steam_lookup")
    async def cmd_lookup(self, event: AstrMessageEvent, steam_input: str = ""):
        """手动查 Steam 资料: /steam_lookup <SteamID或链接>"""
        if not self.steam_api:
            yield event.plain_result("❌ 未配置 steam_api_key")
            return
        if not steam_input:
            yield event.plain_result("用法: /steam_lookup <Steam64 / 自定义URL / STEAM_X:X:X>")
            return

        steam64 = await extract_steam64(steam_input, self.steam_api)
        if not steam64:
            yield event.plain_result(f"❌ 无法解析: {steam_input}")
            return

        yield event.plain_result(f"🔍 查询中 {steam64} ...")

        profile = await self.steam_api.fetch_full_profile(steam64)
        if not profile.get("summary"):
            yield event.plain_result("❌ 未找到该用户")
            return

        await self._ensure_renderer()
        av_url = profile["summary"].get("avatarfull") or profile["summary"].get("avatarmedium", "")
        av_img = await self.steam_api.download_image(av_url) if av_url else None
        card = self.renderer.render(profile, avatar_img=av_img)
        card_b64 = base64.b64encode(card).decode()

        # 用 client 发图（兼容性最好）
        if isinstance(event, AiocqhttpMessageEvent):
            client = event.bot
            gid = getattr(event.message_obj, "group_id", None) or (
                event.message_obj.raw_message.get("group_id") if isinstance(event.message_obj.raw_message, dict) else None
            )
            uid = getattr(event.message_obj, "user_id", None) or (
                event.message_obj.raw_message.get("user_id") if isinstance(event.message_obj.raw_message, dict) else None
            )
            if gid:
                await client.send_group_msg(
                    group_id=int(gid),
                    message=[{"type": "image", "data": {"file": f"base64://{card_b64}"}}],
                )
            elif uid:
                await client.send_private_msg(
                    user_id=int(uid),
                    message=[{"type": "image", "data": {"file": f"base64://{card_b64}"}}],
                )
            else:
                yield event.plain_result("⚠ 无法确定发送目标")
        else:
            yield event.plain_result("⚠ 当前仅支持 aiocqhttp 平台")

    @filter.command("steam_pending")
    async def cmd_pending(self, event: AstrMessageEvent):
        """查看待审核列表: /steam_pending"""
        if not self.pending:
            yield event.plain_result("📋 当前没有待审核的请求")
            return
        lines = ["📋 待审核列表:"]
        for mid, info in self.pending.items():
            mins = int((time.time() - info["timestamp"]) / 60)
            lines.append(
                f"  • QQ {info['user_id']} | Steam {info['steam64']} | "
                f"群 {info['group_id']} | {mins}分钟前"
            )
        yield event.plain_result("\n".join(lines))

    @filter.command("steam_status")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看插件状态: /steam_status"""
        api_ok = "✅" if self.steam_api else "❌"
        font_ok = "✅" if self._font_ready else "⏳ 首次使用时初始化"
        mon = self.config.get("monitored_groups", [])
        mon_txt = ", ".join(str(g) for g in mon) if mon else "全部群"

        yield event.plain_result(
            f"🔧 Steam 加群审核 插件状态\n"
            f"Steam API: {api_ok}\n"
            f"字体/渲染: {font_ok}\n"
            f"监控群: {mon_txt}\n"
            f"待审核: {len(self.pending)}\n"
            f"已处理: {len(self.processed_flags)}\n"
            f"绑定数据: {sum(len(v) for v in self.bindings.values())} 条 / {len(self.bindings)} 个群"
        )
    @filter.command("steam_binds")
    async def cmd_binds(self, event: AstrMessageEvent):
        """查看当前群的绑定列表: /steam_binds"""
        raw = getattr(event.message_obj, "raw_message", None)
        gid = str(raw.get("group_id", "")) if isinstance(raw, dict) else ""
        if not gid:
            yield event.plain_result("⚠ 请在群内使用")
            return
        grp = self.bindings.get(gid, {})
        if not grp:
            yield event.plain_result(f"📋 群 {gid} 暂无绑定记录")
            return
        lines = [f"📋 群 {gid} 绑定列表 ({len(grp)}人):"]
        for qq, sid in grp.items():
            lines.append(f"  QQ {qq} → {sid}")
        # 太长就截断
        text = "\n".join(lines[:50])
        if len(grp) > 50:
            text += f"\n... 等共 {len(grp)} 条"
        yield event.plain_result(text)

    @filter.command("steam_unbind")
    async def cmd_unbind(self, event: AstrMessageEvent, qq_id: str = ""):
        """解绑某人: /steam_unbind <QQ号>"""
        raw = getattr(event.message_obj, "raw_message", None)
        if not isinstance(raw, dict):
            return
        # 权限检查
        sender_role = raw.get("sender", {}).get("role", "member")
        if sender_role not in ("admin", "owner"):
            yield event.plain_result("⚠ 仅管理员可操作")
            return
        gid = str(raw.get("group_id", ""))
        if not gid:
            yield event.plain_result("⚠ 请在群内使用")
            return
        if not qq_id:
            yield event.plain_result("用法: /steam_unbind <QQ号>")
            return
        qq_id = qq_id.strip()
        old = self._get_binding(gid, qq_id)
        if old:
            self._unbind(gid, qq_id)
            yield event.plain_result(f"✅ 已解绑 QQ {qq_id} (原绑定: {old})")
        else:
            yield event.plain_result(f"❌ QQ {qq_id} 在本群无绑定记录")

    @filter.command("steam_bind")
    async def cmd_manual_bind(self, event: AstrMessageEvent, args: str = ""):
        """手动绑定: /steam_bind <QQ号> <SteamID>"""
        raw = getattr(event.message_obj, "raw_message", None)
        if not isinstance(raw, dict):
            return
        sender_role = raw.get("sender", {}).get("role", "member")
        if sender_role not in ("admin", "owner"):
            yield event.plain_result("⚠ 仅管理员可操作")
            return
        gid = str(raw.get("group_id", ""))
        if not gid:
            yield event.plain_result("⚠ 请在群内使用")
            return
        parts = args.strip().split()
        if len(parts) < 2:
            yield event.plain_result("用法: /steam_bind <QQ号> <SteamID或链接>")
            return
        qq_id = parts[0]
        steam_input = parts[1]
        if not self.steam_api:
            yield event.plain_result("❌ 未配置 steam_api_key")
            return
        steam64 = await extract_steam64(steam_input, self.steam_api)
        if not steam64:
            yield event.plain_result(f"❌ 无法解析 Steam ID: {steam_input}")
            return
        dup = self._check_steam_dup(gid, steam64)
        if dup and dup != qq_id:
            yield event.plain_result(f"⚠ Steam {steam64} 已被 QQ {dup} 绑定，请先 /steam_unbind {dup}")
            return
        self._bind(gid, qq_id, steam64)
        yield event.plain_result(f"✅ 已绑定: QQ {qq_id} → {steam64}")

    @filter.command("steam_check")
    async def cmd_check(self, event: AstrMessageEvent, qq_id: str = ""):
        """查某人绑定: /steam_check <QQ号>"""
        raw = getattr(event.message_obj, "raw_message", None)
        gid = str(raw.get("group_id", "")) if isinstance(raw, dict) else ""
        if not gid:
            yield event.plain_result("⚠ 请在群内使用")
            return
        if not qq_id:
            yield event.plain_result("用法: /steam_check <QQ号>")
            return
        sid = self._get_binding(gid, qq_id.strip())
        if sid:
            yield event.plain_result(f"🔗 QQ {qq_id} → Steam64: {sid}\n🔗 https://steamcommunity.com/profiles/{sid}")
        else:
            yield event.plain_result(f"❌ QQ {qq_id} 在本群无绑定记录")
    async def terminate(self):
        self.pending.clear()
        self.processed_flags.clear()
        logger.info("[SteamVerify] 插件已卸载")
