"""Local Douyin gift id/name/price catalog for LightGiftMessage fallback.

Recent public sources publish gift *names + diamond prices* (e.g. 2026-07 price
tables), but not a complete numeric id catalog. This module therefore combines:

1. A small verified id -> name seed (from captured WebcastGiftMessage samples)
2. A 2026 name -> diamond price index (game773 / ali213 Douyin gift price lists, 2026-07)
3. Runtime learning from full GiftMessage / gift list API into gift_map.learned.json
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_DIR = os.path.dirname(os.path.realpath(__file__))
_SEED_PATH = os.path.join(_DIR, 'gift_map.json')
_LEARNED_PATH = os.path.join(_DIR, 'gift_map.learned.json')

# Verified / commonly observed webcast gift ids (Douyin live captures).
# Prefer learning over expanding this with unverified TikTok-only ids.
_DEFAULT_GIFTS: Dict[str, Dict[str, Any]] = {
    '3242': {'name': '入团卡', 'diamond_count': 1},  # captured WebcastGiftMessage
    '5655': {'name': '玫瑰', 'diamond_count': 1},    # widely observed webcast rose id
}

# Name -> diamond from Douyin gift price tables updated 2026-07
# Sources: game773.com/info/134484.html , m.ali213.net/news/gl2402/1323805.html
_NAME_TO_DIAMOND_2026: Dict[str, int] = {
    '嘉年华': 30000,
    '梦幻城堡': 28888,
    '浪漫马车': 28888,
    '抖音飞艇': 20000,
    '无尽浪漫': 19999,
    '一路有你': 17999,
    '云中秘境': 13140,
    '蝶·化蝶飞': 10999,
    '抖音1号': 10001,
    '为爱启航': 10001,
    '情定三生': 9666,
    '金鳞化龙': 9000,
    '跨时空之恋': 9000,
    '真爱永恒': 8999,
    '新春狂欢城': 8888,
    '梦回紫禁城': 8666,
    '摩天大厦': 8222,
    '云霄大厦': 7888,
    '星级玫瑰': 7500,
    '等待花开': 7000,
    '蝶·寄相思': 6800,
    '团团圆圆': 6666,
    '月下瀑布': 6666,
    '天空之境': 6399,
    '豪华邮轮': 6000,
    '火龙爆发': 5000,
    '华灯初上': 5000,
    '壁上飞仙': 4999,
    '福佑万家': 4888,
    '奇幻花潮': 4520,
    '星河相望': 4520,
    '真情萌动': 4433,
    '心动丘比特': 4321,
    '海上生明月': 4166,
    '奏响人生': 3666,
    '薰衣草庄园': 3300,
    '私人飞机': 3000,
    '冰冻战车': 3000,
    '传送门': 2999,
    '直升机': 2999,
    '花海泛舟': 2800,
    '奇幻八音盒': 2399,
    '龙珠纳福': 2388,
    '开运醒狮': 2024,
    '浪漫恋人': 1999,
    '单车恋人': 1899,
    '红墙白雪': 1888,
    '炫彩射击': 1888,
    '点亮孤单': 1800,
    '蝶·比翼鸟': 1700,
    '浪漫营地': 1699,
    '花落长亭': 1588,
    '镜中奇缘': 1500,
    '羊羔崽崽': 1488,
    '繁花秘语': 1314,
    '保时捷': 1200,
    '蜜蜂叮叮': 1000,
    '为你而歌': 999,
    '纸短情长': 921,
    '璀璨舞台': 899,
    '掌上明珠': 888,
    '怦然心动': 766,
    '蝶·书中情': 750,
    '日出相伴': 726,
    '万象烟花': 688,
    '环球旅行车': 650,
    '灵龙现世': 600,
    '浪漫花火': 599,
    '娶你回家': 599,
    '热气球': 520,
    '真的爱你': 520,
    '永生花': 520,
    '花开烂漫': 466,
    '真爱玫瑰': 366,
    '一束花开': 366,
    '比心兔兔': 299,
    '真爱表白': 299,
    'ONE礼挑一': 299,
    '爱的守护': 299,
    '蝶·连理枝': 280,
    '星星点灯': 268,
    '一点心意': 266,
    '比心': 199,
    '为你举牌': 199,
    '礼花筒': 199,
    '拳拳出击': 199,
    '多喝热水': 126,
    '龙抬头': 99,
    '爱的小熊': 99,
    'Thuglife': 99,
    '心动玫瑰': 99,
    '龙的传人': 99,
    '爱的纸鹤': 99,
    '捏捏小脸': 99,
    '恋爱脑': 99,
    '黑凤梨': 99,
    '闪耀星辰': 99,
    '为你弹奏': 99,
    '黄桃罐头': 99,
    '荧光棒': 99,
    '亲吻': 99,
    '爱你呦': 52,
    '爱你哟': 52,
    '送你花花': 49,
    '加油鸭': 15,
    '鲜花': 10,
    '棒棒糖': 9,
    '大啤酒': 2,
    '你最好看': 2,
    '小心心': 1,
    '人气票': 1,
    '玫瑰': 1,
    '贺新春': 1,
    '抖音': 1,
    '称心如意': 1,
    '入团卡': 1,
    '粉丝团灯牌': 1,
    '点亮粉丝团': 1,
}

_DESCRIBE_GIFT_RE = re.compile(
    r'(?:送给主播|送出|送了)\s*(?P<count>\d+)\s*个\s*(?P<name>.+?)\s*$'
)
_DESCRIBE_GIFT_RE2 = re.compile(
    r'(?:送给主播|送出|送了)\s*(?P<name>.+?)(?:\s*x\s*(?P<count>\d+))?\s*$'
)


def parse_gift_from_describe(describe: str) -> Tuple[Optional[str], Optional[int]]:
    """Extract gift name/count from common.describe text when present."""
    if not describe:
        return None, None
    text = describe.strip()
    # Drop leading "nickname:" / "nickname：" prefixes
    if '：' in text:
        text = text.split('：', 1)[-1].strip()
    elif ':' in text:
        text = text.split(':', 1)[-1].strip()
    m = _DESCRIBE_GIFT_RE.search(text)
    if m:
        name = (m.group('name') or '').strip()
        try:
            count = int(m.group('count'))
        except (TypeError, ValueError):
            count = None
        return (name or None), count
    m = _DESCRIBE_GIFT_RE2.search(text)
    if m:
        name = (m.group('name') or '').strip()
        count = None
        if m.group('count'):
            try:
                count = int(m.group('count'))
            except (TypeError, ValueError):
                count = None
        return (name or None), count
    return None, None


class DouyinGiftCatalog:
    """In-memory gift map with verified ids, 2026 price index, and learned cache."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._gifts: Dict[str, Dict[str, Any]] = {}
        self._name_to_diamond: Dict[str, int] = dict(_NAME_TO_DIAMOND_2026)
        self._dirty = False
        self._load()

    def _load(self) -> None:
        for gid, info in _DEFAULT_GIFTS.items():
            self._merge_entry(str(gid), info, persist=False)
        for path in (_SEED_PATH, _LEARNED_PATH):
            if not os.path.isfile(path):
                continue
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    for gid, info in data.items():
                        if str(gid).startswith('_'):
                            continue
                        self._merge_entry(str(gid), info, persist=False)
            except Exception as e:
                logger.debug(f'加载礼物映射失败 {path}: {e}')

    def _merge_entry(self, gift_id: str, info: Any, persist: bool = True) -> None:
        if not gift_id or gift_id in ('0', 'None'):
            return
        if not isinstance(info, dict):
            return
        name = info.get('name') or info.get('gift_name')
        diamond = info.get('diamond_count', info.get('diamondCount'))
        if name and diamond is None:
            diamond = self._name_to_diamond.get(str(name))
        with self._lock:
            cur = self._gifts.get(gift_id, {})
            updated = dict(cur)
            if name:
                if not updated.get('name') or str(updated.get('name', '')).startswith('礼物'):
                    updated['name'] = str(name)
                elif not str(name).startswith('礼物'):
                    updated['name'] = str(name)
            if diamond is not None:
                try:
                    updated['diamond_count'] = int(diamond)
                except (TypeError, ValueError):
                    pass
            if updated.get('name') and updated != cur:
                self._gifts[gift_id] = updated
                if persist:
                    self._dirty = True
            if updated.get('name') and updated.get('diamond_count') is not None:
                self._name_to_diamond.setdefault(updated['name'], int(updated['diamond_count']))

    def learn(self, gift_id: Any, name: str = None, diamond_count: Any = None) -> None:
        if gift_id is None:
            return
        info = {}
        if name:
            info['name'] = name
            if diamond_count is None:
                diamond_count = self._name_to_diamond.get(str(name))
        if diamond_count is not None:
            info['diamond_count'] = diamond_count
        if info:
            self._merge_entry(str(gift_id), info, persist=True)

    def learn_many(self, gifts: list) -> int:
        n = 0
        for g in gifts or []:
            if not isinstance(g, dict):
                continue
            gid = g.get('id', g.get('gift_id', g.get('giftId')))
            name = g.get('name', g.get('gift_name'))
            diamond = g.get('diamond_count', g.get('diamondCount'))
            if gid is None:
                continue
            self.learn(gid, name, diamond)
            n += 1
        if n:
            self.save()
        return n

    def resolve(self, gift_id: Any) -> Optional[Dict[str, Any]]:
        if gift_id is None:
            return None
        with self._lock:
            return self._gifts.get(str(gift_id))

    def lookup_name(self, gift_id: Any, default: str = None) -> str:
        info = self.resolve(gift_id)
        if info and info.get('name'):
            return info['name']
        if default is not None:
            return default
        return f'礼物{gift_id}' if gift_id is not None else '礼物'

    def lookup_diamond(self, gift_id: Any, default: Any = 0) -> Any:
        info = self.resolve(gift_id)
        if info and info.get('diamond_count') is not None:
            return info['diamond_count']
        return default

    def lookup_diamond_by_name(self, name: str, default: Any = None) -> Any:
        if not name:
            return default
        with self._lock:
            if name in self._name_to_diamond:
                return self._name_to_diamond[name]
        return default

    def resolve_light_gift(
        self,
        gift_id: Any = None,
        gift_struct_name: str = None,
        describe: str = None,
        diamond_hint: Any = None,
    ) -> Dict[str, Any]:
        """Best-effort name/diamond resolution for sparse LightGift payloads."""
        mapped = self.resolve(gift_id) or {}
        describe_name, _ = parse_gift_from_describe(describe or '')
        gift_name = (
            gift_struct_name
            or mapped.get('name')
            or describe_name
            or (f'礼物{gift_id}' if gift_id is not None else '礼物')
        )
        diamond = diamond_hint
        if diamond is None:
            diamond = mapped.get('diamond_count')
        if diamond is None:
            diamond = self.lookup_diamond_by_name(gift_name, 0)
        if gift_id and gift_name and not str(gift_name).startswith('礼物'):
            self.learn(gift_id, gift_name, diamond)
        return {
            'gift_id': gift_id,
            'name': gift_name,
            'diamond_count': diamond if diamond is not None else 0,
        }

    def save(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            payload = dict(self._gifts)
            self._dirty = False
        try:
            with open(_LEARNED_PATH, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
        except Exception as e:
            logger.debug(f'保存礼物映射失败: {e}')
            with self._lock:
                self._dirty = True

    def update_from_api_payload(self, payload: dict) -> int:
        """Parse Douyin gift/list-style JSON into the catalog."""
        gifts = []
        data = payload.get('data', payload) if isinstance(payload, dict) else {}
        if isinstance(data, dict):
            if isinstance(data.get('gifts'), list):
                gifts.extend(data['gifts'])
            pages = data.get('pages') or data.get('gift_pages') or []
            if isinstance(pages, list):
                for page in pages:
                    if isinstance(page, dict) and isinstance(page.get('gifts'), list):
                        gifts.extend(page['gifts'])
            for key in ('gift_list', 'gifts_info', 'room_gift_list'):
                val = data.get(key)
                if isinstance(val, list):
                    gifts.extend(val)
                elif isinstance(val, dict) and isinstance(val.get('gifts'), list):
                    gifts.extend(val['gifts'])
        return self.learn_many(gifts)


_catalog: Optional[DouyinGiftCatalog] = None
_catalog_lock = threading.Lock()


def get_gift_catalog() -> DouyinGiftCatalog:
    global _catalog
    if _catalog is None:
        with _catalog_lock:
            if _catalog is None:
                _catalog = DouyinGiftCatalog()
    return _catalog
