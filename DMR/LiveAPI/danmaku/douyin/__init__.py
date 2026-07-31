# 抖音的弹幕录制参考了 https://github.com/LyzenX/DouyinLiveRecorder 和 https://github.com/YunzhiYike/live-tool
# 抖音的弹幕录制参考了 https://github.com/biliup/biliup/blob/master/biliup/plugins/Danmaku/douyin.py
# 2024.6.23 抖音的弹幕录制参考了 https://github.com/SecPhases/DanmakuRender/commit/fd6d85afede5845274ad699bbcdf5db98e68977e

from datetime import datetime
import threading
import asyncio
import gzip
import re
import time
import re
import requests
import urllib
import json
import logging
import random
import websocket
from google.protobuf import json_format
from concurrent.futures import ThreadPoolExecutor, as_completed

from DMR.LiveAPI.douyin import douyin_utils
from DMR.utils import split_url, SimpleDanmaku, GiftDanmaku, EntryDanmaku
from DMR.utils.douyin_login import ensure_douyin_cookies
from .dy_pb2 import PushFrame, Response, ChatMessage, GiftMessage, MemberMessage, LightGiftMessage
from .utils import DouyinDanmakuUtils
from .gift_map import get_gift_catalog
import aiohttp

logger = logging.getLogger(__name__)


