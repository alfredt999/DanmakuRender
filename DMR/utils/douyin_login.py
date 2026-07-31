"""Douyin (live.douyin.com) QR login, cookie persist, validate and soft-refresh."""

from __future__ import annotations

import base64
import json
import logging
import os
import random
import string
import sys
import threading
import time
from typing import Any, Dict, Optional, Tuple

import requests

from DMR.utils.utils import random_user_agent

logger = logging.getLogger(__name__)

DEFAULT_COOKIE_PATH = '.login_info/douyin_dm_cookies.json'
SKIP_VALUES = frozenset({'none', 'null', 'skip', '~'})

_LOGIN_MARKERS = ('sessionid', 'sessionid_ss', 'sid_tt')
_REFRESH_KEEP = (
    'sessionid', 'sessionid_ss', 'sid_tt', 'sid_guard', 'uid_tt', 'uid_tt_ss',
    'ttwid', 'passport_csrf_token', 'passport_csrf_token_default',
    'passport_auth_status', 'passport_auth_status_ss', 'odin_tt', 'msToken',
    '__ac_nonce', '__ac_signature', 's_v_web_id', 'sid_ucp_v1', 'ssid_ucp_v1',
)

_login_lock = threading.Lock()
_skip_this_process = False

_SERVICE = 'https://www.douyin.com'
_NEXT = 'https://www.douyin.com'
_AID = '6383'


def _is_skip_config(value: Any) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() in SKIP_VALUES


def _normalize_path(cookies: Optional[str]) -> Optional[str]:
    """Return cookie JSON path, or None when login should be skipped."""
    if _is_skip_config(cookies):
        return None
    if not cookies or str(cookies).strip() in ('', '~'):
        return DEFAULT_COOKIE_PATH
    path = str(cookies).strip()
    if path.endswith('.json'):
        return path
    # Raw cookie string — caller should parse; no auto-login path
    return path


def _interactive() -> bool:
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except Exception:
        return False


def _generate_fp() -> str:
    chars = string.ascii_letters + string.digits
    return 'verify_' + ''.join(random.choice(chars) for _ in range(32))


def _headers(referer: str = _SERVICE + '/') -> dict:
    return {
        'User-Agent': random_user_agent(),
        'Referer': referer,
        'Accept': 'application/json, text/plain, */*',
    }


def _session_cookies_dict(session: requests.Session) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for c in session.cookies:
        if c.value:
            out[c.name] = c.value
    return out


def load_cookie_dict(path: str) -> Dict[str, str]:
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError('cookie file must be a JSON object')
    return {str(k): str(v) for k, v in data.items() if v is not None and not str(k).startswith('_')}


def save_cookie_dict(path: str, cookies: Dict[str, str]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or '.', exist_ok=True)
    payload = {k: v for k, v in cookies.items() if v}
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)


def has_login_markers(cookies: Dict[str, str]) -> bool:
    return any(cookies.get(k) for k in _LOGIN_MARKERS)


def parse_sid_guard_expiry(cookies: Dict[str, str]) -> Optional[float]:
    """Return unix expiry from sid_guard when present: id|login_ts|ttl|..."""
    guard = cookies.get('sid_guard') or ''
    parts = guard.split('|')
    if len(parts) < 3:
        return None
    try:
        login_ts = int(parts[1])
        ttl = int(parts[2])
        return float(login_ts + ttl)
    except (TypeError, ValueError):
        return None


def cookies_look_expired(cookies: Dict[str, str], skew: int = 300) -> bool:
    exp = parse_sid_guard_expiry(cookies)
    if exp is None:
        return False
    return time.time() >= (exp - skew)


def validate_douyin_cookies(cookies: Dict[str, str], timeout: float = 12) -> bool:
    """Best-effort online check that session cookies still work."""
    if not has_login_markers(cookies):
        return False
    if cookies_look_expired(cookies):
        return False
    session = requests.Session()
    for k, v in cookies.items():
        session.cookies.set(k, v, domain='.douyin.com')
    headers = _headers('https://live.douyin.com/')
    try:
        # Soft probe: logged-in pages/APIs usually keep or echo session cookies.
        r = session.get('https://live.douyin.com/', headers=headers, timeout=timeout, allow_redirects=True)
        merged = dict(cookies)
        merged.update(_session_cookies_dict(session))
        if not has_login_markers(merged):
            return False
        # Secondary: passport web check (may 4xx when blocked; treat markers as enough)
        try:
            r2 = session.get(
                'https://www.douyin.com/passport/account/info/v2/',
                headers=_headers('https://www.douyin.com/'),
                timeout=timeout,
            )
            if r2.status_code == 200:
                try:
                    body = r2.json()
                    # Common shapes: status_code==0 or data.user present
                    if isinstance(body, dict):
                        if body.get('status_code') in (0, '0'):
                            return True
                        if body.get('data'):
                            return True
                except Exception:
                    pass
        except Exception:
            pass
        return has_login_markers(merged) and r.status_code < 500
    except Exception as e:
        logger.debug(f'校验抖音cookies失败: {e}')
        return has_login_markers(cookies) and not cookies_look_expired(cookies)


def refresh_douyin_cookies(cookies: Dict[str, str], timeout: float = 15) -> Dict[str, str]:
    """Visit live/www to refresh short-lived cookies (ttwid etc.) when possible."""
    if not cookies:
        return {}
    session = requests.Session()
    for k, v in cookies.items():
        session.cookies.set(k, v, domain='.douyin.com')
    headers = _headers('https://live.douyin.com/')
    try:
        session.get('https://live.douyin.com/', headers=headers, timeout=timeout)
        session.get('https://www.douyin.com/', headers=_headers('https://www.douyin.com/'), timeout=timeout)
    except Exception as e:
        logger.debug(f'刷新抖音cookies请求失败: {e}')
    merged = dict(cookies)
    for k, v in _session_cookies_dict(session).items():
        if k in _REFRESH_KEEP or k in cookies:
            merged[k] = v
    return merged


def _is_ssh_or_headless() -> bool:
    if os.environ.get('SSH_CONNECTION') or os.environ.get('SSH_CLIENT') or os.environ.get('SSH_TTY'):
        return True
    if sys.platform.startswith('linux') and not os.environ.get('DISPLAY'):
        return True
    return False


def _print_ascii_qr(data: str) -> bool:
    """Render QR as ASCII/Unicode blocks for SSH and normal terminals."""
    try:
        import qrcode
    except ImportError:
        print('未安装 qrcode，无法在终端绘制二维码。请执行: pip install qrcode')
        return False
    try:
        qr = qrcode.QRCode(border=1, box_size=1)
        qr.add_data(data)
        qr.make(fit=True)
        matrix = qr.get_matrix()
        # Half-block unicode: denser and usually scannable in SSH/PuTTY/Windows Terminal
        lines = []
        row = 0
        while row < len(matrix):
            line = []
            top = matrix[row]
            bottom = matrix[row + 1] if row + 1 < len(matrix) else [False] * len(top)
            for t, b in zip(top, bottom):
                if t and b:
                    line.append('█')
                elif t and not b:
                    line.append('▀')
                elif not t and b:
                    line.append('▄')
                else:
                    line.append(' ')
            lines.append(''.join(line))
            row += 2
        print('')
        print('\n'.join(lines))
        print('')
        return True
    except Exception as e:
        logger.debug(f'终端二维码绘制失败: {e}')
        try:
            qr = qrcode.QRCode(border=1)
            qr.add_data(data)
            qr.make(fit=True)
            qr.print_ascii(invert=True)
            return True
        except Exception:
            return False


def _show_qr_image(png_bytes: bytes, save_path: str) -> None:
    """Save PNG; open GUI viewer only when not on SSH/headless."""
    os.makedirs(os.path.dirname(os.path.abspath(save_path)) or '.', exist_ok=True)
    with open(save_path, 'wb') as f:
        f.write(png_bytes)
    print(f'二维码图片已保存到: {save_path}')
    if _is_ssh_or_headless():
        return
    try:
        from PIL import Image
        from io import BytesIO

        def _show():
            try:
                Image.open(BytesIO(png_bytes)).show()
            except Exception:
                pass

        threading.Thread(target=_show, daemon=True).start()
        return
    except Exception:
        pass
    try:
        if sys.platform.startswith('win'):
            os.startfile(save_path)  # type: ignore[attr-defined]
        elif sys.platform == 'darwin':
            os.system(f'open "{save_path}"')
        else:
            os.system(f'xdg-open "{save_path}" >/dev/null 2>&1 &')
    except Exception as e:
        logger.debug(f'自动打开二维码失败: {e}')


def _status_norm(status: Any) -> str:
    return str(status).strip().lower()