class Douyin:
    heartbeat = b':\x02hb'
    heartbeatInterval = 10

    def __init__(self, douyin_dm_cookies:str=None) -> None:
        cookies = None
        try:
            cookies, cookie_path = ensure_douyin_cookies(douyin_dm_cookies)
            if cookies:
                where = cookie_path or '配置字符串'
                logger.info(f'正在使用已登录的抖音 cookies ({where}) 获取弹幕.')
            elif cookie_path:
                logger.info('未使用抖音登录 cookies，将以游客模式获取弹幕.')
        except Exception as e:
            logger.exception(f'准备抖音cookies失败: {e}, 使用游客模式.')
            cookies = None
        self.headers = douyin_utils.get_headers(extra_cookies=cookies)

    async def _refresh_gift_catalog(self, session, room_id_str: str) -> None:
        """Best-effort fetch of room gift list into local id->name map."""
        catalog = get_gift_catalog()
        try:
            gift_url = douyin_utils.build_request_url(
                f'https://live.douyin.com/webcast/gift/list/?room_id={room_id_str}&live_id=1&device_platform=web'
            )
            async with session.get(gift_url, headers=self.headers, timeout=5) as resp:
                if resp.status != 200:
                    return
                payload = json.loads(await resp.text())
                n = catalog.update_from_api_payload(payload)
                if n:
                    logger.debug(f'抖音礼物映射已更新 {n} 条 (room_id={room_id_str})')
        except Exception as e:
            logger.debug(f'拉取抖音礼物列表失败: {e}')

    async def get_ws_info(self, url, **kwargs):
        async with aiohttp.ClientSession() as session:
            _, room_id = split_url(url)
            async with session.get(
                    douyin_utils.build_request_url(f"https://live.douyin.com/webcast/room/web/enter/?web_rid={room_id}"),
                    headers=self.headers, timeout=5) as resp:
                room_info = json.loads(await resp.text())['data']['data'][0]
                USER_UNIQUE_ID = DouyinDanmakuUtils.get_user_unique_id()
                VERSION_CODE = 180800 # https://lf-cdn-tos.bytescm.com/obj/static/webcast/douyin_live/7697.782665f8.js -> a.ry
                WEBCAST_SDK_VERSION = "1.0.15.0" # keep in sync with current douyin web webcast SDK
                # logger.info(f"user_unique_id: {USER_UNIQUE_ID}")
                await self._refresh_gift_catalog(session, room_info['id_str'])
                sig_params = {
                    "live_id": "1",
                    "aid": "6383",
                    "version_code": VERSION_CODE,
                    "webcast_sdk_version": WEBCAST_SDK_VERSION,
                    "room_id": room_info['id_str'],
                    "sub_room_id": "",
                    "sub_channel_id": "",
                    "did_rule": "3",
                    "user_unique_id": USER_UNIQUE_ID,
                    "device_platform": "web",
                    "device_type": "",
                    "ac": "",
                    "identity": "audience"
                }
                try:
                    signature = DouyinDanmakuUtils.get_signature(DouyinDanmakuUtils.get_x_ms_stub(sig_params))
                except Exception as e:
                    signature = 0
                    logger.exception('获取抖音弹幕签名失败:')
                    logger.exception(e)
                # logger.info(f"signature: {signature}")
                webcast5_params = {
                    "room_id": room_info['id_str'],
                    "compress": 'gzip',
                    # "app_name": "douyin_web",
                    "version_code": VERSION_CODE,
                    "webcast_sdk_version": WEBCAST_SDK_VERSION,
                    "update_version_code": WEBCAST_SDK_VERSION,
                    # "cookie_enabled": "true",
                    # "screen_width": "1920",
                    # "screen_height": "1080",
                    # "browser_online": "true",
                    # "tz_name": "Asia/Shanghai",
                    # "cursor": "t-1718899404570_r-1_d-1_u-1_h-7382616636258522175",
                    # "internal_ext": "internal_src:dim|wss_push_room_id:7382580251462732598|wss_push_did:7344670681018189347|first_req_ms:1718899404493|fetch_time:1718899404570|seq:1|wss_info:0-1718899404570-0-0|wrds_v:7382616716703957597",
                    # "host": "https://live.douyin.com",
                    "live_id": "1",
                    "did_rule": "3",
                    # "endpoint": "live_pc",
                    # "support_wrds": "1",
                    "user_unique_id": USER_UNIQUE_ID,
                    # "im_path": "/webcast/im/fetch/",
                    "identity": "audience",
                    # "need_persist_msg_count": "15",
                    # "insert_task_id": "",
                    # "live_reason": "",
                    # "heartbeatDuration": "0",
                    "signature": signature,
                }
                wss_url = f"wss://webcast5-ws-web-lf.douyin.com/webcast/im/push/v2/?{'&'.join([f'{k}={v}' for k, v in webcast5_params.items()])}"
                url = douyin_utils.build_request_url(wss_url)
                return url, []

    @classmethod
    def _gift_from_light_message(cls, data: dict, now: float):
        """Build GiftDanmaku from WebcastLightGiftMessage (sparse vs full GiftMessage)."""
        catalog = get_gift_catalog()
        common = data.get('common') or {}
        user = common.get('user') or {}
        gift_info = data.get('giftInfo') or {}
        gift_struct = data.get('giftStruct') or {}

        name = user.get('nickName') or '观众'
        gift_id = gift_info.get('giftId') or gift_struct.get('id')
        describe = common.get('describe')
        diamond_hint = gift_info.get('diamondCount')
        if diamond_hint is None:
            diamond_hint = gift_struct.get('diamondCount')
        resolved = catalog.resolve_light_gift(
            gift_id=gift_id,
            gift_struct_name=gift_struct.get('name'),
            describe=describe,
            diamond_hint=diamond_hint,
        )
        gift_name = resolved['name']
        diamond_count = str(resolved['diamond_count'])
        gift_count = (
            data.get('count')
            or data.get('repeatCount')
            or data.get('comboCount')
            or 1
        )
        content = describe or f"{name}送给主播{gift_count}个{gift_name}每个价值抖币{diamond_count}"
        catalog.save()

        return GiftDanmaku(
            timestamp=now,
            uname=name,
            content=content,
            gift_name=gift_name,
            gift_count=gift_count,
            gift_price=diamond_count,
            price_unit='抖币',
            dtype='gift',
            color='ffffff'
        )

    @classmethod
    def decode_msg(cls, data):
        wss_package = PushFrame()
        wss_package.ParseFromString(data)
        log_id = wss_package.logId
        decompressed = gzip.decompress(wss_package.payload)
        payload_package = Response()
        payload_package.ParseFromString(decompressed)

        ack = None
        if payload_package.needAck:
            obj = PushFrame()
            obj.payloadType = 'ack'
            obj.logId = log_id
            obj.payloadType = payload_package.internalExt
            ack = obj.SerializeToString()
        
        msgs = []
        catalog = get_gift_catalog()
        for msg in payload_package.messagesList:
            now = datetime.now().timestamp()
            if msg.method == 'WebcastChatMessage':
                chatMessage = ChatMessage()
                chatMessage.ParseFromString(msg.payload)
                data = json_format.MessageToDict(chatMessage, preserving_proto_field_name=True)
                name = data['user']['nickName']
                content = data['content']
                msg_dict = SimpleDanmaku(
                    timestamp=now,
                    uname=name,
                    content=content,
                    dtype='danmaku',
                    color='ffffff'
                )
                # msg_dict = {"timestamp": now, "name": name, "content": content, "msg_type": "danmaku", "color": "ffffff"}
                # print(msg_dict)
            elif msg.method == 'WebcastMemberMessage':
                memberMessage = MemberMessage()
                memberMessage.ParseFromString(msg.payload)
                data = json_format.MessageToDict(memberMessage, preserving_proto_field_name=True)
                name = data['user']['nickName']
                msg_dict = EntryDanmaku(
                    timestamp=now,
                    uname=name,
                    content=f"{name}来了",
                    dtype='entry',
                    color='ffffff'
                )
            elif msg.method == 'WebcastGiftMessage':
                giftMessage = GiftMessage()
                giftMessage.ParseFromString(msg.payload)
                data = json_format.MessageToDict(giftMessage, preserving_proto_field_name=True)
                if 'combo' in data['gift'] and not 'repeatEnd' in data:
                  continue
                name = data['user']['nickName']
                diamondCount=str(data['gift']['diamondCount'])
                gift_id = data.get('giftId') or data.get('gift', {}).get('id')
                gift_name = data['gift']['name']
                catalog.learn(gift_id, gift_name, data['gift'].get('diamondCount'))
                catalog.save()
                msg_dict = GiftDanmaku(
                    timestamp=now,
                    uname=name,
                    content=f"{name}送给主播{data['repeatCount']}个{gift_name}每个价值抖币{diamondCount}",
                    gift_name=gift_name,
                    gift_count=data['repeatCount'],
                    gift_price=diamondCount,
                    price_unit='抖币',
                    dtype='gift',
                    color='ffffff'
                )
            elif msg.method == 'WebcastLightGiftMessage':
                # Fallback when room only pushes light gifts (often without login)
                lightGiftMessage = LightGiftMessage()
                lightGiftMessage.ParseFromString(msg.payload)
                data = json_format.MessageToDict(lightGiftMessage, preserving_proto_field_name=True)
                msg_dict = cls._gift_from_light_message(data, now)
            else:
                msg_dict = {"timestamp": now, "name": "", "content": "", "msg_type": "other", "raw_data": msg}
            msgs.append(msg_dict)
        
        return msgs, ack