def _poll_confirmed(status: str) -> bool:
    return status in ('3', 'confirmed', 'confirm')


def _poll_scanned(status: str) -> bool:
    return status in ('2', 'scanned', 'scaned')


def _poll_waiting(status: str) -> bool:
    return status in ('1', 'new', 'waiting', '0', '')


def _poll_expired(status: str) -> bool:
    return status in ('4', '5', 'expired', 'expire', 'timeout')


def douyin_qr_login(
    cookies_path: str = DEFAULT_COOKIE_PATH,
    timeout: float = 180,
    allow_skip: bool = True,
) -> Optional[Dict[str, str]]:
    """Interactive QR login. Returns cookie dict, or None if skipped/failed."""
    global _skip_this_process
    if _skip_this_process:
        return None
    if not _interactive():
        print('非交互环境，无法扫码登录抖音，将使用游客模式。')
        print(f'可手动将 cookies 写入 {cookies_path}，或设置 douyin_dm_cookies: None 跳过提示。')
        _skip_this_process = True
        return None

    if allow_skip:
        print('')
        print('=' * 56)
        print('抖音弹幕登录 cookies 缺失或已失效。')
        print('扫码登录后可获得更完整的礼物等消息；游客模式功能有限。')
        print('按 Enter 开始扫码登录，输入 s 然后 Enter 跳过：')
        print('=' * 56)
        try:
            choice = input('> ').strip().lower()
        except EOFError:
            choice = 's'
        if choice in ('s', 'skip', 'n', 'no'):
            print('已跳过抖音登录，使用游客模式。')
            _skip_this_process = True
            return None

    fp = _generate_fp()
    session = requests.Session()
    headers = _headers()
    params = {
        'next': _NEXT,
        'aid': _AID,
        'service': _SERVICE,
        'is_vcd': '1',
        'fp': fp,
    }
    try:
        resp = session.get('https://sso.douyin.com/get_qrcode/', params=params, headers=headers, timeout=20)
        payload = resp.json()
    except Exception as e:
        print(f'获取抖音登录二维码失败: {e}')
        return None

    data = payload.get('data') or {}
    if payload.get('error_code') not in (0, '0', None) and not data.get('token'):
        print(f'获取抖音登录二维码失败: {payload}')
        return None

    token = data.get('token')
    qr_b64 = data.get('qrcode')
    qr_url = data.get('qrcode_index_url') or ''
    if not token:
        print(f'获取抖音登录二维码失败: 无 token {payload}')
        return None

    qr_path = os.path.join(os.path.dirname(os.path.abspath(cookies_path)) or '.login_info', 'douyin_login_qr.png')
    # Prefer terminal ASCII QR (works over SSH). GUI open is skipped on SSH/headless.
    shown = False
    if qr_url:
        print('请使用抖音 App 扫描下方终端二维码：')
        shown = _print_ascii_qr(qr_url)
        print(f'扫码链接: {qr_url}')
    if qr_b64:
        try:
            _show_qr_image(base64.b64decode(qr_b64), qr_path)
        except Exception as e:
            print(f'保存二维码图片失败: {e}')
    if not shown and not qr_url:
        print('未能在终端绘制二维码，请查看已保存的 PNG，或安装依赖: pip install qrcode')
    elif _is_ssh_or_headless():
        print('(SSH/无图形界面：请直接扫终端中的二维码；字体请用等宽字体)')

    print('扫码后请在手机上确认登录。')
    if allow_skip:
        print('等待扫码中……输入 s 然后 Enter 可跳过。')

    skip_flag = {'v': False}

    def _stdin_skip():
        if not allow_skip:
            return
        try:
            while not skip_flag['v']:
                line = sys.stdin.readline()
                if not line:
                    break
                if line.strip().lower() in ('s', 'skip', 'n'):
                    skip_flag['v'] = True
                    break
        except Exception:
            pass

    skip_thread = threading.Thread(target=_stdin_skip, daemon=True)
    skip_thread.start()

    check_params = {
        'next': _NEXT,
        'token': token,
        'service': _SERVICE + '/?logintype=user&loginapp=douyin&jump=' + _NEXT,
        'correct_service': _SERVICE + '/?logintype=user&loginapp=douyin&jump=' + _NEXT,
        'aid': _AID,
        'is_vcd': '1',
        'fp': fp,
    }
    check_url = 'https://sso.douyin.com/check_qrconnect/'

    deadline = time.time() + timeout
    last_status = None
    while time.time() < deadline:
        if skip_flag['v']:
            print('已跳过抖音登录，使用游客模式。')
            _skip_this_process = True
            return None
        try:
            chk = session.get(check_url, params=check_params, headers=headers, timeout=15)
            body = chk.json()
        except Exception as e:
            logger.debug(f'轮询抖音扫码状态失败: {e}')
            time.sleep(2)
            continue

        st = _status_norm((body.get('data') or {}).get('status'))
        if st != last_status:
            last_status = st
            if _poll_waiting(st):
                print('等待扫码…')
            elif _poll_scanned(st):
                print('已扫码，请在手机上确认登录…')
            elif _poll_expired(st):
                print('二维码已过期，请重新触发登录。')
                return None
            elif _poll_confirmed(st):
                print('已确认，正在保存 cookies…')
            else:
                print(f'扫码状态: {st or body}')

        if _poll_confirmed(st):
            redirect = (body.get('data') or {}).get('redirect_url')
            if redirect:
                try:
                    session.get(redirect, headers=headers, timeout=20, allow_redirects=True)
                except Exception as e:
                    logger.debug(f'跟随 redirect_url 失败: {e}')
            # Touch live domain so live cookies settle
            try:
                session.get('https://live.douyin.com/', headers=_headers('https://live.douyin.com/'), timeout=15)
            except Exception:
                pass
            cookies = _session_cookies_dict(session)
            if not has_login_markers(cookies):
                print('登录完成但未拿到 sessionid，请重试或手动配置 cookies。')
                return None
            save_cookie_dict(cookies_path, cookies)
            print(f'抖音登录成功，cookies 已保存到 {cookies_path}')
            return cookies

        if _poll_expired(st):
            return None
        time.sleep(2)

    print('扫码登录超时，将使用游客模式。')
    return None


def douyin_islogin(cookies_path: str = DEFAULT_COOKIE_PATH, refresh: bool = True) -> bool:
    if not cookies_path or not os.path.isfile(cookies_path):
        return False
    try:
        cookies = load_cookie_dict(cookies_path)
    except Exception:
        return False
    if not has_login_markers(cookies):
        return False
    if refresh:
        try:
            refreshed = refresh_douyin_cookies(cookies)
            if refreshed != cookies and has_login_markers(refreshed):
                save_cookie_dict(cookies_path, refreshed)
                cookies = refreshed
        except Exception as e:
            logger.debug(f'软刷新抖音cookies失败: {e}')
    return validate_douyin_cookies(cookies)


def ensure_douyin_cookies(
    douyin_dm_cookies: Optional[str] = None,
    *,
    force_login: bool = False,
    allow_skip: bool = True,
) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    """Resolve Douyin cookies for danmaku.

    Returns (cookie_dict_or_None, path_or_None).
    None dict means anonymous/synthetic mode.
    """
    global _skip_this_process

    if _is_skip_config(douyin_dm_cookies):
        return None, None

    # Explicit cookie string (not a json path)
    if douyin_dm_cookies and not str(douyin_dm_cookies).strip().endswith('.json') \
            and str(douyin_dm_cookies).strip() not in ('', '~'):
        from DMR.utils.utils import cookiestr2dict
        try:
            return cookiestr2dict(str(douyin_dm_cookies)), None
        except Exception as e:
            logger.warning(f'解析抖音 cookie 字符串失败: {e}')
            return None, None

    path = _normalize_path(douyin_dm_cookies)
    if path is None:
        return None, None

    with _login_lock:
        if _skip_this_process and not force_login:
            return None, path

        if os.path.isfile(path) and not force_login:
            try:
                cookies = load_cookie_dict(path)
            except Exception as e:
                logger.warning(f'读取抖音cookies失败: {e}')
                cookies = {}
            if has_login_markers(cookies):
                try:
                    refreshed = refresh_douyin_cookies(cookies)
                    if has_login_markers(refreshed):
                        if refreshed != cookies:
                            save_cookie_dict(path, refreshed)
                        cookies = refreshed
                except Exception:
                    pass
                if validate_douyin_cookies(cookies):
                    return cookies, path
                print(f'抖音 cookies 已失效 ({path})，需要重新登录。')
            else:
                print(f'抖音 cookies 文件缺少 sessionid ({path})。')

        cookies = douyin_qr_login(path, allow_skip=allow_skip)
        if cookies:
            return cookies, path
        return None, path
